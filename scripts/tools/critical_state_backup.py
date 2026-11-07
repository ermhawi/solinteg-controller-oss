#!/usr/bin/env python3
"""Private consistent SQLite+critical JSON snapshot and scratch incident restore.

Reads SQLite with mode=ro + sqlite3.backup; no query of data rows. Copies only
explicitly named critical JSON state, never environment files or credentials.
Writes only to a private user-selected staging root, never a live runtime.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
from urllib.parse import quote

SCHEMA = "solinteg-critical-state-backup-v1"
ALLOWED_JSON = frozenset({
    "live", "heartbeat", "evcc_readonly", "evcc_cable",
    "ev_price_pause_owner", "ev_price_controller_status",
})
MAX_JSON_BYTES = 2 * 1024 * 1024
DEFAULT_ROOT = Path.home() / ".local/state/solinteg-critical-snapshots"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def private_destination(root: Path) -> None:
    absolute = root.expanduser().resolve()
    if absolute in (Path.home().resolve(), Path("/tmp")):
        raise ValueError("dedicated private snapshot directory is mandatory")
    if (not absolute.is_relative_to(Path.home().resolve())
            and not absolute.is_relative_to(Path("/tmp"))):
        raise ValueError("snapshot destination must be within home or /tmp")
    if root.is_symlink() or root.parent.is_symlink():
        raise ValueError("snapshot directory cannot be a symlink")


def source_db(db_path: Path):
    uri = "file:" + quote(str(db_path.resolve()), safe="/") + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5)


def validate_sqlite(db_path: Path) -> dict:
    with closing(source_db(db_path)) as source:
        source.execute("PRAGMA query_only=ON")
        result = source.execute("PRAGMA quick_check").fetchone()
        if result != ("ok",):
            raise ValueError("SQLite snapshot quick_check failed")
        tables = sorted(
            name for (name,) in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        )
        if not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name) for name in tables):
            raise ValueError("unsupported table name")
        return {
            "schema_version": source.execute("PRAGMA schema_version").fetchone()[0],
            "user_version": source.execute("PRAGMA user_version").fetchone()[0],
            "tables": tables,
            "integrity": "ok",
        }


def read_critical_json(path: Path) -> bytes | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JSON_BYTES
            or info.st_size < 1):
        raise ValueError("unsafe critical JSON state")
    data = path.read_bytes()
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("critical JSON oversized")
    json.loads(data)
    return data


def capture(db_path: Path, json_sources: dict[str, Path], root: Path) -> dict:
    """Atomic, immutable private evidence. Missing optional JSON is explicitly recorded."""
    private_destination(root)
    if not set(json_sources) <= ALLOWED_JSON:
        raise ValueError("unsupported state label")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    old_mask = os.umask(0o077)
    directory = Path(tempfile.mkdtemp(prefix="incident-", dir=root))
    try:
        db_copy = directory / "telemetry.sqlite"
        with closing(source_db(db_path)) as live, closing(sqlite3.connect(db_copy)) as dst:
            live.execute("PRAGMA query_only=ON")
            live.backup(dst)
        db_copy.chmod(0o600)
        metadata = validate_sqlite(db_copy)
        json_records = {}
        for label, original_path in sorted(json_sources.items()):
            data = read_critical_json(original_path)
            if data is None:
                json_records[label] = {"state": "absent"}
                continue
            target = directory / (label + ".json")
            with target.open("xb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            target.chmod(0o600)
            json_records[label] = {
                "state": "captured", "sha256": digest(data), "bytes": len(data)
            }
        summary = {
            "schema": SCHEMA, "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "sqlite_sha256": digest(db_copy.read_bytes()),
            "sqlite_metadata": metadata,
            "states": json_records,
            "device_writes": 0,
            "production_restore_allowed": False,
            "operator_go": False,
        }
        manifest = directory / "manifest.json"
        with manifest.open("x", encoding="utf-8") as file:
            json.dump(summary, file, indent=2, sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
        manifest.chmod(0o600)
        return {"directory": directory.name, "sqlite_tables": len(metadata["tables"]),
                "json_captured": sum(v["state"] == "captured" for v in json_records.values()),
                "json_absent": sum(v["state"] == "absent" for v in json_records.values()),
                "manifest_sha256": digest(manifest.read_bytes()),
                "release_go": False}
    except BaseException:
        shutil.rmtree(directory)
        raise
    finally:
        os.umask(old_mask)


def verify(snapshot: Path) -> dict:
    manifest = snapshot / "manifest.json"
    if manifest.is_symlink():
        raise ValueError("untrusted manifest")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA or data.get("production_restore_allowed") is not False:
        raise ValueError("incompatible or unsafe restore manifest")
    sqlite_path = snapshot / "telemetry.sqlite"
    if sqlite_path.is_symlink() or digest(sqlite_path.read_bytes()) != data["sqlite_sha256"]:
        raise ValueError("SQLite backup hash mismatch")
    if validate_sqlite(sqlite_path) != data["sqlite_metadata"]:
        raise ValueError("SQLite backup schema mismatch")
    for label, row in data["states"].items():
        if label not in ALLOWED_JSON or row.get("state") not in ("absent", "captured"):
            raise ValueError("unsupported state in manifest")
        if row["state"] == "captured":
            path = snapshot / (label + ".json")
            if path.is_symlink():
                raise ValueError("untrusted state file")
            content = read_critical_json(path)
            if content is None or digest(content) != row.get("sha256") or len(content) != row.get("bytes"):
                raise ValueError("critical state hash mismatch")
    return data


def simulate_restore(snapshot: Path, destination: Path) -> dict:
    """Model incident with a corrupt scratch DB/JSON, then restore only scratch."""
    private_destination(destination)
    data = verify(snapshot)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = Path(tempfile.mkdtemp(prefix="restore-", dir=destination))
    try:
        db_copy = directory / "telemetry.sqlite"
        shutil.copyfile(snapshot / "telemetry.sqlite", db_copy)
        with closing(sqlite3.connect(db_copy)) as connection:
            connection.execute("CREATE TABLE synthetic_incident_marker (id INTEGER)")
            connection.commit()
        # Exercise tamper detection then restore exact bytes from verified backup.
        if digest(db_copy.read_bytes()) == data["sqlite_sha256"]:
            raise AssertionError("scratch incident did not alter SQLite")
        shutil.copyfile(snapshot / "telemetry.sqlite", db_copy)
        for label, row in data["states"].items():
            if row["state"] != "captured":
                continue
            scratch = directory / (label + ".json")
            scratch.write_text("{corrupt", encoding="utf-8")
            shutil.copyfile(snapshot / (label + ".json"), scratch)
            if digest(scratch.read_bytes()) != row["sha256"]:
                raise AssertionError("scratch state restore failed")
        if digest(db_copy.read_bytes()) != data["sqlite_sha256"]:
            raise AssertionError("scratch DB restore failed")
        if validate_sqlite(db_copy) != data["sqlite_metadata"]:
            raise AssertionError("scratch DB schema restore failed")
        verify(snapshot)
        return {"simulation": "verified", "sqlite_tables": len(data["sqlite_metadata"]["tables"]),
                "state_files": sum(x["state"] == "captured" for x in data["states"].values()),
                "production_writes": 0, "physical_writes": 0}
    finally:
        shutil.rmtree(directory)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="source SQLite database; opened read-only")
    parser.add_argument("--json", action="append", default=[], metavar="LABEL=PATH")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--simulate", type=Path, help="verify and exercise existing snapshot")
    args = parser.parse_args()
    if args.simulate:
        result = simulate_restore(args.simulate, args.output_root)
    else:
        if args.db is None:
            parser.error("--db is required unless --simulate")
        sources = {}
        for entry in args.json:
            label, sep, path = entry.partition("=")
            if not sep or label not in ALLOWED_JSON or label in sources or not path:
                parser.error("invalid or repeated --json label")
            sources[label] = Path(path)
        result = capture(args.db, sources, args.output_root)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
