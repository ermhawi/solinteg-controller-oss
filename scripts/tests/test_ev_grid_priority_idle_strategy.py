"""Non-actuating Auto/idle solar hysteresis and historical sample-quality tests."""
from __future__ import annotations

import copy
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import ev_grid_priority_idle_strategy as policy
import ev_grid_priority_solar_shadow as solar

NOW = datetime(2026, 10, 31, 11, 0, tzinfo=timezone.utc)
BASE = {
    "timestamp": NOW.isoformat(),
    "pv_w": 0, "battery_w": 2155, "house_load_w": 2359,
    "work_mode_raw": 0x101,
    "grid_w": -160, "meter_l1_w": 269, "meter_l2_w": 362,
    "meter_l3_w": -792,
}
ADVICE = solar.ShadowAdvice("shadow_hypothesis", "idle", 0, "shadow", {
    "phase_balance_assumed": False, "ev_w_est": 1380,
})


def sample(at: datetime, *, pv=0, load=2359, battery=2155, valid=True):
    return {
        "captured_at_utc": at.isoformat(),
        "inverter_fresh": 1, "inverter_age_s": 12.0,
        "evcc_record_fresh": 1, "evcc_age_s": 11.0,
        "evcc_read_ok": 1, "evcc_state": "C2",
        "evcc_charging_detected": 1, "evcc_6a_limit_verified": int(valid),
        "evcc_measured_a": 6.2, "battery_w": battery,
        "house_soc_pct": 64, "pv_w": pv, "house_load_w": load,
        "grid_w": -160, "l1_grid_w": 269, "l2_grid_w": 362,
        "l3_grid_w": -792,
    }


def window(*, pv=0, load=2359, battery=2155):
    return [sample(NOW - timedelta(seconds=s), pv=pv, load=load, battery=battery)
            for s in (100, 70, 40, 10)]


