"""Gap-aware, current-only AC estimation; all hardware writes are forbidden."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import ev_ac_capture as meter

T = datetime(2026, 10, 28, 19, 15, tzinfo=timezone.utc)


def snapshot(offset: int = 0, *, read_ok=True, state="C2",
             currents=None, raw=100, reported=6.2):
    return {
        "timestamp": (T + timedelta(seconds=offset)).isoformat(),
        "source": "waveshare_evcc_fc03_readonly",
        "read_ok": read_ok,
        "state": state,
        "configured_pwm_raw": raw,
        "pilot_pwm_raw": raw,
        "configured_current_a": 6.0,
        "channel_currents_a": [0.1, 6.0, 0.1] if currents is None else currents,
        "measured_current_a": reported,
        "charging_detected": True,
        "physical_control_eligible": False,
        "modbus_function": "FC03",
        "writes": 0,
    }


def read(offset=0, **kw):
    return meter.parse(snapshot(offset, **kw),
                       now=T + timedelta(seconds=offset + 1))


class EvAcCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "ac.sqlite")

    def tearDown(self):
        self.temp.cleanup()

    def test_only_channel2_is_used_not_observer_sum_with_noise(self):
        row = read()
        self.assertEqual(row["quality"], "estimated")
        self.assertEqual(row["active_current_a"], "6.0")
        self.assertEqual(row["other_channels_a"], ["0.1", "0.1"])
        self.assertEqual(row["configured_pwm_raw"], 100)
        self.assertEqual(row["pilot_pwm_raw"], 100)
        self.assertEqual(row["source_writes"], 0)
        self.assertNotIn("VIN", json.dumps(row).upper())

    def test_two_successful_30s_samples_yield_230v_6a_energy(self):
        self.assertEqual(meter.store(self.db, read()), (True, False))
        self.assertEqual(meter.store(self.db, read(30)), (True, True))
        r = meter.summary(self.db)
        self.assertEqual(r["observations"], 2)
        self.assertEqual(r["covered_intervals"], 1)
        self.assertEqual(r["covered_seconds"], 30)
        self.assertEqual(r["estimated_ac_kwh_covered_only"], 0.0115)
        self.assertIsNone(r["cost_ore"])
        self.assertIsNone(r["vehicle_battery_dc_kwh"])
        self.assertEqual(r["assumed_voltage_v"], 230)
        self.assertEqual(r["assumed_power_factor"], 1)

    def test_more_samples_integrate_only_adjacent_valid_intervals(self):
        meter.store(self.db, read())
        meter.store(self.db, read(30))
        meter.store(self.db, read(60))
        out = meter.summary(self.db)
        self.assertEqual(out["covered_intervals"], 2)
        self.assertEqual(out["estimated_ac_kwh_covered_only"], 0.023)
        self.assertEqual(out["covered_seconds"], 60)
        self.assertEqual(Path(self.db).stat().st_mode & 0o777, 0o600)

    def test_repeated_snapshot_deduplicated_and_conflicts_rejected(self):
        record = read()
        self.assertEqual(meter.store(self.db, record), (True, False))
        self.assertEqual(meter.store(self.db, record), (False, False))
        changed = dict(record, active_current_a="6.2")
        with self.assertRaisesRegex(meter.AcEvidenceError, "conflicting"):
            meter.store(self.db, changed)
        self.assertEqual(meter.summary(self.db)["observations"], 1)

    def test_missed_poll_gap_does_not_add_energy(self):
        meter.store(self.db, read())
        meter.store(self.db, read(65))
        out = meter.summary(self.db)
        self.assertEqual(out["covered_intervals"], 0)
        self.assertIsNone(out["estimated_ac_kwh_covered_only"])

    def test_error_read_breaks_charge_interval_even_short_gap(self):
        meter.store(self.db, read(0))
        meter.store(self.db, read(30, read_ok=False))
        meter.store(self.db, read(60))
        self.assertEqual(meter.summary(self.db)["covered_intervals"], 0)
        meter.store(self.db, read(90))
        self.assertEqual(meter.summary(self.db)["covered_intervals"], 1)

    def test_idle_to_charge_and_charge_to_idle_never_counted(self):
        meter.store(self.db, read(0, state="A1"))
        meter.store(self.db, read(30))
        meter.store(self.db, read(60, state="A1"))
        self.assertEqual(meter.summary(self.db)["covered_intervals"], 0)

    def test_unverified_pwm_breaks_intervals_and_logs_observed_current(self):
        meter.store(self.db, read())
        warn = read(30, raw=166)
        self.assertEqual(warn["quality"], "unverified_current_limit")
        self.assertEqual(warn["active_current_a"], "6.0")
        meter.store(self.db, warn)
        meter.store(self.db, read(60))
        self.assertEqual(meter.summary(self.db)["covered_intervals"], 0)

    def test_other_channel_current_or_invalid_totals_fail_closed(self):
        for bad in (
            {"currents": [1.2, 6.0, 0.1], "reported": 7.3},
            {"currents": [0.1, 6.0, 0.1], "reported": 5.8},
            {"currents": [0.1, 8.5, 0.1], "reported": 8.7},
            {"currents": [None, 6.0, 0.1], "reported": 6.2},
        ):
            with self.subTest(bad=bad):
                value = read(**bad)
                self.assertNotEqual(value["quality"], "estimated")
                self.assertIsNone(value["active_current_a"])

    def test_never_accept_stale_source_future_source_or_write_flag(self):
        with self.assertRaisesRegex(meter.AcEvidenceError, "stale/future"):
            meter.parse(snapshot(), now=T + timedelta(seconds=120))
        with self.assertRaisesRegex(meter.AcEvidenceError, "stale/future"):
            meter.parse(snapshot(15), now=T)
        bad = snapshot()
        bad["writes"] = 1
        with self.assertRaisesRegex(meter.AcEvidenceError, "read-only"):
            meter.parse(bad, now=T)
        bad = snapshot()
        bad["source"] = "untrusted"
        with self.assertRaisesRegex(meter.AcEvidenceError, "source"):
            meter.parse(bad, now=T)

    def test_bad_snapshot_fields_or_timezone_are_rejected(self):
        for value in (None, {}, {"source":"anything"}):
            with self.subTest(value=value), self.assertRaises(meter.AcEvidenceError):
                meter.parse(value, now=T)
        bad = snapshot()
        bad["timestamp"] = "2026-10-28T19:15:00"  # ambiguous timezone
        with self.assertRaisesRegex(meter.AcEvidenceError, "timezone"):
            meter.parse(bad, now=T)

    def test_invalid_out_of_order_is_never_reinterpreted_as_charging(self):
        meter.store(self.db, read(30))
        with self.assertRaisesRegex(meter.AcEvidenceError, "nonmonotonic"):
            meter.store(self.db, read(0))

    def test_summary_does_not_create_database_when_empty(self):
        self.assertFalse(Path(self.db).exists())
        result = meter.summary(self.db)
        self.assertEqual(result["observations"], 0)
        self.assertIsNone(result["estimated_ac_kwh_covered_only"])
        self.assertFalse(Path(self.db).exists())

    def test_code_cannot_control_evcc_or_inverter_or_prices(self):
        source = Path(meter.__file__).read_text()
        for forbidden in ("import evcc_observer", "socket.create_connection",
                          "modbus", "fc10", "requests.post", "charging_start",
                          "import inverter_control", "write_register"):
            if forbidden == "modbus":
                # Comments explicitly mention no Modbus; test executable imports only.
                self.assertNotIn("import modbus", source.lower())
            else:
                self.assertNotIn(forbidden, source.lower())
        self.assertIn("EV_AC_CAPTURE_ENABLED", source)
        self.assertIn("estimated_ac_kwh", source)


if __name__ == "__main__":
    unittest.main()
