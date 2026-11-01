#!/usr/bin/env python3
"""Explicit one-off installation of the supervised SoC25 plan-only bypass.

No Modbus / EVCC writes; the installed dispatcher reads modified optimizer
plan slots, then returns to its original optimizer behavior by 21:45 CET.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/"scripts/services"))
import temporary_soc25_plan_bypass as policy
from start_soc25_evpaused_pilot import _install_owned

ROOT_LIB = policy.ROOT_CODE.parent
SYSTEMD = Path("/etc/systemd/system")
SOURCES = (
    (ROOT/"scripts/services/temporary_soc25_plan_bypass.py",policy.ROOT_CODE),
    (ROOT/"deploy/solinteg-soc25-plan-bypass.service",SYSTEMD/"solinteg-soc25-plan-bypass.service"),
    (ROOT/"deploy/solinteg-soc25-plan-bypass.timer",SYSTEMD/"solinteg-soc25-plan-bypass.timer"),
    (ROOT/"deploy/solinteg-soc25-plan-bypass-deadline.service",
     SYSTEMD/"solinteg-soc25-plan-bypass-deadline.service"),
    (ROOT/"deploy/solinteg-soc25-plan-bypass-deadline.timer",
     SYSTEMD/"solinteg-soc25-plan-bypass-deadline.timer"),
)


def _systemctl(*args) -> None:
    subprocess.run(("/usr/bin/systemctl",*args),check=True,
                   timeout=45,text=True,capture_output=True)


def start() -> dict:
    if os.geteuid() != 0:
        raise PermissionError("root required")
    if policy.STATE.exists() or policy.STATE.is_symlink():
        raise RuntimeError("one-off plan-bypass status already exists")
    ok,reason,metrics=policy.safe_now(__import__("datetime").datetime.now(policy.UTC))
    if not ok:
        return {"state":"blocked","reason":reason,"device_writes":0}
    # Root code must be immutable and be copied from a reviewed repo
    # only once. No replacement of an unknown prior installed file.
    ROOT_LIB.mkdir(mode=0o755,parents=True,exist_ok=True)
    policy.STATE.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    for path,mask in ((ROOT_LIB,0o022),(policy.STATE.parent,0o077)):
        stat=path.stat()
        if stat.st_uid != 0 or stat.st_mode & mask:
            raise RuntimeError("untrusted root-only one-off path")
    for source,dest in SOURCES:
        _install_owned(dest,source.read_bytes())
    initial=policy.activate()
    if initial.get("state")!="prepared":
        return initial
    try:
        _systemctl("daemon-reload")
        # Independent calendar stop first, then periodic patching. Neither
        # changes the current systemd dispatch process or site config.
        _systemctl("enable","--now",policy.DEADLINE_TIMER)
        _systemctl("enable","--now",policy.TIMER)
        first=policy.tick()
        if first.get("state")!="running":
            raise RuntimeError("initial DB plan bypass not supervised")
        return {"state":"started_supervised","initial":first,
                "expires_at_utc":policy.EXPIRES.isoformat(),"device_writes":0}
    except Exception:
        # Stop and exact restore even if enable/initial tick fails.
        policy.tick(force_stop=True)
        raise


def main() -> None:
    a=argparse.ArgumentParser(description=__doc__)
    group=a.add_mutually_exclusive_group(required=True)
    group.add_argument("--check",action="store_true")
    group.add_argument("--start",action="store_true")
    a.add_argument("--confirm",action="store_true")
    opts=a.parse_args()
    if opts.start and not opts.confirm:
        a.error("physical plan-change test requires --confirm")
    if opts.check:
        ok,reason,metrics=policy.safe_now(__import__("datetime").datetime.now(policy.UTC))
        result={"eligible":ok,"reason":reason,"measurements":metrics,
                "device_writes":0}
    else:
        result=start()
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if result.get("state")=="blocked" or (opts.check and not result["eligible"]):
        raise SystemExit(2)


if __name__=="__main__":
    main()
