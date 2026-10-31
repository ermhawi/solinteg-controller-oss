"""Solar-aware counterfactual only: no physical control or auto-mode surprises."""
from __future__ import annotations

import copy
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import ev_grid_priority_solar_shadow as shadow

NOW = datetime(2026, 10, 31, 8, 50, tzinfo=timezone.utc)
LIVE = {
    "timestamp": NOW.isoformat(),
    "grid_unbalanced_output_raw": 0,
    "soc_pct": 64,
    "pv_w": 0,
    "battery_w": 2155,
    "inverter_ac_w": 2199,
    "house_load_w": 2359,
    "grid_w": -160,
    "meter_l1_w": 269,
    "meter_l2_w": 362,
    "meter_l3_w": -792,
}
EVCC = {
    "timestamp": NOW.isoformat(),
    "source": "waveshare_evcc_fc03_readonly",
    "read_ok": True,
    "state": "C2",
    "charging_detected": True,
    "physical_control_eligible": False,
    "writes": 0,
    "pilot_pwm_raw": 100,
    "configured_pwm_raw": 100,
    "channel_currents_a": [.1, 6., .1],
    "measured_current_a": 6.2,
}


class SolarShadowTests(unittest.TestCase):
    def call(self, action="discharge", power=2500, live=None, evcc=None, **kw):
        return shadow.assess(action, power,
                             copy.deepcopy(LIVE if live is None else live),
                             copy.deepcopy(EVCC if evcc is None else evcc),
                             now=NOW, **kw)

    def test_night_discharge_recommends_cap_not_inverter_write(self):
        result = self.call()
        self.assertEqual((result.state, result.candidate_action,
                          result.candidate_power_w),
                         ("shadow_hypothesis", "discharge", 50))
        self.assertFalse(result.measurements["model_actionable"])
        self.assertFalse(result.measurements["battery_to_ev_causality_proven"])
        self.assertEqual(result.measurements["physical_writes"], 0)

    def test_idle_night_identifies_auto_gap_without_changing_control(self):
        result = self.call("idle", 0)
        self.assertEqual((result.state, result.candidate_action,
                          result.candidate_power_w),
                         ("shadow_hypothesis", "discharge", 50))
        self.assertTrue(result.measurements["observed_discharge_during_ev"])

    def test_daylight_auto_never_recommends_forced_discharge(self):
        live = {**LIVE, "pv_w": 3039, "battery_w": -336,
                "inverter_ac_w": 2703, "grid_w": -802,
                "house_load_w": 3505, "meter_l1_w": 110,
                "meter_l2_w": 607, "meter_l3_w": -1520}
        result = self.call("idle", 0, live=live)
        self.assertEqual(result.state, "solar_hold_unverified")
        self.assertIsNone(result.candidate_action)
        self.assertIsNone(result.candidate_power_w)
        self.assertFalse(result.measurements["observed_discharge_during_ev"])

    def test_daylight_discharge_only_produces_scenario_not_authorization(self):
        live = {**LIVE, "pv_w": 1000, "inverter_ac_w": 3200,
                "house_load_w": 3360}
        result = self.call("discharge", 2300, live=live)
        self.assertEqual(result.state, "shadow_hypothesis")
        self.assertEqual(result.candidate_power_w, 50)
        self.assertGreater(result.measurements["l3_solar_contribution_w_est"], 0)
        self.assertFalse(result.measurements["model_actionable"])

    def test_low_soc_recovery_has_priority_even_in_auto(self):
        result = self.call("idle", 0, live={**LIVE, "soc_pct": 20})
        self.assertEqual(result.state, "soc_guard")
        self.assertIsNone(result.candidate_action)

    def test_charge_target_not_replaced(self):
        self.assertEqual(self.call("charge", 300).state, "existing_charge")
        self.assertIsNone(self.call("charge", 300).candidate_action)

    def test_missing_dispatch_action_is_explicit(self):
        self.assertEqual(self.call("unknown", 0).state, "unknown_dispatch")

    def test_not_balanced_fails_closed(self):
        for value in (None, 1, True):
            with self.subTest(value=value):
                result = self.call(live={**LIVE, "grid_unbalanced_output_raw": value})
                self.assertEqual(result.state, "unavailable")
                self.assertIsNone(result.candidate_power_w)

    def test_stale_or_missing_phase_rejected(self):
        ev = {**EVCC, "timestamp": (NOW - timedelta(seconds=120)).isoformat()}
        self.assertEqual(self.call(evcc=ev).state, "unavailable")
        self.assertEqual(self.call(live={**LIVE, "meter_l3_w": None}).state,
                         "unavailable")

    def test_missing_ict2_never_invents_zero_current(self):
        ev = {**EVCC, "channel_currents_a": [0, 0, 0],
              "measured_current_a": 0}
        self.assertEqual(self.call(evcc=ev).state, "unavailable")

    def test_pv_battery_ac_inconsistency_rejected(self):
        self.assertEqual(self.call(live={**LIVE, "pv_w": 4000}).state,
                         "unavailable")

    def test_explicit_historical_phase_assumption_never_actionable(self):
        live = {k: v for k, v in LIVE.items()
                if k != "grid_unbalanced_output_raw"}
        result = self.call(live=live, phase_assumed=True)
        self.assertEqual(result.state, "historical_hypothesis")
        self.assertTrue(result.measurements["phase_balance_assumed"])
        self.assertFalse(result.measurements["model_actionable"])

    def test_predicted_phase_import_bound_can_block_scenario(self):
        # Valid present-time phase readings but forcing battery off would
        # exceed the conservative future phase-import envelope.
        live = {**LIVE, "battery_w": 5000, "inverter_ac_w": 5050,
                "house_load_w": 6350, "grid_w": -1300,
                "meter_l1_w": -4100, "meter_l2_w": 3100,
                "meter_l3_w": -300}
        result = self.call(live=live)
        self.assertEqual(result.state, "phase_import_bound")
        self.assertIsNone(result.candidate_power_w)

    def test_zero_physical_control_dependencies(self):
        src = Path(shadow.__file__).read_text().lower()
        for forbidden in ("import pymodbus", "inverter_control",
                          "write_register", "requests.post", "socket."):
            self.assertNotIn(forbidden, src)


if __name__ == "__main__":
    unittest.main()
