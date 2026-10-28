#!/usr/bin/env python3
"""Separate, append-only XC40 energy-cost ledger; never accesses vehicles or hardware.

Input events must come from verified/meticulously labeled sources. The 30-second
EV historian alone cannot establish bought/solar kWh, true battery DC gain, or
driven distance. No synthetic price, efficiency, or mileage is inferred here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from uuid import uuid4

UTC = timezone.utc
ZERO = Decimal("0")
EPS = Decimal("0.000000001")
SCHEMA_VERSION = 1
QUALITIES = frozenset(("metered", "estimated"))
SOURCES = frozenset(("grid", "solar", "public", "unknown"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS ev_cost_events (
    event_id TEXT PRIMARY KEY,
    occurred_at_utc TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    recorded_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ev_cost_events_time_idx
    ON ev_cost_events(occurred_at_utc, event_id);
"""


def number(value, name: str, *, minimum: Decimal | None = ZERO) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError(f"{name}: expected decimal number")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{name}: invalid decimal number") from exc
    if not result.is_finite() or (minimum is not None and result < minimum):
        raise ValueError(f"{name}: out of range")
    if abs(result) > Decimal("1000000000"):
        raise ValueError(f"{name}: implausible magnitude")
    return result


def instant(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("UTC timestamp required")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid timestamp") from exc
    if dt.tzinfo is None:
        raise ValueError("timezone required (DST-safe)")
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def quality(value: object) -> str:
    if value not in QUALITIES:
        raise ValueError("quality must be metered or estimated")
    return str(value)


def validate(event: dict) -> dict:
    """Canonical event with unique id; never extrapolate missing costs."""
    if not isinstance(event, dict):
        raise ValueError("event must be an object")
    name = event.get("event_id")
    kind = event.get("type")
    if not isinstance(name, str) or not 1 <= len(name) <= 100 or not all(
            c.isascii() and (c.isalnum() or c in "._:-") for c in name):
        raise ValueError("event_id must be a stable ASCII identifier")
    fields = {
        "opening": {"stored_dc_kwh", "inventory_value_ore"},
        "charge": {"stored_dc_kwh", "direct_aux_ac_kwh", "ac_sources", "tariff_version"},
        "drive": {"distance_km", "consumed_dc_kwh", "started_at_utc"},
        "aux": {"consumed_dc_kwh", "reason"},
        "adjust": {"delta_dc_kwh", "reason"},
    }
    if kind not in fields:
        raise ValueError("unknown event type")
    unexpected = set(event) - (fields[kind] | {
        "event_id", "type", "occurred_at_utc", "quality"})
    if unexpected:
        raise ValueError("unexpected event fields (do not store personal data)")
    result = dict(event)
    result["occurred_at_utc"] = instant(event.get("occurred_at_utc"))
    result["quality"] = quality(event.get("quality"))
    if kind == "opening":
        result["stored_dc_kwh"] = str(number(event.get("stored_dc_kwh"), "stored_dc_kwh"))
        v = event.get("inventory_value_ore")
        result["inventory_value_ore"] = (
            str(number(v, "inventory_value_ore", minimum=None)) if v is not None else None)
        if (number(result["stored_dc_kwh"], "stored_dc_kwh") == ZERO
                and result["inventory_value_ore"] is not None
                and number(result["inventory_value_ore"], "inventory_value_ore",
                           minimum=None) != ZERO):
            raise ValueError("empty battery inventory must have zero known value")
    elif kind == "charge":
        amount = number(event.get("stored_dc_kwh"), "stored_dc_kwh")
        result["stored_dc_kwh"] = str(amount)
        aux = number(event.get("direct_aux_ac_kwh", 0), "direct_aux_ac_kwh")
        result["direct_aux_ac_kwh"] = str(aux)
        sources = event.get("ac_sources")
        if not isinstance(sources, list) or not sources or len(sources) > 12:
            raise ValueError("charge requires ac_sources")
        cleaned = []
        ac = ZERO
        for source in sources:
            if (not isinstance(source, dict) or source.get("source") not in SOURCES
                    or set(source) - {"source", "ac_kwh",
                                      "price_ore_per_kwh", "fixed_cost_ore"}):
                raise ValueError("invalid AC source")
            kwh = number(source.get("ac_kwh"), "source.ac_kwh")
            price = source.get("price_ore_per_kwh")
            if source["source"] == "unknown" and price is not None:
                raise ValueError("unknown source cannot have a price")
            fee = number(source.get("fixed_cost_ore", 0), "source.fixed_cost_ore",
                         minimum=None)
            if price is None and fee != ZERO:
                raise ValueError("unpriced source cannot carry a fixed fee")
            p = (str(number(price, "source.price_ore_per_kwh", minimum=None))
                 if price is not None else None)
            if kwh == ZERO and fee != ZERO:
                raise ValueError("fixed fee requires delivered energy")
            cleaned.append(dict(source=source["source"], ac_kwh=str(kwh),
                                price_ore_per_kwh=p, fixed_cost_ore=str(fee)))
            ac += kwh
        if ac <= ZERO or aux > ac or amount > ac - aux + EPS:
            raise ValueError("DC gain exceeds delivered AC after auxiliary load")
        if amount == ZERO and ac - aux > EPS:
            raise ValueError("zero DC gain requires AC entirely consumed by direct auxiliary load")
        result["ac_sources"] = cleaned
        result["tariff_version"] = event.get("tariff_version")
        if result["tariff_version"] is not None and (
                not isinstance(result["tariff_version"], str)
                or len(result["tariff_version"]) > 180):
            raise ValueError("invalid tariff_version")
    elif kind in ("drive", "aux"):
        result["consumed_dc_kwh"] = str(number(event.get("consumed_dc_kwh"),
                                                 "consumed_dc_kwh"))
        if kind == "drive" and number(result["consumed_dc_kwh"], "consumed_dc_kwh") == ZERO:
            raise ValueError("drive energy cannot be zero; unknown is not zero")
        if kind == "drive":
            result["distance_km"] = str(number(event.get("distance_km"), "distance_km"))
            if number(result["distance_km"], "distance_km") == ZERO:
                raise ValueError("drive distance must be positive")
            start = event.get("started_at_utc")
            result["started_at_utc"] = instant(start) if start is not None else None
            if result["started_at_utc"] and result["started_at_utc"] > result["occurred_at_utc"]:
                raise ValueError("drive starts after it ends")
        else:
            reason = event.get("reason")
            if reason not in ("preconditioning", "parked", "other"):
                raise ValueError("aux needs explicit preconditioning/parked/other reason")
    elif kind == "adjust":
        result["delta_dc_kwh"] = str(number(event.get("delta_dc_kwh"),
                                              "delta_dc_kwh", minimum=None))
        if number(result["delta_dc_kwh"], "delta_dc_kwh", minimum=None) == ZERO:
            raise ValueError("zero correction")
        if not isinstance(event.get("reason"), str) or not event["reason"].strip():
            raise ValueError("adjust requires an explicit reason")
    return result


def connection(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    db = sqlite3.connect(path, timeout=5)
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA journal_mode=WAL")
    current = db.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        db.close()
        raise ValueError("newer ledger schema is not supported")
    db.executescript(SCHEMA)
    if current < SCHEMA_VERSION:
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    db.commit()
    return db


def read_events(db: sqlite3.Connection) -> list[dict]:
    return [json.loads(row[0]) for row in db.execute(
        "SELECT payload_json FROM ev_cost_events "
        "ORDER BY occurred_at_utc, event_id")]


def add(path: str, event: dict) -> bool:
    canonical = validate(event)
    payload = json.dumps(canonical, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"))
    with connection(path) as db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute(
            "SELECT payload_json FROM ev_cost_events WHERE event_id=?",
            (canonical["event_id"],)).fetchone()
        if previous:
            if previous[0] != payload:
                raise ValueError("event_id conflict: existing immutable event differs")
            return False
        db.execute(
            "INSERT INTO ev_cost_events VALUES (?, ?, ?, ?, ?)",
            (canonical["event_id"], canonical["occurred_at_utc"],
             canonical["type"], payload, datetime.now(UTC).isoformat()))
        # A rejected out-of-order or overdrawing event must not persist.
        calculate(read_events(db))
        db.commit()
        return True


def as_json(value: Decimal | None, *, digits: str = "0.0001"):
    return float(value.quantize(Decimal(digits), rounding=ROUND_HALF_UP)) if value is not None else None


def calculate(events: list[dict], *, now: datetime | None = None) -> dict:
    """Replay in UTC order; unknown inventory cost remains unpriced."""
    events = sorted((validate(e) for e in events),
                    key=lambda e: (e["occurred_at_utc"], e["event_id"]))
    stored = known_value = unknown = estimated = ZERO
    driven = drive_energy = drive_cost_known = drive_unknown = ZERO
    aux_energy = direct_aux_ac = direct_aux_known_cost = direct_aux_unpriced_ac = ZERO
    charge_ac = {source: ZERO for source in SOURCES}
    journeys: list[dict] = []
    seen_opening = False
    for event in events:
        kind = event["type"]
        q = event["quality"]
        if kind == "opening":
            if seen_opening or stored != ZERO or journeys:
                raise ValueError("exactly one opening event, first in chronology")
            seen_opening = True
            stored = number(event["stored_dc_kwh"], "stored_dc_kwh")
            value = event["inventory_value_ore"]
            known_value = number(value, "inventory_value_ore", minimum=None) if value is not None else ZERO
            unknown = stored if value is None else ZERO
            estimated = stored if q == "estimated" else ZERO
            continue
        if not seen_opening:
            raise ValueError("opening event required before charge/consumption")
        if kind == "charge":
            gain = number(event["stored_dc_kwh"], "stored_dc_kwh")
            aux = number(event["direct_aux_ac_kwh"], "direct_aux_ac_kwh")
            total_ac = known_ac = known_cost = ZERO
            for source in event["ac_sources"]:
                kwh = number(source["ac_kwh"], "ac_kwh")
                total_ac += kwh
                charge_ac[source["source"]] += kwh
                if source["price_ore_per_kwh"] is not None:
                    known_ac += kwh
                    known_cost += (kwh * number(source["price_ore_per_kwh"], "price",
                                                minimum=None)
                                   + number(source["fixed_cost_ore"], "fee", minimum=None))
            # Direct charging-time HVAC consumes AC; allocate its cost
            # proportionally across energy sources, but not to DC inventory.
            direct_aux_ac += aux
            direct_aux_known_cost += known_cost * aux / total_ac
            direct_aux_unpriced_ac += aux * (total_ac - known_ac) / total_ac
            known_value += known_cost * (total_ac - aux) / total_ac
            unknown += gain * (total_ac - known_ac) / total_ac
            estimated += gain if q == "estimated" else ZERO
            stored += gain
            continue
        if kind == "adjust":
            delta = number(event["delta_dc_kwh"], "delta_dc_kwh", minimum=None)
            if delta > ZERO:
                # An unexplained increase in SoC has no known acquisition price.
                stored += delta
                unknown += delta
                estimated += delta
                continue
            consumed = -delta
        else:
            consumed = number(event["consumed_dc_kwh"], "consumed_dc_kwh")
        if consumed > stored + EPS:
            raise ValueError("energy consumption exceeds known battery inventory")
        consumed = min(consumed, stored)
        if consumed == ZERO:
            known_used = unknown_used = estimated_used = ZERO
        else:
            portion = consumed / stored
            known_used = known_value * portion
            unknown_used = unknown * portion
            estimated_used = estimated * portion
        stored -= consumed
        known_value -= known_used
        unknown -= unknown_used
        estimated -= estimated_used
        if stored < EPS:
            stored = known_value = unknown = estimated = ZERO
        if kind == "aux":
            aux_energy += consumed
        if kind == "drive":
            distance = number(event["distance_km"], "distance_km")
            complete = unknown_used <= EPS
            entry_quality = ("partial" if not complete else
                             "estimated" if estimated_used > EPS or q == "estimated" else
                             "metered")
            journeys.append({
                "event_id": event["event_id"],
                "started_at_utc": event.get("started_at_utc"),
                "occurred_at_utc": event["occurred_at_utc"],
                "distance_km": as_json(distance),
                "consumed_dc_kwh": as_json(consumed),
                "known_cost_ore": as_json(known_used),
                "cost_ore": as_json(known_used) if complete else None,
                "unpriced_dc_kwh": as_json(unknown_used),
                "quality": entry_quality,
                "kr_per_mil": as_json(known_used / (distance / 10) / 100, digits="0.01")
                if complete and distance > ZERO else None,
            })
            driven += distance
            drive_energy += consumed
            drive_cost_known += known_used
            drive_unknown += unknown_used

    full = driven > ZERO and drive_unknown <= EPS
    summary_quality = ("unavailable" if driven == ZERO else
                       "partial" if not full else
                       "estimated" if any(x["quality"] == "estimated" for x in journeys)
                       else "metered")
    average = known_value / stored if stored > ZERO and unknown <= EPS else None
    checksum = hashlib.sha256(json.dumps(events, sort_keys=True,
                    ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "ev_cost_ledger",
        "as_of_utc": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
        "ledger_revision": checksum,
        "model": "weighted_average_stored_dc_v1",
        "inventory": {
            "stored_dc_kwh": as_json(stored),
            "known_value_ore": as_json(known_value),
            "inventory_value_ore": as_json(known_value) if unknown <= EPS else None,
            "unpriced_dc_kwh": as_json(unknown),
            "average_ore_per_dc_kwh": as_json(average),
            "quality": ("unavailable" if stored == ZERO else "partial" if unknown > EPS
                        else "estimated" if estimated > EPS else "metered"),
        },
        "summary": {
            "distance_km": as_json(driven),
            "consumed_dc_kwh": as_json(drive_energy),
            "known_cost_ore": as_json(drive_cost_known),
            "cost_ore": as_json(drive_cost_known) if full else None,
            "unpriced_dc_kwh": as_json(drive_unknown),
            "kr_per_mil": as_json(drive_cost_known / (driven / 10) / 100, digits="0.01")
                          if full else None,
            "quality": summary_quality,
        },
        "aux_consumed_dc_kwh": as_json(aux_energy),
        "direct_aux_ac_kwh": as_json(direct_aux_ac),
        "direct_aux_known_cost_ore": as_json(direct_aux_known_cost),
        "direct_aux_cost_ore": (as_json(direct_aux_known_cost)
                               if direct_aux_unpriced_ac <= EPS else None),
        "direct_aux_unpriced_ac_kwh": as_json(direct_aux_unpriced_ac),
        "charged_ac_kwh_by_source": {k: as_json(v) for k, v in charge_ac.items()},
        "driving": journeys,
        "event_count": len(events),
        "writes_to_devices": 0,
    }


def report(path: str, *, now: datetime | None = None) -> dict:
    if not Path(path).exists():
        return calculate([], now=now)
    # The reporter must never run DDL or migrate the private ledger.
    from urllib.parse import quote
    uri = "file:" + quote(str(Path(path).resolve()), safe="/") + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=2) as db:
        return calculate(read_events(db), now=now)


def write_export(path: str, payload: dict) -> None:
    """Atomic private file; grant the web reader explicit read ACL if deployed."""
    tmp = f"{path}.{uuid4().hex}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, separators=(",", ":"))
            output.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.environ.get(
        "EV_COST_DB_PATH", "/opt/solinteg/ev-shadow-runtime/var/ev-cost.sqlite"))
    commands = parser.add_subparsers(dest="action", required=True)
    ingest = commands.add_parser("ingest", help="append locally supplied evidence, never device IO")
    ingest.add_argument("--file", required=True)
    export = commands.add_parser("report", help="read aggregate costs from the ledger")
    export.add_argument("--output", help="atomic private JSON snapshot for authenticated API")
    args = parser.parse_args()
    if args.action == "ingest":
        if Path(args.file).stat().st_size > 1_048_576:
            parser.error("input file too large")
        with open(args.file, encoding="utf-8") as inp:
            event = json.load(inp, parse_float=str)
        print(json.dumps({"recorded": add(args.db, event), "writes_to_devices": 0}))
    else:
        result = report(args.db)
        if args.output:
            write_export(args.output, result)
        else:
            print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
