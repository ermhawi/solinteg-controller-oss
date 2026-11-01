"""Non-actuating SoC 25% handoff policy and historical replay regression tests."""
from __future__ import annotations

import copy
import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "services"))
import soc25_handoff_shadow as shadow

spec = importlib.util.spec_from_file_location(
    "replay_soc25_handoff", BASE / "tools" / "replay_soc25_handoff.py")
replay_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay_tool)

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
LIVE = {
    "timestamp": NOW.isoformat(), "soc_pct": 24.9, "battery_temp_c": 14.0,
    "battery_w": 0, "pv_w": 200, "house_load_w": 2000,
    "grid_w": -1900, "inverter_ac_w": 100,
    "meter_l1_w": -500, "meter_l2_w": -500, "meter_l3_w": -900,
    "grid_unbalanced_output_raw": 0,
}
EVCC = {
    "timestamp": NOW.isoformat(), "read_ok": True, "state": "B2",
    "configured_pwm_raw": 1000, "pilot_pwm_raw": 267,
    "measured_current_a": 0.3, "charging_detected": False, "writes": 0,
}


class HandoffShadowTests(unittest.TestCase):
    def decide(self, *, action="idle", power_w=0, latched=False,
               live=None, evcc=None, at=NOW, phase_assumed=False):
        return shadow.assess(
            copy.deepcopy(LIVE if live is None else live),
            copy.deepcopy(EVCC if evcc is None else evcc),
            action, power_w, now=at, recovery_latched=latched,
            phase_balance_assumed=phase_assumed)

    def test_proposes_only_non_actuating_300w_handoff_below_25(self):
        advice = self.decide()
        self.assertEqual((advice.state, advice.candidate_action, advice.candidate_power_w),
                         ("shadow_candidate", "charge", 300))
        self.assertTrue(advice.recovery_latched)
        self.assertEqual(advice.evidence["device_writes"], 0)
        self.assertFalse(advice.evidence["physical_control_authorized"])

    def test_does_not_return_to_auto_at_25_while_latched(self):
        live = {**LIVE, "soc_pct": 25.0}
        a = self.decide(live=live, latched=True)
        self.assertEqual((a.state, a.candidate_action), ("shadow_candidate", "charge"))
        at_26 = self.decide(live={**live, "soc_pct": 26.0}, latched=a.recovery_latched)
        self.assertEqual((at_26.state, at_26.candidate_action), ("released", None))
        self.assertFalse(at_26.recovery_latched)

    def test_unlatched_between_25_and_26_does_not_start_new_cycle(self):
        a = self.decide(live={**LIVE, "soc_pct": 25.5})
        self.assertEqual(a.state, "released")
        b = self.decide(live={**LIVE, "soc_pct": 25.0})
        self.assertEqual(b.state, "shadow_candidate")

    def test_preserves_existing_300w_or_faster_charge(self):
        for watts in (300, 1500):
            with self.subTest(watts=watts):
                a = self.decide(action="charge", power_w=watts)
                self.assertEqual((a.state, a.candidate_action), ("existing_charge", None))

    def test_pv_surplus_requires_pv_first_review_not_unverified_hold(self):
        live = {**LIVE, "pv_w": 3100, "house_load_w": 2000,
                "inverter_ac_w": 2600, "grid_w": 600, "battery_w": -400,
                "meter_l1_w": 200, "meter_l2_w": 200, "meter_l3_w": 200}
        a = self.decide(live=live)
        self.assertEqual((a.state, a.candidate_action), ("pv_auto_unverified", None))

    def test_b2_pause_accepts_firmware_pilot_267_not_as_permission(self):
        a = self.decide()
        self.assertEqual(a.state, "shadow_candidate")
        for update in ({"state": "C2"}, {"configured_pwm_raw": 100},
                       {"measured_current_a": 6.0}, {"charging_detected": True},
                       {"writes": 1}):
            with self.subTest(update=update):
                evcc = {**EVCC, **update}
                self.assertEqual(self.decide(evcc=evcc).state, "unavailable")

    def test_stale_samples_and_invalid_ct_fail_closed(self):
        old = (NOW - timedelta(seconds=100)).isoformat()
        for live, evcc in (({**LIVE, "timestamp": old}, EVCC),
                           (LIVE, {**EVCC, "timestamp": old}),
                           ({**LIVE, "meter_l3_w": -200}, EVCC),
                           ({**LIVE, "battery_temp_c": 48}, EVCC),
                           ({**LIVE, "grid_unbalanced_output_raw": 1}, EVCC)):
            with self.subTest(live=live, evcc=evcc):
                a = self.decide(live=live, evcc=evcc)
                self.assertEqual((a.state, a.candidate_action), ("unavailable", None))

    def test_historical_phase_assumption_does_not_claim_physical_permission(self):
        a = self.decide(live={k: v for k, v in LIVE.items()
                              if k != "grid_unbalanced_output_raw"},
                        phase_assumed=True)
        self.assertEqual(a.state, "shadow_candidate")
        self.assertTrue(a.evidence["phase_balance_assumed"])
        self.assertFalse(a.evidence["physical_control_authorized"])

    def test_projected_grid_overload_rejects_candidate(self):
        live = {**LIVE, "grid_w": -4650, "meter_l1_w": -700,
                "meter_l2_w": -800, "meter_l3_w": -3150,
                "inverter_ac_w": 500, "house_load_w": 5150}
        a = self.decide(live=live)
        self.assertEqual((a.state, a.candidate_action), ("unsafe_import", None))

    def test_existing_battery_discharge_increases_projected_import(self):
        live = {**LIVE, "battery_w": 6000, "pv_w": 200,
                "inverter_ac_w": 6150, "grid_w": -600,
                "house_load_w": 6750, "meter_l1_w": -200,
                "meter_l2_w": -200, "meter_l3_w": -200}
        a = self.decide(live=live)
        self.assertEqual((a.state, a.candidate_action), ("unsafe_import", None))


