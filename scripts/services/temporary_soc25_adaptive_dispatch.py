#!/usr/bin/env python3
"""One-off 1.5 kW recovery launcher; the installed dispatcher file is unchanged.

Only the existing dispatcher's L3 charging guard is adjusted to use its
original 300 W safe fallback even when temporary SoC recovery requests
1500 W. All Modbus writes remain in the ordinary dispatcher; neither
this launcher nor the independent rollback watchdog opens a second
inverter connection. This is not a fuse-safety device.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

DISPATCH_DIR = Path("/opt/solinteg/app/scripts/services")
MIN_RECOVERY_W = 300
REQUIRED_ENV = {
    "SOLINTEG_TEMP_SOC25_ADAPTIVE": "1",
    "DISPATCH_SOC_RECOVERY_EXIT_PCT": "25",
    "DISPATCH_SOC_FLOOR_RECOVERY_W": "1500",
    "SOLINTEG_BATTERY_MAX_W": "1500",
    "DISPATCH_L3_CHARGE_GUARD_MODE": "1",
    "DISPATCH_L3_CHARGE_BUDGET_W": "3000",
    "DISPATCH_LOOP_INTERVAL_S": "30",
}


def validate_configuration(environ: dict) -> None:
    for name, expected in REQUIRED_ENV.items():
        if environ.get(name) != expected:
            raise RuntimeError(f"temporary recovery pilot config mismatch: {name}")


def install_adaptive_guard(dispatch) -> None:
    """Keep 1.5 kW SoC recovery but allow actual L3 guard to reduce to 300 W.

    The dispatcher is single-threaded. Temporarily substituting only the
    L3 guard's minimum allows its normal fresh-phase/headroom calculation
    to choose a smaller charge target. The original recovery minimum
    remains 1500 W everywhere else, including the SoC recovery decision.
    """
    if (dispatch.SOC_FLOOR_RECOVERY_W != 1500
            or dispatch.L3_CHARGE_GUARD_MODE != "1"
            or dispatch.L3_CHARGE_BUDGET_W != 3000
            or dispatch.LOOP_INTERVAL_S != 30):
        raise RuntimeError("dispatcher did not load the temporary L3 safety settings")

    original_guard = dispatch.guard_l3_charge
    original_needs_apply = dispatch.needs_apply

    def guard_l3_charge(action, power_w, detail, numbers, now):
        prior_minimum = dispatch.SOC_FLOOR_RECOVERY_W
        dispatch.SOC_FLOOR_RECOVERY_W = MIN_RECOVERY_W
        try:
            limited_w, detail, numbers, limited = original_guard(
                action, power_w, detail, numbers, now)
        finally:
            dispatch.SOC_FLOOR_RECOVERY_W = prior_minimum
        if action == "charge":
            numbers["temp_soc25_adaptive_charge_max_w"] = 1500
            numbers["temp_soc25_adaptive_guard_min_w"] = MIN_RECOVERY_W
            numbers["temp_soc25_adaptive_limited"] = limited
            numbers["temp_soc25_adaptive_requested_w"] = power_w
            numbers["temp_soc25_adaptive_applied_w"] = limited_w
        return limited_w, detail, numbers, limited

    def needs_apply(target, last_target, effective_action, effective_power,
                    due_for_reassert, *, soc_recovery=False):
        # Decreases must not wait for the dispatcher's normal 250 W
        # economic/noise deadband, especially near the L3 cutoff.
        if (last_target is not None and target[0] == last_target[0]
                and effective_action == "charge" and last_target[1] == "charge"
                and effective_power < last_target[2]):
            return True
        return original_needs_apply(
            target, last_target, effective_action, effective_power,
            due_for_reassert, soc_recovery=soc_recovery)

    dispatch.guard_l3_charge = guard_l3_charge
    dispatch.needs_apply = needs_apply


def main() -> None:
    validate_configuration(os.environ)
    sys.path.insert(0, str(DISPATCH_DIR))
    import dispatch_loop as dispatch

    if Path(dispatch.__file__).resolve() != DISPATCH_DIR / "dispatch_loop.py":
        raise RuntimeError("not using the already installed original dispatcher")
    if not dispatch.ARMED:
        raise RuntimeError("physical recovery trial requires the ordinary ARMED dispatcher")
    install_adaptive_guard(dispatch)
    dispatch.main()


if __name__ == "__main__":
    main()
