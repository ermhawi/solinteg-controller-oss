#!/usr/bin/env python3
"""Replay only independently recorded EVCC Ict2 + inverter CT evidence.

Older historical rows contain only summed EV current, not Ict2 or 50006.
They are NOT upgraded into invented measurements. Every candidate is an
offline hypothetical 2.3 kW discharge (not the actual dispatch decision).
No EVCC/Modbus network, writes, gates or production changes.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from ev_l3_metered_policy import assess  # noqa: E402

HISTORY = Path("/opt/solinteg/ev-shadow-runtime/var/ev-history.sqlite")
SAMPLE_COLUMNS = (
    "captured_at_utc", "inverter_at_utc", "evcc_at_utc", "inverter_fresh",
    "evcc_record_fresh", "evcc_read_ok", "evcc_state", "evcc_charging_detected",
    "evcc_configured_raw", "evcc_pilot_raw", "evcc_measured_a",
    "evcc_ict1_a", "evcc_ict2_a", "evcc_ict3_a",
    "l1_grid_w", "l2_grid_w", "l3_grid_w", "grid_w", "inverter_ac_w",
    "battery_w", "house_load_w", "pv_w", "house_soc_pct",
    "battery_temp_c", "grid_unbalanced_output_raw",
)
PREVIEW_DISCHARGE_W = 2300


def _utc(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("timezone required")
    return stamp.astimezone(timezone.utc)


def replay(rows: list[dict]) -> dict:
    counts: Counter = Counter()
    max_ev_w = None
    for row in rows:
        counts["history_samples"] += 1
        if row.get("evcc_state") != "C2":
            continue
        counts["reported_c2_samples"] += 1
        if not (row.get("inverter_fresh") == 1
                and row.get("evcc_record_fresh") == 1
                and row.get("evcc_read_ok") == 1
                and row.get("evcc_charging_detected") == 1):
            counts["c2_unverified_state"] += 1
            continue
        if any(row.get(k) is None for k in (
                "evcc_ict1_a", "evcc_ict2_a", "evcc_ict3_a",
                "grid_unbalanced_output_raw", "inverter_ac_w",
                "battery_temp_c")):
            counts["c2_missing_independent_evidence"] += 1
            continue
        live = {
            "timestamp": row.get("inverter_at_utc"),
            "grid_unbalanced_output_raw": row.get("grid_unbalanced_output_raw"),
            "inverter_ac_w": row.get("inverter_ac_w"),
            "battery_temp_c": row.get("battery_temp_c"),
            "meter_l1_w": row.get("l1_grid_w"),
            "meter_l2_w": row.get("l2_grid_w"),
            "meter_l3_w": row.get("l3_grid_w"),
            "grid_w": row.get("grid_w"),
            "battery_w": row.get("battery_w"),
            "house_load_w": row.get("house_load_w"),
            "pv_w": row.get("pv_w"),
            "soc_pct": row.get("house_soc_pct"),
        }
        evcc = {
            "timestamp": row.get("evcc_at_utc"),
            "source": "waveshare_evcc_fc03_readonly",
            "read_ok": True, "state": row.get("evcc_state"),
            "charging_detected": True, "physical_control_eligible": False,
            "writes": 0,
            "configured_pwm_raw": row.get("evcc_configured_raw"),
            "pilot_pwm_raw": row.get("evcc_pilot_raw"),
            "measured_current_a": row.get("evcc_measured_a"),
            "channel_currents_a": [
                row["evcc_ict1_a"], row["evcc_ict2_a"], row["evcc_ict3_a"]],
        }
        advice = assess("discharge", PREVIEW_DISCHARGE_W, live, evcc,
                        now=_utc(row["captured_at_utc"]))
        counts["policy_" + advice.state] += 1
        ev_w = advice.measurements.get("ev_power_w_est")
        if ev_w is not None:
            max_ev_w = ev_w if max_ev_w is None else max(max_ev_w, ev_w)
            counts["samples_with_valid_ev_power_estimate"] += 1
    return {
        "mode": "read_only_metered_ev_l3_replay",
        "synthetic_discharge_preview_w": PREVIEW_DISCHARGE_W,
        "samples": dict(counts),
        "max_ev_w_est": max_ev_w,
        "original_dispatch_targets_used": False,
        "historical_ict2_assumed": False,
        "historical_50006_assumed": False,
        "physical_control_authorized": False,
        "device_writes": 0,
    }


def scan(path: Path, *, since: datetime, until: datetime,
         max_samples: int = 10000) -> dict:
    if (since.tzinfo is None or until.tzinfo is None or since >= until
            or not 1 <= max_samples <= 100000):
        raise ValueError("valid time interval and max_samples required")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro",
                                 uri=True)) as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info(ev_samples)")}
        missing = set(SAMPLE_COLUMNS) - columns
        if missing:
            return {
                "mode": "read_only_metered_ev_l3_replay", "status": "missing_schema",
                "missing_columns": sorted(missing), "device_writes": 0,
                "physical_control_authorized": False,
            }
        con.row_factory = sqlite3.Row
        rows = [dict(x) for x in con.execute(
            "SELECT " + ",".join(SAMPLE_COLUMNS)
            + " FROM ev_samples WHERE captured_at_utc >= ? AND captured_at_utc < ?"
            + " ORDER BY captured_epoch_ms LIMIT ?",
            (since.isoformat(), until.isoformat(), max_samples))]
        return replay(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, default=HISTORY)
    parser.add_argument("--since", type=str)
    parser.add_argument("--until", type=str)
    parser.add_argument("--max-samples", type=int, default=10000)
    args = parser.parse_args()
    until = _utc(args.until) if args.until else datetime.now(timezone.utc)
    since = _utc(args.since) if args.since else until - timedelta(days=1)
    try:
        result = scan(args.history, since=since, until=until,
                      max_samples=args.max_samples)
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        print(json.dumps({"mode": "read_only_metered_ev_l3_replay",
                          "status": "unavailable", "reason": type(exc).__name__,
                          "device_writes": 0}))
        raise SystemExit(2) from exc
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
