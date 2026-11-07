#!/usr/bin/env python3
"""Private source-only rollback snapshot and selective scratch restore.

Never installs, changes systemd, reads env/DB records or touches Modbus/EVCC.
Explicit --sudo-read allows only sudo -n cat on allowlisted module paths.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from release_inventory import MODULES

SCHEMA = "solinteg-rollback-source-v1"
DEFAULT_ROOT = Path.home() / ".local/state/solinteg-release-staging"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def private_root(path: Path) -> None:
    resolved = path.expanduser().resolve()
    if not (resolved.is_relative_to(Path.home().resolve())
            or resolved.is_relative_to(Path("/tmp"))):
        raise ValueError("snapshot must be under home or /tmp")
    if resolved in (Path.home().resolve(), Path("/tmp")):
        raise ValueError("snapshot must use a dedicated private subdirectory")
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("snapshot path must not be a symlink")


def approved_path(group: str, relative: str, base: Path) -> Path:
    if relative not in MODULES.get(group, ()):
        raise ValueError("source path not allowlisted")
    path = base / relative
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError("module path escapes approved root")
    return path


def read_source(path: Path, *, sudo_read: bool = False, max_bytes: int = 10_000_000) -> bytes:
    try:
        data = path.read_bytes()
    except PermissionError:
        if not sudo_read:
            raise
        data = subprocess.run(["sudo", "-n", "/usr/bin/cat", str(path)],
                              check=True, capture_output=True, timeout=12).stdout
    if len(data) > max_bytes:
        raise ValueError("oversized source")
    return data


def capture(root: Path, locations: dict[str, Path], *,
            groups: dict[str, tuple[str, ...]] | None = None,
            sudo_read: bool = False) -> Path:
    """Create hash-verified source-only backup; never read private env or DB."""
    private_root(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    selected = MODULES if groups is None else groups
    snap = Path(tempfile.mkdtemp(prefix="rollback-", dir=root))
    inventory = []
    try:
        for group, paths in selected.items():
            base = locations[group]
            for relative in paths:
                if relative not in MODULES.get(group, ()):
                    raise ValueError("unapproved source path")
                source = approved_path(group, relative, base)
                data = read_source(source, sudo_read=sudo_read)
                dest = snap / "files" / group / relative
                dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with dest.open("xb") as stream:
                    stream.write(data)
                dest.chmod(0o600)
                inventory.append({"chain": group, "path": relative,
                                  "sha256": sha(data), "bytes": len(data)})
        metadata = {"schema": SCHEMA, "sources": inventory,
                    "purpose": "source-rollback-only", "production_restore_allowed": False,
                    "db_restore_allowed": False, "systemd_restore_allowed": False,
                    "physical_auto_allowed": False}
        path = snap / "rollback-manifest.json"
        with path.open("x", encoding="utf-8") as file:
            json.dump(metadata, file, sort_keys=True, indent=2)
        path.chmod(0o600)
        return snap
    except BaseException:
        shutil.rmtree(snap)
        raise


def verify(snapshot: Path) -> dict:
    """Verify saved bytes without editing originals or contacting hardware."""
    data = json.loads((snapshot / "rollback-manifest.json").read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise ValueError("incompatible snapshot")
    seen = set()
    for entry in data["sources"]:
        group, relative = entry["chain"], entry["path"]
        if relative not in MODULES.get(group, ()) or (group, relative) in seen:
            raise ValueError("unapproved or duplicate source")
        seen.add((group, relative))
        path = snapshot / "files" / group / relative
        if not path.resolve().is_relative_to(snapshot.resolve()):
            raise ValueError("backup path escape")
        value = path.read_bytes()
        if sha(value) != entry["sha256"] or len(value) != entry["bytes"]:
            raise ValueError("corrupt rollback snapshot")
    return data


def simulate(snapshot: Path, root: Path) -> dict:
    """Simulate pre/partial/post file swap entirely inside private scratch."""
    private_root(root)
    data = verify(snapshot)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    results = {}
    sources = data["sources"]
    for case in ("before", "partial", "after"):
        scratch = Path(tempfile.mkdtemp(prefix="simulate-" + case + "-", dir=root))
        try:
            for index, entry in enumerate(sources):
                original = (snapshot / "files" / entry["chain"] / entry["path"]).read_bytes()
                target = scratch / entry["chain"] / entry["path"]
                target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                applied = case == "after" or (case == "partial" and index % 2 == 0)
                with target.open("xb") as file:
                    file.write(original + b"\nSIMULATED NEW CODE\n" if applied else original)
                if sha(target.read_bytes()) != entry["sha256"]:
                    with target.open("wb") as file:
                        file.write(original)
                if sha(target.read_bytes()) != entry["sha256"]:
                    raise AssertionError("scratch restore failed")
            results[case] = {"status": "verified", "files": len(sources)}
        finally:
            shutil.rmtree(scratch)
    return {"schema": "solinteg-rollback-simulation-v1",
            "production_writes": 0, "physical_operations": 0, "cases": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--app", type=Path, default=Path("/opt/solinteg/app"))
    parser.add_argument("--ev", type=Path, default=Path("/opt/solinteg/ev-shadow-runtime"))
    parser.add_argument("--sudo-read", action="store_true",
                        help="sudo -n cat only for whitelisted unreadable source files")
    parser.add_argument("--simulate", type=Path,
                        help="existing private snapshot; no active file changes")
    args = parser.parse_args()
    old_mask = os.umask(0o077)
    try:
        if args.simulate:
            result = simulate(args.simulate, args.output_root)
            print(json.dumps(result, sort_keys=True))
            return 0
        locations = {"inverter": args.app, "optimizer_web": args.app, "evcc": args.ev}
        path = capture(args.output_root, locations, sudo_read=args.sudo_read)
        verified = verify(path)
        print(json.dumps({"snapshot": path.name, "files": len(verified["sources"]),
                          "content_hash": sha((path / "rollback-manifest.json").read_bytes()),
                          "production_writes": 0, "release_go": False}, sort_keys=True))
        return 0
    finally:
        os.umask(old_mask)


if __name__ == "__main__":
    raise SystemExit(main())
