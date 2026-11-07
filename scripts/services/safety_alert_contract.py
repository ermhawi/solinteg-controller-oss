"""Read-only alert taxonomy and dedup identity for safety telemetry (T03).

No notifier, network call, command or file write. Validity/provenance is owned
by producers; an unknown input is NEVER treated as an all-clear.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Signal:
    producer: str
    owner: str
    response: str
    severity: str = "critical"


SIGNALS = {
    "broker_telemetry_invalid": Signal(
        "broker/poller", "solinteg-operator",
        "hold new physical decisions; verify broker FC03 and register ownership"),
    "wifi_recovery_escalating": Signal(
        "wifi-recovery-watchdog", "host-network-operator",
        "check driver evidence and escalation state; verify EMS ownership before reboot"),
    "dispatcher_heartbeat_missing": Signal(
        "dispatcher/independent-monitor", "solinteg-operator",
        "check process and control readback; heartbeat absence is NOT safety proof"),
    "broker_owner_unknown": Signal(
        "broker-lease/independent-monitor", "solinteg-operator",
        "fence new writes; prove exclusive TCP owner before recovery"),
    "soc_under_reserve": Signal(
        "poller/independent-soc", "energy-operator",
        "verify native BMS, timestamp, actual SoC and reserve recovery"),
    "ems_state_uncertain": Signal(
        "broker/readback/physical-observer", "energy-operator",
        "do not blindly switch Auto; verify registers, power and a safe rollback"),
    "ev_pause_owner_uncertain": Signal(
        "ev-price-owner/cable-tracker", "ev-operator",
        "inhibit resume; verify cable epoch, manual intervention and FC10 readback"),
}
SCHEMA = "solinteg-safety-alerts-v1"


def classify(code: str, state: bool | None, *, source: str,
             last_state: str = "unknown") -> dict:
    """Return a stable dedup key; repeated active alerts never generate a new key.

    This contract is *source-only*. Notification suppression and escalation
    require a separate persisted state owner. Unknown must never clear active.
    """
    if code not in SIGNALS:
        raise ValueError("unknown signal code")
    if not source or source != SIGNALS[code].producer:
        raise ValueError("unverified telemetry provenance")
    if last_state not in ("active", "clear", "unknown"):
        raise ValueError("invalid prior alert state")
    next_state = "active" if state is True else "clear" if state is False else "unknown"
    spec = SIGNALS[code]
    return {
        "schema": SCHEMA,
        "code": code,
        "dedup_key": "solinteg:" + code,
        "signal_state": next_state,
        "emit_new_alert": next_state == "active" and last_state != "active",
        "clear_active": next_state == "clear" and last_state == "active",
        "owner": spec.owner,
        "severity": spec.severity,
        "response": spec.response,
    }
