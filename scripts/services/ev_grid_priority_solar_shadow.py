#!/usr/bin/env python3
"""Read-only solar-aware EV/L3 counterfactuals; never returns an executable command.

The production EV guard and its checksum-bound watchdog are deliberately separate.
A sunny AUTO/idle slot cannot safely be replaced with a forced discharge: doing so
could prevent the house battery from capturing surplus solar production.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

UTC = timezone.utc
EV_VOLTAGE_ASSUMED = 230.0
BATTERY_TO_AC_EFF_ASSUMED = .95
MIN_CONTROL_DISCHARGE_W = 50
HOUSE_L3_MARGIN_W = 180
MAX_AGE_S = 90
MAX_PHASE_IMPORT_W = 3200
MAX_GRID_IMPORT_W = 6500


@dataclass(frozen=True)
class ShadowAdvice:
    state: str
    candidate_action: str | None
    candidate_power_w: int | None
    reason: str
    measurements: dict

    def as_dict(self) -> dict:
        return asdict(self)


def _number(data: dict, key: str) -> float:
    value = data[key]
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("invalid or missing " + key)
    return float(value)


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise ValueError("timestamp without timezone")
    return date.astimezone(UTC)


def assess(action: str, power_w: int, live: dict, evcc: dict, *,
           now: datetime, phase_assumed: bool = False) -> ShadowAdvice:
    """Compute a non-actuating L3 scenario using only read-only measurements.

    phase_assumed=True is only for replaying historic records that did not
    record 50006. Those counterfactuals can never authorize hardware control.
    """
    empty = ShadowAdvice("unavailable", None, None, "invalid input", {})
    if not isinstance(live, dict) or not isinstance(evcc, dict):
        return empty
    if now.tzinfo is None or type(power_w) is not int or power_w < 0:
        return empty
    now = now.astimezone(UTC)
    try:
        if not all(-5 <= (now - _timestamp(s["timestamp"])).total_seconds() <= MAX_AGE_S
                   for s in (live, evcc)):
            raise ValueError("stale snapshots")
        if not phase_assumed and (type(live.get("grid_unbalanced_output_raw")) is not int
                                  or live["grid_unbalanced_output_raw"] != 0):
            raise ValueError("balanced inverter output not verified")
        if (evcc.get("source") != "waveshare_evcc_fc03_readonly"
                or evcc.get("read_ok") is not True
                or evcc.get("state") != "C2"
                or evcc.get("charging_detected") is not True
                or evcc.get("physical_control_eligible") is not False
                or evcc.get("writes") != 0
                or evcc.get("configured_pwm_raw") != 100
                or evcc.get("pilot_pwm_raw") != 100):
            raise ValueError("EVCC 6 A charging evidence missing")
        amps = evcc.get("channel_currents_a")
        if (type(amps) is not list or len(amps) != 3
                or not all(type(a) in (int, float) and math.isfinite(a)
                           for a in amps)
                or not 5 <= amps[1] <= 6.5
                or not 0 <= amps[0] <= .5
                or not 0 <= amps[2] <= .5
                or abs(sum(amps) - _number(evcc, "measured_current_a")) > .21):
            raise ValueError("EVCC Ict2 phase current unknown")
        soc = _number(live, "soc_pct")
        pv = _number(live, "pv_w")
        battery = _number(live, "battery_w")
        ac = _number(live, "inverter_ac_w")
        grid = _number(live, "grid_w")
        house = _number(live, "house_load_w")
        phases = [_number(live, "meter_l" + n + "_w") for n in ("1", "2", "3")]
        if (not 0 <= soc <= 100 or not 0 <= pv <= 6500
                or not -12000 <= battery <= 12000
                or not -300 <= ac <= 12000 or house < 0
                or abs(sum(phases) - grid) > max(100, abs(grid) * .05)
                or abs(ac - grid - house) > max(150, abs(house) * .10)
                or abs(ac - (pv + battery)) > 500 + .10 * max(abs(ac), pv, abs(battery))):
            raise ValueError("inconsistent CT/PV/battery/AC balance")
        ev_w = amps[1] * EV_VOLTAGE_ASSUMED
        # 50006=0 means balanced AC output, NOT independent RMS phase protection.
        load_l3 = -phases[2] + ac / 3
        non_ev_l3 = load_l3 - ev_w
        if not -300 <= non_ev_l3 <= 10000:
            raise ValueError("L3 non-EV load out of range")
        # Conservative PV-first allocation to non-EV house load; not proof
        # of the actual battery/PV mixing inside the inverter.
        solar_l3 = min(pv, max(ac, 0)) / 3
        allowed_battery_l3 = max(0, non_ev_l3 - solar_l3 - HOUSE_L3_MARGIN_W)
        cap = max(MIN_CONTROL_DISCHARGE_W,
                  int(allowed_battery_l3 * 3 / BATTERY_TO_AC_EFF_ASSUMED // 100) * 100)
        observed_discharge = battery > 500
        metrics = {
            "phase_balance_assumed": phase_assumed,
            "model_actionable": False,
            "physical_writes": 0,
            "ev_ict2_a": round(amps[1], 2),
            "ev_w_est": round(ev_w),
            "pv_w": round(pv),
            "battery_w": round(battery),
            "house_soc_pct": round(soc, 1),
            "observed_discharge_during_ev": observed_discharge,
            "battery_to_ev_causality_proven": False,
            "l3_non_ev_load_w_est": round(non_ev_l3),
            "l3_solar_contribution_w_est": round(solar_l3),
            "hypothetical_battery_discharge_cap_w": cap,
        }
        if action not in ("idle", "discharge", "charge"):
            return ShadowAdvice("unknown_dispatch", None, None,
                                "latest applied dispatch action not known", metrics)
        if action == "charge":
            return ShadowAdvice("existing_charge", None, None,
                                "preserve existing charge/recovery decision", metrics)
        if soc < 25 or soc > 95:
            return ShadowAdvice("soc_guard", None, None,
                                "preserve SoC recovery or upper battery bounds", metrics)
        if action == "idle" and pv > 100:
            return ShadowAdvice("solar_hold_unverified", None, None,
                                "AUTO may discharge; 50 W forced discharge could block solar charging",
                                metrics)
        candidate = min(power_w, cap) if action == "discharge" else MIN_CONTROL_DISCHARGE_W
        # Estimate grid import change against MEASURED, not requested,
        # battery discharge; this is not a current/thermal fuse calculation.
        delta_ac = max(0, battery - candidate) * BATTERY_TO_AC_EFF_ASSUMED
        predicted_phases = [-p + delta_ac / 3 for p in phases]
        predicted_total = -grid + delta_ac
        metrics["predicted_grid_import_per_phase_w"] = [
            round(p) for p in predicted_phases]
        metrics["predicted_total_grid_import_w"] = round(predicted_total)
        if (any(p > MAX_PHASE_IMPORT_W for p in predicted_phases)
                or predicted_total > MAX_GRID_IMPORT_W):
            return ShadowAdvice("phase_import_bound", None, None,
                                "predicted phase/grid import exceeds conservative envelope", metrics)
        state = "historical_hypothesis" if phase_assumed else "shadow_hypothesis"
        if action == "idle":
            reason = "night AUTO could instead use tested 50 W battery-save mode"
        else:
            reason = "PV-first estimate only; physical discharge change is unverified"
        return ShadowAdvice(state, "discharge", candidate, reason, metrics)
    except (TypeError, ValueError, KeyError, OverflowError) as exc:
        return ShadowAdvice("unavailable", None, None, str(exc)[:140], {})
