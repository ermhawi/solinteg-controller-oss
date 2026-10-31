"""Read-only release preflight tests; no production device or service writes."""
from __future__ import annotations

import copy
import hashlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import ev_grid_priority_preflight as preflight

NOW = datetime(2026, 10, 28, 20, 56, tzinfo=timezone.utc)
SOURCE = (
    "from __future__ import annotations\n"
    "from zoneinfo import ZoneInfo\n"
    "from l3_charge_guard import observe_l3_house_w, cap_grid_charge_w\n"
    "def main():\n"
    "    if True:\n"
    "            power_w, detail, numbers, l3_limited = guard_l3_charge(\n"
    "                action, power_w, detail, numbers, iteration_now)\n"
).encode()
LIVE = {
    "timestamp": NOW.isoformat(), "grid_unbalanced_output_raw": 0,
    "soc_pct": 64, "battery_temp_c": 15, "pv_w": 0,
    "meter_l1_w": 269, "meter_l2_w": 362, "meter_l3_w": -792,
    "grid_w": -160, "inverter_ac_w": 2199, "house_load_w": 2359,
}
EVCC = {
    "timestamp": NOW.isoformat(), "source": "waveshare_evcc_fc03_readonly",
    "read_ok": True, "state": "C2", "charging_detected": True,
    "physical_control_eligible": False, "writes": 0,
    "pilot_pwm_raw": 100, "configured_pwm_raw": 100,
    "channel_currents_a": [0.1, 6.0, 0.1], "measured_current_a": 6.2,
}


class PreflightTests(unittest.TestCase):
    def inspect(self, source=SOURCE, live=None, evcc=None):
        return preflight.inspect(source, copy.deepcopy(LIVE if live is None else live),
                                 copy.deepcopy(EVCC if evcc is None else evcc), now=NOW,
                                 baseline_sha256=hashlib.sha256(SOURCE).hexdigest())

    def test_matching_source_and_night_sample_only_preview(self):
        r = self.inspect()
        self.assertTrue(r["baseline_verified"])
        self.assertEqual((r["shadow_state"], r["shadow_preview_target_w"]), ("shadow", 50))
        self.assertEqual(r["evcc_ict2_a"], 6.0)
        self.assertFalse(r["physical_activation_authorized"])
        self.assertFalse(r["independent_l3_rms_protection_verified"])

    def test_low_soc_or_daylight_rejects_preview(self):
        for changed in ({"soc_pct": 19}, {"pv_w": 400}):
            with self.subTest(changed=changed):
                r = self.inspect(live={**LIVE, **changed})
                self.assertFalse(r["shadow_eligible_now"])
                self.assertEqual(r["shadow_state"], "unavailable")
                self.assertIsNone(r["shadow_preview_target_w"])

    def test_invalid_numeric_snapshot_fails_closed(self):
        r = self.inspect(live={**LIVE, "soc_pct": 10 ** 400})
        self.assertFalse(r["shadow_eligible_now"])
        self.assertIsNone(r["house_battery_soc_pct"])

    def test_stale_evcc_rejected(self):
        evcc = {**EVCC, "timestamp": (NOW - timedelta(minutes=3)).isoformat()}
        self.assertEqual(self.inspect(evcc=evcc)["shadow_state"], "unavailable")

    def test_unknown_dispatcher_refuses_release_preview(self):
        r = self.inspect(source=SOURCE + b"# changed\n")
        self.assertFalse(r["baseline_verified"])
        self.assertIsNone(r["candidate_sha256"])
        self.assertIsNone(r["shadow_preview_target_w"])

    def test_inspection_does_not_mutate_snapshots(self):
        live, evcc = copy.deepcopy(LIVE), copy.deepcopy(EVCC)
        preflight.inspect(SOURCE, live, evcc, now=NOW,
                          baseline_sha256=hashlib.sha256(SOURCE).hexdigest())
        self.assertEqual((live, evcc), (LIVE, EVCC))


if __name__ == "__main__":
    unittest.main()
