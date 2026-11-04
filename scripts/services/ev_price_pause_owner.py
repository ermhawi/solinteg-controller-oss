"""Durable ownership of EVCC price pauses issued and verified by this controller.

The Volvo Cars app has no home-charge Stop control for this installation.
No synthetic lossless manual-intent source is required or asserted.
Only a successfully acknowledged + physically read-back FC10 pause may
be resumed, and only for the identical verified session and cable epoch.
Polling cannot guarantee detection of a brief unplug/replug between reads.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

SCHEMA = 2
SOURCE = "evcc_price_controller_verified_fc10"
MAX_OWNERSHIP_AGE_S = 24 * 3600
MAX_CABLE_AGE_S = 90
STATES = frozenset(("pending", "owned", "releasing", "released", "revoked"))


class OwnershipError(ValueError):
    """Unverifiable pause ownership: never issue an EVCC command."""


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
    if type(value) is not int or not 0 <= value <= 2**63 - 2:
        raise OwnershipError("invalid event generation")
    return value


def _validate(record: dict) -> None:
    if (not isinstance(record, dict)
            or type(record.get("schema_version")) is not int
            or record["schema_version"] != SCHEMA
            or record.get("source") != SOURCE
            or record.get("state") not in STATES
            or record.get("charge_mode") not in ("economy", "deadline")
            or type(record.get("verified_fc10_pause")) is not bool):
        raise OwnershipError("unrecognized price-pause owner state")
    _uuid(record.get("session_id"))
    _uuid(record.get("owner_id"))
    _epoch(record.get("connection_epoch"))
    _epoch(record.get("evcc_cable_epoch"))
    _epoch(record.get("cable_coverage_epoch"))
    _uuid(record.get("cable_tracker_id"))
    _instant(record.get("created_at_utc"))
    if (record["state"] == "owned" and record["verified_fc10_pause"] is not True
            or record["state"] == "pending" and record["verified_fc10_pause"] is not False):
        raise OwnershipError("unverified FC10 pause provenance")


def _cable_valid(cable: dict | None, *, now: datetime) -> bool:
    if not isinstance(cable, dict) or now.tzinfo is None:
        return False
    try:
        if (cable.get("source") != "waveshare_evcc_fc03_polled_connection"
                or cable.get("status") != "connected"
                or cable.get("observed_state") not in ("B1", "B2", "C1", "C2")
                or cable.get("candidate_status") is not None
                or cable.get("lossless") is not False
                or cable.get("physical_control_eligible") is not False
                or cable.get("device_writes") != 0):
            return False
        _epoch(cable.get("connection_epoch"))
        _epoch(cable.get("coverage_epoch"))
        _uuid(cable.get("tracking_id"))
        age = (now - _instant(cable.get("last_sample_at_utc"))).total_seconds()
        return 0 <= age <= MAX_CABLE_AGE_S
    except (ValueError, KeyError, TypeError):
        return False


def read_owner(path: Path) -> dict | None:
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077 or info.st_size > 8192
                or info.st_uid != os.geteuid()):
            raise OwnershipError("insecure persisted pause owner")
        with path.open(encoding="utf-8") as stream:
            record = json.load(stream)
        _validate(record)
        return record
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise OwnershipError("price-pause owner record invalid") from exc


def is_owned(record: dict | None, *, session: dict, cable: dict | None,
             now: datetime) -> bool:
    if record is None or record.get("state") != "owned" or not _cable_valid(cable, now=now):
        return False
    try:
        _validate(record)
        created = _instant(record["created_at_utc"])
        return (
            record["session_id"] == _uuid(session.get("session_id"))
            and record["charge_mode"] == session.get("charge_mode")
            and record["connection_epoch"] == _epoch(session.get("connection_epoch"))
            and record["evcc_cable_epoch"] == cable["connection_epoch"]
            and record["cable_coverage_epoch"] == cable["coverage_epoch"]
            and record["cable_tracker_id"] == cable["tracking_id"]
            and 0 <= (now - created).total_seconds() <= MAX_OWNERSHIP_AGE_S
        )
    except (ValueError, KeyError, TypeError):
        return False


def new_owner(*, session: dict, cable: dict | None, now: datetime,
              owner_id: str) -> dict:
    if not _cable_valid(cable, now=now):
        raise OwnershipError("unverified cable session")
    if session.get("charge_mode") not in ("economy", "deadline"):
        raise OwnershipError("unsupported charge mode")
    record = {
        "schema_version": SCHEMA,
        "source": SOURCE,
        "owner_id": _uuid(owner_id),
        "session_id": _uuid(session["session_id"]),
        "connection_epoch": _epoch(session["connection_epoch"]),
        "evcc_cable_epoch": _epoch(cable["connection_epoch"]),
        "cable_coverage_epoch": _epoch(cable["coverage_epoch"]),
        "cable_tracker_id": _uuid(cable["tracking_id"]),
        "charge_mode": session["charge_mode"],
        "created_at_utc": now.astimezone(timezone.utc).isoformat(),
        "verified_fc10_pause": False,
        "state": "pending",
    }
    _validate(record)
    return record


def write_owner(path: Path, record: dict) -> None:
    """Atomic private state transition before/after FC10; never blind retry."""
    _validate(record)
    if not path.parent.is_dir() or path.is_symlink():
        raise OwnershipError("price-pause state directory unavailable or unsafe")
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
