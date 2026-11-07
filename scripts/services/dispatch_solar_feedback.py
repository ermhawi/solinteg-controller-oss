"""D06: independent evidence gate for the solar-capture policy latch.

An accepted register target is not proof of physical mode or battery power.
The General latch may continue through short load spikes without claiming
that the battery is charging. No I/O: caller supplies poller measurements.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from energy_priority_policy import SolarState, _valid_samples, SOLAR_SAMPLE_MAX_AGE_S

CaptureEvidence = Literal["observed_charging", "observed_general", "unknown"]


@dataclass(frozen=True)
class SolarStateResult:
    state: SolarState
    physical_confirmation: CaptureEvidence
    reason: str


def observe_post_apply_general(readings: list[dict], *, applied_at: datetime | None,
                               now: datetime) -> CaptureEvidence:
    """Two validated post-ack General samples, distinguishing mode from charge."""
    if applied_at is None or applied_at.tzinfo is None or now.tzinfo is None:
        return "unknown"
    valid = _valid_samples(readings, now)
    if len(valid) < 2 or len(readings) < 2:
        return "unknown"
    try:
        raw = sorted(
            ((datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")), row)
             for row in readings),
            key=lambda entry: entry[0],
        )
        latest = raw[-2:]
        stamps = [entry[0] for entry in latest]
        if any(stamp.tzinfo is None or stamp <= applied_at for stamp in stamps):
            return "unknown"
        if stamps != [entry[0] for entry in valid[-2:]]:
            # An invalid/conflicting newer reading is never skipped.
            return "unknown"
        gap = (stamps[1] - stamps[0]).total_seconds()
        if not 15 <= gap <= 55:
            return "unknown"
        if not 0 <= (now - stamps[-1]).total_seconds() <= SOLAR_SAMPLE_MAX_AGE_S:
            return "unknown"
        if not all(row["work_mode_raw"] == 0x101 for _, row in latest):
            return "unknown"
        if all(type(row["battery_w"]) in (float, int) and row["battery_w"] <= -100
               for _, row in latest):
            return "observed_charging"
        return "observed_general"
    except (KeyError, ValueError, TypeError, AttributeError):
        return "unknown"


def reconcile_solar_state(_previous: SolarState, proposed: SolarState, *,
                          requested_action: str, outcome: str | None,
                          acknowledged_idle: bool,
                          observation: CaptureEvidence) -> SolarStateResult:
    """Never carry a solar-capture latch without independent post-ack evidence.

    outcome=None is the no-write branch of needs_apply(). A previously
    acknowledged General target can persist, but no-write alone is not proof.
    Inhibited, pending, skipped and failed writes invalidate the old latch.
    """
    if outcome not in (None, "applied"):
        return SolarStateResult(SolarState(), "unknown", "apply_not_verified")
    if requested_action != "idle":
        return SolarStateResult(
            SolarState(blocked_until=proposed.blocked_until),
            "unknown", "not_general")
    if not proposed.capturing:
        return SolarStateResult(proposed, "unknown", "no_solar_capture_proposed")
    if not acknowledged_idle or observation == "unknown":
        return SolarStateResult(SolarState(), "unknown", "missing_post_apply_evidence")
    return SolarStateResult(proposed, observation, "independent_poller_observation")
