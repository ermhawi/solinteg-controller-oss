"""EVCC Ict2 controls evidence for shadow L3 demand; never actuates devices."""
from __future__ import annotations

import copy
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

import ev_l3_metered_policy as policy  # noqa: E402
from test_ev_grid_priority_guard import EVCC, LIVE, NOW  # noqa: E402


def charger(amps: float, *, pwm_raw: int, live: dict = LIVE):
    ev = {**EVCC, "configured_pwm_raw": pwm_raw, "pilot_pwm_raw": pwm_raw,
          "channel_currents_a": [.1, amps, .1],
          "measured_current_a": round(amps + .2, 2)}
    extra = round((amps - 6) * policy.EV_NOMINAL_V)
    inv = {**live, "meter_l3_w": live["meter_l3_w"] - extra,
           "grid_w": live["grid_w"] - extra,
           "house_load_w": live["house_load_w"] + extra}
    return inv, ev


class EvL3MeteredPolicyTests(unittest.TestCase):
    def call(self, action="discharge", target=2300, live=None, evcc=None):
        return policy.assess(action, target,
                             copy.deepcopy(LIVE if live is None else live),
                             copy.deepcopy(EVCC if evcc is None else evcc), now=NOW)

    def test_verified_six_amp_measured_not_inferred_from_l3_threshold(self):
        original = (copy.deepcopy(LIVE), copy.deepcopy(EVCC))
        r = self.call()
        self.assertEqual((r.state, r.candidate_action, r.candidate_power_w),
                         ("shadow_hypothesis", "discharge", 50))
        self.assertEqual(r.measurements["ev_ict2_a"], 6)
        self.assertEqual(r.measurements["ev_power_w_est"], 1380)
        self.assertEqual(r.measurements["non_ev_l3_w_est"], 145)
        self.assertEqual(r.measurements["estimated_battery_reduction_w"], 2250)
        self.assertFalse(r.measurements["model_actionable"])
        self.assertFalse(r.measurements["physical_control_authorized"])
        self.assertFalse(r.measurements["ev_power_metered"])
        self.assertEqual(r.measurements["device_writes"], 0)
        self.assertEqual(original, (LIVE, EVCC))

    def test_ten_amp_variable_pilot_and_current(self):
        live, evcc = charger(10, pwm_raw=167)
        r = self.call(live=live, evcc=evcc)
        self.assertEqual(r.state, "shadow_hypothesis")
        self.assertEqual(r.measurements["ev_power_w_est"], 2300)
        self.assertEqual(r.candidate_power_w, 50)
        self.assertEqual(r.measurements["ev_configured_limit_a"], 10.02)

    def test_sixteen_amp_is_measured_but_soft_grid_import_bound_blocks_control(self):
        live, evcc = charger(16, pwm_raw=267)
        r = self.call(live=live, evcc=evcc)
        self.assertEqual(r.state, "phase_import_bound")
        self.assertEqual(r.measurements["ev_ict2_a"], 16)
        self.assertEqual(r.measurements["ev_power_w_est"], 3680)
        self.assertGreater(r.measurements["predicted_phase_import_w"][2],
                           policy.MAX_PHASE_IMPORT_W)
        self.assertIsNone(r.candidate_power_w)

    def test_eighteen_amp_is_measured_but_not_assumed_safe(self):
        live, evcc = charger(18, pwm_raw=300)
        r = self.call(live=live, evcc=evcc)
        self.assertEqual(r.measurements["ev_power_w_est"], 4140)
        self.assertEqual(r.state, "phase_import_bound")
        self.assertIsNone(r.candidate_action)

    def test_evcc_not_charging_with_large_l3_appliance_is_not_an_ev(self):
        appliance = {**LIVE, "meter_l3_w": -3200, "grid_w": -2568,
                     "house_load_w": 4767}
        paused = {**EVCC, "state": "A1", "charging_detected": False,
                  "configured_pwm_raw": 1000, "pilot_pwm_raw": 267,
                  "channel_currents_a": [None, None, None],
                  "measured_current_a": None}
        r = self.call(live=appliance, evcc=paused)
        self.assertEqual(r.state, "no_verified_charging")
        self.assertNotIn("ev_power_w_est", r.measurements)
        self.assertIsNone(r.candidate_power_w)

    def test_evcc_missing_or_stale_never_means_zero_current(self):
        for ev in ({**EVCC, "read_ok": False},
                   {**EVCC, "timestamp": (NOW - timedelta(seconds=90)).isoformat()},
                   {**EVCC, "state": "C2", "channel_currents_a": [None, None, None]}):
            with self.subTest(ev=ev):
                r = self.call(evcc=ev)
                self.assertEqual(r.state, "unavailable")
                self.assertIsNone(r.candidate_power_w)
                self.assertNotIn("ev_power_w_est", r.measurements)

    def test_pilot_mismatch_pause_and_overcurrent_rejected(self):
        for ev in ({**EVCC, "configured_pwm_raw": 1000, "pilot_pwm_raw": 267},
                   {**EVCC, "pilot_pwm_raw": 267},
                   {**EVCC, "channel_currents_a": [.1, 10, .1],
                    "measured_current_a": 10.2},
                   {**EVCC, "configured_pwm_raw": True, "pilot_pwm_raw": True}):
            with self.subTest(ev=ev):
                self.assertEqual(self.call(evcc=ev).state, "unavailable")

    def test_cross_phase_or_forged_sum_never_used(self):
        for channels, total in [([6., 0., 0.], 6.), ([.8, 6., 0.], 6.8),
                                ([.1, 6., .1], 18.), ([.1, float("nan"), .1], 0.)]:
            with self.subTest(channels=channels):
                ev = {**EVCC, "channel_currents_a": channels,
                      "measured_current_a": total}
                self.assertEqual(self.call(evcc=ev).state, "unavailable")

    def test_fake_source_or_write_signal_rejected(self):
        for extra in ({"source": "untrusted"}, {"writes": 1},
                      {"physical_control_eligible": True},
                      {"charging_detected": None}):
            with self.subTest(extra=extra):
                self.assertEqual(self.call(evcc={**EVCC, **extra}).state,
                                 "unavailable")

    def test_unbalanced_or_missing_ct_never_returns_candidate(self):
        for live in ({**LIVE, "grid_unbalanced_output_raw": 1},
                     {**LIVE, "grid_unbalanced_output_raw": True},
                     {**LIVE, "meter_l3_w": None},
                     {**LIVE, "grid_w": -9999},
                     {**LIVE, "inverter_ac_w": float("nan")},
                     {**LIVE, "battery_w": 9000}):
            with self.subTest(live=live):
                r = self.call(live=live)
                self.assertEqual(r.state, "unavailable")
                self.assertEqual(r.measurements["ev_power_w_est"], 1380)
                self.assertIsNone(r.candidate_power_w)

    def test_soc_temperature_and_solar_are_vetoes_not_current_meters(self):
        for live, state in [({**LIVE, "soc_pct": 20}, "soc_or_bms_guard"),
                            ({**LIVE, "soc_pct": 97}, "soc_or_bms_guard"),
                            ({**LIVE, "battery_temp_c": 7}, "soc_or_bms_guard"),
                            ({**LIVE, "pv_w": 200, "inverter_ac_w": 2300,
                              "house_load_w": 2460}, "solar_unverified")]:
            with self.subTest(state=state):
                r = self.call(live=live)
                self.assertEqual(r.state, state)
                self.assertEqual(r.measurements["ev_power_w_est"], 1380)
                self.assertIsNone(r.candidate_action)

    def test_charge_solar_charge_and_holds_are_never_overridden(self):
        for action in ("charge", "solar_charge", "freeze", "reserve_hold"):
            with self.subTest(action=action):
                r = self.call(action=action, target=1500)
                self.assertEqual(r.state, "preserve_existing_action")
                self.assertIsNone(r.candidate_power_w)

    def test_idle_requires_stability_not_instant_mode_change(self):
        r = self.call(action="idle", target=0)
        self.assertEqual(r.state, "idle_stability_unverified")
        self.assertIsNone(r.candidate_power_w)

    def test_non_ev_l3_load_can_keep_some_battery_discharge(self):
        house = {**LIVE, "meter_l1_w": -300, "meter_l2_w": -400,
                 "meter_l3_w": -800, "grid_w": -1500,
                 "inverter_ac_w": 2400, "house_load_w": 3900,
                 "battery_w": 2360}
        r = self.call(live=house, target=2500)
        self.assertEqual(r.state, "shadow_hypothesis")
        self.assertGreater(r.candidate_power_w, 50)
        self.assertLess(r.candidate_power_w, 2500)

    def test_no_increase_or_auto_on_already_small_target(self):
        for target in (0, 25, 50):
            with self.subTest(target=target):
                r = self.call(target=target)
                self.assertEqual(r.state, "already_limited")
                self.assertIsNone(r.candidate_power_w)

    def test_missing_temperature_is_not_assumed_safe(self):
        r = self.call(live={k: v for k, v in LIVE.items()
                            if k != "battery_temp_c"})
        self.assertEqual(r.state, "unavailable")
        self.assertIsNone(r.candidate_power_w)

    def test_invalid_inputs_and_naive_clock_are_not_control(self):
        for now, target in ((NOW.replace(tzinfo=None), 2300),
                            (NOW, -1)):
            with self.subTest(now=now, target=target):
                r = policy.assess("discharge", target, LIVE, EVCC, now=now)
                self.assertEqual(r.state, "unavailable")
                self.assertEqual(r.measurements["device_writes"], 0)

    def test_policy_has_no_actuation_or_filesystem_imports(self):
        source = Path(policy.__file__).read_text()
        for forbidden in ("import socket", "import sqlite3", "import inverter_control",
                          "import evcc_ascii", "write_register", "set_limit_raw",
                          "return_to_auto", "open("):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
