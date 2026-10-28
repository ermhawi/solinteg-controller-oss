"""Two EV charging intents sharing the HOUSE optimizer's buy/sell prices.

This module is pure advisory except for *read-only* access to the existing
optimizer_runs and the trusted EV session JSON. It never writes to EVCC, the
inverter or the house battery plan.

- deadline: meet the battery target by departure if physically feasible;
  minimize cost only after that constraint is satisfied.
- economy: no departure deadline; buy only in relatively cheap windows,
  optionally subject to an absolute user-selected price ceiling.

Solar has an opportunity cost: the export revenue the house loses by using it.
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
ECONOMY_LOOKAHEAD_H = 48
ECONOMY_MIN_HORIZON_H = 6
ECONOMY_CHEAP_QUANTILE = 0.25
DEADLINE_RESERVE_H = 0.25
CHARGE_MODES = frozenset(("deadline", "economy"))


@dataclass(frozen=True)
class PriceDecision:
    grid_budget_a: int
    buy_ore: float | None
    sell_ore: float | None
    reason: str
    planned_grid_kwh: float = 0.0
    solar_allowed: bool = False
    cheap_threshold_ore: float | None = None
    charge_mode: str | None = None


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
    """Read SAME priced house optimizer plan without publishing EV scenarios."""
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
    """Validate an HA-produced EV need; do NOT synthesize stale SoC as fresh.

    deadline requires a fixed departure instant. economy deliberately has no
    deadline. An optional absolute price ceiling is a *user preference*, not
    an assumption about what the user considers cheap.
    """
    if not path or now.tzinfo is None:
        return None
    try:
        with open(path, encoding="utf-8") as source:
            value = json.load(source)
        mode = value.get("charge_mode", "deadline")  # older sessions default to Smart
        if mode not in CHARGE_MODES:
            return None
        age = (now - _instant(value["observed_at_utc"])).total_seconds()
        remaining = _number(value["remaining_ac_kwh"])
        target = _number(value.get("target_soc_pct", 80))
        if (value.get("connected") is not True or value.get("source") != "home_assistant"
                or not 0 <= age <= MAX_SESSION_AGE_S
                or not 0 <= remaining <= 100 or target != 80):
            return None
        raw_soc = value.get("vehicle_soc_pct")
        if raw_soc is not None:
            actual_soc = _number(raw_soc)
            if not 0 <= actual_soc <= 100 or (actual_soc >= target and remaining > 0):
                return None
        price_cap = value.get("max_buy_ore_per_kwh")
        if price_cap is not None:
            price_cap = _number(price_cap)
            if not -200 <= price_cap <= 3000:
                return None
        departure = None
        if mode == "deadline":
            departure = _instant(value["departure_at_utc"])
            if not now < departure <= now + timedelta(hours=48):
                return None
        return dict(charge_mode=mode, departure_at_utc=departure,
                    remaining_ac_kwh=remaining, target_soc_pct=80,
                    max_buy_ore_per_kwh=price_cap)
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return None


def read_shadow_ev_session(path: str | None, *, now: datetime) -> dict | None:
    """Read an unverified EV energy *scenario* only for shadow planners.

    Kept separate from read_ev_session: these assumptions and an API fetch
    timestamp are NOT source-verified BMS measurements and MUST NEVER enable
    EVCC/inverter/vehicle commands. A shadow session cannot pass the strict
    read_ev_session source check.
    """
    if not path or now.tzinfo is None:
        return None
    try:
        with open(path, encoding="utf-8") as source:
            value = json.load(source)
        if (not isinstance(value, dict)
                or value.get("source") != "home_assistant_shadow"
                or value.get("shadow_only") is not True
                or value.get("physical_control_eligible") is not False
                or value.get("api_fetch_confirmed") is not True
                or value.get("energy_estimate_unverified") is not True
                or value.get("observation_time_semantics") != "api_fetch_not_bms_sample"
                or value.get("oem_timestamp_role") != "last_changed_not_sampled"
                or value.get("observation_quality") not in (
                    "api_recent_fields_changed_recently",
                    "api_recent_field_change_unverified",
                ) or value.get("connected") is not True):
            return None
        age = (now - _instant(value["observed_at_utc"])).total_seconds()
        if not -5 <= age <= 5 * 60:
            return None
        mode = value["charge_mode"]
        soc = _number(value["vehicle_soc_pct"])
        target = _number(value["target_soc_pct"])
        capacity = _number(value["assumed_usable_capacity_kwh"])
        efficiency = _number(value["assumed_ac_efficiency"])
        remaining = _number(value["remaining_ac_kwh"])
        if (mode not in CHARGE_MODES or not 0 <= soc <= 100 or target != 80
                or not 45 <= capacity <= 100 or not 0.7 <= efficiency <= 1
                or not 0 <= remaining <= 100
                or abs(remaining - round(max(0, target - soc) / 100
                                          * capacity / efficiency, 3)) > 0.002):
            return None
        price_cap = value.get("max_buy_ore_per_kwh")
        if price_cap is not None:
            price_cap = _number(price_cap)
            if not -200 <= price_cap <= 3000:
                return None
        departure = None
        if mode == "deadline":
            departure = _instant(value["departure_at_utc"])
            if not now < departure <= now + timedelta(hours=48):
                return None
        elif value.get("departure_at_utc") is not None:
            return None
        return {
            "charge_mode": mode, "departure_at_utc": departure,
            "remaining_ac_kwh": remaining, "target_soc_pct": 80,
            "max_buy_ore_per_kwh": price_cap,
            "shadow_only": True, "physical_control_eligible": False,
            "observation_quality": value["observation_quality"],
            "energy_estimate_unverified": True,
        }
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return None


def decide_grid_price(plan: dict | None, *, now: datetime,
                      session: dict | None, max_current_a: int = 6,
                      voltage_v: int = 230,
                      forecast_risk_ore: float = 0) -> PriceDecision:
    """Advice only; same buy/sell tariff and forecast premium as house DP.

    Deadline mode selects cheapest feasible solar/export and grid slots to meet
    required AC energy. If the price feed is missing, TIME wins over price.

    Economy mode selects the cheapest quartile of the next up-to-48h *full buy
    price* curve (with house forecast risk premium); it may also enforce an
    optional absolute buy ceiling from the user. Solar is cheap when its lost
    export revenue is below the same cutoff. A percentile is relative, not
    proof of absolute cheapness; the optional ceiling is important for that.
    """
    def no(reason: str, *, mode: str | None = None) -> PriceDecision:
        return PriceDecision(0, None, None, reason, charge_mode=mode)

    if not session or now.tzinfo is None:
        return no("missing fresh EV session or target")
    try:
        mode = session.get("charge_mode", "deadline")
        remaining = _number(session["remaining_ac_kwh"])
        risk = _number(forecast_risk_ore)
        if (mode not in CHARGE_MODES or not 0 <= remaining <= 100
                or not 0 <= risk <= 100 or not 6 <= max_current_a <= 16
                or not 200 <= voltage_v <= 250):
            return no("invalid EV mode or limits")
        if remaining <= 0:
            return no("80 percent target reached; do not charge", mode=mode)
        if mode == "deadline":
            departure = session["departure_at_utc"]
            if (not isinstance(departure, datetime) or departure.tzinfo is None
                    or not now < departure <= now + timedelta(hours=48)):
                return no("invalid departure", mode=mode)
            time_left_h = (departure - now).total_seconds() / 3600
            needed_h = remaining * 1000 / (voltage_v * max_current_a)
            urgent = needed_h >= time_left_h - DEADLINE_RESERVE_H
        else:
            departure = now + timedelta(hours=ECONOMY_LOOKAHEAD_H)
            urgent = False
        cap = session.get("max_buy_ore_per_kwh")
        if cap is not None:
            cap = _number(cap)
            if not -200 <= cap <= 3000:
                return no("invalid user price ceiling", mode=mode)
    except (KeyError, TypeError, ValueError, OverflowError):
        return no("invalid EV session")

    def price_unavailable(reason: str) -> PriceDecision:
        if mode == "deadline" and urgent:
            # If the deadline is already at risk, charging may be necessary
            # despite an unavailable price feed. Electrical/battery gates
            # remain independent and cannot be bypassed by this advice.
            return PriceDecision(max_current_a, None, None,
                                 reason + "; urgent deadline, price ignored",
                                 solar_allowed=True, charge_mode=mode)
        return no(reason + "; waiting for usable shared price plan", mode=mode)

    if not plan:
        return price_unavailable("missing shared house price plan")
    try:
        age = (now - _instant(plan["logged_at"])).total_seconds()
        if not 0 <= age <= MAX_PLAN_AGE_S:
            return price_unavailable("stale shared house price plan")
        inputs, dispatch = plan["inputs"], plan["dispatch"]
        if not isinstance(inputs, list) or not 1 <= len(inputs) <= 300 or len(inputs) != len(dispatch):
            raise ValueError("misaligned house plan")
        segments: list[tuple[float, int, str, float]] = []
        price_samples: list[float] = []
        first: dict | None = None
        first_index: int | None = None
        current_start: datetime | None = None
        prev_end: datetime | None = None
        covered_until: datetime | None = None
        total_capacity_kwh = 0.0
        for i, (price, house) in enumerate(zip(inputs, dispatch)):
            if price["startTime"] != house["startTime"]:
                raise ValueError("optimizer price/dispatch time mismatch")
            start = _instant(price["startTime"])
            end = start + SLOT
            if end <= now or start >= departure:
                continue
            if prev_end is not None and start != prev_end:
                raise ValueError("gap in price horizon")
            if first is None:
                if start > now or price.get("priceSource") != "actual":
                    raise ValueError("current settled price missing")
                first = price
                first_index = i
                current_start = start
            prev_end = end
            covered_until = min(end, departure)
            duration_h = (min(end, departure) - max(start, now)).total_seconds() / 3600
            if duration_h <= 0:
                continue
            capacity_kwh = voltage_v * max_current_a * duration_h / 1000
            total_capacity_kwh += capacity_kwh
            buy = _number(price["buyPrice"])
            sell = _number(price["sellPrice"])
            solar_export = _number(house["solarExportKwh"])
            if (not -500 <= buy <= 3000 or not -500 <= sell <= 3000
                    or solar_export < 0 or price.get("priceSource") not in ("actual", "forecast")):
                raise ValueError("invalid shared price/solar")
            premium = risk if price["priceSource"] == "forecast" else 0
            price_samples.append(buy + premium)
            solar_kwh = min(capacity_kwh, solar_export * duration_h / 0.25)
            grid_kwh = capacity_kwh - solar_kwh
            if solar_kwh > 0:
                segments.append((sell, i, "solar", solar_kwh))
            if grid_kwh > 0:
                segments.append((buy + premium, i, "grid", grid_kwh))

        if first is None or first_index is None or current_start is None or covered_until is None:
            raise ValueError("current price slot not available")
        buy_now = _number(first["buyPrice"])
        sell_now = _number(first["sellPrice"])
        current_duration_h = (min(current_start + SLOT, departure) - now).total_seconds() / 3600

        if mode == "economy":
            if (covered_until - now).total_seconds() < ECONOMY_MIN_HORIZON_H * 3600:
                return no("insufficient lookahead to identify cheap hours", mode=mode)
            ordered = sorted(price_samples)
            threshold = ordered[max(0, math.floor((len(ordered) - 1)
                                                   * ECONOMY_CHEAP_QUANTILE))]
            median = ordered[(len(ordered) - 1) // 2]
            if cap is not None:
                threshold = min(threshold, cap)
            elif median > 0:
                # A flat, expensive 48h horizon has q25 == median; its
                # "cheapest quarter" is not actually cheap. In the automatic
                # relative mode require STRICTLY below the median. Buy prices
                # are published to 0.01 öre accuracy, so the displayed
                # threshold and real decision now agree on ties.
                threshold = min(threshold, round(median, 2) - 0.01)
            # Free/negative power is cheap even when every price is identical.
            grid_allowed = buy_now <= threshold
            # Current sell price is the marginal cost of using otherwise
            # exported solar. We NEVER take energy reserved by the house DP.
            solar_allowed = sell_now <= threshold
            requested = min(remaining, voltage_v * max_current_a * current_duration_h / 1000)
            return PriceDecision(
                max_current_a if grid_allowed else 0, buy_now, sell_now,
                "economy: cheap grid window" if grid_allowed else
                "economy: solar opportunity cost below threshold" if solar_allowed else
                "economy: price too high; wait without deadline",
                round(requested, 4) if grid_allowed else 0,
                solar_allowed=solar_allowed, cheap_threshold_ore=round(threshold, 2),
                charge_mode=mode,
            )

        # Deadline mode: the energy target has priority over marginal costs.
        # A 15-minute reserve helps prevent missing departure due to delays.
        if covered_until < departure:
            return price_unavailable("price horizon ends before departure")
        if urgent:
            reason = "deadline urgent: prioritize 80 percent at departure"
            if remaining > total_capacity_kwh + 1e-9:
                reason += "; target physically infeasible at configured current"
            return PriceDecision(max_current_a, buy_now, sell_now, reason,
                                 solar_allowed=True, charge_mode=mode)

        selected_grid_kwh = 0.0
        selected_solar_kwh = 0.0
        needed = remaining
        for _, index, kind, available in sorted(segments, key=lambda item: (item[0], item[1])):
            used = min(needed, available)
            needed -= used
            if index == first_index and kind == "grid":
                selected_grid_kwh = used
            if index == first_index and kind == "solar":
                selected_solar_kwh = used
            if needed < 1e-9:
                break
        # AC charging cannot command 1-5A; small final energy requires a
        # minimum 6A pulse plus a separate meter/SoC stop (not implemented).
        grid_a = (min(max_current_a, max(6, math.ceil(selected_grid_kwh * 1000 /
                   (voltage_v * current_duration_h) - 1e-9)))
                  if selected_grid_kwh > 1e-9 else 0)
        reason = ("deadline: grid allocated in low-cost slot" if grid_a else
                  "deadline: solar allocated now" if selected_solar_kwh > 0 else
                  "deadline: cheaper energy planned later")
        if remaining > total_capacity_kwh + 1e-9:
            reason += "; target physically infeasible at configured current"
        return PriceDecision(grid_a, buy_now, sell_now, reason,
                             round(selected_grid_kwh, 4),
                             solar_allowed=selected_solar_kwh > 0,
                             charge_mode=mode)
    except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        # Corrupt or ambiguous inputs are not equivalent to an unavailable
        # supplier feed. Do not accidentally turn a bad snapshot into an
        # automatic grid-charging decision.
        return no("invalid or ambiguous shared price plan", mode=mode)
