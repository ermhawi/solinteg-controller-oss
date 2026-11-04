"""Time-aligned private SQLite EV diagnostic history, no device operations."""
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
import ev_history as history

NOW = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)


def inverter(*, stamp=NOW):
    return {
        "timestamp": stamp.isoformat(),
        "meter_l1_w": -300, "meter_l2_w": -190,
        "meter_l3_w": -566, "grid_w": -1056,
        "battery_w": 180, "house_load_w": 1400, "pv_w": 700,
        "soc_pct": 94,
    }


def evcc(*, stamp=NOW, raw=100):
    return {
        "timestamp": stamp.isoformat(),
        "source": "waveshare_evcc_fc03_readonly",
        "read_ok": True,
        "modbus_function": "FC03",
        "configured_pwm_raw": raw,
        "pilot_pwm_raw": raw,
        "state": "A1",
        "measured_current_a": 0.3,
        "charging_detected": False,
        "physical_control_eligible": False,
        "writes": 0,
    }


def ha(*, stamp=NOW, api=NOW, changed=None):
    changed = changed or NOW - timedelta(hours=3)
    return {
        "source": "home_assistant",
        "checked_at_utc": stamp.isoformat(),
        "shadow_observation_usable": True,
        "physical_control_eligible": False,
        "energy_api_received_at_utc": api.isoformat(),
        "battery_reported_at_utc": changed.isoformat(),
        "battery_soc_pct": 63,
        "target_soc_pct": 80,
        "connected": True,
        "observation_quality": "api_recent_field_change_unverified",
    }


