"""Historical replay must classify idle/Auto and response lag without control writes."""
from __future__ import annotations

import importlib.util
import sqlite3
from contextlib import closing
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "ev_grid_priority_replay.py"
spec = importlib.util.spec_from_file_location("ev_grid_priority_replay", TOOL)
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)
NOW = datetime(2026, 10, 31, 8, 50, tzinfo=timezone.utc)


def record(offset: int, battery: float, *, pv: float = 0.0,
           verified: bool = True) -> dict:
    at = (NOW + timedelta(seconds=offset)).isoformat()
    return {
        "captured_at_utc": at, "inverter_at_utc": at, "evcc_at_utc": at,
        "inverter_fresh": 1, "evcc_record_fresh": 1,
        "evcc_read_ok": 1, "evcc_state": "C2",
        "evcc_charging_detected": 1, "evcc_6a_limit_verified": int(verified),
        "evcc_measured_a": 6.2, "house_soc_pct": 64,
        "pv_w": pv, "battery_w": battery, "grid_w": -160,
        "house_load_w": 2359 + pv + battery - 2155,
        "l1_grid_w": 269, "l2_grid_w": 362, "l3_grid_w": -792,
    }


def control(offset: int, action: str, power: int, outcome: str = "applied") -> dict:
    return {"timestamp": (NOW+timedelta(seconds=offset)).isoformat(),
            "planned_action": action, "power_w": power, "outcome": outcome}


class SolarReplayTests(unittest.TestCase):
    def test_idle_auto_episode_and_charge_pending_are_separate(self):
        samples = [record(10, 2100), record(40, 4200),
                   record(70, 1700), record(100, -300)]
        controls = [control(0, "idle", 0),
                    control(65, "charge", 300,
                            "accepted_pending_physical_confirmation")]
        report = replay.replay(samples, controls)
        self.assertEqual(report["counts"]["verified_ev_samples"], 4)
        self.assertEqual(report["counts"]["simultaneous_battery_discharge_samples"], 3)
        self.assertEqual(report["counts"]["discharge_episodes"], 2)
        self.assertEqual({x["classification"] for x in report["examples"]},
                         {"idle_auto", "charge_pending_response"})
        self.assertEqual(report["device_writes"], 0)
        self.assertFalse(report["independent_phase_balance_recorded"])

    def test_malformed_or_unverified_evcc_is_not_charging(self):
        sample = record(20, 4200, verified=False)
        report = replay.replay([sample], [control(0, "idle", 0)])
        self.assertEqual(report["counts"]["total_samples"], 1)
        self.assertNotIn("verified_ev_samples", report["counts"])
        self.assertEqual(report["counts"]["discharge_episodes"], 0)

    def test_missing_applied_action_does_not_claim_auto(self):
        sample = record(1000, 3000)
        report = replay.replay([sample], [control(0, "idle", 0)])
        self.assertEqual(report["counts"]["action_unknown"], 1)
        self.assertEqual(report["examples"][0]["classification"],
                         "unknown_applied_action")

    def test_gap_creates_separate_episodes(self):
        samples = [record(0, 3000), record(180, 2500)]
        report = replay.replay(samples, [control(0, "discharge", 3000)])
        self.assertEqual(report["counts"]["discharge_episodes"], 2)

    def test_ro_sqlite_roundtrip_and_control_lookback(self):
        with tempfile.TemporaryDirectory() as folder:
            history = Path(folder) / "history.sqlite"
            controls = Path(folder) / "controls.sqlite"
            samples = [record(10, 4000)]
            actions = [control(-80, "idle", 0)]
            with closing(sqlite3.connect(history)) as conn:
                cols = replay.SAMPLE_COLUMNS
                conn.execute("CREATE TABLE ev_samples ("
                             + ",".join(n + " TEXT" if n.endswith("_utc")
                                        or n in ("evcc_state",)
                                        else n + " REAL" for n in cols) + ")")
                conn.execute("INSERT INTO ev_samples VALUES (" +
                             ",".join("?" for _ in cols) + ")",
                             tuple(samples[0][n] for n in cols))
                conn.commit()
            with closing(sqlite3.connect(controls)) as conn:
                conn.execute("CREATE TABLE control_actions "
                             "(timestamp TEXT,planned_action TEXT,power_w INTEGER,outcome TEXT)")
                conn.execute("INSERT INTO control_actions VALUES (?,?,?,?)",
                             tuple(actions[0][n] for n in replay.CONTROL_COLUMNS))
                conn.commit()
            before = (history.stat().st_size, controls.stat().st_size)
            report = replay.run(history, controls,
                                (NOW-timedelta(minutes=1)).isoformat(),
                                (NOW+timedelta(minutes=1)).isoformat())
            self.assertEqual(report["examples"][0]["classification"], "idle_auto")
            self.assertEqual(before, (history.stat().st_size, controls.stat().st_size))


if __name__ == "__main__":
    unittest.main()
