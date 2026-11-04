"""Debounced, durable EVCC cable presence from the EXISTING FC03 observer.

A1/A2 means no vehicle; B1/B2/C1/C2 means pilot sees a connected car.
The state transition is *observed*, not a guaranteed physical event timestamp
or proof of who unplugged. Polling can miss a short unplug/replug sequence.
No network/Modbus/car/inverter calls, no physical write permission, and this
source MUST NOT be used as proof that the Volvo app was not manually paused.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 1
SOURCE = "waveshare_evcc_fc03_polled_connection"
OBSERVER_SOURCE = "waveshare_evcc_fc03_readonly"
MAX_SAMPLE_AGE_S = 90
MAX_OBSERVATION_GAP_S = 100
CONFIRM_SAMPLES = 2
DISCONNECTED = frozenset(("A1", "A2"))
CONNECTED = frozenset(("B1", "B2", "C1", "C2"))


class CableObservationError(ValueError):
    """Invalid or unrecoverable cable evidence. No source-based permission."""


def _instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise CableObservationError("observation timestamp missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CableObservationError("observation timestamp invalid") from exc
    if parsed.tzinfo is None:
        raise CableObservationError("observation timestamp lacks timezone")
    return parsed.astimezone(timezone.utc)


def _status(snapshot: dict, *, now: datetime) -> tuple[str, datetime]:
    if (not isinstance(snapshot, dict) or snapshot.get("source") != OBSERVER_SOURCE
            or snapshot.get("writes") != 0
            or snapshot.get("physical_control_eligible") is not False):
        raise CableObservationError("untrusted EVCC FC03 source")
    stamp = _instant(snapshot.get("timestamp"))
    if not 0 <= (now - stamp).total_seconds() <= MAX_SAMPLE_AGE_S:
        raise CableObservationError("stale or future FC03 observation")
    cp = snapshot.get("state")
    if snapshot.get("read_ok") is not True:
        return "unknown", stamp
    if cp in DISCONNECTED:
        return "disconnected", stamp
    if cp in CONNECTED:
        return "connected", stamp
    return "unknown", stamp


def _initial() -> dict:
    return {
        "schema_version": SCHEMA,
        "source": SOURCE,
        "status": "unknown",
        "observed_state": "unknown",
        "connection_epoch": 0,
        "confirmed_replug": False,
        "candidate_status": None,
        "candidate_samples": 0,
        "last_sample_at_utc": None,
        "last_disconnected_at_utc": None,
        "last_reconnected_at_utc": None,
        "last_transition": None,
        "last_transition_at_utc": None,
        "signal_quality": "debounced_polled_not_lossless",
        "lossless": False,
        "physical_control_eligible": False,
        "device_writes": 0,
    }


def _check_prior(prior: dict) -> None:
    if (not isinstance(prior, dict) or prior.get("schema_version") != SCHEMA
            or prior.get("source") != SOURCE
            or prior.get("status") not in ("unknown", "connected", "disconnected")
            or prior.get("candidate_status") not in (None, "connected", "disconnected")
            or type(prior.get("candidate_samples")) is not int
            or not 0 <= prior["candidate_samples"] <= CONFIRM_SAMPLES
            or type(prior.get("connection_epoch")) is not int
            or not 0 <= prior["connection_epoch"] <= 2**63 - 2
            or type(prior.get("confirmed_replug")) is not bool
            or prior.get("physical_control_eligible") is not False
            or prior.get("lossless") is not False
            or prior.get("device_writes") != 0):
        raise CableObservationError("invalid persisted cable state")
    if prior["last_sample_at_utc"] is not None:
        _instant(prior["last_sample_at_utc"])


def advance(prior: dict | None, snapshot: dict, *, now: datetime) -> dict:
    """Pure transition logic; monotonic epochs only on confirmed A->B/C.

    After lost polling continuity, require a NEW confirmed disconnected state
    before any subsequent connected observation can count as a replug.
    """
    if now.tzinfo is None:
        raise CableObservationError("now lacks timezone")
    state, stamp = _status(snapshot, now=now)
    previous = dict(prior) if prior is not None else _initial()
    _check_prior(previous)
    last = previous["last_sample_at_utc"]
    if last is not None:
        age = (stamp - _instant(last)).total_seconds()
        if age <= 0:
            # Idempotent on duplicate or out-of-order polling.
            return previous
        if age > MAX_OBSERVATION_GAP_S:
            previous.update(status="unknown", confirmed_replug=False,
                            candidate_status=None, candidate_samples=0)
    previous["last_sample_at_utc"] = stamp.isoformat()
    previous["observed_state"] = snapshot.get("state") if state != "unknown" else "unknown"
    previous["last_transition"] = None
    if state == "unknown":
        previous.update(status="unknown", confirmed_replug=False,
                        candidate_status=None, candidate_samples=0)
        return previous

    if state == previous["status"]:
        previous["candidate_status"] = None
        previous["candidate_samples"] = 0
        return previous
    if state == previous["candidate_status"]:
        previous["candidate_samples"] += 1
    else:
        previous["candidate_status"] = state
        previous["candidate_samples"] = 1

    if previous["candidate_samples"] < CONFIRM_SAMPLES:
        return previous

    was = previous["status"]
    previous["status"] = state
    previous["candidate_status"] = None
    previous["candidate_samples"] = 0
    if state == "disconnected":
        previous["confirmed_replug"] = False
        previous["last_disconnected_at_utc"] = stamp.isoformat()
        if was == "connected":
            previous["last_transition"] = "disconnected"
            previous["last_transition_at_utc"] = stamp.isoformat()
    elif was == "disconnected":
        previous["connection_epoch"] += 1
        previous["confirmed_replug"] = True
        previous["last_reconnected_at_utc"] = stamp.isoformat()
        previous["last_transition"] = "reconnected"
        previous["last_transition_at_utc"] = stamp.isoformat()
    else:
        previous["confirmed_replug"] = False
    return previous


def read(path: Path) -> dict | None:
    try:
        if path.stat().st_size > 64 * 1024:
            raise CableObservationError("oversized cable state")
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, TypeError, ValueError) as exc:
        raise CableObservationError("unreadable cable state") from exc
    _check_prior(record)
    return record


def read_fresh_connected(path: Path, *, now: datetime) -> dict | None:
    """Supplementary guard: does not establish absence of app-level pause."""
    record = read(path)
    if (record is None or record["status"] != "connected"
            or record["candidate_status"] is not None
            or record["observed_state"] not in CONNECTED):
        return None
    if not 0 <= (now - _instant(record["last_sample_at_utc"])).total_seconds() <= MAX_SAMPLE_AGE_S:
        return None
    return record


def store(path: Path, data: dict) -> None:
    """Atomic private 0600 file replacement; never edits hardware state."""
    _check_prior(data)
    if not path.parent.is_dir():
        raise CableObservationError("cable state directory unavailable")
    fd, name = tempfile.mkstemp(prefix=".evcc-cable-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(data, output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        dirfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def update(path: Path, snapshot: dict, *, now: datetime) -> dict:
    result = advance(read(path), snapshot, now=now)
    store(path, result)
    return result
