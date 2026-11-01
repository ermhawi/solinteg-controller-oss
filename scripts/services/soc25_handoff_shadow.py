#!/usr/bin/env python3
"""Read-only 25% SoC recovery/Auto handoff hypothesis (no device control).

Counterfactual only: a validated, separately approved inverter actuator and
independent watchdog would be required before applying any candidate setpoint.
This module must not import Modbus, EVCC writers or the armed dispatcher.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

RESERVE_ENTRY_PCT = 25.0
RESERVE_EXIT_PCT = 26.0
RECOVERY_W = 300
MAX_PHASE_IMPORT_W = 3200
MAX_TOTAL_IMPORT_W = 6500
MAX_INVERTER_AGE_S = 60
MAX_EVCC_AGE_S = 65


@dataclass(frozen=True)
class Advice:
    state: str
    candidate_action: str | None
    candidate_power_w: int | None
    reason: str
    recovery_latched: bool
    evidence: dict

    def as_dict(self) -> dict:
        return asdict(self)


def _number(record: dict, key: str) -> float:
    value = record[key]
    if type(value) not in (float, int) or not math.isfinite(value):
        raise ValueError(f"missing or invalid {key}")
    return float(value)


def _age(now: datetime, timestamp: str) -> float:
    observed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if observed.tzinfo is None:
        raise ValueError("timestamp lacks timezone")
    return (now.astimezone(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()


def assess(live: dict, evcc: dict, action: str, power_w: int, *,
           now: datetime, recovery_latched: bool = False,
           phase_balance_assumed: bool = False) -> Advice:
    """Assess an EV-paused recovery handoff; every result is non-actuating.

    Hysteresis is a *candidate* for a separately watchdogged physical trial:
    enter at/below 25%; do not hand back to Auto before 26%. It may still
    cycle 25-26%; it is not a verified zero-power battery hold.
    """
    evidence = {
        "device_writes": 0,
        "physical_control_authorized": False,
        "phase_balance_assumed": phase_balance_assumed,
        "hysteresis_entry_pct": RESERVE_ENTRY_PCT,
        "hysteresis_exit_pct": RESERVE_EXIT_PCT,
    }

    def result(state: str, reason: str, *, latched: bool = recovery_latched,
               candidate: bool = False) -> Advice:
        return Advice(state, "charge" if candidate else None,
                      RECOVERY_W if candidate else None, reason, latched, evidence)

    if now.tzinfo is None or not isinstance(live, dict) or not isinstance(evcc, dict):
        return result("unavailable", "missing or untimed observations")
    if action not in ("idle", "charge", "discharge") or type(power_w) is not int or power_w < 0:
        return result("unavailable", "original action unknown")
    try:
        if not 0 <= _age(now, live["timestamp"]) <= MAX_INVERTER_AGE_S:
            raise ValueError("stale inverter observation")
        if not 0 <= _age(now, evcc["timestamp"]) <= MAX_EVCC_AGE_S:
            raise ValueError("stale EVCC observation")
        if (evcc.get("read_ok") is not True or evcc.get("state") != "B2"
                or evcc.get("configured_pwm_raw") != 1000
                or evcc.get("charging_detected") is not False
                or evcc.get("writes") != 0
                or not 0 <= _number(evcc, "measured_current_a") <= 1):
            raise ValueError("EVCC pause unverified; do not infer from a pilot register")
        soc = _number(live, "soc_pct")
        temp = _number(live, "battery_temp_c")
        pv = _number(live, "pv_w")
        battery = _number(live, "battery_w")
        house = _number(live, "house_load_w")
        grid = _number(live, "grid_w")
        ac = _number(live, "inverter_ac_w")
        phases = [_number(live, "meter_l" + n + "_w") for n in "123"]
        if not 0 <= soc <= 100 or not 5 <= temp <= 45 or pv < 0 or house < 0:
            raise ValueError("SoC, temperature or PV outside model bounds")
        if (not phase_balance_assumed
                and (type(live.get("grid_unbalanced_output_raw")) is not int
                     or live["grid_unbalanced_output_raw"] != 0)):
            raise ValueError("balanced phase output not verified")
        if (abs(sum(phases) - grid) > max(100, abs(grid) * .05)
                or abs(ac - grid - house) > max(150, house * .10)
                or abs(ac - pv - battery) > 500 + .10 * max(abs(ac), pv, abs(battery))):
            raise ValueError("inconsistent inverter and phase CT measurements")
    except (KeyError, ValueError, TypeError, OverflowError, AttributeError) as exc:
        return result("unavailable", str(exc)[:160])

    latched = (recovery_latched or soc <= RESERVE_ENTRY_PCT) and soc < RESERVE_EXIT_PCT
    evidence.update({"house_soc_pct": soc, "battery_w": battery, "pv_w": pv,
                     "original_action": action, "original_power_w": power_w})
    if not latched:
        return result("released", "above 26% or not yet within 25% reserve band",
                      latched=False)
    if action == "charge" and power_w >= RECOVERY_W:
        return result("existing_charge", "preserve existing verified recovery/charge",
                      latched=True)
    if pv - house > 350 and battery < -150:
        return result("pv_auto_unverified",
                      "observed PV surplus/charging; avoid suppressing PV before hold validation",
                      latched=True)

    # Worst-case grid increase when replacing observed battery discharge
    # with an additional 300 W charge. Equal thirds are an assumption in
    # historical replay; neither this nor 30 s CT sampling is fuse protection.
    added_import = max(0.0, battery) * .95 + RECOVERY_W
    proposed_phases = [-p + added_import / 3 for p in phases]
    proposed_total = -grid + added_import
    evidence["predicted_import_per_phase_w"] = [round(p) for p in proposed_phases]
    evidence["predicted_total_import_w"] = round(proposed_total)
    if (any(p >= MAX_PHASE_IMPORT_W for p in proposed_phases)
            or proposed_total >= MAX_TOTAL_IMPORT_W):
        return result("unsafe_import",
                      "predicted import exceeds conservative trial envelope",
                      latched=True)
    return result("shadow_candidate",
                  "avoid immediate Auto/discharge through 25-26% hysteresis; "
                  "300 W is not a verified retention actuator",
                  latched=True, candidate=True)
