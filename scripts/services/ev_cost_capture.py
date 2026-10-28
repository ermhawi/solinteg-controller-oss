#!/usr/bin/env python3
"""Read-only capture of Volvo journal distance/energy evidence from Home Assistant.

Stores only source time intervals, distance and reported consumption in a private
SQLite database. NO VIN, GPS, trip IDs, charging commands or inferred cost.
Reported journal consumption is NOT verified net battery DC depletion; never
promote these rows automatically into ev_cost_ledger.drive events.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

UTC = timezone.utc
SOURCE = "volvo_trip_energy"
MEASUREMENT = "reported_electrical_consumption_not_net_battery_dc"
ENDPOINT = "/api/volvo_trip_energy/cost_segments"
MAX_RESPONSE_BYTES = 512 * 1024
MAX_SEGMENTS = 2400
MAX_HISTORY_MS = 30 * 86400 * 1000
MAX_RESPONSE_AGE_MS = 5 * 60 * 1000
MAX_JOURNAL_AGE_MS = 90 * 60 * 1000
MAX_SEGMENT_DURATION_MS = 5 * 3600 * 1000
MAX_DISTANCE_M = 12000
MAX_KWH_PER_10_KM = Decimal("15")
SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS ev_cost_drive_evidence (
    evidence_id TEXT PRIMARY KEY,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    distance_m TEXT NOT NULL,
    reported_consumption_kwh TEXT NOT NULL,
    received_at_utc TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ev_cost_drive_evidence_interval_idx
ON ev_cost_drive_evidence(start_ms, end_ms);
"""


class EvidenceError(ValueError):
    """Safe error: no secrets, URL, GPS, VIN or source content."""


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvidenceError(f"{label}: expected numeric JSON value")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise EvidenceError(f"{label}: invalid decimal") from exc
    if not result.is_finite() or result < 0:
        raise EvidenceError(f"{label}: invalid value")
    return result


