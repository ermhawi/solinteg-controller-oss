"""EVCC physical qualification safety gates and simulated two-command sequence."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import evcc_physical_qualification as qual  # noqa: E402
from evcc_ascii import RAW_PAUSE, EvccUncertainWrite

NOW = datetime(2026, 11, 4, 21, 47, tzinfo=timezone.utc)
VIN = "a" * 64


class PhysicalQualificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.grid = self.root / "live.json"
        self.evcc = {
            "timestamp": NOW.isoformat(), "source": "waveshare_evcc_fc03_readonly",
            "read_ok": True, "writes": 0, "physical_control_eligible": False,
            "state": "C2", "configured_pwm_raw": 100,
            "pilot_pwm_raw": 100, "channel_currents_a": [0.1, 6.0, 0.1],
        }
        self.write("evcc-readonly.json", self.evcc)
        self.write("status.json", {
            "connected": True, "target_soc_pct": 80, "battery_soc_pct": 66.0,
            "checked_at_utc": NOW.isoformat(),
            "energy_api_received_at_utc": NOW.isoformat(),
        })
        self.grid.write_text(json.dumps({
            "timestamp": NOW.isoformat(), "grid_w": -2500,
            "meter_l3_w": -1600, "grid_unbalanced_output_raw": 0}))
        (self.root / "evcc-reader.lock").touch(mode=0o644)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, record):
        (self.root / name).write_text(json.dumps(record))

    def check(self, *, cable=True, smart="off"):
        cable_record = {
            "status": "connected", "observed_state": "C2",
            "connection_epoch": 0, "coverage_epoch": 0,
        } if cable else None
        with patch.object(qual.cable_tracker, "read_fresh_connected",
                          return_value=cable_record), \
             patch.object(qual.smart, "read_smart_charging",
                          return_value=(smart, "diagnostic off")), \
             patch.object(qual.owner, "read_owner", return_value=None):
            return qual.preflight(now=NOW, directory=self.root,
                                  grid_path=self.grid, expected_vin_hash=VIN)

    def test_qualified_baseline_does_not_write(self):
        out = self.check()
        self.assertTrue(out["ready"])
        self.assertEqual(out["device_writes"], 0)

    def test_unknown_smart_charging_and_cable_block(self):
        self.assertFalse(self.check(smart="unknown")["ready"])
        self.assertFalse(self.check(smart="on")["ready"])
        self.assertFalse(self.check(cable=False)["ready"])

    def test_l3_fuse_budget_and_cable_state_are_required(self):
        self.grid.write_text(json.dumps({
            "timestamp": NOW.isoformat(), "grid_w": -2500,
            "meter_l3_w": -3100, "grid_unbalanced_output_raw": 0}))
        self.assertFalse(self.check()["ready"])
        self.evcc["state"] = "A1"
        self.write("evcc-readonly.json", self.evcc)
        self.assertFalse(self.check()["ready"])

    def test_physical_test_requires_all_independent_flags(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(PermissionError):
                qual.run_physical(expected_vin_hash=VIN, host="127.0.0.1",
                                  directory=self.root)

    def test_simulated_pause_then_resume_once_under_locks(self):
        current = SimpleNamespace(state="C2", configured_raw=100,
                                  pilot_raw=100, currents_a=(0.1, 6.0, 0.1))
        paused = SimpleNamespace(state="B2", configured_raw=RAW_PAUSE,
                                 pilot_raw=267, currents_a=(0.1, .1, 0.1))
        calls = []

        class Fake:
            def __init__(self, *args, **kwargs):pass

            def status(self):return current

            def set_limit_raw(self, raw, *, expected_raw):
                calls.append((raw, expected_raw))
                return paused if raw == RAW_PAUSE else current

        flags = {"EVCC_PHYSICAL_QUALIFICATION_APPROVED": "1",
                 "EV_ENABLED": "1", "EVCC_CONTROL_ARMED": "1",
                 "EVCC_MANUAL_TEST": "1", "EVCC_START_APPROVED": "1",
                 "INVERTER_DATA_PATH": str(self.grid)}
        with patch.dict(os.environ, flags, clear=True), \
             patch.object(qual, "preflight", return_value={"ready": True}), \
             patch.object(qual, "EvccAscii", Fake), \
             patch.object(qual.time, "sleep"):
            out = qual.run_physical(expected_vin_hash=VIN, host="127.0.0.1",
                                    directory=self.root)
        self.assertEqual(out["device_writes"], 2)
        self.assertTrue(out["physically_qualified"])
        self.assertEqual(calls, [(RAW_PAUSE, 100), (100, RAW_PAUSE)])

    def test_uncertain_first_fc10_never_retries_or_resumes(self):
        calls = []

        class Bad:
            def __init__(self, *args, **kwargs):pass

            def status(self):
                return SimpleNamespace(state="C2", configured_raw=100,
                                       pilot_raw=100, currents_a=(0.1, 6.0, 0.1))

            def set_limit_raw(self, raw, *, expected_raw):
                calls.append(raw)
                raise EvccUncertainWrite("simulated uncertain FC10")

        flags = {"EVCC_PHYSICAL_QUALIFICATION_APPROVED": "1",
                 "EV_ENABLED": "1", "EVCC_CONTROL_ARMED": "1",
                 "EVCC_MANUAL_TEST": "1", "EVCC_START_APPROVED": "1",
                 "INVERTER_DATA_PATH": str(self.grid)}
        with patch.dict(os.environ, flags, clear=True), \
             patch.object(qual, "preflight", return_value={"ready": True}), \
             patch.object(qual, "EvccAscii", Bad):
            with self.assertRaises(qual.PauseOutcomeUncertain):
                qual.run_physical(expected_vin_hash=VIN, host="127.0.0.1",
                                  directory=self.root)
        self.assertEqual(calls, [RAW_PAUSE])


if __name__ == "__main__":
    unittest.main()
