#!/usr/bin/env python3
"""Sanitized, read-only release evidence. No network, Modbus, EVCC or service writes.

Run as an unprivileged user to record inaccessible runtime data explicitly;
use read-only sudo if the operator permits reading private installation files.
Output never contains raw systemd arguments, environment or absolute site paths.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess


MODULES = {
    "inverter": (
        "scripts/services/dispatch_loop.py",
        "scripts/services/inverter_control.py",
        "scripts/services/energy_priority_policy.py",
        "scripts/services/modbus_poller.py",
        "scripts/services/watchdog.py",
        "scripts/services/modbus_broker.py",
        "scripts/services/modbus_broker_control_service.py",
        "scripts/services/modbus_broker_ipc.py",
    ),
    "optimizer_web": (
        "lib/optimizer.ts", "lib/plan.ts", "lib/prices.ts",
        "app/api/ev/settings/route.ts",
    ),
    "evcc": (
        # Four systemd entrypoints plus their transitive local Python imports.
        "scripts/services/ev_price_controller.py",
        "scripts/services/evcc_observer.py",
        "scripts/services/ev_history.py",
        "scripts/services/ha_ev_session.py",
        "scripts/services/ev_price_empirical_session.py",
        "scripts/services/ev_price_pause_owner.py",
        "scripts/services/ev_price_policy.py",
        "scripts/services/ev_volvo_smart_charging.py",
        "scripts/services/evcc_ascii.py",
        "scripts/services/evcc_cable_tracker.py",
        "scripts/services/ev_charge_end.py",
        "scripts/services/common.py",
    ),
}
UNITS = (
    "solinteg-dispatch.service", "solinteg-poller.service",
    "solinteg-broker-control.service", "solinteg-watchdog.timer",
    "solinteg-ev-price-controller.service", "solinteg-ev-price-controller.timer",
    "solinteg-ev-observer.service", "solinteg-ev-history.service",
    "solinteg-ev-ha.service", "solinteg-web.service",
    "wifi-recovery-watchdog.timer",
)
# Only explicit, low-entropy operational switches may leave the machine.
FLAGS = {
    "solinteg-dispatch.service": {
        "SOLINTEG_CONTROL_ARMED": ("scripts/services/inverter_control.py", ("", "0", "1")),
        "SOLINTEG_MODBUS_SOURCE": ("scripts/services/inverter_control.py", ("direct", "broker", "broker_transaction")),
        "DISPATCH_ENERGY_PRIORITY": ("scripts/services/dispatch_loop.py", ("0", "1")),
        "DISPATCH_RESERVE_HOLD_VERIFIED": ("scripts/services/dispatch_loop.py", ("0", "1")),
    },
    "solinteg-broker-control.service": {
        "SOLINTEG_CONTROL_ARMED": ("scripts/services/inverter_control.py", ("", "0", "1")),
        "SOLINTEG_BROKER_TRANSACTIONS_ENABLED": ("scripts/services/modbus_broker_control_service.py", ("0", "1")),
        "SOLINTEG_BROKER_HOLD_PROBE_ENABLED": ("scripts/services/modbus_broker_control_service.py", ("", "0", "1")),
    },
}
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
SAFE_SHA = re.compile(r"^[a-f0-9]{40}$")


def basename(path: str) -> str:
    name = Path(path).name
    return name if SAFE_NAME.fullmatch(name) else "redacted"


def sha256(path: Path) -> tuple[str, str | None]:
    try:
        return "ok", hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "missing", None
    except (PermissionError, IsADirectoryError, OSError):
        return "not-readable", None


def parity(source: Path, installed: Path) -> dict:
    src_status, src_hash = sha256(source)
    dst_status, dst_hash = sha256(installed)
    if src_status != "ok":
        comparison = "source-" + src_status
    elif dst_status != "ok":
        comparison = dst_status
    else:
        comparison = "equal" if src_hash == dst_hash else "different"
    return {"source_sha256": src_hash, "installed_sha256": dst_hash, "parity": comparison}


def git_head(repo: Path) -> str | None:
    try:
        p = subprocess.run(["git", "-c", "safe.directory=" + str(repo.resolve()), "-C", str(repo), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=5, check=True)
        value = p.stdout.strip()
        return value if SAFE_SHA.fullmatch(value) else None
    except (OSError, subprocess.SubprocessError):
        return None


def git_dirty(repo: Path) -> bool | None:
    try:
        p = subprocess.run(["git", "-c", "safe.directory=" + str(repo.resolve()), "-C", str(repo), "status", "--porcelain"],
                           capture_output=True, text=True, timeout=8, check=True)
        return bool(p.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return None


def read_unit(unit: str, runner=subprocess.run) -> tuple[dict, int, list[tuple[str, Path]]]:
    public = {"load": "unavailable", "active": "unavailable",
              "sub": "unavailable", "exec_binary": None,
              "exec_entry": None, "fragment": None, "drop_ins": []}
    try:
        p = runner(["systemctl", "show", "--no-pager",
                    "-p", "LoadState", "-p", "ActiveState", "-p", "SubState",
                    "-p", "MainPID", "-p", "ExecStart", "-p", "FragmentPath",
                    "-p", "DropInPaths", unit],
                   capture_output=True, text=True, timeout=6)
        if p.returncode != 0:
            return public, 0, []
        fields = dict(line.split("=", 1) for line in p.stdout.splitlines() if "=" in line)
        for field, key in (("LoadState", "load"), ("ActiveState", "active"),
                           ("SubState", "sub")):
            value = fields.get(field, "")
            public[key] = value if SAFE_NAME.fullmatch(value) else "unknown"
        match = re.search(r"(?:^|[;{ ])path=([^ ;}]+)", fields.get("ExecStart", ""))
        if match:
            public["exec_binary"] = basename(match.group(1))
        entries = re.findall(r"/[A-Za-z0-9_./-]+\.(?:py|js|mjs)", fields.get("ExecStart", ""))
        if entries:
            public["exec_entry"] = basename(entries[-1])
        fragment = fields.get("FragmentPath", "")
        public["fragment"] = basename(fragment) if fragment else None
        drops = [Path(v) for v in fields.get("DropInPaths", "").split() if v.startswith("/")]
        public["drop_ins"] = [basename(str(p)) for p in drops]
        config = ([("unit", Path(fragment))] if fragment.startswith("/") else [])
        config += [("drop-in", f) for f in drops]
        pid_text = fields.get("MainPID", "0")
        pid = int(pid_text) if pid_text.isdecimal() else 0
        return public, pid if pid > 0 else 0, config
    except (OSError, subprocess.SubprocessError, ValueError):
        return public, 0, []


def extract_assignments(config: list[tuple[str, Path]], accepted: set[str]) -> dict[str, tuple[str, str, str]]:
    """Find only literal whitelisted flags in systemd Environment and EnvironmentFile.

    Last explicit assignment is a provenance *candidate*, not proof of an
    effective process value. Unsupported systemd expansion is left unattributed.
    """
    found = {}
    for kind, path in config:
        try:
            data = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for line in data.splitlines():
            line = line.strip()
            if line.startswith("Environment="):
                try:
                    tokens = shlex.split(line.split("=", 1)[1])
                except ValueError:
                    continue
                for token in tokens:
                    key, sep, value = token.partition("=")
                    if sep and key in accepted:
                        found[key] = (value, kind, basename(str(path)))
            elif line.startswith("EnvironmentFile="):
                try:
                    files = shlex.split(line.split("=", 1)[1])
                except ValueError:
                    continue
                for filename in files:
                    f = Path(filename.lstrip("-"))
                    if not f.is_absolute() or "%" in str(f):
                        continue
                    try:
                        lines = f.read_text(encoding="utf-8").splitlines()
                    except (OSError, UnicodeError):
                        continue
                    for entry in lines:
                        entry = entry.strip()
                        if entry.startswith("#") or "=" not in entry:
                            continue
                        key, value = entry.split("=", 1)
                        if key in accepted:
                            found[key] = (value.strip().strip('"').strip("'"), "environment-file", basename(str(f)))
    return found


def process_flags(pid: int, accepted: set[str]) -> tuple[str, dict[str, str]]:
    if not pid:
        return "not-running", {}
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return "not-readable", {}
    result = {}
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep:
            name = key.decode("ascii", "ignore")
            if name in accepted:
                result[name] = value.decode("ascii", "ignore")
    return "ok", result


def source_default(repo: Path, name: str, source: str, allowed: tuple[str, ...]) -> str | None:
    try:
        data = (repo / source).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    pattern = r'(?:os\.environ\.get|environ\.get)\(\s*["\']' + re.escape(name) + r'["\'](?:\s*,\s*["\']([^"\']*)["\'])?\s*\)'
    matches = re.findall(pattern, data)
    if not matches:
        return None
    value = matches[0]
    return value if value in allowed else None


def snapshot(repo: Path, installed: Path, ev_runtime: Path,
             *, rollback_ref: str | None = None, tests: dict | None = None,
             runner=subprocess.run) -> dict:
    modules = []
    for chain, paths in MODULES.items():
        for path in paths:
            root = ev_runtime if chain == "evcc" else installed
            modules.append({"chain": chain, "path": path,
                            "runtime": "ev" if chain == "evcc" else "app",
                            **parity(repo / path, root / path)})
    units = {}
    flags = {}
    for unit in UNITS:
        public, pid, config = read_unit(unit, runner=runner)
        units[unit] = public
        if unit not in FLAGS:
            continue
        spec = FLAGS[unit]
        status, values = process_flags(pid, set(spec))
        assignments = extract_assignments(config, set(spec))
        for name, (source, allowed) in spec.items():
            actual = values.get(name)
            safe_actual = actual if actual in allowed else ("unrecognized" if actual is not None else None)
            candidate = assignments.get(name)
            provenance = {"kind": "process-unattributed", "file": None}
            if actual is not None and candidate and candidate[0] == actual:
                provenance = {"kind": candidate[1], "file": candidate[2]}
            flags[unit + ":" + name] = {
                "source_default": source_default(repo, name, source, allowed),
                "effective": safe_actual,
                "read_status": status if status != "ok" else ("set" if actual is not None else "unset"),
                "origin": provenance,
            }
    return {
        "schema": "solinteg-sanitized-release-v1",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "repo_head": git_head(repo),
        "repo_dirty": git_dirty(repo),
        "modules": modules,
        "units": units,
        "flags": flags,
        "tests": tests if tests is not None else {"status": "not-supplied"},
        "rollback_reference": rollback_ref,
    }


def compare_snapshots(previous: dict, current: dict) -> dict:
    """Fail closed on unreadable runtime. Report only canonical names, never raw values."""
    changed = []
    if previous.get("repo_head") != current.get("repo_head"):
        changed.append({"kind": "git", "id": "HEAD"})
    if previous.get("repo_dirty") != current.get("repo_dirty"):
        changed.append({"kind": "git", "id": "working-tree"})
    old_mods = {m.get("path"): m for m in previous.get("modules", [])
                if isinstance(m, dict) and m.get("path") in {p for group in MODULES.values() for p in group}}
    for module in current["modules"]:
        old = old_mods.get(module["path"], {})
        fields = ("source_sha256", "installed_sha256", "parity")
        if any(old.get(k) != module.get(k) for k in fields):
            changed.append({"kind": "module", "id": module["path"]})
    for unit in UNITS:
        old = previous.get("units", {}).get(unit, {})
        now = current["units"].get(unit, {})
        if any(old.get(k) != now.get(k) for k in
               ("load", "active", "exec_binary", "exec_entry", "fragment", "drop_ins")):
            changed.append({"kind": "unit", "id": unit})
    for name, current_flag in current["flags"].items():
        if name not in {u + ":" + n for u, flags in FLAGS.items() for n in flags}:
            continue
        old_flag = previous.get("flags", {}).get(name, {})
        if any(old_flag.get(k) != current_flag.get(k) for k in
               ("source_default", "effective", "read_status", "origin")):
            changed.append({"kind": "flag", "id": name})
    incomplete = any(m["parity"] not in ("equal", "different") for m in current["modules"])
    incomplete |= any(f["read_status"] in ("not-running", "not-readable")
                      for f in current["flags"].values())
    unreconciled = [m["path"] for m in current["modules"] if m["parity"] == "different"]
    blockers = []
    if changed:
        blockers.append("changed-since-baseline")
    if incomplete:
        blockers.append("incomplete-runtime-evidence")
    if unreconciled:
        blockers.append("unreconciled-source-runtime-parity")
    if current.get("repo_dirty") is not False:
        blockers.append("unreproducible-git-worktree")
    return {"schema": "solinteg-release-diff-v1",
            "status": "different" if changed else ("incomplete" if incomplete else "equal"),
            "changes": changed,
            "runtime_complete": not incomplete,
            "manifest_gate": "blocked" if blockers else "pass",
            "block_reasons": blockers,
            "unreconciled_modules": unreconciled}


def read_test_summary(path: Path) -> dict:
    """Only counters from an optional local JSON result, never raw logs."""
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("test summary must be an object")
    result = {}
    for key in ("python", "vitest", "typecheck", "lint", "build", "docs"):
        if key not in obj:
            continue
        data = obj[key]
        if not isinstance(data, dict):
            raise ValueError("test group must be an object")
        allowed = ("passed", "failed", "skipped")
        counters = {k: v for k, v in data.items() if k in allowed and type(v) is int and 0 <= v <= 1000000}
        result[key] = counters
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--installed", type=Path, default=Path("/opt/solinteg/app"))
    parser.add_argument("--ev-runtime", type=Path, default=Path("/opt/solinteg/ev-shadow-runtime"))
    parser.add_argument("--rollback-ref", help="Opaque rollback evidence ID, never a filesystem path")
    parser.add_argument("--test-summary", type=Path, help="JSON with whitelisted test counters")
    parser.add_argument("--compare-json", type=Path,
                        help="Compare live read-only evidence to a prior JSON snapshot; fails closed")
    args = parser.parse_args()
    if args.rollback_ref and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", args.rollback_ref):
        parser.error("rollback reference must be an opaque alphanumeric ID")
    tests = read_test_summary(args.test_summary) if args.test_summary else None
    current = snapshot(args.repo, args.installed, args.ev_runtime,
                       rollback_ref=args.rollback_ref, tests=tests)
    if args.compare_json:
        previous = json.loads(args.compare_json.read_text(encoding="utf-8"))
        if not isinstance(previous, dict) or previous.get("schema") != current["schema"]:
            parser.error("incompatible release snapshot schema")
        result = compare_snapshots(previous, current)
        print(json.dumps(result, indent=2, sort_keys=True))
        if result["manifest_gate"] == "blocked":
            return 5
        return 0
    print(json.dumps(current, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
