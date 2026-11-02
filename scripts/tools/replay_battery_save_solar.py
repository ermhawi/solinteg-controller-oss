#!/usr/bin/env python3
"""Offline replay of battery-save solar capture using real, read-only timestamps.

Historical readings do NOT include inverter register 50006. Report strict
verification separately from a clearly labelled balanced-output assumption.
No Modbus, watchdog, EVCC or production-file writer is imported or called.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import battery_save_solar_shadow as policy

TELEMETRY = Path("/opt/solinteg/telemetry.db")
HISTORY = Path("/opt/solinteg/ev-shadow-runtime/var/ev-history.sqlite")
READINGS = ("timestamp", "soc_pct", "pv_w", "house_load_w", "grid_w",
            "battery_w", "inverter_ac_w", "work_mode_raw",
            "meter_l1_w", "meter_l2_w", "meter_l3_w")
HISTORIAN = ("captured_at_utc", "inverter_at_utc", "inverter_age_s",
             "inverter_fresh", "house_soc_pct", "pv_w", "house_load_w",
             "grid_w", "battery_w", "l1_grid_w", "l2_grid_w", "l3_grid_w")
CONTROLS = ("timestamp", "planned_action", "power_w", "armed",
            "outcome", "detail_json")
MAX_CONTROL_AGE_S = 900


def _utc(s: str) -> datetime:
    date = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if date.utcoffset() is None:
        raise ValueError("timestamp missing timezone")
    return date.astimezone(timezone.utc)


def _ro(path: Path) -> sqlite3.Connection:
    # immutable=1 would silently ignore a live DB's WAL.
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _rows(con: sqlite3.Connection, table: str, columns: tuple[str, ...],
          time_column: str, since: str, until: str) -> list[dict]:
    # SQL identifiers are fixed module constants; timestamps are parameters.
    query = ("SELECT " + ",".join(columns) + " FROM " + table
             + " WHERE " + time_column + " >= ? AND " + time_column
             + " < ? ORDER BY " + time_column)
    return [dict(row) for row in con.execute(query, (since, until))]


def _matched(h: dict, reading: dict | None, now: datetime) -> bool:
    """Require historian source time and exact source values, not nearest-sample guesses."""
    if reading is None or h.get("inverter_fresh") != 1:
        return False
    try:
        at = _utc(h["inverter_at_utc"])
        if (h["inverter_at_utc"] != reading["timestamp"]
                or not 0 <= (now - at).total_seconds() <= 60):
            return False
        pairs = (("house_soc_pct", "soc_pct"), ("pv_w", "pv_w"),
                 ("house_load_w", "house_load_w"), ("grid_w", "grid_w"),
                 ("battery_w", "battery_w"), ("l1_grid_w", "meter_l1_w"),
                 ("l2_grid_w", "meter_l2_w"), ("l3_grid_w", "meter_l3_w"))
        for recorded, original in pairs:
            a, b = h.get(recorded), reading.get(original)
            if (type(a) not in (int, float) or type(b) not in (int, float)
                    or not math.isfinite(a) or not math.isfinite(b)
                    or abs(a - b) > 0.1):
                return False
        return True
    except (ValueError, TypeError, OverflowError, KeyError):
        return False


def _save(action: dict | None, now: datetime) -> tuple[bool, float | None]:
    if action is None:
        return False, None
    try:
        age = (now - _utc(action["timestamp"])).total_seconds()
        detail = json.loads(action.get("detail_json") or "{}")
        valid = (0 <= age <= MAX_CONTROL_AGE_S
                 and action.get("planned_action") == "discharge"
                 and action.get("outcome") == "applied"
                 and action.get("armed") == 1
                 and type(action.get("power_w")) is int
                 and action["power_w"] > 0
                 and isinstance(detail, dict)
                 and type(detail.get("battery_save_w")) is int
                 and detail["battery_save_w"] == action["power_w"])
        return valid, age
    except (ValueError, TypeError, KeyError, OverflowError):
        return False, None


def _episodes(events: list[dict], max_examples: int) -> list[dict]:
    episodes: list[dict] = []
    active = None
    for entry in events:
        if entry["state"] != "solar_capture_candidate":
            if active is not None:
                episodes.append(active)
                active = None
            continue
        stamp = _utc(entry["timestamp"])
        if (active is not None and
                (stamp - _utc(active["end_utc"])).total_seconds() <= 65):
            active["end_utc"] = entry["timestamp"]
            active["sample_count"] += 1
            active["max_export_w"] = max(active["max_export_w"], entry["grid_w"])
            active["min_soc_pct"] = min(active["min_soc_pct"], entry["soc_pct"])
        else:
            if active is not None:
                episodes.append(active)
            active = {"start_utc": entry["timestamp"],
                      "end_utc": entry["timestamp"],
                      "sample_count": 1, "max_export_w": entry["grid_w"],
                      "min_soc_pct": entry["soc_pct"],
                      "historical_balance_50006_assumed": True}
    if active is not None:
        episodes.append(active)
    return episodes[:max_examples]


def _auto_transitions(readings: list[dict], controls: list[dict],
                      max_examples: int) -> list[dict]:
    events = []
    times = [_utc(r["timestamp"]) for r in readings]
    for index in range(1, len(controls)):
        row = controls[index]
        prior = controls[index - 1]
        at = _utc(row["timestamp"])
        save_prior, _ = _save(prior, at)
        if (not save_prior or row["planned_action"] != "idle"
                or row["outcome"] != "applied" or row["armed"] != 1):
            continue
        following_end = min(
            at + timedelta(minutes=10),
            _utc(controls[index + 1]["timestamp"])
            if index + 1 < len(controls) else at + timedelta(minutes=10))
        segment = [r for r in readings[
            bisect.bisect_left(times, at):bisect.bisect_left(times, following_end)]
            if r["work_mode_raw"] == 0x101]
        if not segment:
            continue
        soc = [r["soc_pct"] for r in segment if type(r["soc_pct"]) in (int, float)]
        battery = [r["battery_w"] for r in segment if type(r["battery_w"]) in (int, float)]
        if not soc or not battery:
            continue
        events.append({"idle_at_utc": row["timestamp"],
                       "first_observed_soc_pct": soc[0],
                       "min_observed_soc_pct": min(soc),
                       "max_observed_battery_discharge_w": max(battery),
                       "sample_count": len(segment),
                       "no_causal_handoff_proof": True})
    return events[:max_examples]


def stress_handoff(candidate_events: list[dict], *, reserve_pct: float,
                   exit_pct: float, capacity_kwh: float | None,
                   discharge_w: float | None, response_s: float | None) -> dict:
    """Counterfactual time-to-reserve, never proof of a safe Auto transition.

    Constant assumed discharge; actual inverter/BMS maxima and reaction
    latency are unknown. No command may depend on this estimate.
    """
    basic = {"mode": "offline_stress_only", "physical_control_authorized": False,
             "real_supervisor_latency_verified": False}
    parameters = (capacity_kwh, discharge_w, response_s)
    if all(v is None for v in parameters):
        return dict(basic, state="not_evaluated")
    if (any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0
            for v in parameters)):
        return dict(basic, state="invalid_hypothetical_parameters")
    # Recovery exit and planner reserve are separate from the true BMS floor.
    # Buffer is a REVIEW boundary, not a firmware discharge cutoff.
    review_floor = max(reserve_pct, exit_pct) + policy.SOC_BUFFER_PCT
    candidate_soc = [e["soc_pct"] for e in candidate_events
                     if e["state"] == "solar_capture_candidate"]
    if not candidate_soc:
        return dict(basic, state="no_candidate")
    seconds = [max(0.0, (soc - review_floor) / 100 * capacity_kwh * 1000)
               * 3600 / discharge_w for soc in candidate_soc]
    below = sum(s <= response_s for s in seconds)
    return dict(basic,
                state=("response_margin_insufficient" if below
                       else "physical_safety_unverified"),
                assumed_battery_capacity_kwh=capacity_kwh,
                assumed_discharge_w=discharge_w,
                assumed_response_s=response_s,
                review_floor_pct=round(review_floor, 3),
                candidate_count=len(seconds),
                candidates_below_response_budget=below,
                minimum_time_to_review_floor_s=round(min(seconds), 1),
                no_automatic_handoff=True)


def replay(history: list[dict], readings: list[dict], controls: list[dict], *,
           reserve_pct: float, entry_pct: float, exit_pct: float,
           since: datetime, until: datetime, max_examples: int = 8,
           capacity_kwh: float | None = None, discharge_w: float | None = None,
           response_s: float | None = None) -> dict:
    """Use source-exact samples and the *latest* control action, including failures."""
    history = sorted(history, key=lambda h: _utc(h["captured_at_utc"]))
    readings = sorted(readings, key=lambda r: _utc(r["timestamp"]))
    controls = sorted(controls, key=lambda c: _utc(c["timestamp"]))
    by_reading_time = {r["timestamp"]: r for r in readings}
    control_times = [_utc(c["timestamp"]) for c in controls]
    counts = Counter()
    strict_states = Counter()
    assumed_states = Counter()
    candidate_events: list[dict] = []
    stable_samples: list[dict] = []
    previous_time = None
    for h in history:
        now = _utc(h["captured_at_utc"])
        if not since <= now < until:
            # Include samples from before since so four-sample windows can cross
            # the analysis boundary, but never count them as analyzed data.
            include = False
        else:
            include = True
            counts["historian_samples"] += 1
        idx = bisect.bisect_right(control_times, now) - 1
        action = controls[idx] if idx >= 0 else None
        confirmed, age = _save(action, now)
        original = by_reading_time.get(h.get("inverter_at_utc"))
        matched = _matched(h, original, now)
        if (previous_time is not None and
                (now - previous_time).total_seconds() > 65):
            stable_samples.clear()
        previous_time = now
        # A historical window cannot span an unknown mode or a different
        # applied setpoint. This is stricter than merely using dispatch age.
        if not (matched and confirmed and original["work_mode_raw"] == 0x303):
            stable_samples.clear()
        else:
            stable_samples.append(dict(h))
            stable_samples = stable_samples[-6:]
        if not include:
            continue
        if not matched:
            counts["source_missing_or_mismatch"] += 1
            continue
        counts["source_exact_matched"] += 1
        if not confirmed:
            counts["not_confirmed_battery_save"] += 1
            continue
        counts["confirmed_battery_save_samples"] += 1
        if original["work_mode_raw"] != 0x303:
            counts["wrong_mode_samples"] += 1
        live = dict(original)
        # Unknown historical inverter register 50006 is NOT 0.
        live["grid_unbalanced_output_raw"] = None
        strict = policy.assess("discharge", action["power_w"], live,
                               stable_samples, now=now, dispatch_age_s=age,
                               battery_save_confirmed=True,
                               planner_reserve_pct=reserve_pct,
                               recovery_entry_pct=entry_pct,
                               recovery_exit_pct=exit_pct)
        strict_states[strict.state] += 1
        # Separate counterfactual only: assume 50006=0 as a hypothesis,
        # never as a reconstructed historical observation.
        assumed = policy.assess("discharge", action["power_w"],
                                {**live, "grid_unbalanced_output_raw": 0},
                                stable_samples, now=now, dispatch_age_s=age,
                                battery_save_confirmed=True,
                                planner_reserve_pct=reserve_pct,
                                recovery_entry_pct=entry_pct,
                                recovery_exit_pct=exit_pct)
        assumed_states[assumed.state] += 1
        if assumed.state == "solar_capture_candidate":
            candidate_events.append({
                "timestamp": h["captured_at_utc"],
                "state": assumed.state,
                "soc_pct": original["soc_pct"],
                "grid_w": original["grid_w"]})
        elif candidate_events:
            candidate_events.append({"timestamp": h["captured_at_utc"],
                                     "state": assumed.state})
    episodes = _episodes(candidate_events, max_examples)
    return {
        "mode": "read_only_historical_replay",
        "since_utc": since.isoformat(), "until_utc": until.isoformat(),
        "thresholds_pct": {"planner_reserve": reserve_pct, "recovery_entry": entry_pct,
                           "recovery_exit": exit_pct},
        "device_writes": 0, "physical_control_authorized": False,
        "verified_balanced_mode_samples": 0,
        "strict_verified_solar_capture_candidates": strict_states["solar_capture_candidate"],
        "historical_register_50006": "not_recorded",
        "assumed_balance_candidates_are_not_authorization": True,
        "counts": dict(sorted(counts.items())),
        "strict_states": dict(sorted(strict_states.items())),
        "assumed_balance_states": dict(sorted(assumed_states.items())),
        "assumed_balance_episode_count": len(_episodes(candidate_events, 10**6)),
        "assumed_balance_episodes": episodes,
        "hypothetical_handoff_stress": stress_handoff(
            candidate_events, reserve_pct=reserve_pct, exit_pct=exit_pct,
            capacity_kwh=capacity_kwh, discharge_w=discharge_w,
            response_s=response_s),
        "observed_actual_save_to_auto": _auto_transitions(readings, controls,
                                                          max_examples),
    }


def run(telemetry: Path, historian: Path, since: datetime, until: datetime, *,
        reserve_pct: float, entry_pct: float, exit_pct: float,
        max_examples: int = 8, capacity_kwh: float | None = None,
        discharge_w: float | None = None,
        response_s: float | None = None) -> dict:
    early = (since - timedelta(minutes=MAX_CONTROL_AGE_S // 60 + 4)).isoformat()
    with closing(_ro(telemetry)) as t, closing(_ro(historian)) as h:
        readings = _rows(t, "readings", READINGS, "timestamp", early, until.isoformat())
        controls = _rows(t, "control_actions", CONTROLS,
                         "timestamp", early, until.isoformat())
        samples = _rows(h, "ev_samples", HISTORIAN,
                        "captured_at_utc", early, until.isoformat())
    return replay(samples, readings, controls, since=since, until=until,
                  reserve_pct=reserve_pct, entry_pct=entry_pct, exit_pct=exit_pct,
                  max_examples=max_examples, capacity_kwh=capacity_kwh,
                  discharge_w=discharge_w, response_s=response_s)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--telemetry", type=Path, default=TELEMETRY)
    parser.add_argument("--history", type=Path, default=HISTORY)
    parser.add_argument("--since", required=True, help="ISO 8601 with UTC offset, inclusive")
    parser.add_argument("--until", required=True, help="ISO 8601 with UTC offset, exclusive")
    parser.add_argument("--planner-reserve-pct", type=float, required=True)
    parser.add_argument("--recovery-entry-pct", type=float, required=True)
    parser.add_argument("--recovery-exit-pct", type=float, required=True)
    parser.add_argument("--max-examples", type=int, default=8)
    parser.add_argument("--capacity-kwh", type=float,
                        help="explicit stress assumption: usable battery capacity")
    parser.add_argument("--stress-discharge-w", type=float,
                        help="explicit stress assumption: possible battery discharge W")
    parser.add_argument("--response-budget-s", type=float,
                        help="hypothetical failback latency, NOT measured")
    args = parser.parse_args()
    try:
        start, end = _utc(args.since), _utc(args.until)
        if (not start < end or end - start > timedelta(days=2)
                or not 0 <= args.max_examples <= 20
                or not 0 <= args.recovery_entry_pct < args.recovery_exit_pct < 98
                or not 0 <= args.planner_reserve_pct < 98):
            parser.error("invalid time range, thresholds or max_examples")
        result = run(args.telemetry, args.history, start, end,
                     reserve_pct=args.planner_reserve_pct,
                     entry_pct=args.recovery_entry_pct,
                     exit_pct=args.recovery_exit_pct,
                     max_examples=args.max_examples,
                     capacity_kwh=args.capacity_kwh,
                     discharge_w=args.stress_discharge_w,
                     response_s=args.response_budget_s)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(type(exc).__name__ + ": " + str(exc)[:120])
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
