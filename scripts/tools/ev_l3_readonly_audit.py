#!/usr/bin/env python3
"""Read-only EV/L3 evidence check. No Modbus, EVCC, or inverter writes."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from ev_grid_priority_guard import PRODUCTION_POLICY_ID, suggest  # noqa: E402
from l3_battery_policy import observe_l3  # noqa: E402

LIVE = Path("/opt/solinteg/live.json")
EVCC = Path("/opt/solinteg/ev-shadow-runtime/var/evcc-readonly.json")
PAUSE_PWM_RAW = 1000


def audit(live: dict, evcc: dict, *, now: datetime,
          preview_discharge_w: int = 2300) -> dict:
    """Evaluate the existing night-only guard against *synthetic* shadow intent.

    The synthetic gate exists only in memory; this function cannot authorize
    physical control or create an activation file. The preview is not the
    dispatcher's actual target.
    """
    if now.tzinfo is None or preview_discharge_w < 50:
        raise ValueError("timezone-aware time and positive preview are required")
    if not isinstance(live, dict) or not isinstance(evcc, dict):
        raise ValueError("both snapshots must be objects")

    now = now.astimezone(timezone.utc)
    phase = observe_l3(live, now)
    balanced = type(live.get("grid_unbalanced_output_raw")) is int
    balanced = balanced and live["grid_unbalanced_output_raw"] == 0
    gate = {
        "schema_version": 2,
        "mode": "shadow",
        "policy_id": PRODUCTION_POLICY_ID,
        "activation_id": "readonly-preview",
        "activated_utc": now.isoformat(),
        "dispatcher_sha256": "0" * 64,
    }
    candidate = suggest("discharge", preview_discharge_w, live, evcc,
                        now=now, gate=gate)
    available = candidate.state == "shadow"
    return {
        "mode": "read_only_evidence_check",
        "checked_at_utc": now.isoformat(),
        "source": "in_memory_shadow_preview",
        "preview_discharge_w": preview_discharge_w,
        "evcc_read_ok": evcc.get("read_ok") is True,
        "evcc_state": evcc.get("state"),
        "evcc_charging_detected": evcc.get("charging_detected"),
        "evcc_paused_by_pwm": (evcc.get("configured_pwm_raw") == PAUSE_PWM_RAW
                               or evcc.get("pilot_pwm_raw") == PAUSE_PWM_RAW),
        "evcc_pwm_matches": (evcc.get("configured_pwm_raw") is not None
                              and evcc.get("configured_pwm_raw") ==
                              evcc.get("pilot_pwm_raw")),
        "evcc_measured_current_a": evcc.get("measured_current_a"),
        "balanced_output_confirmed": balanced,
        "phase_telemetry_valid": phase is not None,
        "house_l3_estimated_w": round(phase.house_l3_w) if phase and balanced else None,
        "preview_state": candidate.state,
        "preview_reason": candidate.reason,
        "hypothetical_discharge_w": candidate.power_w if available else None,
        "estimated_ev_w": (candidate.measurements.get("ev_grid_trial_ev_w_est")
                            if available else None),
        "physical_control_authorized": False,
        "model_actionable": False,
        "device_writes": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", type=Path, default=LIVE)
    parser.add_argument("--evcc", type=Path, default=EVCC)
    parser.add_argument("--preview-discharge-w", type=int, default=2300)
    args = parser.parse_args()
    try:
        live = json.loads(args.live.read_text(encoding="utf-8"))
        evcc = json.loads(args.evcc.read_text(encoding="utf-8"))
        result = audit(live, evcc, now=datetime.now(timezone.utc),
                       preview_discharge_w=args.preview_discharge_w)
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"mode": "read_only_evidence_check", "state": "unavailable",
                          "reason": type(exc).__name__, "device_writes": 0}))
        raise SystemExit(2) from exc
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
