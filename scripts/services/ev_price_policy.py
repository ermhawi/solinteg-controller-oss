"""Price-aware EV scheduling using the HOUSE optimizer's buy/sell price slots.

Pure advice only. House optimizer_runs is read-only; no EVCC or inverter writes.
The car has a departure/energy requirement, not battery arbitrage capability.
"""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo


STOCKHOLM = ZoneInfo("Europe/Stockholm")
SLOT = timedelta(minutes=15)
MAX_PLAN_AGE_S = 90 * 60
MAX_SESSION_AGE_S = 20 * 60


@dataclass(frozen=True)
class PriceDecision:
    grid_budget_a: int
    buy_ore: float | None
    sell_ore: float | None
    reason: str
    planned_grid_kwh: float = 0.0


def _number(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a measurement")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite measurement")
    return number


def _instant(raw: object) -> datetime:
    parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        # Legacy optimizer inputs use naive Stockholm times. Refuse the repeated
        # autumn 02:xx hour until the price pipeline supplies unambiguous UTC.
        a = parsed.replace(tzinfo=STOCKHOLM, fold=0)
        b = parsed.replace(tzinfo=STOCKHOLM, fold=1)
        if a.utcoffset() != b.utcoffset():
            raise ValueError("ambiguous local price slot")
        if a.astimezone(timezone.utc).astimezone(STOCKHOLM).replace(tzinfo=None) != parsed:
            raise ValueError("nonexistent local price slot")
        parsed = a
    return parsed.astimezone(timezone.utc)


def read_house_price_plan(path: str) -> dict | None:
    """Use the existing planner's actual/forecast slots and full retail prices.

    optimizer_runs.inputs_json already contains buyPrice (incl. VAT, supplier
    surcharge, energy tax and grid transfer) and sellPrice. Never duplicate
    the tariff formula or publish an EV candidate into optimizer_runs.
    """
    try:
        uri = "file:" + quote(str(Path(path).resolve()), safe="/") + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
            row = db.execute(
                "SELECT logged_at, inputs_json, dispatch_json "
                "FROM optimizer_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return (dict(logged_at=row[0], inputs=json.loads(row[1]),
                     dispatch=json.loads(row[2])) if row else None)
    except (OSError, ValueError, sqlite3.Error):
        return None


def read_ev_session(path: str | None, *, now: datetime) -> dict | None:
    """Consume only an explicitly configured, freshly measured EV session.

    A read-only HA producer must provide remaining AC energy (NOT guessed SoC)
    and a fixed UTC departure instant. Until it exists, grid top-up is off.
    """
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as source:
            value = json.load(source)
        age = (now - _instant(value["observed_at_utc"])).total_seconds()
        departure = _instant(value["departure_at_utc"])
        remaining = _number(value["remaining_ac_kwh"])
        if (value.get("connected") is not True or value.get("source") != "home_assistant"
                or not 0 <= age <= MAX_SESSION_AGE_S
                or not now < departure <= now + timedelta(hours=48)
                or not 0 <= remaining <= 100):
            return None
        return dict(departure_at_utc=departure, remaining_ac_kwh=remaining)
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return None


