#!/usr/bin/env python3
"""Independent root supervisor for the process-local 25% Sunwoda reserve.

Contains NO inverter, charger or network writers. Hash-checked rollback
removes only its owned 48-prefixed drop-in and restarts original dispatch.
The 15-second CT stop envelopes are advisory and not fuse protection.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

import temporary_soc25_watchdog as common
from soc25_reserve_dispatch import REQUIRED_ENV
from soc25_reserve_policy import RECOVERY_EXIT_PCT, verified_evcc_safe

ID = "soc25-reserve-20261101"
STATE = Path("/var/lib/solinteg-soc25-reserve/status.json")
DROPIN = Path("/etc/systemd/system/solinteg-dispatch.service.d/48-soc25-reserve.conf")
ROOT_LIB = Path("/usr/local/libexec/solinteg-soc25-reserve")
WRAPPER = ROOT_LIB / "soc25_reserve_dispatch.py"
POLICY = ROOT_LIB / "soc25_reserve_policy.py"
TIMER = "solinteg-soc25-reserve-watchdog.timer"
DEADLINE_TIMER = "solinteg-soc25-reserve-deadline.timer"
MARKER = "# solinteg-soc25-reserve-20261101 (do not edit)"
PHASE_STOP_W = 3200
SITE_STOP_W = 6500
OBSERVATION_MAX_S = 65
START_SETTLE_S = 120
NONRESPONSE_MAX_S = 180


@dataclass(frozen=True)
class Decision:
    state: str  # safe|reserve_nonresponse|abort
    reason: str
    measurements: dict


def configure_common() -> None:
    common.STATE = STATE
    common.DROPIN = DROPIN
    common.TIMER = TIMER
    common.PILOT_MARKER = MARKER
    common.OVERRIDE_ENV = REQUIRED_ENV


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _age_ok(data: dict, now: datetime) -> bool:
    age = (now - common._stamp(data["timestamp"])).total_seconds()
    return -5 <= age <= OBSERVATION_MAX_S


def evaluate(live: dict, evcc: dict, *, now: datetime, started: datetime,
             service_ok: bool, process_ok: bool) -> Decision:
    if not isinstance(live, dict) or not isinstance(evcc, dict):
        return Decision("abort", "missing live or charger telemetry", {})
    if any(x.tzinfo is None for x in (now, started)):
        return Decision("abort", "timestamp without timezone", {})
    values = {}
    try:
        if not _age_ok(live, now) or not _age_ok(evcc, now):
            raise ValueError("stale inverter or EVCC telemetry")
        soc = common._finite(live["soc_pct"])
        battery_w = common._finite(live["battery_w"])
        pv = common._finite(live["pv_w"])
        house = common._finite(live["house_load_w"])
        ac = common._finite(live["inverter_ac_w"])
        grid = common._finite(live["grid_w"])
        phases = [common._finite(live[f"meter_l{n}_w"]) for n in "123"]
        values.update(soc_pct=soc, battery_w=round(battery_w),
                      pv_w=round(pv), house_load_w=round(house),
                      grid_import_w=round(max(0, -grid)),
                      phase_import_w=[round(max(0, -x)) for x in phases],
                      solar_surplus_w=round(max(0, pv-house)))
        if not 23 <= soc <= 100:
            raise ValueError("house SoC outside 23-100% deployment protection band")
        if not 5 <= common._finite(live["battery_temp_c"]) <= 45:
            raise ValueError("battery temperature outside 5-45 C")
        if (type(live.get("grid_unbalanced_output_raw")) is not int
                or live["grid_unbalanced_output_raw"] != 0):
            raise ValueError("inverter balanced output register 50006 invalid")
        if (pv < 0 or house < 0
                or abs(sum(phases) - grid) > max(100, abs(grid) * .05)
                or abs(ac-grid-house) > max(150, house * .1)
                or abs(ac-pv-battery_w) > 500 + .1*max(abs(ac), pv, abs(battery_w))):
            raise ValueError("CT/house/PV/battery energy balance invalid")
        if any(-x >= PHASE_STOP_W for x in phases) or -grid >= SITE_STOP_W:
            raise ValueError("conservative site/phase import stop envelope exceeded")
        evcc_mode = verified_evcc_safe(evcc)
        values["evcc_no_charge_mode"] = evcc_mode
        if evcc_mode is None:
            raise ValueError("EVCC neither unplugged A1 nor independently paused B2")
        elapsed = (now-started).total_seconds()
        if elapsed < -5:
            raise ValueError("clock earlier than deployment")
        if elapsed >= START_SETTLE_S:
            if not service_ok or not process_ok:
                raise ValueError("dispatcher or reserve process not verified")
        surplus = max(0, pv-house)
        max_expected_charge = max(1900, min(5500, surplus) + 500)
        if soc < RECOVERY_EXIT_PCT and battery_w < -max_expected_charge:
            raise ValueError("reserve recovery charging faster than supervised grid/PV envelope")
        if soc <= 26 and elapsed >= START_SETTLE_S and live.get("work_mode_raw") != 0x303:
            # The poller can report 26% just after dispatch handled a 27%
            # sample; the next 30s iteration has not reached charge mode.
            # The existing 180s nonresponse timer gives it time to react,
            # but never overrides immediate CT/thermal/EV/overcharge stops.
            return Decision("reserve_nonresponse",
                            "low SoC: awaiting EMS recovery mode", values)
        if soc <= 26 and elapsed >= START_SETTLE_S:
            if battery_w > -70:
                return Decision("reserve_nonresponse",
                                "house reserve at/below 26% without physical charging", values)
            if surplus >= 400 and grid >= 150 and battery_w > -350:
                return Decision("reserve_nonresponse",
                                "PV surplus apparently exported instead of battery recovery", values)
        return Decision("safe", "reserve charge/solar priority under independent supervision", values)
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, OverflowError) as exc:
        return Decision("abort", str(exc)[:160], values)


def safety_services_ok() -> bool:
    return all(common._service("is-active", unit) == "active" for unit in (
        "solinteg-poller.service", "solinteg-watchdog.timer",
        "solinteg-ev-grid-priority-watchdog.timer", "solinteg-ev-observer.service",
        DEADLINE_TIMER))


def wrapper_process_ok() -> bool:
    try:
        pid = int(common._service("show", common.SERVICE, "--property=MainPID", "--value"))
        if pid <= 0:
            return False
        args = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
        return str(WRAPPER).encode("ascii") in args
    except (OSError, ValueError):
        return False


def _disable_deadline_timer() -> None:
    # The second timer is independent of the 15-second watchdog. It must
    # not be left enabled after completing or aborting this one-off trial.
    subprocess.run(
        ["/usr/bin/systemctl", "disable", "--now", "--no-block", DEADLINE_TIMER],
        capture_output=True, check=False, timeout=20)


def _restore_owned(status: dict, *, reason: str, completed: bool = False) -> dict:
    result = common._restore(status, reason=reason, completed=completed)
    _disable_deadline_timer()
    return result


def tick(*, stop: bool = False) -> dict:
    configure_common()
    if not STATE.exists():
        if DROPIN.exists() or DROPIN.is_symlink():
            raise RuntimeError("reserve drop-in exists without root-owned state")
        return {"state": "not_installed"}
    status = common._load(STATE)
    if status.get("schema_version") != 1 or status.get("pilot_id") != ID:
        raise RuntimeError("unexpected protected reserve state")
    if status.get("state") in ("aborted", "completed") and not DROPIN.exists():
        return {"state": status["state"], "reason": status.get("reason")}
    if status.get("state") in ("restore_blocked", "restoring"):
        return _restore_owned(status,
                               reason=status.get("restore_reason", "interrupted rollback"),
                               completed=False)
    if status.get("state") not in ("prepared", "running"):
        raise RuntimeError("invalid reserve lifecycle")
    if stop:
        return _restore_owned(status, reason="operator requested reserve rollback",
                               completed=False)
    now = datetime.now(timezone.utc)
    try:
        if now >= common._stamp(status["expires_at_utc"]):
            return _restore_owned(status, reason="six-hour reserve trial deadline reached",
                                   completed=False)
        if status.get("boot_id") != _boot_id():
            return _restore_owned(status, reason="reserve cannot cross reboot without reapproval",
                                   completed=False)
        if DROPIN.is_symlink() or not DROPIN.exists() or common._checksum(DROPIN) != status.get("dropin_sha256"):
            return _restore_owned(status, reason="owned reserve drop-in changed", completed=False)
        if common.GATE.exists() or common.GATE.is_symlink():
            return _restore_owned(status, reason="EV grid-priority root gate unexpectedly enabled",
                                   completed=False)
        if (common._checksum(common.DISPATCH) != status.get("dispatcher_sha256")
                or common._checksum(WRAPPER) != status.get("wrapper_sha256")
                or common._checksum(POLICY) != status.get("policy_sha256")):
            return _restore_owned(status, reason="reserve process or original dispatcher changed",
                                   completed=False)
        if not safety_services_ok():
            return _restore_owned(status, reason="independent Solinteg safety services inactive",
                                   completed=False)
        live, evcc = common._load(common.LIVE), common._load(common.EVCC)
        result = evaluate(live, evcc, now=now,
                          started=common._stamp(status["started_at_utc"]),
                          service_ok=common._dispatch_running(),
                          process_ok=wrapper_process_ok() and common._process_overrides_present())
    except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return _restore_owned(status, reason="reserve observation unavailable: " + type(exc).__name__,
                               completed=False)
    if result.state == "abort":
        return _restore_owned(status, reason=result.reason, completed=False)
    if result.state == "reserve_nonresponse":
        first = status.get("nonresponse_since_utc")
        if first is None:
            status["nonresponse_since_utc"] = now.isoformat()
        elif (now - common._stamp(first)).total_seconds() > NONRESPONSE_MAX_S:
            return _restore_owned(status, reason=result.reason + " for over 180 seconds",
                                   completed=False)
    else:
        status.pop("nonresponse_since_utc", None)
    status.update(state="running", checked_at_utc=now.isoformat(),
                  measurements=result.measurements, check_reason=result.reason,
                  physical_control_authorized=True)
    common._atomic_json(STATE, status)
    return {"state": "running", "measurements": result.measurements,
            "check_reason": result.reason}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    opts = p.add_mutually_exclusive_group(required=True)
    opts.add_argument("--tick", action="store_true")
    opts.add_argument("--stop", action="store_true")
    opts.add_argument("--status", action="store_true")
    a = p.parse_args()
    if a.status:
        x = common._load(STATE) if STATE.exists() else {"state": "not_installed"}
        print(json.dumps({k: x.get(k) for k in (
            "state", "started_at_utc", "expires_at_utc", "checked_at_utc", "check_reason",
            "measurements", "reason", "restored_at_utc")}, sort_keys=True))
    else:
        print(json.dumps(tick(stop=a.stop), sort_keys=True))


if __name__ == "__main__":
    main()
