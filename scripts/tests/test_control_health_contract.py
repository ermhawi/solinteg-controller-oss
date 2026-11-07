"""Offline status-contract tests: no Modbus or EVCC is imported."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
from control_health_contract import classify, SIGNALS  # noqa: E402


UTC = timezone.utc
NOW = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)


def observation(name: str, value: bool, age_s: float = 0, source: str | None = None) -> dict:
    expected, _ = SIGNALS[name]
    return {"value": value, "source": source or expected,
            "observed_at": (NOW - timedelta(seconds=age_s)).isoformat()}


def payload(**entries: dict) -> dict:
    return {"timestamp": NOW.isoformat(), "control_health_v1": entries}


class HealthContractTests(unittest.TestCase):
    def test_legacy_heartbeat_is_backward_compatible_but_never_claims_safe(self):
        result = classify({"timestamp": NOW.isoformat()}, NOW)
        self.assertEqual(set(result["signals"].values()), {"unknown"})
        self.assertEqual(result["violations"], [])

    def test_positive_heartbeat_does_not_imply_decision_or_apply_ok(self):
        result = classify(payload(process_alive=observation("process_alive", True),
                                  last_decision_ok=observation("last_decision_ok", False)),
                          NOW)["signals"]
        self.assertEqual(result["process_alive"], "true")
        self.assertEqual(result["last_decision_ok"], "false")
        self.assertEqual(result["last_apply_verified"], "unknown")
        self.assertEqual(result["physical_state_unknown"], "unknown")

    def test_ttl_boundary_and_future_clock_skew_do_not_accept_stale(self):
        ttl = SIGNALS["telemetry_fresh"][1]
        self.assertEqual(classify(payload(
            telemetry_fresh=observation("telemetry_fresh", True, ttl)), NOW)
            ["signals"]["telemetry_fresh"], "true")
        self.assertEqual(classify(payload(
            telemetry_fresh=observation("telemetry_fresh", True, ttl + 0.001)), NOW)
            ["signals"]["telemetry_fresh"], "stale")
        self.assertEqual(classify(payload(
            telemetry_fresh=observation("telemetry_fresh", True, -6)), NOW)
            ["signals"]["telemetry_fresh"], "stale")

    def test_wrong_provenance_and_string_boolean_never_count(self):
        self.assertEqual(classify(payload(
            last_apply_verified=observation("last_apply_verified", True, source="dispatcher"),
            broker_owned={"value": "true", "source": "lease",
                          "observed_at": NOW.isoformat()},
            physical_state_unknown={"value": False, "source": "dispatcher",
                                    "observed_at": NOW.isoformat()},
        ), NOW)["signals"]["last_apply_verified"], "unknown")
        self.assertEqual(classify(payload(
            broker_owned={"value": "true", "source": "lease",
                          "observed_at": NOW.isoformat()}
        ), NOW)["signals"]["broker_owned"], "unknown")

    def test_physical_known_requires_independent_and_fresh_supporting_evidence(self):
        known = payload(
            last_apply_verified=observation("last_apply_verified", True),
            telemetry_fresh=observation("telemetry_fresh", True),
            broker_owned=observation("broker_owned", True),
            physical_state_unknown=observation("physical_state_unknown", False),
        )
        self.assertEqual(classify(known, NOW)["signals"]["physical_state_unknown"], "false")
        later = NOW + timedelta(seconds=100)
        degraded = classify(known, later)
        self.assertEqual(degraded["signals"]["physical_state_unknown"], "stale")
        self.assertNotEqual(degraded["signals"]["physical_state_unknown"], "false")
        fresh_physical = payload(
            last_apply_verified=observation("last_apply_verified", True),
            telemetry_fresh=observation("telemetry_fresh", True),
            broker_owned=observation("broker_owned", True, 76),
            physical_state_unknown=observation("physical_state_unknown", False),
        )
        unsupported = classify(fresh_physical, NOW)
        self.assertEqual(unsupported["signals"]["physical_state_unknown"], "unknown")
        self.assertEqual(unsupported["violations"], ["unsupported-physical-known-claim"])

    def test_evidence_cannot_make_unknown_physical_state_known_without_broker(self):
        incomplete = payload(
            last_apply_verified=observation("last_apply_verified", True),
            telemetry_fresh=observation("telemetry_fresh", True),
            physical_state_unknown=observation("physical_state_unknown", False),
        )
        result = classify(incomplete, NOW)
        self.assertEqual(result["signals"]["physical_state_unknown"], "unknown")
        self.assertEqual(result["violations"], ["unsupported-physical-known-claim"])

    def test_rejects_invalid_time_and_unknown_newer_payload_gracefully(self):
        with self.assertRaises(ValueError):
            classify({}, NOW.replace(tzinfo=None))
        result = classify({"timestamp": NOW.isoformat(),
                           "control_health_v2": {"process_alive": True}}, NOW)
        self.assertEqual(result["signals"]["process_alive"], "unknown")


if __name__ == "__main__":
    unittest.main()
