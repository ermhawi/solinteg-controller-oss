#!/usr/bin/env python3
"""Prepare (never deploy) the minimal EV-grid hook for the verified older dispatcher.

The production box runs an older ARMED dispatcher than Git HEAD. This tool
refuses unknown source hashes and only emits an offline candidate in /tmp.
It never stops services, changes /opt/solinteg or writes inverter registers.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path

LIVE_DISPATCH = Path("/opt/solinteg/app/scripts/services/dispatch_loop.py")
VERIFIED_SHA256 = "a2f3aba4be8d64771ed2b20c71c9ceed5c3ce25ff99cad246bc5529afd65a8b7"
PATH_ANCHOR = "from zoneinfo import ZoneInfo\n"
PATH_HOOK = "from pathlib import Path\n"
IMPORT_ANCHOR = "from l3_charge_guard import observe_l3_house_w, cap_grid_charge_w\n"
IMPORT_HOOK = "from ev_grid_priority_guard import guard_dispatch as guard_ev_grid_priority\n"
CALL_ANCHOR = "            power_w, detail, numbers, l3_limited = guard_l3_charge(\n"
CALL_HOOK = (
    "            # Reducing-only EV L3 grid priority; absent/root-untrusted gate is a no-op.\n"
    "            action, power_w, expected_soc_kwh, solar_skip, detail, numbers = guard_ev_grid_priority(\n"
    "                action, power_w, expected_soc_kwh, solar_skip, detail, numbers, iteration_now,\n"
    "                live_path=Path(LIVE_JSON_PATH),\n"
    "                evcc_path=Path(\"/opt/solinteg/ev-shadow-runtime/var/evcc-readonly.json\"))\n"
)


def patch_source(content: bytes, expected_sha256: str=VERIFIED_SHA256) -> bytes:
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError("dispatcher baseline SHA256 changed; do not patch unknown code")
    source = content.decode("utf-8")
    if IMPORT_HOOK.strip() in source:
        raise ValueError("dispatcher already contains an EV-grid hook")
    if (source.count(PATH_ANCHOR) != 1 or source.count(IMPORT_ANCHOR) != 1
            or source.count(CALL_ANCHOR) != 1 or PATH_HOOK in source):
        raise ValueError("dispatcher hook anchors changed; manual review required")
    output = source.replace(PATH_ANCHOR, PATH_HOOK + PATH_ANCHOR, 1)
    output = output.replace(IMPORT_ANCHOR, IMPORT_HOOK + IMPORT_ANCHOR, 1)
    output = output.replace(CALL_ANCHOR, CALL_HOOK + CALL_ANCHOR, 1)
    ast.parse(output, filename="dispatch_loop.py")
    return output.encode("utf-8")


def prepare_file(source: Path, output: Path | None,
                 expected_sha256: str=VERIFIED_SHA256) -> dict:
    patched = patch_source(source.read_bytes(), expected_sha256)
    report = {"original_sha256": expected_sha256,
              "candidate_sha256": hashlib.sha256(patched).hexdigest(),
              "written": False}
    if output is not None:
        if output.resolve().parent != Path("/tmp"):
            raise ValueError("offline release candidate must be directly under /tmp")
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(patched)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            output.unlink(missing_ok=True)
            raise
        report["written"] = True
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="optional candidate directly under /tmp")
    args = parser.parse_args()
    if args.source.resolve() != LIVE_DISPATCH:
        parser.error("only the verified active dispatcher may be used as CLI source")
    print(json.dumps(prepare_file(args.source, args.output), sort_keys=True))


if __name__ == "__main__":
    main()
