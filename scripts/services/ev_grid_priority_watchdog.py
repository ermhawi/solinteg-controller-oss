#!/usr/bin/env python3
"""Independent watchdog and rollback for the opt-in permanent EV L3 grid priority.

Runs as root under its own systemd timer. No Modbus, EVCC or inverter writes.
Its root-owned state directory is deliberately separate from /opt/solinteg,
which is writable by the regular Solinteg service user.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

TRUSTED_CODE = Path("/usr/local/libexec/solinteg-ev-grid-priority")
sys.path.insert(0, str(TRUSTED_CODE))
import ev_grid_priority_guard as guard

POLICY_TRUSTED = TRUSTED_CODE / "ev_grid_priority_guard.py"
POLICY_DEPLOYED = Path("/opt/solinteg/app/scripts/services/ev_grid_priority_guard.py")
ROOT = Path("/var/lib/solinteg-ev-grid-priority")
GATE = ROOT / "gate.json"
STATUS = ROOT / "status.json"
BACKUP = ROOT / "dispatch-before.py"
LOCK = ROOT / "watchdog.lock"
DISPATCH = guard.DISPATCH_PATH
LIVE = Path("/opt/solinteg/live.json")
EVCC = Path("/opt/solinteg/ev-shadow-runtime/var/evcc-readonly.json")
SERVICE = "solinteg-dispatch.service"
GRACE_SECONDS = 100


def run(args: list[str], *, timeout: int=30) -> None:
    subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout)


def read_json(path: Path) -> dict:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("missing trusted snapshot")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("snapshot is not an object")
    return data


def atomic_status(data: dict) -> None:
    fd, name = tempfile.mkstemp(dir=str(ROOT), prefix=".ev-grid-status-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, 0o644)
        os.replace(name, STATUS)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def rollback(reason: str, now: datetime, gate: dict | None=None) -> None:
    """Disarm first. Refuse to overwrite a dispatcher changed since activation."""
    status = {"at_utc": now.isoformat(), "state": "rollback_in_progress",
              "reason": reason[:140], "rollback_ok": False}
    atomic_status(status)
    try:
        if GATE.exists() or GATE.is_symlink():
            os.replace(GATE, ROOT / "gate.disabled.json")
    except OSError as exc:
        status.update(state="rollback_failed_gate_not_revoked",
                      error_type=type(exc).__name__)
        atomic_status(status)
        return
    try:
        if not guard._trusted_file(BACKUP):
            raise RuntimeError("root-owned rollback backup missing")
        if DISPATCH.is_symlink() or not DISPATCH.is_file():
            raise RuntimeError("installed dispatcher missing")
        original = BACKUP.read_bytes()
        installed = DISPATCH.read_bytes()
        if (b"def main()" not in original
                or b"from ev_grid_priority_guard import" not in installed):
            raise RuntimeError("dispatcher or backup signature mismatch")
        if (not isinstance(gate, dict)
                or hashlib.sha256(installed).hexdigest() != gate.get("dispatcher_sha256")):
            raise RuntimeError("installed dispatcher changed - refusing overwrite")
        previous = DISPATCH.stat()
        # Normal service shutdown reverts its inverter setpoint to auto.
        run(["/usr/bin/systemctl", "stop", SERVICE])
        fd, name = tempfile.mkstemp(dir=str(DISPATCH.parent),
                                    prefix=".ev-grid-restore-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(original)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(name, previous.st_mode & 0o777)
            os.chown(name, previous.st_uid, previous.st_gid)
            os.replace(name, DISPATCH)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        run(["/usr/bin/systemctl", "start", SERVICE])
        run(["/usr/bin/systemctl", "is-active", "--quiet", SERVICE], timeout=10)
        status.update(state="restored_previous_dispatch", rollback_ok=True)
    except Exception as exc:
        # Gate is revoked even if an operator must manually recover the service.
        status.update(state="rollback_failed_manual_intervention",
                      error_type=type(exc).__name__, rollback_ok=False)
    atomic_status(status)


def idle_ev(evcc: dict, now: datetime) -> bool:
    """A fresh and explicit disconnected/idle EV reading, never a guessed 0 A."""
    try:
        age = (now - guard._stamp(evcc["timestamp"])).total_seconds()
        currents = evcc["channel_currents_a"]
        return (0 <= age <= guard.MAX_EV_AGE_SECONDS
                and evcc.get("source") == "waveshare_evcc_fc03_readonly"
                and evcc.get("read_ok") is True
                and evcc.get("writes") == 0
                and evcc.get("physical_control_eligible") is False
                and (evcc.get("charging_detected") is False
                     or evcc.get("state") == "C2")
                and evcc.get("state") in ("A1", "A2", "B1", "B2", "C2")
                and type(currents) is list and len(currents) == 3
                and all(type(v) in (int, float) and math.isfinite(v)
                        and 0 <= v <= 0.5 for v in currents)
                and type(evcc.get("measured_current_a")) in (int, float)
                and math.isfinite(evcc["measured_current_a"])
                and 0 <= evcc["measured_current_a"] <= 0.6)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def expected_standby(live: dict, evcc: dict, gate: dict, now: datetime) -> bool:
    """Preserve normal battery/solar planning outside the night-tested envelope.

    Only classify intentional PV/SoC standby if all other EV/phase checks pass.
    Stale data, communication failures and thermal faults still cause rollback.
    """
    try:
        pv = guard._finite(live, "pv_w")
        soc = guard._finite(live, "soc_pct")
        temp = guard._finite(live, "battery_temp_c")
        if (not 0 <= pv <= 6000 or not 0 <= soc <= 100
                or not guard.MIN_BATTERY_TEMP_C <= temp <= guard.MAX_BATTERY_TEMP_C
                or not (pv > 100 or not 25 <= soc <= 95)):
            return False
        normalised = {**live, "pv_w":min(pv, 100),
                      "soc_pct":max(25, min(soc, 95))}
        return guard.suggest("discharge", 2300, normalised, evcc,
                             now=now, gate=gate).state == "active"
    except (ValueError, KeyError, TypeError, OverflowError):
        return False


def monitor(now: datetime) -> str:
    try:
        gate = read_json(GATE)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
        return "not_armed"
    mode = gate.get("mode")
    if mode not in ("shadow", "active"):
        return "not_armed"
    if not guard._trusted_file(GATE) or guard.validate_gate(gate, now) != mode:
        rollback("untrusted or invalid gate", now, gate)
        return "rolled_back"
    try:
        if (not guard._trusted_file(Path(__file__).resolve())
                or not guard._trusted_file(POLICY_TRUSTED)):
            raise RuntimeError("watchdog code/policy not root-owned")
        if POLICY_DEPLOYED.is_symlink() or not POLICY_DEPLOYED.is_file():
            raise RuntimeError("installed EV grid policy missing")
        if (hashlib.sha256(POLICY_DEPLOYED.read_bytes()).digest()
                != hashlib.sha256(POLICY_TRUSTED.read_bytes()).digest()):
            raise RuntimeError("root watchdog policy differs from installed dispatcher policy")
        if not guard._trusted_file(BACKUP):
            raise RuntimeError("rollback backup not root-owned")
        if hashlib.sha256(DISPATCH.read_bytes()).hexdigest() != gate["dispatcher_sha256"]:
            raise RuntimeError("dispatcher hash changed")
        run(["/usr/bin/systemctl", "is-active", "--quiet", SERVICE], timeout=8)
        if mode == "shadow":
            atomic_status({"at_utc": now.isoformat(), "state": "shadow_monitor_ok",
                           "activation_id": gate["activation_id"]})
            return "shadow_monitor_ok"
        evcc = read_json(EVCC)
        if idle_ev(evcc, now):
            atomic_status({"at_utc": now.isoformat(), "state": "idle_ev_not_charging",
                           "activation_id": gate["activation_id"]})
            return "idle_ev_not_charging"
        live = read_json(LIVE)
        result = guard.suggest("discharge", 2300, live, evcc, now=now, gate=gate)
        if result.state != "active":
            if expected_standby(live, evcc, gate, now):
                atomic_status({"at_utc": now.isoformat(), "state": "standby_pv_or_soc",
                               "activation_id": gate["activation_id"]})
                return "standby_pv_or_soc"
            raise RuntimeError("unsafe/unknown L3, BMS, EVCC or inverter evidence: " + result.reason)
        battery = live["battery_w"]
        if type(battery) not in (int, float) or not math.isfinite(battery) or not -12000 <= battery <= 12000:
            raise ValueError("invalid battery power")
        try:
            previous = read_json(STATUS)
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
            previous = {}
        eligible_since = now
        if (previous.get("state") == "active_monitor_ok"
                and previous.get("activation_id") == gate["activation_id"]):
            eligible_since = guard._stamp(previous["eligible_since_utc"])
        # A new charging session gets a fresh grace period, even days after rollout.
        if ((now - eligible_since).total_seconds() > GRACE_SECONDS
                and battery > max(850, result.power_w + 650)):
            raise RuntimeError("battery discharge exceeds protective target")
        atomic_status({"at_utc": now.isoformat(), "state": "active_monitor_ok",
                       "activation_id": gate["activation_id"],
                       "eligible_since_utc": eligible_since.isoformat(),
                       "recommended_w": result.power_w})
        return "active_monitor_ok"
    except (OSError, ValueError, TypeError, KeyError, RuntimeError,
            subprocess.SubprocessError) as exc:
        rollback("watchdog preflight failed: " + str(exc)[:110], now, gate)
        return "rolled_back"


def main() -> None:
    with LOCK.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = monitor(datetime.now(timezone.utc))
    print(json.dumps({"state": state, "device_writes": 0}))


if __name__ == "__main__":
    main()
