"""Single-owner dispatcher adapter for bounded live EV solar capture.

No inverter or EVCC driver is imported. The pre-existing dispatcher remains
the only process allowed to submit charge setpoints through the Modbus broker.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from ev_l3_live_policy import evaluate

EVCC_PATH = Path("/opt/solinteg/ev-shadow-runtime/var/evcc-readonly.json")


def coordinate(action: str, power_w: int, expected_soc_kwh,
               solar_skip: bool, detail: str, numbers: dict, now: datetime,
               *, live_path: Path, read_json, evcc_path: Path = EVCC_PATH):
    if os.environ.get("DISPATCH_EV_GRID_LIVE", "0") != "1":
        return action, power_w, expected_soc_kwh, solar_skip, detail, numbers
    live = read_json(str(live_path))
    evcc = read_json(str(evcc_path))
    advice = evaluate(action, power_w, live, evcc, now=now,
                      soc_recovery=numbers.get("soc_recovery") is True,
                      solar_skip=solar_skip)
    numbers["ev_grid_live_status"] = advice.state
    numbers["ev_grid_live_ev_w_est"] = advice.metrics.get("ev_w_est")
    numbers["ev_grid_live_non_ev_house_w"] = advice.metrics.get("non_ev_house_w_est")
    numbers["ev_grid_live_grid_target_gap_w"] = advice.metrics.get("ev_grid_target_gap_w_est")
    numbers["ev_grid_live_pv_headroom_w"] = advice.metrics.get("solar_headroom_w_est")
    numbers["ev_grid_live_allowed_ac_w"] = advice.metrics.get("desired_inverter_ac_max_w")
    if advice.power_w is None:
        return action, power_w, expected_soc_kwh, solar_skip, detail, numbers
    if advice.action == "discharge" and action == "discharge" and advice.power_w < power_w:
        numbers["ev_grid_live_discharge_cap_w"] = advice.power_w
        return ("discharge", advice.power_w, None, False,
                f"EV/L3 grid-first: cap discharge for non-EV L3 demand to {advice.power_w} W; {detail}",
                numbers)
    if advice.action != "charge":
        return action, power_w, expected_soc_kwh, solar_skip, detail, numbers
    numbers["ev_grid_live_charge_target_w"] = advice.power_w
    # Tagged independently from SoC recovery: the live PV-driven transition
    # may replace a mid-slot 50 W battery-save decision, but ordinary planner
    # charge reversals remain protected by the existing write gate.
    numbers["ev_grid_live_priority_charge"] = True
    return ("charge", advice.power_w, None, False,
            f"EV/L3 grid-first PV capture {advice.power_w} W, inverter AC L3 limited; {detail}",
            numbers)
