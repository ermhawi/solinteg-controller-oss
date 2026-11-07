"""T06: synthetic SQLite WAL backup and incident replay; never uses live paths."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "critical_state_backup", ROOT / "scripts/tools/critical_state_backup.py"
)
backup = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(backup)


class CriticalStateBackupTests(unittest.TestCase):
    def create_incident(self, root: Path) -> tuple[Path, dict[str, Path]]:
        db_path = root / "source.sqlite"
        with sqlite3.connect(db_path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE control_actions (id INTEGER PRIMARY KEY, outcome TEXT)")
            db.execute("CREATE TABLE readings (id INTEGER PRIMARY KEY, soc_pct REAL)")
            db.execute("INSERT INTO control_actions (outcome) VALUES ('error_revert_failed')")
            db.execute("INSERT INTO readings(soc_pct) VALUES (13.0)")
        heartbeat = root / "heartbeat.json"
        heartbeat.write_text('{"result":"error_revert_failed","timestamp":"2026-11-07T08:00:00Z"}')
        owner = root / "owner.json"
        owner.write_text('{"state":"pending","verified_fc10_pause":false}')
        return db_path, {"heartbeat": heartbeat, "ev_price_pause_owner": owner}

    def test_backup_and_restore_reproduce_control_failure_and_pending_ev_pause(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db_path, files = self.create_incident(root)
            source_before = db_path.read_bytes()
            originals = {k: v.read_bytes() for k, v in files.items()}
            result = backup.capture(db_path, files, root / "backups")
            self.assertEqual(result["sqlite_tables"], 2)
            self.assertEqual(result["json_captured"], 2)
            self.assertFalse(result["release_go"])
            snapshot = root / "backups" / result["directory"]
            verified = backup.verify(snapshot)
            self.assertEqual(verified["sqlite_metadata"]["tables"],
                             ["control_actions", "readings"])
            self.assertEqual(verified["sqlite_metadata"]["integrity"], "ok")
            with backup.closing(sqlite3.connect(snapshot / "telemetry.sqlite")) as db:
                self.assertEqual(db.execute(
                    "SELECT outcome FROM control_actions").fetchone()[0],
                    "error_revert_failed")
            self.assertEqual(json.loads((snapshot / "ev_price_pause_owner.json").read_text())
                             ["state"], "pending")
            restore = backup.simulate_restore(snapshot, root / "scratch")
            self.assertEqual(restore["simulation"], "verified")
            self.assertEqual((restore["production_writes"], restore["physical_writes"]), (0, 0))
            self.assertEqual(db_path.read_bytes(), source_before)
            self.assertEqual({k: v.read_bytes() for k, v in files.items()}, originals)

    def test_missing_optional_state_is_explicitly_absent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db, sources = self.create_incident(root)
            sources["evcc_cable"] = root / "no-cable-ever.json"
            result = backup.capture(db, sources, root / "backups")
            manifest = backup.verify(root / "backups" / result["directory"])
            self.assertEqual(result["json_absent"], 1)
            self.assertEqual(manifest["states"]["evcc_cable"]["state"], "absent")

    def test_private_snapshot_and_no_raw_secrets_in_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db, sources = self.create_incident(root)
            sources["heartbeat"].write_text(
                '{"access_token":"PRIVATE_SENTINEL","result":"error_revert_failed"}')
            result = backup.capture(db, sources, root / "backups")
            snap = root / "backups" / result["directory"]
            manifest = (snap / "manifest.json").read_text()
            self.assertNotIn("PRIVATE_SENTINEL", manifest)
            self.assertNotIn(str(root), manifest)
            self.assertEqual(snap.stat().st_mode & 0o777, 0o700)
            for p in (snap / "manifest.json", snap / "telemetry.sqlite", snap / "heartbeat.json"):
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_corrupt_backup_and_state_block_restore_without_touching_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db, sources = self.create_incident(root)
            result = backup.capture(db, sources, root / "backups")
            snap = root / "backups" / result["directory"]
            (snap / "heartbeat.json").write_text('{"result":"safe"}')
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                backup.simulate_restore(snap, root / "scratch")
            self.assertEqual(db.exists(), True)
            self.assertFalse((root / "scratch").exists())

    def test_rejects_wrong_destination_and_unapproved_state_label(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db, sources = self.create_incident(root)
            for destination in (Path("/opt/solinteg/app/state"), Path("/tmp"), Path.home()):
                with self.subTest(destination=destination), self.assertRaises(ValueError):
                    backup.capture(db, sources, destination)
            with self.assertRaises(ValueError):
                backup.capture(db, {"private_env": root / ".env"}, root / "backups")
            self.assertFalse((root / "backups").exists())

    def test_state_symlink_and_invalid_json_fail_before_backup_publish(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db, sources = self.create_incident(root)
            link = root / "link.json"
            link.symlink_to(sources["heartbeat"])
            with self.assertRaisesRegex(ValueError, "unsafe critical JSON"):
                backup.capture(db, {"heartbeat": link}, root / "backups")
            sources["heartbeat"].write_text("{malformed")
            with self.assertRaises(json.JSONDecodeError):
                backup.capture(db, {"heartbeat": sources["heartbeat"]}, root / "backups")
            self.assertEqual(list((root / "backups").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
