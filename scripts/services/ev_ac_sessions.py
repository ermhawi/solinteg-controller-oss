#!/usr/bin/env python3
"""Private, read-only report of *covered* DEFA/ABL AC charging fragments.

Reconciles validated EVCC Ict2 230V/PF1 estimated intervals with immutable,
historical 15-minute price-model evidence. Prices are a hypothetical all-grid,
spot/model-only scenario, NOT a Tibber invoice or actual energy source.
No EVCC, Volvo, inverter, HA or network access. No DB writes.
"""
from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import ev_ac_capture as ac

UTC = timezone.utc
MAX_OBSERVATIONS = 120_000
MODEL_PRICE_QUALITY = "modelled_day_ahead_excludes_grid_and_unreconciled"
EPSILON = Decimal("0.000000001")
STOCKHOLM = ZoneInfo("Europe/Stockholm")
# Sundsvall Elnat's PUBLIC 2026 residential tariff (VAT included).
# It has NOT been reconciled against this customer's invoices.
PUBLIC_NETWORK_2026_ORE_KWH = Decimal("26.30") + Decimal("45.00")
PUBLIC_NETWORK_2026_SOURCE = (
    "https://sundsvallelnat.se/min-anslutning/priser-och-avtalsvillkor/"
    "2026-sakringsabonnemang-privat"
)


class SessionEvidenceError(ValueError):
    """Source evidence invalid, with no sensitive details."""


def _time_ms(milliseconds: int) -> str:
    return datetime.fromtimestamp(milliseconds / 1000, UTC).isoformat(timespec="milliseconds")


def _number(value: object, *, minimum: Decimal | None = None) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SessionEvidenceError("nondecimal evidence") from exc
    if not number.is_finite() or (minimum is not None and number < minimum):
        raise SessionEvidenceError("invalid evidence decimal")
    return number


def _round(value: Decimal, digits: str = "0.000001") -> float:
    return float(value.quantize(Decimal(digits), rounding=ROUND_HALF_UP))


def _ro_connect(path: str) -> sqlite3.Connection | None:
    source = Path(path)
    if not source.is_file():
        return None
    uri = "file:" + quote(str(source.resolve()), safe="/") + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=3)


def _load_ac(path: str, since_ms: int, now_ms: int) -> tuple[list[tuple[int, dict]], dict[tuple[int, int], Decimal]]:
    conn = _ro_connect(path)
    if conn is None:
        return [], {}
    try:
        conn.execute("BEGIN")  # a consistent SQLite read snapshot, never DDL
        rows = conn.execute(
            "SELECT observed_epoch_ms,payload_json FROM ev_ac_observations "
            "WHERE observed_epoch_ms >= ? AND observed_epoch_ms <= ? "
            "ORDER BY observed_epoch_ms LIMIT ?",
            (since_ms, now_ms + 5000, MAX_OBSERVATIONS + 1),
        ).fetchall()
        if len(rows) > MAX_OBSERVATIONS:
            raise SessionEvidenceError("observation window too large")
        source = []
        for epoch, payload in rows:
            try:
                record = json.loads(payload)
            except (ValueError, TypeError) as exc:
                raise SessionEvidenceError("invalid private observation JSON") from exc
            if not isinstance(record, dict) or record.get("observed_epoch_ms") != epoch:
                raise SessionEvidenceError("observation timestamp differs from database key")
            if record.get("source_writes") != 0:
                raise SessionEvidenceError("evidence has unsafe source writes")
            source.append((epoch, record))
        intervals: dict[tuple[int, int], Decimal] = {}
        for begin, end, kwh in conn.execute(
                "SELECT start_epoch_ms,end_epoch_ms,estimated_ac_kwh "
                "FROM ev_ac_estimated_intervals "
                "WHERE start_epoch_ms >= ? ORDER BY start_epoch_ms",
                (since_ms,)):
            key = (begin, end)
            if key in intervals or end <= begin:
                raise SessionEvidenceError("duplicate or invalid AC intervals")
            intervals[key] = _number(kwh, minimum=Decimal("0"))
        conn.commit()
        return source, intervals
    except sqlite3.Error as exc:
        raise SessionEvidenceError("AC evidence database unavailable") from exc
    finally:
        conn.close()


