#!/usr/bin/env python3
"""Read-only, gap-aware EV charging AC energy ESTIMATE from ABL EVCC FC03 JSON.

NO Modbus/network access, no charging controls and no meter claims.
We use the previously observed *active* current channel Ict2, NOT the
observer's three-channel sum (which includes ~0.1 A noise on Ict1/Ict3).
Voltage (230 V) and power factor (1.0) are ASSUMED, never measured.
Each added kWh covers only two adjacent valid C2 observations. Missing
or failed reads and any longer gap contribute UNKNOWN, not zero.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

UTC = timezone.utc
SCHEMA_VERSION = 1
SOURCE = "waveshare_evcc_fc03_readonly"
VOLTAGE = Decimal("230")
POWER_FACTOR = Decimal("1.0")
MAX_AGE_S = 90
MAX_GAP_MS = 45_000
MAX_FILE_BYTES = 64 * 1024
ZERO = Decimal("0")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ev_ac_observations (
  observed_epoch_ms INTEGER PRIMARY KEY,
  payload_json TEXT NOT NULL,
  recorded_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ev_ac_estimated_intervals (
  start_epoch_ms INTEGER PRIMARY KEY,
  end_epoch_ms INTEGER NOT NULL UNIQUE,
  current_start_a TEXT NOT NULL,
  current_end_a TEXT NOT NULL,
  voltage_assumed_v TEXT NOT NULL,
  power_factor_assumed TEXT NOT NULL,
  estimated_ac_kwh TEXT NOT NULL,
  quality TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ev_ac_interval_end_idx
ON ev_ac_estimated_intervals(end_epoch_ms);
"""


class AcEvidenceError(ValueError):
    """Safe diagnostic; never include raw HTTP responses or private config."""


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise AcEvidenceError("missing UTC observation time")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AcEvidenceError("invalid observation timestamp") from exc
    if result.tzinfo is None:
        raise AcEvidenceError("observation timestamp missing timezone")
    return result.astimezone(UTC)


def _num(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (float, int, str, Decimal)):
        return None
    try:
        result = Decimal(str(value))
    except Exception:
        return None
    return result if result.is_finite() and ZERO <= result <= Decimal("32") else None


def parse(data: object, *, now: datetime | None = None) -> dict:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    if not isinstance(data, dict) or data.get("source") != SOURCE:
        raise AcEvidenceError("unverified EVCC observation source")
    if data.get("writes") != 0 or data.get("physical_control_eligible") is not False:
        raise AcEvidenceError("source not certified read-only")
    t = _time(data.get("timestamp"))
    age = (now - t).total_seconds()
    if not -5 <= age <= MAX_AGE_S:
        raise AcEvidenceError("EVCC observation stale/future")
    stamp = int(t.timestamp() * 1000)

    quality = "unavailable"
    active = None
    channels = None
    config = pilot = None
    state = None
    if data.get("read_ok") is True:
        state = data.get("state")
        config, pilot = data.get("configured_pwm_raw"), data.get("pilot_pwm_raw")
        raw = data.get("channel_currents_a")
        if isinstance(raw, (list, tuple)) and len(raw) == 3:
            channels = [_num(x) for x in raw]
        if state == "C2" and channels is not None and all(x is not None for x in channels):
            c1, c2, c3 = channels
            reported_total = _num(data.get("measured_current_a"))
            if (c1 <= Decimal("0.5") and c3 <= Decimal("0.5")
                    and Decimal("1") <= c2 <= Decimal("7")
                    and reported_total is not None
                    and abs(reported_total - sum(channels)) <= Decimal("0.2")
                    and data.get("charging_detected") is True):
                active = c2
                quality = ("estimated" if type(config) is int and type(pilot) is int
                           and config == pilot == 100 else "unverified_current_limit")
            else:
                quality = "invalid_current_channels"
        elif state != "C2":
            quality = "not_charging"
        else:
            quality = "unavailable"
    elif data.get("read_ok") is not False:
        raise AcEvidenceError("EVCC read_ok missing or invalid")

    # Privacy-preserving, source-validated projection: no IP, VIN, raw frames,
    # inverter write controls, API credentials or hardware identifiers.
    return {
        "observed_epoch_ms": stamp,
        "quality": quality,
        "state": state if state in ("A1", "B1", "B2", "C1", "C2", "D1", "D2") else None,
        "active_channel": "Ict2",
        "active_current_a": str(active) if active is not None else None,
        "other_channels_a": ([str(channels[0]), str(channels[2])]
                             if channels is not None and all(x is not None for x in channels)
                             else None),
        "configured_pwm_raw": config if type(config) is int else None,
        "pilot_pwm_raw": pilot if type(pilot) is int else None,
        "source_writes": 0,
    }


def connect(path: str) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    c = sqlite3.connect(str(p), timeout=4)
    try:
        c.execute("PRAGMA busy_timeout=4000")
        c.execute("PRAGMA journal_mode=WAL")
        version = c.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise AcEvidenceError("unsupported later AC evidence schema")
        c.executescript(SCHEMA)
        if version < SCHEMA_VERSION:
            c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        c.commit()
        return c
    except BaseException:
        c.close()
        raise


