#!/usr/bin/env python3
"""Replay correlated EVCC and battery history without changing either database.

The EV historian contains no per-sample inverter register 50006 or Ict2.
Replay marks both phase balance and 6 A channel allocation as assumptions;
any counterfactual is strictly non-actuating and not sufficient to arm policy.
"""
from __future__ import annotations

import argparse
import bisect
import json
from contextlib import closing
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import ev_grid_priority_solar_shadow as solar

HISTORY = Path("/opt/solinteg/ev-shadow-runtime/var/ev-history.sqlite")
CONTROLS = Path("/opt/solinteg/telemetry.db")
SAMPLE_COLUMNS = (
    "captured_at_utc", "inverter_at_utc", "evcc_at_utc", "inverter_fresh",
    "evcc_record_fresh", "evcc_read_ok", "evcc_state", "evcc_charging_detected",
    "evcc_6a_limit_verified", "evcc_measured_a", "house_soc_pct", "pv_w",
    "battery_w", "grid_w", "house_load_w", "l1_grid_w", "l2_grid_w", "l3_grid_w")
CONTROL_COLUMNS = ("timestamp", "planned_action", "power_w", "outcome")
MAX_CONTROL_AGE_S = 900
MAX_GAP_S = 91


def _ro_connect(path: Path) -> sqlite3.Connection:
    # sqlite immutable=1 would miss the historian's WAL; use mode=ro instead.
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def _utc(s: str) -> datetime:
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError("timestamps must carry a timezone")
    return d.astimezone(timezone.utc)


def _rows(con: sqlite3.Connection, table: str, fields: tuple[str, ...],
          since: str, until: str) -> list[dict]:
    # Table and fields are constants in this module, not CLI-provided SQL.
    q = ("SELECT " + ",".join(fields) + " FROM " + table
         + " WHERE " + ("captured_at_utc" if table == "ev_samples" else "timestamp")
         + " >= ? AND " + ("captured_at_utc" if table == "ev_samples" else "timestamp")
         + " < ? ORDER BY " + ("captured_at_utc" if table == "ev_samples" else "timestamp"))
    con.row_factory = sqlite3.Row
    return [dict(row) for row in con.execute(q, (since, until))]


def verified_ev(sample: dict) -> bool:
    return (sample.get("inverter_fresh") == 1
            and sample.get("evcc_record_fresh") == 1
            and sample.get("evcc_read_ok") == 1
            and sample.get("evcc_state") == "C2"
            and sample.get("evcc_charging_detected") == 1
            and sample.get("evcc_6a_limit_verified") == 1
            and isinstance(sample.get("evcc_measured_a"), (int, float))
            and 5.8 <= sample["evcc_measured_a"] <= 6.7
            and all(sample.get(k) is not None for k in
                    ("l1_grid_w", "l2_grid_w", "l3_grid_w", "pv_w",
                     "battery_w", "grid_w", "house_load_w", "house_soc_pct")))


def action_at(sample_time: datetime, controls: list[dict],
              control_times: list[datetime]) -> dict | None:
    idx = bisect.bisect_right(control_times, sample_time) - 1
    if idx < 0:
        return None
    elapsed = (sample_time - control_times[idx]).total_seconds()
    return controls[idx] if 0 <= elapsed <= MAX_CONTROL_AGE_S else None


def scenario(sample: dict, action: dict | None) -> solar.ShadowAdvice:
    now = _utc(sample["captured_at_utc"])
    # Signed import at each CT is negative; AC reconstructed without
    # claiming its source (PV or house battery) is independently metered.
    live = {
        "timestamp": sample.get("inverter_at_utc") or sample["captured_at_utc"],
        "grid_unbalanced_output_raw": 0,  # HISTORICAL ASSUMPTION, not evidence
        "soc_pct": sample["house_soc_pct"],
        "pv_w": sample["pv_w"],
        "battery_w": sample["battery_w"],
        "grid_w": sample["grid_w"],
        "house_load_w": sample["house_load_w"],
        "inverter_ac_w": sample["grid_w"] + sample["house_load_w"],
        "meter_l1_w": sample["l1_grid_w"],
        "meter_l2_w": sample["l2_grid_w"],
        "meter_l3_w": sample["l3_grid_w"],
    }
    # Historic EVCC recorded the summed internal channel current, not
    # Ict2 separately. Approximate two other channels at 0.1 A each.
    amps = sample["evcc_measured_a"]
    evcc = {
        "timestamp": sample.get("evcc_at_utc") or sample["captured_at_utc"],
        "source": "waveshare_evcc_fc03_readonly",
        "read_ok": True, "state": "C2", "charging_detected": True,
        "physical_control_eligible": False, "writes": 0,
        "pilot_pwm_raw": 100, "configured_pwm_raw": 100,
        "channel_currents_a": [.1, round(amps - .2, 2), .1],
        "measured_current_a": amps,
    }
    return solar.assess(action["planned_action"] if action else "unknown",
                        int(action["power_w"]) if action and action["power_w"] is not None else 0,
                        live, evcc, now=now, phase_assumed=True)


