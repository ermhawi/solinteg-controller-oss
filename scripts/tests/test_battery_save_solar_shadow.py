"""Offline-only battery-save solar handoff evidence and failure regressions."""
from __future__ import annotations

import copy
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import battery_save_solar_shadow as policy

NOW = datetime(2026, 11, 2, 12, 55, tzinfo=timezone.utc)
LIVE = {
    "timestamp": NOW.isoformat(), "soc_pct": 22.0, "pv_w": 2250,
    "house_load_w": 850, "battery_w": 150, "grid_w": 1550,
    "inverter_ac_w": 2400, "work_mode_raw": 0x303,
    "grid_unbalanced_output_raw": 0,
    "meter_l1_w": 500, "meter_l2_w": 500, "meter_l3_w": 550,
}


def sample(at: datetime, **updates) -> dict:
    data = {
        "captured_at_utc": at.isoformat(),
        "inverter_at_utc": (at - timedelta(seconds=5)).isoformat(),
        "inverter_fresh": 1, "inverter_age_s": 5.0,
        "house_soc_pct": 22.0, "pv_w": 2250, "house_load_w": 850,
        "battery_w": 150, "grid_w": 1550,
        "l1_grid_w": 500, "l2_grid_w": 500, "l3_grid_w": 550,
    }
    data.update(updates)
    return data


def window() -> list[dict]:
    return [sample(NOW - timedelta(seconds=ago)) for ago in (100, 70, 40, 10)]


