#!/usr/bin/env python3
"""Build a private, nondeploying release candidate from a *committed* Git tree.

This tool only reads Git and optionally systemd's public unit metadata.
It NEVER reads private environment values, writes /opt, installs, restarts
services, or contacts Modbus/EVCC. It is NOT permission to deploy.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile

from release_inventory import MODULES, UNITS, validate_source_manifest


SCHEMA = "solinteg-private-release-candidate-v1"
DEPENDENCIES = (
    "package-lock.json", "package.json", "scripts/requirements.txt",
    "requirements-docs.txt", "deploy/schema.sql",
)
SAFE_REF = re.compile(r"^[0-9a-f]{40}$")
SAFE_UNIT = re.compile(r"^[a-zA-Z0-9_.@-]+\.(?:service|timer)$")
DEFAULT_ROOT = Path.home() / ".local/state/solinteg-release-staging"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checked_output(args: list[str], *, cwd: Path) -> bytes:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True,
                          timeout=60).stdout


def committed_tree(repo: Path) -> tuple[str, bytes]:
    sha = checked_output(["git", "rev-parse", "HEAD"], cwd=repo).decode("ascii").strip()
    if not SAFE_REF.fullmatch(sha):
        raise ValueError("unverifiable HEAD")
    # Never include the working tree, so pre-existing dirty files are preserved.
    tar_data = checked_output(["git", "archive", "--format=tar", sha], cwd=repo)
    return sha, tar_data


def safe_member(name: str) -> bool:
    if not isinstance(name, str) or "\x00" in name:
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and all(x not in (".", "..", "") for x in path.parts)


def extract_regular_source(archive: bytes, dst: Path) -> dict[str, str]:
    hashes = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        for member in tar:
            name = member.name.rstrip("/")
            if not safe_member(name):
                raise ValueError("unsafe Git archive entry")
            target = dst.joinpath(*PurePosixPath(name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            if not member.isfile() or member.size > 30_000_000:
                raise ValueError("unsupported Git archive object")
            data_file = tar.extractfile(member)
            if data_file is None:
                raise ValueError("unreadable archive entry")
            data = data_file.read()
            if len(data) != member.size:
                raise ValueError("truncated archive entry")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open("xb") as stream:
                stream.write(data)
            target.chmod(0o700 if member.mode & 0o111 else 0o600)
            hashes[name] = digest(data)
    return hashes


def template_snapshot(staged: Path, hashes: dict[str, str]) -> dict:
    files = {}
    for dep in DEPENDENCIES:
        files[dep] = hashes.get(dep)
    missing = [p for p, sha in files.items() if sha is None]
    schema_tables = []
    schema = staged / "deploy/schema.sql"
    if schema.is_file():
        source_sql = re.sub(r"--[^\n]*", "", schema.read_text(encoding="utf-8"))
        for match in re.finditer(r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_]\w*)",
                                 source_sql, flags=re.I):
            schema_tables.append(match.group(1))
    units = {
        path: sha for path, sha in hashes.items()
        if path.startswith("deploy/") and path.endswith((".service", ".timer"))
    }
    return {"dependencies": files, "missing_dependencies": missing,
            "schema_tables": sorted(set(schema_tables)),
            "unit_templates": units}


def read_unit_parity(staged: Path, *, runner=subprocess.run) -> dict:
    """Read-only public unit hash comparison; missing/unreadable stays unknown."""
    results = {}
    for unit in UNITS:
        if not SAFE_UNIT.fullmatch(unit):
            continue
        try:
            p = runner(["systemctl", "show", "--no-pager", "-p", "LoadState",
                        "-p", "ActiveState", "-p", "FragmentPath", unit],
                       check=False, capture_output=True, text=True, timeout=5)
            attrs = dict(line.split("=", 1) for line in p.stdout.splitlines()
                         if "=" in line) if p.returncode == 0 else {}
        except (OSError, subprocess.SubprocessError):
            attrs = {}
        fragment = attrs.get("FragmentPath", "")
        staged_unit = staged / "deploy" / unit
        source_hash = digest(staged_unit.read_bytes()) if staged_unit.is_file() else None
        installed_hash = None
        if fragment.startswith("/") and not any(x in fragment for x in ("\x00", "\n")):
            try:
                installed_hash = digest(Path(fragment).read_bytes())
            except OSError:
                pass
        if source_hash is not None and installed_hash is not None:
            parity = "equal" if source_hash == installed_hash else "different"
        else:
            parity = "unverified"
        results[unit] = {
            "load": attrs.get("LoadState") if attrs.get("LoadState") in
                    ("loaded", "not-found", "masked") else "unavailable",
            "active": attrs.get("ActiveState") if attrs.get("ActiveState") in
                      ("active", "inactive", "failed", "activating", "deactivating")
                      else "unavailable",
            "source_hash": source_hash,
            "installed_hash": installed_hash,
            "parity": parity,
        }
    return results


def assert_private_stage_root(root: Path) -> None:
    resolved = root.expanduser().resolve()
    if not (resolved.is_relative_to(Path.home().resolve())
            or resolved.is_relative_to(Path("/tmp"))):
        raise ValueError("stage must be under user's home or /tmp")
    if resolved in (Path.home().resolve(), Path("/tmp")):
        raise ValueError("stage must use a dedicated private subdirectory")
    if resolved == Path("/opt") or Path("/opt") in resolved.parents:
        raise ValueError("cannot stage within a live installation")
    if any(p.is_symlink() for p in (root, root.parent)):
        raise ValueError("stage root must not be a symlink")


def create_candidate(repo: Path, root: Path, *, inspect_systemd: bool = False) -> dict:
    """Create a new immutable candidate directory. No runtime writes."""
    assert_private_stage_root(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    head, archive = committed_tree(repo)
    tmp = Path(tempfile.mkdtemp(prefix="candidate-" + head[:12] + "-", dir=root))
    try:
        hashes = extract_regular_source(archive, tmp)
        validation = validate_source_manifest(tmp)
        templates = template_snapshot(tmp, hashes)
        units = read_unit_parity(tmp) if inspect_systemd else {}
        manifest = {
            "schema": SCHEMA,
            "git_sha": head,
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "source": {"file_count": len(hashes), "file_hashes": hashes,
                       "content_sha256": digest(json.dumps(hashes, sort_keys=True).encode())},
            "runtime_modules": {chain: list(paths) for chain, paths in MODULES.items()},
            "template_inventory": templates,
            "source_validation": validation,
            "systemd_parity": units,
            "rollback_snapshot": "R08-pending",
            "release_go": False,
            "stop_reasons": ["site-parity-unverified", "rollback-unverified"] +
                            (["source-invalid"] if validation["status"] != "pass" else []) +
                            (["dependency-manifest-incomplete"] if templates["missing_dependencies"] else []),
        }
        with (tmp / "candidate-manifest.json").open("x", encoding="utf-8") as output:
            json.dump(manifest, output, sort_keys=True, indent=2)
            output.write("\n")
        (tmp / "candidate-manifest.json").chmod(0o600)
        return {"stage": tmp, "git_sha": head, "files": len(hashes),
                "manifest_sha256": digest((tmp / "candidate-manifest.json").read_bytes()),
                "content_sha256": manifest["source"]["content_sha256"],
                "source_validation": validation["status"], "release_go": False}
    except BaseException:
        shutil.rmtree(tmp)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--inspect-systemd", action="store_true",
                        help="Read only hashed public systemd metadata; NO service writes")
    args = parser.parse_args()
    old_umask = os.umask(0o077)
    try:
        result = create_candidate(args.repo, args.output_root,
                                  inspect_systemd=args.inspect_systemd)
    finally:
        os.umask(old_umask)
    # The manifest and full paths stay private. Output only safe, opaque IDs.
    print(json.dumps({key: (value.name if isinstance(value, Path) else value)
                      for key, value in result.items()}, sort_keys=True))
    return 0 if result["source_validation"] == "pass" else 6


if __name__ == "__main__":
    raise SystemExit(main())
