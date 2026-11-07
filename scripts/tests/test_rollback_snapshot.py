"""Rollback evidence integrity and private simulation; no production writes."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/tools"))
spec = importlib.util.spec_from_file_location(
    "rollback_snapshot", ROOT / "scripts/tools/rollback_snapshot.py")
rb = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(rb)


class RollbackTests(unittest.TestCase):
    def fixture(self, root):
        live = root / "fake-live"
        for name in ("scripts/services/watchdog.py", "scripts/services/inverter_control.py"):
            p = live / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("ORIGINAL " + name)
        return live

    def test_capture_verify_simulate_without_changing_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = self.fixture(root)
            original = {p.name: p.read_bytes() for p in
                        (live / "scripts/services").iterdir()}
            groups = {"inverter": ("scripts/services/watchdog.py",
                                   "scripts/services/inverter_control.py")}
            snap = rb.capture(root / "private", {"inverter": live}, groups=groups)
            verified = rb.verify(snap)
            self.assertEqual(len(verified["sources"]), 2)
            self.assertFalse(verified["production_restore_allowed"])
            self.assertFalse(verified["db_restore_allowed"])
            self.assertFalse(verified["physical_auto_allowed"])
            outcome = rb.simulate(snap, root / "scratch")
            self.assertEqual(set(outcome["cases"]), {"before", "partial", "after"})
            self.assertEqual({v["status"] for v in outcome["cases"].values()},
                             {"verified"})
            self.assertEqual({p.name: p.read_bytes() for p in
                              (live / "scripts/services").iterdir()}, original)
            self.assertEqual(snap.stat().st_mode & 0o777, 0o700)
            self.assertEqual((snap / "rollback-manifest.json").stat().st_mode & 0o777,
                             0o600)

    def test_corruption_blocks_simulation_and_preserves_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = self.fixture(root)
            snap = rb.capture(root / "private", {"inverter": live},
                              groups={"inverter": ("scripts/services/watchdog.py",)})
            p = snap / "files/inverter/scripts/services/watchdog.py"
            p.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "corrupt"):
                rb.simulate(snap, root / "scratch")
            self.assertEqual(p.read_bytes(), b"tampered")
            self.assertEqual(list((root / "scratch").glob("*")) if
                             (root / "scratch").exists() else [], [])

    def test_path_rejects_unknown_modules_and_private_destinations(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                rb.approved_path("evcc", "../private.env", Path(tmp))
            for forbidden in (Path("/opt/solinteg/app/backups"),
                              Path.home(), Path("/tmp")):
                with self.assertRaises(ValueError):
                    rb.capture(forbidden, {"inverter": Path(tmp)},
                               groups={"inverter": ()})

    def test_sudo_is_opt_in_and_only_reading_approved_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dummy = root / "read-protected"
            with mock.patch.object(Path, "read_bytes", side_effect=PermissionError), \
                 mock.patch.object(rb.subprocess, "run") as runner:
                runner.return_value.stdout = b"live source bytes"
                with self.assertRaises(PermissionError):
                    rb.read_source(dummy)
                self.assertEqual(rb.read_source(dummy, sudo_read=True),
                                 b"live source bytes")
                args = runner.call_args.args[0]
                self.assertEqual(args[:3], ["sudo", "-n", "/usr/bin/cat"])


if __name__ == "__main__":
    unittest.main()