def _utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise EvidenceError("missing UTC source time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError("invalid source time") from exc
    if parsed.tzinfo is None:
        raise EvidenceError("source time requires timezone")
    return parsed.astimezone(UTC)


def _record(raw: dict, *, now_ms: int) -> dict:
    if (not isinstance(raw, dict)
            or set(raw) != {"start_ms", "end_ms", "distance_m", "kwh"}):
        raise EvidenceError("unexpected journal segment fields")
    start = raw["start_ms"]
    end = raw["end_ms"]
    if (type(start) is not int or type(end) is not int
            or not now_ms - MAX_HISTORY_MS <= start < end <= now_ms + 60000
            or end - start > MAX_SEGMENT_DURATION_MS):
        raise EvidenceError("journal segment has invalid time range")
    distance = _decimal(raw["distance_m"], "distance_m")
    kwh = _decimal(raw["kwh"], "kwh")
    if not 0 < distance <= MAX_DISTANCE_M or kwh > MAX_KWH_PER_10_KM * distance / 10000:
        raise EvidenceError("journal segment exceeds plausible distance/energy")
    return {
        "evidence_id": "volvo:" + hashlib.sha256(
            f"{start}:{end}".encode("ascii")).hexdigest()[:32],
        "start_ms": start,
        "end_ms": end,
        "distance_m": format(distance.normalize(), "f"),
        "reported_consumption_kwh": format(kwh.normalize(), "f"),
    }


def validate(payload: object, *, now: datetime | None = None) -> list[dict]:
    """Accept complete, recent direct journal intervals; never price them."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise EvidenceError("current time requires timezone")
    now_ms = int(now.astimezone(UTC).timestamp() * 1000)
    if not isinstance(payload, dict) or set(payload) != {
            "schema_version", "source", "measurement_kind", "data_status",
            "last_success_at_utc", "generated_at_ms", "segments"}:
        raise EvidenceError("unexpected journal evidence payload")
    if (type(payload["schema_version"]) is not int
            or payload["schema_version"] != 1
            or payload["source"] != SOURCE
            or payload["measurement_kind"] != MEASUREMENT):
        raise EvidenceError("unknown journal schema or energy semantics")
    if payload["data_status"] != "ok":
        raise EvidenceError("journal incomplete or stale; no capture")
    generated = payload["generated_at_ms"]
    if type(generated) is not int or not -5000 <= now_ms - generated <= MAX_RESPONSE_AGE_MS:
        raise EvidenceError("journal response too old or from future")
    source_age_ms = (now - _utc(payload["last_success_at_utc"])).total_seconds() * 1000
    if not -5000 <= source_age_ms <= MAX_JOURNAL_AGE_MS:
        raise EvidenceError("journal source last success too old")
    raw_segments = payload["segments"]
    if not isinstance(raw_segments, list) or len(raw_segments) > MAX_SEGMENTS:
        raise EvidenceError("journal segment list unavailable or too large")
    unique: dict[tuple[int, int], dict] = {}
    for raw in raw_segments:
        row = _record(raw, now_ms=now_ms)
        key = (row["start_ms"], row["end_ms"])
        if key in unique and unique[key] != row:
            raise EvidenceError("conflicting journal evidence for one interval")
        unique[key] = row
    records = sorted(unique.values(), key=lambda x: (x["start_ms"], x["end_ms"]))
    for previous, current in zip(records, records[1:]):
        if previous["end_ms"] > current["start_ms"]:
            raise EvidenceError("overlapping journal intervals may double count energy")
    return records


def _private_url(base: str) -> str:
    parts = urlsplit(base)
    if (parts.scheme not in ("http", "https") or not parts.hostname
            or parts.username or parts.password or parts.path not in ("", "/")
            or parts.query or parts.fragment):
        raise EvidenceError("invalid private HA base URL")
    try:
        addr = ipaddress.ip_address(parts.hostname)
        allowed = addr.version == 4 and any(
            addr in ipaddress.ip_network(net)
            for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
    except ValueError:
        allowed = False
    if not allowed:
        raise EvidenceError("HA URL must use a private IPv4 address")
    return base.rstrip("/") + ENDPOINT


def fetch(*, url: str, token: str) -> object:
    """Single bounded GET to a private HA API. Never redirect or use a proxy."""
    target = _private_url(url)
    if not isinstance(token, str) or len(token) < 20:
        raise EvidenceError("private HA read token unavailable")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            raise EvidenceError("HA redirect refused")

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(
        target, headers={"Authorization": "Bearer " + token,
                         "Accept": "application/json"}, method="GET")
    try:
        with opener.open(request, timeout=6) as response:
            if response.status != 200:
                raise EvidenceError("HA cost evidence API unavailable")
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise EvidenceError("HA cost evidence GET failed") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise EvidenceError("HA cost evidence payload too large")
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise EvidenceError("HA cost evidence JSON invalid") from exc


def _token() -> str:
    path = os.environ.get("HA_READONLY_TOKEN_FILE")
    if path:
        with open(path, encoding="utf-8") as source:
            return source.read().strip()
    return os.environ.get("HA_READONLY_TOKEN", "").strip()


def connect(path: str) -> sqlite3.Connection:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    db = sqlite3.connect(str(target), timeout=3)
    try:
        db.execute("PRAGMA busy_timeout=3000")
        db.execute("PRAGMA journal_mode=WAL")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise EvidenceError("newer journal evidence DB schema")
        db.executescript(SCHEMA)
        if version < SCHEMA_VERSION:
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        db.commit()
        return db
    except BaseException:
        db.close()
        raise


def append(path: str, rows: list[dict]) -> int:
    """One atomic, idempotent batch; an amended interval is never overwritten."""
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        inserted = 0
        for row in rows:
            previous = db.execute(
                "SELECT distance_m,reported_consumption_kwh "
                "FROM ev_cost_drive_evidence WHERE evidence_id=?",
                (row["evidence_id"],)).fetchone()
            if previous:
                if previous != (row["distance_m"], row["reported_consumption_kwh"]):
                    raise EvidenceError("existing journal interval has revised measurements")
                continue
            conflict = db.execute(
                "SELECT 1 FROM ev_cost_drive_evidence "
                "WHERE start_ms < ? AND end_ms > ? LIMIT 1",
                (row["end_ms"], row["start_ms"]),
            ).fetchone()
            if conflict:
                raise EvidenceError("overlapping journal interval across captures")
            db.execute(
                "INSERT INTO ev_cost_drive_evidence VALUES (?,?,?,?,?,?)",
                (row["evidence_id"], row["start_ms"], row["end_ms"],
                 row["distance_m"], row["reported_consumption_kwh"],
                 datetime.now(UTC).isoformat()))
            inserted += 1
        db.commit()
        return inserted


def overview(rows: list[dict], *, inserted: int | None = None) -> dict:
    distance = sum(Decimal(r["distance_m"]) for r in rows)
    consumption = sum(Decimal(r["reported_consumption_kwh"]) for r in rows)
    return {
        "source": SOURCE,
        "measurement_kind": MEASUREMENT,
        "segments": len(rows),
        "distance_km": float(distance / 1000),
        "reported_consumption_kwh": float(consumption),
        "evidence_inserted": inserted,
        "kr_per_mil": None,  # Requires cost ledger + net DC validation.
        "cost_quality": "unavailable",
        "device_writes": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="saved, sanitized HA source JSON (offline)")
    src.add_argument("--ha", action="store_true", help="one private read-only HA GET")
    parser.add_argument("--capture", action="store_true",
                        help="append validated evidence to separate SQLite")
    parser.add_argument("--db", default=os.environ.get(
        "EV_COST_EVIDENCE_DB_PATH",
        "/opt/solinteg/ev-shadow-runtime/var/ev-cost-evidence.sqlite"))
    args = parser.parse_args()
    try:
        if args.ha:
            if args.capture and os.environ.get("EV_COST_CAPTURE_ENABLED") != "1":
                raise EvidenceError("HA capture disabled by default")
            payload = fetch(url=os.environ.get("HA_API_URL", ""), token=_token())
        else:
            if args.file == "-":
                # A sudo service user may inherit a readable pipe but cannot
                # reopen /dev/stdin owned by the caller's UID.
                raw = sys.stdin.buffer.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise EvidenceError("source JSON stream too large")
                payload = json.loads(raw)
            else:
                if Path(args.file).stat().st_size > MAX_RESPONSE_BYTES:
                    raise EvidenceError("source JSON file too large")
                with open(args.file, encoding="utf-8") as inp:
                    payload = json.load(inp)
        rows = validate(payload)
        inserted = append(args.db, rows) if args.capture else None
        print(json.dumps(overview(rows, inserted=inserted), ensure_ascii=False))
    except (OSError, EvidenceError, ValueError) as exc:
        # Never log secrets, raw requests, URLs, VIN, trip identifiers or GPS.
        print(json.dumps({"status": "unavailable", "reason": (
            str(exc) if isinstance(exc, EvidenceError) else type(exc).__name__),
            "device_writes": 0}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
