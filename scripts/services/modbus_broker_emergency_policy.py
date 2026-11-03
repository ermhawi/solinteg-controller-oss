"""Offline-only review of independently measured emergency General fallback.

A successful broker fence is NOT an authorization to enter General: General may
self-discharge the battery toward the inverter's native EndSOC instead of the
site's higher economic reserve. This pure policy models a *candidate* only.
A future privileged source must prove BMS/native limit enforcement and obtain
post-fence evidence. No installed service consumes this module.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class GeneralFallbackEvidence:
    """Claims supplied by a future separately verified, privileged reader.

    The bools and source label are NOT authenticators. Tests inject them; a
    production implementation must establish the claimed provenance itself.
    """
    soc_pct: float
    native_end_soc_pct: float
    mode_raw: int
    observed_at: datetime
    verified: bool
    native_floor_enforced: bool
    bms_healthy: bool
    source: str


@dataclass(frozen=True)
class GeneralFallbackReview:
    candidate: bool
    reason: str


def _percentage(value) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and 0 <= value <= 100
    except (OverflowError, ValueError):
        return False


def review_general_fallback(
    evidence: GeneralFallbackEvidence | None, *,
    now: datetime, fence_started_at: datetime,
    retained_soc_pct: float = 20.0,
    headroom_pct: float = 2.0,
    max_evidence_age_s: float = 15.0,
) -> GeneralFallbackReview:
    """Conservative candidate gate, never permission for a physical write.

    A lower native floor than the required retained reserve makes an automatic
    General transition ineligible. The current installation has exactly this
    mismatch (native ~10%, economic reserve ~20%).
    """
    deny = lambda reason: GeneralFallbackReview(False, reason)
    if (not isinstance(now, datetime) or not isinstance(fence_started_at, datetime)
            or now.utcoffset() is None or fence_started_at.utcoffset() is None):
        return deny("unverified_clock")
    elapsed = (now - fence_started_at).total_seconds()
    if not 0 <= elapsed <= 120:
        return deny("invalid_fence_window")
    if (not _percentage(retained_soc_pct) or not _percentage(headroom_pct)
            or retained_soc_pct <= 0 or headroom_pct <= 0
            or retained_soc_pct + headroom_pct >= 100
            or type(max_evidence_age_s) not in (float, int)
            or not math.isfinite(max_evidence_age_s)
            or not 0 < max_evidence_age_s <= 30):
        return deny("invalid_policy")
    if not isinstance(evidence, GeneralFallbackEvidence):
        return deny("missing_independent_evidence")
    if (evidence.verified is not True or evidence.native_floor_enforced is not True
            or evidence.bms_healthy is not True
            or evidence.source != "independent_post_fence"):
        return deny("independent_safety_proof_missing")
    if (not isinstance(evidence.observed_at, datetime)
            or evidence.observed_at.utcoffset() is None):
        return deny("invalid_evidence_time")
    age = (now - evidence.observed_at).total_seconds()
    post_fence = (evidence.observed_at - fence_started_at).total_seconds()
    if not 0 <= age <= max_evidence_age_s or post_fence < 0:
        return deny("evidence_stale_future_or_pre_fence")
    if (not _percentage(evidence.soc_pct)
            or not _percentage(evidence.native_end_soc_pct)):
        return deny("invalid_soc_or_native_floor")
    if type(evidence.mode_raw) is not int or evidence.mode_raw not in (0x101, 0x303):
        return deny("unexpected_inverter_mode")
    if evidence.native_end_soc_pct < retained_soc_pct:
        return deny("native_floor_below_required_reserve")
    if evidence.soc_pct < max(evidence.native_end_soc_pct, retained_soc_pct) + headroom_pct:
        return deny("insufficient_soc_headroom")
    return GeneralFallbackReview(True, "post_fence_evidence_eligible")
