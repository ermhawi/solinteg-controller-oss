"""Pure S09 vendor-evidence preflight; no runtime reads or FC06 writes."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
from native_soc_safety_evidence import NativeSocEvidence, documentary_gate  # noqa: E402


class NativeSocGateTest(unittest.TestCase):
    def test_historically_read_end_soc_does_not_qualify_unknown_scale_or_ems_priority(self):
        evidence = NativeSocEvidence(
            economic_reserve_pct=20, software_floor_pct=12,
            native_protection_enabled=True,
            native_end_soc_pct=None,  # raw 52503=100 is ambiguous
        )
        result = documentary_gate(evidence)
        self.assertEqual(result["status"], "NO-GO")
        self.assertIn("native-end-soc-unverified", result["reasons"])
        self.assertIn("missing-register_scale_vendor_confirmed", result["reasons"])
        self.assertFalse(result["physical_go"])

    def test_software_economic_floor_never_pretends_to_be_physical(self):
        evidence = NativeSocEvidence(
            economic_reserve_pct=20, software_floor_pct=12,
            native_protection_enabled=True, native_end_soc_pct=10,
            firmware_identified=True, register_scale_vendor_confirmed=True,
            vendor_ems_discharge_priority_confirmed=True,
            vendor_stale_setpoint_cutoff_confirmed=True, bms_response_verified=True,
        )
        result = documentary_gate(evidence)
        self.assertIn("native-below-economic-reserve", result["reasons"])
        self.assertEqual(result["status"], "NO-GO")

    def test_even_complete_documentation_never_authorizes_hardware(self):
        evidence = NativeSocEvidence(
            economic_reserve_pct=20, software_floor_pct=12,
            native_protection_enabled=True, native_end_soc_pct=20,
            firmware_identified=True, register_scale_vendor_confirmed=True,
            vendor_ems_discharge_priority_confirmed=True,
            vendor_stale_setpoint_cutoff_confirmed=True, bms_response_verified=True,
        )
        result = documentary_gate(evidence)
        self.assertEqual(result["status"], "documentary-review-eligible")
        self.assertEqual(result["reasons"], [])
        self.assertFalse(result["physical_go"])
        self.assertFalse(result["physical_state_verified"])
        self.assertTrue(result["operator_approval_required"])

    def test_invalid_nan_inf_bool_and_string_can_never_pass(self):
        base = dict(
            economic_reserve_pct=20, software_floor_pct=12,
            native_protection_enabled=True, native_end_soc_pct=20,
            firmware_identified=True, register_scale_vendor_confirmed=True,
            vendor_ems_discharge_priority_confirmed=True,
            vendor_stale_setpoint_cutoff_confirmed=True, bms_response_verified=True,
        )
        for field, value in (
            ("economic_reserve_pct", float("nan")), ("economic_reserve_pct", True),
            ("software_floor_pct", "12"), ("native_end_soc_pct", float("inf")),
            ("native_end_soc_pct", "20"), ("native_end_soc_pct", False),
        ):
            with self.subTest(field=field, value=value):
                evidence = NativeSocEvidence(**(base | {field: value}))
                self.assertEqual(documentary_gate(evidence)["status"], "NO-GO")


if __name__ == "__main__":
    unittest.main()
