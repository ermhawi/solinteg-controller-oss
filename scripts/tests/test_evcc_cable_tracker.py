"""Offline EVCC cable/replug tracking; only FC03 snapshots, never commands."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import evcc_cable_tracker as cable  # noqa: E402

START = datetime(2026, 11, 4, 8, 40, tzinfo=timezone.utc)


def fc03(state: str, seconds: int, **changes) -> dict:
    row = {
        "timestamp": (START + timedelta(seconds=seconds)).isoformat(),
        "source": "waveshare_evcc_fc03_readonly",
        "read_ok": True,
        "writes": 0,
        "physical_control_eligible": False,
        "state": state,
        # Real 2026-11-04 B2 observations when PWM=1000 had pilot=267.
        "configured_pwm_raw": 1000,
        "pilot_pwm_raw": 267,
        "channel_currents_a": [0.0, 0.1, 0.0],
    }
    row.update(changes)
    return row


def ingest(prev: dict | None, state: str, seconds: int, **changes) -> dict:
    return cable.advance(prev, fc03(state, seconds, **changes),
                         now=START + timedelta(seconds=seconds + 1))


class CableTrackingTests(unittest.TestCase):
    def test_recorded_a1_to_b2_recognized_even_with_267_pilot(self):
        record = ingest(None, "A1", 0)
        self.assertEqual(record["status"], "unknown")
        record = ingest(record, "A1", 30)
        self.assertEqual(record["status"], "disconnected")
        record = ingest(record, "B2", 60)
        self.assertEqual(record["status"], "disconnected")
        self.assertEqual(record["candidate_status"], "connected")
        record = ingest(record, "B2", 90)
        self.assertEqual(record["status"], "connected")
        self.assertEqual(record["connection_epoch"], 1)
        self.assertTrue(record["confirmed_replug"])
        self.assertEqual(record["last_transition"], "reconnected")
        self.assertFalse(record["lossless"])
        self.assertFalse(record["physical_control_eligible"])
        self.assertEqual(record["device_writes"], 0)

    def test_initial_connection_is_not_replug(self):
        record = ingest(None, "C2", 0)
        record = ingest(record, "C2", 30)
        self.assertEqual(record["status"], "connected")
        self.assertEqual(record["connection_epoch"], 0)
        self.assertFalse(record["confirmed_replug"])
        self.assertIsNone(record["last_reconnected_at_utc"])

    def test_connected_unplugged_connected_increments_epoch(self):
        record = None
        for i, state in enumerate(("C2", "C2", "A1", "A1", "B2", "B2",
                                   "C2", "A1", "A1", "C2", "C2")):
            record = ingest(record, state, i * 30)
        self.assertEqual(record["connection_epoch"], 2)
        self.assertTrue(record["confirmed_replug"])

    def test_single_a1_never_qualifies_connected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cable.json"
            record = ingest(ingest(None, "C2", 0), "C2", 30)
            record = ingest(record, "A1", 60)
            cable.store(path, record)
            self.assertIsNone(cable.read_fresh_connected(
                path, now=START + timedelta(seconds=61)))
            self.assertEqual(record["status"], "connected")
            self.assertEqual(record["candidate_status"], "disconnected")

    def test_restart_keeps_epoch_and_idempotent_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cable.json"
            for sec, state in ((0, "A1"), (30, "A1"), (60, "B2"), (90, "B2")):
                cable.update(path, fc03(state, sec),
                             now=START + timedelta(seconds=sec + 1))
            first = cable.read(path)
            self.assertEqual(first["connection_epoch"], 1)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            duplicate = cable.update(path, fc03("A1", 90),
                                     now=START + timedelta(seconds=91))
            self.assertEqual(first, duplicate)
            self.assertEqual(duplicate["status"], "connected")
            self.assertIsNotNone(cable.read_fresh_connected(
                path, now=START + timedelta(seconds=91)))

    def test_gap_invalidates_continuity_without_inventing_event(self):
        record = ingest(ingest(None, "A1", 0), "A1", 30)
        record = ingest(record, "B2", 300)
        record = ingest(record, "B2", 330)
        self.assertEqual(record["status"], "connected")
        self.assertEqual(record["connection_epoch"], 0)
        self.assertFalse(record["confirmed_replug"])

    def test_read_failure_invalidates_coverage(self):
        record = ingest(ingest(None, "A1", 0), "A1", 30)
        record = ingest(record, "unknown", 60, read_ok=False)
        self.assertEqual(record["status"], "unknown")
        record = ingest(ingest(record, "B2", 90), "B2", 120)
        self.assertFalse(record["confirmed_replug"])
        self.assertEqual(record["connection_epoch"], 0)

    def test_invalid_and_stale_sources_rejected(self):
        for changes in ({"source": "unknown"},
                        {"writes": 1}, {"physical_control_eligible": True}):
            with self.subTest(changes=changes), self.assertRaises(
                    cable.CableObservationError):
                ingest(None, "A1", 0, **changes)
        with self.assertRaises(cable.CableObservationError):
            cable.advance(None, fc03("A1", 0),
                          now=START + timedelta(seconds=95))

    def test_unexpected_cp_fault_is_unknown_not_disconnected(self):
        record = ingest(ingest(None, "C2", 0), "C2", 30)
        record = ingest(record, "F2", 60)
        self.assertEqual(record["status"], "unknown")
        self.assertIsNone(record["last_disconnected_at_utc"])

    def test_corrupt_persisted_state_never_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cable.json"
            path.write_text('{"schema_version":99}')
            with self.assertRaises(cable.CableObservationError):
                cable.update(path, fc03("A1", 0),
                             now=START + timedelta(seconds=1))
            self.assertEqual(json.loads(path.read_text())["schema_version"], 99)

    def test_sanitize_and_no_manual_intent_assertions(self):
        state = ingest(ingest(None, "A1", 0), "A1", 30)
        state = ingest(ingest(state, "B2", 60), "B2", 90)
        self.assertNotIn("manual_paused", state)
        self.assertNotIn("manual_generation", state)
        self.assertNotIn("allow_resume", state)
        self.assertFalse(state["lossless"])
        self.assertEqual(state["signal_quality"], "debounced_polled_not_lossless")


if __name__ == "__main__":
    unittest.main()
