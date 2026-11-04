"""Bounded *live* EV/L3 solar capture through the existing battery dispatcher.

Only when measured 6 A EV charging and fresh balanced three-phase telemetry
exist. PV capture tracks the AC output that would otherwise offset the EV's
L3 import, instead of using a fixed PV-surplus threshold. The old
SoC recovery and Modbus broker
remain the sole actuator and failure owner. No EVSE writes or hardware drivers.
This is NOT a certified EV power-source attribution or fuse protection.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

EV_WINDOW_S = 55
INVERTER_WINDOW_S = 55
EV_NOMINAL_V = 230
EV_CURRENT_LOW = 5.7
EV_CURRENT_HIGH = 6.4
PV_RESPONSE_MARGIN_W = 100
MIN_SOLAR_CHARGE_W = 300
MAX_SOLAR_CHARGE_W = 3500
MAX_PHASE_IMPORT_W = 3000
MAX_SITE_IMPORT_W = 6000
EV_L3_LOCAL_MARGIN_W = 90
BATTERY_AC_EFF_EST = 0.95
MIN_EMS_DISCHARGE_W = 50
BMS_TEMP_LOW_C = 10
BMS_TEMP_HIGH_C = 45


@dataclass(frozen=True)
class EvLiveDecision:
    state: str
    action: str | None
    power_w: int | None
    reason: str
    metrics: dict

    def as_dict(self) -> dict:
        return asdict(self)


def _number(d: dict, key: str) -> float:
    v = d[key]
    if type(v) not in (int, float) or not math.isfinite(v):
        raise ValueError("invalid " + key)
    return float(v)


def _age(d: dict, now: datetime, limit: int) -> float:
    stamp = d["timestamp"]
    if type(stamp) is not str:
        raise ValueError("invalid timestamp")
    date = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise ValueError("timezone required")
    age = (now - date.astimezone(timezone.utc)).total_seconds()
    if not -5 <= age <= limit:
        raise ValueError("stale")
    return age


def evaluate(action: str, power_w: int, live: dict, evcc: dict, *,
             now: datetime, soc_recovery: bool = False,
             solar_skip: bool = False) -> EvLiveDecision:
    base = {"device_writes": 0, "evcc_writes": 0, "physical_fuse_protection": False}
    def result(state: str, reason: str, candidate: int | None = None,
               candidate_action: str = "charge") -> EvLiveDecision:
        return EvLiveDecision(state, candidate_action if candidate is not None else None,
                              candidate, reason, dict(base))
    if (not isinstance(live, dict) or not isinstance(evcc, dict)
            or not isinstance(now, datetime) or now.tzinfo is None
            or type(power_w) is not int or power_w < 0
            or type(solar_skip) is not bool
            or action not in ("idle", "discharge", "charge", "solar_charge", "freeze", "reserve_hold")):
        return result("unavailable", "invalid inputs")
    if action in ("solar_charge", "freeze", "reserve_hold") or soc_recovery:
        return result("preserve_existing_action", "existing solar capture, recovery or hold takes priority")
    now = now.astimezone(timezone.utc)
    try:
        ages = (_age(live, now, INVERTER_WINDOW_S), _age(evcc, now, EV_WINDOW_S))
        base["source_age_s"] = round(max(ages), 1)
        if (evcc.get("source") != "waveshare_evcc_fc03_readonly"
                or evcc.get("read_ok") is not True
                or evcc.get("state") != "C2"
                or evcc.get("charging_detected") is not True
                or evcc.get("physical_control_eligible") is not False
                or type(evcc.get("writes")) is not int or evcc["writes"] != 0
                or type(evcc.get("configured_pwm_raw")) is not int
                or evcc["configured_pwm_raw"] != 100
                or evcc.get("pilot_pwm_raw") != 100):
            raise ValueError("unverified 6A EVCC pilot")
        phases_a = evcc.get("channel_currents_a")
        if (type(phases_a) is not list or len(phases_a) != 3
                or any(type(v) not in (int,float) or not math.isfinite(v) for v in phases_a)):
            raise ValueError("missing EVCC phase currents")
        if (not 0 <= phases_a[0] <= .5
                or not EV_CURRENT_LOW <= phases_a[1] <= EV_CURRENT_HIGH
                or not 0 <= phases_a[2] <= .5
                or abs(sum(phases_a) - _number(evcc, "measured_current_a")) > .21):
            raise ValueError("unexpected EVCC phase loading")
        if (type(live.get("grid_unbalanced_output_raw")) is not int
                or live["grid_unbalanced_output_raw"] != 0
                or live.get("work_mode_raw") not in (0x101, 0x303)):
            raise ValueError("inverter mode/phase balance unverified")
        soc = _number(live, "soc_pct")
        temp = _number(live, "battery_temp_c")
        if not 16 <= soc <= 95 or not BMS_TEMP_LOW_C <= temp <= BMS_TEMP_HIGH_C:
            return result("soc_or_bms_guard", "battery SoC or temperature out of live trial bounds")
        phases = [_number(live, "meter_l" + p + "_w") for p in "123"]
        grid = _number(live, "grid_w")
        ac = _number(live, "inverter_ac_w")
        house = _number(live, "house_load_w")
        pv = _number(live, "pv_w")
        battery = _number(live, "battery_w")
        if (not 0 <= pv <= 6500 or not -12000 <= battery <= 12000
                or not -300 <= ac <= 12000 or house <= 0
                or abs(sum(phases) - grid) > max(80, abs(grid)*.05)
                or abs(ac - grid - house) > max(150, abs(house)*.10)
                or abs(ac - (pv + battery)) > 500 + .10*max(abs(ac), pv, abs(battery))):
            raise ValueError("inconsistent CT / inverter power")
        ev_w = phases_a[1]*EV_NOMINAL_V
        non_ev_house_w = max(0.0, house - ev_w)
        house_l3_w = -phases[2] + ac / 3
        non_ev_l3_w = house_l3_w - ev_w
        if non_ev_l3_w < -300:
            raise ValueError("EVCC Ict2 disagrees with measured L3 house demand")
        solar_headroom_w = pv - non_ev_house_w
        grid_l3_import_w = max(0, -phases[2])
        base.update(ev_ict2_a=round(phases_a[1],2), ev_w_est=round(ev_w),
                    pv_w=round(pv), total_house_w=round(house),
                    non_ev_house_w_est=round(non_ev_house_w),
                    non_ev_l3_w_est=round(non_ev_l3_w),
                    solar_headroom_w_est=round(solar_headroom_w),
                    grid_l3_import_w=round(grid_l3_import_w),
                    ev_grid_target_w_est=round(ev_w),
                    ev_grid_target_gap_w_est=round(max(0, ev_w-grid_l3_import_w)),
                    soc_pct=round(soc,1))
        # With balanced inverter output every 3 W of AC generation adds about
        # 1 W on L3. A transient heater on L3 may temporarily make the EV look
        # completely grid-fed, but stopping battery PV capture immediately
        # sends that AC power back to the EV when the heater switches off.
        # Keep capturing solar PV while the EV charges, regardless of transient
        # non-EV phase demand. This can shift L1/L2 house demand to the GRID.
        # Never request more than the current PV with a 100 W reaction reserve.
        allowed_ac_w = 3 * max(0, non_ev_l3_w - EV_L3_LOCAL_MARGIN_W)
        pv_charge_budget_w = max(0, pv - PV_RESPONSE_MARGIN_W)
        target = min(MAX_SOLAR_CHARGE_W,
                     int(pv_charge_budget_w * BATTERY_AC_EFF_EST // 100) * 100)
        base.update(desired_inverter_ac_max_w=round(allowed_ac_w),
                    pv_only_charge_budget_w=round(pv_charge_budget_w),
                    grid_first_charge_target_w=target,
                    target_limited_by_charge_cap=target == MAX_SOLAR_CHARGE_W)
        if target < MIN_SOLAR_CHARGE_W:
            # The dispatch planner's raw house load includes EV. Discharge that
            # follows that load may power the car. Limit battery AC output to
            # the *non-EV* L3 demand; PV has first claim on that budget.
            if action == "discharge" and power_w > MIN_EMS_DISCHARGE_W:
                allowed_inverter_ac = 3 * max(0, non_ev_l3_w - EV_L3_LOCAL_MARGIN_W)
                battery_cap_w = max(MIN_EMS_DISCHARGE_W,
                                    int(max(0, allowed_inverter_ac - pv)
                                        / BATTERY_AC_EFF_EST // 100) * 100)
                battery_cap_w = min(power_w, battery_cap_w)
                reduced_ac = max(0, battery-battery_cap_w) * BATTERY_AC_EFF_EST
                phase_import = [-p + reduced_ac/3 for p in phases]
                total_import = -grid + reduced_ac
                base.update(discharge_cap_w=battery_cap_w,
                            predicted_phase_import_w=[round(x) for x in phase_import],
                            predicted_total_import_w=round(total_import))
                if (battery_cap_w < power_w
                        and all(x <= MAX_PHASE_IMPORT_W for x in phase_import)
                        and total_import <= MAX_SITE_IMPORT_W):
                    return result("grid_priority_discharge_cap",
                                  "restrict battery discharge to non-EV demand on L3",
                                  battery_cap_w, "discharge")
            return result("insufficient_verified_solar",
                          "insufficient solar for forced charging; original dispatch retained")
        if action == "charge" and power_w >= target and not solar_skip:
            return result("preserve_existing_action",
                          "planned battery charging already absorbs the PV target")
        # Conservative upper bound on added site import if today's battery
        # discharge turns into this battery charge. Normal house/EV load is
        # NOT subtracted from the import/fuse measurements.
        delta_ac_w = max(0., battery + target) * 1.2
        estimated_phase_import = [-p + delta_ac_w/3 for p in phases]
        estimated_total_import = -grid + delta_ac_w
        base.update(target_battery_charge_w=target,
                    predicted_phase_import_w=[round(x) for x in estimated_phase_import],
                    predicted_total_import_w=round(estimated_total_import))
        if (any(x > MAX_PHASE_IMPORT_W for x in estimated_phase_import)
                or estimated_total_import > MAX_SITE_IMPORT_W):
            return result("import_guard", "projected import exceeds trial envelope")
        return result("grid_first_pv_capture",
                      "capture PV in house battery to minimize balanced inverter output on EV L3",
                      target)
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        return result("unavailable", str(exc)[:140])
