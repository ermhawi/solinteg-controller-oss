"""EVCC-Ict2 based L3 battery coordination: pure, non-actuating shadow policy.

The EVSE reports measured current per channel (0.1 A resolution); it does
NOT meter real power. 230 V and PF≈1 are assumptions, not exact EV watts.
Never treat this economic estimate as phase-fuse or thermal protection.

No EVCC/Modbus drivers, file access, control commands or activation gates.
The existing physical 6 A EV guard is deliberately NOT changed here.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

MAX_EV_AGE_S = 65
MAX_INVERTER_AGE_S = 60
EV_NOMINAL_V = 230.0
EV_CURRENT_MIN_A = 5.0
EV_CURRENT_MAX_A = 18.5
EV_PILOT_MIN_A = 5.9
EV_PILOT_MAX_A = 18.1
EV_OTHER_PHASE_MAX_A = 0.5
EV_CURRENT_SUM_TOLERANCE_A = 0.21
EV_PILOT_MARGIN_A = 0.5
PV_SHADOW_LIMIT_W = 100
SOC_MIN_PCT = 25
SOC_MAX_PCT = 95
BATT_TEMP_MIN_C = 10
BATT_TEMP_MAX_C = 45
MIN_DISCHARGE_W = 50
NON_EV_L3_MARGIN_W = 180
BATTERY_TO_AC_EFF_ASSUMED = 0.95
MAX_PHASE_IMPORT_W = 3200
MAX_TOTAL_IMPORT_W = 6500


@dataclass(frozen=True)
class MeteredAdvice:
    state: str
    candidate_action: str | None
    candidate_power_w: int | None
    reason: str
    measurements: dict

    def as_dict(self) -> dict:
        return asdict(self)


def _finite(data: dict, key: str) -> float:
    value = data[key]
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("missing or invalid " + key)
    return float(value)


def _age(data: dict, now: datetime, max_age: int) -> float:
    value = data["timestamp"]
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("timezone missing")
    age = (now - timestamp.astimezone(timezone.utc)).total_seconds()
    if not -5 <= age <= max_age:
        raise ValueError("stale measurement")
    return age


def assess(action: str, power_w: int, live: dict, evcc: dict, *,
           now: datetime) -> MeteredAdvice:
    """Compute *advisory* battery discharge reduction from measured EVCC Ict2.

    A positive result is a counterfactual, never an executable setpoint.
    At night only, model balanced inverter AC contribution per phase, subtract
    EVCC-measured car demand from inferred house L3 demand, and retain the
    estimated non-EV load plus a conservative margin. Daylight/idle needs
    separate solar-aware and temporal-stability validation before any release.
    """
    base = {"model_actionable": False, "physical_control_authorized": False,
            "device_writes": 0, "ev_power_metered": False,
            "ev_nominal_voltage_v": EV_NOMINAL_V, "ev_power_factor_assumed": 1.0}
    def result(state: str, reason: str, *, candidate: int | None = None) -> MeteredAdvice:
        return MeteredAdvice(state, "discharge" if candidate is not None else None,
                             candidate, reason, dict(base))

    if (not isinstance(live, dict) or not isinstance(evcc, dict)
            or not isinstance(now, datetime) or now.tzinfo is None
            or not isinstance(action, str) or type(power_w) is not int or power_w < 0):
        return result("unavailable", "invalid inputs")
    now = now.astimezone(timezone.utc)
    try:
        ev_age = _age(evcc, now, MAX_EV_AGE_S)
        inv_age = _age(live, now, MAX_INVERTER_AGE_S)
        base["source_age_s"] = round(max(ev_age, inv_age), 1)
        if (evcc.get("source") != "waveshare_evcc_fc03_readonly"
                or evcc.get("read_ok") is not True
                or evcc.get("physical_control_eligible") is not False
                or type(evcc.get("writes")) is not int or evcc["writes"] != 0):
            raise ValueError("unverified EVCC FC03 source")
        if evcc.get("state") != "C2" or evcc.get("charging_detected") is not True:
            if evcc.get("state") in ("A1", "A2", "B1", "B2") and (
                    evcc.get("charging_detected") is False):
                return result("no_verified_charging",
                              "EVCC reports no charging; current is unknown, not 0 A")
            raise ValueError("EVCC current or charging state unknown")

        config = evcc.get("configured_pwm_raw")
        pilot = evcc.get("pilot_pwm_raw")
        if (type(config) is not int or type(pilot) is not int
                or config != pilot or not EV_PILOT_MIN_A <= config * .06 <= EV_PILOT_MAX_A):
            raise ValueError("EVCC pilot mismatch, pause or unsupported limit")
        limit_a = config * .06
        channels = evcc.get("channel_currents_a")
        if (type(channels) is not list or len(channels) != 3
                or any(type(a) not in (int, float) or not math.isfinite(a)
                       for a in channels)):
            raise ValueError("EVCC phase currents unknown")
        i1, ict2, i3 = channels
        if (not 0 <= i1 <= EV_OTHER_PHASE_MAX_A
                or not 0 <= i3 <= EV_OTHER_PHASE_MAX_A
                or not EV_CURRENT_MIN_A <= ict2 <= EV_CURRENT_MAX_A
                or ict2 > limit_a + EV_PILOT_MARGIN_A
                or abs(sum(channels) - _finite(evcc, "measured_current_a"))
                   > EV_CURRENT_SUM_TOLERANCE_A):
            raise ValueError("EVCC Ict2/channel current inconsistent")
        ev_w = ict2 * EV_NOMINAL_V
        base.update(ev_ict2_a=round(ict2, 2),
                    ev_configured_limit_a=round(limit_a, 2),
                    ev_power_w_est=round(ev_w),
                    ev_power_source="measured_ict2_x_nominal_230v_pf_assumed",
                    ev_current_resolution_a=0.1)

        if (type(live.get("grid_unbalanced_output_raw")) is not int
                or live["grid_unbalanced_output_raw"] != 0):
            raise ValueError("balanced inverter output not verified")
        phases = [_finite(live, "meter_l" + n + "_w") for n in "123"]
        grid = _finite(live, "grid_w")
        ac = _finite(live, "inverter_ac_w")
        house = _finite(live, "house_load_w")
        pv = _finite(live, "pv_w")
        battery = _finite(live, "battery_w")
        soc = _finite(live, "soc_pct")
        temp = _finite(live, "battery_temp_c")
        if (abs(sum(phases) - grid) > max(80, abs(grid) * .05)
                or abs(ac - grid - house) > max(150, abs(house) * .10)
                or abs(ac - (pv + battery))
                   > 500 + .10 * max(abs(ac), pv, abs(battery))
                or not 0 <= pv <= 6500 or not -12000 <= battery <= 12000
                or not -300 <= ac <= 12000 or house < 0
                or not 0 <= soc <= 100):
            raise ValueError("inverter/phase/energy measurements inconsistent")
        house_l3 = -phases[2] + ac / 3
        non_ev_l3 = house_l3 - ev_w
        if not -300 <= non_ev_l3 <= 10000:
            raise ValueError("EV current incompatible with L3 power")
        base.update(house_l3_w_est=round(house_l3),
                    non_ev_l3_w_est=round(non_ev_l3),
                    pv_w=round(pv), soc_pct=round(soc, 1),
                    battery_w=round(battery),
                    battery_to_ev_causality_proven=False,
                    phase_fuse_protection_verified=False)

        if action in ("charge", "solar_charge", "freeze", "reserve_hold"):
            return result("preserve_existing_action", "do not override charge or holds")
        if not SOC_MIN_PCT <= soc <= SOC_MAX_PCT or not BATT_TEMP_MIN_C <= temp <= BATT_TEMP_MAX_C:
            return result("soc_or_bms_guard", "battery reserve/temperature takes priority")
        if pv > PV_SHADOW_LIMIT_W:
            return result("solar_unverified",
                          "solar may supply EV or house battery; do not force discharge")
        if action == "idle":
            return result("idle_stability_unverified",
                          "Auto-to-50W change needs independent stable idle/EV history")
        if action != "discharge":
            return result("unknown_action", "unsupported dispatch action")
        if power_w <= MIN_DISCHARGE_W:
            return result("already_limited", "planned discharge is already at minimum")

        available_l3 = max(0., non_ev_l3 - NON_EV_L3_MARGIN_W)
        cap = max(MIN_DISCHARGE_W,
                  int(available_l3 * 3 / BATTERY_TO_AC_EFF_ASSUMED // 100) * 100)
        cap = min(power_w, cap)
        delta_ac = max(0., battery - cap) * BATTERY_TO_AC_EFF_ASSUMED
        predicted_phase = [-x + delta_ac / 3 for x in phases]
        predicted_total = -grid + delta_ac
        base.update(estimated_battery_cap_w=cap,
                    estimated_battery_reduction_w=power_w - cap,
                    predicted_phase_import_w=[round(v) for v in predicted_phase],
                    predicted_total_import_w=round(predicted_total))
        if (any(v > MAX_PHASE_IMPORT_W for v in predicted_phase)
                or predicted_total > MAX_TOTAL_IMPORT_W):
            return result("phase_import_bound",
                          "projected import exceeds provisional economic envelope")
        if cap >= power_w:
            return result("no_reduction_needed", "non-EV L3 load permits existing target")
        return result("shadow_hypothesis",
                      "hypothetical battery cap based on EVCC Ict2 and L3 balance",
                      candidate=cap)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        return result("unavailable", str(exc)[:140])
