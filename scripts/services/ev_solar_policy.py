"""Pure solar-surplus and price-aware EV recommendations, never hardware commands.

Assumes Solinteg's grid_w is POSITIVE for export and battery_w is
POSITIVE for discharge. Grid export already includes any ongoing EV load:
add measured EV power back when determining the available solar budget.
This is economic control, not 20 A fuse protection.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping


MIN_AMPS = 6
DEFAULT_MAX_AMPS = 6   # Do not raise above tested low current without phase safety.
GRID_MARGIN_W = 200
STOP_BATTERY_DISCHARGE_W = 150


@dataclass(frozen=True)
class SolarDecision:
    suggested_amps: int
    permitted_amps: int
    reason: str
    surplus_w: int | None
    phase_guard_verified: bool


def _number(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a measurement")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("invalid measurement")
    return value


def decide_solar(
    live: Mapping | None,
    *,
    now: datetime,
    ev_measured_a: float | None,
    ev_running: bool,
    stable_surplus_samples: int,
    phase_guard_verified: bool = False,
    phase_headroom_a: float | None = None,
    max_current_a: int = DEFAULT_MAX_AMPS,
    grid_topup_a: int = 0,
    voltage_v: int = 230,
    max_age_seconds: int = 90,
) -> SolarDecision:
    """One advisory tick; 0 means pause/wait. No service may treat it as a command.

    A future actuator MUST independently verify physical phase current, BMS
    policy, local failure mode and a fresh EVCC readback before any resume.
    """
    def result(amps: int, reason: str, surplus: int | None = None) -> SolarDecision:
        if not phase_guard_verified or phase_headroom_a is None:
            return SolarDecision(amps, 0, reason + "; L3 protection unverified",
                                 surplus, False)
        try:
            allowed = max(0, math.floor(_number(phase_headroom_a)))
        except (ValueError, TypeError, OverflowError):
            return SolarDecision(amps, 0, reason + "; invalid L3 headroom",
                                 surplus, False)
        return SolarDecision(amps, min(amps, allowed) if allowed >= MIN_AMPS else 0,
                             reason, surplus, True)

    if live is None or now.tzinfo is None:
        return result(0, "missing inverter telemetry")
    try:
        stamp = datetime.fromisoformat(str(live["timestamp"]).replace("Z", "+00:00"))
        age = (now - stamp).total_seconds()
        if stamp.tzinfo is None or not -5 <= age <= max_age_seconds:
            return result(0, "stale inverter telemetry")
        soc = _number(live["soc_pct"])
        grid_w = _number(live["grid_w"])
        battery_w = _number(live["battery_w"])
        pv_w = _number(live["pv_w"])
        house_load_w = _number(live["house_load_w"])
        measured_ev = _number(ev_measured_a)
        grid_topup = _number(grid_topup_a)
        if not (0 <= soc <= 100 and pv_w >= 0 and house_load_w >= 0 and
                measured_ev >= 0 and voltage_v > 0 and
                6 <= max_current_a <= 16 and 0 <= grid_topup <= max_current_a and
                stable_surplus_samples >= 0):
            return result(0, "invalid telemetry or limits")
    except (KeyError, ValueError, TypeError, OverflowError):
        return result(0, "invalid telemetry")
    if battery_w > STOP_BATTERY_DISCHARGE_W:
        return result(0, "house battery discharging")
    if soc < (99.0 if ev_running else 99.5):
        return result(0, "house battery not yet full")
    # EV load is INCLUDED in house_load_w and grid_w. Without this term a
    # started EV destroys the measured export and oscillates to zero.
    available_w = grid_w + (measured_ev * voltage_v) - GRID_MARGIN_W
    available_a = max(0, math.floor(available_w / voltage_v))
    # Grid top-up is chosen against the house optimizer's SAME buy/sell prices,
    # but may not consume more than its separate price-scheduled allocation.
    suggested = min(max_current_a, available_a + int(grid_topup))
    if suggested < MIN_AMPS:
        return result(0, "insufficient solar surplus and cheap grid budget", round(available_w))
    if not ev_running and stable_surplus_samples < 3 and grid_topup < MIN_AMPS:
        return result(0, "waiting for stable solar surplus", round(available_w))
    funding = ("solar and price-scheduled grid" if grid_topup > 0 and available_a > 0
               else "price-scheduled grid" if grid_topup > 0 else "solar surplus")
    return result(suggested, "house full; " + funding + " available", round(available_w))
