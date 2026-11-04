"""Durable, fail-closed ownership of EVCC pauses made by the price controller.

This is NOT a manual-pause detector. A separate, lossless manual-intent and
connection-epoch producer is required before automatic resume can be armed.
The producer must serialize manual actions with the controller's owner lock.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

SCHEMA = 1
MANUAL_SOURCE = "verified_manual_intent_audit"
MAX_MANUAL_AGE_S = 90
MAX_OWNERSHIP_AGE_S = 24 * 3600
STATES = frozenset(("pending", "owned", "releasing", "released", "revoked"))


class OwnershipError(ValueError):
    """Unverifiable owner or manual-intent record: never issue an EVCC write."""


def _instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise OwnershipError("missing timestamp")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OwnershipError("invalid timestamp") from exc
    if stamp.tzinfo is None:
        raise OwnershipError("timestamp without timezone")
    return stamp.astimezone(timezone.utc)


def _uuid(value: object) -> str:
    if not isinstance(value, str):
        raise OwnershipError("missing session identity")
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise OwnershipError("invalid session identity") from exc


def _epoch(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise OwnershipError("invalid event generation")
    return value


def read_manual_intent(path: str | None, *, now: datetime) -> dict | None:
    """Only a trusted lossless source may clear the manual-pause interlock.

    A current Volvo/HA *charging status* or FC03 PWM alone is insufficient:
    neither can distinguish a manual stop while the EVCC is already paused.
    """
    if not path or now.tzinfo is None:
        return None
    try:
        with open(path, encoding="utf-8") as stream:
            item = json.load(stream)
        if (not isinstance(item, dict) or item.get("schema_version") != SCHEMA
                or item.get("source") != MANUAL_SOURCE
                or item.get("lossless") is not True
                or type(item.get("manual_paused")) is not bool):
            raise OwnershipError("manual intent source is not independently verified")
        observed = _instant(item["observed_at_utc"])
        continuous = _instant(item["continuous_since_utc"])
        if (not 0 <= (now - observed).total_seconds() <= MAX_MANUAL_AGE_S
                or continuous > observed):
            raise OwnershipError("manual intent coverage is stale or incomplete")
        return {
            "session_id": _uuid(item["session_id"]),
            "connection_epoch": _epoch(item["connection_epoch"]),
            "manual_generation": _epoch(item["manual_generation"]),
            "manual_paused": item["manual_paused"],
            "observed_at_utc": observed,
            "continuous_since_utc": continuous,
        }
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise OwnershipError("manual intent input invalid") from exc


def read_owner(path: Path) -> dict | None:
    try:
        with path.open(encoding="utf-8") as stream:
            record = json.load(stream)
        if (not isinstance(record, dict) or record.get("schema_version") != SCHEMA
                or record.get("state") not in STATES
                or record.get("charge_mode") not in ("economy", "deadline")):
            raise OwnershipError("unrecognized price-pause owner state")
        _uuid(record["session_id"])
        _uuid(record["owner_id"])
        _epoch(record["connection_epoch"])
        _epoch(record["manual_generation"])
        _instant(record["created_at_utc"])
        return record
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise OwnershipError("price-pause owner record invalid") from exc


def is_owned(record: dict | None, *, session: dict, manual: dict | None,
             now: datetime) -> bool:
    if not record or record["state"] != "owned" or manual is None:
        return False
    created = _instant(record["created_at_utc"])
    return (
        record["session_id"] == session.get("session_id") == manual["session_id"]
        and record["charge_mode"] == session.get("charge_mode")
        and record["connection_epoch"] == session.get("connection_epoch") == manual["connection_epoch"]
        and record["manual_generation"] == manual["manual_generation"]
        and manual["manual_paused"] is False
        and 0 <= (now - created).total_seconds() <= MAX_OWNERSHIP_AGE_S
        and manual["continuous_since_utc"] <= created <= manual["observed_at_utc"]
    )


def new_owner(*, session: dict, manual: dict, now: datetime, owner_id: str) -> dict:
    if not (session.get("session_id") == manual["session_id"]
            and session.get("connection_epoch") == manual["connection_epoch"]
            and manual["manual_paused"] is False
            and manual["continuous_since_utc"] <= now):
        raise OwnershipError("manual pause or connection identity mismatch")
    return {
        "schema_version": SCHEMA,
        "owner_id": _uuid(owner_id),
        "session_id": _uuid(session["session_id"]),
        "connection_epoch": _epoch(manual["connection_epoch"]),
        "manual_generation": _epoch(manual["manual_generation"]),
        "charge_mode": session["charge_mode"],
        "created_at_utc": now.astimezone(timezone.utc).isoformat(),
        "state": "pending",
    }


def write_owner(path: Path, record: dict) -> None:
    """Atomic 0600 replacement, including fsync, before/after FC10."""
    if not path.parent.is_dir():
        raise OwnershipError("price-pause state directory unavailable")
    fd, name = tempfile.mkstemp(prefix=".ev-price-owner-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            os.fchmod(out.fileno(), 0o600)
            json.dump(record, out, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)
