"""EV AC/SoC empirical diagnostic: advisory only, no control calibration."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import ev_ac_soc_empirical as model  # noqa: E402

UTC = timezone.utc
START = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)
BASE_MS = int(START.timestamp() * 1000)
DELTA = 2_400_000  # 40 minutes / 1 SoC percentage point at 1.38 kW
INTERVAL = 30_000
KWH = 1.38 * INTERVAL / 3_600_000


def ac_intervals(n=900):
    return [(BASE_MS + i * INTERVAL,
             BASE_MS + (i + 1) * INTERVAL, KWH) for i in range(n)]


def observed(step, *, bad_quality=False, old_api=False):
    stamp = START + timedelta(milliseconds=DELTA * step)
    received = stamp - timedelta(seconds=30)
    changed = received - timedelta(seconds=15)
    return {
        "captured_epoch_ms": int(stamp.timestamp() * 1000),
        "volvo_soc_pct": 50.0 + step,
        "volvo_observation_usable": 1,
        "ha_record_fresh": 1,
        "volvo_connected": 1,
        "evcc_read_ok": 1,
        "evcc_charging_detected": 1,
        "evcc_state": "C2",
        "volvo_quality": ("other_source" if bad_quality else
                          model.audit.SOURCE_QUALITY),
        "volvo_battery_last_changed_utc": changed.isoformat(),
        "volvo_api_received_at_utc": (
            received - timedelta(minutes=7) if old_api else received).isoformat(),
    }


class EmpiricalAcSocTests(unittest.TestCase):
    def test_linear_estimate_is_advisory_not_calibration(self):
        points = model._soc_steps([observed(i) for i in range(11)])
        fit = model._fit(points, ac_intervals())
        self.assertEqual(fit["observed_soc_levels"], 11)
        self.assertEqual(fit["soc_delta_percentage_points"], 10)
        self.assertAlmostEqual(fit["linear_fit_kwh_per_soc_pct"], .92)
        self.assertAlmostEqual(fit["endpoint_kwh_per_soc_pct"], .92)
        self.assertEqual(fit["linear_fit_r_squared"], 1.0)

        fragment_end = START + timedelta(seconds=900 * 30)
        source = {
            "fragments_truncated": False,
            "observed_charging_fragments": [{
                "observed_start_utc": START.isoformat(),
                "observed_end_utc": fragment_end.isoformat(),
                "covered_seconds": (fragment_end - START).total_seconds(),
                "estimated_ac_kwh_covered_only": 10.35,
            }],
        }
        with patch.object(model.sessions, "analyze", return_value=source), \
             patch.object(model.audit, "read_samples",
                          return_value=[observed(i) for i in range(11)]), \
             patch.object(model, "_intervals", return_value=ac_intervals()):
            result = model.inspect(Path("ac"), Path("history"),
                                   now=fragment_end, days=1)
        self.assertEqual(result["status"], "empirical_candidate_only")
        self.assertEqual(result["first_soc_pct"], 51.0)
        self.assertEqual(result["observed_soc_levels"], 10)
        self.assertFalse(result["independent_ac_meter_verified"])
        self.assertIsNone(result["calibrated_ac_kwh_per_soc_pct"])
        self.assertFalse(result["physical_control_eligible"])
        self.assertEqual(result["device_writes"], 0)

    def test_soc_samples_require_source_quality_and_recent_upstream_report(self):
        rows = [observed(0), observed(1, bad_quality=True),
                observed(2, old_api=True), observed(3)]
        steps = model._soc_steps(rows)
        self.assertEqual([soc for _, soc in steps], [50.0, 53.0])
        self.assertIsNone(model._fit(steps, ac_intervals()))

    def test_gap_broken_soc_progression_and_missing_energy_fail(self):
        valid = model._soc_steps([observed(i) for i in range(11)])
        self.assertIsNone(model._fit(valid[:5], ac_intervals()))
        broken = valid[:]
        broken[5] = (broken[5][0], 60.0)
        self.assertIsNone(model._fit(broken, ac_intervals()))
        self.assertIsNone(model._fit(valid, ac_intervals(n=400)))

    def test_insufficient_or_truncated_fragments_remain_non_actionable(self):
        for report in ({}, {"fragments_truncated": True,
                            "observed_charging_fragments": [{}]},
                       {"fragments_truncated": False,
                        "observed_charging_fragments": []}):
            with self.subTest(report=report), \
                 patch.object(model.sessions, "analyze", return_value=report):
                result = model.inspect(Path("ac"), Path("history"),
                                       now=START, days=1)
                self.assertEqual(result["status"], "insufficient_evidence")
                self.assertIsNone(result["calibrated_ac_kwh_per_soc_pct"])
                self.assertFalse(result["physical_control_eligible"])
                self.assertEqual(result["device_writes"], 0)


if __name__ == "__main__":
    unittest.main()
