#!/usr/bin/env python3
"""A01/A02 reproducible, read-only source/runtime entrypoint inventory.

Static nonreachability alone never authorizes deletion. Prints only sanitized
source module names, unit names, relative references, status and file hashes.
Never opens sockets or touches installed systemd/runtime state.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess


CATEGORIES = frozenset({"live", "manual", "recovery-reserve", "historical", "unknown"})
ROOT_RUNTIME = (Path("/opt/solinteg/app"), Path("/opt/solinteg/ev-shadow-runtime"))
SAFE_NAME = re.compile(r"^[a-zA-Z0-9_.@-]+$")
SAFETY_TERMS = (
    "write_register", "set_limit_raw", "force_charge", "force_discharge",
    "return_to_auto", "systemctl", "reboot", "shutdown", "transaction_hold",
)


def source_modules(repo: Path) -> dict[str, Path]:
    return {p.stem: p for p in sorted((repo / "scripts/services").glob("*.py"))}


def imports(path: Path, known: set[str]) -> tuple[list[str], list[str]]:
    """Direct imports and literal dynamic imports, never execute source code."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    dynamic: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and (isinstance(node.func, ast.Name)
                and node.func.id in ("__import__", "import_module") or isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"):
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                dynamic.add(node.args[0].value.split(".")[0])
            else:
                dynamic.add("<computed>")
    return sorted(names & known), sorted(dynamic)