def decide_grid_price(plan: dict | None, *, now: datetime,
                      session: dict | None, max_current_a: int = 6,
                      voltage_v: int = 230,
                      forecast_risk_ore: float = 0) -> PriceDecision:
    """Allocate required car kWh to cheapest solar/export and grid-buy windows.

    Solar's marginal cost is the export revenue foregone (sellPrice); grid's
    marginal cost is the same full buyPrice used by the house battery planner.
    Projected solar is ONLY the house planner's already-exported solar, never
    energy reserved for the house battery. The live solar controller separately
    verifies physical surplus; this function authorizes only the GRID portion.
    """
    def no(reason: str) -> PriceDecision:
        return PriceDecision(0, None, None, reason)

    if not plan or not session or now.tzinfo is None:
        return no("missing shared price plan or fresh EV energy need")
    try:
        age = (now - _instant(plan["logged_at"])).total_seconds()
        if not 0 <= age <= MAX_PLAN_AGE_S:
            return no("stale house optimizer prices")
        deadline = session["departure_at_utc"]
        remaining = _number(session["remaining_ac_kwh"])
        risk = _number(forecast_risk_ore)
        if (not isinstance(deadline, datetime) or deadline.tzinfo is None
                or not now < deadline <= now + timedelta(hours=48)
                or not 0 <= remaining <= 100 or not 0 <= risk <= 100
                or not 6 <= max_current_a <= 16 or not 200 <= voltage_v <= 250):
            return no("invalid EV or price limits")
        if remaining <= 0:
            return no("car target already reached")
        inputs, dispatch = plan["inputs"], plan["dispatch"]
        if not isinstance(inputs, list) or not 1 <= len(inputs) <= 300 or len(inputs) != len(dispatch):
            return no("misaligned house price/dispatch plan")

        segments: list[tuple[float, int, str, float]] = []
        first: dict | None = None
        current_index: int | None = None
        prev_end: datetime | None = None
        total_capacity_kwh = 0.0
        covered_until: datetime | None = None
        for i, (price, house) in enumerate(zip(inputs, dispatch)):
            if price["startTime"] != house["startTime"]:
                raise ValueError("optimizer price/dispatch time mismatch")
            start = _instant(price["startTime"])
            end = start + SLOT
            if end <= now or start >= deadline:
                continue
            if prev_end is not None and start != prev_end:
                raise ValueError("gap in shared price horizon")
            if first is None:
                if start > now:
                    raise ValueError("current price slot missing")
                first = price
                current_index = i
                if first.get("priceSource") != "actual":
                    raise ValueError("current price is forecast, not settled")
            prev_end = end
            covered_until = end
            duration = (min(end, deadline) - max(start, now)).total_seconds() / 3600
            if duration <= 0:
                continue
            capacity = voltage_v * max_current_a * duration / 1000
            total_capacity_kwh += capacity
            buy = _number(price["buyPrice"])
            sell = _number(price["sellPrice"])
            solar = _number(house["solarExportKwh"])
            if solar < 0 or not -500 <= buy <= 3000 or not -500 <= sell <= 3000:
                raise ValueError("invalid house plan price/solar")
            if price.get("priceSource") not in ("actual", "forecast"):
                raise ValueError("unknown price source")
            solar_kwh = min(capacity, solar * duration / 0.25)
            grid_kwh = capacity - solar_kwh
            if solar_kwh > 0:
                segments.append((sell, i, "solar", solar_kwh))
            if grid_kwh > 0:
                premium = risk if price["priceSource"] == "forecast" else 0
                segments.append((buy + premium, i, "grid", grid_kwh))

        if first is None or covered_until is None or covered_until < deadline:
            return no("shared price horizon does not cover departure")
        current_buy = _number(first["buyPrice"])
        current_sell = _number(first["sellPrice"])
        selected_grid_kwh = 0.0
        selected_solar_kwh = 0.0
        needed = remaining
        # Same economic inputs as house optimizer, different EV constraint:
        # reach required AC energy by deadline with no ability to resell from EV.
        for _, index, kind, available in sorted(segments, key=lambda s: (s[0], s[1])):
            used = min(needed, available)
            needed -= used
            if index == current_index and kind == "grid":
                selected_grid_kwh = used
            if index == current_index and kind == "solar":
                selected_solar_kwh = used
            if needed < 1e-9:
                break
        current_duration_h = (min(_instant(first["startTime"]) + SLOT, deadline) - now).total_seconds() / 3600
        # AC charging cannot command 1-5 A. A fractional final slot needs a
        # minimum 6 A pulse; its duration must be bounded by delivered energy.
        grid_a = (min(max_current_a, max(6, math.ceil(selected_grid_kwh * 1000 /
                  (voltage_v * current_duration_h) - 1e-9))) if selected_grid_kwh > 1e-9 else 0)
        reason = ("price-scheduled grid top-up" if grid_a else
                  "solar scheduled now; no grid top-up" if selected_solar_kwh > 0 else
                  "cheaper energy scheduled later")
        if remaining > total_capacity_kwh + 1e-9:
            reason += "; departure target physically infeasible at configured current"
        return PriceDecision(grid_a, current_buy, current_sell, reason,
                             round(selected_grid_kwh, 4))
    except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        return no("invalid or ambiguous shared price plan")
