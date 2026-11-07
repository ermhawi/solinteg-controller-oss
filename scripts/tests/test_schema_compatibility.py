"""SQLite read-only compatibility tests with isolated temporary databases."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "schema_compatibility", ROOT / "scripts/tools/schema_compatibility.py")
checker = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(checker)


class SchemaCompatibilityTests(unittest.TestCase):
    def test_exact_source_schema_is_compatible(self):
        with tempfile.TemporaryDirectory() as d:
            source = ROOT / "deploy/schema.sql"
            path = Path(d) / "database.sqlite"
            with sqlite3.connect(path) as db:
                db.executescript(source.read_text())
            result = checker.evaluate(source, path)
            self.assertEqual(result["status"], "compatible")
            self.assertEqual(result["source_tables"], result["runtime_tables"])
            self.assertEqual(result["blockers"], [])
            self.assertEqual(result["db_rows_read"], 0)
            self.assertEqual(result["runtime_writes"], 0)

    def test_missing_column_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ref = root / "ref.sql"
            ref.write_text("CREATE TABLE t(id INTEGER PRIMARY KEY, value TEXT NOT NULL);")
            db_file = root / "live.db"
            with sqlite3.connect(db_file) as db:
                db.execute("CREATE TABLE t(id INTEGER PRIMARY KEY)")
            result = checker.evaluate(ref, db_file)
            self.assertEqual(result["status"], "blocked")
            self.assertIn({"code": "missing-column", "table": "t", "column": "value"},
                          result["blockers"])

    def test_optional_extra_column_warns_not_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ref = root / "ref.sql"
            ref.write_text("CREATE TABLE t(id INTEGER PRIMARY KEY);")
            db_file = root / "live.db"
            with sqlite3.connect(db_file) as db:
                db.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, note TEXT)")
            result = checker.evaluate(ref, db_file)
            self.assertEqual(result["status"], "compatible")
            self.assertEqual(result["warnings"], [
                {"code": "extra-column", "table": "t", "column": "note"}])

    def test_new_mandatory_column_is_not_safe(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ref = root / "ref.sql"
            ref.write_text("CREATE TABLE t(id INTEGER PRIMARY KEY);")
            db_file = root / "live.db"
            with sqlite3.connect(db_file) as db:
                db.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, needs_value TEXT NOT NULL)")
            result = checker.evaluate(ref, db_file)
            self.assertEqual(result["status"], "blocked")
            self.assertIn({"code": "new-required-column",
                           "table": "t", "column": "needs_value"}, result["blockers"])

    def test_missing_runtime_database_never_creates_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "never_create.db"
            with self.assertRaises(sqlite3.OperationalError):
                checker.evaluate(ROOT / "deploy/schema.sql", path)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
