#!/usr/bin/env python3
"""Publish a sanitized Volvo read-only snapshot to the inactive EV price gate.

The authenticated private probe runs separately. This tool never authenticates,
contacts a vehicle, or writes to EVCC, Modbus or the inverter.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
from ev_volvo_smart_charging import read_smart_charging  # noqa: E402

DESTINATION = Path("/opt/solinteg/ev-shadow-runtime/var/volvo-smart-charging.json")
MAX_BYTES = 65536


def publish(source: Path, destination: Path, *, expected_vin_sha256: str,
            now: datetime) -> dict:
    """Write only allowlisted fields, atomically, with the destination owner's 0600."""
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        meta = os.fstat(fd)
        if (not stat.S_ISREG(meta.st_mode) or meta.st_mode & 0o077
                or meta.st_size > MAX_BYTES):
            raise ValueError("insecure or oversized Volvo source")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("oversized Volvo source")
    finally:
        os.close(fd)

    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("invalid Volvo source")
    chronos = data.get("chronos")
    if not isinstance(chronos, dict):
        raise ValueError("incomplete Volvo Chronos source")
    locations = chronos.get("locations")
    timer = chronos.get("timer")
    if not isinstance(locations, dict) or not isinstance(timer, dict):
        raise ValueError("incomplete Volvo Chronos source")
    if (any(key not in data for key in
            ("captured_utc", "method", "set_amp_limit_calls", "target_vin_sha256"))
            or any(key not in chronos for key in
                   ("current_amp_limit", "pending_amp_limit"))
            or any(key not in locations for key in
                   ("locations", "pending_locations"))
            or any(key not in timer for key in
                   ("global_timer", "pending_global_timer"))):
        raise ValueError("incomplete Volvo Chronos snapshot")
    records = locations.get("locations")
    if not isinstance(records, list):
        raise ValueError("missing Volvo charge locations")

    def active_entries(values: object) -> list[dict] | None:
        if not isinstance(values, list):
            return None
        return [{"active": item.get("active")} if isinstance(item, dict) else {}
                for item in values[:100]]

    global_timer = timer.get("global_timer")
    slim = {
        "captured_utc": data.get("captured_utc"),
        "method": data.get("method"),
        "set_amp_limit_calls": data.get("set_amp_limit_calls"),
        "target_vin_sha256": data.get("target_vin_sha256"),
        "chronos": {
            "locations": {
                "locations": [{
                    "optimized_charging_enabled": record.get("optimized_charging_enabled"),
                    "timers": active_entries(record.get("timers")),
                    "departure_times": active_entries(record.get("departure_times")),
                } if isinstance(record, dict) else {} for record in records[:5]],
                "pending_locations": locations.get("pending_locations"),
            },
            "timer": {
                "global_timer": ({"active": global_timer.get("active")}
                                 if isinstance(global_timer, dict) else
                                 None if global_timer is None else {}),
                "pending_global_timer": (None if timer.get("pending_global_timer") is None
                                         else {"pending": True}),
            },
            "current_amp_limit": chronos.get("current_amp_limit"),
            "pending_amp_limit": chronos.get("pending_amp_limit"),
        },
    }

    if not destination.parent.is_dir() or destination.is_symlink():
        raise ValueError("invalid Volvo runtime destination")
    parent = destination.parent.stat()
    temp_fd, temp_name = tempfile.mkstemp(prefix=".volvo-smart-", dir=destination.parent)
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as out:
            os.fchmod(out.fileno(), 0o600)
            os.fchown(out.fileno(), parent.st_uid, parent.st_gid)
            json.dump(slim, out, separators=(",", ":"), ensure_ascii=False)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        state, reason = read_smart_charging(
            temp_name, now=now, expected_vin_sha256=expected_vin_sha256)
        # A trusted source may say "on"; publishing this immediately blocks EVCC.
        if state == "unknown":
            raise ValueError("Volvo snapshot verification failed: " + reason)
        os.replace(temp_name, destination)
        directory = os.open(destination.parent, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return {"state": state, "reason": reason, "published": True, "device_writes": 0}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", type=Path, default=DESTINATION)
    args = parser.parse_args()
    expected = os.environ.get("EV_VOLVO_TARGET_VIN_SHA256", "")
    try:
        result = publish(args.source, args.destination,
                         expected_vin_sha256=expected,
                         now=datetime.now(timezone.utc))
    except (OSError, ValueError, TypeError) as exc:
        result = {"published": False, "device_writes": 0, "reason": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False))
    if result["published"] is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
