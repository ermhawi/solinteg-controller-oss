"""Offline-only stale-telemetry gate for the opt-in broker dispatcher prototype.

The installed dispatcher, watchdog and broker services are unchanged.
It performs no I/O and does not choose a physical power target or General/Auto.
A "candidate" still requires the existing independent SoC, phase, BMS, arming
and control/readback gates before any physical operation can be considered.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

PLAN_CANDIDATE = "plan_candidate"
RECOVERY_CANDIDATE = "recovery_candidate"
INHIBIT = "inhibit"


@dataclass(frozen=True)
class IndependentSoc:
    """Evidence ONLY from a separately verified, non-competing safety path."""

    percent: float
    observed_at: datetime
    verified: bool = False


@dataclass(frozen=True)
class StalePolicyDecision:
    disposition: str
    reason: str
    soc_pct: float | None
    supervisor_required: bool

    @property
    def plan_eligible(self) -> bool:
        return self.disposition == PLAN_CANDIDATE


def _finite_number(value) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def _percentage(value) -> float | None:
    if not _finite_number(value) or not 0 <= value <= 100:
        return None
    return float(value)


def _age_s(value, now: datetime, max_age_s: float) -> float | None:
    try:
        observed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if (not isinstance(observed, datetime) or observed.utcoffset() is None
                or now.utcoffset() is None):
            return None
        age = (now - observed).total_seconds()
        return age if 0 <= age <= max_age_s else None
    except (TypeError, ValueError, OverflowError):
        return None


def _trusted_live_soc(sample, now: datetime, max_age_s: float,
                      max_sample_s: float) -> float | None:
    # The broker-mode poller writes these fields only for an accepted snapshot.
    # Legacy/direct live.json lacks a quality claim and cannot pass this gate.
    if not isinstance(sample, Mapping) or sample.get("sample_quality") != "valid":
        return None
    if not isinstance(sample.get("sample_id"), str) or not sample["sample_id"]:
        return None
    if _age_s(sample.get("timestamp"), now, max_age_s) is None:
        return None
    duration = sample.get("sample_duration_s")
    if (not _finite_number(duration)
            or not 0 <= duration <= max_sample_s):
        return None
    for name in ("pv_w", "house_load_w", "grid_w",
                 "meter_l1_w", "meter_l2_w", "meter_l3_w"):
        value = sample.get(name)
        if not _finite_number(value):
            return None
        if name in ("pv_w", "house_load_w") and value < 0:
            return None
    phase_sum = sum(sample[name] for name in ("meter_l1_w", "meter_l2_w", "meter_l3_w"))
    if abs(phase_sum - sample["grid_w"]) > max(30, abs(sample["grid_w"]) * 0.05):
        return None
    # Do not treat a manually selected or unknown inverter mode as permission
    # to resume ordinary plan control.
    if type(sample.get("work_mode_raw")) is not int:
        return None
    if sample["work_mode_raw"] not in (0x101, 0x303):
        return None
    return _percentage(sample.get("soc_pct"))


def assess_stale_dispatch(
    sample: Mapping | None, *, now: datetime,
    recovery_active: bool, recovery_entry_pct: float, recovery_exit_pct: float,
    independent_soc: IndependentSoc | None = None,
    max_age_s: float = 120.0, max_sample_s: float = 20.0,
) -> StalePolicyDecision:
    """Classify a potential plan; NEVER issue or approve any hardware write.

    Stale/unknown data never returns a normal plan candidate, even for idle:
    mapping an inhibited decision to idle/Auto would itself be a physical choice.
    Independently verified LOW SoC produces a *recovery candidate*, not an
    authorization for a blind charge or a competing direct Modbus connection.
    """
    denied = StalePolicyDecision(INHIBIT, "telemetry_or_soc_unverified", None, True)
    if (type(recovery_active) is not bool or not isinstance(now, datetime)
            or now.utcoffset() is None
            or any(not _finite_number(v) for v in (
                recovery_entry_pct, recovery_exit_pct, max_age_s, max_sample_s))
            or not 0 <= recovery_entry_pct < recovery_exit_pct < 100
            or max_age_s <= 0 or max_sample_s <= 0):
        return denied

    soc = _trusted_live_soc(sample, now, max_age_s, max_sample_s)
    fresh = soc is not None
    if not fresh and isinstance(independent_soc, IndependentSoc):
        if (independent_soc.verified is True
                and _age_s(independent_soc.observed_at, now, max_age_s) is not None):
            soc = _percentage(independent_soc.percent)

    if soc is None:
        return denied

    needs_recovery = soc < recovery_entry_pct or (
        recovery_active and soc < recovery_exit_pct
    )
    if needs_recovery:
        return StalePolicyDecision(
            RECOVERY_CANDIDATE, "verified_soc_requires_recovery", soc,
            supervisor_required=True,
        )
    if not fresh:
        # Fresh SoC alone cannot validate solar, phase, load or old plan power.
        return StalePolicyDecision(INHIBIT, "live_snapshot_untrusted", soc, True)
    return StalePolicyDecision(PLAN_CANDIDATE, "fresh_broker_sample", soc, False)
