"""Replayed EV/L3 demands require real archived per-channel/phase evidence."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import ev_history as history  # noqa: E402
from ev_l3_metered_replay import replay, scan, SAMPLE_COLUMNS  # noqa: E402
from test_ev_grid_priority_guard import LIVE, EVCC, NOW  # noqa: E402


def record():
    return {
        "captured_at_utc": NOW.isoformat(),
        "inverter_at_utc": LIVE["timestamp"],
        "evcc_at_utc": EVCC["timestamp"],
        "inverter_fresh": 1, "evcc_record_fresh": 1,
        "evcc_read_ok": 1, "evcc_state": "C2",
        "evcc_charging_detected": 1,
        "evcc_configured_raw": 100, "evcc_pilot_raw": 100,
        "evcc_measured_a": 6.2, "evcc_ict1_a": .1,
        "evcc_ict2_a": 6.0, "evcc_ict3_a": .1,
        "l1_grid_w": LIVE["meter_l1_w"], "l2_grid_w": LIVE["meter_l2_w"],
        "l3_grid_w": LIVE["meter_l3_w"], "grid_w": LIVE["grid_w"],
        "inverter_ac_w": LIVE["inverter_ac_w"],
        "battery_w": LIVE["battery_w"], "house_load_w": LIVE["house_load_w"],
        "pv_w": LIVE["pv_w"], "house_soc_pct": LIVE["soc_pct"],
        "battery_temp_c": LIVE["battery_temp_c"],
        "grid_unbalanced_output_raw": LIVE["grid_unbalanced_output_raw"],
    }


class MeteredReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "history.sqlite"

    def test_six_amp_uses_real_ict2_and_balanced_readback(self):
        r = replay([record()])
        self.assertEqual(r["samples"]["policy_shadow_hypothesis"], 1)
        self.assertEqual(r["max_ev_w_est"], 1380)
        self.assertFalse(r["historical_ict2_assumed"])
        self.assertFalse(r["historical_50006_assumed"])
        self.assertFalse(r["physical_control_authorized"])
        self.assertEqual(r["device_writes"], 0)

    def test_missing_historical_ict2_or_50006_never_inferred(self):
        for key in ("evcc_ict2_a", "grid_unbalanced_output_raw", "battery_temp_c"):
            with self.subTest(key=key):
                s = record()
                s[key] = None
                r = replay([s])
                self.assertEqual(r["samples"]["c2_missing_independent_evidence"], 1)
                self.assertNotIn("policy_shadow_hypothesis", r["samples"])
                self.assertIsNone(r["max_ev_w_est"])

    def test_readonly_scan_against_new_schema(self):
        with sqlite3.connect(self.path) as con:
            con.executescript(history.SCHEMA)
            row = record()
            con.execute("INSERT INTO ev_samples (captured_epoch_ms,"
                        + ",".join(SAMPLE_COLUMNS) + ") VALUES (?"
                        + ",?" * len(SAMPLE_COLUMNS) + ")",
                        (int(NOW.timestamp() * 1000),)
                        + tuple(row[k] for k in SAMPLE_COLUMNS))
            con.commit()
        r = scan(self.path, since=NOW-timedelta(minutes=1),
                 until=NOW+timedelta(minutes=1))
        self.assertEqual(r["samples"]["policy_shadow_hypothesis"], 1)
        with sqlite3.connect(self.path) as con:
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM ev_samples").fetchone()[0], 1)

    def test_old_schema_is_reported_instead_of_migrated(self):
        with sqlite3.connect(self.path) as con:
            con.execute("CREATE TABLE ev_samples (captured_at_utc TEXT)")
            con.commit()
        r = scan(self.path, since=NOW-timedelta(minutes=1),
                 until=NOW+timedelta(minutes=1))
        self.assertEqual(r["status"], "missing_schema")
        self.assertIn("evcc_ict2_a", r["missing_columns"])
        with sqlite3.connect(self.path) as con:
            self.assertEqual(len(con.execute(
                "PRAGMA table_info(ev_samples)").fetchall()), 1)

    def test_a1_no_current_and_stale_record_not_misrepresented(self):
        a1 = {**record(), "evcc_state": "A1",
              "evcc_charging_detected": None, "evcc_ict2_a": None}
        stale = {**record(), "evcc_read_ok": 0}
        r = replay([a1, stale])
        self.assertEqual(r["samples"]["history_samples"], 2)
        self.assertEqual(r["samples"]["c2_unverified_state"], 1)
        self.assertIsNone(r["max_ev_w_est"])

    def test_16_a_is_measured_not_declared_safe(self):
        row = record()
        delta = (16 - 6) * 230
        row.update(evcc_configured_raw=267, evcc_pilot_raw=267,
                   evcc_measured_a=16.2, evcc_ict2_a=16.,
                   l3_grid_w=row["l3_grid_w"] - delta,
                   grid_w=row["grid_w"] - delta,
                   house_load_w=row["house_load_w"] + delta)
        r = replay([row])
        self.assertEqual(r["max_ev_w_est"], 3680)
        self.assertEqual(r["samples"]["policy_phase_import_bound"], 1)

    def test_sunny_ict2_replay_reports_house_battery_priority(self):
        row = record()
        row.update(pv_w=2026, battery_w=196, inverter_ac_w=2173,
                   house_load_w=2137, grid_w=36, l1_grid_w=400,
                   l2_grid_w=391, l3_grid_w=-755, house_soc_pct=25)
        result = replay([row])
        self.assertEqual(result["samples"]["policy_solar_storage_priority_unverified"], 1)
        self.assertEqual(result["max_ev_w_est"], 1380)
        self.assertFalse(result["physical_control_authorized"])
        self.assertEqual(result["device_writes"], 0)

    def test_invalid_time_interval_is_rejected(self):
        with self.assertRaises(ValueError):
            scan(self.path, since=NOW, until=NOW)


if __name__ == "__main__":
    unittest.main()
