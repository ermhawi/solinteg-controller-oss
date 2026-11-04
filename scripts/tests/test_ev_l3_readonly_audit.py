"""Read-only EV/L3 audit must never create a physical control candidate."""
from __future__ import annotations

import copy
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from ev_l3_readonly_audit import audit  # noqa: E402
from test_ev_grid_priority_guard import EVCC, LIVE, NOW  # noqa: E402


class EvL3ReadOnlyAuditTests(unittest.TestCase):
    def call(self, live=None, evcc=None, preview_discharge_w=2300):
        return audit(copy.deepcopy(LIVE if live is None else live),
                     copy.deepcopy(EVCC if evcc is None else evcc),
                     now=NOW, preview_discharge_w=preview_discharge_w)

    def test_verified_six_ampere_shadow_is_a_non_actuating_hypothesis(self):
        before_live = copy.deepcopy(LIVE)
        before_evcc = copy.deepcopy(EVCC)
        result = self.call()
        self.assertEqual(result["preview_state"], "shadow")
        self.assertEqual(result["hypothetical_discharge_w"], 50)
        self.assertEqual(result["estimated_ev_w"], 1380)
        self.assertTrue(result["phase_telemetry_valid"])
        self.assertTrue(result["balanced_output_confirmed"])
        self.assertFalse(result["physical_control_authorized"])
        self.assertFalse(result["model_actionable"])
        self.assertEqual(result["device_writes"], 0)
        self.assertEqual(LIVE, before_live)
        self.assertEqual(EVCC, before_evcc)

    def test_disconnected_paused_pwm_mismatch_never_proposes_control(self):
        disconnected = {**EVCC, "state": "A1", "charging_detected": False,
                        "configured_pwm_raw": 1000, "pilot_pwm_raw": 267,
                        "channel_currents_a": [None, None, None],
                        "measured_current_a": None}
        result = self.call(evcc=disconnected)
        self.assertTrue(result["evcc_read_ok"])
        self.assertTrue(result["evcc_paused_by_pwm"])
        self.assertFalse(result["evcc_pwm_matches"])
        self.assertEqual(result["preview_state"], "unavailable")
        self.assertIsNone(result["hypothetical_discharge_w"])
        self.assertEqual(result["device_writes"], 0)

    def test_stale_evcc_never_means_zero_amps(self):
        stale = {**EVCC, "timestamp": (NOW - timedelta(seconds=90)).isoformat()}
        result = self.call(evcc=stale)
        self.assertEqual(result["preview_state"], "unavailable")
        self.assertIsNone(result["hypothetical_discharge_w"])

    def test_unknown_ct_or_inverter_balance_fails_closed(self):
        for delta in ({"meter_l3_w": None}, {"grid_unbalanced_output_raw": 1},
                      {"grid_unbalanced_output_raw": True}):
            with self.subTest(delta=delta):
                result = self.call(live={**LIVE, **delta})
                self.assertEqual(result["preview_state"], "unavailable")
                self.assertIsNone(result["hypothetical_discharge_w"])

    def test_soc_and_pv_require_night_guard_even_in_shadow(self):
        for delta in ({"soc_pct": 21}, {"pv_w": 900}):
            with self.subTest(delta=delta):
                result = self.call(live={**LIVE, **delta})
                self.assertEqual(result["preview_state"], "unavailable")
                self.assertIsNone(result["hypothetical_discharge_w"])

    def test_invalid_inputs_rejected(self):
        with self.assertRaises(ValueError):
            self.call(preview_discharge_w=0)
        with self.assertRaises(ValueError):
            audit([], EVCC, now=NOW)
        with self.assertRaises(ValueError):
            audit(LIVE, EVCC, now=NOW.replace(tzinfo=None))


if __name__ == "__main__":
    unittest.main()
