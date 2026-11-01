"""Pure rules for a 25% house-battery reserve, without a physical hold.

Never changes BMS/inverter minimum-SoC registers, PV priority or EVCC state.
The installed ARMED dispatcher remains the only Modbus writer.
"""
from __future__ import annotations

import math

RECOVERY_ENTRY_PCT = 26.5
RECOVERY_EXIT_PCT = 28.0
MAX_GRID_RECOVERY_W = 1500
MAX_SOLAR_RECOVERY_W = 5500
MIN_RECOVERY_W = 300
RECOVERY_L3_BUDGET_W = 2700


def cap_recovery_charge(requested_w: int, solar_surplus_w: float | None) -> int:
    """Bound reserve recovery's grid-funded part, retaining PV-surplus capture.

    Request at least 1500W in the recovery band, but never grid-fund more
    than that on purpose. Live PV surplus may fund a larger charge.
    The original dispatcher still enforces its CT/L3 guard afterward.
    """
    if type(requested_w) is not int or requested_w < 0:
        raise ValueError("invalid charge request")
    if (type(solar_surplus_w) not in (float, int)
            or not math.isfinite(solar_surplus_w)
            or solar_surplus_w < 0):
        solar_surplus_w = 0.0
    solar_w = min(MAX_SOLAR_RECOVERY_W, int(solar_surplus_w // 100) * 100)
    return min(max(requested_w, MAX_GRID_RECOVERY_W), max(MAX_GRID_RECOVERY_W, solar_w))


def verified_evcc_safe(evcc: dict) -> str | None:
    """Distinguish unplugged A1 (no current sensor) from verified paused B2.

    A1 is NOT a measured zero-ampere current. It is an independently
    observed disconnected IEC 61851 state with no EV current samples.
    Fail closed on any other state, including C2 (actively charging).
    """
    if not isinstance(evcc, dict):
        return None
    if (evcc.get("source") != "waveshare_evcc_fc03_readonly"
            or evcc.get("read_ok") is not True
            or evcc.get("configured_pwm_raw") != 1000
            or evcc.get("paused_by_pwm") is not True
            or evcc.get("charging_detected") is not False
            or type(evcc.get("writes")) is not int or evcc["writes"] != 0):
        return None
    state = evcc.get("state")
    if state == "A1":
        currents = evcc.get("channel_currents_a")
        if (evcc.get("measured_current_a", "missing") is None
                and type(currents) is list and len(currents) == 3
                and all(v is None for v in currents)):
            return "disconnected"
        return None
    if state == "B2":
        amps = evcc.get("measured_current_a")
        if type(amps) in (int, float) and math.isfinite(amps) and 0 <= amps <= 1:
            return "paused"
    return None


def reserve_state(soc_pct: float, *, active: bool) -> bool:
    """Hysteresis: re-charge before 25%, release at 28%."""
    if type(soc_pct) not in (float, int) or not math.isfinite(soc_pct):
        raise ValueError("invalid SoC")
    if not 0 <= soc_pct <= 100:
        raise ValueError("SoC outside physical range")
    return soc_pct < RECOVERY_ENTRY_PCT or (active and soc_pct < RECOVERY_EXIT_PCT)