def shadow(*, stamp=NOW):
    return {
        "source": "ha_price_scenario_with_evcc",
        "timestamp": stamp.isoformat(),
        "mode": "shadow",
        "charge_mode": "deadline",
        "charging_suggested_a": 0,
        "charging_permitted_a": 0,
        "grid_topup_suggested_a": 0,
        "price_buy_ore_kwh": 119.47,
        "price_cheap_threshold_ore_kwh": 102.75,
        "price_reason": "cheaper later",
    }


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.paths = {
            "inverter_path": base / "live.json",
            "evcc_path": base / "evcc.json",
            "ha_path": base / "ha.json",
            "plan_path": base / "plan.json",
        }
        self.db = base / "history.sqlite"
        for path, value in zip(self.paths.values(),
                               (inverter(), evcc(), ha(), shadow())):
            path.write_text(json.dumps(value))

    def tearDown(self):
        self.temp.cleanup()

    def capture(self, *, now=NOW):
        return history.capture(
            now=now, **{key: str(path) for key, path in self.paths.items()})

    def change(self, key, record):
        self.paths[key].write_text(json.dumps(record))

    def test_coherent_sample_preserves_sign_and_distinct_clock_sources(self):
        row = self.capture()
        self.assertEqual(row["inverter_fresh"], 1)
        self.assertEqual(row["grid_w"], -1056)  # negative = GRID IMPORT
        self.assertEqual(row["l3_grid_w"], -566)
        self.assertEqual(row["battery_w"], 180)  # positive = BATT DISCHARGE
        self.assertEqual(row["house_soc_pct"], 94)
        self.assertEqual(row["evcc_read_ok"], 1)
        self.assertEqual(row["evcc_configured_raw"], 100)
        self.assertEqual(row["evcc_pilot_raw"], 100)
        self.assertEqual(row["evcc_6a_limit_verified"], 1)
        self.assertEqual(row["evcc_measured_a"], 0.3)
        self.assertEqual(row["evcc_charging_detected"], 0)
        self.assertEqual(row["volvo_soc_pct"], 63)
        self.assertEqual(row["volvo_api_received_at_utc"], NOW.isoformat())
        self.assertNotEqual(row["volvo_api_received_at_utc"],
                            row["volvo_battery_last_changed_utc"])
        self.assertEqual(row["volvo_observation_usable"], 1)
        self.assertEqual(row["suggested_current_a"], 0)
        self.assertEqual(row["physical_writes"], 0)

    def test_new_ict2_and_phase_evidence_is_recorded_per_channel(self):
        live = {**inverter(), "inverter_ac_w": 1630,
                "battery_temp_c": 16.0, "grid_unbalanced_output_raw": 0}
        self.change("inverter_path", live)
        ev = {**evcc(raw=267), "state": "C2",
              "channel_currents_a": [.1, 16.0, .1],
              "measured_current_a": 16.2, "charging_detected": True}
        self.change("evcc_path", ev)
        row = self.capture()
        self.assertEqual(row["evcc_6a_limit_verified"], 0)
        self.assertEqual(row["evcc_ict1_a"], .1)
        self.assertEqual(row["evcc_ict2_a"], 16.0)
        self.assertEqual(row["evcc_ict3_a"], .1)
        self.assertEqual(row["inverter_ac_w"], 1630)
        self.assertEqual(row["battery_temp_c"], 16)
        self.assertEqual(row["grid_unbalanced_output_raw"], 0)
        history.append(str(self.db), row)
        with sqlite3.connect(self.db) as con:
            stored = con.execute(
                "SELECT evcc_ict2_a, inverter_ac_w, battery_temp_c, "
                "grid_unbalanced_output_raw FROM ev_samples").fetchone()
        self.assertEqual(stored, (16.0, 1630.0, 16.0, 0))

    def test_unverified_channel_sum_or_missing_ct_stays_null(self):
        ev = {**evcc(raw=100), "state": "C2",
              "channel_currents_a": [.1, 6., .1],
              "measured_current_a": 20., "charging_detected": True}
        self.change("evcc_path", ev)
        row = self.capture()
        self.assertIsNone(row["evcc_ict2_a"])
        self.assertEqual(row["evcc_measured_a"], 20.)
        ev["channel_currents_a"] = [None, None, None]
        ev["measured_current_a"] = None
        self.change("evcc_path", ev)
        row = self.capture()
        self.assertIsNone(row["evcc_ict2_a"])
        self.assertIsNone(row["evcc_measured_a"])

    def test_failed_evcc_read_is_unknown_not_zero_current(self):
        ev = evcc()
        ev.update({"read_ok": False, "reason": "EvccReadError",
                   "state": "unknown", "charging_detected": None,
                   "measured_current_a": None})
        self.change("evcc_path", ev)
        row = self.capture()
        self.assertEqual(row["evcc_record_fresh"], 1)
        self.assertEqual(row["evcc_read_ok"], 0)
        self.assertEqual(row["evcc_error"], "EvccReadError")
        self.assertIsNone(row["evcc_measured_a"])
        self.assertIsNone(row["evcc_charging_detected"])
        self.assertIsNone(row["evcc_6a_limit_verified"])
        self.assertIsNone(row["evcc_state"])

    def test_old_evcc_data_is_null_even_if_last_good_showed_6_a(self):
        self.change("evcc_path", evcc(stamp=NOW-timedelta(minutes=4)))
        row = self.capture()
        self.assertEqual(row["evcc_record_fresh"], 0)
        self.assertIsNone(row["evcc_read_ok"])
        self.assertIsNone(row["evcc_configured_raw"])
        self.assertIsNone(row["evcc_6a_limit_verified"])

    def test_16_amp_pilot_is_mismatch_not_6_amp_verified(self):
        self.change("evcc_path", evcc(raw=267))
        row = self.capture()
        self.assertEqual(row["evcc_6a_limit_verified"], 0)
        self.assertEqual(row["evcc_configured_raw"], 267)

    def test_inverter_stale_never_silently_means_no_l3_consumption(self):
        self.change("inverter_path", inverter(stamp=NOW-timedelta(minutes=3)))
        row = self.capture()
        self.assertEqual(row["inverter_fresh"], 0)
        self.assertIsNone(row["l3_grid_w"])
        self.assertIsNone(row["battery_w"])
        self.assertIsNone(row["grid_w"])

    def test_historical_unchanged_volvo_value_is_distinct_from_recent_api(self):
        self.change("ha_path", ha(api=NOW, changed=NOW-timedelta(days=10)))
        row = self.capture()
        self.assertEqual(row["volvo_api_received_at_utc"], NOW.isoformat())
        self.assertEqual(row["volvo_battery_last_changed_utc"],
                         (NOW-timedelta(days=10)).isoformat())
        self.assertEqual(row["volvo_soc_pct"], 63)

    def test_missing_or_old_api_reply_is_not_trusted(self):
        self.change("ha_path", ha(api=NOW-timedelta(minutes=6)))
        row = self.capture()
        self.assertEqual(row["volvo_observation_usable"], 0)
        self.assertIsNone(row["volvo_soc_pct"])
        self.assertIsNone(row["volvo_api_received_at_utc"])
        self.change("ha_path", ha(stamp=NOW-timedelta(minutes=4)))
        self.assertEqual(self.capture()["ha_record_fresh"], 0)

    def test_invalid_json_or_missing_file_never_crashes_sampler(self):
        self.paths["evcc_path"].write_text("{broken")
        self.paths["inverter_path"].unlink()
        row = self.capture()
        self.assertEqual(row["evcc_record_fresh"], 0)
        self.assertIsNone(row["evcc_measured_a"])
        self.assertIsNone(row["battery_w"])
        self.assertEqual(row["physical_writes"], 0)

    def test_sqlite_persists_multiple_samples_and_retention_prunes_only_own_data(self):
        first = self.capture(now=NOW - timedelta(hours=24))
        second = self.capture(now=NOW)
        history.append(str(self.db), first, retention_days=1, prune=False)
        history.append(str(self.db), second, retention_days=1, prune=True)
        with sqlite3.connect(self.db) as con:
            rows = con.execute(
                "SELECT captured_at_utc, grid_w, l3_grid_w, evcc_measured_a "
                "FROM ev_samples ORDER BY captured_epoch_ms").fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[-1][1:], (-1056, -566, 0.3))
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(con.execute("SELECT physical_writes FROM ev_samples LIMIT 1")
                             .fetchone()[0], 0)
        third = self.capture(now=NOW + timedelta(days=5))
        history.append(str(self.db), third, retention_days=1, prune=True)
        with sqlite3.connect(self.db) as con:
            rows = con.execute(
                "SELECT captured_at_utc FROM ev_samples").fetchall()
            self.assertEqual(rows, [(third["captured_at_utc"],)])

    def test_public_soc_chart_export_preserves_api_provenance_and_0640(self):
        first = self.capture()
        first["volvo_soc_pct"] = 52
        history.append(str(self.db), first)
        later_at = NOW + timedelta(minutes=6)
        later = {**first,
                 "captured_at_utc": later_at.isoformat(),
                 "captured_epoch_ms": int(later_at.timestamp() * 1000),
                 "volvo_soc_pct": 53,
                 "volvo_api_received_at_utc": later_at.isoformat()}
        history.append(str(self.db), later)
        # A failed/new sample cannot overwrite the most recent valid value.
        bad_at = later_at + timedelta(minutes=1)
        invalid = {**later, "captured_at_utc": bad_at.isoformat(),
                   "captured_epoch_ms": int(bad_at.timestamp() * 1000),
                   "volvo_quality": None, "volvo_soc_pct": 99}
        history.append(str(self.db), invalid)

        target = self.db.with_name("soc-timeline.json")
        result = history.publish_soc_timeline(
            str(self.db), str(target), now=NOW + timedelta(minutes=8))
        self.assertEqual(result["quality"], "api_reported_not_verified_bms_sample")
        self.assertEqual(len(result["points"]), 1)
        self.assertEqual(result["latest"]["time"], "2026-10-28T16:00:00")
        self.assertEqual(result["latest"]["socPct"], 53)
        self.assertEqual(result["latest"]["reportedAtUtc"],
                         first["volvo_battery_last_changed_utc"])
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o640)
        self.assertEqual(json.loads(target.read_text()), result)

    def test_soc_chart_keeps_two_autumn_dst_02_quarters_distinct(self):
        first = self.capture()
        stamps = (datetime(2026, 10, 25, 0, 15, tzinfo=timezone.utc),
                  datetime(2026, 10, 25, 1, 15, tzinfo=timezone.utc))
        for i, stamp in enumerate(stamps):
            row = {**first,
                   "captured_at_utc": stamp.isoformat(),
                   "captured_epoch_ms": int(stamp.timestamp() * 1000),
                   "volvo_soc_pct": 60+i,
                   "volvo_api_received_at_utc": stamp.isoformat(),
                   "volvo_battery_last_changed_utc":
                       (stamp-timedelta(minutes=1)).isoformat()}
            history.append(str(self.db), row)
        result = history.build_soc_timeline(
            str(self.db), now=datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc))
        self.assertEqual([p["time"] for p in result["points"]],
                         ["2026-10-25T02:15:00", "2026-10-25T02:15:00#2"])
        self.assertEqual([p["socPct"] for p in result["points"]], [60., 61.])

    def test_old_historian_schema_migrates_without_filling_past_ict2(self):
        old_schema = history.SCHEMA
        for name, kind in history.ADDED_METER_COLUMNS.items():
            old_schema = old_schema.replace(f"    {name} {kind},\n", "")
        with sqlite3.connect(self.db) as con:
            con.executescript(old_schema)
            old = self.capture()
            legacy_fields = [k for k in old if k not in history.ADDED_METER_COLUMNS]
            con.execute("INSERT INTO ev_samples (" + ",".join(legacy_fields)
                        + ") VALUES (" + ",".join("?" for _ in legacy_fields)
                        + ")", tuple(old[k] for k in legacy_fields))
            con.commit()

        new_ev = {**evcc(raw=100), "state": "C2",
                  "channel_currents_a": [.1, 6., .1],
                  "measured_current_a": 6.2, "charging_detected": True}
        self.change("evcc_path", new_ev)
        new_live = {**inverter(), "inverter_ac_w": 1400,
                    "battery_temp_c": 15., "grid_unbalanced_output_raw": 0}
        self.change("inverter_path", new_live)
        history.append(str(self.db), self.capture(), prune=False)
        with sqlite3.connect(self.db) as con:
            rows = con.execute(
                "SELECT evcc_ict2_a, grid_unbalanced_output_raw, inverter_ac_w "
                "FROM ev_samples ORDER BY id").fetchall()
            columns = {r[1] for r in con.execute("PRAGMA table_info(ev_samples)")}
        self.assertEqual(rows, [(None, None, None), (6.0, 0, 1400.)])
        self.assertTrue(set(history.ADDED_METER_COLUMNS).issubset(columns))

    def test_invalid_retention_is_rejected(self):
        for days in (-1, 0, 91, 1000):
            with self.subTest(days=days), self.assertRaises(ValueError):
                history.append(str(self.db), self.capture(),
                               retention_days=days)

    def test_history_reader_has_no_network_or_hardware_drivers(self):
        source = Path(history.__file__).read_text()
        for forbidden in (
            "import evcc_ascii", "import modbus_poller",
            "import inverter_control", "socket.create_connection",
            "set_limit_raw", "write_register", "requests.post",
        ):
            self.assertNotIn(forbidden, source)
        with mock.patch.dict(os.environ, {"EV_HISTORY_ENABLED": "0"}), \
             mock.patch.object(sys, "argv", ["ev_history.py"]), \
             redirect_stdout(io.StringIO()) as output:
            history.main()
        self.assertEqual(json.loads(output.getvalue())["writes_to_devices"], 0)
        self.assertFalse(self.db.exists())


if __name__ == "__main__":
    unittest.main()