def _load_prices(path: str | None, start_ms: int, end_ms: int) -> tuple[list[tuple[int, int, Decimal]], list[int]]:
    if not path:
        return [], []
    conn = _ro_connect(path)
    if conn is None:
        return [], []
    try:
        rows = conn.execute(
            "SELECT slot_start_epoch_ms,slot_end_epoch_ms,buy_before_grid_ore,quality "
            "FROM ev_cost_price_evidence "
            "WHERE slot_end_epoch_ms > ? AND slot_start_epoch_ms < ? "
            "ORDER BY slot_start_epoch_ms",
            (start_ms, end_ms),
        ).fetchall()
    except sqlite3.Error as exc:
        raise SessionEvidenceError("historical price evidence DB unavailable") from exc
    finally:
        conn.close()
    prices = []
    previous_end = None
    for start, end, value, quality in rows:
        if (type(start) is not int or type(end) is not int or end <= start
                or end - start != 900000 or quality != MODEL_PRICE_QUALITY):
            raise SessionEvidenceError("untrusted or non-quarter-hour price evidence")
        if previous_end is not None and start < previous_end:
            raise SessionEvidenceError("overlapping historical price intervals")
        prices.append((start, end, _number(value)))
        previous_end = end
    return prices, [row[0] for row in prices]


def _energy_between(start: int, end: int, a: Decimal, b: Decimal,
                    part_start: int, part_end: int) -> Decimal:
    total = Decimal(end - start)
    ratio_start = Decimal(part_start - start) / total
    ratio_end = Decimal(part_end - start) / total
    first = a + (b - a) * ratio_start
    last = a + (b - a) * ratio_end
    return ((first + last) / 2 * ac.VOLTAGE * ac.POWER_FACTOR
            * Decimal(part_end - part_start) / Decimal("3600000000"))


def _scenario_ore(start: int, end: int, a: Decimal, b: Decimal,
                  prices: list[tuple[int, int, Decimal]], starts: list[int]) -> Decimal | None:
    """Exact linear-current split across UTC price boundary; NULL on any missing slice."""
    current = start
    model_ore = Decimal("0")
    while current < end:
        idx = bisect.bisect_right(starts, current) - 1
        if idx < 0 or idx >= len(prices):
            return None
        slot_start, slot_end, price = prices[idx]
        if not slot_start <= current < slot_end:
            return None
        stop = min(end, slot_end)
        model_ore += _energy_between(start, end, a, b, current, stop) * price
        current = stop
    return model_ore


def _public_network_2026_cost(
    start_ms: int, end_ms: int, energy_kwh: Decimal,
    spot_model_ore: Decimal, *, complete_model_price: bool,
) -> Decimal | None:
    """Separate opt-in hypothetical *all-grid* cost using published 2026 grid rates.

    Never invoices, actual sourcing, Tibber variable purchase costs or fixed fees.
    If the fragment spans a different calendar year in Sweden, refuse.
    """
    if not complete_model_price or end_ms <= start_ms:
        return None
    first = datetime.fromtimestamp(start_ms / 1000, UTC).astimezone(STOCKHOLM)
    last = datetime.fromtimestamp((end_ms - 1) / 1000, UTC).astimezone(STOCKHOLM)
    if first.year != 2026 or last.year != 2026:
        return None
    return spot_model_ore + energy_kwh * PUBLIC_NETWORK_2026_ORE_KWH


def _active(record: dict) -> Decimal | None:
    if (record.get("quality") != "estimated"
            or record.get("state") != "C2"
            or record.get("source_writes") != 0
            or record.get("active_channel") != "Ict2"
            or record.get("configured_pwm_raw") != 100
            or record.get("pilot_pwm_raw") != 100):
        return None
    current = _number(record.get("active_current_a"), minimum=Decimal("0"))
    if not Decimal("1") <= current <= Decimal("7"):
        raise SessionEvidenceError("invalid verified EVCC channel current")
    return current


