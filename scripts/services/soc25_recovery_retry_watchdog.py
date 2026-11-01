#!/usr/bin/env python3
"""Independent read-only supervisor for the one-off 26->28% retry.

All inverter writes remain inside the unmodified installed dispatch loop.
The root timer rolls back only its own SHA-pinned drop-in. The previous
aborted reserve trial and its evidence stay untouched.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT_LIB = Path("/usr/local/libexec/solinteg-soc25-recovery-retry")
# At install time this module is imported from the checkout; the immutable
# deployment copy imports its own pinned base watchdog from this root path.
if ROOT_LIB.is_dir():
    sys.path.insert(0, str(ROOT_LIB))
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import soc25_reserve_watchdog as base
from soc25_reserve_policy import RECOVERY_EXIT_PCT

ID = "soc25-recovery-retry-20261101"
STATE = Path("/var/lib/solinteg-soc25-recovery-retry/status.json")
DROPIN = Path("/etc/systemd/system/solinteg-dispatch.service.d/49-soc25-recovery-retry.conf")
WRAPPER = ROOT_LIB / "soc25_recovery_retry_dispatch.py"
POLICY = ROOT_LIB / "soc25_reserve_policy.py"
TIMER = "solinteg-soc25-recovery-retry-watchdog.timer"
DEADLINE_TIMER = "solinteg-soc25-recovery-retry-deadline.timer"
MARKER = "# solinteg-soc25-recovery-retry-20261101 (do not edit)"
MIN_CHARGE_W = 100
MIN_DISTINCT_SAMPLES = 2
MIN_SAMPLE_SEPARATION_S = 20


def configure() -> None:
    base.ID = ID
    base.STATE = STATE
    base.DROPIN = DROPIN
    base.ROOT_LIB = ROOT_LIB
    base.WRAPPER = WRAPPER
    base.POLICY = POLICY
    base.TIMER = TIMER
    base.DEADLINE_TIMER = DEADLINE_TIMER
    base.MARKER = MARKER
    base.configure_common()


def _fresh_distinct_charge(live: dict, status: dict) -> bool:
    """Two real, time-separated 30s telemetry samples prove charge, not command."""
    if (live.get("work_mode_raw") != 0x303
            or type(live.get("battery_w")) not in (int, float)
            or not -1900 <= live["battery_w"] <= -MIN_CHARGE_W
            or type(live.get("soc_pct")) not in (int, float)
            or not 24 <= live["soc_pct"] < RECOVERY_EXIT_PCT):
        return False
    timestamp = base.common._stamp(live["timestamp"])
    previous = status.get("charge_last_sample_utc")
    if previous is not None:
        elapsed = (timestamp - base.common._stamp(previous)).total_seconds()
        if elapsed < MIN_SAMPLE_SEPARATION_S:
            return False
    status["charge_last_sample_utc"] = timestamp.isoformat()
    status["charge_samples"] = int(status.get("charge_samples", 0)) + 1
    status["charge_last_w"] = round(live["battery_w"])
    return True


def tick(*, stop: bool = False) -> dict:
    configure()
    STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The 15s timer and independent deadline must never race on status
    # or restart the dispatch service twice.
    with (STATE.parent / "supervisor.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if STATE.exists():
            status = base.common._load(STATE)
            if status.get("state") in ("prepared", "running"):
                try:
                    source_sha = base.common._checksum(ROOT_LIB / "soc25_reserve_dispatch.py")
                except OSError:
                    source_sha = None
                if source_sha != status.get("base_wrapper_sha256"):
                    return base._restore_owned(
                        status, reason="pinned underlying dispatcher wrapper changed",
                        completed=False)
        result = base.tick(stop=stop)
        if result.get("state") != "running":
            return result
        status = base.common._load(STATE)
        live = base.common._load(base.common.LIVE)
        now = datetime.now(timezone.utc)
        if not base._age_ok(live, now):
            return base._restore_owned(status, reason="retry completion telemetry stale")
        new_charge_sample = _fresh_distinct_charge(live, status)
        if status.get("charge_samples", 0) and new_charge_sample:
            status["physical_charge_verified"] = status["charge_samples"] >= MIN_DISTINCT_SAMPLES
        if live.get("soc_pct", 0) >= RECOVERY_EXIT_PCT:
            if (status.get("charge_samples", 0) >= MIN_DISTINCT_SAMPLES
                    and status.get("initial_soc_pct", 100) <= 26):
                status["completion_soc_pct"] = live["soc_pct"]
                status["completion_observed_at_utc"] = now.isoformat()
                base.common._atomic_json(STATE, status)
                return base._restore_owned(
                    status, reason="verified physical 26-to-28% recharge",
                    completed=True)
            return base._restore_owned(
                status, reason="28% reached without two independent charging samples",
                completed=False)
        base.common._atomic_json(STATE, status)
        result["physical_charge_samples"] = status.get("charge_samples", 0)
        return result


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    cmd = p.add_mutually_exclusive_group(required=True)
    cmd.add_argument("--tick", action="store_true")
    cmd.add_argument("--stop", action="store_true")
    cmd.add_argument("--status", action="store_true")
    a = p.parse_args()
    if a.status:
        status = base.common._load(STATE) if STATE.exists() else {"state":"not_installed"}
        print(json.dumps({key:status.get(key) for key in (
            "state","pilot_id","started_at_utc","expires_at_utc",
            "checked_at_utc","check_reason","reason","measurements",
            "charge_samples","physical_charge_verified",
            "completion_soc_pct","completion_observed_at_utc",
            "restored_at_utc")},ensure_ascii=False))
    else:
        print(json.dumps(tick(stop=a.stop),ensure_ascii=False))


if __name__ == "__main__":
    main()
