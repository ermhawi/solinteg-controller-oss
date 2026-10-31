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
from test_ev_grid_priority_idle_strategy import sample as historian_sample


class ShadowObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        folder = Path(self.tmp.name)
        self.live = folder / "live.json"
        self.evcc = folder / "evcc.json"
        self.gate = folder / "gate.json"
        self.actions = folder / "control.sqlite"
        self.history = folder / "missing-history.sqlite"
        self.live.write_text(json.dumps({**LIVE, "work_mode_raw": 0x101}))
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
                                self.gate, history_path=self.history, now=now)

    def test_observer_recognizes_actual_idle_and_hypothetical_cap(self):
        r = self.inspect()
        self.assertEqual(r["state"], "shadow_hypothesis")
        self.assertEqual(r["observed_action"], "idle")
        self.assertEqual(r["advice"]["candidate_power_w"], 50)
        self.assertFalse(r["physical_control_authorized"])
        self.assertEqual(r["device_writes"], 0)

    def test_no_historian_never_claims_stable_idle_action(self):
        r = self.inspect()
        self.assertEqual(r["idle_strategy"]["state"], "await_deficit_stability")
        self.assertIsNone(r["idle_strategy"]["hypothetical_mode"])
        self.assertFalse(r["idle_strategy"]["measurements"]["model_actionable"])

    def test_contiguous_read_only_historian_yields_tested_shadow_candidate(self):
        fields = ("captured_at_utc", "inverter_fresh", "inverter_age_s",
                  "evcc_record_fresh", "evcc_age_s", "evcc_read_ok", "evcc_state",
                  "evcc_charging_detected", "evcc_6a_limit_verified",
                  "evcc_measured_a", "battery_w", "house_soc_pct", "pv_w",
                  "house_load_w", "grid_w", "l1_grid_w", "l2_grid_w", "l3_grid_w")
        with closing(sqlite3.connect(self.history)) as con:
            con.execute("CREATE TABLE ev_samples ("
                        "captured_epoch_ms INTEGER NOT NULL, "
                        + ",".join(n + " TEXT" if n in ("captured_at_utc", "evcc_state")
                                   else n + " REAL" for n in fields) + ")")
            for offset in (100, 70, 40, 10):
                timestamp = NOW - timedelta(seconds=offset)
                s = historian_sample(timestamp)
                con.execute(
                    "INSERT INTO ev_samples VALUES (" + ",".join("?" for _ in range(len(fields)+1)) + ")",
                    (int(timestamp.timestamp()*1000),) + tuple(s[n] for n in fields))
            con.commit()
        with closing(sqlite3.connect(self.actions)) as con:
            con.execute("UPDATE control_actions SET timestamp=?",
                        ((NOW-timedelta(minutes=5)).isoformat(),))
            con.commit()
        r = self.inspect()
        self.assertEqual(r["state"], "shadow_hypothesis")
        self.assertEqual(r["idle_strategy"]["state"], "battery_save_candidate")
        self.assertEqual(r["idle_strategy"]["measurements"]["hypothetical_battery_save_target_w"], 50)
        self.assertFalse(r["idle_strategy"]["measurements"]["physical_control_authorized"])
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