def replay(samples: list[dict], controls: list[dict], *,
           max_examples: int = 6) -> dict:
    controls = sorted(controls, key=lambda c: _utc(c["timestamp"]))
    times = [_utc(c["timestamp"]) for c in controls]
    counts = Counter()
    events: list[dict] = []
    current: dict | None = None
    previous: datetime | None = None
    for sample in sorted(samples, key=lambda s: _utc(s["captured_at_utc"])):
        counts["total_samples"] += 1
        stamp = _utc(sample["captured_at_utc"])
        good = verified_ev(sample)
        if not good:
            if current:
                events.append(current)
                current = None
            previous = stamp
            continue
        counts["verified_ev_samples"] += 1
        action = action_at(stamp, controls, times)
        if sample["pv_w"] > 100:
            counts["pv_over_100_samples"] += 1
        if action:
            counts["action_" + action["planned_action"]] += 1
            if action["planned_action"] == "idle":
                # This window tests whether the historical AUTO gap can
                # validate the NEW policy: low SoC forbids a 50 W hold.
                counts["idle_with_verified_ev"] += 1
                if 25 <= sample["house_soc_pct"] <= 95:
                    counts["idle_in_shadow_soc_band"] += 1
                else:
                    counts["idle_outside_shadow_soc_band"] += 1
        else:
            counts["action_unknown"] += 1
        advice = scenario(sample, action)
        counts["shadow_" + advice.state] += 1
        discharge = sample["battery_w"] > 500
        if discharge:
            counts["simultaneous_battery_discharge_samples"] += 1
            label = "unknown_applied_action"
            if action:
                if action["planned_action"] == "idle":
                    label = "idle_auto"
                elif action["planned_action"] == "charge":
                    label = "charge_pending_response"
                elif action["planned_action"] == "discharge":
                    label = "planned_discharge"
            if (current is not None and previous is not None
                    and (stamp-previous).total_seconds() <= MAX_GAP_S
                    and label == current["classification"]):
                current["end_utc"] = stamp.isoformat()
                current["sample_count"] += 1
                current["peak_battery_discharge_w"] = max(
                    current["peak_battery_discharge_w"], round(sample["battery_w"]))
                current["peak_pv_w"] = max(current["peak_pv_w"], round(sample["pv_w"]))
            else:
                if current:
                    events.append(current)
                current = {
                    "start_utc": stamp.isoformat(),
                    "end_utc": stamp.isoformat(),
                    "classification": label,
                    "sample_count": 1,
                    "peak_battery_discharge_w": round(sample["battery_w"]),
                    "peak_pv_w": round(sample["pv_w"]),
                    "causal_battery_to_ev_proof": False,
                }
        elif current:
            events.append(current)
            current = None
        previous = stamp
    if current:
        events.append(current)
    counts["discharge_episodes"] = len(events)
    return {
        "mode": "read_only_historical_replay",
        "device_writes": 0,
        "independent_phase_balance_recorded": False,
        "independent_evcc_ict2_recorded": False,
        "counts": dict(sorted(counts.items())),
        "examples": sorted(events, key=lambda x: x["peak_battery_discharge_w"],
                           reverse=True)[:max_examples],
        "interpretation": ("Coincident battery discharge and verified EV charging "
                           "does not establish which physical source supplied the EV."),
    }


def run(history: Path, controls: Path, since: str, until: str, *,
        max_examples: int = 6) -> dict:
    with closing(_ro_connect(history)) as h, closing(_ro_connect(controls)) as c:
        samples = _rows(h, "ev_samples", SAMPLE_COLUMNS, since, until)
        actions = _rows(c, "control_actions", CONTROL_COLUMNS,
                        datetime.fromtimestamp(
                            _utc(since).timestamp()-MAX_CONTROL_AGE_S,
                            tz=timezone.utc).isoformat(), until)
    return replay(samples, actions, max_examples=max_examples)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, default=HISTORY)
    parser.add_argument("--controls", type=Path, default=CONTROLS)
    parser.add_argument("--since", required=True, help="ISO 8601 UTC inclusive")
    parser.add_argument("--until", required=True, help="ISO 8601 UTC exclusive")
    parser.add_argument("--max-examples", type=int, default=6)
    args = parser.parse_args()
    try:
        since, until = _utc(args.since), _utc(args.until)
        if not since < until or (until-since).days > 31 or not 0 <= args.max_examples <= 20:
            parser.error("invalid UTC range or max examples")
        result = run(args.history, args.controls,
                     since.isoformat(), until.isoformat(),
                     max_examples=args.max_examples)
    except (sqlite3.Error, OSError, ValueError) as exc:
        parser.error(type(exc).__name__ + ": " + str(exc)[:100])
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