def analyze(ac_path: str, price_path: str | None = None, *,
            now: datetime | None = None, days: int = 7,
            published_network_2026: bool = False) -> dict:
    """Report observed coverage fragments, NOT guaranteed entire charging sessions."""
    if not 1 <= days <= 30:
        raise ValueError("days must be 1..30")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must contain timezone")
    now = now.astimezone(UTC)
    now_ms = int(now.timestamp() * 1000)
    since_ms = now_ms - days * 86400000
    observations, stored_intervals = _load_ac(ac_path, since_ms, now_ms)
    if not observations:
        return {
            "source": "evcc_ict2_readonly",
            "model": "230v_pf1_gap_aware_v1",
            "quality": "unavailable", "as_of_utc": now.isoformat(),
            "observations": 0, "covered_intervals": 0,
            "observed_charging_fragments": [],
            "covered_seconds": 0,
            "estimated_ac_kwh_covered_only": None,
            "hypothetical_grid_only_spot_model_ore": None,
            **({"hypothetical_grid_only_public_2026_network_ore": None,
                "published_network_2026_ore_per_kwh": float(PUBLIC_NETWORK_2026_ORE_KWH),
                "published_network_2026_tariff_verified_for_account": False}
               if published_network_2026 else {}),
            "actual_cost_ore": None, "vehicle_battery_dc_kwh": None,
            "device_writes": 0,
        }
    prices, starts = _load_prices(
        price_path, observations[0][0], observations[-1][0] + 1)
    # Each linked pair must have an immutable stored integral, and its
    # computed value must reproduce that integral, not invent charge.
    links = []
    gaps = 0
    for (prev_ms, prev), (next_ms, current) in zip(observations, observations[1:]):
        duration = next_ms - prev_ms
        a, b = _active(prev), _active(current)
        if not (0 < duration <= ac.MAX_GAP_MS and a is not None and b is not None):
            gaps += 1
            continue
        stored = stored_intervals.get((prev_ms, next_ms))
        if stored is None:
            raise SessionEvidenceError("validated charging interval missing from source")
        recomputed = _energy_between(prev_ms, next_ms, a, b, prev_ms, next_ms)
        if abs(stored - recomputed) > EPSILON:
            raise SessionEvidenceError("stored charging energy does not match current evidence")
        spot = _scenario_ore(prev_ms, next_ms, a, b, prices, starts)
        links.append((prev_ms, next_ms, stored, spot))
    fragments = []
    for link in links:
        if not fragments or fragments[-1]["end_ms"] != link[0]:
            fragments.append({
                "start_ms": link[0], "end_ms": link[1],
                "energy": Decimal("0"), "price": Decimal("0"),
                "price_complete": True, "intervals": 0,
            })
        f = fragments[-1]
        f["end_ms"] = link[1]
        f["intervals"] += 1
        f["energy"] += link[2]
        if link[3] is None:
            f["price_complete"] = False
        else:
            f["price"] += link[3]
    by_epoch = {epoch: pos for pos, (epoch, _) in enumerate(observations)}
    rendered = []
    for f in fragments:
        first_i = by_epoch[f["start_ms"]]
        last_i = by_epoch[f["end_ms"]]
        before = observations[first_i-1] if first_i > 0 else None
        after = observations[last_i+1] if last_i+1 < len(observations) else None
        def bounded(neighbor: tuple[int, dict] | None, boundary: int) -> bool:
            return (neighbor is not None
                    and abs(neighbor[0] - boundary) <= ac.MAX_GAP_MS
                    and neighbor[1].get("quality") == "not_charging"
                    and neighbor[1].get("state") != "C2")
        public_cost = (
            _public_network_2026_cost(
                f["start_ms"], f["end_ms"], f["energy"], f["price"],
                complete_model_price=f["price_complete"])
            if published_network_2026 else None
        )
        rendered.append({
            "observed_start_utc": _time_ms(f["start_ms"]),
            "observed_end_utc": _time_ms(f["end_ms"]),
            "covered_seconds": round((f["end_ms"] - f["start_ms"]) / 1000, 2),
            "covered_intervals": f["intervals"],
            "estimated_ac_kwh_covered_only": _round(f["energy"]),
            "start_bounded_by_idle_read": bounded(before, f["start_ms"]),
            "end_bounded_by_idle_read": bounded(after, f["end_ms"]),
            "full_session_energy_known": False,
            "hypothetical_grid_only_spot_model_ore": (
                _round(f["price"], "0.0001") if f["price_complete"] else None),
            **({"hypothetical_grid_only_public_2026_network_ore": (
                _round(public_cost, "0.0001") if public_cost is not None else None)}
               if published_network_2026 else {}),
            "cost_ore": None,
        })
    total_energy = sum((f["energy"] for f in fragments), Decimal("0"))
    full_price = bool(fragments) and all(f["price_complete"] for f in fragments)
    total_price = sum((f["price"] for f in fragments), Decimal("0"))
    public_total = (
        _public_network_2026_cost(
            min(f["start_ms"] for f in fragments),
            max(f["end_ms"] for f in fragments),
            total_energy, total_price, complete_model_price=full_price)
        if published_network_2026 and fragments else None
    )
    last_t, last = observations[-1]
    return {
        "source": "evcc_ict2_readonly",
        "model": "230v_pf1_gap_aware_v1",
        "as_of_utc": now.isoformat(),
        "quality": "estimated_partial_coverage" if fragments else "unavailable",
        "assumed_voltage_v": float(ac.VOLTAGE),
        "assumed_power_factor": float(ac.POWER_FACTOR),
        "energy_not_observed_during_gaps": "unknown",
        "source_price_quality": MODEL_PRICE_QUALITY if prices else "unavailable",
        "hypothetical_pricing_assumption": "all_covered_AC_is_purchased_grid_energy_excluding_grid_tariff_and_reconciliation",
        "observations": len(observations),
        "skipped_source_transitions_or_gaps": gaps,
        "covered_intervals": len(links),
        "covered_seconds": round(sum((end-start)/1000 for start,end,_,_ in links), 2),
        "fragment_count": len(fragments),
        "observed_charging_fragments": rendered[-100:],
        "fragments_truncated": len(fragments) > 100,
        "estimated_ac_kwh_covered_only": _round(total_energy) if links else None,
        "hypothetical_grid_only_spot_model_ore": (
            _round(total_price, "0.0001") if full_price else None),
        **({
            "hypothetical_grid_only_public_2026_network_ore": (
                _round(public_total, "0.0001") if public_total is not None else None),
            "published_network_2026_ore_per_kwh": float(PUBLIC_NETWORK_2026_ORE_KWH),
            "published_network_2026_source": PUBLIC_NETWORK_2026_SOURCE,
            "published_network_2026_tariff_verified_for_account": False,
            "fixed_fees_allocated": False,
            "supplier_variable_costs_verified": False,
        } if published_network_2026 else {}),
        "actual_cost_ore": None,
        "vehicle_battery_dc_kwh": None,
        "full_session_ac_kwh": None,
        "charging_observed_recently": (
            _active(last) is not None
            and -5000 <= now_ms - last_t <= ac.MAX_AGE_S * 1000),
        "last_source_utc": _time_ms(last_t),
        "device_writes": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ac-db", default="/opt/solinteg/ev-shadow-runtime/var/ev-ac-estimate.sqlite")
    parser.add_argument("--price-db", default="/opt/solinteg/ev-shadow-runtime/var/ev-cost-price-evidence.sqlite")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--published-network-2026", action="store_true",
                        help="public 2026 network rate only: unverified hypothetical grid scenario")
    args = parser.parse_args()
    try:
        result = analyze(args.ac_db, args.price_db, days=args.days,
                         published_network_2026=args.published_network_2026)
        print(json.dumps(result, ensure_ascii=False))
    except (SessionEvidenceError, sqlite3.Error, OSError) as exc:
        print(json.dumps({"quality": "unavailable",
                          "reason": str(exc) if isinstance(exc, SessionEvidenceError)
                          else type(exc).__name__,
                          "actual_cost_ore": None, "device_writes": 0}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
