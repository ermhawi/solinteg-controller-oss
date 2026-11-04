#!/usr/bin/env python3
"""SHA-locked additive deployment candidate for the installed read-only sidecar.

Never modifies service files, systemd, the root gate or any device.
The deployed legacy observer is intentionally older than the Git observer:
this preserves its exact bytes except one import and one isolated read-only
metered-policy field, rather than replacing it with newer unreleased code.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

BASELINE_SHA256 = "91b569847b2380304c038913cd0a68f915117edb663f04f01f5caae5596aeb79"
INSTALLED = Path(
    "/usr/local/libexec/solinteg-ev-grid-solar-shadow/ev_grid_priority_shadow_observer.py"
)
IMPORT_ANCHOR = "from ev_grid_priority_idle_strategy import evaluate as evaluate_idle\n"
RESULT_ANCHOR = (
    "    }\n"
    "    try:\n"
    "        try:\n"
    "            gate = json.loads(gate_path.read_text(encoding=\"utf-8\"))"
)
IMPORT_ADD = "from ev_l3_metered_shadow_observer import collect as collect_metered\n"
RESULT_ADD = (
    "    # Independent Ict2 shadow: retired root gate still controls legacy advice.\n"
    "    result[\"metered_policy\"] = collect_metered(\n"
    "        live_path, evcc_path, controls_path, now=now, last_applied=last_applied)\n"
)


def patch_text(source: str) -> str:
    if source.count(IMPORT_ANCHOR) != 1 or source.count(RESULT_ANCHOR) != 1:
        raise ValueError("unexpected installed observer structure")
    if IMPORT_ADD in source or RESULT_ADD in source:
        raise ValueError("already contains metered integration")
    result = source.replace(IMPORT_ANCHOR, IMPORT_ANCHOR + IMPORT_ADD, 1)
    result = result.replace(RESULT_ANCHOR, "    }\n" + RESULT_ADD
                            + RESULT_ANCHOR.removeprefix("    }\n"), 1)
    compile(result, str(INSTALLED), "exec")
    return result


def prepare(installed_bytes: bytes) -> bytes:
    if hashlib.sha256(installed_bytes).hexdigest() != BASELINE_SHA256:
        raise ValueError("installed observer drift: do not deploy")
    return patch_text(installed_bytes.decode("utf-8")).encode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed", type=Path, default=INSTALLED)
    args = parser.parse_args()
    original = args.installed.read_bytes()
    candidate = prepare(original)
    print("baseline_sha256", hashlib.sha256(original).hexdigest())
    print("candidate_sha256", hashlib.sha256(candidate).hexdigest())
    print("candidate_compiles", True, "filesystem_changed", False)


if __name__ == "__main__":
    main()
