"""Real-timestamp, read-only battery-save solar replay and handoff regressions."""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import replay_battery_save_solar as replay

START = datetime(2026, 11, 2, 11, 45, tzinfo=timezone.utc)


def at(seconds: int) -> str:
    return (START + timedelta(seconds=seconds)).isoformat()


def original(seconds=0, *, pv=2200, load=900, battery=200, soc=22,
             mode=0x303) -> dict:
    grid = pv + battery - load
    return {"timestamp": at(seconds), "soc_pct": soc,
            "pv_w": pv, "house_load_w": load, "battery_w": battery,
            "grid_w": grid, "inverter_ac_w": pv + battery,
            "work_mode_raw": mode,
            "meter_l1_w": grid // 3, "meter_l2_w": grid // 3,
            "meter_l3_w": grid - 2 * (grid // 3)}


def sample(raw: dict, *, delay=3) -> dict:
    return {"captured_at_utc": (
                replay._utc(raw["timestamp"]) + timedelta(seconds=delay)).isoformat(),
            "inverter_at_utc": raw["timestamp"], "inverter_age_s": delay,
            "inverter_fresh": 1, "house_soc_pct": raw["soc_pct"],
            "pv_w": raw["pv_w"], "house_load_w": raw["house_load_w"],
            "battery_w": raw["battery_w"], "grid_w": raw["grid_w"],
            "l1_grid_w": raw["meter_l1_w"],
            "l2_grid_w": raw["meter_l2_w"],
            "l3_grid_w": raw["meter_l3_w"]}


def save_action(seconds=-300, *, armed=1, marker=50, outcome="applied") -> dict:
    return {"timestamp": at(seconds), "planned_action": "discharge",
            "power_w": 50, "armed": armed, "outcome": outcome,
            "detail_json": json.dumps({"battery_save_w": marker})}


def fixtures():
    readings = [original(i * 30) for i in range(7)]
    history = [sample(r) for r in readings]
    return history, readings, [save_action()]


def analyze(history, readings, controls):
    return replay.replay(history, readings, controls,
                         reserve_pct=20, entry_pct=19.5, exit_pct=21,
                         since=START, until=START+timedelta(minutes=15))


class ReplayBatterySaveTests(unittest.TestCase):
    def test_four_real_source_timestamps_yield_hypothesis_not_physical_permission(self):
        h, r, c = fixtures()
        result = analyze(h, r, c)
        self.assertEqual(result["counts"]["source_exact_matched"], 7)
        self.assertEqual(result["counts"]["confirmed_battery_save_samples"], 7)
        self.assertEqual(result["strict_verified_solar_capture_candidates"], 0)
        self.assertEqual(result["strict_states"], {"mode_unverified": 7})
        self.assertGreater(result["assumed_balance_states"]["solar_capture_candidate"], 0)
        self.assertEqual(result["assumed_balance_episode_count"], 1)
        self.assertTrue(result["assumed_balance_episodes"][0]
                        ["historical_balance_50006_assumed"])
        self.assertFalse(result["physical_control_authorized"])
        self.assertEqual(result["device_writes"], 0)

    def test_no_provenance_or_disarmed_has_no_candidates(self):
        h, r, c = fixtures()
        for control in ({**c[0], "armed": 0},
                        {**c[0], "detail_json": "{}"},
                        {**c[0], "outcome": "error_revert_failed"}):
            with self.subTest(control=control):
                result = analyze(h, r, [control])
                self.assertEqual(result["counts"]["not_confirmed_battery_save"], 7)
                self.assertNotIn("solar_capture_candidate", result["assumed_balance_states"])

    def test_source_mismatch_is_not_filled_from_nearest_reading(self):
        h, r, c = fixtures()
        h[3]["pv_w"] += 400
        result = analyze(h, r, c)
        self.assertEqual(result["counts"]["source_missing_or_mismatch"], 1)
        # Four new, contiguous matched samples cannot span the corrupted row.
        self.assertEqual(result["assumed_balance_states"].get("solar_capture_candidate", 0), 0)

    def test_missing_phase_ct_invalidates_source_sample(self):
        h, r, c = fixtures()
        h[3]["l3_grid_w"] = None
        r[3]["meter_l3_w"] = None
        result = analyze(h, r, c)
        self.assertEqual(result["counts"]["source_missing_or_mismatch"], 1)
        self.assertEqual(result["strict_verified_solar_capture_candidates"], 0)
        self.assertNotIn("solar_capture_candidate", result["assumed_balance_states"])

    def test_duplicate_original_inverter_timestamp_is_not_four_measurements(self):
        h, r, c = fixtures()
        h = copy.deepcopy(h)
        for i in range(1, len(h)):
            h[i] = sample(r[0], delay=i * 30 + 3)
        result = analyze(h, r, c)
        self.assertNotIn("solar_capture_candidate", result["assumed_balance_states"])
        self.assertEqual(result["strict_verified_solar_capture_candidates"], 0)

    def test_unexpected_mode_change_invalidates_history(self):
        h, r, c = fixtures()
        r[3]["work_mode_raw"] = 0x101
        result = analyze(h, r, c)
        self.assertEqual(result["counts"]["wrong_mode_samples"], 1)
        self.assertEqual(result["assumed_balance_states"].get("solar_capture_candidate", 0), 0)

    def test_abrupt_solar_loss_stops_hypothetical_handoff(self):
        h, r, c = fixtures()
        for i in range(7, 10):
            raw = original(i * 30, pv=300, load=900, battery=200, soc=21.5)
            r.append(raw)
            h.append(sample(raw))
        result = analyze(h, r, c)
        self.assertEqual(result["assumed_balance_episode_count"], 1)
        self.assertEqual(result["assumed_balance_episodes"][0]["end_utc"], h[6]["captured_at_utc"])
        self.assertGreater(result["assumed_balance_states"].get(
            "no_stable_solar_opportunity", 0), 0)
        self.assertEqual(result["strict_verified_solar_capture_candidates"], 0)

    def test_soc_reserve_preempts_even_stable_solar(self):
        h, r, c = fixtures()
        for i in range(len(h)):
            r[i]["soc_pct"] = 21
            h[i]["house_soc_pct"] = 21
        result = analyze(h, r, c)
        self.assertEqual(result["assumed_balance_states"].get(
            "reserve_priority"), 7)
        self.assertNotIn("solar_capture_candidate", result["assumed_balance_states"])

    def test_latest_control_failure_is_not_skipped(self):
        h, r, c = fixtures()
        c.append({**save_action(80), "outcome": "error_revert_failed"})
        result = analyze(h, r, c)
        self.assertEqual(result["counts"]["not_confirmed_battery_save"], 4)
        self.assertNotIn("solar_capture_candidate", result["assumed_balance_states"])

    def test_actual_auto_transition_is_observed_not_assumed_safe(self):
        h, r, c = fixtures()
        raw1 = original(240, mode=0x101, pv=100, load=900, battery=800,
                        soc=21)
        raw2 = original(270, mode=0x101, pv=100, load=900, battery=800,
                        soc=20)
        r += [raw1, raw2]
        h += [sample(raw1), sample(raw2)]
        c.append({"timestamp": at(230), "planned_action": "idle",
                  "power_w": 0, "armed": 1, "outcome": "applied",
                  "detail_json": "{}"})
        result = analyze(h, r, c)
        event = result["observed_actual_save_to_auto"][0]
        self.assertEqual(event["min_observed_soc_pct"], 20)
        self.assertEqual(event["max_observed_battery_discharge_w"], 800)
        self.assertTrue(event["no_causal_handoff_proof"])

    def test_db_run_is_readonly_and_uses_real_source_timestamps(self):
        h, r, c = fixtures()
        with tempfile.TemporaryDirectory() as tmp:
            telemetry = Path(tmp) / "telemetry.db"
            historian = Path(tmp) / "ev-history.sqlite"
            with closing(sqlite3.connect(telemetry)) as con:
                con.execute("CREATE TABLE readings (" +
                            ",".join(k+" TEXT" if k=="timestamp" else k+" INTEGER" if k=="work_mode_raw" else k+" REAL"
                                     for k in replay.READINGS) + ")")
                con.execute("CREATE TABLE control_actions (" +
                            ",".join(k+" INTEGER" if k in ("power_w", "armed") else k+" TEXT" for k in replay.CONTROLS) + ")")
                con.executemany(
                    "INSERT INTO readings VALUES (" +
                    ",".join("?" for _ in replay.READINGS)+")",
                    [tuple(x[k] for k in replay.READINGS) for x in r])
                con.executemany(
                    "INSERT INTO control_actions VALUES (" +
                    ",".join("?" for _ in replay.CONTROLS)+")",
                    [tuple(x[k] for k in replay.CONTROLS) for x in c])
                con.commit()
            with closing(sqlite3.connect(historian)) as con:
                con.execute("CREATE TABLE ev_samples (" +
                            ",".join(k+" TEXT" if k in (
                                "captured_at_utc","inverter_at_utc")
                                else k+" INTEGER" if k=="inverter_fresh"
                                else k+" REAL" for k in replay.HISTORIAN) + ")")
                con.executemany(
                    "INSERT INTO ev_samples VALUES (" +
                    ",".join("?" for _ in replay.HISTORIAN)+")",
                    [tuple(x[k] for k in replay.HISTORIAN) for x in h])
                con.commit()
            before = {str(p):hashlib.sha256(p.read_bytes()).digest()
                      for p in (telemetry, historian)}
            result = replay.run(telemetry, historian,
                                START, START+timedelta(minutes=15),
                                reserve_pct=20, entry_pct=19.5, exit_pct=21)
            self.assertGreater(result["assumed_balance_states"]
                               ["solar_capture_candidate"], 0)
            self.assertEqual(before, {str(p):hashlib.sha256(p.read_bytes()).digest()
                                     for p in (telemetry, historian)})
            self.assertFalse(any(Path(tmp).glob("*-wal")))
            self.assertFalse(any(Path(tmp).glob("*-shm")))

    def test_handoff_stress_shows_short_50s_margin_at_22pct(self):
        h, r, c = fixtures()
        for i in range(len(h)):
            h[i]["house_soc_pct"] = 22
            r[i]["soc_pct"] = 22
        result = replay.replay(h, r, c, reserve_pct=20, entry_pct=19.5,
                               exit_pct=21, since=START,
                               until=START+timedelta(minutes=15),
                               capacity_kwh=15.055, discharge_w=5363,
                               response_s=120)
        stress = result["hypothetical_handoff_stress"]
        self.assertEqual(stress["state"], "response_margin_insufficient")
        self.assertEqual(stress["candidates_below_response_budget"],
                         stress["candidate_count"])
        self.assertAlmostEqual(stress["minimum_time_to_review_floor_s"], 50.5, delta=.2)
        self.assertFalse(stress["real_supervisor_latency_verified"])
        self.assertFalse(stress["physical_control_authorized"])

    def test_stress_never_authorizes_even_with_slow_assumed_discharge(self):
        h, r, c = fixtures()
        result = replay.replay(h, r, c, reserve_pct=20, entry_pct=19.5,
                               exit_pct=21, since=START,
                               until=START+timedelta(minutes=15),
                               capacity_kwh=15.055, discharge_w=500,
                               response_s=30)
        risk = result["hypothetical_handoff_stress"]
        self.assertEqual(risk["state"], "physical_safety_unverified")
        self.assertTrue(risk["no_automatic_handoff"])
        self.assertFalse(risk["physical_control_authorized"])

    def test_missing_stress_inputs_are_not_assumed_verified(self):
        h, r, c = fixtures()
        result = analyze(h, r, c)
        self.assertEqual(result["hypothetical_handoff_stress"]["state"],
                         "not_evaluated")
        partial = replay.stress_handoff(
            [{"state": "solar_capture_candidate", "soc_pct": 22}],
            reserve_pct=20, exit_pct=21,
            capacity_kwh=15.055, discharge_w=None, response_s=120)
        self.assertEqual(partial["state"], "invalid_hypothetical_parameters")
        self.assertFalse(partial["physical_control_authorized"])

    def test_no_network_or_actuator_import_in_offline_replay(self):
        source = Path(replay.__file__).read_text(encoding="utf-8").lower()
        for forbidden in ("inverter_control", "modbus_tcp", "write_register",
                          "requests.", "socket.", "systemctl", "subprocess."):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
