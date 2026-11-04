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
from test_battery_save_solar_shadow import LIVE as SAVE_LIVE, sample as save_sample, NOW as SAVE_NOW


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
                         "planned_action TEXT,power_w INTEGER,outcome TEXT,armed INTEGER,detail_json TEXT)")
            conn.execute("INSERT INTO control_actions "
                         "(timestamp,planned_action,power_w,outcome) "
                         "VALUES (?,?,?,?)", (NOW.isoformat(), "idle", 0, "applied"))
            conn.commit()

    def inspect(self, now=NOW, **kwargs):
        return observer.inspect(self.live, self.evcc, self.actions,
                                self.gate, history_path=self.history, now=now, **kwargs)

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
        fields = ("captured_at_utc", "inverter_at_utc", "inverter_fresh", "inverter_age_s",
                  "evcc_record_fresh", "evcc_age_s", "evcc_read_ok", "evcc_state",
                  "evcc_charging_detected", "evcc_6a_limit_verified",
                  "evcc_measured_a", "battery_w", "house_soc_pct", "pv_w",
                  "house_load_w", "grid_w", "l1_grid_w", "l2_grid_w", "l3_grid_w")
        with closing(sqlite3.connect(self.history)) as con:
            con.execute("CREATE TABLE ev_samples ("
                        "captured_epoch_ms INTEGER NOT NULL, "
                        + ",".join(n + " TEXT" if n in ("captured_at_utc", "inverter_at_utc", "evcc_state")
                                   else n + " REAL" for n in fields) + ")")
            for offset in (100, 70, 40, 10):
                timestamp = NOW - timedelta(seconds=offset)
                s = historian_sample(timestamp)
                s["inverter_at_utc"] = (timestamp - timedelta(seconds=5)).isoformat()
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

    def setup_battery_save_solar(self):
        self.live.write_text(json.dumps(SAVE_LIVE))
        with closing(sqlite3.connect(self.actions)) as con:
            con.execute(
                "UPDATE control_actions SET timestamp=?, planned_action='discharge', "
                "power_w=50, outcome='applied', armed=1, detail_json=?",
                ((SAVE_NOW - timedelta(minutes=5)).isoformat(),
                 json.dumps({"battery_save_w": 50})))
            con.commit()
        fields = ("captured_at_utc", "inverter_at_utc", "inverter_fresh",
                  "inverter_age_s", "evcc_record_fresh", "evcc_age_s",
                  "evcc_read_ok", "evcc_state", "evcc_charging_detected",
                  "evcc_6a_limit_verified", "evcc_measured_a", "battery_w",
                  "house_soc_pct", "pv_w", "house_load_w", "grid_w",
                  "l1_grid_w", "l2_grid_w", "l3_grid_w")
        with closing(sqlite3.connect(self.history)) as con:
            con.execute(
                "CREATE TABLE ev_samples (captured_epoch_ms INTEGER NOT NULL, "
                + ",".join(n + " TEXT" if n in (
                    "captured_at_utc", "inverter_at_utc", "evcc_state")
                    else n + " INTEGER" if n == "inverter_fresh"
                    else n + " REAL" for n in fields) + ")")
            for ago in (100, 70, 40, 10):
                stamp = SAVE_NOW - timedelta(seconds=ago)
                sample = save_sample(stamp)
                con.execute(
                    "INSERT INTO ev_samples VALUES ("
                    + ",".join("?" for _ in range(len(fields) + 1)) + ")",
                    (int(stamp.timestamp() * 1000),)
                    + tuple(sample.get(n) for n in fields))
            con.commit()

    def inspect_battery_save(self, **kwargs):
        return self.inspect(
            now=SAVE_NOW, planner_reserve_pct=20,
            recovery_entry_pct=19.5, recovery_exit_pct=21, **kwargs)

    def test_verified_armed_battery_save_detects_stable_solar_shadow(self):
        self.setup_battery_save_solar()
        result = self.inspect_battery_save()
        self.assertTrue(result["battery_save_verified"])
        self.assertEqual(result["observed_action"], "discharge")
        self.assertEqual(result["battery_save_solar"]["state"], "solar_capture_candidate")
        self.assertFalse(result["battery_save_solar"]["evidence"]["model_actionable"])
        self.assertEqual(result["device_writes"], 0)

    def test_house_solar_shadow_survives_missing_evcc_record(self):
        self.setup_battery_save_solar()
        self.evcc.unlink()
        result = self.inspect_battery_save()
        self.assertEqual(result["advice"]["state"], "unavailable")
        self.assertEqual(result["battery_save_solar"]["state"], "solar_capture_candidate")
        self.assertEqual(result["device_writes"], 0)

    def test_idle_ev_strategy_still_requires_evcc_record(self):
        self.evcc.unlink()
        result = self.inspect()
        self.assertEqual(result["state"], "unavailable")
        self.assertNotIn("idle_strategy", result)

    def test_battery_save_without_site_thresholds_stays_unverified(self):
        self.setup_battery_save_solar()
        result = self.inspect(now=SAVE_NOW)
        self.assertEqual(result["battery_save_solar"]["state"], "thresholds_unverified")

    def test_arbitrary_unarmed_or_unmarked_50w_discharge_not_proven_save(self):
        self.setup_battery_save_solar()
        for armed, detail_json in ((0, '{"battery_save_w":50}'),
                                   (1, '{}'), (1, '{"battery_save_w":51}'),
                                   (1, '{"battery_save_w":true}')):
            with self.subTest(armed=armed, detail_json=detail_json):
                with closing(sqlite3.connect(self.actions)) as con:
                    con.execute("UPDATE control_actions SET armed=?, detail_json=?",
                                (armed, detail_json))
                    con.commit()
                result = self.inspect_battery_save()
                self.assertFalse(result["battery_save_verified"])
                self.assertEqual(result["battery_save_solar"]["state"],
                                 "not_verified_save")

    def test_active_gate_disables_independent_shadow_observer(self):
        self.gate.write_text(json.dumps({"mode": "active"}))
        r = self.inspect()
        self.assertEqual(r["state"], "disabled")
        self.assertNotIn("advice", r)

    def test_metered_policy_runs_when_legacy_root_gate_is_missing(self):
        self.gate.unlink()
        self.live.write_text(json.dumps({**LIVE, "battery_temp_c": 15}))
        r = self.inspect()
        self.assertEqual(r["state"], "disabled")
        self.assertEqual(r["reason"], "root gate missing")
        self.assertNotIn("advice", r)
        self.assertEqual(r["metered_policy"]["state"], "idle_stability_unverified")
        self.assertEqual(r["metered_policy"]["measurements"]["ev_grid_target_w_est"], 1380)
        self.assertFalse(r["metered_policy"]["measurements"]["physical_control_authorized"])
        self.assertEqual(r["device_writes"], 0)

    def test_metered_policy_observes_daylight_with_no_legacy_gate(self):
        self.gate.unlink()
        self.live.write_text(json.dumps({
            **LIVE, "battery_temp_c": 15, "pv_w": 2026, "battery_w": 196,
            "inverter_ac_w": 2173, "house_load_w": 2137,
            "grid_w": 36, "meter_l1_w": 400,
            "meter_l2_w": 391, "meter_l3_w": -755, "soc_pct": 25,
        }))
        r = self.inspect()
        self.assertEqual(r["state"], "disabled")
        self.assertEqual(r["metered_policy"]["state"],
                         "solar_storage_priority_unverified")
        self.assertEqual(r["metered_policy"]["measurements"]["ev_grid_target_w_est"], 1380)
        self.assertEqual(r["metered_policy"]["measurements"]["ev_solar_allowance_w_est"], 0)
        self.assertEqual(r["metered_policy"]["measurements"]["house_battery_solar_charge_headroom_w_est"],
                         1269)
        self.assertIsNone(r["metered_policy"]["candidate_power_w"])

    def test_metered_policy_missing_evcc_fails_closed_independently(self):
        self.gate.unlink()
        self.evcc.unlink()
        r = self.inspect()
        self.assertEqual(r["state"], "disabled")
        self.assertEqual(r["metered_policy"]["state"], "unavailable")
        self.assertEqual(r["metered_policy"]["measurements"]["device_writes"], 0)
        self.assertIsNone(r["metered_policy"]["candidate_action"])

    def test_metered_policy_missing_control_history_still_has_metered_grid_target(self):
        self.gate.unlink()
        self.actions.unlink()
        self.live.write_text(json.dumps({**LIVE, "battery_temp_c": 15}))
        r = self.inspect()
        self.assertEqual(r["state"], "disabled")
        self.assertEqual(r["metered_policy"]["state"], "unknown_action")
        self.assertEqual(r["metered_policy"]["measurements"]["ev_grid_target_w_est"], 1380)
        self.assertIsNone(r["metered_policy"]["candidate_power_w"])

    def test_missing_gate_is_disabled_after_watchdog_rollback(self):
        self.gate.unlink()
        r = self.inspect()
        self.assertEqual(r["state"], "disabled")
        self.assertEqual(r["reason"], "root gate missing")
        self.assertEqual(r["device_writes"], 0)
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