class BatterySaveSolarTests(unittest.TestCase):
    def check(self, *, live=None, samples=None, now=NOW, action="discharge",
              power_w=50, confirmed=True, age=300, reserve=20,
              entry=19.5, exit_pct=21):
        return policy.assess(action, power_w,
                             copy.deepcopy(LIVE if live is None else live),
                             copy.deepcopy(window() if samples is None else samples),
                             now=now, dispatch_age_s=age,
                             battery_save_confirmed=confirmed,
                             planner_reserve_pct=reserve,
                             recovery_entry_pct=entry,
                             recovery_exit_pct=exit_pct)

    def test_observed_sun_export_near_22pct_yields_shadow_review_only(self):
        decision = self.check()
        self.assertEqual(decision.state, "solar_capture_candidate")
        self.assertEqual(decision.hypothetical_mode, "review_pv_capture_handoff")
        self.assertEqual(decision.evidence["measured_pv_surplus_w"], 1400)
        self.assertTrue(decision.evidence["history_window_verified"])
        self.assertEqual(decision.evidence["device_writes"], 0)
        self.assertFalse(decision.evidence["model_actionable"])
        self.assertFalse(decision.evidence["physical_control_authorized"])
        self.assertFalse(decision.evidence["pv_to_battery_causality_proven"])

    def test_never_confuse_arbitrary_50w_discharge_with_verified_battery_save(self):
        for action, power, marker in (("discharge", 50, False),
                                      ("charge", 50, True),
                                      ("idle", 50, True),
                                      ("discharge", 0, True)):
            with self.subTest(action=action, power=power, marker=marker):
                self.assertEqual(self.check(action=action, power_w=power,
                                            confirmed=marker).state, "not_verified_save")

    def test_missing_or_invalid_reserve_settings_block_candidate(self):
        for settings in ({"reserve": None}, {"entry": None}, {"exit_pct": None},
                         {"entry": 21, "exit_pct": 20},
                         {"reserve": 99}, {"entry": float("nan")}):
            with self.subTest(settings=settings):
                self.assertEqual(self.check(**settings).state, "thresholds_unverified")

    def test_low_soc_or_recovery_exit_has_priority(self):
        for soc, reserve, exit_pct in ((20.0, 20.0, 21.0),
                                       (21.49, 20.0, 21.0),
                                       (22.0, 23.0, 24.0)):
            with self.subTest(soc=soc, reserve=reserve, exit_pct=exit_pct):
                entry = min(19.5, exit_pct - .5)
                self.assertEqual(
                    self.check(live={**LIVE, "soc_pct": soc},
                               reserve=reserve, exit_pct=exit_pct,
                               entry=entry).state, "reserve_priority")
        self.assertEqual(self.check(live={**LIVE, "soc_pct": 21.5}).state,
                         "solar_capture_candidate")

    def test_approaching_upper_soc_bound_does_not_propose_handoff(self):
        self.assertEqual(self.check(live={**LIVE, "soc_pct": 98}).state, "high_soc")

    def test_stale_future_naive_or_invalid_inverter_data_fail_closed(self):
        for update in (
            {"timestamp": (NOW - timedelta(seconds=61)).isoformat()},
            {"timestamp": (NOW + timedelta(seconds=1)).isoformat()},
            {"timestamp": "2026-11-02T12:55:00"},
            {"pv_w": float("nan")}, {"soc_pct": True},
            {"grid_w": 100}, {"meter_l3_w": 0},
            {"inverter_ac_w": -999}, {"house_load_w": -1},
        ):
            with self.subTest(update=update):
                self.assertEqual(self.check(live={**LIVE, **update}).state,
                                 "unavailable")

    def test_manual_or_auto_mode_and_unverified_balancing_block(self):
        for update in ({"work_mode_raw": 0x101},
                       {"work_mode_raw": 0x302},
                       {"work_mode_raw": None},
                       {"grid_unbalanced_output_raw": 1},
                       {"grid_unbalanced_output_raw": None}):
            with self.subTest(update=update):
                self.assertEqual(self.check(live={**LIVE, **update}).state,
                                 "mode_unverified")

    def test_stale_control_provenance_prevents_candidate(self):
        for age in (None, -1, 901):
            with self.subTest(age=age):
                self.assertEqual(self.check(age=age).state, "unavailable")

    def test_requires_four_quality_checked_distinct_samples(self):
        items = window()
        cases = (
            items[:3],
            [{**x, "inverter_at_utc": items[0]["inverter_at_utc"]} for x in items],
            [{**x, "inverter_age_s": 61} if i == 2 else x
             for i, x in enumerate(items)],
            [{**x, "inverter_fresh": 0} if i == 2 else x
             for i, x in enumerate(items)],
            [{**x, "l3_grid_w": None} if i == 2 else x
             for i, x in enumerate(items)],
            [{**x, "house_soc_pct": 19} if i == 2 else x
             for i, x in enumerate(items)],
            [{**x, "pv_w": 850} if i == 2 else x
             for i, x in enumerate(items)],
        )
        for bad in cases:
            with self.subTest(bad=bad):
                self.assertEqual(self.check(samples=bad).state, "await_solar_stability")

    def test_short_new_dispatch_or_stale_history_never_claim_stability(self):
        self.assertEqual(self.check(age=60).state, "await_solar_stability")
        self.assertEqual(self.check(now=NOW + timedelta(seconds=70),
                                    live={**LIVE, "timestamp": (
                                        NOW + timedelta(seconds=65)).isoformat()}
                                    ).state, "await_solar_stability")
        shifted = [sample(NOW - timedelta(seconds=s)) for s in (180, 150, 120, 90)]
        self.assertEqual(self.check(samples=shifted).state, "await_solar_stability")

    def test_sun_just_appeared_waits_for_stability(self):
        items = window()
        items[2]["pv_w"] = 200
        items[2]["grid_w"] = -500
        items[2]["l1_grid_w"] = -200
        items[2]["l2_grid_w"] = -200
        items[2]["l3_grid_w"] = -100
        self.assertEqual(self.check(samples=items).state, "await_solar_stability")

    def test_low_pv_or_missing_export_keeps_economic_hold_untouched(self):
        no_sun = {**LIVE, "pv_w": 450, "grid_w": -350,
                  "inverter_ac_w": 500, "battery_w": 50,
                  "meter_l1_w": -100, "meter_l2_w": -100, "meter_l3_w": -150}
        self.assertEqual(self.check(live=no_sun).state, "no_stable_solar_opportunity")
        no_export = {**LIVE, "grid_w": 100, "battery_w": -1300,
                     "inverter_ac_w": 950,
                     "meter_l1_w": 50, "meter_l2_w": 20, "meter_l3_w": 30}
        self.assertEqual(self.check(live=no_export).state, "already_charging")

    def test_unexpected_renewed_discharge_is_an_anomaly_not_auto_permission(self):
        high_discharge = {**LIVE, "battery_w": 650, "grid_w": 2050,
                          "inverter_ac_w": 2900,
                          "meter_l1_w": 650, "meter_l2_w": 700, "meter_l3_w": 700}
        decision = self.check(live=high_discharge)
        self.assertEqual(decision.state, "solar_discharge_anomaly")
        self.assertIsNone(decision.hypothetical_mode)

    def test_current_battery_charge_is_preserved(self):
        already = {**LIVE, "battery_w": -300, "grid_w": 1100,
                   "inverter_ac_w": 1950,
                   "meter_l1_w": 350, "meter_l2_w": 350, "meter_l3_w": 400}
        self.assertEqual(self.check(live=already).state, "already_charging")

    def test_ignores_evcc_data_and_never_imports_physical_control(self):
        src = Path(policy.__file__).read_text(encoding="utf-8").lower()
        for forbidden in ("pymodbus", "inverter_control", "write_register",
                          "socket.", "requests.", "subprocess."):
            self.assertNotIn(forbidden, src)


if __name__ == "__main__":
    unittest.main()
