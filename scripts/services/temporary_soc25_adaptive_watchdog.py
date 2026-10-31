#!/usr/bin/env python3
"""One-off 1.5 kW recovery safety monitor and verified rollback (no Modbus).

Reuses the already-tested original one-off safety checks and restore
routine in a distinct root-owned process and state namespace. The
previous aborted 1000 W pilot is neither deleted nor reactivated.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import temporary_soc25_watchdog as common

PILOT_ID = "soc25-adaptive-20261031"
STATE = Path("/var/lib/solinteg-recovery-25-adaptive-pilot/status.json")
DROPIN = Path("/etc/systemd/system/solinteg-dispatch.service.d/46-temp-soc25-adaptive-pilot.conf")
WRAPPER = Path("/usr/local/libexec/solinteg-recovery-25-pilot/temporary_soc25_adaptive_dispatch.py")
TIMER = "solinteg-temp-soc25-adaptive-watchdog.timer"
MARKER = "# solinteg-temporary-soc25-adaptive-pilot (do not edit)"
OVERRIDE_ENV = {
    "SOLINTEG_TEMP_SOC25_ADAPTIVE": "1",
    "DISPATCH_SOC_RECOVERY_EXIT_PCT": "25",
    "DISPATCH_SOC_FLOOR_RECOVERY_W": "1500",
    "SOLINTEG_BATTERY_MAX_W": "1500",
    "DISPATCH_L3_CHARGE_GUARD_MODE": "1",
    "DISPATCH_L3_CHARGE_BUDGET_W": "3000",
    "DISPATCH_LOOP_INTERVAL_S": "30",
}


def configure_common() -> None:
    # The standalone watchdog process has its own module instance. This
    # never alters the original inactive 1000 W watchdog's on-disk file.
    common.STATE = STATE
    common.DROPIN = DROPIN
    common.TIMER = TIMER
    common.PILOT_MARKER = MARKER
    common.OVERRIDE_ENV = OVERRIDE_ENV
    common.MAX_CHARGE_MEASURED_W = 1850
    # Keep the *same* 3200 W/phase, 6500 W total, SoC and CT guards.
    common.MAX_PHASE_GRID_IMPORT_W = 3200
    common.MAX_SITE_IMPORT_W = 6500


def original_safety_watchdogs_ok(now: datetime) -> bool:
    """Do not continue if either independent pre-existing safety check stops."""
    try:
        for unit in ("solinteg-watchdog.timer",
                     "solinteg-ev-grid-priority-watchdog.timer"):
            if not common._service("is-active", unit) == "active":
                return False
        state = common._load(Path("/var/lib/solinteg-ev-grid-priority/status.json"))
        age = (now - common._stamp(state["at_utc"])).total_seconds()
        return state.get("state") == "shadow_monitor_ok" and -5 <= age <= 65
    except (OSError, ValueError, KeyError, TypeError):
        return False


def wrapper_process_ok() -> bool:
    try:
        pid = int(common._service(
            "show", common.SERVICE, "--property=MainPID", "--value"))
        if pid <= 0:
            return False
        arguments = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
        return str(WRAPPER).encode("ascii") in arguments
    except (ValueError, OSError):
        return False


def tick(*, force_stop: bool = False) -> dict:
    configure_common()
    if not STATE.exists():
        if DROPIN.exists() and DROPIN.read_bytes().startswith(MARKER.encode("ascii")):
            raise RuntimeError("temporary drop-in without status: operator intervention required")
        return {"state": "not_installed"}
    status = common._load(STATE)
    if status.get("schema_version") != 1 or status.get("pilot_id") != PILOT_ID:
        raise ValueError("invalid adaptive pilot state identity")
    if status.get("state") in ("completed", "aborted") and not DROPIN.exists():
        return {"state": status["state"], "reason": status.get("reason")}
    if status.get("state") in ("restoring", "restore_blocked"):
        return common._restore(status, reason=status.get(
            "restore_reason", "interrupted restore"), completed=False)
    if status.get("state") not in ("prepared", "running"):
        raise ValueError("unexpected lifecycle state")
    if force_stop:
        return common._restore(status, reason="operator requested stop", completed=False)
    now = datetime.now(timezone.utc)
    if now >= common._stamp(status["expires_at_utc"]):
        return common._restore(status, reason="two-hour pilot deadline reached",
                               completed=False)
    if not DROPIN.exists() or common._checksum(DROPIN) != status.get("dropin_sha256"):
        return common._restore(status, reason="temporary dispatcher drop-in changed",
                               completed=False)
    if not original_safety_watchdogs_ok(now):
        return common._restore(status, reason="original independent safety watchdog unhealthy",
                               completed=False)
    try:
        gate = common._load(common.GATE)
        if (gate.get("mode") != "shadow" or gate.get("schema_version") != 2
                or common._checksum(common.DISPATCH) != gate.get("dispatcher_sha256")
                or common._checksum(common.DISPATCH) != status.get("dispatcher_sha256")):
            return common._restore(status, reason="root EV gate or ARMED dispatcher changed",
                                   completed=False)
        if common._checksum(WRAPPER) != status.get("adaptive_wrapper_sha256"):
            return common._restore(status, reason="adaptive launcher changed",
                                   completed=False)
        live = common._load(common.LIVE)
        evcc = common._load(common.EVCC)
        process_ok = (common._process_overrides_present() and wrapper_process_ok())
        decision = common.evaluate(
            live, evcc, now=now, started=common._stamp(status["started_at_utc"]),
            expires=common._stamp(status["expires_at_utc"]),
            service_ok=common._dispatch_running(), process_overrides_ok=process_ok)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return common._restore(status, reason="watchdog or root safety input unavailable",
                               completed=False)
    if decision.action != "continue":
        reason = decision.reason.replace("1250 W", "1850 W").replace(
            "1000 W clamp", "1500 W adaptive clamp")
        return common._restore(status, reason=reason,
                               completed=decision.action == "complete")
    metrics = decision.metrics
    metrics["adaptive_wrapper_verified"] = process_ok
    status.update(state="running", checked_at_utc=now.isoformat(),
                  check_reason="within 1500 W adaptive recovery envelope",
                  measurements=metrics, physical_control_authorized=True)
    common._atomic_json(STATE, status)
    return {"state": "running", "measurements": metrics}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    flags = parser.add_mutually_exclusive_group(required=True)
    flags.add_argument("--tick", action="store_true")
    flags.add_argument("--stop", action="store_true")
    flags.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.status:
        data = common._load(STATE) if STATE.exists() else {"state": "not_installed"}
        print(json.dumps({key: data.get(key) for key in (
            "state", "started_at_utc", "expires_at_utc", "checked_at_utc",
            "measurements", "reason", "restored_at_utc")}, sort_keys=True))
    else:
        print(json.dumps(tick(force_stop=args.stop), sort_keys=True))


if __name__ == "__main__":
    main()
