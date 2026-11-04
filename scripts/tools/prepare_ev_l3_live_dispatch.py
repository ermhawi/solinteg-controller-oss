#!/usr/bin/env python3
"""Prepare SHA-locked additive live EV policy patch for installed Dell dispatcher.

Deliberately retains the entire installed dispatcher, its broker transactions,
physical SoC recovery, normal failure scenario and exact original code paths.
Only an opt-in post-recovery hook is added before the existing L3 charge guard.
This program never modifies live files or performs physical writes.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

INSTALLED = Path("/opt/solinteg/app/scripts/services/dispatch_loop.py")
BASELINE_SHA256 = "34d0a927d970a1de343a16dd22d2c13fb6d1eb43d665dc5ec4bce8d740b84e57"
PATH_ANCHOR = "from datetime import datetime, timedelta\n"
PATH_IMPORT = "from pathlib import Path\n"
IMPORT_ANCHOR = "from l3_charge_guard import observe_l3_house_w, cap_grid_charge_w\n"
IMPORT_ADD = "from ev_l3_live_dispatch import coordinate as coordinate_ev_grid_live\n"
HOOK_ANCHOR = (
    "            power_w, detail, numbers, l3_limited = guard_l3_charge(\n"
    "                action, power_w, detail, numbers, iteration_now)"
)
HOOK_ADD = (
    "            # Verified EVCC 6A + solar capture; same broker writer and SoC recovery.\n"
    "            try:\n"
    "                action, power_w, expected_soc_kwh, solar_skip, detail, numbers = coordinate_ev_grid_live(\n"
    "                    action, power_w, expected_soc_kwh, solar_skip, detail, numbers, iteration_now,\n"
    "                    live_path=Path(LIVE_JSON_PATH), read_json=common.read_json)\n"
    "            except Exception as exc:  # noqa: BLE001\n"
    "                log.warning(\"EV/L3 live coordination unavailable (%s) — original dispatch retained\", exc)\n"
)


def patch_text(source: str) -> str:
    if (source.count(PATH_ANCHOR) != 1 or source.count(IMPORT_ANCHOR) != 1
            or source.count(HOOK_ANCHOR) != 1
            or PATH_IMPORT in source or IMPORT_ADD in source or HOOK_ADD in source):
        raise ValueError("unexpected installed dispatcher structure")
    patched = source.replace(PATH_ANCHOR, PATH_ANCHOR + PATH_IMPORT, 1)
    patched = patched.replace(IMPORT_ANCHOR, IMPORT_ANCHOR + IMPORT_ADD, 1)
    patched = patched.replace(HOOK_ANCHOR, HOOK_ADD + HOOK_ANCHOR, 1)
    compile(patched, str(INSTALLED), "exec")
    return patched


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
