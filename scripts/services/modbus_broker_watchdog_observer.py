#!/usr/bin/env python3
"""Portable, observe-only Modbus-broker watchdog for a separate systemd timer.

Checks local dispatcher and poller timestamps, and optionally the broker's
read-only Unix socket. It has NO Modbus client, service-control operation,
privileged fallback or inverter write path. A Dell outage cannot be detected
by this Dell-local observer; an off-host supervisor is still required.
"""
from __future__ import annotations

import json
import logging
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("solinteg.broker_observer")
UTC = timezone.utc
DISPATCH_PATH = "/opt/solinteg/dispatch-heartbeat.json"
POLLER_PATH = "/opt/solinteg/live.json"
BROKER_SOCKET = "/run/solinteg-broker/broker.sock"
STATE_PATH = "/var/lib/solinteg-broker-observer/status.json"
MAX_IPC_BYTES = 16384


def read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as stream:
            result = json.load(stream)
        return result if isinstance(result, dict) else None
    except (OSError, ValueError):
        return None


def age_seconds(data: dict | None, now: datetime, limit_s: float) -> float | None:
    """Return age only for a timezone-aware timestamp within the stated limit."""
    try:
        observed = datetime.fromisoformat(data["timestamp"])
        if observed.utcoffset() is None or now.utcoffset() is None:
            return None
        age = (now - observed).total_seconds()
        return age if 0 <= age <= limit_s else None
    except (TypeError, ValueError, KeyError, OverflowError, AttributeError):
        return None


def broker_status(socket_path: str, timeout_s: float = 2.0) -> dict | None:
    """Only 'status' via AF_UNIX, never a snapshot or a control transaction."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(timeout_s)
            peer.connect(socket_path)
            peer.sendall(b'{"operation":"status","timeout_s":2}\n')
            data = bytearray()
            while not data.endswith(b"\n"):
                chunk = peer.recv(1024)
                if not chunk or len(data) + len(chunk) > MAX_IPC_BYTES:
                    return None
                data.extend(chunk)
        envelope = json.loads(data)
        if envelope.get("ok") is not True or not isinstance(envelope.get("result"), dict):
            return None
        return envelope["result"]
    except (OSError, ValueError, TypeError):
        return None


def assess(
    now: datetime, dispatcher: dict | None, poller: dict | None,
    *, broker_required: bool, broker: dict | None = None,
    dispatch_stale_s: float = 180, poller_stale_s: float = 120,
) -> tuple[str, tuple[str, ...]]:
    """Classify observability, never authorize a physical fallback."""
    problems = []
    if age_seconds(dispatcher, now, dispatch_stale_s) is None:
        problems.append("dispatch_heartbeat_missing_stale_or_invalid")
    if age_seconds(poller, now, poller_stale_s) is None:
        problems.append("poller_telemetry_missing_stale_or_invalid")

    if isinstance(dispatcher, dict):
        if dispatcher.get("supervisor_required") is True or dispatcher.get("broker_policy") == "inhibit":
            problems.append("dispatcher_requires_supervisor")
        elif dispatcher.get("supervisor_required") not in (None, False):
            problems.append("dispatcher_safety_status_invalid")
        if dispatcher.get("broker_policy") not in (None, "plan_candidate", "inhibit"):
            problems.append("dispatcher_broker_policy_unknown")

    if broker_required:
        if not isinstance(broker, dict):
            problems.append("broker_ipc_unavailable")
        else:
            if broker.get("read_only") is not True or broker.get("armed") is not False:
                problems.append("broker_not_verified_read_only")
            if broker.get("safety_latched") is not False:
                problems.append("broker_safety_status_unverified")
            sample = {"timestamp": broker.get("last_valid_timestamp")}
            if age_seconds(sample, now, poller_stale_s) is None:
                problems.append("broker_sample_missing_stale_or_invalid")
            if not isinstance(poller, dict) or poller.get("sample_quality") != "valid":
                problems.append("broker_poller_sample_unverified")

    return ("fault" if problems else "healthy"), tuple(problems)


def write_json_atomic(path: str, payload: dict) -> None:
    target = Path(path)
    temporary = target.with_name(target.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, separators=(",", ":"))
        stream.write("\n")
    os.replace(temporary, target)


def observe(now: datetime, environ=None) -> tuple[str, tuple[str, ...]]:
    env = os.environ if environ is None else environ
    required = env.get("BROKER_OBSERVER_REQUIRE_BROKER", "0")
    if required not in ("0", "1"):
        raise ValueError("BROKER_OBSERVER_REQUIRE_BROKER must be 0 or 1")
    if env.get("BROKER_OBSERVER_MODE", "observe") != "observe":
        raise ValueError("only observe mode is implemented")

    heartbeat = read_json(env.get("BROKER_OBSERVER_DISPATCH_PATH", DISPATCH_PATH))
    poller = read_json(env.get("BROKER_OBSERVER_POLLER_PATH", POLLER_PATH))
    broker = None
    if required == "1":
        broker = broker_status(env.get("BROKER_OBSERVER_SOCKET", BROKER_SOCKET))
    return assess(now, heartbeat, poller, broker_required=required == "1", broker=broker)


def run_once(now: datetime | None = None, environ=None) -> int:
    env = os.environ if environ is None else environ
    now = now or datetime.now(UTC)
    result, reasons = observe(now, env)
    state_path = env.get("BROKER_OBSERVER_STATE_PATH", STATE_PATH)
    previous = read_json(state_path) or {}
    previous_reasons = previous.get("reasons")
    previous_key = (
        previous.get("status"),
        tuple(previous_reasons) if isinstance(previous_reasons, list) else (),
    )
    current_key = (result, reasons)
    changed = current_key != previous_key
    reminder_due = (result == "fault" and age_seconds(
        {"timestamp": previous.get("last_report_at")}, now, 900) is None)
    reported_at = previous.get("last_report_at")

    # Log state changes immediately, then repeat unresolved faults every 15 min.
    if changed or reminder_due:
        message = "broker observer %s: %s", result, ", ".join(reasons) or "dispatch and poller fresh"
        if result == "fault":
            log.warning(*message)
        else:
            log.info(*message)
        reported_at = now.isoformat()

    if changed or age_seconds(previous, now, 60) is None:
        write_json_atomic(state_path, {
            "timestamp": now.isoformat(), "status": result,
            "reasons": list(reasons), "mode": "observe",
            "broker_required": env.get("BROKER_OBSERVER_REQUIRE_BROKER", "0") == "1",
            "physical_writes": False, "last_report_at": reported_at,
        })
    return 0  # Faults are observations, not systemd restarts or actuator approval.


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.umask(0o077)
    return run_once()


if __name__ == "__main__":
    raise SystemExit(main())