def read_units(runner=subprocess.run) -> list[dict]:
    """Public systemd metadata only; never publish ExecStart argv or environment."""
    try:
        p = runner(["systemctl", "list-units", "--type=service", "--all",
                    "--no-legend", "--no-pager"], capture_output=True,
                   text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    units = []
    for name in sorted(set(re.findall(r"(?:solinteg|wifi)[A-Za-z0-9_.@-]+\.service",
                                    p.stdout if p.returncode == 0 else ""))):
        if not SAFE_NAME.fullmatch(name):
            continue
        try:
            r = runner(["systemctl", "show", name, "--no-pager",
                        "-p", "ExecStart", "-p", "ActiveState", "-p", "LoadState"],
                       capture_output=True, text=True, timeout=6)
        except (OSError, subprocess.SubprocessError):
            continue
        fields = dict(row.split("=", 1) for row in r.stdout.splitlines() if "=" in row)
        if r.returncode != 0 or fields.get("LoadState") != "loaded":
            continue
        entrypoint_names = sorted(set(Path(p).stem for p in re.findall(
            r"/[A-Za-z0-9_./-]+\.py\b", fields.get("ExecStart", "")
        )))
        units.append({
            "unit": name,
            "active": fields.get("ActiveState") if fields.get("ActiveState") in
                      ("active", "inactive", "failed", "activating", "deactivating") else "unknown",
            "entrypoints": entrypoint_names,
        })
    return units


def sha(path: Path, *, sudo_read: bool = False) -> dict:
    try:
        return {"status": "ok", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    except FileNotFoundError:
        return {"status": "missing"}
    except OSError:
        if not sudo_read:
            return {"status": "not-readable"}
    # Explicit opt-in: only the trusted sha256sum binary, only already allowlisted
    # source paths. Never print stderr, environment, or privileged file content.
    try:
        proc = subprocess.run(["sudo", "-n", "/usr/bin/sha256sum", str(path)],
                              capture_output=True, text=True, timeout=5)
        words = proc.stdout.split()
        if proc.returncode == 0 and words and re.fullmatch(r"[a-f0-9]{64}", words[0]):
            return {"status": "ok", "sha256": words[0]}
        if proc.returncode != 0 and "No such file or directory" in proc.stderr:
            return {"status": "missing"}
    except (OSError, subprocess.SubprocessError):
        pass
    return {"status": "not-readable"}


def purpose(name: str, path: Path) -> tuple[str, str]:
    """A vetted docstring headline, with an explicit conservative fallback."""
    doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
    headline = doc.splitlines()[0].strip() if doc else ""
    if (re.fullmatch(r"[A-Za-z0-9 ,._():/-]{4,140}", headline)
            and not re.search(r"(?:https?://|/opt/|/etc/|[0-9]{1,3}\\.[0-9]{1,3}\\.)", headline)):
        return headline, "source-docstring"
    return name.replace("_", " "), "module-name-fallback"


def ownership(name: str, reachable: bool, unit_ref: bool) -> tuple[str, str]:
    if name in ("common", "notify"):
        domain = "operations"
    elif name.startswith(("battery_save", "energy_priority", "l3_", "native_soc")):
        domain = "inverter/broker"
    elif name.startswith(("ev", "ha_ev", "smart_")):
        domain = "EVCC/vehicle"
    elif name.startswith(("modbus", "inverter", "dispatch", "control", "safety")):
        domain = "inverter/broker"
    elif name.startswith(("soc25", "temporary")):
        domain = "SoC/recovery"
    elif name.startswith(("weather", "solar")):
        domain = "forecast/weather"
    elif name.startswith(("health", "watchdog", "backup", "boot")):
        domain = "operations"
    else:
        domain = "unassigned"
    if reachable or unit_ref:
        category = "live"
    elif name.startswith(("soc25", "temporary", "modbus_broker_emergency",
                         "modbus_broker_fencing", "modbus_broker_root",
                         "modbus_broker_supervisor", "modbus_broker_host")):
        category = "recovery-reserve"
    elif "shadow" in name or name.startswith("boot_"):
        category = "historical"
    elif any(x in name for x in ("audit", "manual", "service", "ledger", "publish")):
        category = "manual"
    else:
        category = "unknown"
    return domain, category


def inventory(repo: Path, units: list[dict], *,
              runtimes: tuple[Path, ...] = ROOT_RUNTIME,
              sudo_read: bool = False) -> dict:
    modules = source_modules(repo)
    known = set(modules)
    direct = {}
    dynamic = {}
    for name, path in modules.items():
        direct[name], dynamic[name] = imports(path, known)
    loaded_units = {
        module: [] for module in modules
    }
    roots: set[str] = set()
    for unit in units:
        for module in unit["entrypoints"]:
            if module in loaded_units:
                roots.add(module)
                loaded_units[module].append({
                    "name": unit["unit"], "active": unit["active"],
                })
    seen: set[str] = set()
    todo = sorted(roots)
    while todo:
        name = todo.pop(0)
        if name in seen:
            continue
        seen.add(name)
        todo.extend(sorted(set(direct[name]) - seen))

    tests = {p.name: p.read_text(encoding="utf-8")
             for p in sorted((repo / "scripts/tests").glob("test_*.py"))}
    tools = {p.name: p.read_text(encoding="utf-8")
             for p in sorted((repo / "scripts/tools").glob("*"))
             if p.suffix in (".py", ".sh")}
    deploy = {p.name: p.read_text(encoding="utf-8")
              for p in sorted((repo / "deploy").rglob("*"))
              if p.is_file() and p.suffix in (".service", ".timer", ".sh")}
    try:
        tracked = set(subprocess.run(["git", "ls-files", "--", "scripts/services"],
                                    cwd=repo, capture_output=True, text=True,
                                    check=True, timeout=8).stdout.splitlines())
    except (OSError, subprocess.SubprocessError):
        tracked = set()

    rows = []
    for name, path in sorted(modules.items()):
        identifier = path.name
        rel = "scripts/services/" + identifier
        refs = {
            "tests": sorted(n for n, txt in tests.items() if identifier in txt or
                            re.search(r"\b" + re.escape(name) + r"\b", txt)),
            "tools": sorted(n for n, txt in tools.items() if identifier in txt or
                            re.search(r"\b" + re.escape(name) + r"\b", txt)),
            "deploy": sorted(n for n, txt in deploy.items() if identifier in txt),
        }
        imported_by = sorted(n for n, imported in direct.items() if name in imported)
        dynamic_references = sorted(n for n, imported in dynamic.items() if name in imported)
        is_root = bool(loaded_units[name])
        owner, category = ownership(name, name in seen, is_root)
        module_purpose, purpose_origin = purpose(name, path)
        data = path.read_text(encoding="utf-8")
        parity = {
            "app": sha(runtimes[0] / rel, sudo_read=sudo_read),
            "ev": sha(runtimes[1] / rel, sudo_read=sudo_read),
        }
        rows.append({
            "path": rel, "owner_domain": owner,
            "purpose": module_purpose,
            "purpose_origin": purpose_origin,
            "category": category,
            "reachable_from_loaded_units": name in seen,
            "systemd_units": loaded_units[name],
            "direct_imports": direct[name], "imported_by": imported_by,
            "literal_dynamic_imports": dynamic[name],
            "dynamic_imported_by": dynamic_references,
            "manual_or_test_references": refs,
            "last_execution": "not_verified",
            "possible_physical_effect": ("possible" if any(x in data for x in SAFETY_TERMS)
                                         else "not_established"),
            "source_git_backup": "tracked" if rel in tracked else "not_tracked",
            "runtime": parity,
            "deletion_proposed": False,
        })
    return {
        "schema": "solinteg-python-reachability-v1",
        "source_module_count": len(rows),
        "loaded_systemd_service_count": len(units),
        "reachable_count": sum(x["reachable_from_loaded_units"] for x in rows),
        "candidate_count": sum(not x["reachable_from_loaded_units"] for x in rows),
        "category_counts": {category: sum(x["category"] == category for x in rows)
                            for category in sorted(CATEGORIES)},
        "deletion_candidates": [],
        "notes": [
            "Source-only import reachability is not proof of unused runtime code",
            "Loaded inactive oneshots can be scheduled by timers or run manually",
            "All dynamic/indirect, shell and recovery references require independent checking",
            "No filesystem content, environment values, hostnames or raw argv are published",
        ],
        "modules": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path,
                        default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path,
                        help="Write sanitized JSON evidence to this NEW source file")
    parser.add_argument("--source-only", action="store_true",
                        help="skip installed systemd inspection (CI/static checks)")
    parser.add_argument("--sudo-read", action="store_true",
                        help="read protected runtime source hashes using sudo -n sha256sum only")
    args = parser.parse_args()
    result = inventory(args.repo, [] if args.source_only else read_units(),
                       sudo_read=args.sudo_read)
    if args.output:
        if args.output.exists():
            parser.error("refusing to overwrite an existing inventory")
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                               encoding="utf-8")
    else:
        # Only compact counts on stdout, no private systemd metadata.
        print(json.dumps({k: v for k, v in result.items() if k != "modules"},
                         sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
