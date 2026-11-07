"""Offline-only control-health signal contract (S03).

This module does not authorize physical control. It only judges individually
provenanced signals; legacy heartbeat files remain readable and mean *unknown*.
"""
from __future__ import annotations

from datetime import datetime, timezone

# Seconds until evidence becomes stale, deliberately separate from write policy.
SIGNALS = {
    "process_alive": ("systemd", 120),
    "last_decision_ok": ("dispatcher", 120),
    "last_apply_verified": ("register_and_power", 75),
    "telemetry_fresh": ("poller", 90),
    "broker_owned": ("lease", 75),
    "physical_state_unknown": ("independent_observer", 75),
}
MAX_FUTURE_S = 5.0
SCHEMA = "solinteg-control-health-v1"


def classify(heartbeat: dict | None, now: datetime) -> dict:
    """Evaluate raw observations as true/false/stale/unknown, never as a GO gate.

    A timestamp alone is NOT proof of process health, successful apply, broker
    ownership or physical safety. Physical known requires independent evidence
    and all supporting signals to still be true. No network or filesystem I/O.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    payload = heartbeat.get("control_health_v1") if isinstance(heartbeat, dict) else None
    result = {name: "unknown" for name in SIGNALS}
    for name, (source, ttl_s) in SIGNALS.items():
        evidence = payload.get(name) if isinstance(payload, dict) else None
        if not isinstance(evidence, dict) or evidence.get("source") != source:
            continue
        if type(evidence.get("value")) is not bool:
            continue
        try:
            stamp = datetime.fromisoformat(evidence["observed_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                continue
            age = (now - stamp).total_seconds()
        except (TypeError, AttributeError, ValueError, KeyError, OverflowError):
            continue
        if not -MAX_FUTURE_S <= age <= ttl_s:
            result[name] = "stale"
        else:
            result[name] = "true" if evidence["value"] else "false"
    failures = []
    if result["physical_state_unknown"] == "false":
        required = ("last_apply_verified", "telemetry_fresh", "broker_owned")
        if any(result[name] != "true" for name in required):
            result["physical_state_unknown"] = "unknown"
            failures.append("unsupported-physical-known-claim")
    return {"schema": SCHEMA, "signals": result, "violations": failures}