def store(dbpath: str, record: dict) -> tuple[bool, bool]:
    """Deduplicate fresh snapshots and insert *only* bounded, adjacent valid AC intervals."""
    blob = json.dumps(record, sort_keys=True, separators=(",", ":"))
    with connect(dbpath) as db:
        db.execute("BEGIN IMMEDIATE")
        epoch = record["observed_epoch_ms"]
        existing = db.execute(
            "SELECT payload_json FROM ev_ac_observations WHERE observed_epoch_ms=?",
            (epoch,)).fetchone()
        if existing is not None:
            if existing[0] != blob:
                raise AcEvidenceError("conflicting immutable EVCC source snapshot")
            return False, False
        previous = db.execute(
            "SELECT observed_epoch_ms,payload_json FROM ev_ac_observations "
            "ORDER BY observed_epoch_ms DESC LIMIT 1").fetchone()
        if previous is not None and epoch <= previous[0]:
            raise AcEvidenceError("nonmonotonic EVCC observation")
        db.execute(
            "INSERT INTO ev_ac_observations VALUES (?,?,?)",
            (epoch, blob, datetime.now(UTC).isoformat()))
        interval_added = False
        if previous is not None:
            start, prev_blob = previous
            old = json.loads(prev_blob)
            duration_ms = epoch - start
            if (0 < duration_ms <= MAX_GAP_MS
                    and old.get("quality") == record.get("quality") == "estimated"
                    and old.get("active_current_a") is not None
                    and record.get("active_current_a") is not None):
                a = Decimal(old["active_current_a"])
                b = Decimal(record["active_current_a"])
                kwh = (((a + b) / 2) * VOLTAGE * POWER_FACTOR
                       * Decimal(duration_ms) / Decimal("3600000000"))
                db.execute(
                    "INSERT INTO ev_ac_estimated_intervals VALUES (?,?,?,?,?,?,?,?)",
                    (start, epoch, str(a), str(b), str(VOLTAGE),
                     str(POWER_FACTOR), str(kwh), "estimated_voltage_pf"))
                interval_added = True
        db.commit()
        return True, interval_added


def summary(dbpath: str) -> dict:
    path = Path(dbpath)
    if not path.exists():
        return {
            "source": "evcc_ict2_ac_estimate", "quality": "unavailable",
            "observations": 0, "covered_intervals": 0,
            "covered_seconds": 0, "estimated_ac_kwh_covered_only": None,
            "cost_ore": None, "device_writes": 0,
        }
    from urllib.parse import quote
    uri = "file:" + quote(str(path.resolve()), safe="/") + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=3) as db:
        n = db.execute("SELECT COUNT(*) FROM ev_ac_observations").fetchone()[0]
        intervals = db.execute(
            "SELECT start_epoch_ms,end_epoch_ms,estimated_ac_kwh "
            "FROM ev_ac_estimated_intervals ORDER BY start_epoch_ms").fetchall()
        current = db.execute(
            "SELECT payload_json FROM ev_ac_observations "
            "ORDER BY observed_epoch_ms DESC LIMIT 1").fetchone()
    amount = sum((Decimal(kwh) for _, _, kwh in intervals), ZERO)
    return {
        "source": "evcc_ict2_ac_estimate",
        "quality": "estimated" if intervals else "unavailable",
        "assumed_voltage_v": float(VOLTAGE),
        "assumed_power_factor": float(POWER_FACTOR),
        "observations": n,
        "covered_intervals": len(intervals),
        "covered_seconds": round(sum((end - start)/1000 for start,end,_ in intervals),2),
        "estimated_ac_kwh_covered_only": (
            float(amount.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))
            if intervals else None),
        "last_observation": json.loads(current[0]) if current else None,
        "cost_ore": None,
        "vehicle_battery_dc_kwh": None,
        "device_writes": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", default=os.environ.get(
        "EVCC_READONLY_PATH", "/opt/solinteg/evcc-readonly.json"))
    parser.add_argument("--db", default=os.environ.get(
        "EV_AC_EVIDENCE_DB_PATH",
        "/opt/solinteg/ev-shadow-runtime/var/ev-ac-estimate.sqlite"))
    parser.add_argument("--capture", action="store_true",
                        help="append evidence only, gated by EV_AC_CAPTURE_ENABLED=1")
    parser.add_argument("--summary", action="store_true",
                        help="print read-only coverage, energy and quality")
    args = parser.parse_args()
    if args.summary:
        print(json.dumps(summary(args.db)))
        return
    if args.capture and os.environ.get("EV_AC_CAPTURE_ENABLED") != "1":
        raise SystemExit("AC evidence capture disabled by default")
    try:
        p = Path(args.snapshot)
        if p.stat().st_size > MAX_FILE_BYTES:
            raise AcEvidenceError("EVCC source snapshot too large")
        with p.open(encoding="utf-8") as inp:
            snapshot = json.load(inp)
        record = parse(snapshot)
    except (OSError, AcEvidenceError, ValueError) as exc:
        # Missing/stale input must be an explicit gap, NOT a zero-current measurement.
        print(json.dumps({"quality": "unavailable",
                          "reason": str(exc) if isinstance(exc, AcEvidenceError)
                          else type(exc).__name__, "device_writes": 0}))
        raise SystemExit(0 if isinstance(exc, AcEvidenceError)
                         and "stale/future" in str(exc) else 1)
    if args.capture:
        recorded, interval_added = store(args.db, record)
    else:
        recorded = interval_added = False
    print(json.dumps({"quality": record["quality"],
                      "active_current_a": record["active_current_a"],
                      "recorded": recorded, "interval_added": interval_added,
                      "device_writes": 0}))


if __name__ == "__main__":
    main()
