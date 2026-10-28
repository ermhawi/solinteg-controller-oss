"""Read-only Volvo journal cost evidence contract, privacy and deduplication."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import ev_cost_capture as capture

NOW = datetime(2026, 10, 28, 18, 10, tzinfo=timezone.utc)
MS = int(NOW.timestamp() * 1000)


def segment(offset=0, *, distance=1000.0, energy=0.2):
    begin = MS - 10 * 60_000 + offset
    return dict(start_ms=begin, end_ms=begin + 3 * 60_000,
                distance_m=distance, kwh=energy)


def payload(*segments, status="ok"):
    return {
        "schema_version": 1,
        "source": "volvo_trip_energy",
        "measurement_kind": "reported_electrical_consumption_not_net_battery_dc",
        "data_status": status,
        "last_success_at_utc": (NOW - timedelta(minutes=30)).isoformat(),
        "generated_at_ms": MS,
        "segments": list(segments),
    }


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "private-evidence.sqlite")

    def tearDown(self):
        self.temp.cleanup()

    def validated(self, value):
        return capture.validate(value, now=NOW)

    def test_accepts_source_reported_wh_and_meters_without_pricing(self):
        records = self.validated(payload(segment()))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["distance_m"], "1000")
        result = capture.overview(records)
        self.assertEqual(result["reported_consumption_kwh"], 0.2)
        self.assertEqual(result["distance_km"], 1)
        self.assertIsNone(result["kr_per_mil"])
        self.assertEqual(result["cost_quality"], "unavailable")
        self.assertEqual(result["device_writes"], 0)

    def test_duplicate_same_payload_is_deduplicated(self):
        same = segment()
        records = self.validated(payload(same, same))
        self.assertEqual(len(records), 1)

    def test_overlapping_and_conflicting_intervals_fail_atomically(self):
        with self.assertRaisesRegex(capture.EvidenceError, "overlapping"):
            self.validated(payload(segment(), segment(1000)))
        mismatch = {**segment(), "kwh": 0.4}
        with self.assertRaisesRegex(capture.EvidenceError, "conflicting"):
            self.validated(payload(segment(), mismatch))
        self.assertFalse(Path(self.db).exists())

    def test_source_refresh_time_and_status_are_not_measurement_freshness(self):
        for bad in (
            payload(segment(), status="partial"),
            payload(segment(), status="stale"),
            dict(payload(segment()), generated_at_ms=MS - 10 * 60_000),
            dict(payload(segment()), last_success_at_utc=(NOW - timedelta(hours=2)).isoformat()),
            dict(payload(segment()), source="other"),
            dict(payload(segment()), measurement_kind="verified_battery_dc"),
        ):
            with self.subTest(bad=bad.get("data_status")), \
                    self.assertRaises(capture.EvidenceError):
                self.validated(bad)

    def test_rejects_privacy_fields_bad_numbers_old_segments_and_future(self):
        candidates = [
            payload({**segment(), "vin": "DO_NOT_STORE"}),
            payload({**segment(), "latitude": 62.0}),
            payload({**segment(), "kwh": -0.1}),
            payload({**segment(), "kwh": float("nan")}),
            payload({**segment(), "distance_m": 0}),
            payload({**segment(), "kwh": 999}),
            payload({**segment(), "end_ms": MS + 300000}),
            payload({**segment(), "start_ms": MS - 31 * 86400 * 1000}),
            {**payload(segment()), "tripId": "DO_NOT_STORE"},
        ]
        for candidate in candidates:
            with self.subTest(which=str(candidate)[:70]), \
                    self.assertRaises(capture.EvidenceError):
                self.validated(candidate)

    def test_cumulative_journal_is_idempotent_across_fetches(self):
        first = self.validated(payload(segment()))
        self.assertEqual(capture.append(self.db, first), 1)
        self.assertEqual(capture.append(self.db, first), 0)
        later = self.validated(payload(segment(), segment(4 * 60_000)))
        self.assertEqual(capture.append(self.db, later), 1)
        with sqlite3.connect("file:" + self.db + "?mode=ro", uri=True) as db:
            rows = db.execute(
                "SELECT start_ms,end_ms,distance_m,reported_consumption_kwh "
                "FROM ev_cost_drive_evidence ORDER BY start_ms").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][-2:], ("1000", "0.2"))
        self.assertEqual(Path(self.db).stat().st_mode & 0o777, 0o600)
        self.assertNotIn(b"vin", Path(self.db).read_bytes())
        self.assertNotIn(b"latitude", Path(self.db).read_bytes())

    def test_revised_prior_interval_never_overwrites_existing_evidence(self):
        capture.append(self.db, self.validated(payload(segment())))
        changed = self.validated(payload({**segment(), "kwh": 0.19}))
        with self.assertRaisesRegex(capture.EvidenceError, "revised"):
            capture.append(self.db, changed)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute(
                "SELECT reported_consumption_kwh FROM ev_cost_drive_evidence").fetchone(),
                ("0.2",))

    def test_overlaps_across_multiple_captures_fail_closed(self):
        capture.append(self.db, self.validated(payload(segment())))
        overlapping = self.validated(payload(segment(60_000)))
        with self.assertRaisesRegex(capture.EvidenceError, "overlapping"):
            capture.append(self.db, overlapping)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM ev_cost_drive_evidence").fetchone()[0], 1)

    def test_private_ha_url_with_no_credentials_or_public_hosts(self):
        self.assertEqual(
            capture._private_url("http://10.0.0.20:8123"),
            "http://10.0.0.20:8123/api/volvo_trip_energy/cost_segments")
        for url in ("https://example.com", "http://127.0.0.1:8123",
                    "http://10.0.0.3/admin", "http://name:secret@10.0.0.1",
                    "http://169.254.169.254"):
            with self.subTest(url=url), self.assertRaises(capture.EvidenceError):
                capture._private_url(url)

    def test_valid_zero_reported_energy_is_not_silently_a_free_charge(self):
        records = self.validated(payload(segment(energy=0)))
        self.assertEqual(records[0]["reported_consumption_kwh"], "0")
        self.assertIsNone(capture.overview(records)["kr_per_mil"])

    def test_default_file_mode_is_preview_no_sqlite_writes(self):
        source = Path(self.temp.name) / "source.json"
        source.write_text(json.dumps(payload(segment())))
        with mock.patch.object(sys, "argv", ["capture", "--file", str(source),
                                               "--db", self.db]), \
                mock.patch.object(capture, "datetime", wraps=datetime) as clock, \
                redirect_stdout(io.StringIO()) as out:
            clock.now.return_value = NOW
            capture.main()
        # Even if time validation is patched above, no DB may be created.
        self.assertFalse(Path(self.db).exists())
        self.assertIsNone(json.loads(out.getvalue())["evidence_inserted"])

    def test_private_pipe_capture_does_not_reopen_dev_stdin(self):
        stream = io.TextIOWrapper(io.BytesIO(json.dumps(payload(segment())).encode()),
                                  encoding="utf-8")
        with mock.patch.object(sys, "argv", ["capture", "--file", "-",
                                               "--db", self.db, "--capture"]), \
                mock.patch.object(sys, "stdin", stream), \
                mock.patch.object(capture, "datetime", wraps=datetime) as clock, \
                redirect_stdout(io.StringIO()) as out:
            clock.now.return_value = NOW
            capture.main()
        self.assertEqual(json.loads(out.getvalue())["evidence_inserted"], 1)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM ev_cost_drive_evidence").fetchone()[0], 1)

    def test_pure_reader_has_no_device_commands_or_other_database_writes(self):
        source = Path(capture.__file__).read_text()
        for forbidden in ("import evcc_ascii", "import modbus_poller",
                          "import inverter_control", "set_limit_raw", "FC10",
                          "requests.post", "method=\"POST\""):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
