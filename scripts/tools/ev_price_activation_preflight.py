#!/usr/bin/env python3
"""Read-only EVCC price-control activation preflight. Never issues device commands.

This report is evidence, not permission to arm the price controller. In
particular, estimated EVCC AC energy and polled Volvo charging status can
never substitute for a calibrated session or lossless manual-stop events.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import ev_price_pause_owner as ownership  # noqa: E402
import ev_volvo_smart_charging as smart  # noqa: E402
from ev_price_policy import (  # noqa: E402
    decide_grid_price, read_ev_session, read_house_price_plan,
)

ROOT = Path("/opt/solinteg/ev-shadow-runtime/var")
MAX_INPUT_BYTES = 65536
MAX_SAMPLE_AGE_SECONDS = 65


def _json_file(path: Path) -> dict | None:
    try:
        if path.stat().st_size > MAX_INPUT_BYTES:
            return None
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _fresh(value: object, *, now: datetime, max_seconds: int) -> bool:
    if not isinstance(value, str):
        return False
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.tzinfo is not None and 0 <= (now - stamp).total_seconds() <= max_seconds
    except (ValueError, TypeError, OverflowError):
        return False


def _evcc_usable(row: dict | None, now: datetime) -> bool:
    if (not isinstance(row, dict) or row.get("source") != "waveshare_evcc_fc03_readonly"
            or row.get("read_ok") is not True or row.get("writes") != 0
            or row.get("physical_control_eligible") is not False
            or row.get("state") not in ("B2", "C2")
            or not _fresh(row.get("timestamp"), now=now, max_seconds=MAX_SAMPLE_AGE_SECONDS)):
        return False
    config = row.get("configured_pwm_raw")
    pilot = row.get("pilot_pwm_raw")
    if type(config) is not int or config != pilot or config not in (100, 1000):
        return False
    currents = row.get("channel_currents_a")
    if (not isinstance(currents, list) or len(currents) != 3
            or any(type(x) not in (float, int) or not math.isfinite(x) for x in currents)
            or not 0 <= currents[0] <= 0.3 or not 0 <= currents[2] <= 0.3
            or not 0 <= currents[1] <= 6.5):
        return False
    return ((config == 100 and row["state"] == "C2" and currents[1] >= 5.5)
            or (config == 1000 and row["state"] == "B2" and currents[1] <= 0.5))


def _grid_usable(row: dict | None, now: datetime) -> bool:
    return bool(
        isinstance(row, dict)
        and row.get("grid_unbalanced_output_raw") == 0
        and _fresh(row.get("timestamp"), now=now, max_seconds=MAX_SAMPLE_AGE_SECONDS)
        and all(type(row.get(k)) in (float, int) and math.isfinite(row[k])
                for k in ("meter_l3_w", "grid_w"))
    )


def inspect(*, now: datetime, root: Path = ROOT, vin_hash: str | None = None,
            price_db: Path = Path("/opt/solinteg/telemetry.db"),
            grid_path: Path = Path("/opt/solinteg/live.json")) -> dict:
    if now.tzinfo is None:
        raise ValueError("timezone-aware now required")
    session = read_ev_session(str(root / "strict-session.json"), now=now)
    smart_state, smart_reason = smart.read_smart_charging(
        str(root / "volvo-smart-charging.json"), now=now,
        expected_vin_sha256=vin_hash)
    try:
        manual = ownership.read_manual_intent(
            "/run/solinteg/ev-manual-intent.json", now=now)
    except ownership.OwnershipError:
        manual = None
    manual_ok = bool(
        session and manual and manual["manual_paused"] is False
        and manual["session_id"] == session["session_id"]
        and manual["connection_epoch"] == session["connection_epoch"]
    )
    evcc = _json_file(root / "evcc-readonly.json")
    grid = _json_file(grid_path)
    price = (decide_grid_price(read_house_price_plan(str(price_db)),
                               now=now, session=session, max_current_a=6)
             if session is not None else None)
    checks = {
        "volvo_smart_charging_explicitly_off": smart_state == "off",
        "independently_calibrated_fresh_ev_session": session is not None,
        "lossless_manual_stop_source_verified": manual_ok,
        "fresh_consistent_evcc_fc03": _evcc_usable(evcc, now),
        "fresh_valid_grid_and_l3": _grid_usable(grid, now),
        "current_price_plan_verified": bool(price and price.buy_ore is not None),
        # Must be closed by separately controlled physical qualification.
        "physical_evcc_pause_resume_and_fault_tests": False,
    }
    ha_status = _json_file(root / "status.json")
    planning = {"quality": "unverified_display_only"}
    if (ha_status and ha_status.get("energy_estimate_unverified") is True
            and _fresh(ha_status.get("checked_at_utc"), now=now, max_seconds=300)):
        remaining = ha_status.get("remaining_ac_kwh")
        if type(remaining) in (int, float) and math.isfinite(remaining) and 0 <= remaining <= 100:
            planning["unverified_remaining_ac_kwh"] = remaining
            planning["unverified_hours_at_6a_230v"] = round(remaining * 1000 / 1380, 2)
            if ha_status.get("charge_mode") == "deadline":
                clock = ha_status.get("departure_local_time")
                if isinstance(clock, str):
                    try:
                        from ha_ev_session import next_departure
                        dep = next_departure(clock, now=now)
                        planning["hours_until_departure"] = round(
                            (dep - now).total_seconds() / 3600, 2)
                        planning["indicative_deadline_reachable_at_6a"] = (
                            remaining * 1000 / 1380 < (dep - now).total_seconds() / 3600)
                    except (ValueError, TypeError):
                        pass
    return {
        "state": "ready_for_qualification" if all(checks.values()) else "not_ready",
        "checks": checks,
        "volvo_smart_charging": smart_state,
        "volvo_gate_reason": smart_reason,
        "evcc_observed_state": evcc.get("state") if evcc else None,
        "price_mode": session["charge_mode"] if session else None,
        "planning_advisory": planning,
        "device_writes": 0,
        "arm_command_issued": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--price-db", type=Path, default=Path("/opt/solinteg/telemetry.db"))
    parser.add_argument("--grid", type=Path, default=Path("/opt/solinteg/live.json"))
    args = parser.parse_args()
    result = inspect(now=datetime.now(timezone.utc), root=args.root,
                     vin_hash=os.environ.get("EV_VOLVO_TARGET_VIN_SHA256"),
                     price_db=args.price_db, grid_path=args.grid)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))


if __name__ == "__main__":
    main()
