"""Economic coordination of the house battery with EV-like demand on house phase L3.

No EVCC/Waveshare connection and no fuse protection. Only the existing Solinteg
meter/inverter telemetry is used. This is intentionally a PURE policy module:
nothing here opens Modbus, writes registers, or decides whether to arm controls.

On this installation the inverter's "Grid Unbalanced Output" setting (register
50006) was READ as 0 on 2026-10-24. When the inverter supplies/absorbs power
symmetrically on its three AC phases, the power of the LOAD connected to L3 is:

    house_L3_W ~= -grid_meter_L3_W + inverter_total_AC_W/3

Unlike raw L3 grid import this sees an EV start even if battery discharge masks
part of it. It is an estimate, not a separate measurement of car charging, and
MUST NOT be applied if inverter output becomes unbalanced. The caller checks
50006 independently before using any suggestion to control the inverter.

High L3 demand is only a proxy for car charging. A large appliance on L3 may
produce the same signal; this policy is for economical BATTERY use, not for
person/fuse protection, and does not send commands to the vehicle.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping


@dataclass(frozen=True)
class L3Observation:
    house_l3_w: float
    grid_l3_w: float
    inverter_ac_w: float
    pv_surplus_w: float
    age_s: float


@dataclass(frozen=True)
class BatterySuggestion:
    action: str
    power_w: int
    reason: str
    metrics: dict


def _finite_number(raw) -> float:
    if isinstance(raw, bool):
        raise ValueError("boolean is not a physical power")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("non-finite power")
    return value


def observe_l3(reading: Mapping, now: datetime, *, max_age_s: int = 90) -> L3Observation | None:
    """Accept only fresh and self-consistent three-phase readings.

    A missing phase is UNKNOWN, not zero. Sum of the three grid CTs must agree
    with total meter power, and total house load must agree with inverter AC.
    """
    try:
        timestamp = datetime.fromisoformat(reading["timestamp"])
        age_s = (now - timestamp).total_seconds()
        if not -10 <= age_s <= max_age_s:
            return None
        l1 = _finite_number(reading["meter_l1_w"])
        l2 = _finite_number(reading["meter_l2_w"])
        l3 = _finite_number(reading["meter_l3_w"])
        grid = _finite_number(reading["grid_w"])
        inverter_ac = _finite_number(reading["inverter_ac_w"])
        house = _finite_number(reading["house_load_w"])
        pv = _finite_number(reading["pv_w"])
        if abs((l1 + l2 + l3) - grid) > max(80, abs(grid) * 0.05):
            return None
        if abs((inverter_ac - grid) - house) > max(150, abs(house) * 0.1):
            return None
        house_l3 = -l3 + inverter_ac / 3.0
        if house_l3 < -300 or house_l3 > 20000 or house < 0 or pv < 0:
            return None
        return L3Observation(
            house_l3_w=max(0.0, house_l3),
            grid_l3_w=l3,
            inverter_ac_w=inverter_ac,
            pv_surplus_w=max(0.0, pv - house),
            age_s=age_s,
        )
    except (KeyError, ValueError, TypeError, OverflowError):
        return None


def suggest_battery_target(
    action: str,
    power_w: int,
    observed: L3Observation,
    *,
    l3_charge_budget_w: int = 4000,
    ev_like_threshold_w: int = 2500,
    non_ev_baseline_w: int = 600,
    estimated_ev_max_w: int = 4300,
    min_forced_charge_w: int = 300,
    min_forced_discharge_w: int = 50,
) -> BatterySuggestion:
    """Recommend a *BATTERY* target, never a command to car/charger.

    For grid-funded battery charging, cap inverter battery charging so L3's
    estimated non-battery demand plus one third of battery charging approximately
    remains within an ECONOMIC phase budget. No fuse safety is implied.

    A sustained EV-like L3 load gets subtracted from normal forced discharge,
    so the grid supplies that incremental load instead of spending house-battery
    energy. Below the remaining target, keep a tiny forced-discharge setpoint
    rather than returning to Auto: Auto would immediately discharge into the EV.
    This is the established battery-save emulation, NOT a physical 0 W freeze.
    """
    original = (action, power_w)
    high_l3 = observed.house_l3_w >= ev_like_threshold_w
    estimated_extra = (
        min(estimated_ev_max_w, max(0, round(observed.house_l3_w - non_ev_baseline_w)))
        if high_l3 else 0
    )
    metrics = {
        "l3_house_est_w": round(observed.house_l3_w),
        "l3_grid_w": round(observed.grid_l3_w),
        "l3_meter_age_s": round(observed.age_s, 1),
        "l3_ev_like": high_l3,
        "l3_ev_like_component_w": estimated_extra,
        "l3_charge_budget_w": l3_charge_budget_w,
        "l3_original_action": action,
        "l3_original_power_w": power_w,
    }

    if action == "charge" and power_w > 0:
        # If the PV surplus ALONE covers the charge, it does not compete for grid
        # import. Do not waste solar in the name of EV coordination.
        if observed.pv_surplus_w < power_w:
            room_w = max(0.0, l3_charge_budget_w - observed.house_l3_w)
            max_charge_w = max(min_forced_charge_w, int(room_w * 3 // 100) * 100)
            if power_w > max_charge_w:
                suggestion = BatterySuggestion(
                    "charge", max_charge_w,
                    "reduce grid-funded battery charging to leave L3 for other loads",
                    metrics,
                )
                return suggestion
    elif action in ("discharge", "idle") and high_l3 and observed.pv_surplus_w < 100:
        if action == "idle":
            # With no PV, Auto would use the house battery to cover the EV.
            suggested = min_forced_discharge_w
        else:
            remaining_w = max(0, power_w - estimated_extra)
            suggested = max(min_forced_discharge_w, remaining_w // 100 * 100)
        if (action, suggested) != original:
            return BatterySuggestion(
                "discharge", suggested,
                "preserve house battery: supply EV-like incremental L3 load from grid",
                metrics,
            )
    return BatterySuggestion(action, power_w, "no L3 battery adjustment", metrics)
