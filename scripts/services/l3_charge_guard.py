"""Conservative charge-side L3 budget using Solinteg's own phase measurements.

No EVCC, Waveshare, Volvo writes or fuse-safety guarantees. The separate
three-phase inverter's "Grid Unbalanced Output" register 50006 MUST read 0
before the caller may use the equal-third phase power estimate.

On missing/stale/inconsistent CT telemetry or unknown balance, a planned
high-power GRID charge falls back to a 300 W charge (not auto/self-use):
this avoids an unbounded grid import while preserving SoC recovery.
Actual fuse protection remains the installation's circuit breaker.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Mapping

# Use conservative headroom below a nominal 20 A x 230 V L3 circuit.
DEFAULT_L3_BUDGET_W = 4000
MIN_RECOVERY_CHARGE_W = 300
ONE_WAY_EFF = math.sqrt(0.96) * 0.97


def _number(value) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a power reading")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite power")
    return number


def observe_l3_house_w(reading: Mapping | None, now: datetime, *, max_age_s: int = 90) -> float | None:
    """Estimate L3 load, only from fresh and internally consistent three-phase CT data."""
    if reading is None:
        return None
    try:
        when = datetime.fromisoformat(reading["timestamp"])
        age = (now - when).total_seconds()
        if not -10 <= age <= max_age_s:
            return None
        l1 = _number(reading["meter_l1_w"])
        l2 = _number(reading["meter_l2_w"])
        l3 = _number(reading["meter_l3_w"])
        grid = _number(reading["grid_w"])
        inverter_ac = _number(reading["inverter_ac_w"])
        house = _number(reading["house_load_w"])
        pv = _number(reading["pv_w"])
        if house < 0 or pv < 0:
            return None
        if abs(l1 + l2 + l3 - grid) > max(80, abs(grid) * 0.05):
            return None
        if abs(inverter_ac - grid - house) > max(150, house * 0.1):
            return None
        estimated_l3 = -l3 + inverter_ac / 3.0
        if not -300 <= estimated_l3 <= 20000:
            return None
        return max(0.0, estimated_l3)
    except (KeyError, ValueError, TypeError, OverflowError):
        return None


def cap_grid_charge_w(
    requested_w: int,
    *,
    house_l3_w: float | None,
    balanced_output: bool,
    budget_w: int = DEFAULT_L3_BUDGET_W,
    efficiency: float = ONE_WAY_EFF,
    min_charge_w: int = MIN_RECOVERY_CHARGE_W,
) -> tuple[int, str]:
    """Limit *additional* planned battery charging within a conservative L3 budget.

    The estimate ignores the EV-versus-appliance distinction (both compete on L3).
    The minimum 300 W preserves the existing low-SoC safety recovery path.
    A high-power setpoint is never allowed from untrusted phase data.
    """
    if requested_w <= min_charge_w:
        return requested_w, "low-power recovery; no grid-charge boost"
    if not balanced_output or house_l3_w is None:
        return min_charge_w, "L3 phase telemetry or inverter balance unverified"
    if not (math.isfinite(house_l3_w) and 0 < efficiency <= 1):
        return min_charge_w, "L3 readings invalid"
    headroom_w = max(0.0, budget_w - house_l3_w)
    max_charge = max(min_charge_w, int(headroom_w * 3 * efficiency // 100) * 100)
    limited = min(requested_w, max_charge)
    return limited, (
        f"L3 estimated load {house_l3_w:.0f} W, economic budget {budget_w} W, "
        f"battery request {requested_w} -> {limited} W"
    )
