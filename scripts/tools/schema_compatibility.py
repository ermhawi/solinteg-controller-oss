#!/usr/bin/env python3
"""Read-only SQLite DDL compatibility check. Never reads table contents.

Source schema is evaluated in a temporary in-memory SQLite database. Runtime
is opened explicitly in URI mode=ro. No writes or backup to production.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote

SCHEMA = "solinteg-sqlite-compat-v1"
SAFE_TABLE = re.compile(r"^[A-Za-z0-9_]{1,100}$")


def table_schema(conn: sqlite3.Connection) -> dict[str, dict]:
    result = {}
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    for (name,) in rows:
        if not SAFE_TABLE.fullmatch(name):
            raise ValueError("invalid table identifier in SQLite schema")
        columns = {}
        for cid, column_name, declared_type, not_null, default, pk in conn.execute(
            'PRAGMA table_info("' + name + '")'
        ):
            if not SAFE_TABLE.fullmatch(column_name):
                raise ValueError("invalid column identifier")
            columns[column_name] = {
                "type": declared_type.strip().upper(),
                "not_null": bool(not_null),
                "pk": bool(pk),
                "required_insert": bool(not_null and default is None and not pk),
            }
        result[name] = columns
    return result


def compare(reference: dict[str, dict], installed: dict[str, dict]) -> dict:
    """Forbid missing/incompatible columns or newly mandatory extra columns."""
    blockers = []
    warnings = []
    for name, required in sorted(reference.items()):
        runtime = installed.get(name)
        if runtime is None:
            blockers.append({"code": "missing-table", "table": name})
            continue
        for column, attributes in sorted(required.items()):
            actual = runtime.get(column)
            if actual is None:
                blockers.append({"code": "missing-column", "table": name,
                                 "column": column})
            elif any(attributes[k] != actual[k] for k in ("type", "not_null", "pk")):
                blockers.append({"code": "incompatible-column", "table": name,
                                 "column": column})
        for extra in sorted(set(runtime) - set(required)):
            if runtime[extra]["required_insert"]:
                blockers.append({"code": "new-required-column", "table": name,
                                 "column": extra})
            else:
                warnings.append({"code": "extra-column", "table": name, "column": extra})
    for name in sorted(set(installed) - set(reference)):
        warnings.append({"code": "extra-table", "table": name})
    return {"schema": SCHEMA, "status": "blocked" if blockers else "compatible",
            "source_tables": len(reference), "runtime_tables": len(installed),
            "blockers": blockers, "warnings": warnings}


def evaluate(source: Path, runtime: Path) -> dict:
    with closing(sqlite3.connect(":memory:")) as expected:
        sql = source.read_text(encoding="utf-8")
        expected.executescript(sql)
        reference = table_schema(expected)
    uri = "file:" + quote(str(runtime.resolve()), safe="/") + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=3)) as live:
        live.execute("PRAGMA query_only=ON")
        installed = table_schema(live)
        versions = {"source_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                    "schema_version": live.execute("PRAGMA schema_version").fetchone()[0],
                    "user_version": live.execute("PRAGMA user_version").fetchone()[0]}
    return {**compare(reference, installed), **versions, "db_rows_read": 0,
            "runtime_writes": 0}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path,
                        default=Path(__file__).resolve().parents[2] / "deploy/schema.sql")
    parser.add_argument("--live-db", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.source, args.live_db)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "compatible" else 8


if __name__ == "__main__":
    raise SystemExit(main())
