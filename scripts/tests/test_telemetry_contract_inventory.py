"""T01: schema/producer ownership contracts; source-only, no SQLite runtime reads."""
from __future__ import annotations

import sqlite3
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SOURCES = {
    "readings": "scripts/services/modbus_poller.py",
    "weather": "scripts/services/weather_poller.py",
    "price_snapshots": "lib/telemetry/readings.ts",
    "optimizer_runs": "lib/telemetry/dispatch.ts",
    "solar_forecasts": "lib/telemetry/readings.ts",
    "control_actions": "scripts/services/dispatch_loop.py",
    "control_readbacks": "scripts/services/dispatch_loop.py",
    "oracle_daily": "lib/telemetry/oracle.ts",
}
JSON_CONTRACTS = {
    "live.json": ("scripts/services/modbus_poller.py", "scripts/services/common.py"),
    "dispatch-heartbeat.json": ("scripts/services/dispatch_loop.py", "scripts/services/common.py"),
    "evcc-readonly.json": ("scripts/services/evcc_observer.py", "scripts/services/common.py"),
    "evcc-cable-state.json": ("scripts/services/ev_history.py", "scripts/services/evcc_cable_tracker.py"),
    "ev-price-pause-owner.json": ("scripts/services/ev_price_controller.py",
                                  "scripts/services/ev_price_pause_owner.py"),
    "ev-price-controller-status.json": ("scripts/services/ev_price_controller.py",),
}


class TelemetryOwnershipTests(unittest.TestCase):
    def test_all_eight_canonical_sqlite_tables_have_source_owner(self):
        with sqlite3.connect(":memory:") as db:
            db.executescript((ROOT / "deploy/schema.sql").read_text(encoding="utf-8"))
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )}
        self.assertEqual(tables, set(SOURCES))
        for name, relative in SOURCES.items():
            with self.subTest(table=name):
                self.assertIn(name, (ROOT / relative).read_text(encoding="utf-8"))

    def test_sqlite_writers_share_wal_and_busy_timeout(self):
        python = (ROOT / "scripts/services/common.py").read_text(encoding="utf-8")
        node = (ROOT / "lib/telemetry/core.ts").read_text(encoding="utf-8")
        for source in (python, node):
            self.assertIn("journal_mode", source)
            self.assertIn("WAL", source)
            self.assertIn("busy_timeout=5000", source)
        self.assertIn("telemetry_connect", (ROOT / SOURCES["readings"]).read_text())
        self.assertIn("telemetry_connect", (ROOT / SOURCES["control_actions"]).read_text())

    def test_critical_json_producers_and_readers_have_explicit_ownership(self):
        for name, producers in JSON_CONTRACTS.items():
            with self.subTest(name=name):
                self.assertGreaterEqual(len(producers), 1)
                for path in producers:
                    source = ROOT / path
                    self.assertTrue(source.is_file())
                    self.assertIn("json", source.read_text(encoding="utf-8").lower())
        self.assertIn("os.replace", (ROOT / "scripts/services/common.py").read_text())
        for source in ("evcc_cable_tracker.py", "ev_price_pause_owner.py"):
            data = (ROOT / "scripts/services" / source).read_text()
            self.assertIn("os.replace(", data)
            self.assertIn("os.fsync(", data)

    def test_safety_relevant_age_and_failure_boundaries_are_explicit(self):
        ctrl = (ROOT / "scripts/services/ev_price_controller.py").read_text()
        cable = (ROOT / "scripts/services/evcc_cable_tracker.py").read_text()
        owner = (ROOT / "scripts/services/ev_price_pause_owner.py").read_text()
        self.assertIn("MAX_SAMPLE_AGE_S = 65", ctrl)
        self.assertIn("MAX_SAMPLE_AGE_S = 90", cable)
        self.assertIn("MAX_OWNERSHIP_AGE_S = 24 * 3600", owner)
        self.assertIn("MAX_CABLE_AGE_S = 90", owner)
        self.assertIn('return None', (ROOT / "scripts/services/common.py").read_text())


if __name__ == "__main__":
    unittest.main()
