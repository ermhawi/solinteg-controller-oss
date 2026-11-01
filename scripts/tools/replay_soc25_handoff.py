#!/usr/bin/env python3
"""Read-only historical replay for the proposed EV-paused 25% reserve handoff.

Queries telemetry.db and ev-history.sqlite with SQLite mode=ro, including WAL.
All estimates are counterfactual; no inverter/EVCC API is imported or called.
"""
from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import soc25_handoff_shadow as policy

TELEMETRY = Path("/opt/solinteg/telemetry.db")
EV_HISTORY = Path("/opt/solinteg/ev-shadow-runtime/var/ev-history.sqlite")
READINGS = ("timestamp", "soc_pct", "battery_temp_c", "battery_w", "pv_w",
            "house_load_w", "grid_w", "inverter_ac_w", "meter_l1_w",
            "meter_l2_w", "meter_l3_w")
EV = ("captured_at_utc", "evcc_at_utc", "evcc_read_ok", "evcc_state",
      "evcc_configured_raw", "evcc_measured_a", "evcc_charging_detected",
      "physical_writes")
CONTROLS = ("timestamp", "planned_action", "power_w", "outcome")


def _utc(value: str) -> datetime:
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError("timezone required for replay")
    return t.astimezone(timezone.utc)


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def _rows(con: sqlite3.Connection, table: str, fields: tuple[str, ...],
          time_field: str, start: str, end: str) -> list[dict]:
    # SQL identifiers are module constants, never arbitrary user input.
    con.row_factory = sqlite3.Row
    query = ("SELECT " + ",".join(fields) + " FROM " + table + " WHERE "
             + time_field + " >= ? AND " + time_field + " < ? ORDER BY "
             + time_field)
    return [dict(row) for row in con.execute(query, (start, end))]


def replay(readings: list[dict], ev_samples: list[dict], controls: list[dict],
           *, max_examples: int = 5) -> dict:
    ev = sorted(ev_samples, key=lambda r: _utc(r["captured_at_utc"]))
    actions = sorted((a for a in controls if a["outcome"] == "applied"),
                     key=lambda a: _utc(a["timestamp"]))
    ev_times = [_utc(e["captured_at_utc"]) for e in ev]
    action_times = [_utc(a["timestamp"]) for a in actions]
    rows = sorted(readings, key=lambda r: _utc(r["timestamp"]))
    counts = Counter()
    unavailable_reasons = Counter()
    examples = []
    example_states = set()
    latched = False

    for sample in rows:
        now = _utc(sample["timestamp"])
        ev_idx = bisect.bisect_right(ev_times, now) - 1
        action_idx = bisect.bisect_right(action_times, now) - 1
        if (ev_idx < 0 or action_idx < 0
                or (now - ev_times[ev_idx]).total_seconds() > 60
                or (now - action_times[action_idx]).total_seconds() > 600):
            counts["unmatched"] += 1
            continue
        e, a = ev[ev_idx], actions[action_idx]
        live = sample
        evcc = {
            "timestamp": e["evcc_at_utc"],
            "read_ok": e["evcc_read_ok"] == 1,
            "state": e["evcc_state"],
            "configured_pwm_raw": e["evcc_configured_raw"],
            "measured_current_a": e["evcc_measured_a"],
            "charging_detected": (False if e["evcc_charging_detected"] == 0
                                  else True if e["evcc_charging_detected"] == 1 else None),
            "writes": e["physical_writes"],
        }
        decision = policy.assess(
            live, evcc, a["planned_action"], a["power_w"], now=now,
            recovery_latched=latched, phase_balance_assumed=True)
        latched = decision.recovery_latched
        counts[decision.state] += 1
        counts["matched"] += 1
        if evcc["state"] == "B2" and evcc["configured_pwm_raw"] == 1000:
            counts["observed_b2_pause_samples"] += 1
        if decision.state == "unavailable":
            unavailable_reasons[decision.reason] += 1
        label = (decision.state, decision.reason)
        if label not in example_states and len(examples) < max_examples:
            example_states.add(label)
            examples.append({"timestamp": sample["timestamp"],
                             "soc_pct": sample["soc_pct"],
                             "original_action": a["planned_action"],
                             "advice_state": decision.state,
                             "reason": decision.reason,
                             "counterfactual_only": True})

    # Observed transitions are evidence of what happened, not what the
    # proposed policy would have physically prevented.
    drops = []
    last_drop_at = None
    read_times = [_utc(r["timestamp"]) for r in rows]
    for a in actions:
        if a["planned_action"] != "idle":
            continue
        t = _utc(a["timestamp"])
        i = bisect.bisect_left(read_times, t)
        if i >= len(rows) or (read_times[i] - t).total_seconds() > 65:
            continue
        initial = rows[i]["soc_pct"]
        if initial is None or not 20 <= initial <= 26:
            continue
        j = bisect.bisect_right(read_times, t + timedelta(minutes=10))
        following = [r["soc_pct"] for r in rows[i:j] if r["soc_pct"] is not None]
        if (following and initial - min(following) >= 1
                and (last_drop_at is None or t - last_drop_at > timedelta(minutes=10))):
            drops.append({"idle_at_utc": a["timestamp"], "soc_at_idle_pct": initial,
                          "min_soc_next_10min_pct": min(following),
                          "observed_decline_pct": round(initial-min(following), 2)})
            last_drop_at = t
    return {
        "mode": "read_only_historical_replay",
        "device_writes": 0,
        "physical_control_authorized": False,
        "independent_phase_balance_recorded": False,
        "model_assumption": "Historical balanced inverter AC output assumed; not a validated fuse/retention control",
        "counts": dict(sorted(counts.items())),
        "unavailable_reasons": dict(unavailable_reasons.most_common(5)),
        "observed_idle_soc_drops": sorted(
            drops, key=lambda x: x["observed_decline_pct"], reverse=True)[:max_examples],
        "counterfactual_examples": examples,
    }


def run(telemetry: Path, ev_history: Path, since: str, until: str) -> dict:
    # Extra history permits joining the preceding EV/action records; only
    # readings inside the requested interval count in replay.
    early = (_utc(since) - timedelta(minutes=10)).isoformat()
    with closing(_ro(telemetry)) as con, closing(_ro(ev_history)) as ev:
        readings = _rows(con, "readings", READINGS, "timestamp", since, until)
        actions = _rows(con, "control_actions", CONTROLS, "timestamp", early, until)
        ev_samples = _rows(ev, "ev_samples", EV, "captured_at_utc", early, until)
    return replay(readings, ev_samples, actions)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--telemetry", type=Path, default=TELEMETRY)
    p.add_argument("--ev-history", type=Path, default=EV_HISTORY)
    p.add_argument("--hours", type=float, default=4)
    args = p.parse_args()
    if not 0 < args.hours <= 48:
        p.error("--hours must be between 0 and 48")
    until = datetime.now(timezone.utc)
    since = until - timedelta(hours=args.hours)
    print(json.dumps(run(args.telemetry, args.ev_history, since.isoformat(),
                         until.isoformat()), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
