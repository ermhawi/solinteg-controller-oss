"""Fail-closed Volvo Smart Charging interlock from a read-only Chronos snapshot.

Input is the sanitized output of the existing, VIN-scoped
smart_schedule_read_only.py probe. No OEM calls or device writes here.
"""
from __future__ import annotations

import hmac
import json
import re
from datetime import datetime
from pathlib import Path

MAX_AGE_SECONDS = 10 * 60
SOURCE_METHOD = "read_only_graphql_and_chronos"


def read_smart_charging(path: str | None, *, now: datetime,
                        expected_vin_sha256: str | None) -> tuple[str, str]:
    """Return (off|on|unknown, reason); only explicit fresh off is safe."""
    def unknown(reason: str) -> tuple[str, str]:
        return "unknown", reason

    if not path or now.tzinfo is None:
        return unknown("Volvo Smart Charging snapshot unavailable")
    try:
        with Path(path).open(encoding="utf-8") as source:
            raw = source.read(65537)
        if len(raw) > 65536:
            return unknown("Volvo Smart Charging snapshot too large")
        data = json.loads(raw)
        if (not isinstance(data, dict) or data.get("method") != SOURCE_METHOD
                or type(data.get("set_amp_limit_calls")) is not int
                or data["set_amp_limit_calls"] != 0):
            return unknown("unverified Volvo Smart Charging source")
        observed_vin = data.get("target_vin_sha256")
        if (not isinstance(expected_vin_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_vin_sha256)
                or not isinstance(observed_vin, str)
                or not hmac.compare_digest(observed_vin, expected_vin_sha256)):
            return unknown("Volvo vehicle identity missing or mismatched")
        stamp = datetime.fromisoformat(data["captured_utc"].replace("Z", "+00:00"))
        if stamp.tzinfo is None or not 0 <= (now - stamp).total_seconds() <= MAX_AGE_SECONDS:
            return unknown("stale Volvo Smart Charging snapshot")
        chronos = data["chronos"]
        locations = chronos["locations"]
        records = locations["locations"]
        if (not isinstance(records, list) or len(records) != 1
                or type(locations["pending_locations"]) is not int
                or locations["pending_locations"] != 0):
            return unknown("ambiguous or pending Volvo charge location")
        location = records[0]
        enabled = location["optimized_charging_enabled"]
        if type(enabled) is not bool:
            return unknown("Volvo Smart Charging on/off not reported")
        if enabled:
            return "on", "Volvo Smart Charging enabled"
        for key in ("timers", "departure_times"):
            entries = location[key]
            if (not isinstance(entries, list)
                    or any(not isinstance(item, dict)
                           or type(item.get("active")) is not bool for item in entries)):
                return unknown("unverified Volvo local charging schedule")
            if any(item["active"] for item in entries):
                return unknown("Volvo local charging schedule still active")
        timer = chronos["timer"]
        if not isinstance(timer, dict) or timer["pending_global_timer"] is not None:
            return unknown("unverified or pending Volvo charge timer")
        global_timer = timer["global_timer"]
        if global_timer is not None:
            if not isinstance(global_timer, dict) or type(global_timer.get("active")) is not bool:
                return unknown("unknown Volvo charge timer state")
            if global_timer["active"]:
                return unknown("Volvo charge timer still active")
        if (type(chronos.get("current_amp_limit")) is not int
                or not 6 <= chronos["current_amp_limit"] <= 32
                or "pending_amp_limit" not in chronos):
            return unknown("Volvo charging current state not verified")
        if chronos["pending_amp_limit"] is not None:
            return unknown("pending Volvo charging current change")
        return "off", "Volvo Smart Charging explicitly disabled"
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return unknown("missing or invalid Volvo Smart Charging snapshot")
