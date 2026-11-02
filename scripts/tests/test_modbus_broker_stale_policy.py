"""Offline-only checks for the proposed broker stale-data/SoC gate.

No service, filesystem, inverter, socket, or Modbus operations occur here.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from modbus_broker_stale_policy import (  # noqa: E402
    INHIBIT, PLAN_CANDIDATE, RECOVERY_CANDIDATE,
    IndependentSoc, assess_stale_dispatch,
)

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def accepted_sample(**changes):
    sample = {
        "timestamp": NOW.isoformat(),
        "sample_id": "offline:1",
        "sample_quality": "valid",
        "sample_duration_s": 0.8,
        "soc_pct": 35.0,
        "pv_w": 600,
        "house_load_w": 1000,
        "grid_w": -400,
        "meter_l1_w": -100,
        "meter_l2_w": -100,
        "meter_l3_w": -200,
        "work_mode_raw": 0x101,
    }
    sample.update(changes)
    return sample


class StaleBrokerPolicyTest(unittest.TestCase):
    def assess(self, sample, *, recovery_active=False, independent_soc=None,
               now=NOW, **kwargs):
        return assess_stale_dispatch(
            sample, now=now, recovery_active=recovery_active,
            recovery_entry_pct=19.5, recovery_exit_pct=21.0,
            independent_soc=independent_soc, **kwargs,
        )

    def test_fresh_accepted_broker_sample_is_only_a_plan_candidate(self):
        result = self.assess(accepted_sample())
        self.assertEqual(result.disposition, PLAN_CANDIDATE)
        self.assertTrue(result.plan_eligible)
        self.assertFalse(result.supervisor_required)
        self.assertFalse(hasattr(result, "power_w"))
        self.assertFalse(hasattr(result, "action"))

    def test_exact_120_second_boundary_is_allowed_but_older_is_not(self):
        good = accepted_sample(timestamp=(NOW - timedelta(seconds=120)).isoformat())
        old = accepted_sample(timestamp=(NOW - timedelta(seconds=120, microseconds=1)).isoformat())
        self.assertTrue(self.assess(good).plan_eligible)
        result = self.assess(old)
        self.assertEqual(result.disposition, INHIBIT)
        self.assertTrue(result.supervisor_required)

    def test_missing_invalid_or_rejected_broker_snapshot_is_not_plan_eligible(self):
        samples = (
            None, {}, [], "garbage",
            accepted_sample(sample_quality="stale"),
            accepted_sample(sample_quality="invalid"),
            accepted_sample(sample_quality=None),
            accepted_sample(sample_id=""),
            accepted_sample(sample_duration_s=21),
            accepted_sample(sample_duration_s=-1),
            accepted_sample(sample_duration_s=float("nan")),
            accepted_sample(sample_duration_s=10 ** 500),
        )
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(self.assess(sample).disposition, INHIBIT)

    def test_legacy_live_json_without_quality_flag_is_denied(self):
        sample = accepted_sample()
        sample.pop("sample_quality")
        self.assertEqual(self.assess(sample).disposition, INHIBIT)

    def test_clock_anomalies_and_naive_timestamps_fail_closed(self):
        samples = (
            accepted_sample(timestamp="invalid"),
            accepted_sample(timestamp="2026-11-02T12:00:00"),
            accepted_sample(timestamp=(NOW + timedelta(microseconds=1)).isoformat()),
            accepted_sample(timestamp=(NOW + timedelta(hours=1)).isoformat()),
        )
        for sample in samples:
            with self.subTest(sample=sample["timestamp"]):
                self.assertEqual(self.assess(sample).disposition, INHIBIT)
        self.assertEqual(self.assess(accepted_sample(), now=NOW.replace(tzinfo=None)).disposition, INHIBIT)

    def test_soc_out_of_range_nan_missing_or_boolean_is_untrusted(self):
        for value in (-1, 100.1, float("nan"), float("inf"), 10 ** 500, True, None, "19"):
            with self.subTest(soc=value):
                self.assertEqual(self.assess(accepted_sample(soc_pct=value)).disposition, INHIBIT)

    def test_invalid_load_or_missing_phase_data_is_untrusted(self):
        for name, value in (
            ("pv_w", float("inf")), ("pv_w", -1),
            ("house_load_w", -1), ("house_load_w", float("nan")),
            ("meter_l1_w", None), ("meter_l2_w", float("nan")),
            ("grid_w", None), ("grid_w", 10 ** 500),
            ("work_mode_raw", True), ("work_mode_raw", 65536),
            ("work_mode_raw", 0x400),
        ):
            with self.subTest(name=name, value=value):
                self.assertEqual(self.assess(accepted_sample(**{name: value})).disposition, INHIBIT)

    def test_inconsistent_phase_sum_never_becomes_valid_plan_evidence(self):
        self.assertEqual(
            self.assess(accepted_sample(meter_l3_w=900)).disposition, INHIBIT
        )

    def test_low_soc_preempts_economic_plan_even_with_fresh_telemetry(self):
        for soc in (0.0, 8.0, 19.49):
            with self.subTest(soc=soc):
                result = self.assess(accepted_sample(soc_pct=soc))
                self.assertEqual(result.disposition, RECOVERY_CANDIDATE)
                self.assertFalse(result.plan_eligible)
                self.assertTrue(result.supervisor_required)

    def test_recovery_hysteresis_preserved_with_accepted_sample(self):
        self.assertEqual(self.assess(accepted_sample(soc_pct=19.5)).disposition, PLAN_CANDIDATE)
        self.assertEqual(self.assess(
            accepted_sample(soc_pct=19.5), recovery_active=True
        ).disposition, RECOVERY_CANDIDATE)
        self.assertEqual(self.assess(
            accepted_sample(soc_pct=20.99), recovery_active=True
        ).disposition, RECOVERY_CANDIDATE)
        self.assertEqual(self.assess(
            accepted_sample(soc_pct=21.0), recovery_active=True
        ).disposition, PLAN_CANDIDATE)

    def test_stale_sample_plus_independent_low_soc_is_only_recovery_candidate(self):
        sample = accepted_sample(timestamp=(NOW - timedelta(minutes=5)).isoformat(), soc_pct=80)
        independent = IndependentSoc(18.0, NOW, verified=True)
        result = self.assess(sample, independent_soc=independent)
        self.assertEqual(result.disposition, RECOVERY_CANDIDATE)
        self.assertFalse(result.plan_eligible)
        self.assertTrue(result.supervisor_required)
        self.assertEqual(result.soc_pct, 18.0)

    def test_stale_sample_plus_independent_safe_soc_cannot_resume_plan(self):
        independent = IndependentSoc(45.0, NOW, verified=True)
        result = self.assess(None, independent_soc=independent)
        self.assertEqual(result.disposition, INHIBIT)
        self.assertTrue(result.supervisor_required)
        self.assertFalse(result.plan_eligible)

    def test_expired_or_unverified_independent_soc_is_ignored(self):
        samples = (
            IndependentSoc(18, NOW),  # Unverified provenance
            IndependentSoc(18, NOW - timedelta(minutes=3), True),
            IndependentSoc(18, NOW + timedelta(seconds=1), True),
            IndependentSoc(float("nan"), NOW, True),
            IndependentSoc(True, NOW, True),
            IndependentSoc(-1, NOW, True),
        )
        for evidence in samples:
            with self.subTest(evidence=evidence):
                result = self.assess(None, independent_soc=evidence)
                self.assertEqual(result.disposition, INHIBIT)
                self.assertIsNone(result.soc_pct)

    def test_stale_soc_recovery_latch_does_not_guess_auto_or_discharge(self):
        result = self.assess(None, recovery_active=True)
        self.assertEqual(result.disposition, INHIBIT)
        self.assertTrue(result.supervisor_required)
        self.assertNotIn(result.disposition, ("idle", "auto", "discharge"))

    def test_independent_soc_with_active_hysteresis_only_proposes_recovery(self):
        evidence = IndependentSoc(20.5, NOW, verified=True)
        result = self.assess(None, independent_soc=evidence, recovery_active=True)
        self.assertEqual(result.disposition, RECOVERY_CANDIDATE)
        self.assertTrue(result.supervisor_required)

    def test_invalid_thresholds_and_time_windows_fail_closed(self):
        for kwargs in (
            {"recovery_entry_pct": 21.0, "recovery_exit_pct": 19.5},
            {"recovery_entry_pct": float("nan")},
            {"recovery_entry_pct": -1},
            {"recovery_exit_pct": 100},
            {"max_age_s": 0},
            {"max_age_s": float("nan")},
            {"max_sample_s": -1},
        ):
            with self.subTest(kwargs=kwargs):
                params = dict(sample=accepted_sample(), now=NOW, recovery_active=False,
                              recovery_entry_pct=19.5, recovery_exit_pct=21.0)
                params.update(kwargs)
                result = assess_stale_dispatch(**params)
                self.assertEqual(result.disposition, INHIBIT)


if __name__ == "__main__":
    unittest.main()
