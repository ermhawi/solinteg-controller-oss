#!/usr/bin/env python3
"""Fail-closed isolation check for hosted CI. No sockets, systemd or private reads."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

EXPECTED = {
    "SOLINTEG_CONTROL_ARMED": "0",
    "SOLINTEG_HOST": "127.0.0.1",
    "EVCC_CONTROL_ARMED": "0",
    "EVCC_PRICE_CONTROL_ARMED": "0",
    "EVCC_MANUAL_TEST": "0",
    "EVCC_START_APPROVED": "0",
    "DISPATCH_FREEZE_HOLDS": "0",
    "DISPATCH_RESERVE_HOLD_VERIFIED": "0",
    "SOLINTEG_HOLD_ENABLED": "0",
}
DEFAULT_RUNTIME = (Path("/opt/solinteg/app"), Path("/opt/solinteg/ev-shadow-runtime"))


def check(env: dict[str, str], repo: Path,
          runtime: tuple[Path, ...] = DEFAULT_RUNTIME) -> list[str]:
    """Return public failure codes only. Never print configuration values."""
    errors = []
    for key, expected in EXPECTED.items():
        if env.get(key) != expected:
            errors.append("unsafe-or-missing:" + key)
    if env.get("GITHUB_ACTIONS") != "true":
        errors.append("not-github-actions")
    if (repo / ".env.local").exists() or (repo / ".env").exists():
        errors.append("private-repo-environment-present")
    if any(root.exists() for root in runtime):
        errors.append("private-runtime-mounted")
    return errors


def main() -> int:
    repo = Path(__file__).resolve().parents[2]
    errors = check(dict(os.environ), repo)
    print(json.dumps({"schema": "solinteg-ci-safety-v1",
                      "status": "blocked" if errors else "pass",
                      "errors": errors}, sort_keys=True))
    return 0 if not errors else 7


if __name__ == "__main__":
    sys.exit(main())