class AutoIdleStrategyTests(unittest.TestCase):
    def call(self, *, live=None, advice=ADVICE, samples=None,
             action="idle", age=300):
        return policy.evaluate(action,
                               copy.deepcopy(BASE if live is None else live),
                               advice,
                               copy.deepcopy(window() if samples is None else samples),
                               now=NOW, age_of_dispatch_s=age)

    def test_sustained_night_ev_discharge_proposes_tested_50w_shadow(self):
        r = self.call()
        self.assertEqual(r.state, "battery_save_candidate")
        self.assertEqual(r.hypothetical_mode, "tested_50w_battery_save")
        self.assertEqual(r.measurements["hypothetical_battery_save_target_w"], 50)
        self.assertTrue(r.measurements["stable_deficit"])
        self.assertFalse(r.measurements["model_actionable"])
        self.assertFalse(r.measurements["physical_control_authorized"])
        self.assertEqual(r.measurements["device_writes"], 0)
        self.assertFalse(r.measurements["battery_to_ev_causality_proven"])

    def test_single_battery_spike_is_not_hysteresis_proof(self):
        items = window()
        items[1]["battery_w"] = -300
        r = self.call(samples=items)
        self.assertEqual((r.state, r.hypothetical_mode),
                         ("await_deficit_stability", None))

    def test_projected_l3_import_blocks_even_a_tested_50w_hold(self):
        live = {**BASE, "meter_l1_w": 450, "meter_l2_w": 250,
                "meter_l3_w": -3200, "grid_w": -2500}
        r = self.call(live=live)
        self.assertEqual((r.state, r.hypothetical_mode),
                         ("projected_grid_import_high", None))
        self.assertGreater(r.measurements["projected_phase_import_w"][2], 3200)

    def test_projected_total_grid_import_blocks_even_with_stable_ev(self):
        live = {**BASE, "meter_l1_w": -1900, "meter_l2_w": -1900,
                "meter_l3_w": -2100, "grid_w": -5900}
        r = self.call(live=live)
        self.assertEqual(r.state, "projected_grid_import_high")
        self.assertGreater(r.measurements["projected_total_import_w"], 6500)

    def test_new_idle_dispatch_requires_settling_before_switch(self):
        r = self.call(age=60)
        self.assertEqual((r.state, r.hypothetical_mode),
                         ("await_deficit_stability", None))

    def test_missing_phase_ct_or_6a_check_denies_stability(self):
        for corruption in ({"l3_grid_w": None}, {"evcc_6a_limit_verified": 0},
                           {"evcc_measured_a": None}, {"inverter_age_s": 120}):
            with self.subTest(corruption=corruption):
                items = window()
                items[2].update(corruption)
                self.assertEqual(self.call(samples=items).state,
                                 "await_deficit_stability")

    def test_discontinuous_old_or_future_sample_is_not_stable(self):
        for stamps in ((300, 70, 40, 10), (100, 70, 40, -5),
                       (170, 140, 110, 80)):
            with self.subTest(stamps=stamps):
                items = [sample(NOW - timedelta(seconds=s)) for s in stamps]
                self.assertEqual(self.call(samples=items).state,
                                 "await_deficit_stability")

    def test_missing_history_is_not_evidence_of_discharge(self):
        for items in ([], window()[:2]):
            with self.subTest(length=len(items)):
                self.assertEqual(self.call(samples=items).state,
                                 "await_deficit_stability")

    def test_enough_pv_for_whole_house_keeps_auto_after_stable_window(self):
        live = {**BASE, "pv_w": 3600, "house_load_w": 2500, "battery_w": 200}
        items = window(pv=3600, load=2500, battery=200)
        r = self.call(live=live, samples=items)
        self.assertEqual((r.state, r.hypothetical_mode),
                         ("solar_surplus_keep_auto", "keep_auto"))
        self.assertTrue(r.measurements["stable_total_solar_surplus"])

    def test_discharge_during_sustained_whole_house_solar_surplus_is_anomaly(self):
        live = {**BASE, "pv_w": 3600, "house_load_w": 2500, "battery_w": 650}
        items = window(pv=3600, load=2500, battery=650)
        r = self.call(live=live, samples=items)
        self.assertEqual((r.state, r.hypothetical_mode),
                         ("solar_surplus_discharge_anomaly", None))
        self.assertFalse(r.measurements["model_actionable"])

    def test_solar_appeared_only_in_latest_sample_wait_for_release(self):
        live = {**BASE, "pv_w": 3600, "house_load_w": 2500, "battery_w": 650}
        items = window(pv=300, load=2500, battery=650)
        r = self.call(live=live, samples=items)
        self.assertEqual((r.state, r.hypothetical_mode),
                         ("await_solar_stability", None))

    def test_solar_funding_non_ev_house_is_unverified_charge_experiment(self):
        live = {**BASE, "pv_w": 1500, "house_load_w": 2400, "battery_w": 600}
        items = window(pv=1500, load=2400, battery=600)
        r = self.call(live=live, samples=items)
        self.assertEqual(r.state, "pv_first_charge_unverified")
        self.assertIsNone(r.hypothetical_mode)
        self.assertGreater(r.measurements["non_ev_house_solar_headroom_w"], 350)
        self.assertFalse(r.measurements["model_actionable"])
        self.assertTrue(r.measurements["stable_non_ev_solar_surplus"])

    def test_solar_priority_requires_sustained_pv(self):
        live = {**BASE, "pv_w": 1500, "house_load_w": 2400, "battery_w": 600}
        items = window(pv=0, load=2400, battery=600)
        self.assertEqual(self.call(live=live, samples=items).state,
                         "await_solar_stability")

    def test_narrow_solar_boundary_does_not_chase_pv(self):
        live = {**BASE, "pv_w": 1000, "house_load_w": 2400, "battery_w": 600}
        r = self.call(live=live)
        self.assertEqual(r.state, "solar_boundary_uncertain")
        self.assertIsNone(r.hypothetical_mode)

    def test_observed_charging_is_preserved_without_source_attribution(self):
        live = {**BASE, "pv_w": 1200, "battery_w": -300}
        r = self.call(live=live)
        self.assertEqual((r.state, r.hypothetical_mode),
                         ("observed_battery_charging", "keep_auto"))

    def test_soc_floor_has_absolute_priority(self):
        advice = solar.ShadowAdvice("soc_guard", "idle", 0, "low SOC", {})
        r = self.call(advice=advice)
        self.assertEqual((r.state, r.hypothetical_mode),
                         ("soc_recovery_priority", None))

    def test_manual_inverter_mode_overrides_stale_idle_log(self):
        for raw in (0x303, 0x302, None, True):
            with self.subTest(raw=raw):
                live = {**BASE, "work_mode_raw": raw}
                r = self.call(live=live)
                self.assertEqual((r.state, r.hypothetical_mode),
                                 ("dispatch_mode_mismatch", None))

    def test_unknown_evcc_or_phase_balance_blocks_idle_strategy(self):
        for state in ("unavailable", "off", "not_applicable"):
            with self.subTest(state=state):
                advice = solar.ShadowAdvice(state, "idle", 0, "untrusted", {})
                self.assertEqual(self.call(advice=advice).state, "unavailable")
        assumed = solar.ShadowAdvice("shadow_hypothesis", "idle", 0,
                                    "historical", {"phase_balance_assumed": True})
        self.assertEqual(self.call(advice=assumed).state, "unavailable")

    def test_existing_charge_and_forced_discharge_are_not_overridden(self):
        for action in ("charge", "discharge", "unknown"):
            with self.subTest(action=action):
                r = self.call(action=action)
                self.assertEqual((r.state, r.hypothetical_mode),
                                 ("not_idle", None))

    def test_no_device_write_imports_or_hardware_control(self):
        src = Path(policy.__file__).read_text().lower()
        for forbidden in ("pymodbus", "inverter_control",
                          "write_register", "socket.", "requests.", "subprocess."):
            self.assertNotIn(forbidden, src)


if __name__ == "__main__":
    unittest.main()
