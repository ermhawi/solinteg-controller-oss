"""EVCC manual FC10 must share scheduled writer locks and fail closed offline."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

SERVICES = Path(__file__).resolve().parents[1] / "services"
sys.path.insert(0, str(SERVICES))
import evcc_manual_lock as manual  # noqa: E402
import ev_price_controller as scheduled  # noqa: E402
import evcc_service as cli  # noqa: E402


class FakeClient:
    def __init__(self):
        self.calls = []

    def set_limit_raw(self, raw, *, expected_raw):
        self.calls.append((raw, expected_raw))
        return {"set": raw}


class ManualWriterLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.owner_lock = self.root / "price-owner.lock"
        self.reader_lock = self.root / "reader.lock"
        self.owner_file = self.root / "pause-owner.json"
        self.env = mock.patch.dict(os.environ, {
            "EV_PRICE_PAUSE_OWNER_LOCK": str(self.owner_lock),
            "EV_PRICE_PAUSE_OWNER_PATH": str(self.owner_file),
            "EVCC_READER_LOCK": str(self.reader_lock),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_manual_uses_same_default_lock_paths_as_scheduled(self):
        self.assertEqual(manual.OWNER_LOCK, scheduled.OWNER_LOCK)
        self.assertEqual(manual.READER_LOCK, scheduled.LOCK)

    def test_manual_write_without_price_owner_acquires_both_locks(self):
        client = FakeClient()
        with mock.patch.object(manual.ownership, "read_owner", return_value=None):
            actual = manual.guarded_manual_write(client, 1000, expected_raw=100)
        self.assertEqual(actual, {"set": 1000})
        self.assertEqual(client.calls, [(1000, 100)])
        self.assertEqual(self.owner_lock.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.reader_lock.stat().st_mode & 0o777, 0o600)

    def test_live_price_owner_or_pending_owner_blocks_manual_writes(self):
        for state in ("owned", "pending", "releasing"):
            with self.subTest(state=state):
                client = FakeClient()
                with mock.patch.object(manual.ownership, "read_owner",
                                       return_value={"state": state}):
                    with self.assertRaises(manual.ManualWriterBlocked):
                        manual.guarded_manual_write(client, 1000, expected_raw=100)
                self.assertEqual(client.calls, [])

    def test_invalid_owner_and_held_owner_or_reader_lock_block(self):
        client = FakeClient()
        with mock.patch.object(manual.ownership, "read_owner",
                               side_effect=manual.ownership.OwnershipError("invalid")):
            with self.assertRaises(manual.ManualWriterBlocked):
                manual.guarded_manual_write(client, 1000, expected_raw=100)
        self.assertEqual(client.calls, [])
        for path in (self.owner_lock, self.reader_lock):
            with self.subTest(path=path):
                path.touch(mode=0o600)
                with path.open("a+b") as held:
                    fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    try:
                        with mock.patch.object(manual.ownership, "read_owner",
                                               return_value=None):
                            with self.assertRaises(manual.ManualWriterBlocked):
                                manual.guarded_manual_write(client, 1000, expected_raw=100)
                    finally:
                        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        self.assertEqual(client.calls, [])

    def test_symlink_or_world_readable_lock_never_reaches_fc10(self):
        client = FakeClient()
        target = self.root / "real.lock"
        target.write_text("unchanged")
        self.owner_lock.symlink_to(target)
        with mock.patch.object(manual.ownership, "read_owner", return_value=None):
            with self.assertRaises(manual.ManualWriterBlocked):
                manual.guarded_manual_write(client, 1000, expected_raw=100)
        self.owner_lock.unlink()
        self.owner_lock.write_text("unused")
        self.owner_lock.chmod(0o644)
        with mock.patch.object(manual.ownership, "read_owner", return_value=None):
            with self.assertRaises(manual.ManualWriterBlocked):
                manual.guarded_manual_write(client, 1000, expected_raw=100)
        self.assertEqual(target.read_text(), "unchanged")
        self.assertEqual(client.calls, [])

    def test_parallel_manual_writer_never_interleaves(self):
        entered = threading.Event()
        release = threading.Event()
        responses = []

        class SlowClient(FakeClient):
            def set_limit_raw(self, raw, *, expected_raw):
                entered.set()
                self.assert_waited = release.wait(3)
                return super().set_limit_raw(raw, expected_raw=expected_raw)

        first = SlowClient()
        second = FakeClient()

        def writer():
            with mock.patch.object(manual.ownership, "read_owner", return_value=None):
                responses.append(manual.guarded_manual_write(first, 1000, expected_raw=100))

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            with mock.patch.object(manual.ownership, "read_owner", return_value=None):
                with self.assertRaises(manual.ManualWriterBlocked):
                    manual.guarded_manual_write(second, 100, expected_raw=1000)
        finally:
            release.set()
            thread.join(4)
        self.assertEqual(len(responses), 1)
        self.assertEqual(second.calls, [])

    def test_cli_manual_action_uses_guarded_write(self):
        import ast
        tree = ast.parse((SERVICES / "evcc_service.py").read_text())
        main = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "main")
        self.assertTrue(any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "guarded_manual_write"
            for node in ast.walk(main)
        ))


if __name__ == "__main__":
    unittest.main()
