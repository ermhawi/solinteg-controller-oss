#!/usr/bin/env python3
"""Fail-closed, one-off activation of the EV-paused SoC25 recovery trial.

A *fresh* root-only, read-only preflight must pass before ANY installation.
With --start --confirm this changes the ARMED dispatch process and requires
an operator supervising the physical response; never run unattended.
The independently installed root watchdog owns rollback and expiry.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "services"))
import preflight_soc25_evpaused as preflight
import temporary_soc25_evpaused_watchdog as watchdog
import temporary_soc25_watchdog as common
from temporary_soc25_evpaused_dispatch import REQUIRED_ENV

ROOT_LIB = Path("/usr/local/libexec/solinteg-recovery-25-pilot")
SYSTEMD = Path("/etc/systemd/system")
STATUS_DIR = watchdog.STATE.parent
DISPATCH_EXE = Path("/opt/solinteg/app/.venv/bin/python")
MAX_DURATION = timedelta(hours=2)


def _systemctl(*args: str) -> None:
    subprocess.run(("/usr/bin/systemctl", *args), check=True, timeout=90,
                   capture_output=True, text=True)


def _safe_parent(path: Path) -> bool:
    mode = path.parent.stat()
    return mode.st_uid == 0 and not (mode.st_mode & 0o022)


def _install_owned(path: Path, data: bytes) -> None:
    """Create only in trusted root-owned directories; never clobber unknown files."""
    if not _safe_parent(path):
        raise RuntimeError(f"unsafe root deployment directory: {path.parent.name}")
    if path.is_symlink() or path.exists():
        if path.is_file() and not path.is_symlink() and path.read_bytes() == data:
            installed = path.stat()
            if installed.st_uid == 0 and not (installed.st_mode & 0o022):
                return
        raise RuntimeError(f"refusing to overwrite existing installed file: {path.name}")
    fd, temp = tempfile.mkstemp(prefix=".soc25-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp, 0o644)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _dropin() -> bytes:
    env = " ".join(k + "=" + v for k, v in REQUIRED_ENV.items())
    return (
        watchdog.MARKER + "\n"
        "[Service]\n"
        "Restart=no\n"
        "RuntimeMaxSec=2h\n"
        "ExecStart=\n"
        "ExecStart=/usr/bin/env " + env + " " + str(DISPATCH_EXE) + " "
        + str(watchdog.WRAPPER) + "\n"
    ).encode("ascii")


def _sources() -> list[tuple[Path, Path]]:
    return [
        (REPO_ROOT / "scripts/services/temporary_soc25_evpaused_dispatch.py",
         watchdog.WRAPPER),
        (REPO_ROOT / "scripts/services/temporary_soc25_evpaused_watchdog.py",
         ROOT_LIB / "temporary_soc25_evpaused_watchdog.py"),
        (REPO_ROOT / "deploy/solinteg-temp-soc25-evpaused-watchdog.service",
         SYSTEMD / "solinteg-temp-soc25-evpaused-watchdog.service"),
        (REPO_ROOT / "deploy/solinteg-temp-soc25-evpaused-watchdog.timer",
         SYSTEMD / "solinteg-temp-soc25-evpaused-watchdog.timer"),
    ]


def _status_directory_secure(path: Path) -> bool:
    info = path.stat()
    return info.st_uid == 0 and not (info.st_mode & 0o077)


def activate(*, preflight_result: dict | None = None) -> dict:
    if os.geteuid() != 0:
        raise PermissionError("root required to activate physical test")
    admission = preflight.report() if preflight_result is None else preflight_result
    if not admission.get("eligible"):
        return {"state": "blocked", "reasons": admission.get("reasons", []),
                "device_writes": 0}
    # A caller may only use the optional injected result in tests; production
    # always refreshes the full preflight just before mounting the override.
    if preflight_result is None:
        admission = preflight.report()
        if not admission.get("eligible"):
            return {"state": "blocked", "reasons": admission.get("reasons", []),
                    "device_writes": 0}
    if watchdog.STATE.exists() or watchdog.STATE.is_symlink():
        raise RuntimeError("one-off trial state already exists; never reactivate it")
    if watchdog.DROPIN.exists() or watchdog.DROPIN.is_symlink():
        raise RuntimeError("one-off dispatcher drop-in already exists")
    if not DISPATCH_EXE.is_file():
        raise RuntimeError("installed dispatcher Python environment missing")

    ROOT_LIB.mkdir(parents=True, exist_ok=True)
    SYSTEMD.mkdir(parents=True, exist_ok=True)
    STATUS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not _status_directory_secure(STATUS_DIR):
        raise RuntimeError("pilot status directory must be root-only")
    watchdog.DROPIN.parent.mkdir(parents=True, exist_ok=True)

    for source, dest in _sources():
        _install_owned(dest, source.read_bytes())
    # Units may now be installed but remain inactive. A second read-only
    # admission check catches any SoC/phase shift during the file copies.
    final_admission = preflight.report() if preflight_result is None else admission
    if not final_admission.get("eligible"):
        return {"state": "blocked", "reasons": final_admission.get("reasons", []),
                "units_installed_inactive": True, "device_writes": 0}

    start = datetime.now(timezone.utc)
    dropin_content = _dropin()
    status = {
        "schema_version": 1,
        "pilot_id": watchdog.PILOT_ID,
        "state": "prepared",
        "boot_id": watchdog._boot_id(),
        "started_at_utc": start.isoformat(),
        "expires_at_utc": (start + MAX_DURATION).isoformat(),
        "dispatcher_sha256": hashlib.sha256(common.DISPATCH.read_bytes()).hexdigest(),
        "wrapper_sha256": hashlib.sha256(watchdog.WRAPPER.read_bytes()).hexdigest(),
        "dropin_sha256": hashlib.sha256(dropin_content).hexdigest(),
        "physical_control_authorized": False,
    }
    watchdog.configure_common()
    common._atomic_json(watchdog.STATE, status)
    try:
        _install_owned(watchdog.DROPIN, dropin_content)
        _systemctl("daemon-reload")
        _systemctl("enable", "--now", watchdog.TIMER)
        _systemctl("restart", common.SERVICE)
        first = watchdog.tick()
        if first.get("state") != "running":
            raise RuntimeError("root watchdog did not accept initial pilot tick")
        return {"state": "started_supervised", "pilot_id": watchdog.PILOT_ID,
                "expires_at_utc": status["expires_at_utc"],
                "initial_watchdog": first, "physical_charge_requires_readback": True}
    except Exception:
        # An interrupted install must not leave an unmonitored temporary
        # ARMED dispatcher. The tested restore only deletes our own hashed
        # drop-in and restarts the original dispatcher.
        watchdog.tick(force_stop=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--start", action="store_true")
    parser.add_argument("--confirm", action="store_true",
                        help="operator approved physical ARMED dispatcher change")
    args = parser.parse_args()
    if args.start and not args.confirm:
        parser.error("--start requires --confirm")
    if args.check:
        result = preflight.report()
    else:
        result = activate()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if (args.check and not result.get("eligible")) or result.get("state") == "blocked":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
