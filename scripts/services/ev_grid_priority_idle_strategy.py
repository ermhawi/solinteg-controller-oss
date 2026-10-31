#!/usr/bin/env python3
"""Non-actuating idle/Auto arbitration: preserve solar charging while examining EV battery use.

This file is NOT a dispatcher or a hardware recipe. In particular neither a
PV-first charge setpoint nor a low-discharge hold has been validated with PV
on the Sundsvall inverter; proposed modes are hypotheses for later testing.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

DISCHARGE_EVIDENCE_W = 500
CHARGE_EVIDENCE_W = -150
PV_NON_EV_HEADROOM_W = 350
PV_NON_EV_DEFICIT_W = -300
TOTAL_PV_HEADROOM_W = 350
SAMPLE_COUNT = 4
MIN_WINDOW_S = 70
MAX_GAP_S = 50
MAX_SAMPLE_AGE_S = 65
MIN_DISPATCH_SETTLE_S = 30
NIGHT_TESTED_BATTERY_SAVE_W = 50
BATTERY_TO_AC_EFF_ASSUMED = .95
MAX_PHASE_IMPORT_W = 3200
MAX_TOTAL_GRID_IMPORT_W = 6500


@dataclass(frozen=True)
class IdleStrategy:
    state: str
    hypothetical_mode: str | None
    reason: str
    measurements: dict

    def as_dict(self) -> dict:
        return asdict(self)


def _finite(value) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("missing or invalid evidence")
    return float(value)


def _stamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing sample timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("sample timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _sample_window(samples: list[dict], now: datetime, age_of_dispatch_s: float | None):
    """Verify four contiguous, fresh 6 A historian samples in the SAME dispatch slot.

    The historian has only a 3-channel current SUM. We use that here solely
    as evidence of *continuity*, never as independent proof of Ict2 or 50006.
    The live shadow guard separately verifies Ict2 and balanced phase output.
    """
    if (type(age_of_dispatch_s) not in (int, float)
            or not math.isfinite(age_of_dispatch_s)
            or not isinstance(samples, list) or len(samples) < SAMPLE_COUNT):
        return None
    try:
        ordered = sorted(samples, key=lambda r: _stamp(r["captured_at_utc"]))
        window = ordered[-SAMPLE_COUNT:]
        stamps = [_stamp(sample["captured_at_utc"]) for sample in window]
        gaps = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])]
        span = (stamps[-1] - stamps[0]).total_seconds()
        age = (now - stamps[-1]).total_seconds()
        if (not MIN_WINDOW_S <= span <= 180
                or not 0 <= age <= MAX_SAMPLE_AGE_S
                or not all(20 <= gap <= MAX_GAP_S for gap in gaps)
                or age_of_dispatch_s < span + MIN_DISPATCH_SETTLE_S):
            return None
        for s in window:
            if (s.get("inverter_fresh") != 1
                    or s.get("evcc_record_fresh") != 1
                    or s.get("evcc_read_ok") != 1
                    or s.get("evcc_state") != "C2"
                    or s.get("evcc_charging_detected") != 1
                    or s.get("evcc_6a_limit_verified") != 1):
                return None
            ev_total = _finite(s.get("evcc_measured_a"))
            if not 5.8 <= ev_total <= 6.7:
                return None
            if not (0 <= _finite(s.get("inverter_age_s")) <= 60
                    and 0 <= _finite(s.get("evcc_age_s")) <= 65):
                return None
            if not 25 <= _finite(s.get("house_soc_pct")) <= 95:
                return None
            phases = [_finite(s.get("l" + n + "_grid_w")) for n in "123"]
            grid = _finite(s.get("grid_w"))
            if abs(sum(phases) - grid) > max(100, abs(grid) * .05):
                return None
            if _finite(s.get("pv_w")) < 0 or _finite(s.get("house_load_w")) < 0:
                return None
        return window
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _headroom(sample: dict, ev_w: float) -> tuple[float, float]:
    pv = _finite(sample.get("pv_w"))
    house = _finite(sample.get("house_load_w"))
    # Estimated non-EV house load; this is a planning assumption, not
    # independent proof of PV battery funding or electric energy routing.
    non_ev_house = max(0, house - ev_w)
    return pv - house, pv - non_ev_house


def evaluate(action: str, live: dict, guard_advice, samples: list[dict], *,
             now: datetime, age_of_dispatch_s: float | None) -> IdleStrategy:
    """Return hypotheses only. Must never be used as a physical command.

    An EV is on L3, so a forced 50 W battery discharge during sunlight can
    suppress charging the HOUSE battery. Separate at least three cases:
    no solar available for non-EV house load, PV might fund a battery charge
    while the grid funds the EV, and PV supplies the entire house+EV.
    """
    empty = {
        "model_actionable": False,
        "physical_control_authorized": False,
        "device_writes": 0,
    }
    if action != "idle":
        return IdleStrategy("not_idle", None,
                            "original dispatch action is not idle", empty)
    if now.tzinfo is None:
        return IdleStrategy("unavailable", None, "timezone required", empty)
    if not isinstance(live, dict):
        return IdleStrategy("unavailable", None, "missing inverter observation", empty)
    if guard_advice.state == "soc_guard":
        return IdleStrategy("soc_recovery_priority", None,
                            "do not override low/high battery SoC", empty)
    # A logged idle action is not a guarantee that the user left the
    # inverter in Auto; never second-guess an intervening manual mode.
    if type(live.get("work_mode_raw")) is not int or live["work_mode_raw"] != 0x101:
        return IdleStrategy("dispatch_mode_mismatch", None,
                            "actual inverter working mode is not verified General/Auto",
                            empty)
    if (guard_advice.state not in ("solar_hold_unverified", "shadow_hypothesis")
            or guard_advice.measurements.get("phase_balance_assumed") is True):
        return IdleStrategy("unavailable", None,
                            "live inverter/EVCC data not independently verified", empty)
    try:
        metrics = guard_advice.measurements
        ev_w = _finite(metrics["ev_w_est"])
        battery = _finite(live.get("battery_w"))
        total_headroom, non_ev_headroom = _headroom(live, ev_w)
        verified = _sample_window(samples, now.astimezone(timezone.utc),
                                  age_of_dispatch_s)
        stable_deficit = (verified is not None
                          and all(_finite(s.get("battery_w")) > DISCHARGE_EVIDENCE_W
                                  and _headroom(s, ev_w)[1] <= PV_NON_EV_DEFICIT_W
                                  for s in verified))
        stable_solar = (verified is not None
                        and all(_headroom(s, ev_w)[0] >= TOTAL_PV_HEADROOM_W
                                for s in verified))
        stable_non_ev_solar = (verified is not None
                               and all(_headroom(s, ev_w)[1] >= PV_NON_EV_HEADROOM_W
                                       for s in verified))
        evidence = dict(empty, pv_w=round(_finite(live.get("pv_w"))),
                        battery_w=round(battery), estimated_ev_w=round(ev_w),
                        total_house_solar_headroom_w=round(total_headroom),
                        non_ev_house_solar_headroom_w=round(non_ev_headroom),
                        history_window_verified=verified is not None,
                        stable_deficit=stable_deficit,
                        stable_total_solar_surplus=stable_solar,
                        stable_non_ev_solar_surplus=stable_non_ev_solar,
                        estimated_non_ev_house_load_w=round(
                            max(0, _finite(live.get("house_load_w")) - ev_w)),
                        battery_to_ev_causality_proven=False)
        if battery <= CHARGE_EVIDENCE_W:
            return IdleStrategy("observed_battery_charging", "keep_auto",
                                "battery is charging; preserve Auto, source attribution unknown",
                                evidence)
        if total_headroom >= TOTAL_PV_HEADROOM_W:
            if not stable_solar:
                return IdleStrategy("await_solar_stability", None,
                                    "PV appears to cover house+EV; await stable observations",
                                    evidence)
            if battery > DISCHARGE_EVIDENCE_W:
                return IdleStrategy("solar_surplus_discharge_anomaly", None,
                                    "battery still discharges despite whole-house PV surplus",
                                    evidence)
            return IdleStrategy("solar_surplus_keep_auto", "keep_auto",
                                "sustained whole-house PV surplus; allow normal solar charging",
                                evidence)
        if non_ev_headroom >= PV_NON_EV_HEADROOM_W:
            if not stable_non_ev_solar:
                return IdleStrategy("await_solar_stability", None,
                                    "PV may fund house battery; await sustained readings",
                                    evidence)
            # Holding at +50 W could prevent solar energy that should first
            # charge the house battery. Forcing a PV charge is NOT proven:
            # power-source allocation must be tested on this exact unit.
            return IdleStrategy("pv_first_charge_unverified", None,
                                "PV may cover non-EV loads and charge house battery; "
                                "a solar-priority charge mode is not hardware-verified",
                                dict(evidence, candidate_pv_charge_upper_bound_w=(
                                    max(0, int((non_ev_headroom - PV_NON_EV_HEADROOM_W)
                                               // 100) * 100))))
        if non_ev_headroom <= PV_NON_EV_DEFICIT_W and battery > DISCHARGE_EVIDENCE_W:
            if not stable_deficit:
                return IdleStrategy("await_deficit_stability", None,
                                    "avoid switching on one battery/solar transient",
                                    evidence)
            # Read-only projection: reducing battery AC output moves import
            # to ALL THREE phases. A total-grid cap does not protect L3.
            phase_import = [
                -_finite(live.get("meter_l" + n + "_w"))
                + max(0, battery - NIGHT_TESTED_BATTERY_SAVE_W)
                * BATTERY_TO_AC_EFF_ASSUMED / 3
                for n in "123"
            ]
            total_import = (
                -_finite(live.get("grid_w"))
                + max(0, battery - NIGHT_TESTED_BATTERY_SAVE_W)
                * BATTERY_TO_AC_EFF_ASSUMED
            )
            evidence["projected_phase_import_w"] = [round(p) for p in phase_import]
            evidence["projected_total_import_w"] = round(total_import)
            if (any(p > MAX_PHASE_IMPORT_W for p in phase_import)
                    or total_import > MAX_TOTAL_GRID_IMPORT_W):
                return IdleStrategy("projected_grid_import_high", None,
                                    "50 W shadow move would exceed provisional phase/import bounds",
                                    evidence)
            return IdleStrategy("battery_save_candidate", "tested_50w_battery_save",
                                "sustained EV + non-EV solar deficit: shadow candidate only; "
                                "50 W target still causes residual discharge",
                                dict(evidence,
                                     hypothetical_battery_save_target_w=NIGHT_TESTED_BATTERY_SAVE_W))
        if battery > DISCHARGE_EVIDENCE_W:
            return IdleStrategy("solar_boundary_uncertain", None,
                                "solar near non-EV demand; do not chase changing PV",
                                evidence)
        return IdleStrategy("auto_no_significant_discharge", "keep_auto",
                            "no sustained battery discharge to correct", evidence)
    except (KeyError, ValueError, TypeError, OverflowError):
        return IdleStrategy("unavailable", None, "inconsistent solar or battery samples", empty)
