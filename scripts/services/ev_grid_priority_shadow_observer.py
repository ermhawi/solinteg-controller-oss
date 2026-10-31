#!/usr/bin/env python3
"""Independent read-only EV battery priority shadow observer.

No inverter/EVCC driver, no network, no control register writes. Reads the
actual dispatch decision from its existing SQLite DB in read-only mode and
publishes a private, advisory JSON snapshot only.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from ev_grid_priority_solar_shadow import assess

LIVE = Path("/opt/solinteg/live.json")
EVCC = Path("/opt/solinteg/ev-shadow-runtime/var/evcc-readonly.json")
CONTROLS = Path("/opt/solinteg/telemetry.db")
GATE = Path("/var/lib/solinteg-ev-grid-priority/gate.json")
OUTPUT = Path("/opt/solinteg/ev-shadow-runtime/var/ev-grid-priority-solar-shadow.json")
MAX_CONTROL_AGE_S = 900


def _utc(value: str) -> datetime:
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise ValueError("dispatch time missing timezone")
    return date.astimezone(timezone.utc)


def last_applied(controls: Path, *, now: datetime) -> tuple[str, int, dict]:
    """Unknown is safer than treating a stale row as the inverter's state."""
    with closing(sqlite3.connect(controls.resolve().as_uri() + "?mode=ro", uri=True)) as con:
        row = con.execute(
            "SELECT timestamp, planned_action, power_w, outcome "
            "FROM control_actions ORDER BY id DESC LIMIT 1").fetchone()
    if row is None or row[2] is None:
        return "unknown", 0, {"dispatch_reason": "no applied decision"}
    age = (now - _utc(row[0])).total_seconds()
    if not 0 <= age <= MAX_CONTROL_AGE_S or row[3] in (
            "error_reverted", "error_revert_failed"):
        return "unknown", 0, {"dispatch_reason": "stale or failed applied decision"}
    return row[1], int(row[2]), {
        "dispatch_at_utc": row[0],
        "dispatch_age_s": round(age, 1),
        "dispatch_outcome": row[3],
    }


def inspect(live_path: Path = LIVE, evcc_path: Path = EVCC,
            controls_path: Path = CONTROLS, gate_path: Path = GATE,
            *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    result = {
        "timestamp": now.isoformat(),
        "mode": "read_only_shadow_observer",
        "device_writes": 0,
        "phase_fuse_protection_verified": False,
        "physical_control_authorized": False,
    }
    try:
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
        if not isinstance(gate, dict) or gate.get("mode") != "shadow":
            result.update(state="disabled", reason="root gate is not shadow")
            return result
        live = json.loads(live_path.read_text(encoding="utf-8"))
        evcc = json.loads(evcc_path.read_text(encoding="utf-8"))
        if not isinstance(live, dict) or not isinstance(evcc, dict):
            raise ValueError("invalid live snapshot")
        try:
            action, power, details = last_applied(controls_path, now=now)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            action, power, details = "unknown", 0, {
                "dispatch_reason": "control history unavailable"}
        result.update(details)
        result["observed_action"] = action
        result["observed_target_w"] = power if action != "unknown" else None
        result["advice"] = assess(action, power, live, evcc, now=now).as_dict()
        result["state"] = result["advice"]["state"]
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        result.update(state="unavailable", reason=type(exc).__name__)
    return result


def publish(destination: Path, data: dict) -> None:
    if destination.is_symlink() or destination.parent.is_symlink():
        raise ValueError("symlink destination not allowed")
    fd, temporary = tempfile.mkstemp(prefix=".ev-grid-solar-", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", type=Path, default=LIVE)
    parser.add_argument("--evcc", type=Path, default=EVCC)
    parser.add_argument("--controls", type=Path, default=CONTROLS)
    parser.add_argument("--gate", type=Path, default=GATE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    result = inspect(args.live, args.evcc, args.controls, args.gate)
    publish(args.output, result)
    print(json.dumps({"state": result["state"], "device_writes": 0}))


if __name__ == "__main__":
    main()
