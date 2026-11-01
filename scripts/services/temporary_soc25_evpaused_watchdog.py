#!/usr/bin/env python3
"""Independent EV-paused 25% recovery watchdog with owned systemd rollback.

No Modbus, EVCC write, or network API. This component is not deployed by
merely checking it into Git: an operator must first pass the read-only
preflight and separately install an exact root-owned one-off service/timer.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import temporary_soc25_watchdog as common
from temporary_soc25_evpaused_dispatch import REQUIRED_ENV

PILOT_ID = "soc25-evpaused-20261101"
STATE = Path("/var/lib/solinteg-recovery-25-evpaused/status.json")
DROPIN = Path("/etc/systemd/system/solinteg-dispatch.service.d/47-temp-soc25-evpaused-pilot.conf")
WRAPPER = Path("/usr/local/libexec/solinteg-recovery-25-pilot/temporary_soc25_evpaused_dispatch.py")
TIMER = "solinteg-temp-soc25-evpaused-watchdog.timer"
MARKER = "# solinteg-temporary-soc25-evpaused-pilot (do not edit)"
MIN_SOC_PCT = 19.0
TARGET_SOC_PCT = 26.0  # 1% buffer above the user's 25% requirement.
MAX_PHASE_IMPORT_W = 3200
MAX_GRID_IMPORT_W = 6500
MAX_PHYSICAL_CHARGE_W = 1850
MAX_SNAPSHOT_AGE_S = 65
MAX_STARTUP_SETTLE_S = 120


@dataclass(frozen=True)
class Decision:
    action: str  # continue|complete|abort
    reason: str
    metrics: dict


def configure_common() -> None:
    # Reuse the tested checksum-restricted restoration routine, but never
    # modify the old 45-/46-pilot state files, drop-ins or watchdog timers.
    common.STATE = STATE
    common.DROPIN = DROPIN
    common.TIMER = TIMER
    common.PILOT_MARKER = MARKER
    common.OVERRIDE_ENV = REQUIRED_ENV


def _fresh(record: dict, now: datetime) -> bool:
    return -5 <= (now - common._stamp(record["timestamp"])).total_seconds() <= MAX_SNAPSHOT_AGE_S


def evaluate(live: dict, evcc: dict, *, now: datetime, started: datetime,
             expires: datetime, service_ok: bool, process_ok: bool) -> Decision:
    if any(x.tzinfo is None for x in (now, started, expires)):
        return Decision("abort", "timezone required", {})
    if now >= expires or now < started:
        return Decision("abort", "pilot deadline or invalid clock", {})
    if not isinstance(live, dict) or not isinstance(evcc, dict):
        return Decision("abort", "inverter/EVCC observation unavailable", {})
    metrics = {}
    try:
        if not (_fresh(live, now) and _fresh(evcc, now)):
            raise ValueError("stale inverter or EVCC snapshot")
        soc = common._finite(live["soc_pct"])
        battery = common._finite(live["battery_w"])
        grid = common._finite(live["grid_w"])
        pv = common._finite(live["pv_w"])
        house = common._finite(live["house_load_w"])
        ac = common._finite(live["inverter_ac_w"])
        phase = [common._finite(live[f"meter_l{i}_w"]) for i in (1, 2, 3)]
        metrics.update(soc_pct=soc, battery_w=round(battery),
                       grid_import_w=round(max(0, -grid)),
                       phase_import_w=[round(max(0, -x)) for x in phase])
        if not MIN_SOC_PCT <= soc <= 100:
            return Decision("abort", "house SoC below 19% safety threshold", metrics)
        if not 5 <= common._finite(live["battery_temp_c"]) <= 45:
            raise ValueError("battery temperature outside 5-45 C")
        if (type(live.get("grid_unbalanced_output_raw")) is not int
                or live["grid_unbalanced_output_raw"] != 0):
            raise ValueError("50006 inverter phase balance unavailable")
        if (pv < 0 or house < 0
                or abs(sum(phase) - grid) > max(100, abs(grid) * .05)
                or abs(ac - grid - house) > max(150, house * .10)
                or abs(ac - pv - battery) > 500 + .10 * max(abs(ac), pv, abs(battery))):
            raise ValueError("inconsistent CT/PV/AC/battery readings")
        if any(-x > MAX_PHASE_IMPORT_W for x in phase):
            return Decision("abort", "conservative 3200W phase STOP exceeded", metrics)
        if -grid > MAX_GRID_IMPORT_W:
            return Decision("abort", "conservative 6500W total import STOP exceeded", metrics)
        if (evcc.get("source") != "waveshare_evcc_fc03_readonly"
                or evcc.get("read_ok") is not True or evcc.get("state") != "B2"
                or evcc.get("configured_pwm_raw") != 1000
                or evcc.get("paused_by_pwm") is not True
                or evcc.get("charging_detected") is not False
                or evcc.get("writes") != 0
                or not 0 <= common._finite(evcc["measured_current_a"]) <= 1):
            raise ValueError("EVCC pause no longer independently confirmed")
        elapsed = (now - started).total_seconds()
        if battery < -3000:
            return Decision("abort", "pre-existing charge exceeds 3000W startup safety", metrics)
        if battery < -MAX_PHYSICAL_CHARGE_W and elapsed >= MAX_STARTUP_SETTLE_S:
            return Decision("abort", "house battery charge still exceeds 1850W after startup", metrics)
        if elapsed >= MAX_STARTUP_SETTLE_S:
            if not service_ok or not process_ok:
                raise ValueError("pilot dispatcher/overrides not active")
            if live.get("work_mode_raw") != 0x303:
                raise ValueError("inverter not in EMS BattCtrl mode")
            if battery > -100:
                raise ValueError("house battery not physically charging")
        # Completion must occur *before* the dispatcher reaches its 27%
        # configured exit; do not allow the pilot to pass through Auto.
        if soc >= TARGET_SOC_PCT:
            return Decision("complete", "house SoC reached 26% buffered target", metrics)
        return Decision("continue", "within EV-paused SoC25 trial envelope", metrics)
    except (OSError, ValueError, KeyError, TypeError, IndexError, OverflowError) as exc:
        return Decision("abort", str(exc)[:160], metrics)


def safety_services_ok() -> bool:
    # Root gate was removed by an earlier rollback. Do not silently recreate
    # the old gate as part of this independent EV-paused experiment.
    return all(common._service("is-active", unit) == "active" for unit in (
        "solinteg-watchdog.timer", "solinteg-ev-grid-priority-watchdog.timer",
        "solinteg-ev-observer.service", "solinteg-poller.service"))


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def wrapper_process_ok() -> bool:
    try:
        pid = int(common._service("show", common.SERVICE, "--property=MainPID", "--value"))
        if pid <= 0:
            return False
        cmdline = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
        return str(WRAPPER).encode("ascii") in cmdline
    except (OSError, ValueError):
        return False


def tick(*, force_stop: bool = False) -> dict:
    configure_common()
    if not STATE.exists():
        if DROPIN.exists():
            raise RuntimeError("owned temporary drop-in without root state: intervene manually")
        return {"state": "not_installed"}
    status = common._load(STATE)
    if status.get("schema_version") != 1 or status.get("pilot_id") != PILOT_ID:
        raise ValueError("unexpected one-off pilot identity")
    if status.get("state") in ("completed", "aborted") and not DROPIN.exists():
        return {"state": status["state"], "reason": status.get("reason")}
    if status.get("state") in ("restoring", "restore_blocked"):
        return common._restore(
            status, reason=status.get("restore_reason", "interrupted rollback"),
            completed=False)
    if status.get("state") not in ("prepared", "running"):
        raise ValueError("unexpected one-off pilot lifecycle")
    if force_stop:
        return common._restore(status, reason="operator requested stop", completed=False)
    now = datetime.now(timezone.utc)
    try:
        boot_changed = not status.get("boot_id") or status["boot_id"] != _boot_id()
        expired = now >= common._stamp(status["expires_at_utc"])
    except (OSError, KeyError, ValueError, TypeError):
        return common._restore(status, reason="pilot boot/deadline evidence invalid",
                               completed=False)
    if boot_changed:
        return common._restore(status, reason="pilot must not continue after a reboot",
                               completed=False)
    if expired:
        return common._restore(status, reason="two-hour pilot deadline reached",
                               completed=False)
    if not DROPIN.exists() or common._checksum(DROPIN) != status.get("dropin_sha256"):
        return common._restore(status, reason="pilot systemd drop-in changed", completed=False)
    try:
        if common.GATE.exists():
            return common._restore(status, reason="root EV priority gate unexpectedly enabled",
                                   completed=False)
        if (common._checksum(common.DISPATCH) != status.get("dispatcher_sha256")
                or common._checksum(WRAPPER) != status.get("wrapper_sha256")):
            return common._restore(status, reason="original dispatcher or wrapper changed",
                                   completed=False)
        if not safety_services_ok():
            return common._restore(status, reason="independent watchdog or observer unhealthy",
                                   completed=False)
        live, evcc = common._load(common.LIVE), common._load(common.EVCC)
        process_ok = common._process_overrides_present() and wrapper_process_ok()
        result = evaluate(live, evcc, now=now,
                          started=common._stamp(status["started_at_utc"]),
                          expires=common._stamp(status["expires_at_utc"]),
                          service_ok=common._dispatch_running(), process_ok=process_ok)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return common._restore(status, reason="root supervision input unavailable",
                               completed=False)
    if result.action != "continue":
        return common._restore(
            status, reason=result.reason, completed=result.action == "complete")
    status.update(state="running", checked_at_utc=now.isoformat(),
                  measurements=result.metrics, physical_control_authorized=True)
    common._atomic_json(STATE, status)
    return {"state": "running", "measurements": result.metrics}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    args = parser.add_mutually_exclusive_group(required=True)
    args.add_argument("--tick", action="store_true")
    args.add_argument("--stop", action="store_true")
    args.add_argument("--status", action="store_true")
    flags = parser.parse_args()
    if flags.status:
        value = common._load(STATE) if STATE.exists() else {"state": "not_installed"}
        print(json.dumps({key: value.get(key) for key in (
            "state", "started_at_utc", "expires_at_utc", "checked_at_utc",
            "measurements", "reason", "restored_at_utc")}, sort_keys=True))
    else:
        print(json.dumps(tick(force_stop=flags.stop), sort_keys=True))


if __name__ == "__main__":
    main()
