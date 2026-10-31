#!/usr/bin/env python3
"""Read-only EV L3 priority release preflight; never arm or control devices."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import ev_grid_priority_guard as guard
import prepare_ev_grid_priority_dispatch as prepare

LIVE = Path("/opt/solinteg/live.json")
EVCC = Path("/opt/solinteg/ev-shadow-runtime/var/evcc-readonly.json")
PREVIEW_DISCHARGE_W = 2500


def finite_number(data: dict, key: str) -> float | None:
    value = data.get(key)
    if type(value) not in (int, float):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except (OverflowError, ValueError):
        return None


def inspect(source: bytes, live: dict, evcc: dict, *, now: datetime,
            baseline_sha256: str = prepare.VERIFIED_SHA256) -> dict:
    """Check the exact dispatcher baseline and preview the pure shadow decision."""
    result = {
        "checked_at_utc": now.isoformat(),
        "mode": "read_only_preflight",
        "installed_dispatcher_sha256": hashlib.sha256(source).hexdigest(),
        "baseline_verified": False,
        "candidate_sha256": None,
        "shadow_eligible_now": False,
        "shadow_state": "unavailable",
        "shadow_reason": "source not verified",
        "shadow_preview_original_discharge_w": PREVIEW_DISCHARGE_W,
        "shadow_preview_target_w": None,
        "evcc_read_ok": evcc.get("read_ok") is True,
        "evcc_state": evcc.get("state"),
        "evcc_ict2_a": None,
        "evcc_pwm_6a": evcc.get("pilot_pwm_raw") == 100
                       and evcc.get("configured_pwm_raw") == 100,
        "house_battery_soc_pct": finite_number(live, "soc_pct"),
        "pv_w": finite_number(live, "pv_w"),
        "independent_l3_rms_protection_verified": False,
        "physical_activation_authorized": False,
    }
    currents = evcc.get("channel_currents_a")
    if isinstance(currents, list) and len(currents) == 3:
        result["evcc_ict2_a"] = finite_number({"value": currents[1]}, "value")
    try:
        patched = prepare.patch_source(source, baseline_sha256)
    except (UnicodeError, ValueError) as exc:
        result["shadow_reason"] = str(exc)[:140]
        return result
    result["baseline_verified"] = True
    result["candidate_sha256"] = hashlib.sha256(patched).hexdigest()
    gate = {
        "schema_version": 2,
        "mode": "shadow",
        "policy_id": guard.PRODUCTION_POLICY_ID,
        "activation_id": "offline-read-only-preview",
        "activated_utc": now.isoformat(),
        "dispatcher_sha256": result["candidate_sha256"],
    }
    suggestion = guard.suggest("discharge", PREVIEW_DISCHARGE_W, live, evcc,
                               now=now, gate=gate)
    result["shadow_state"] = suggestion.state
    result["shadow_reason"] = suggestion.reason
    result["shadow_eligible_now"] = suggestion.state == "shadow"
    if result["shadow_eligible_now"]:
        result["shadow_preview_target_w"] = suggestion.power_w
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=prepare.LIVE_DISPATCH)
    parser.add_argument("--live", type=Path, default=LIVE)
    parser.add_argument("--evcc", type=Path, default=EVCC)
    args = parser.parse_args()
    try:
        source = args.source.read_bytes()
        live = json.loads(args.live.read_text(encoding="utf-8"))
        evcc = json.loads(args.evcc.read_text(encoding="utf-8"))
        if not isinstance(live, dict) or not isinstance(evcc, dict):
            raise ValueError("snapshots must be JSON objects")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"mode": "read_only_preflight", "status": "unavailable",
                          "reason": type(exc).__name__}, sort_keys=True))
        raise SystemExit(2) from exc
    report = inspect(source, live, evcc, now=datetime.now(timezone.utc))
    print(json.dumps(report, sort_keys=True))
    if not report["baseline_verified"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
