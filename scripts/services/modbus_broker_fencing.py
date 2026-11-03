"""Pure fail-closed broker fencing protocol for a future emergency fallback.

This module performs NO OS actions or Modbus I/O. Production integration must
supply an independently privileged supervisor and independently verified
broker PID, socket and TCP ownership. Do NOT wire into live systemd by default.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable


class FenceFailure(RuntimeError):
    """There is no proof that the broker has relinquished its hardware session."""


@dataclass(frozen=True)
class FenceObservation:
    broker_alive: bool | None
    broker_tcp_sessions: int | None
    other_tcp_sessions: int | None
    broker_socket_listening: bool | None
    checked_at: float

    def exclusive_release_verified(self) -> bool:
        return (
            self.broker_alive is False
            and self.broker_tcp_sessions == 0
            and self.other_tcp_sessions == 0
            and self.broker_socket_listening is False
        )


@dataclass(frozen=True)
class FenceResult:
    stopped: bool
    released: bool
    emergency_attempted: bool
    auto_readback_verified: bool
    error: str | None


def fenced_emergency_auto(
    *,
    stop_broker: Callable[[], bool],
    observe: Callable[[], FenceObservation],
    emergency_auto: Callable[[], bool],
    alert: Callable[[str], None],
    before_write: Callable[[], bool] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    deadline_s: float = 10.0,
) -> FenceResult:
    """Stop, re-observe and ONLY THEN invoke one emergency Auto attempt.

    The observer MUST independently check OS process/socket/TCP ownership.
    Its result cannot come from the broker IPC endpoint being fenced.
    No exceptions from lifecycle/alert handlers may silently authorize a write.
    """
    def notify(message: str):
        try:
            alert(message)
        except Exception:
            pass

    if deadline_s <= 0 or deadline_s > 120:
        raise ValueError("invalid deadline")
    began = monotonic()
    try:
        stopped = stop_broker() is True
    except Exception as exc:
        notify("broker fencing failed: supervisor error")
        return FenceResult(False, False, False, False, type(exc).__name__)
    if not stopped:
        notify("broker fencing failed: stop not confirmed")
        return FenceResult(False, False, False, False, "stop_unconfirmed")
    try:
        state = observe()
        if not isinstance(state, FenceObservation):
            raise TypeError("independent observation required")
        if monotonic() - began > deadline_s:
            raise FenceFailure("fencing deadline exceeded")
        if not state.exclusive_release_verified():
            raise FenceFailure("broker or other Modbus owner still present")
        # Re-observe at handover to reduce the check/use window. A privileged
        # service mask and port policy are still REQUIRED to prevent restart
        # after observation (this in-memory gate alone is not sufficient).
        confirmation = observe()
        if not isinstance(confirmation, FenceObservation) or not confirmation.exclusive_release_verified():
            raise FenceFailure("TCP ownership changed before emergency")
        if monotonic() - began > deadline_s:
            raise FenceFailure("fencing deadline exceeded before emergency")
    except Exception as exc:
        notify("broker fencing failed: no exclusive Modbus release proof")
        return FenceResult(True, False, False, False, type(exc).__name__)
    try:
        if before_write is None:
            notify("emergency Auto blocked: no final safety authorization gate")
            return FenceResult(True, True, False, False, "write_guard_missing")
        if before_write() is not True:
            notify("emergency Auto blocked: final ownership or safety authorization denied")
            return FenceResult(True, True, False, False, "write_guard_denied")
    except Exception as exc:
        notify("emergency Auto blocked: authorization unverified")
        return FenceResult(True, True, False, False, type(exc).__name__)
    # A slow independent SoC/BMS read must not silently extend the fenced
    # handover deadline; recheck immediately before any physical command.
    if monotonic() - began > deadline_s:
        notify("emergency Auto blocked: deadline expired during safety authorization")
        return FenceResult(True, True, False, False, "deadline_exceeded_before_write")
    try:
        verified = emergency_auto() is True
    except Exception as exc:
        notify("emergency Auto failed: readback unknown")
        return FenceResult(True, True, True, False, type(exc).__name__)
    if not verified:
        notify("emergency Auto failed: readback unverified")
        return FenceResult(True, True, True, False, "auto_unverified")
    return FenceResult(True, True, True, True, None)
