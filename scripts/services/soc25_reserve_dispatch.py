#!/usr/bin/env python3
"""Process-local 25% house reserve: existing SoC recovery, no freeze/BMS writes.

Only the original installed ARMED dispatcher writes inverter registers. Site
env, original dispatcher source, inverter's 10% floor and EVCC remain intact.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

DISPATCH_DIR = Path("/opt/solinteg/app/scripts/services")
ROOT_LIB = Path("/usr/local/libexec/solinteg-soc25-reserve")
REQUIRED_ENV = {
    "SOLINTEG_SOC25_RESERVE_ENABLED": "1",
    "DISPATCH_LOOP_INTERVAL_S": "30",
}


def install_reserve(dispatch, policy) -> None:
    """Inject only recovery thresholds and charge budget into installed code."""
    if (not dispatch.ARMED or dispatch.SOC_FLOOR_PCT != 12
            or not 15 <= dispatch.BATTERY_KWH <= 15.1
            or dispatch.PLANNER_RESERVE_PCT != 20
            or dispatch.SOC_RECOVERY_ENTRY_PCT > 20
            or dispatch.SOC_RECOVERY_EXIT_PCT != 21
            or dispatch.SOC_FLOOR_RECOVERY_W != 300
            or dispatch.L3_CHARGE_GUARD_MODE != "1"
            or dispatch.LOOP_INTERVAL_S != 30):
        raise RuntimeError("installed dispatcher not in independently verified configuration")

    original_guard = dispatch.guard_l3_charge
    original_needs_apply = dispatch.needs_apply
    dispatch.SOC_RECOVERY_ENTRY_PCT = policy.RECOVERY_ENTRY_PCT
    dispatch.SOC_RECOVERY_EXIT_PCT = policy.RECOVERY_EXIT_PCT
    dispatch._soc_recovery_active = True

    def guard_l3_charge(action, power_w, detail, numbers, now):
        if action != "charge" or not numbers.get("soc_recovery"):
            return original_guard(action, power_w, detail, numbers, now)
        surplus = dispatch.read_live_solar_surplus_w(now)
        charge_w = policy.cap_recovery_charge(power_w, surplus)
        # The *original* guard still checks live phase CT/50006. Its 300W
        # fallback remains the only allowed high-power path on stale CT.
        previous_budget = dispatch.L3_CHARGE_BUDGET_W
        dispatch.L3_CHARGE_BUDGET_W = policy.RECOVERY_L3_BUDGET_W
        try:
            limited_w, detail, numbers, original_limited = original_guard(
                action, charge_w, detail, numbers, now)
        finally:
            dispatch.L3_CHARGE_BUDGET_W = previous_budget
        numbers["soc25_reserve_active"] = True
        numbers["soc25_reserve_entry_pct"] = policy.RECOVERY_ENTRY_PCT
        numbers["soc25_reserve_exit_pct"] = policy.RECOVERY_EXIT_PCT
        numbers["soc25_reserve_solar_surplus_w"] = (
            round(surplus) if surplus is not None else None)
        numbers["soc25_reserve_charge_cap_w"] = charge_w
        return (limited_w, detail, numbers,
                original_limited or limited_w != power_w)

    def needs_apply(target, last_target, action, power, due, *, soc_recovery=False):
        # An immediate downward reduction must bypass ordinary 250W deadband.
        if (last_target is not None and target[0] == last_target[0]
                and action == "charge" and last_target[1] == "charge"
                and power < last_target[2]):
            return True
        return original_needs_apply(
            target, last_target, action, power, due, soc_recovery=soc_recovery)

    dispatch.guard_l3_charge = guard_l3_charge
    dispatch.needs_apply = needs_apply


def main() -> None:
    if any(os.environ.get(k) != v for k, v in REQUIRED_ENV.items()):
        raise RuntimeError("reserve deployment process environment not authorized")
    sys.path.insert(0, str(DISPATCH_DIR))
    import dispatch_loop as dispatch
    if Path(dispatch.__file__).resolve() != DISPATCH_DIR / "dispatch_loop.py":
        raise RuntimeError("wrong installed dispatcher")
    # Copy immutable policy from root-owned library, not the public repo's
    # potentially changing checkout. State/watchdog pin its SHA256.
    sys.path.insert(0, str(ROOT_LIB))
    import soc25_reserve_policy as policy

    install_reserve(dispatch, policy)
    dispatch.main()


if __name__ == "__main__":
    main()
