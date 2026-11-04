#!/usr/bin/env python3
"""Prepare a SHA-pinned, log-only EV/L3 shadow hook for the active Dell dispatcher.

This utility never installs a candidate, restarts a service, creates a gate
or writes an inverter/EVCC register. The original physical control tuple is
preserved even when the optional read-only observation fails.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path

LIVE_DISPATCH = Path("/opt/solinteg/app/scripts/services/dispatch_loop.py")
VERIFIED_SHA256 = "34d0a927d970a1de343a16dd22d2c13fb6d1eb43d665dc5ec4bce8d740b84e57"
CALL_ANCHOR = "            power_w, detail, numbers, l3_limited = guard_l3_charge(\n"
HOOK = (
    "            # Passive EV/L3 evidence only: never alters action or power_w.\n"
    "            if os.environ.get(\"DISPATCH_EV_L3_SHADOW_LOG\", \"0\") == \"1\":\n"
    "                try:\n"
    "                    from ev_l3_metered_policy import assess\n"
    "                    _ev_l3_shadow = assess(\n"
    "                        action, power_w, common.read_json(LIVE_JSON_PATH),\n"
    "                        common.read_json(\"/opt/solinteg/ev-shadow-runtime/var/evcc-readonly.json\"),\n"
    "                        now=iteration_now,\n"
    "                    )\n"
    "                    log.info(\"EV/L3 metered shadow (no actuation): %s; Ict2=%s A; EV~%s W; cap=%s W\",\n"
    "                             _ev_l3_shadow.state,\n"
    "                             _ev_l3_shadow.measurements.get(\"ev_ict2_a\"),\n"
    "                             _ev_l3_shadow.measurements.get(\"ev_power_w_est\"),\n"
    "                             _ev_l3_shadow.candidate_power_w)\n"
    "                except Exception as exc:\n"
    "                    log.warning(\"EV/L3 shadow unavailable: %s\", type(exc).__name__)\n"
)


def patch_source(content: bytes, expected_sha256: str = VERIFIED_SHA256) -> bytes:
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError("dispatcher baseline SHA256 changed; refuse unknown code")
    source = content.decode("utf-8")
    if source.count(CALL_ANCHOR) != 1 or HOOK.strip() in source:
        raise ValueError("dispatcher hook anchor changed or already installed")
    imports = {alias.name for node in ast.parse(source).body
               if isinstance(node, ast.Import) for alias in node.names}
    if not {"os", "common"}.issubset(imports):
        raise ValueError("dispatcher required imports changed")
    patched = source.replace(CALL_ANCHOR, HOOK + CALL_ANCHOR, 1)
    ast.parse(patched, filename="dispatch_loop.py")
    return patched.encode("utf-8")


def prepare_file(source: Path, output: Path | None = None) -> dict:
    original = source.read_bytes()
    patched = patch_source(original)
    result = {
        "mode": "offline_shadow_candidate",
        "original_sha256": VERIFIED_SHA256,
        "candidate_sha256": hashlib.sha256(patched).hexdigest(),
        "source_changes_applied": False,
        "physical_control_authorized": False,
        "device_writes": 0,
    }
    if output is not None:
        if output.parent.resolve() != Path("/tmp") or output.is_symlink():
            raise ValueError("candidate output must be directly under /tmp")
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(patched)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            output.unlink(missing_ok=True)
            raise
        result["output_written"] = True
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=LIVE_DISPATCH)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.source.resolve() != LIVE_DISPATCH:
        parser.error("only the active dispatcher path is accepted")
    try:
        print(json.dumps(prepare_file(args.source, args.output), sort_keys=True))
    except (OSError, UnicodeError, ValueError, SyntaxError) as exc:
        print(json.dumps({"mode": "offline_shadow_candidate", "status": "blocked",
                          "reason": type(exc).__name__, "device_writes": 0}))
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
