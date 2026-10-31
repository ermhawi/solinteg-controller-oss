"""Root-gated, local-only solar observer never alters inverter/EVCC state."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import ev_grid_priority_shadow_observer as observer
from test_ev_grid_priority_solar_shadow import EVCC, LIVE, NOW


class ShadowObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        folder = Path(self.tmp.name)
        self.live = folder / "live.json"
        self.evcc = folder / "evcc.json"
        self.gate = folder / "gate.json"
        self.actions = folder / "control.sqlite"
        self.live.write_text(json.dumps(LIVE))
        self.evcc.write_text(json.dumps(EVCC))
        self.gate.write_text(json.dumps({"mode": "shadow"}))
        with closing(sqlite3.connect(self.actions)) as conn:
            conn.execute("CREATE TABLE control_actions "
                         "(id INTEGER PRIMARY KEY, timestamp TEXT, "
                         "planned_action TEXT,power_w INTEGER,outcome TEXT)")
            conn.execute("INSERT INTO control_actions "
                         "(timestamp,planned_action,power_w,outcome) "
                         "VALUES (?,?,?,?)", (NOW.isoformat(), "idle", 0, "applied"))
            conn.commit()

    def inspect(self, now=NOW):
        return observer.inspect(self.live, self.evcc, self.actions,
                                self.gate, now=now)

    def test_observer_recognizes_actual_idle_and_hypothetical_cap(self):
        r = self.inspect()
        self.assertEqual(r["state"], "shadow_hypothesis")
        self.assertEqual(r["observed_action"], "idle")
        self.assertEqual(r["advice"]["candidate_power_w"], 50)
        self.assertFalse(r["physical_control_authorized"])
        self.assertEqual(r["device_writes"], 0)

    def test_active_gate_disables_independent_shadow_observer(self):
        self.gate.write_text(json.dumps({"mode": "active"}))
        r = self.inspect()
        self.assertEqual(r["state"], "disabled")
        self.assertNotIn("advice", r)

    def test_unavailable_history_does_not_guess_applied_action(self):
        self.actions.unlink()
        r = self.inspect()
        self.assertEqual(r["observed_action"], "unknown")
        self.assertEqual(r["state"], "unknown_dispatch")

    def test_stale_control_row_is_not_authoritative(self):
        r = self.inspect(NOW + timedelta(minutes=20))
        self.assertEqual(r["observed_action"], "unknown")
        self.assertEqual(r["state"], "unavailable")  # stale inverter and EVCC

    def test_output_is_private_and_atomic(self):
        output = Path(self.tmp.name) / "latest.json"
        observer.publish(output, self.inspect())
        self.assertEqual(json.loads(output.read_text())["mode"],
                         "read_only_shadow_observer")
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        observer.publish(output, {"state": "unknown"})
        self.assertEqual(json.loads(output.read_text())["state"], "unknown")

    def test_symlink_output_rejected(self):
        output = Path(self.tmp.name) / "latest.json"
        output.symlink_to(self.live)
        with self.assertRaises(ValueError):
            observer.publish(output, self.inspect())


if __name__ == "__main__":
    unittest.main()
