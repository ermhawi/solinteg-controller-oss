"""Local-only EVCC Ict2 shadow collection, independent of the retired EV gate.

Read existing snapshots and dispatch history only; never import a hardware
driver, open network connections, modify device control or change the
legacy observer's fail-closed gate.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from ev_l3_metered_policy import assess


def collect(live_path: Path, evcc_path: Path, controls_path: Path, *,
            now: datetime, last_applied) -> dict:
    """Keep read/parse failures isolated from the existing shadow observer."""
    try:
        live = json.loads(live_path.read_text(encoding="utf-8"))
        evcc = json.loads(evcc_path.read_text(encoding="utf-8"))
        if not isinstance(live, dict) or not isinstance(evcc, dict):
            raise ValueError("invalid observation snapshot")
    except (OSError, UnicodeError, ValueError, TypeError):
        return assess("unknown", 0, {}, {}, now=now).as_dict()

    try:
        action, power, _details = last_applied(controls_path, now=now)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        action, power = "unknown", 0

    return assess(action, power, live, evcc, now=now).as_dict()
