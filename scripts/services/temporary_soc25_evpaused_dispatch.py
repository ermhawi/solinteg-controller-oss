#!/usr/bin/env python3
"""Isolated EV-paused 25% recovery pilot launcher; original ARMED source unchanged.

The original dispatcher remains the *only* inverter writer. Its 1500W recovery
request is reduced to 300W on stale or over-budget L3 observations. The pilot
watchdog restores the original dispatcher before this process reaches the 27%
release threshold (normal completion is at 26%). Nothing controls the EVCC.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

DISPATCH_DIR = Path("/opt/solinteg/app/scripts/services")
MIN_RECOVERY_W = 300
MAX_RECOVERY_W = 1500
L3_BUDGET_W = 2700
REQUIRED_ENV = {
    "SOLINTEG_TEMP_SOC25_EVPAUSED": "1",
    "DISPATCH_SOC_RECOVERY_EXIT_PCT": "27",
    "DISPATCH_SOC_FLOOR_RECOVERY_W": str(MAX_RECOVERY_W),
    "SOLINTEG_BATTERY_MAX_W": str(MAX_RECOVERY_W),
    "DISPATCH_L3_CHARGE_GUARD_MODE": "1",
    "DISPATCH_L3_CHARGE_BUDGET_W": str(L3_BUDGET_W),
    "DISPATCH_LOOP_INTERVAL_S": "30",
}


def validate_configuration(environ: dict) -> None:
    for name, value in REQUIRED_ENV.items():
        if environ.get(name) != value:
            raise RuntimeError(f"EV-paused recovery pilot process config mismatch: {name}")


def install_guard(dispatch) -> None:
    """Limit *all* charge actions using the existing tested L3 charge guard."""
    if (dispatch.SOC_FLOOR_RECOVERY_W != MAX_RECOVERY_W
            or dispatch.SOC_RECOVERY_EXIT_PCT != 27
            or dispatch.L3_CHARGE_GUARD_MODE != "1"
            or dispatch.L3_CHARGE_BUDGET_W != L3_BUDGET_W
            or dispatch.LOOP_INTERVAL_S != 30
            or not dispatch.ARMED):
        raise RuntimeError("installed dispatcher does not match physical trial constraints")

    original_guard = dispatch.guard_l3_charge
    original_needs_apply = dispatch.needs_apply

    def guard_l3_charge(action, power_w, detail, numbers, now):
        # The dispatcher is single-threaded. Restore the original recovery
        # floor even when phase checking throws: no persistent monkeypatch.
        previous = dispatch.SOC_FLOOR_RECOVERY_W
        dispatch.SOC_FLOOR_RECOVERY_W = MIN_RECOVERY_W
        try:
            limited_w, detail, numbers, limited = original_guard(
                action, power_w, detail, numbers, now)
        finally:
            dispatch.SOC_FLOOR_RECOVERY_W = previous
        if action == "charge":
            numbers["temp_evpaused_charge_max_w"] = MAX_RECOVERY_W
            numbers["temp_evpaused_charge_min_w"] = MIN_RECOVERY_W
            numbers["temp_evpaused_l3_limited"] = limited
            numbers["temp_evpaused_requested_w"] = power_w
            numbers["temp_evpaused_applied_w"] = limited_w
        return limited_w, detail, numbers, limited

    def needs_apply(target, last_target, effective_action, effective_power,
                    due_for_reassert, *, soc_recovery=False):
        # Respond to every downward charge adjustment, even within the
        # economic 250W deadband used by the ordinary dispatcher.
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
        raise RuntimeError("refusing to run a different dispatcher")
    install_guard(dispatch)
    dispatch.main()


if __name__ == "__main__":
    main()
