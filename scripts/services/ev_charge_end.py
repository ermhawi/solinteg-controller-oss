"""Persist confirmed EVCC charge-session endings from the existing historian.

No device/network access. The cable tracker confirms unplugging; a distinct
recent Volvo API observation supplies the last reported SoC, NOT a BMS sample.
Older samples are immutable even if the car's SoC changes after departure.
"""
from __future__ import annotations

import json
import math
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import evcc_cable_tracker as cable_tracker

UTC = timezone.utc
SOURCE = "evcc_confirmed_disconnect_volvo_api_soc"
MAX_CABLE_AGE_S = 90
MAX_SOC_AGE_S = 300
MAX_CHARGE_LOOKBACK_H = 24

SCHEMA = """
CREATE TABLE IF NOT EXISTS ev_charge_ends (
    confirmed_disconnected_at_utc TEXT PRIMARY KEY,
    last_charging_at_utc TEXT NOT NULL,
    soc_pct REAL,
    soc_api_received_at_utc TEXT,
    soc_oem_last_changed_at_utc TEXT
);
"""


def _instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("timestamp lacks UTC offset")
    return stamp.astimezone(UTC)


def _confirmed_unplug(path: Path, now: datetime) -> datetime | None:
    try:
        record = cable_tracker.read(path)
        if (record is None or record["status"] != "disconnected"
                or record.get("observed_state") not in ("A1", "A2")
                or record.get("candidate_status") is not None
                or record.get("last_transition") not in (None, "disconnected")
                or record.get("last_transition_at_utc") != record.get("last_disconnected_at_utc")):
            return None
        seen = _instant(record["last_sample_at_utc"])
        ended = _instant(record["last_disconnected_at_utc"])
        if (not 0 <= (now - seen).total_seconds() <= MAX_CABLE_AGE_S
                or not 0 <= (now - ended).total_seconds() <= 30 * 86400
                or ended > seen):
            return None
        return ended
    except (OSError, KeyError, TypeError, ValueError, cable_tracker.CableObservationError):
        return None


def _latest(con: sqlite3.Connection) -> dict | None:
    row = con.execute(
        "SELECT confirmed_disconnected_at_utc, last_charging_at_utc, soc_pct, "
        "soc_api_received_at_utc, soc_oem_last_changed_at_utc "
        "FROM ev_charge_ends ORDER BY confirmed_disconnected_at_utc DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    end, charging, soc, api, changed = row
    return {
        "source": SOURCE,
        "quality": "polled_cable_and_api_reported_soc_not_bms",
        "confirmedAtUtc": end,
        "lastChargingAtUtc": charging,
        "socPct": soc,
        "socApiReceivedAtUtc": api,
        "socOemLastChangedAtUtc": changed,
    }


def _record(con: sqlite3.Connection, ended: datetime) -> None:
    stamp = ended.isoformat()
    ms = int(ended.timestamp() * 1000)
    charging = con.execute(
        "SELECT evcc_at_utc FROM ev_samples "
        "WHERE captured_epoch_ms BETWEEN ? AND ? "
        "AND evcc_record_fresh = 1 AND evcc_read_ok = 1 "
        "AND evcc_state = 'C2' AND evcc_charging_detected = 1 "
        "AND evcc_ict2_a BETWEEN 5.5 AND 6.5 "
        "ORDER BY captured_epoch_ms DESC LIMIT 1",
        (ms - MAX_CHARGE_LOOKBACK_H * 3600 * 1000, ms),
    ).fetchone()
    if charging is None:
        return  # A disconnected idle car is not a completed charge session.
    charged_at = _instant(charging[0])
    if not 0 <= (ended - charged_at).total_seconds() <= MAX_CHARGE_LOOKBACK_H * 3600:
        return

    soc = None
    api_at = None
    oem_at = None
    candidate = con.execute(
        "SELECT volvo_soc_pct, volvo_api_received_at_utc, "
        "volvo_battery_last_changed_utc "
        "FROM ev_samples WHERE captured_epoch_ms BETWEEN ? AND ? "
        "AND ha_record_fresh = 1 AND volvo_observation_usable = 1 "
        "AND volvo_connected = 1 AND volvo_soc_pct BETWEEN 0 AND 100 "
        "AND volvo_quality IN (?, ?) "
        "ORDER BY captured_epoch_ms DESC LIMIT 1",
        (ms - MAX_SOC_AGE_S * 1000, ms,
         "api_recent_field_change_unverified",
         "api_recent_fields_changed_recently"),
    ).fetchone()
    if candidate is not None:
        try:
            value, api, changed = candidate
            api_stamp = _instant(api)
            oem_stamp = _instant(changed)
            if (isinstance(value, (float, int)) and not isinstance(value, bool)
                    and math.isfinite(value)
                    and 0 <= (ended - api_stamp).total_seconds() <= MAX_SOC_AGE_S
                    and oem_stamp <= api_stamp):
                soc, api_at, oem_at = float(value), api_stamp.isoformat(), oem_stamp.isoformat()
        except (ValueError, TypeError):
            pass

    con.execute(
        "INSERT OR IGNORE INTO ev_charge_ends VALUES (?, ?, ?, ?, ?)",
        (stamp, charged_at.isoformat(), soc, api_at, oem_at),
    )


def record_and_publish(db_path: str, cable_path: str, output_path: str,
                       *, now: datetime | None = None) -> dict | None:
    """Idempotent, durable SQLite ending and sanitized 0640 latest-event export."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    ended = _confirmed_unplug(Path(cable_path), now)
    with sqlite3.connect(db_path, timeout=3) as con:
        con.execute("PRAGMA busy_timeout=3000")
        con.executescript(SCHEMA)
        if ended is not None:
            _record(con, ended)
            con.commit()
        latest = _latest(con)
    if latest is None:
        return None

    target = Path(output_path)
    if target.is_symlink() or not target.parent.is_dir():
        raise OSError("invalid charge-end export path")
    content = (json.dumps(latest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if target.is_file() and target.read_bytes() == content:
        return latest
    temp = str(target) + "." + secrets.token_hex(6) + ".tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(content)
            os.fchmod(out.fileno(), 0o640)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, target)
        dir_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return latest
