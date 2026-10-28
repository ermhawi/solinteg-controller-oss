#!/usr/bin/env python3
"""Read-only import of historical day-ahead price *evidence* from Solinteg.

The source price_snapshots table contains local Stockholm prices and was
upserted by the house optimizer. Its buy price EXCLUDES grid tax/transfer,
and tariff revisions/discounts are not independently verified. These rows
MUST NEVER be represented as invoiced XC40 charging costs without separate
reconciliation, AC metering and source allocation. No inverter/EVCC imports.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

UTC = timezone.utc
STOCKHOLM = ZoneInfo("Europe/Stockholm")
SLOT = timedelta(minutes=15)
MAX_PRICE_JSON_BYTES = 256 * 1024
SCHEMA_VERSION = 1
QUALITY = "modelled_day_ahead_excludes_grid_and_unreconciled"
SCHEMA = """
CREATE TABLE IF NOT EXISTS ev_cost_price_evidence (
    slot_start_epoch_ms INTEGER PRIMARY KEY,
    slot_end_epoch_ms INTEGER NOT NULL,
    source_date TEXT NOT NULL,
    source_logged_at_utc TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    buy_before_grid_ore TEXT NOT NULL,
    sell_model_ore TEXT NOT NULL,
    quality TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ev_cost_price_date_idx
    ON ev_cost_price_evidence(source_date);
"""


class PriceEvidenceError(ValueError):
    """Fail-closed without logging raw telemetry, contracts or tariff secrets."""


def _number(value: object, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PriceEvidenceError(f"{label}: must be a JSON number")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise PriceEvidenceError(f"{label}: invalid number") from exc
    if not result.is_finite() or not Decimal("-500") <= result <= Decimal("3000"):
        raise PriceEvidenceError(f"{label}: impossible price")
    return format(result.normalize(), "f")


def _instant(value: object) -> datetime:
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PriceEvidenceError("invalid source snapshot timestamp") from exc
    if timestamp.tzinfo is None:
        raise PriceEvidenceError("source snapshot timestamp must include timezone")
    return timestamp.astimezone(UTC)


def _local_start(raw: object, *, day: date) -> datetime:
    if not isinstance(raw, str):
        raise PriceEvidenceError("missing local start time")
    try:
        local = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise PriceEvidenceError("invalid local start time") from exc
    if local.tzinfo is not None or local.date() != day:
        raise PriceEvidenceError("price slot has unexpected date/offset")
    return local


def normalize_day(day_string: str, logged_at: str, raw_json: str) -> list[dict]:
    """Accept only one full actual-source price day; map repeated 02:xx by occurrence."""
    try:
        day = date.fromisoformat(day_string)
    except ValueError as exc:
        raise PriceEvidenceError("invalid source date") from exc
    logged = _instant(logged_at).isoformat()
    if not isinstance(raw_json, str) or len(raw_json.encode("utf-8")) > MAX_PRICE_JSON_BYTES:
        raise PriceEvidenceError("price source snapshot missing or too large")
    try:
        source = json.loads(raw_json)
    except (ValueError, TypeError) as exc:
        raise PriceEvidenceError("invalid price JSON") from exc
    if not isinstance(source, list):
        raise PriceEvidenceError("price source needs a slot array")
    actual = []
    for slot in source:
        if not isinstance(slot, dict):
            raise PriceEvidenceError("invalid price slot")
        raw_start = slot.get("startTime")
        if isinstance(raw_start, str) and raw_start.startswith(day_string):
            if slot.get("source") != "actual":
                continue  # A forecast is never a historical settled price.
            actual.append(slot)
    midnight = datetime.combine(day, time(), tzinfo=STOCKHOLM).astimezone(UTC)
    next_midnight = datetime.combine(day + timedelta(days=1), time(),
                                     tzinfo=STOCKHOLM).astimezone(UTC)
    expected = int((next_midnight - midnight) / SLOT)
    if len(actual) != expected:
        raise PriceEvidenceError("source has missing/non-actual 15-minute slots")
    observed_names = {}
    normalized = []
    last_start = midnight - SLOT
    source_hash = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
    for slot in actual:
        local = _local_start(slot.get("startTime"), day=day)
        name = slot["startTime"]
        occurrence = observed_names.get(name, 0)
        if occurrence > 1:
            raise PriceEvidenceError("more than two local price clock repetitions")
        candidate0 = local.replace(tzinfo=STOCKHOLM, fold=0)
        candidate1 = local.replace(tzinfo=STOCKHOLM, fold=1)
        ambiguous = candidate0.utcoffset() != candidate1.utcoffset()
        if not ambiguous and occurrence:
            raise PriceEvidenceError("duplicate non-DST local price slot")
        chosen = local.replace(tzinfo=STOCKHOLM, fold=occurrence if ambiguous else 0)
        start = chosen.astimezone(UTC)
        if start.astimezone(STOCKHOLM).replace(tzinfo=None) != local:
            raise PriceEvidenceError("nonexistent local price time")
        if start - last_start != SLOT:
            raise PriceEvidenceError("unsorted/gapped/ambiguous UTC price slots")
        end = start + SLOT
        if slot.get("endTime") != end.astimezone(STOCKHOLM).strftime("%Y-%m-%dT%H:%M:%S"):
            raise PriceEvidenceError("price end time does not match UTC slot")
        observed_names[name] = occurrence + 1
        last_start = start
        normalized.append({
            "slot_start_epoch_ms": int(start.timestamp() * 1000),
            "slot_end_epoch_ms": int(end.timestamp() * 1000),
            "source_date": day_string,
            "source_logged_at_utc": logged,
            "source_sha256": source_hash,
            "buy_before_grid_ore": _number(slot.get("priceIncludingTaxAndSurcharge"),
                                            "buy_before_grid_ore"),
            "sell_model_ore": _number(slot.get("price"), "sell_model_ore"),
            "quality": QUALITY,
        })
    if last_start + SLOT != next_midnight:
        raise PriceEvidenceError("price day has incorrect UTC terminal boundary")
    return normalized


def historical_slots(source_path: str, *, today: date, days: int = 30) -> tuple[list[dict], list[str]]:
    """Read historical daily price snapshots in SQLite read-only mode."""
    if not 1 <= days <= 90:
        raise PriceEvidenceError("days must be 1..90")
    path = Path(source_path)
    if not path.exists():
        raise PriceEvidenceError("source price SQLite unavailable")
    uri = "file:" + quote(str(path.resolve()), safe="/") + "?mode=ro"
    start = today - timedelta(days=days - 1)
    with closing(sqlite3.connect(uri, uri=True, timeout=3)) as db:
        rows = db.execute(
            "SELECT date,logged_at,prices_json FROM price_snapshots "
            "WHERE date >= ? AND date <= ? ORDER BY date",
            (start.isoformat(), today.isoformat())).fetchall()
    source_by_date = {row[0]: row for row in rows}
    accepted = []
    missing = []
    for offset in range(days):
        current = start + timedelta(days=offset)
        row = source_by_date.get(current.isoformat())
        if row is None:
            missing.append(current.isoformat())
            continue
        try:
            accepted.extend(normalize_day(*row))
        except (PriceEvidenceError, TypeError):
            missing.append(current.isoformat())
    return accepted, missing


def connect(path: str) -> sqlite3.Connection:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    db = sqlite3.connect(path, timeout=3)
    try:
        db.execute("PRAGMA busy_timeout=3000")
        db.execute("PRAGMA journal_mode=WAL")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise PriceEvidenceError("newer price evidence schema")
        db.executescript(SCHEMA)
        if version < SCHEMA_VERSION:
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        db.commit()
        return db
    except BaseException:
        db.close()
        raise


def append(path: str, slots: list[dict]) -> int:
    """Never rewrite a historical price, even if a later upstream snapshot changed."""
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        inserted = 0
        for row in slots:
            prior = db.execute(
                "SELECT buy_before_grid_ore,sell_model_ore FROM ev_cost_price_evidence "
                "WHERE slot_start_epoch_ms=?", (row["slot_start_epoch_ms"],)
            ).fetchone()
            if prior is not None:
                if prior != (row["buy_before_grid_ore"], row["sell_model_ore"]):
                    raise PriceEvidenceError("historical price changed; explicit revision required")
                continue
            db.execute(
                "INSERT INTO ev_cost_price_evidence VALUES (?,?,?,?,?,?,?,?)",
                (row["slot_start_epoch_ms"], row["slot_end_epoch_ms"],
                 row["source_date"], row["source_logged_at_utc"],
                 row["source_sha256"], row["buy_before_grid_ore"],
                 row["sell_model_ore"], row["quality"]),
            )
            inserted += 1
        db.commit()
        return inserted


def summary(slots: list[dict], missing: list[str], *, inserted: int | None = None) -> dict:
    return {
        "source": "solinteg_price_snapshots_readonly",
        "price_quality": QUALITY,
        "complete_days": len({row["source_date"] for row in slots}),
        "missing_or_invalid_days": len(missing),
        "actual_price_slots": len(slots),
        "evidence_inserted": inserted,
        "first_day": min((row["source_date"] for row in slots), default=None),
        "last_day": max((row["source_date"] for row in slots), default=None),
        "invoiced_buy_price_ore": None,
        "vehicle_cost_ore": None,
        "device_writes": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=os.environ.get(
        "TELEMETRY_DB_PATH", "/opt/solinteg/telemetry.db"))
    parser.add_argument("--db", default=os.environ.get(
        "EV_COST_PRICE_EVIDENCE_DB_PATH",
        "/opt/solinteg/ev-shadow-runtime/var/ev-cost-price-evidence.sqlite"))
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--capture", action="store_true")
    args = parser.parse_args()
    try:
        if args.capture and os.environ.get("EV_COST_PRICE_CAPTURE_ENABLED") != "1":
            raise PriceEvidenceError("price evidence capture disabled by default")
        today = datetime.now(STOCKHOLM).date()
        slots, missing = historical_slots(args.source, today=today, days=args.days)
        if not slots:
            raise PriceEvidenceError("no full actual-price days available")
        inserted = append(args.db, slots) if args.capture else None
        print(json.dumps(summary(slots, missing, inserted=inserted), ensure_ascii=False))
    except (sqlite3.Error, OSError, ValueError) as exc:
        print(json.dumps({"status": "unavailable", "reason": (
            str(exc) if isinstance(exc, PriceEvidenceError) else type(exc).__name__),
            "device_writes": 0}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
