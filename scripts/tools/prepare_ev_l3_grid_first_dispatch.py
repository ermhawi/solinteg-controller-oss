#!/usr/bin/env python3
"""SHA-pinned, read-only preparation of Dell's EV grid-priority apply-gate patch.

The installed dispatcher predates the repository's solar_charge action and
disallows discharge->charge within the same optimizer slot. This minimal
additive patch permits only EVCC-validated grid-first charge transitions;
ordinary economic plan changes and physical recovery retain their old gates.
No files or registers are written by this preparation script.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
from pathlib import Path

INSTALLED = Path("/opt/solinteg/app/scripts/services/dispatch_loop.py")
BASELINE_SHA256 = "979b6e3f51b7d2c6237a528fc00e739376357c0f14d684e2d238fd9a1bf52f10"

PATCHES = (
    (
        "def needs_apply(target, last_target, effective_action: str, effective_power: int,\n"
        "                due_for_reassert: bool, *, soc_recovery: bool = False) -> bool:",
        "def needs_apply(target, last_target, effective_action: str, effective_power: int,\n"
        "                due_for_reassert: bool, *, soc_recovery: bool = False,\n"
        "                ev_grid_priority_charge: bool = False) -> bool:",
    ),
    (
        "    if mid_slot_drift_to_charge and not soc_recovery:\n"
        "        return False",
        "    if mid_slot_drift_to_charge and not (soc_recovery or ev_grid_priority_charge):\n"
        "        return False",
    ),
    (
        "        and abs(effective_power - last_target[2]) < LIVE_LOAD_DEADBAND_W\n"
        "    )",
        "        and abs(effective_power - last_target[2]) < LIVE_LOAD_DEADBAND_W\n"
        "        and not (ev_grid_priority_charge and effective_action == \"charge\"\n"
        "                 and effective_power < last_target[2])\n"
        "    )",
    ),
    (
        "            if needs_apply(target, last_target, effective_action, effective_power, due_for_reassert,\n"
        "                           soc_recovery=bool(numbers.get(\"soc_recovery\"))):",
        "            if needs_apply(target, last_target, effective_action, effective_power, due_for_reassert,\n"
        "                           soc_recovery=bool(numbers.get(\"soc_recovery\")),\n"
        "                           ev_grid_priority_charge=numbers.get(\"ev_grid_live_priority_charge\") is True):",
    ),
)


def patch_text(original: str) -> str:
    if "ev_grid_priority_charge" in original:
        raise ValueError("dispatcher already modified: refuse another patch")
    result = original
    for before, after in PATCHES:
        if result.count(before) != 1 or after in result:
            raise ValueError("unexpected dispatcher structure: refuse patch")
        result = result.replace(before, after, 1)
    ast.parse(result)
    return result


def prepare(installed: bytes) -> bytes:
    if hashlib.sha256(installed).hexdigest() != BASELINE_SHA256:
        raise ValueError("installed dispatcher drift: refuse patch")
    return patch_text(installed.decode("utf-8")).encode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed", type=Path, default=INSTALLED)
    args = parser.parse_args()
    before = args.installed.read_bytes()
    after = prepare(before)
    print("baseline_sha256", hashlib.sha256(before).hexdigest())
    print("candidate_sha256", hashlib.sha256(after).hexdigest())
    print("candidate_compiles", True, "filesystem_changed", False)


if __name__ == "__main__":
    main()
