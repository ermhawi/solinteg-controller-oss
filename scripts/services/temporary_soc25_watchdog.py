#!/usr/bin/env python3
"""Privileged, bounded, one-off SoC 25% recovery pilot supervisor.

The pilot changes ONLY the dispatch unit's process environment, by a
temporary root-owned systemd drop-in. Normal Solinteg env and the live
dispatcher source are untouched. The supervisor never opens Modbus or
changes EVCC current; it can only remove its exact drop-in and restart
the ordinary dispatch service to restore its original environment.

All thresholds are conservative pilot STOP limits, not certified RMS
fuse protection. The existing root and ordinary dispatch watchdogs
remain independent and enabled throughout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc
STATE = Path("/var/lib/solinteg-recovery-25-pilot/status.json")
DROPIN = Path("/etc/systemd/system/solinteg-dispatch.service.d/45-temp-soc25-pilot.conf")
LIVE = Path("/opt/solinteg/live.json")
EVCC = Path("/opt/solinteg/ev-shadow-runtime/var/evcc-readonly.json")
GATE = Path("/var/lib/solinteg-ev-grid-priority/gate.json")
DISPATCH = Path("/opt/solinteg/app/scripts/services/dispatch_loop.py")
SERVICE = "solinteg-dispatch.service"
TIMER = "solinteg-temp-soc25-watchdog.timer"
PILOT_MARKER = "# solinteg-temporary-soc25-pilot (do not edit)"
MAX_OBSERVATION_AGE_S = 65
MAX_PHASE_GRID_IMPORT_W = 3200
MAX_SITE_IMPORT_W = 6500
MAX_CHARGE_MEASURED_W = 1250
MIN_CHARGE_MEASURED_W = 100
MAX_INITIAL_SETTLE_S = 120
MIN_BATTERY_SOC_PCT = 19.0
TARGET_SOC_PCT = 25.0
OVERRIDE_ENV = {
    "DISPATCH_SOC_RECOVERY_EXIT_PCT": "25",
    "DISPATCH_SOC_FLOOR_RECOVERY_W": "1000",
    "SOLINTEG_BATTERY_MAX_W": "1000",
}


@dataclass(frozen=True)
class Decision:
    action: str  # continue|complete|abort
    reason: str
    metrics: dict


def _finite(value):
    if type(value) not in (float, int) or not math.isfinite(value):
        raise ValueError("invalid or absent numeric reading")
    return float(value)


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("missing time zone")
    return stamp.astimezone(UTC)


def _fresh(data: dict, now: datetime) -> bool:
    age = (now - _stamp(data["timestamp"])).total_seconds()
    return -5 <= age <= MAX_OBSERVATION_AGE_S


def evaluate(live, evcc, *, now: datetime, started: datetime, expires: datetime,
             service_ok: bool, process_overrides_ok: bool) -> Decision:
    """Pure, fail-closed decision on two existing local observations.

    User-approved physical maximum is a *dispatcher-process* 1000 W
    clamp; phase/CT data are an additional emergency STOP envelope.
    """
    if now.tzinfo is None or started.tzinfo is None or expires.tzinfo is None:
        return Decision("abort", "timestamp missing timezone", {})
    now, started, expires = (x.astimezone(UTC) for x in (now, started, expires))
    elapsed = (now - started).total_seconds()
    if now >= expires or elapsed < -5:
        return Decision("abort", "pilot deadline reached or invalid clock", {})
    if not isinstance(live, dict) or not isinstance(evcc, dict):
        return Decision("abort", "live or EVCC snapshot unavailable", {})
    try:
        if not _fresh(live, now) or not _fresh(evcc, now):
            raise ValueError("stale inverter or EVCC snapshot")
        soc = _finite(live["soc_pct"])
        metrics = {
            "soc_pct": round(soc, 2),
            "battery_w": round(_finite(live["battery_w"])),
            "pv_w": round(_finite(live["pv_w"])),
            "grid_w": round(_finite(live["grid_w"])),
            "ev_a": round(_finite(evcc["channel_currents_a"][1]), 2),
        }
        if not 0 <= soc <= 100:
            raise ValueError("SoC outside physical range")
        # A reported full target ends the pilot even if another reading
        # becomes invalid in the same sample.
        if soc >= TARGET_SOC_PCT:
            return Decision("complete", "house battery reached 25% SoC", metrics)
        if soc < MIN_BATTERY_SOC_PCT:
            return Decision("abort", "house battery fell below 19% SoC", metrics)
        if not 5 <= _finite(live["battery_temp_c"]) <= 45:
            raise ValueError("battery temperature outside conservative range")
        if type(live.get("grid_unbalanced_output_raw")) is not int or live["grid_unbalanced_output_raw"] != 0:
            raise ValueError("inverter balanced phase output unverified")
        phase = [_finite(live["meter_l" + n + "_w"]) for n in "123"]
        grid = _finite(live["grid_w"])
        ac = _finite(live["inverter_ac_w"])
        house = _finite(live["house_load_w"])
        pv = _finite(live["pv_w"])
        battery = _finite(live["battery_w"])
        if house < 0 or pv < 0 or not -12000 <= battery <= 12000:
            raise ValueError("inconsistent load, PV or battery power")
        if (abs(sum(phase) - grid) > max(100, abs(grid) * .05)
                or abs(ac - grid - house) > max(150, house * .1)):
            raise ValueError("invalid three-phase meter balance")
        import_phases = [max(0, -p) for p in phase]
        metrics["phase_import_w"] = [round(p) for p in import_phases]
        if any(p > MAX_PHASE_GRID_IMPORT_W for p in import_phases):
            return Decision("abort", "conservative L1/L2/L3 import bound exceeded", metrics)
        if -grid > MAX_SITE_IMPORT_W:
            return Decision("abort", "conservative total grid import bound exceeded", metrics)
        if (evcc.get("read_ok") is not True
                or evcc.get("state") != "C2"
                or evcc.get("charging_detected") is not True
                or evcc.get("configured_pwm_raw") != 100
                or evcc.get("pilot_pwm_raw") != 100
                or evcc.get("writes") != 0):
            raise ValueError("vehicle 6 A observation lost")
        currents = evcc["channel_currents_a"]
        if (type(currents) is not list or len(currents) != 3
                or not 5 <= _finite(currents[1]) <= 6.5
                or not 0 <= _finite(currents[0]) <= .5
                or not 0 <= _finite(currents[2]) <= .5):
            raise ValueError("EVCC Ict2 not verified within 6 A cap")
        if not service_ok:
            if elapsed >= MAX_INITIAL_SETTLE_S:
                raise ValueError("normal ARMED dispatcher not running")
        if not process_overrides_ok and elapsed >= MAX_INITIAL_SETTLE_S:
            raise ValueError("dispatcher process lacks temporary 1000 W clamp")
        if live.get("work_mode_raw") != 0x303 and elapsed >= MAX_INITIAL_SETTLE_S:
            raise ValueError("inverter is not in expected EMS charge mode")
        if battery < -MAX_CHARGE_MEASURED_W:
            return Decision("abort", "measured house battery charge exceeds 1250 W", metrics)
        if battery > MIN_CHARGE_MEASURED_W and elapsed >= MAX_INITIAL_SETTLE_S:
            return Decision("abort", "battery is discharging during approved charge trial", metrics)
        if battery >= -MIN_CHARGE_MEASURED_W and elapsed >= MAX_INITIAL_SETTLE_S:
            return Decision("abort", "battery not accepting the expected charge", metrics)
        return Decision("continue", "within temporary 1000 W recovery envelope", metrics)
    except (ValueError, KeyError, TypeError, IndexError, OverflowError) as exc:
        return Decision("abort", str(exc)[:150], {})


def _atomic_json(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".soc25-status-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _service(*args) -> str:
    proc = subprocess.run(["/usr/bin/systemctl", *args], capture_output=True,
                          text=True, timeout=60, check=True)
    return proc.stdout.strip()


def _dispatch_running() -> bool:
    return subprocess.run(
        ["/usr/bin/systemctl", "is-active", "--quiet", SERVICE],
        capture_output=True, timeout=8).returncode == 0


def _process_overrides_present() -> bool:
    try:
        pid = int(_service("show", SERVICE, "--property=MainPID", "--value"))
        if pid <= 0:
            return False
        environment = (Path("/proc") / str(pid) / "environ").read_bytes().split(b"\0")
        return all((k + "=" + v).encode("ascii") in environment
                   for k, v in OVERRIDE_ENV.items())
    except (ValueError, OSError, subprocess.SubprocessError):
        return False


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("expected JSON object")
    return data


def _restore(status: dict, *, reason: str, completed: bool) -> dict:
    """Crash-retryable, idempotent restoration of the original unit start.

    A missing drop-in after an interrupted prior attempt STILL requires
    daemon-reload + restart, to remove a still-running pilot process.
    """
    now = datetime.now(UTC)
    status.update(state="restoring", restore_reason=reason,
                  restoring_at_utc=now.isoformat())
    _atomic_json(STATE, status)
    if DROPIN.exists():
        raw = DROPIN.read_bytes()
        if (not raw.startswith(PILOT_MARKER.encode("ascii"))
                or _checksum(DROPIN) != status.get("dropin_sha256")):
            status.update(state="restore_blocked", reason="drop-in unexpectedly changed")
            _atomic_json(STATE, status)
            raise RuntimeError("pilot drop-in checksum changed; refusing to remove unrelated configuration")
        DROPIN.unlink()
    _service("daemon-reload")
    _service("restart", SERVICE)
    if not _dispatch_running() or _process_overrides_present():
        raise RuntimeError("original dispatch process not confirmed after restore")
    status.update(state="completed" if completed else "aborted",
                  restored_at_utc=datetime.now(UTC).isoformat(),
                  reason=reason, physical_control_authorized=False)
    _atomic_json(STATE, status)
    # One-shot pilot should not leave a recurring timer behind.
    subprocess.run(["/usr/bin/systemctl", "disable", "--now", "--no-block", TIMER],
                   capture_output=True, timeout=20, check=False)
    return status


def tick(*, force_stop: bool = False) -> dict:
    """Root-only lifecycle tick. Never imports a Modbus client."""
    if not STATE.exists():
        if DROPIN.exists() and DROPIN.read_bytes().startswith(PILOT_MARKER.encode("ascii")):
            raise RuntimeError("pilot drop-in present without root status: manual recovery required")
        return {"state": "not_installed"}
    status = _load(STATE)
    if status.get("schema_version") != 1 or status.get("pilot_id") != "soc25-20261031":
        raise ValueError("invalid or unexpected pilot state")
    if status["state"] in ("completed", "aborted") and not DROPIN.exists():
        return {"state": status["state"], "reason": status.get("reason")}
    if status["state"] in ("restoring", "restore_blocked"):
        return _restore(status, reason=status.get("restore_reason", "interrupted restore"),
                        completed=False)
    if status["state"] not in ("prepared", "running"):
        raise ValueError("unknown pilot lifecycle state")
    if force_stop:
        return _restore(status, reason="operator requested stop", completed=False)
    now = datetime.now(UTC)
    if now >= _stamp(status["expires_at_utc"]):
        return _restore(status, reason="pilot maximum duration exceeded", completed=False)
    if not DROPIN.exists() or _checksum(DROPIN) != status.get("dropin_sha256"):
        return _restore(status, reason="temporary dispatch unit config missing or changed",
                        completed=False)
    try:
        gate = _load(GATE)
        if (gate.get("mode") != "shadow" or gate.get("schema_version") != 2
                or _checksum(DISPATCH) != gate.get("dispatcher_sha256")
                or _checksum(DISPATCH) != status.get("dispatcher_sha256")):
            return _restore(status, reason="root gate or physical dispatcher changed",
                            completed=False)
        live = _load(LIVE)
        evcc = _load(EVCC)
        decision = evaluate(live, evcc, now=now,
                            started=_stamp(status["started_at_utc"]),
                            expires=_stamp(status["expires_at_utc"]),
                            service_ok=_dispatch_running(),
                            process_overrides_ok=_process_overrides_present())
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return _restore(status, reason="monitoring or root-gate input unavailable",
                        completed=False)
    if decision.action != "continue":
        return _restore(status, reason=decision.reason,
                        completed=decision.action == "complete")
    status.update(state="running", checked_at_utc=now.isoformat(),
                  check_reason=decision.reason, measurements=decision.metrics,
                  physical_control_authorized=True)
    _atomic_json(STATE, status)
    return {"state": "running", "measurements": decision.metrics}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tick", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if not (args.tick or args.stop or args.status):
        parser.error("one of --tick, --stop, --status is required")
    if args.status:
        value = _load(STATE) if STATE.exists() else {"state": "not_installed"}
        print(json.dumps({k: value.get(k) for k in (
            "state", "started_at_utc", "expires_at_utc", "checked_at_utc",
            "measurements", "reason", "restored_at_utc")}, sort_keys=True))
    else:
        print(json.dumps(tick(force_stop=args.stop), sort_keys=True))


if __name__ == "__main__":
    main()
