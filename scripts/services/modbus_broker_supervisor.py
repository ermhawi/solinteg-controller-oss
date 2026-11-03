"""Offline-only orchestration of a future privileged Modbus emergency fallback.

No systemctl, filesystem mutation, TCP or Modbus implementation is provided.
The approved production supervisor must supply independent, privilege-checked
callbacks. This module MUST NOT be used by the current production watchdog.

Required order: revoke broker approval -> verify absence -> stop broker ->
acquire the SAME exclusive flock -> independently observe the host twice ->
verify approval still absent AND independently authorize General ->
attempt Auto ONCE with confirmed readback -> release the flock.
Missing authorization fails closed; no fallback implementation is installed.
A failure NEVER restores approval or retries writes.
"""
from __future__ import annotations

import time
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from typing import Callable

from modbus_broker_emergency_policy import GeneralFallbackEvidence, review_general_fallback
from modbus_broker_fencing import (
    FenceFailure,
    FenceObservation,
    FenceResult,
    fenced_emergency_auto,
)


def coordinated_emergency_auto(
    *,
    revoke_approval: Callable[[], bool],
    approval_absent: Callable[[], bool],
    stop_broker: Callable[[], bool],
    ownership_lease: Callable[[], AbstractContextManager],
    observe: Callable[[], FenceObservation],
    emergency_auto: Callable[[], bool],
    alert: Callable[[str], None],
    read_independent_evidence: Callable[[], GeneralFallbackEvidence] | None = None,
    now_utc: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    monotonic: Callable[[], float] = time.monotonic,
    deadline_s: float = 10.0,
) -> FenceResult:
    """Run the existing fenced safety procedure while holding the owner lock.

    The supervisor must not consider the return value of revoke_approval proof
    of removal: approval_absent must independently inspect its protected path.
    Production implementations must inhibit restart even after this function
    returns (the approval marker remains revoked until a separate manual go).
    read_independent_evidence must supply verified post-fence SoC and native
    protection through a separately trusted source. This coordinator performs
    the conservative policy check itself. No callback means NO intervention.
    """
    if not 0 < deadline_s <= 120:
        raise ValueError("invalid emergency deadline")
    started = monotonic()

    def notify(reason: str) -> None:
        try:
            alert(reason)
        except Exception:  # noqa: BLE001 - alert failure may not authorize writes
            pass

    def denied(stopped: bool, reason: str) -> FenceResult:
        notify("emergency takeover blocked: " + reason)
        return FenceResult(stopped, False, False, False, reason)

    if read_independent_evidence is None:
        return denied(False, "independent_evidence_source_missing")

    try:
        if revoke_approval() is not True:
            return denied(False, "approval_not_revoked")
        if approval_absent() is not True:
            return denied(False, "approval_still_present")
    except Exception as exc:  # noqa: BLE001 - unknown marker state is unsafe
        return denied(False, type(exc).__name__)

    if monotonic() - started >= deadline_s:
        return denied(False, "deadline_exceeded")

    try:
        stopped = stop_broker() is True
    except Exception as exc:  # noqa: BLE001
        return denied(False, type(exc).__name__)
    if not stopped:
        return denied(False, "broker_stop_unconfirmed")

    result = None
    try:
        # This must be the same stable root:solinteg /run inode used by the
        # broker; the supplied lease must use nonblocking LOCK_EX (fail closed).
        with ownership_lease():
            if approval_absent() is not True:
                return denied(True, "approval_restored_before_observation")
            remaining = deadline_s - (monotonic() - started)
            if remaining <= 0:
                return denied(True, "deadline_exceeded")

            def guarded_observe() -> FenceObservation:
                if approval_absent() is not True:
                    raise FenceFailure("broker approval restored during fencing")
                return observe()

            def final_write_guard() -> bool:
                # Check independently after fencing, while holding the lease.
                # The verified native floor must preserve the configured
                # reserve: neither the broker nor legacy live.json is evidence.
                if approval_absent() is not True:
                    return False
                fenced_at = now_utc()
                evidence = read_independent_evidence()
                review = review_general_fallback(
                    evidence, now=now_utc(), fence_started_at=fenced_at,
                )
                return review.candidate

            result = fenced_emergency_auto(
                stop_broker=lambda: True,  # already stopped BEFORE taking lease
                observe=guarded_observe,
                emergency_auto=emergency_auto,
                before_write=final_write_guard,
                alert=alert,
                monotonic=monotonic,
                deadline_s=remaining,
            )
    except Exception as exc:  # noqa: BLE001 - includes busy/invalid flock
        if result is not None:
            notify("emergency takeover lock release failed")
            return FenceResult(
                result.stopped, result.released, result.emergency_attempted,
                result.auto_readback_verified, type(exc).__name__,
            )
        return denied(True, type(exc).__name__)

    return result
