#!/usr/bin/env python3
"""Explicit fresh one-off 26->28% Sunwoda trial, never reuse aborted state.

Root-only, read-only safety preflight twice. Original dispatch source,
native SoC floor, EVCC and the prior aborted trial remain unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "services"))
import preflight_soc25_reserve as preflight
import soc25_recovery_third_watchdog as watchdog
import soc25_reserve_watchdog as base
import temporary_soc25_watchdog as common
from soc25_reserve_dispatch import REQUIRED_ENV
from start_soc25_evpaused_pilot import _install_owned

SYSTEMD = Path("/etc/systemd/system")
PYTHON = Path("/opt/solinteg/app/.venv/bin/python")
MAX_TRIAL_DURATION = timedelta(minutes=60)
PREVIOUS = Path("/var/lib/solinteg-soc25-reserve/status.json")
PREVIOUS_BYPASS = Path("/var/lib/solinteg-soc25-plan-bypass/status.json")
PREVIOUS_RETRY = Path("/var/lib/solinteg-soc25-recovery-retry/status.json")
PREVIOUS_RETRY_DROPIN = Path("/etc/systemd/system/solinteg-dispatch.service.d/49-soc25-recovery-retry.conf")
ORIGINAL_ENTRY = 26.5
TARGET = 28.0


def _service(*args: str) -> None:
    subprocess.run(("/usr/bin/systemctl", *args), check=True, timeout=90,
                   capture_output=True, text=True)


def _active(unit: str) -> bool:
    return subprocess.run(["/usr/bin/systemctl", "is-active", "--quiet", unit],
                          check=False, timeout=8).returncode == 0


def _sources() -> list[tuple[Path, Path]]:
    lib = watchdog.ROOT_LIB
    return [
        (REPO/"scripts/services/soc25_reserve_policy.py", lib/"soc25_reserve_policy.py"),
        (REPO/"scripts/services/soc25_reserve_dispatch.py", lib/"soc25_reserve_dispatch.py"),
        (REPO/"scripts/services/soc25_reserve_watchdog.py", lib/"soc25_reserve_watchdog.py"),
        (REPO/"scripts/services/temporary_soc25_watchdog.py", lib/"temporary_soc25_watchdog.py"),
        (REPO/"scripts/services/soc25_recovery_third_dispatch.py", watchdog.WRAPPER),
        (REPO/"scripts/services/soc25_recovery_third_watchdog.py",
         lib/"soc25_recovery_third_watchdog.py"),
        (REPO/"deploy/solinteg-soc25-recovery-third-watchdog.service",
         SYSTEMD/"solinteg-soc25-recovery-third-watchdog.service"),
        (REPO/"deploy/solinteg-soc25-recovery-third-watchdog.timer",
         SYSTEMD/"solinteg-soc25-recovery-third-watchdog.timer"),
        (REPO/"deploy/solinteg-soc25-recovery-third-deadline.service",
         SYSTEMD/"solinteg-soc25-recovery-third-deadline.service"),
        (REPO/"deploy/solinteg-soc25-recovery-third-deadline.timer",
         SYSTEMD/"solinteg-soc25-recovery-third-deadline.timer"),
    ]


def _dropin() -> bytes:
    env = " ".join(k+"="+v for k,v in REQUIRED_ENV.items())
    return (watchdog.MARKER+"\n[Service]\n"
            "ExecStart=\n"
            "ExecStart=/usr/bin/env "+env+" "+str(PYTHON)+" "+str(watchdog.WRAPPER)+"\n"
            ).encode("ascii")


def _require_previous_trial_restored() -> None:
    previous = json.loads(PREVIOUS.read_text())
    bypass = json.loads(PREVIOUS_BYPASS.read_text())
    second = json.loads(PREVIOUS_RETRY.read_text())
    if (previous.get("state") != "aborted"
            or second.get("state") != "aborted"
            or second.get("charge_samples") != 0
            or second.get("physical_control_authorized") is not False
            or bypass.get("state") != "stopped"
            or bypass.get("changed_runs_skipped") != 0
            or bypass.get("restored_runs") != 49):
        raise RuntimeError("previous trial and 49 optimizer rows not independently restored")
    if PREVIOUS_RETRY_DROPIN.exists() or PREVIOUS_RETRY_DROPIN.is_symlink():
        raise RuntimeError("second trial overlay not independently restored")
    for unit in (
            "solinteg-soc25-reserve-watchdog.timer",
            "solinteg-soc25-reserve-deadline.timer",
            "solinteg-soc25-recovery-retry-watchdog.timer",
            "solinteg-soc25-recovery-retry-deadline.timer",
            "solinteg-soc25-plan-bypass.timer",
            "solinteg-soc25-plan-bypass-deadline.timer"):
        if _active(unit):
            raise RuntimeError("earlier trial timer still active: "+unit)


def _secure(path: Path, mask: int) -> bool:
    info = path.stat()
    return info.st_uid == 0 and not info.st_mode & mask


def activate() -> dict:
    if os.geteuid() != 0:
        raise PermissionError("root required for physical trial")
    admission = preflight.report()
    if not admission["eligible"]:
        return {"state":"blocked", "reasons":admission["reasons"],"device_writes":0}
    soc = admission["measurements"]["soc_pct"]
    if not 25 <= soc <= 26:
        return {"state":"blocked","reasons":["retry only eligible at 25-26% SoC"],
                "device_writes":0}
    _require_previous_trial_restored()
    watchdog.configure()
    if watchdog.STATE.exists() or watchdog.STATE.is_symlink():
        raise RuntimeError("retry identity already exists: refuse reactivation")
    if watchdog.DROPIN.exists() or watchdog.DROPIN.is_symlink():
        raise RuntimeError("retry override already exists")
    if not PYTHON.is_file() or _active(watchdog.TIMER) or _active(watchdog.DEADLINE_TIMER):
        raise RuntimeError("retry runtime already active or interpreter missing")
    watchdog.ROOT_LIB.mkdir(mode=0o755, parents=True, exist_ok=True)
    watchdog.STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not (_secure(watchdog.ROOT_LIB,0o022) and _secure(watchdog.STATE.parent,0o077)):
        raise RuntimeError("retry root paths not private or root owned")
    watchdog.DROPIN.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
    for source, dest in _sources():
        _install_owned(dest, source.read_bytes())
    admission = preflight.report()
    if not admission["eligible"] or not 25 <= admission["measurements"]["soc_pct"] <= 26:
        return {"state":"blocked","reasons":admission.get("reasons",[])+[
                "SoC drifted outside retry window"],"units_installed_inactive":True,
                "device_writes":0}
    start = datetime.now(timezone.utc)
    status = {
        "schema_version":1,
        "pilot_id":watchdog.ID,
        "state":"prepared",
        "boot_id":base._boot_id(),
        "started_at_utc":start.isoformat(),
        "expires_at_utc":(start+MAX_TRIAL_DURATION).isoformat(),
        "initial_soc_pct":admission["measurements"]["soc_pct"],
        "charge_samples":0,
        "physical_charge_verified":False,
        "dispatcher_sha256":hashlib.sha256(common.DISPATCH.read_bytes()).hexdigest(),
        "dropin_sha256":hashlib.sha256(_dropin()).hexdigest(),
        "wrapper_sha256":hashlib.sha256(watchdog.WRAPPER.read_bytes()).hexdigest(),
        "policy_sha256":hashlib.sha256(watchdog.POLICY.read_bytes()).hexdigest(),
        "base_wrapper_sha256":hashlib.sha256(
            (watchdog.ROOT_LIB/"soc25_reserve_dispatch.py").read_bytes()).hexdigest(),
        "physical_control_authorized":False,
    }
    common._atomic_json(watchdog.STATE,status)
    try:
        _install_owned(watchdog.DROPIN,_dropin())
        _service("daemon-reload")
        _service("enable","--now",watchdog.DEADLINE_TIMER)
        _service("restart",common.SERVICE)
        # Start the periodic timer AFTER the first synchronous check.
        # OnBootSec triggers instantly when the host has been up >30s.
        initial=watchdog.tick()
        if initial.get("state")!="running":
            raise RuntimeError("independent retry supervisor rejected startup")
        _service("enable","--now",watchdog.TIMER)
        return {
            "state":"started_supervised","started_at_utc":start.isoformat(),
            "expires_at_utc":status["expires_at_utc"],
            "entry_soc_pct":ORIGINAL_ENTRY,"exit_soc_pct":TARGET,
            "checks":initial,"device_writes":"original dispatcher only",
            "original_trial_untouched":True,
        }
    except Exception:
        watchdog.tick(stop=True)
        raise


def main() -> None:
    p=argparse.ArgumentParser(description=__doc__)
    cmd=p.add_mutually_exclusive_group(required=True)
    cmd.add_argument("--check",action="store_true")
    cmd.add_argument("--start",action="store_true")
    p.add_argument("--confirm",action="store_true")
    args=p.parse_args()
    if args.start and not args.confirm:
        p.error("--start requires --confirm")
    if args.check:
        try:
            result=preflight.report()
            if result["eligible"]:
                soc=result["measurements"]["soc_pct"]
                if not 25 <= soc <= 26:
                    result.update(eligible=False,reasons=["retry only at 25-26% SoC"])
                else:
                    _require_previous_trial_restored()
        except (OSError,ValueError,KeyError,RuntimeError) as exc:
            result={"eligible":False,"reasons":[str(exc)[:130]],"device_writes":0}
    else:
        result=activate()
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if result.get("state")=="blocked" or (args.check and not result["eligible"]):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
