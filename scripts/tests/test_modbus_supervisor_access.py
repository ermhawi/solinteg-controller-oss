"""S06 offline-only safety-supervisor role and Modbus preflight regression.

Synthetic FakeDongle only, no 502/TCP network and no installed services.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "services"))
from fakes import install_pymodbus_stub
install_pymodbus_stub()

from modbus_broker import Broker, EMS_BATTCTRL, GENERAL  # noqa: E402
from modbus_broker_control_service import role_for_systemd_peer  # noqa: E402
from modbus_broker_safety_policy import (  # noqa: E402
    SUPERVISOR_UNIT, SAFETY_ENABLED_ENV, approve_supervisor_role,
    validate_safety_auto_preflight,
)
from test_modbus_broker import FakeDongle  # noqa: E402


class SupervisorAccessTests(unittest.TestCase):
    def test_role_requires_exact_cgroup_uid_and_opt_in(self):
        uid = os.getuid()
        self.assertFalse(approve_supervisor_role(SUPERVISOR_UNIT, {}))
        for unit in (SUPERVISOR_UNIT, "solinteg-watchdog.service",
                     "solinteg-dispatch.service",
                     "solinteg-broker-safety-supervisor.service.attacker"):
            with self.subTest(unit=unit):
                with patch("modbus_broker_control_service.Path.read_text",
                           return_value=f"0::/system.slice/{unit}\n"), \
                     patch.dict(os.environ, {SAFETY_ENABLED_ENV: "0"}):
                    self.assertEqual(role_for_systemd_peer(123, uid, uid),
                                     "control" if unit == "solinteg-dispatch.service" else None)
                with patch("modbus_broker_control_service.Path.read_text",
                           return_value=f"0::/system.slice/{unit}\n"), \
                     patch.dict(os.environ, {SAFETY_ENABLED_ENV: "1"}):
                    expected = ("safety" if unit == SUPERVISOR_UNIT else
                                "control" if unit == "solinteg-dispatch.service" else None)
                    self.assertEqual(role_for_systemd_peer(123, uid, uid), expected)
                    self.assertIsNone(role_for_systemd_peer(123, uid + 1, uid))
        with patch("modbus_broker_control_service.Path.read_text",
                   side_effect=PermissionError):
            self.assertIsNone(role_for_systemd_peer(123, uid, uid))

    def new_broker(self, dongle, **kwargs):
        broker = Broker(
            dongle.connection, armed=True, runtime_mode="control",
            transactions_enabled=True, min_write_interval_s=0, **kwargs)
        self.addCleanup(broker.close)
        return broker

    def test_default_off_denies_before_opening_modbus(self):
        dongle = FakeDongle()
        broker = self.new_broker(dongle)
        with self.assertRaisesRegex(PermissionError, "control runtime operation"):
            broker.submit("safety_auto", role="safety", request_id="closed")
        self.assertEqual((dongle.clients, dongle.writes), (0, []))

    def test_explicitly_enabled_has_no_side_effect_when_auto_or_low_soc(self):
        dongle = FakeDongle()
        broker = self.new_broker(dongle, safety_supervisor_enabled=True)
        dongle.load(grid=-800, battery=200, ac=200, pv=0, mode=GENERAL, soc=56)
        result = broker.submit("safety_auto", role="safety", request_id="already")
        self.assertEqual(result["status"], "already_general")
        self.assertEqual(dongle.writes, [])

        low = FakeDongle()
        worker = self.new_broker(low, safety_supervisor_enabled=True)
        low.load(grid=-800, battery=200, ac=200, pv=0, mode=EMS_BATTCTRL, soc=13)
        with self.assertRaisesRegex(PermissionError, "SoC"):
            worker.submit("safety_auto", role="safety", request_id="low-soc")
        self.assertEqual(low.writes, [])

    def test_bad_meter_or_unowned_ems_blocks_fc06(self):
        for variant in ("ct_unknown", "ems_unowned"):
            with self.subTest(variant=variant):
                dongle = FakeDongle()
                worker = self.new_broker(dongle, safety_supervisor_enabled=True)
                dongle.load(grid=-800, battery=200, ac=200, pv=0, mode=EMS_BATTCTRL, soc=40)
                if variant == "ct_unknown":
                    dongle.read_error_addr = 10994
                with self.assertRaisesRegex(PermissionError, "snapshot|ownership"):
                    worker.submit("safety_auto", role="safety",
                                  request_id=variant)
                self.assertEqual(dongle.writes, [])

    def test_approved_owned_ems_still_returns_only_register_evidence(self):
        dongle = FakeDongle()
        dongle.load(grid=-800, battery=200, ac=200, pv=0, mode=EMS_BATTCTRL, soc=55)
        worker = self.new_broker(dongle, safety_supervisor_enabled=True)
        worker._owns_control = True  # synthetic recognized owner, never a live override
        result = worker.submit("safety_auto", role="safety", request_id="owned")
        self.assertEqual(result["status"], "registers_verified_only")
        self.assertEqual(dongle.regs[50000], GENERAL)
        self.assertEqual(dongle.peak, 1)
        self.assertEqual(worker.submit("safety_auto", role="safety",
                                       request_id="owned"), result)
        self.assertEqual(len(dongle.writes), 4)

    def test_operation_scope_denies_dispatcher_and_other_safety_commands(self):
        dongle = FakeDongle()
        broker = self.new_broker(dongle, safety_supervisor_enabled=True)
        for role, operation in (("control", "safety_auto"),
                                ("read", "safety_auto"),
                                ("safety", "transaction_charge"),
                                ("safety", "write_register")):
            with self.subTest(role=role, operation=operation):
                with self.assertRaises(PermissionError):
                    broker.submit(operation, role=role, request_id="denied")
        self.assertEqual(dongle.clients, 0)

    def test_invalid_supervisor_runtime_cannot_be_enabled(self):
        for config in (
            {"armed": False, "runtime_mode": "control", "transactions_enabled": True},
            {"armed": True, "runtime_mode": "offline", "transactions_enabled": False},
            {"armed": True, "runtime_mode": "control", "transactions_enabled": False},
        ):
            with self.subTest(config=config), self.assertRaises(PermissionError):
                Broker(lambda: None, safety_supervisor_enabled=True, **config)

    def test_pure_preflight_fails_on_corrupted_soc_and_mode(self):
        sample = {"quality": "valid", "soc_pct": 55, "work_mode_raw": EMS_BATTCTRL}
        for patch_values in (
            {"soc_pct": None}, {"soc_pct": False}, {"soc_pct": float("nan")},
            {"soc_pct": 19.99}, {"work_mode_raw": None}, {"work_mode_raw": 0x305},
        ):
            with self.subTest(patch_values=patch_values), self.assertRaises(PermissionError):
                validate_safety_auto_preflight(
                    {"status": "valid", "sample": sample | patch_values},
                    broker_owns_control=True)


if __name__ == "__main__":
    unittest.main()
