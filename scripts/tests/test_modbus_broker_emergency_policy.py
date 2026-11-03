"""Offline-only review tests: independent post-fence evidence is mandatory.

No systemd actions, real Modbus connections, or inverter operations.
"""
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
from modbus_broker_emergency_policy import (  # noqa: E402
    GeneralFallbackEvidence, review_general_fallback,
)

NOW = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
FENCED = NOW - timedelta(seconds=3)


def evidence(**changes):
    data = GeneralFallbackEvidence(
        soc_pct=45.0, native_end_soc_pct=22.0, mode_raw=0x303,
        observed_at=NOW - timedelta(seconds=1), verified=True,
        native_floor_enforced=True, bms_healthy=True,
        source="independent_post_fence",
    )
    return replace(data, **changes)


class GeneralFallbackReviewTests(unittest.TestCase):
    def review(self, sample=None, **kwargs):
        return review_general_fallback(
            sample if sample is not None else evidence(),
            now=kwargs.pop("now", NOW),
            fence_started_at=kwargs.pop("fence_started_at", FENCED),
            **kwargs,
        )

    def test_verified_post_fence_evidence_only_produces_a_candidate(self):
        review = self.review()
        self.assertTrue(review.candidate)
        self.assertEqual(review.reason, "post_fence_evidence_eligible")
        self.assertFalse(hasattr(review, "action"))
        self.assertFalse(hasattr(review, "register"))
        self.assertFalse(hasattr(review, "power_w"))

    def test_actual_site_native_floor_below_reserve_never_authorizes_general(self):
        # Site example: 10% native OnGrid EndSOC versus 20% desired reserve.
        result = self.review(evidence(native_end_soc_pct=10.0))
        self.assertFalse(result.candidate)
        self.assertEqual(result.reason, "native_floor_below_required_reserve")

    def test_low_soc_never_becomes_safe_by_trusting_general_mode(self):
        for soc in (0, 12, 20, 23.99):
            with self.subTest(soc=soc):
                self.assertEqual(self.review(
                    evidence(soc_pct=soc, native_end_soc_pct=22.0)
                ).reason, "insufficient_soc_headroom")

    def test_safe_soc_headroom_boundary_and_configurable_minimum(self):
        self.assertTrue(self.review(evidence(soc_pct=24.0)).candidate)
        self.assertFalse(self.review(
            evidence(soc_pct=25), retained_soc_pct=26).candidate)
        self.assertFalse(self.review(
            evidence(native_end_soc_pct=21), retained_soc_pct=22).candidate)

    def test_missing_unverified_legacy_or_unhealthy_evidence_is_denied(self):
        self.assertEqual(
            review_general_fallback(None, now=NOW, fence_started_at=FENCED).reason,
            "missing_independent_evidence",
        )
        for changes in (
            {"verified": False}, {"verified": None}, {"native_floor_enforced": False},
            {"bms_healthy": False}, {"source": "broker_cached_live_json"},
            {"source": "other"}, {"source": None},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(self.review(evidence(**changes)).reason,
                                 "independent_safety_proof_missing")

    def test_old_future_and_pre_fencing_soc_readbacks_are_denied(self):
        for time in (
            NOW - timedelta(seconds=16),
            NOW + timedelta(milliseconds=1),
            FENCED - timedelta(microseconds=1),
            NOW.replace(tzinfo=None),
        ):
            with self.subTest(time=time):
                self.assertFalse(self.review(evidence(observed_at=time)).candidate)

    def test_untrusted_numeric_soc_or_native_floor_is_denied(self):
        values = (-1, 101, True, float("nan"), float("inf"), 10**500, None, "40")
        for value in values:
            with self.subTest(value=repr(value)):
                self.assertEqual(self.review(evidence(soc_pct=value)).reason,
                                 "invalid_soc_or_native_floor")
                self.assertEqual(self.review(evidence(native_end_soc_pct=value)).reason,
                                 "invalid_soc_or_native_floor")

    def test_unexpected_mode_is_denied(self):
        for value in (0x302, 0x400, True, None, "0x303"):
            with self.subTest(mode=value):
                self.assertEqual(self.review(evidence(mode_raw=value)).reason,
                                 "unexpected_inverter_mode")
        self.assertTrue(self.review(evidence(mode_raw=0x101)).candidate)

    def test_invalid_clocks_are_denied(self):
        for params in (
            {"now": NOW.replace(tzinfo=None)},
            {"fence_started_at": FENCED.replace(tzinfo=None)},
            {"fence_started_at": NOW + timedelta(seconds=1)},
            {"fence_started_at": NOW - timedelta(seconds=121)},
        ):
            with self.subTest(params=params):
                self.assertFalse(self.review(**params).candidate)

    def test_invalid_policy_parameters_fail_closed(self):
        for params in (
            {"retained_soc_pct": 0}, {"retained_soc_pct": -1},
            {"retained_soc_pct": True}, {"retained_soc_pct": float("nan")},
            {"headroom_pct": 0}, {"headroom_pct": -1},
            {"headroom_pct": 101}, {"headroom_pct": True},
            {"max_evidence_age_s": 0}, {"max_evidence_age_s": 31},
            {"max_evidence_age_s": float("nan")},
            {"retained_soc_pct": 99}, {"retained_soc_pct": 80, "headroom_pct": 21},
        ):
            with self.subTest(params=params):
                self.assertEqual(self.review(**params).reason, "invalid_policy")


if __name__ == "__main__":
    unittest.main()