def reading(t: datetime, soc: float, *, battery=0):
    row = dict(LIVE)
    row.update(timestamp=t.isoformat(), soc_pct=soc, battery_w=battery)
    return row


def sample(t: datetime):
    return {"captured_at_utc": t.isoformat(), "evcc_at_utc": t.isoformat(),
            "evcc_read_ok": 1, "evcc_state": "B2",
            "evcc_configured_raw": 1000, "evcc_measured_a": 0.3,
            "evcc_charging_detected": 0, "physical_writes": 0}


def control(t: datetime, action: str, power: int):
    return {"timestamp": t.isoformat(), "planned_action": action,
            "power_w": power, "outcome": "applied"}


class ReplayTests(unittest.TestCase):
    def test_replay_identifies_idle_drop_and_counterfactual(self):
        readings = [reading(NOW + timedelta(seconds=n), soc)
                    for n, soc in ((0, 25), (30, 25), (60, 24), (90, 23))]
        evs = [sample(NOW + timedelta(seconds=n))
               for n in (0, 30, 60, 90)]
        result = replay_tool.replay(readings, evs,
                                    [control(NOW, "idle", 0)])
        self.assertGreaterEqual(result["counts"].get("shadow_candidate", 0), 1)
        self.assertEqual(result["observed_idle_soc_drops"][0]["observed_decline_pct"], 2)
        self.assertEqual(result["device_writes"], 0)
        self.assertFalse(result["physical_control_authorized"])
        self.assertFalse(result["independent_phase_balance_recorded"])

    def test_replay_does_not_infer_paused_charger_from_c2(self):
        ev = sample(NOW)
        ev["evcc_state"] = "C2"
        result = replay_tool.replay([reading(NOW, 24)], [ev],
                                    [control(NOW, "idle", 0)])
        self.assertEqual(result["counts"].get("shadow_candidate", 0), 0)
        self.assertEqual(result["counts"]["unavailable"], 1)

    def test_replay_with_no_correlated_action_never_issues_candidate(self):
        result = replay_tool.replay([reading(NOW, 24)], [sample(NOW)], [])
        self.assertEqual(result["counts"]["unmatched"], 1)
        self.assertEqual(result["device_writes"], 0)


if __name__ == "__main__":
    unittest.main()
