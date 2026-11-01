#!/usr/bin/env python3
"""Safely install current-boot SoC25 reserve protection with root watchdog.

Physical activation is explicit (--start --confirm), never on a read-only
check. Independent watchdog owns rollback; no shared .env, firmware/BMS,
EVCC or installed dispatcher source is rewritten.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "services"))
import preflight_soc25_reserve as preflight
import soc25_reserve_watchdog as watchdog
import temporary_soc25_watchdog as common
from soc25_reserve_dispatch import REQUIRED_ENV

# Reuse the verified root-ownership/write-once installer primitive.
from start_soc25_evpaused_pilot import _install_owned

SYSTEMD = Path("/etc/systemd/system")
STATUS_DIR = watchdog.STATE.parent
PYTHON = Path("/opt/solinteg/app/.venv/bin/python")
MAX_TRIAL_DURATION = timedelta(hours=6)


def _service(*args: str) -> None:
    subprocess.run(("/usr/bin/systemctl", *args), check=True, timeout=90,
                   capture_output=True, text=True)


def _dropin() -> bytes:
    env = " ".join(k + "=" + v for k, v in REQUIRED_ENV.items())
    # No RuntimeMaxSec: a previous trial showed it being retroactively
    # enforced on the ALREADY-RUNNING dispatcher at daemon-reload.
    return (watchdog.MARKER + "\n[Service]\n"
            "ExecStart=\n"
            "ExecStart=/usr/bin/env " + env + " " + str(PYTHON) + " "
            + str(watchdog.WRAPPER) + "\n").encode("ascii")


def _sources() -> list[tuple[Path, Path]]:
    return [
        (REPO/"scripts/services/soc25_reserve_policy.py", watchdog.POLICY),
        (REPO/"scripts/services/soc25_reserve_dispatch.py", watchdog.WRAPPER),
        (REPO/"scripts/services/soc25_reserve_watchdog.py",
         watchdog.ROOT_LIB/"soc25_reserve_watchdog.py"),
        (REPO/"scripts/services/temporary_soc25_watchdog.py",
         watchdog.ROOT_LIB/"temporary_soc25_watchdog.py"),
        (REPO/"deploy/solinteg-soc25-reserve-watchdog.service",
         SYSTEMD/"solinteg-soc25-reserve-watchdog.service"),
        (REPO/"deploy/solinteg-soc25-reserve-watchdog.timer",
         SYSTEMD/"solinteg-soc25-reserve-watchdog.timer"),
        (REPO/"deploy/solinteg-soc25-reserve-deadline.service",
         SYSTEMD/"solinteg-soc25-reserve-deadline.service"),
        (REPO/"deploy/solinteg-soc25-reserve-deadline.timer",
         SYSTEMD/"solinteg-soc25-reserve-deadline.timer"),
    ]


def _root_dirs_secure() -> bool:
    for path, mask in ((STATUS_DIR, 0o077), (watchdog.ROOT_LIB, 0o022)):
        info = path.stat()
        if info.st_uid != 0 or info.st_mode & mask:
            return False
    return True


def activate() -> dict:
    if os.geteuid() != 0:
        raise PermissionError("root required for commissioning")
    admission = preflight.report()
    if not admission["eligible"]:
        return {"state": "blocked", "reasons": admission["reasons"], "device_writes": 0}
    if watchdog.STATE.exists() or watchdog.STATE.is_symlink():
        raise RuntimeError("reserve status already exists; must review before repeat")
    if watchdog.DROPIN.exists() or watchdog.DROPIN.is_symlink():
        raise RuntimeError("reserve overlay already exists")
    if not PYTHON.is_file():
        raise RuntimeError("original installed dispatcher interpreter missing")
    watchdog.ROOT_LIB.mkdir(mode=0o755, parents=True, exist_ok=True)
    STATUS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not _root_dirs_secure():
        raise RuntimeError("root-owned reserve directories not secure")
    watchdog.DROPIN.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    for source, dest in _sources():
        _install_owned(dest, source.read_bytes())
    admission = preflight.report()
    if not admission["eligible"]:
        return {"state": "blocked", "reasons": admission["reasons"],
                "units_installed_inactive": True, "device_writes": 0}
    start = datetime.now(timezone.utc)
    status = {
        "schema_version": 1, "pilot_id": watchdog.ID, "state": "prepared",
        "boot_id": watchdog._boot_id(),
        "started_at_utc": start.isoformat(),
        "expires_at_utc": (start + MAX_TRIAL_DURATION).isoformat(),
        "dispatcher_sha256": hashlib.sha256(common.DISPATCH.read_bytes()).hexdigest(),
        "dropin_sha256": hashlib.sha256(_dropin()).hexdigest(),
        "wrapper_sha256": hashlib.sha256(watchdog.WRAPPER.read_bytes()).hexdigest(),
        "policy_sha256": hashlib.sha256(watchdog.POLICY.read_bytes()).hexdigest(),
        "physical_control_authorized": False,
    }
    watchdog.configure_common()
    common._atomic_json(watchdog.STATE, status)
    try:
        _install_owned(watchdog.DROPIN, _dropin())
        _service("daemon-reload")
        # A boot-relative watchdog tick may fire immediately on enable.
        # The independent deadline must already be active when that happens.
        _service("enable", "--now", watchdog.DEADLINE_TIMER)
        _service("enable", "--now", watchdog.TIMER)
        _service("restart", common.SERVICE)
        initial = watchdog.tick()
        if initial["state"] != "running":
            raise RuntimeError("independent watchdog did not accept startup")
        return {"state": "started_supervised", "activated_at_utc": status["started_at_utc"],
                "expires_at_utc": status["expires_at_utc"],
                "entry_soc_pct": 26.5, "exit_soc_pct": 28.0,
                "checks": initial, "battery_charge_requires_physical_readback": True,
                "boot_scoped": True}
    except Exception:
        watchdog.tick(stop=True)
        raise


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    opts = p.add_mutually_exclusive_group(required=True)
    opts.add_argument("--check", action="store_true")
    opts.add_argument("--start", action="store_true")
    p.add_argument("--confirm", action="store_true")
    args = p.parse_args()
    if args.start and not args.confirm:
        p.error("--start requires --confirm")
    result = preflight.report() if args.check else activate()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("state") == "blocked" or (args.check and not result["eligible"]):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
