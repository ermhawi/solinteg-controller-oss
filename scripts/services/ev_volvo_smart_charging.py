"""Fail-closed Volvo Smart Charging interlock from a read-only Chronos snapshot.

Input is the sanitized output of the existing, VIN-scoped
smart_schedule_read_only.py probe. No OEM calls or device writes here.
"""
from __future__ import annotations

import hmac
import json
import math
import re
from datetime import datetime
from pathlib import Path

MAX_AGE_SECONDS = 10 * 60
SOURCE_METHOD = "read_only_graphql_and_chronos"


BATTERY_SOURCE = "chronos_get_latest_battery_readonly"
BATTERY_QUALITY = "chronos_reported_sample_time_not_meter_calibration"
BATTERY_MAX_AGE_SECONDS = 300


def verified_battery_observation(data: object, *, now: datetime,
                                 expected_vin_sha256: str | None) -> dict | None:
    """Sanitize optional Chronos BMS report; never assert manual-stop intent.

    The reported BMS timestamp is independent of the Energy API's last-change
    timestamp, but not proof of physical energy metering or guaranteed delivery.
    """
    if not isinstance(data, dict) or now.tzinfo is None:
        return None
    try:
        if (data.get("source") != BATTERY_SOURCE
                or data.get("observation_quality") != BATTERY_QUALITY
                or type(data.get("device_writes")) is not int
                or data["device_writes"] != 0
                or not isinstance(expected_vin_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_vin_sha256)
                or not isinstance(data.get("target_vin_sha256"), str)
                or not hmac.compare_digest(data["target_vin_sha256"],
                                           expected_vin_sha256)):
            return None
        captured = datetime.fromisoformat(data["captured_utc"].replace("Z", "+00:00"))
        sampled = datetime.fromisoformat(
            data["battery_sampled_at_utc"].replace("Z", "+00:00"))
        if (captured.tzinfo is None or sampled.tzinfo is None
                or not 0 <= (now - captured).total_seconds() <= MAX_AGE_SECONDS
                or not 0 <= (now - sampled).total_seconds() <= BATTERY_MAX_AGE_SECONDS
                or not -3 <= (captured - sampled).total_seconds() <= BATTERY_MAX_AGE_SECONDS):
            return None
        soc = data["soc_pct"]
        if type(soc) not in (int, float) or not math.isfinite(soc) or not 0 <= soc <= 100:
            return None
        status = data["charging_status_v2"]
        if not isinstance(status, str) or status not in (
                "unspecified", "charging", "good_to_go", "charging_en_route",
                "charging_min_soc", "idle", "scheduled", "scheduled_cannot_complete",
                "scheduled_will_complete", "discharging", "discharging_v2h",
                "discharging_v2l", "smart_charging", "smart_charging_paused",
                "smart_charging_will_not_finish", "error", "done", "unknown"):
            return None
        numeric = ("charging_power_w", "charging_current_a",
                   "charging_voltage_v", "charger_power_status_code",
                   "connection_status_code")
        for key in numeric:
            val = data.get(key)
            if val is not None and (type(val) is not int or val < 0
                                    or val > (400000 if key == "charging_power_w" else 1000)):
                return None
        override = data.get("charge_now_override")
        if override is not None and (
                not isinstance(override, dict)
                or override.get("manual_stop_proven") is not False
                or override.get("action") not in (
                    "unspecified", "charge_now", "resume_schedule",
                    "not_applicable", "unknown")
                or override.get("override_state") not in (
                    "unspecified", "not_active", "active_pending",
                    "active", "not_active_pending", "unknown")):
            return None
        return {
            "source": BATTERY_SOURCE,
            "captured_utc": data["captured_utc"],
            "battery_sampled_at_utc": data["battery_sampled_at_utc"],
            "target_vin_sha256": data["target_vin_sha256"],
            "soc_pct": soc,
            "charging_status_v2": status,
            "charging_power_w": data.get("charging_power_w"),
            "charging_current_a": data.get("charging_current_a"),
            "charging_voltage_v": data.get("charging_voltage_v"),
            "charger_power_status_code": data.get("charger_power_status_code"),
            "connection_status_code": data.get("connection_status_code"),
            "charge_now_override": {
                "action": override["action"],
                "override_state": override["override_state"],
                "manual_stop_proven": False,
            } if override is not None else None,
            "observation_quality": BATTERY_QUALITY,
            "manual_stop_lossless": False,
            "metered_ev_energy_available": False,
            "device_writes": 0,
        }
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


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
