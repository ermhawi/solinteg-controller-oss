#!/usr/bin/env python3
"""Bounded EVCC2 6 A -> pause -> 6 A physical acceptance test.

Only for an explicitly approved, attended local test. Two FC10 attempts
maximum and NEVER a blind retry. If outcome is uncertain, stop immediately
and require human/device inspection before any further write. No Volvo,
house inverter or grid changes. Pure preflight needs no write permission.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
from evcc_ascii import (EvccAscii, EvccUncertainWrite, RAW_PAUSE,
                        observed_evcc_mode, resume_setpoint_readback)
import evcc_cable_tracker as cable_tracker
import ev_price_pause_owner as owner
import ev_volvo_smart_charging as smart

BASE = Path("/opt/solinteg/ev-shadow-runtime/var")


class PauseOutcomeUncertain(RuntimeError):
    """Pause FC10 may have been applied; stop and inspect physical FC03."""


class ResumeOutcomeUncertain(RuntimeError):
    """Resume FC10 may have been applied; stop and inspect physical FC03."""


def _recent(stamp: str, *, now: datetime, max_age: int = 65) -> bool:
    t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return t.tzinfo is not None and 0 <= (now - t).total_seconds() <= max_age


def preflight(*, now: datetime, directory: Path = BASE, grid_path: Path = Path("/opt/solinteg/live.json"),
              expected_vin_hash: str | None) -> dict:
    """Read existing snapshots only; zero devices touched."""
    evcc = json.loads((directory / "evcc-readonly.json").read_text())
    grid = json.loads(grid_path.read_text())
    cable = cable_tracker.read_fresh_connected(
        directory / "evcc-cable-state.json", now=now)
    smart_state, reason = smart.read_smart_charging(
        directory / "volvo-smart-charging.json", now=now,
        expected_vin_sha256=expected_vin_hash)
    own = owner.read_owner(directory / "ev-price-pause-owner.json")
    if (cable is None or own is not None or smart_state != "off"
            or not _recent(evcc["timestamp"], now=now)
            or not _recent(grid["timestamp"], now=now)
            or evcc.get("source") != "waveshare_evcc_fc03_readonly"
            or evcc.get("read_ok") is not True
            or evcc.get("writes") != 0
            or evcc.get("physical_control_eligible") is not False
            or evcc["state"] != cable["observed_state"]
            or observed_evcc_mode(evcc["state"], evcc["configured_pwm_raw"],
                                  evcc["pilot_pwm_raw"],
                                  evcc["channel_currents_a"]) != "charging_6a"
            or grid.get("grid_unbalanced_output_raw") != 0
            or any(type(grid.get(k)) not in (float, int)
                   or not math.isfinite(grid[k]) for k in ("grid_w", "meter_l3_w"))
            or max(0, -grid["meter_l3_w"]) > 3000
            or max(0, -grid["grid_w"]) > 6000):
        return {"ready": False, "reason": "qualification source or fuse envelope invalid",
                "device_writes": 0}
    status = json.loads((directory / "status.json").read_text())
    if (status.get("connected") is not True or status.get("target_soc_pct") != 80
            or type(status.get("battery_soc_pct")) not in (int, float)
            or not 0 <= status["battery_soc_pct"] < 80
            or not _recent(status["checked_at_utc"], now=now, max_age=300)
            or not _recent(status["energy_api_received_at_utc"], now=now, max_age=300)):
        return {"ready": False, "reason": "Volvo charge target or source freshness unverified",
                "device_writes": 0}
    return {"ready": True, "reason": reason, "evcc_before": "C2/100/100/6A",
            "volvo_soc_pct": status["battery_soc_pct"],
            "cable_epoch": cable["connection_epoch"],
            "coverage_epoch": cable["coverage_epoch"],
            "device_writes": 0}


def run_physical(*, expected_vin_hash: str | None, host: str,
                 port: int = 4196, directory: Path = BASE) -> dict:
    if os.environ.get("EVCC_PHYSICAL_QUALIFICATION_APPROVED") != "1":
        raise PermissionError("physical qualification not explicitly approved")
    if any(os.environ.get(key) != "1" for key in (
            "EV_ENABLED", "EVCC_CONTROL_ARMED", "EVCC_MANUAL_TEST",
            "EVCC_START_APPROVED")):
        raise PermissionError("physical EVCC gates are not fully armed")
    if not host or not 1 <= port <= 65535:
        raise ValueError("EVCC endpoint required")
    now = datetime.now(timezone.utc)
    grid = Path(os.environ.get("INVERTER_DATA_PATH", "/opt/solinteg/live.json"))
    prior = preflight(now=now, directory=directory,
                      grid_path=grid, expected_vin_hash=expected_vin_hash)
    if not prior["ready"]:
        return prior

    def _locked_file(path: Path, *, private: bool):
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or (info.st_mode & (0o077 if private else 0o022))):
            os.close(fd)
            raise PermissionError("unsafe EVCC lock")
        return os.fdopen(fd, "a+b")

    # Same physical serial lock and price-owner lock as the production writer.
    with _locked_file(directory / "ev-price-pause-owner.lock", private=True) as owner_lock:
        fcntl.flock(owner_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with _locked_file(directory / "evcc-reader.lock", private=False) as reader_lock:
            fcntl.flock(reader_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            refreshed = preflight(now=datetime.now(timezone.utc),
                                  directory=directory, grid_path=grid,
                                  expected_vin_hash=expected_vin_hash)
            if not refreshed["ready"]:
                return refreshed
            evcc = EvccAscii(host, port=port, manual_test=True)
            initial = evcc.status()
            if observed_evcc_mode(initial.state, initial.configured_raw,
                                  initial.pilot_raw, initial.currents_a) != "charging_6a":
                return {"ready": False, "reason": "physical EVCC differs from preflight",
                        "device_writes": 0}
            # First FC10 attempts stop. Never retry a failed or ambiguous ACK.
            try:
                paused = evcc.set_limit_raw(RAW_PAUSE, expected_raw=100)
            except EvccUncertainWrite as exc:
                raise PauseOutcomeUncertain('pause uncertain; inspect FC03') from exc
            if observed_evcc_mode(paused.state, paused.configured_raw,
                                  paused.pilot_raw, paused.currents_a) != "paused_connected":
                raise RuntimeError("pause not physically verified")
            time.sleep(2)
            # Second FC10 restores the *same* 6A setpoint, not a higher limit.
            try:
                resumed = evcc.set_limit_raw(100, expected_raw=RAW_PAUSE)
            except EvccUncertainWrite as exc:
                raise ResumeOutcomeUncertain('resume uncertain; inspect FC03') from exc
            if not resume_setpoint_readback(resumed.state, resumed.configured_raw,
                                            resumed.pilot_raw, resumed.currents_a):
                raise RuntimeError("resume not verified")
            return {"ready": True, "physically_qualified": True,
                    "test": "6A_pause1000_resume100",
                    "pause_readback": "B2/1000/267/low_current",
                    "resume_readback": resumed.state + "/100/100",
                    "fuse_limits_unchanged": True,
                    "device_writes": 2}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--preflight", action="store_true")
    group.add_argument("--physical", action="store_true")
    args = parser.parse_args()
    try:
        if args.preflight:
            result = preflight(
                now=datetime.now(timezone.utc),
                expected_vin_hash=os.environ.get("EV_VOLVO_TARGET_VIN_SHA256"))
        else:
            result = run_physical(
                expected_vin_hash=os.environ.get("EV_VOLVO_TARGET_VIN_SHA256"),
                host=os.environ.get("EVCC_HOST", ""),
                port=int(os.environ.get("EVCC_PORT", "4196")))
    except Exception as exc:
        # Do not print secrets/endpoints; an uncertain write is NOT a success.
        result = {"ready": False, "error_type": type(exc).__name__,
                  "physical_state_uncertain": args.physical, "device_writes": None}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if result.get("ready") is not True:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
