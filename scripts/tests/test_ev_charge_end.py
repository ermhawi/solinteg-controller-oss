"""Durable, read-only charging end events from debounced EVCC + Volvo history."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import ev_charge_end as end
import ev_history as history
import evcc_cable_tracker as tracker

UTC = timezone.utc
UNPLUG = datetime(2026, 11, 5, 6, 10, 41, tzinfo=UTC)
NOW = UNPLUG + timedelta(minutes=20)


class ChargeEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = root / "ev-history.sqlite"
        self.cable = root / "evcc-cable-state.json"
        self.export = root / "latest-charge.json"
        self.write_cable()

    def tearDown(self):
        self.temp.cleanup()

    def write_cable(self, *, status="disconnected", sample_at=NOW,
                    confirmed_at=UNPLUG):
        state = tracker._initial()
        state.update(
            status=status,
            observed_state="A1" if status == "disconnected" else "C2",
            last_sample_at_utc=sample_at.isoformat(),
            last_disconnected_at_utc=confirmed_at.isoformat(),
            last_transition_at_utc=confirmed_at.isoformat(),
            last_transition=None,
        )
        tracker.store(self.cable, state)

    def append(self, stamp: datetime, *, charging=False, soc=77.,
               api_at: datetime | None = None, quality=True):
        record = {
            "captured_at_utc": stamp.isoformat(),
            "captured_epoch_ms": int(stamp.timestamp() * 1000),
            "evcc_at_utc": stamp.isoformat(),
            "evcc_record_fresh": 1,
            "evcc_read_ok": 1,
            "evcc_state": "C2" if charging else "A1",
            "evcc_charging_detected": 1 if charging else 0,
            "evcc_ict2_a": 6.0 if charging else None,
            "ha_record_fresh": 1,
            "volvo_observation_usable": 1 if quality else 0,
            "volvo_soc_pct": soc,
            "volvo_connected": 1 if charging else 0,
            "volvo_api_received_at_utc": (api_at or stamp - timedelta(seconds=20)).isoformat(),
            "volvo_battery_last_changed_utc": (stamp - timedelta(minutes=1)).isoformat(),
            "volvo_quality": "api_recent_field_change_unverified" if quality else None,
            "physical_writes": 0,
        }
        history.append(str(self.db), record)

    def run_export(self):
        return end.record_and_publish(
            str(self.db), str(self.cable), str(self.export), now=NOW)

    def test_confirms_unplug_at_77_and_ignores_later_74_percent(self):
        self.append(UNPLUG - timedelta(minutes=1, seconds=19),
                    charging=True, api_at=UNPLUG - timedelta(minutes=2))
        self.append(UNPLUG + timedelta(minutes=10), soc=74.)
        event = self.run_export()
        self.assertEqual(event["socPct"], 77.)
        self.assertEqual(event["confirmedAtUtc"], UNPLUG.isoformat())
        self.assertEqual(event["lastChargingAtUtc"],
                         (UNPLUG - timedelta(minutes=1, seconds=19)).isoformat())
        self.assertEqual(event["quality"], "polled_cable_and_api_reported_soc_not_bms")
        self.assertEqual(self.export.stat().st_mode & 0o777, 0o640)
        content = self.export.read_bytes()
        self.assertEqual(json.loads(content), event)
        self.assertEqual(self.run_export(), event)
        self.assertEqual(self.export.read_bytes(), content)
        self.assertEqual(sqlite3.connect(self.db).execute(
            "SELECT COUNT(*) FROM ev_charge_ends").fetchone()[0], 1)

    def test_unverified_or_stale_disconnect_cannot_create_event(self):
        self.append(UNPLUG - timedelta(minutes=1), charging=True)
        self.write_cable(status="connected")
        self.assertIsNone(self.run_export())
        self.assertFalse(self.export.exists())
        self.write_cable(sample_at=NOW - timedelta(minutes=3))
        self.assertIsNone(self.run_export())
        self.assertFalse(self.export.exists())
        self.write_cable()
        record = json.loads(self.cable.read_text())
        record["last_transition_at_utc"] = None
        self.cable.write_text(json.dumps(record))
        self.assertIsNone(self.run_export())

    def test_no_confirmed_recent_charge_does_not_claim_completed_session(self):
        self.append(UNPLUG - timedelta(minutes=1), charging=False)
        self.assertIsNone(self.run_export())
        self.assertFalse(self.export.exists())

    def test_uncertain_or_stale_volvo_soc_remains_unknown(self):
        self.append(UNPLUG - timedelta(minutes=1), charging=True,
                    api_at=UNPLUG - timedelta(minutes=6))
        event = self.run_export()
        self.assertIsNotNone(event)
        self.assertIsNone(event["socPct"])
        self.assertIsNone(event["socApiReceivedAtUtc"])
        self.assertEqual(sqlite3.connect(self.db).execute(
            "SELECT COUNT(*) FROM ev_charge_ends").fetchone()[0], 1)

    def test_history_raw_sample_retention_does_not_erase_end_record(self):
        self.append(UNPLUG - timedelta(minutes=1), charging=True)
        event = self.run_export()
        self.assertIsNotNone(event)
        another = UNPLUG + timedelta(days=32)
        self.append(another, charging=False)
        history.append(str(self.db), {
            "captured_at_utc": (another+timedelta(minutes=1)).isoformat(),
            "captured_epoch_ms": int((another+timedelta(minutes=1)).timestamp()*1000),
            "physical_writes": 0,
        }, retention_days=30, prune=True)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM ev_charge_ends").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
