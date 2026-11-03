"""Root-only Modbus broker stop adapter tests: all systemctl calls are faked.

No live services, /run markers, Modbus endpoints, or operator assets touched.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
from modbus_broker_root_adapter import (
    APPROVAL_MARKER, BROKER_UNIT, BROKER_UNIT_FILE, RootBrokerStopAdapter,
)
from modbus_broker_emergency_policy import GeneralFallbackEvidence
from modbus_broker_fencing import FenceObservation
from modbus_broker_lease import LeaseBusy, broker_ownership_lease
from modbus_broker_supervisor import coordinated_emergency_auto

def synthetic_post_fence_evidence():
    return GeneralFallbackEvidence(
        soc_pct=45.0, native_end_soc_pct=22.0, mode_raw=0x303,
        observed_at=datetime.now(timezone.utc), verified=True,
        native_floor_enforced=True, bms_healthy=True,
        source="independent_post_fence",
    )


RUNNING = {
    "LoadState": "loaded", "ActiveState": "active",
    "MainPID": "12345", "ControlPID": "0", "KillMode": "control-group",
    "PrivateNetwork": "no", "NetworkNamespacePath": "",
    "User": "solinteg", "Group": "solinteg",
    "FragmentPath": BROKER_UNIT_FILE,
}


class FakeSystemd:
    def __init__(self):
        self.properties = RUNNING.copy()
        self.calls = []
        self.stop_returncode = 0
        self.show_returncode = 0
        self.error = None
        self.on_show = None
        self.on_stop = None
        self.stop_changes_state = True

    def __call__(self, args, **opts):
        self.calls.append(list(args))
        assert args[0] == "systemctl"
        assert args[1] == "--system"
        assert args[-1] == BROKER_UNIT
        assert opts == {
            "capture_output": True, "text": True, "timeout": opts["timeout"],
            "check": False,
        }
        assert 0 < opts["timeout"] <= 8
        if self.error is not None:
            raise self.error
        if "show" in args:
            if self.on_show is not None:
                self.on_show()
            stdout = "\n".join(f"{k}={v}" for k, v in self.properties.items())
            return SimpleNamespace(returncode=self.show_returncode, stdout=stdout)
        if "stop" in args:
            if self.stop_changes_state and self.stop_returncode == 0:
                self.properties.update(ActiveState="inactive", MainPID="0",
                                       ControlPID="0")
            if self.on_stop is not None:
                self.on_stop()
            return SimpleNamespace(returncode=self.stop_returncode, stdout="")
        raise AssertionError(f"Unexpected command: {args}")

    @property
    def commands(self):
        return [c[2] if "show" in c else "stop" for c in self.calls]


class RootAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.parent = Path(self.tempdir.name)
        self.marker = self.parent / "broker-approved"
        self.marker.touch(mode=0o600)
        self.marker.chmod(0o600)
        self.fake = FakeSystemd()
        self.uid = os.geteuid()
        self.adapter = RootBrokerStopAdapter(
            marker_path=self.marker, run=self.fake,
            geteuid=lambda: 0, owner_uid=self.uid,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_exact_future_target_and_marker_are_fixed_constants(self):
        self.assertEqual(BROKER_UNIT, "solinteg-broker-readonly.service")
        self.assertEqual(APPROVAL_MARKER,
                         Path("/run/solinteg-broker-readonly-approved"))
        self.assertEqual(BROKER_UNIT_FILE,
                         "/etc/systemd/system/solinteg-broker-readonly.service")

    def test_revoke_is_idempotent_and_never_recreates_marker(self):
        self.assertFalse(self.adapter.approval_absent())
        self.assertTrue(self.adapter.revoke_approval())
        self.assertFalse(self.marker.exists())
        self.assertTrue(self.adapter.approval_absent())
        self.assertTrue(self.adapter.revoke_approval())
        self.assertEqual(self.fake.calls, [])

    def test_revoke_then_stop_verifies_service_inactive_and_pid_zero(self):
        self.assertTrue(self.adapter.revoke_approval())
        self.assertTrue(self.adapter.stop_broker())
        self.assertEqual(self.fake.commands, ["show", "stop", "show"])
        self.assertFalse(self.fake.properties["ActiveState"] == "active")
        self.assertTrue(self.adapter.approval_absent())
        self.assertTrue(all(call[-1] == BROKER_UNIT for call in self.fake.calls))

    def test_stop_never_runs_with_present_approval(self):
        self.assertFalse(self.adapter.stop_broker())
        self.assertEqual(self.fake.calls, [])
        self.assertTrue(self.marker.exists())

    def test_non_root_is_denied_without_marker_or_systemd_mutation(self):
        adapter = RootBrokerStopAdapter(
            marker_path=self.marker, run=self.fake,
            geteuid=lambda: 1000, owner_uid=self.uid,
        )
        self.assertFalse(adapter.revoke_approval())
        self.assertFalse(adapter.approval_absent())
        self.assertFalse(adapter.stop_broker())
        self.assertEqual(self.fake.calls, [])
        self.assertTrue(self.marker.exists())

    def test_untrusted_parent_is_rejected_not_mistaken_for_absence(self):
        self.parent.chmod(0o777)
        try:
            self.assertFalse(self.adapter.approval_absent())
            self.assertFalse(self.adapter.revoke_approval())
            self.assertFalse(self.adapter.stop_broker())
            self.assertTrue(self.marker.exists())
        finally:
            self.parent.chmod(0o700)

    def test_missing_parent_must_not_count_as_safe_marker_absence(self):
        missing = RootBrokerStopAdapter(
            marker_path=self.parent / "missing" / "broker-approved",
            run=self.fake, geteuid=lambda: 0, owner_uid=self.uid,
        )
        self.assertFalse(missing.approval_absent())
        self.assertFalse(missing.revoke_approval())
        self.assertFalse(missing.stop_broker())

    def test_symlink_is_never_unlinked(self):
        target = self.parent / "operator-data"
        target.write_text("preserve")
        self.marker.unlink()
        self.marker.symlink_to(target)
        self.assertFalse(self.adapter.revoke_approval())
        self.assertFalse(self.adapter.approval_absent())
        self.assertTrue(self.marker.is_symlink())
        self.assertEqual(target.read_text(), "preserve")

    def test_marker_mode_and_hardlinks_are_refused(self):
        for mode in (0o644, 0o666, 0o400):
            with self.subTest(mode=mode):
                self.marker.chmod(mode)
                self.assertFalse(self.adapter.revoke_approval())
                self.assertTrue(self.marker.exists())
        self.marker.chmod(0o600)
        linked = self.parent / "hardlink-to-marker"
        os.link(self.marker, linked)
        self.assertFalse(self.adapter.revoke_approval())
        self.assertTrue(self.marker.exists())
        linked.unlink()

    def test_marker_directory_is_not_unlinked(self):
        self.marker.unlink()
        self.marker.mkdir()
        self.assertFalse(self.adapter.revoke_approval())
        self.assertFalse(self.adapter.approval_absent())
        self.assertTrue(self.marker.is_dir())

    def test_systemd_wrong_service_profile_cannot_be_stopped(self):
        changes = {
            "LoadState": "not-found",
            "User": "root",
            "Group": "root",
            "FragmentPath": "/etc/systemd/system/another.service",
            "KillMode": "process",
            "PrivateNetwork": "yes",
            "NetworkNamespacePath": "/run/netns/hidden",
            "ActiveState": "unknown",
            "MainPID": "not-a-pid",
        }
        self.assertTrue(self.adapter.revoke_approval())
        for name, value in changes.items():
            with self.subTest(name=name):
                self.fake.properties = RUNNING | {name: value}
                self.fake.calls.clear()
                self.assertFalse(self.adapter.stop_broker())
                self.assertEqual(self.fake.commands, ["show"])

    def test_systemctl_show_error_or_timeout_prevents_stop(self):
        self.assertTrue(self.adapter.revoke_approval())
        for error in ((OSError("systemd unavailable")),
                      subprocess.TimeoutExpired(["systemctl"], 5)):
            with self.subTest(error=str(error)):
                self.fake.calls.clear()
                self.fake.error = error
                self.assertFalse(self.adapter.stop_broker())
                self.assertEqual(self.fake.commands, ["show"])
        self.fake.error = None
        self.fake.show_returncode = 1
        self.fake.calls.clear()
        self.assertFalse(self.adapter.stop_broker())
        self.assertEqual(self.fake.commands, ["show"])

    def test_stop_error_or_timeout_fails_closed(self):
        self.assertTrue(self.adapter.revoke_approval())
        self.fake.stop_returncode = 1
        self.assertFalse(self.adapter.stop_broker())
        self.assertEqual(self.fake.commands, ["show", "stop"])
        self.fake.calls.clear()
        self.fake.stop_returncode = 0
        def timeout_on_stop():
            raise subprocess.TimeoutExpired(["systemctl", "stop"], 8)
        self.fake.on_stop = timeout_on_stop
        self.assertFalse(self.adapter.stop_broker())
        self.assertEqual(self.fake.commands, ["show", "stop"])

    def test_stop_success_without_proven_process_exit_fails_closed(self):
        self.assertTrue(self.adapter.revoke_approval())
        self.fake.stop_changes_state = False
        self.assertFalse(self.adapter.stop_broker())
        self.assertEqual(self.fake.commands, ["show", "stop", "show"])
        self.assertEqual(self.fake.properties["ActiveState"], "active")

    def test_after_stop_wrong_user_or_mode_is_unverified(self):
        self.assertTrue(self.adapter.revoke_approval())
        for key, value in (("User", "root"), ("KillMode", "process"),
                           ("MainPID", "55"), ("ActiveState", "deactivating")):
            with self.subTest(key=key):
                self.fake.properties = RUNNING.copy()
                self.fake.calls.clear()
                def contaminate():
                    self.fake.properties[key] = value
                self.fake.on_stop = contaminate
                self.assertFalse(self.adapter.stop_broker())
                self.assertEqual(self.fake.commands, ["show", "stop", "show"])
        self.fake.on_stop = None

    def test_marker_restored_during_pre_stop_check_prevents_stop(self):
        self.assertTrue(self.adapter.revoke_approval())
        def restore_on_show():
            self.marker.touch(mode=0o600)
            self.marker.chmod(0o600)
        self.fake.on_show = restore_on_show
        self.assertFalse(self.adapter.stop_broker())
        self.assertEqual(self.fake.commands, ["show"])
        self.assertTrue(self.marker.exists())

    def test_marker_restored_during_stop_prevents_success(self):
        self.assertTrue(self.adapter.revoke_approval())
        def restore_on_stop():
            self.marker.touch(mode=0o600)
            self.marker.chmod(0o600)
        self.fake.on_stop = restore_on_stop
        self.assertFalse(self.adapter.stop_broker())
        self.assertEqual(self.fake.commands, ["show", "stop", "show"])
        self.assertTrue(self.marker.exists())

    def test_coordinator_uses_root_adapter_callbacks_and_temporary_lock(self):
        lock_path = self.parent / "ownership.lock"
        lock_path.touch()
        lock_path.chmod(0o440)
        writes = []

        def lease():
            return broker_ownership_lease(lock_path, owner_uid=self.uid,
                                          owner_gid=os.getegid())

        def observe():
            with self.assertRaises(LeaseBusy):
                with lease():
                    self.fail("broker could take emergency ownership")
            return FenceObservation(False, 0, 0, False, 123.0)

        def emergency():
            writes.append("simulated_auto")
            return True

        result = coordinated_emergency_auto(
            revoke_approval=self.adapter.revoke_approval,
            approval_absent=self.adapter.approval_absent,
            stop_broker=self.adapter.stop_broker,
            ownership_lease=lease,
            observe=observe,
            emergency_auto=emergency,
            read_independent_evidence=synthetic_post_fence_evidence,
            alert=lambda _: self.fail("unexpected alarm"),
            monotonic=lambda: 0.0,
        )
        self.assertTrue(result.auto_readback_verified)
        self.assertEqual(writes, ["simulated_auto"])
        self.assertEqual(self.fake.commands, ["show", "stop", "show"])
        self.assertTrue(self.adapter.approval_absent())
        with lease():
            pass

    def test_coordinator_rejects_wrong_service_before_lock_or_fake_auto(self):
        self.fake.properties["User"] = "root"
        self.assertTrue(self.marker.exists())
        writes = []
        result = coordinated_emergency_auto(
            revoke_approval=self.adapter.revoke_approval,
            approval_absent=self.adapter.approval_absent,
            stop_broker=self.adapter.stop_broker,
            ownership_lease=lambda: self.fail("unexpected lease"),
            observe=lambda: self.fail("unexpected observation"),
            emergency_auto=lambda: writes.append("unsafe"),
            read_independent_evidence=synthetic_post_fence_evidence,
            alert=lambda _: None,
            monotonic=lambda: 0.0,
        )
        self.assertEqual(result.error, "broker_stop_unconfirmed")
        self.assertEqual(writes, [])
        self.assertTrue(self.adapter.approval_absent())
        self.assertEqual(self.fake.commands, ["show"])

    def test_rejected_marker_protects_other_files(self):
        self.marker.unlink()
        other = self.parent / "operator-private"
        other.write_text("must stay")
        self.assertTrue(self.adapter.revoke_approval())
        self.assertEqual(other.read_text(), "must stay")
        self.assertTrue(other.is_file())


if __name__ == "__main__":
    unittest.main()
