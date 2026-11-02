#!/usr/bin/env python3
"""Read-only solar capture assessment during a verified battery-save dispatch.

This is a counterfactual, not an Auto/EMS actuator. In particular, returning
to Auto at low SoC can cause renewed discharge when solar production falls.
No Modbus, systemd or independent safety-supervisor functionality lives here.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

MIN_SOLAR_SURPLUS_W = 400
MIN_GRID_EXPORT_W = 250
MAX_OBSERVATION_AGE_S = 60
MAX_HISTORY_AGE_S = 65
MIN_SETTLE_S = 30
SOC_BUFFER_PCT = 0.5


@dataclass(frozen=True)
class SolarSaveAdvice:
    state: str
    hypothetical_mode: str | None
    reason: str
    evidence: dict

    def as_dict(self) -> dict:
        return asdict(self)


def _number(value) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("invalid measurement")
    return float(value)


def _stamp(value) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing time")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("timezone required")
    return stamp.astimezone(timezone.utc)


def _flows(record: dict, *, historical: bool) -> tuple[float, float, float, float]:
    pv = _number(record.get("pv_w"))
    house = _number(record.get("house_load_w"))
    grid = _number(record.get("grid_w"))
    battery = _number(record.get("battery_w"))
    phases = [_number(record.get(("l" + p + "_grid_w") if historical
                                 else ("meter_l" + p + "_w"))) for p in "123"]
    if (pv < 0 or house < 0
            or abs(sum(phases) - grid) > max(100, abs(grid) * .05)
            or abs((pv + battery - house) - grid) > max(350, pv * .12)):
        raise ValueError("inconsistent PV, battery or phase CT")
    return pv, house, grid, battery


def _history_is_stable(samples: list[dict], now: datetime, dispatch_age_s: float,
                       soc_floor: float) -> bool:
    if not isinstance(samples, list) or len(samples) < 4:
        return False
    try:
        recent = sorted(samples, key=lambda s: _stamp(s["captured_at_utc"]))[-4:]
        times = [_stamp(s["captured_at_utc"]) for s in recent]
        inverter_times = [_stamp(s["inverter_at_utc"]) for s in recent]
        gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
        if (not 70 <= (times[-1] - times[0]).total_seconds() <= 180
                or not 0 <= (now - times[-1]).total_seconds() <= MAX_HISTORY_AGE_S
                or not all(20 <= gap <= 50 for gap in gaps)
                or dispatch_age_s < (times[-1] - times[0]).total_seconds() + MIN_SETTLE_S):
            return False
        # A timestamp updated by the historian is not proof the inverter was
        # polled again. Require distinct, advancing original inverter timestamps.
        if not all(b > a for a, b in zip(inverter_times, inverter_times[1:])):
            return False
        for s, captured, measured in zip(recent, times, inverter_times):
            if (type(s.get("inverter_fresh")) is not int
                    or s["inverter_fresh"] != 1
                    or not 0 <= _number(s.get("inverter_age_s")) <= MAX_OBSERVATION_AGE_S
                    or not 0 <= (captured - measured).total_seconds() <= MAX_OBSERVATION_AGE_S
                    or _number(s.get("house_soc_pct")) < soc_floor):
                return False
            pv, house, grid, battery = _flows(s, historical=True)
            if (pv - house < MIN_SOLAR_SURPLUS_W or grid < MIN_GRID_EXPORT_W
                    or battery < -150 or battery > 500):
                return False
        return True
    except (ValueError, KeyError, TypeError, OverflowError, AttributeError):
        return False


def assess(action: str, power_w: int, live: dict, samples: list[dict], *,
           now: datetime, dispatch_age_s: float | None,
           battery_save_confirmed: bool,
           planner_reserve_pct: float | None,
           recovery_entry_pct: float | None,
           recovery_exit_pct: float | None) -> SolarSaveAdvice:
    """Assess a *known applied* battery-save target with fresh read-only data.

    No decision here permits a mode transition. Recovery/physical phase guards
    and an independently verified sun-to-Auto handoff are still required.
    """
    empty = {"model_actionable": False, "physical_control_authorized": False,
             "device_writes": 0, "pv_to_battery_causality_proven": False}

    def result(state: str, reason: str, evidence: dict | None = None,
               hypothetical: str | None = None) -> SolarSaveAdvice:
        return SolarSaveAdvice(state, hypothetical, reason,
                               dict(empty) if evidence is None else evidence)

    if (action != "discharge" or type(power_w) is not int or power_w <= 0
            or battery_save_confirmed is not True):
        return result("not_verified_save", "applied battery-save provenance missing")
    if not isinstance(now, datetime) or now.utcoffset() is None:
        return result("unavailable", "untimed evaluation")
    try:
        entry = _number(recovery_entry_pct)
        exit_pct = _number(recovery_exit_pct)
        reserve = _number(planner_reserve_pct)
    except (ValueError, TypeError, OverflowError):
        return result("thresholds_unverified", "site reserve/recovery limits not supplied")
    if not 0 <= entry < exit_pct < 98 or not 0 <= reserve < 98:
        return result("thresholds_unverified", "invalid site reserve/recovery limits")
    floor = max(reserve, exit_pct) + SOC_BUFFER_PCT
    try:
        if (not isinstance(live, dict) or type(live.get("work_mode_raw")) is not int
                or live["work_mode_raw"] != 0x303
                or type(live.get("grid_unbalanced_output_raw")) is not int
                or live["grid_unbalanced_output_raw"] != 0):
            return result("mode_unverified", "physical EMS/balanced mode not verified")
        if not 0 <= (now - _stamp(live["timestamp"])).total_seconds() <= MAX_OBSERVATION_AGE_S:
            raise ValueError("stale inverter data")
        age = _number(dispatch_age_s)
        if not 0 <= age <= 900:
            raise ValueError("stale applied dispatch")
        soc = _number(live.get("soc_pct"))
        if not 0 <= soc <= 100:
            raise ValueError("invalid SoC")
        pv, house, grid, battery = _flows(live, historical=False)
        ac = _number(live.get("inverter_ac_w"))
        if abs(ac - grid - house) > max(150, house * .10):
            raise ValueError("inconsistent AC balance")
    except (ValueError, KeyError, TypeError, OverflowError, AttributeError) as exc:
        return result("unavailable", str(exc)[:120])

    evidence = dict(empty, soc_pct=soc, soc_floor_pct=round(floor, 2),
                    pv_w=round(pv), house_load_w=round(house),
                    grid_export_w=round(grid), battery_w=round(battery),
                    measured_pv_surplus_w=round(pv - house),
                    history_window_verified=False)
    if soc < floor:
        return result("reserve_priority", "SoC below reserve/recovery buffer", evidence)
    if soc >= 98:
        return result("high_soc", "near upper battery SoC bound", evidence)
    if battery < -150:
        return result("already_charging", "preserve observed charging, source unproven", evidence)
    if battery > 500 and pv - house >= MIN_SOLAR_SURPLUS_W:
        return result("solar_discharge_anomaly", "battery discharges despite PV surplus", evidence)
    if pv - house < MIN_SOLAR_SURPLUS_W or grid < MIN_GRID_EXPORT_W:
        return result("no_stable_solar_opportunity", "no verified live solar/export margin", evidence)
    if not _history_is_stable(samples, now.astimezone(timezone.utc), age, floor):
        return result("await_solar_stability",
                      "require four distinct, quality-checked inverter observations", evidence)
    evidence["history_window_verified"] = True
    return result("solar_capture_candidate",
                  "hypothetical PV-capture handoff; Auto discharge safety NOT proven",
                  evidence, "review_pv_capture_handoff")
