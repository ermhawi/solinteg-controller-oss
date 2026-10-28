#!/usr/bin/env python3
"""Independent 15-s watchdog for one temporary Solinteg L3 EV-grid trial.

Runs as ROOT in its own systemd timer, not in dispatch's process.
On deadline, missing fresh EVCC/inverter evidence, incoherent phase CT,
unexpected battery discharge or stopped dispatch:
  1. revoke the ROOT-owned active trial gate;
  2. restore the backed-up original dispatcher atomically;
  3. restart original dispatch service; record result.
No inverter/EVSE Modbus connection or direct physical register writes.
The original dispatcher preserves existing BMS safety, optimizer and gates.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0,"/opt/solinteg/app/scripts/services")
import ev_grid_priority_guard as guard

ROOT=Path("/opt/solinteg")
GATE=ROOT/"ev-grid-priority-trial.json"
BACKUP=ROOT/"ev-grid-priority-dispatch-before.py"
DISPATCH=ROOT/"app/scripts/services/dispatch_loop.py"
LIVE=ROOT/"live.json"
EVCC=ROOT/"ev-shadow-runtime/var/evcc-readonly.json"
STATUS=ROOT/"ev-grid-priority-trial-status.json"
LOCK=ROOT/"ev-grid-priority-trial-watchdog.lock"
SERVICE="solinteg-dispatch.service"
GRACE_SECONDS=100


def run(args: list[str], *, timeout: int=24) -> None:
    subprocess.run(args,check=True,capture_output=True,text=True,timeout=timeout)


def read_json(path: Path) -> dict:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("missing trusted source")
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_status(data: dict) -> None:
    fd,name=tempfile.mkstemp(dir=str(ROOT),prefix=".ev-grid-trial-status-")
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as f:
            json.dump(data,f,sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name,0o644)
        os.replace(name,STATUS)
    finally:
        if os.path.exists(name):os.unlink(name)


def restore(reason: str, now: datetime) -> None:
    status={"at_utc":now.isoformat(),"state":"rollback_in_progress",
            "reason":reason[:140],"rollback_ok":False}
    atomic_status(status)
    # Disarm first, even if later file or systemd calls fail.
    if GATE.exists():
        GATE.rename(ROOT/"ev-grid-priority-trial.finished.json")
    try:
        if not BACKUP.is_file():
            raise RuntimeError("original dispatcher backup missing")
        original=BACKUP.read_bytes()
        if "def main()" not in original.decode("utf-8"):
            raise RuntimeError("original dispatcher backup invalid")
        backup_stat=BACKUP.stat()
        existing_stat=DISPATCH.stat()
        if b"from ev_grid_priority_guard import" not in DISPATCH.read_bytes():
            raise RuntimeError("dispatch patch marker missing - no overwrite")
        # Graceful stop lets existing service revert its own setpoint to auto.
        run(["/usr/bin/systemctl","stop",SERVICE],timeout=24)
        fd,tmp=tempfile.mkstemp(dir=str(DISPATCH.parent),prefix=".ev-grid-trial-restore-")
        try:
            with os.fdopen(fd,"wb") as f:
                f.write(original);f.flush();os.fsync(f.fileno())
            os.chmod(tmp,existing_stat.st_mode & 0o777)
            os.chown(tmp,existing_stat.st_uid,existing_stat.st_gid)
            os.replace(tmp,DISPATCH)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)
        run(["/usr/bin/systemctl","start",SERVICE],timeout=28)
        run(["/usr/bin/systemctl","is-active","--quiet",SERVICE],timeout=7)
        status.update(state="restored_original_dispatch",rollback_ok=True)
    except Exception as exc:
        status.update(state="rollback_failed_dispatch_may_be_stopped",
                      error_type=type(exc).__name__,rollback_ok=False)
        # An operator must inspect/recover; do not retry blindly every 15s.
    atomic_status(status)


def quality_reason(now: datetime,gate: dict) -> str | None:
    try:
        if "from ev_grid_priority_guard import" not in DISPATCH.read_text(encoding="utf-8"):
            return "guard hook no longer loaded"
        run(["/usr/bin/systemctl","is-active","--quiet",SERVICE],timeout=8)
        live=read_json(LIVE)
        evcc=read_json(EVCC)
        recommendation=guard.suggest("discharge",2300,live,evcc,now=now,gate=gate)
        if recommendation.state != "active":
            return "fresh EV or BMS/L3 data not reliable: "+recommendation.reason
        if live.get("work_mode") != "EMS BattCtrl":
            return "unexpected inverter work mode"
        elapsed=(now-guard._stamp(gate["started_utc"])).total_seconds()
        batt=live["battery_w"]
        if (type(batt) not in (int,float) or not -200 <= batt <= 12000):
            return "implausible battery power"
        # Allow one dispatcher 60s tick and inverter mode transition.
        # A small EMS minimum setpoint often reads 180-300W physically.
        if elapsed>GRACE_SECONDS and batt>max(
            850, recommendation.power_w+650):
            return "battery discharge remains above EV-protective target"
        return None
    except (OSError,ValueError,TypeError,RuntimeError,json.JSONDecodeError,
            subprocess.SubprocessError) as exc:
        return "watchdog preflight failed: "+type(exc).__name__


def monitor(now: datetime) -> str:
    try:
        gate=read_json(GATE)
    except (OSError,ValueError,json.JSONDecodeError,RuntimeError):
        return "no_active_trial"
    if gate.get("mode") != "active":
        return "not_armed"
    try:
        expiry=guard._stamp(gate["expires_utc"])
    except (ValueError,TypeError,KeyError):
        restore("invalid expiration",now)
        return "rolled_back"
    if now>=expiry:
        restore("scheduled trial deadline",now)
        return "rolled_back"
    if guard.validate_gate(gate,now)!="active":
        restore("invalid or expired authorization",now)
        return "rolled_back"
    reason=quality_reason(now,gate)
    if reason:
        restore(reason,now)
        return "rolled_back"
    atomic_status({
        "at_utc":now.isoformat(),"state":"active_monitor_ok",
        "expires_utc":expiry.isoformat(),"rollback_ok":None})
    return "active_monitor_ok"


def main() -> None:
    with LOCK.open("a+") as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
        state=monitor(datetime.now(timezone.utc))
    print(json.dumps({"state":state,"device_writes":0},ensure_ascii=False))


if __name__=="__main__":
    main()
