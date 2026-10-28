#!/usr/bin/env python3
"""Time-limited economic guard: keep the EV load on L3 supplied by the grid.

PURE decision core, no Modbus/EVCC network. Reads only existing snapshot JSON.
Never changes charger, car, battery registers or fuse protection itself.
For balanced inverter output (grid-unbalanced-output register 50006 == 0),
a total battery discharge target supplies roughly one third of its AC output
to each phase; subtracting EV watts once from total battery watts does NOT
remove EV supply on L3. Cap inverter AC L3 contribution to estimated non-EV
house load, with a conservative measurement margin.

Physical activation is gated by a ROOT-owned, time-limited trial manifest.
Absent/expired/malformed manifest -> original decision unchanged. No fallback
to unverified "0 A" when a charging sample is stale. Independent watchdog
must restore the original running dispatcher before expiration.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
TRIAL_PATH = Path("/opt/solinteg/ev-grid-priority-trial.json")
MAX_TRIAL_SECONDS = 900
MAX_EV_AGE_SECONDS = 65
MAX_INVERTER_AGE_SECONDS = 60
EV_VOLTAGE_ASSUMED = 230.0
OUTPUT_TO_BATTERY_EFF_ESTIMATE = 0.95
L3_IMPORT_HARD_TEST_BOUND_W = 3200.0
GRID_IMPORT_HARD_TEST_BOUND_W = 6500.0
L3_EV_GRID_MARGIN_W = 180.0
MIN_CONTROL_DISCHARGE_W = 50
MIN_HOUSE_SOC_PCT = 25.0
MIN_BATTERY_TEMP_C = 10.0
MAX_BATTERY_TEMP_C = 45.0


@dataclass(frozen=True)
class Recommendation:
    state: str
    action: str
    power_w: int
    reason: str
    measurements: dict


def _finite(record: dict, key: str) -> float:
    v = record[key]
    if type(v) not in (int, float) or not math.isfinite(v):
        raise ValueError("invalid or missing power evidence")
    return float(v)


def _stamp(value: object) -> datetime:
    if not isinstance(value,str):
        raise ValueError("timestamp missing")
    stamp = datetime.fromisoformat(value.replace("Z","+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("timezone missing")
    return stamp.astimezone(UTC)


def validate_gate(gate: dict | None, now: datetime) -> str:
    if not isinstance(gate,dict) or set(gate) != {
        "schema_version", "mode", "started_utc", "expires_utc",
        "trial_id", "max_duration_seconds"}:
        return "off"
    if (type(gate["schema_version"]) is not int or gate["schema_version"] != 1
            or gate["mode"] not in ("shadow","active")
            or gate["max_duration_seconds"] != MAX_TRIAL_SECONDS
            or gate["trial_id"] != "ev-grid-priority-2026-10-28"):
        return "off"
    try:
        started = _stamp(gate["started_utc"])
        expires = _stamp(gate["expires_utc"])
    except (ValueError, TypeError):
        return "off"
    now = now.astimezone(UTC)
    if not (started <= now < expires
            and timedelta(seconds=5) < expires-started <= timedelta(seconds=MAX_TRIAL_SECONDS)):
        return "off"
    return gate["mode"]


def suggest(action: str, power_w: int, live: dict | None, evcc: dict | None,
            *, now: datetime, gate: dict | None) -> Recommendation:
    mode = validate_gate(gate, now)
    if mode == "off":
        return Recommendation("off",action,power_w,"trial not armed or expired",{})
    if action != "discharge" or type(power_w) is not int or power_w < 0:
        return Recommendation("not_applicable",action,power_w,"only reduces forced discharge",{})
    if not isinstance(live,dict) or not isinstance(evcc,dict):
        return Recommendation("unavailable",action,power_w,"missing independent measurements",{})
    try:
        lt = _stamp(live["timestamp"])
        et = _stamp(evcc["timestamp"])
        if not (-5 <= (now-lt).total_seconds() <= MAX_INVERTER_AGE_SECONDS
                and -5 <= (now-et).total_seconds() <= MAX_EV_AGE_SECONDS):
            raise ValueError("stale inverter or EVCC")
        if (type(live.get("grid_unbalanced_output_raw")) is not int
                or live["grid_unbalanced_output_raw"] != 0):
            raise ValueError("balanced inverter output not confirmed")
        if (evcc.get("source") != "waveshare_evcc_fc03_readonly"
                or evcc.get("read_ok") is not True
                or evcc.get("state") != "C2"
                or evcc.get("charging_detected") is not True
                or evcc.get("physical_control_eligible") is not False
                or evcc.get("writes") != 0
                or evcc.get("pilot_pwm_raw") != 100
                or evcc.get("configured_pwm_raw") != 100):
            raise ValueError("EVCC charging state/pilot unverified")
        currents = evcc.get("channel_currents_a")
        if not isinstance(currents,list) or len(currents)!=3:
            raise ValueError("missing phase currents")
        amps = [_number for _number in currents]
        if any(type(a) not in (int,float) or not math.isfinite(a) for a in amps):
            raise ValueError("invalid EVCC amps")
        if (not 5.0 <= amps[1] <= 6.5
                or not 0 <= amps[0] <= .5 or not 0 <= amps[2] <= .5
                or abs(_finite(evcc,"measured_current_a") - sum(amps)) > .21):
            raise ValueError("EVCC 6A Ict2 verification failed")
        soc = _finite(live,"soc_pct")
        temp = _finite(live,"battery_temp_c")
        pv = _finite(live,"pv_w")
        if not (MIN_HOUSE_SOC_PCT <= soc <= 95
                and MIN_BATTERY_TEMP_C <= temp <= MAX_BATTERY_TEMP_C
                and 0 <= pv <= 100):
            raise ValueError("battery SoC/temperature/PV outside night trial bounds")
        l1 = _finite(live,"meter_l1_w")
        l2 = _finite(live,"meter_l2_w")
        l3 = _finite(live,"meter_l3_w")
        grid = _finite(live,"grid_w")
        ac = _finite(live,"inverter_ac_w")
        house = _finite(live,"house_load_w")
        if (abs(l1+l2+l3-grid) > max(80,abs(grid)*.05)
                or abs(ac-grid-house) > max(150,abs(house)*.10)
                or grid < -GRID_IMPORT_HARD_TEST_BOUND_W
                or ac < -200 or house <= 0 or ac > 12000):
            raise ValueError("inconsistent inverter/phase meter readings")
        ev_w = amps[1]*EV_VOLTAGE_ASSUMED
        non_ev_l3 = -l3 + ac/3 - ev_w
        if not -300 <= non_ev_l3 <= 10000:
            raise ValueError("L3/EV balance outside plausible range")
        # Never over-allocate inverter phase-L3 contribution to EV demand.
        allowed_phase_battery_ac_w = max(0.0,non_ev_l3-L3_EV_GRID_MARGIN_W)
        allowed_battery_dc_w = (allowed_phase_battery_ac_w * 3 /
                                OUTPUT_TO_BATTERY_EFF_ESTIMATE)
        target = max(MIN_CONTROL_DISCHARGE_W,
                     (int(allowed_battery_dc_w)//100)*100)
        target = min(power_w,target)
        # Predicted import assuming balanced inverter reduction. Fixed minimum
        # 50W setpoint may yield a bit more inverter power; conservatively cap.
        remaining_ac = min(max(0,ac),target*OUTPUT_TO_BATTERY_EFF_ESTIMATE)
        predicted_l3_import = -l3+max(0,ac-remaining_ac)/3.0
        predicted_total_import = -grid+max(0,ac-remaining_ac)
        if (predicted_l3_import > L3_IMPORT_HARD_TEST_BOUND_W
                or predicted_total_import > GRID_IMPORT_HARD_TEST_BOUND_W):
            raise ValueError("predicted grid import exceeds conservative trial envelope")
    except (KeyError,TypeError,ValueError,OverflowError) as exc:
        return Recommendation("unavailable",action,power_w,str(exc)[:140],{})
    metrics = {
        "ev_grid_trial_mode":mode,
        "ev_grid_trial_ict2_a":round(amps[1],2),
        "ev_grid_trial_ev_w_est":round(ev_w),
        "ev_grid_trial_non_ev_l3_w_est":round(non_ev_l3),
        "ev_grid_trial_predicted_l3_import_w":round(predicted_l3_import),
        "ev_grid_trial_predicted_total_grid_import_w":round(predicted_total_import),
        "ev_grid_trial_original_target_w":power_w,
        "ev_grid_trial_target_w":target,
        "ev_grid_trial_inverter_soc_pct":soc,
        "ev_grid_trial_inverter_battery_temp_c":temp,
        "ev_grid_trial_source_age_s":round(max((now-lt).total_seconds(),
                                               (now-et).total_seconds()),1)
    }
    return Recommendation("active" if mode=="active" else "shadow",
        "discharge",target,
        "L3 EV supply prioritized from grid; preserve battery for estimated non-EV L3 demand",
        metrics)


def evaluate_snapshot(action: str, power_w: int, now: datetime,
                      live_path: Path, evcc_path: Path, gate_path: Path=TRIAL_PATH
                      ) -> Recommendation:
    """Use existing immutable-ish disk snapshots only, never any hardware API."""
    try:
        if not gate_path.is_file() or gate_path.is_symlink():
            return Recommendation("off",action,power_w,"no regular gate file",{})
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
        if validate_gate(gate,now)=="off":
            return Recommendation("off",action,power_w,"trial not valid",{})
        live=json.loads(live_path.read_text(encoding="utf-8"))
        evcc=json.loads(evcc_path.read_text(encoding="utf-8"))
    except (OSError,UnicodeError,json.JSONDecodeError,ValueError):
        return Recommendation("unavailable",action,power_w,"source snapshots unavailable",{})
    return suggest(action,power_w,live,evcc,now=now,gate=gate)


def guard_dispatch(action: str, power_w: int, expected_soc_kwh, solar_skip: bool,
                   detail: str, numbers: dict, now: datetime,
                   *, live_path: Path, evcc_path: Path,
                   gate_path: Path=TRIAL_PATH):
    """A single OPTIONAL, reducing-only hook in the existing ARMED dispatcher."""
    if numbers.get("soc_recovery"):
        return action,power_w,expected_soc_kwh,solar_skip,detail,numbers
    result=evaluate_snapshot(action,power_w,now,live_path,evcc_path,gate_path)
    if result.state in ("active","shadow"):
        numbers.update(result.measurements)
        numbers["ev_grid_trial_status"]=result.state
    elif result.state=="unavailable":
        numbers["ev_grid_trial_status"]="unavailable"
        numbers["ev_grid_trial_reason"]=result.reason
    if (result.state != "active" or result.power_w >= power_w
            or action != "discharge"):
        return action,power_w,expected_soc_kwh,solar_skip,detail,numbers
    return "discharge",result.power_w,None,False,(
        f"EV L3 trial cap {power_w}->{result.power_w}W; {detail}"),numbers
