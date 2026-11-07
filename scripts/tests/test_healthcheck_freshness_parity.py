"""T04 golden alert fingerprints for the first extracted freshness group.

Only in-memory SQLite. No live telemetry database, network or notify send.
The expected tuples are frozen from the former healthcheck.py code path.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import healthcheck as hc  # noqa: E402


NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


def fixture(table: str, age_seconds: int | None):
    con = sqlite3.connect(":memory:")
    con.execute(f"CREATE TABLE {table} (timestamp TEXT)")
    if age_seconds is not None:
        con.execute(f"INSERT INTO {table} VALUES (?)", (
            (NOW - timedelta(seconds=age_seconds)).isoformat(),))
    return con


class FreshnessAlertGoldenTests(unittest.TestCase):
    def test_poller_no_data_and_stale_emit_identical_alert_tuple(self):
        with fixture("readings", None) as conn:
            self.assertEqual(hc.check_poller_stale(conn, NOW), (
                "poller_no_data", hc.notify.PRIORITY_HIGH,
                "Solinteg: no inverter readings yet",
                "The readings table is empty — has solinteg-poller ever run successfully?",
            ))
        with fixture("readings", 301) as conn:
            self.assertEqual(hc.check_poller_stale(conn, NOW), (
                "poller_stale", hc.notify.PRIORITY_HIGH,
                "Solinteg: inverter poller looks dead",
                "Last reading was 301s ago (expected every 30s). "
                "Check solinteg-poller on the NUC.",
            ))

    def test_weather_no_data_stale_and_disabled_preserve_previous_messages(self):
        with patch.object(hc, "WEATHER_STALE_S", 1800):
            with fixture("weather", None) as conn:
                self.assertEqual(hc.check_weather_stale(conn, NOW), (
                    "weather_no_data", hc.notify.PRIORITY_LOW,
                    "Solinteg: no weather data yet",
                    "The weather table is empty — has solinteg-weather ever run successfully?",
                ))
            with fixture("weather", 1801) as conn:
                self.assertEqual(hc.check_weather_stale(conn, NOW), (
                    "weather_stale", hc.notify.PRIORITY_LOW,
                    "Solinteg: weather data is stale",
                    "Last weather reading was 1801s ago. Non-urgent — the solar forecast "
                    "just falls back to climatology until this recovers. Check solinteg-weather "
                    "and the Ecowitt station/cloud API.",
                ))
        with patch.object(hc, "WEATHER_STALE_S", 0):
            with fixture("weather", None) as conn:
                self.assertIsNone(hc.check_weather_stale(conn, NOW))

    def test_existing_alert_fingerprint_cooldown_is_unchanged(self):
        with fixture("readings", 301) as conn:
            event = hc.check_poller_stale(conn, NOW)
        self.assertEqual(event[0], "poller_stale")
        fingerprint = str(event[1])
        entry = {
            "last_alert": (NOW - timedelta(minutes=5)).isoformat(),
            "last_seen": (NOW - timedelta(minutes=5)).isoformat(),
            "fingerprint": fingerprint,
        }
        self.assertEqual(hc.should_alert(entry, fingerprint, NOW)[0], False)
        # Returning None when fresh means the caller's unchanged resolve
        # hysteresis can eventually clear the existing poller_stale issue.
        with fixture("readings", 10) as conn:
            self.assertIsNone(hc.check_poller_stale(conn, NOW))

    def test_threshold_and_missing_tables_keep_previous_fail_open_semantics(self):
        with fixture("readings", 300) as conn:
            self.assertIsNone(hc.check_poller_stale(conn, NOW))
        with fixture("weather", 1800) as conn:
            self.assertIsNone(hc.check_weather_stale(conn, NOW))
        with sqlite3.connect(":memory:") as conn:
            self.assertEqual(hc.check_poller_stale(conn, NOW)[0], "poller_no_data")
            self.assertEqual(hc.check_weather_stale(conn, NOW)[0], "weather_no_data")


if __name__ == "__main__":
    unittest.main()
