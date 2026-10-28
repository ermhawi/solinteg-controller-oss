"""Read-only HA/Volvo to Solinteg EV-session bridge.

No vehicle, charger, Home Assistant or inverter commands. This process performs
GETs against an explicitly configured HA REST URL. It writes only two local
JSON snapshots: a status for the UI and a fail-closed planning session.

Use the original Volvo Energy State field timestamp for battery level,
connection and target. A fresh Home Assistant coordinator callback or HTTP
request does not refresh an old OEM measurement. Legacy HA sensor
last_reported remains a diagnostic but is NOT accepted for this bridge.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, time as dt_time, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import common

UTC = timezone.utc
STOCKHOLM = ZoneInfo("Europe/Stockholm")
BATTERY = "sensor.volvo_xc40_battery"
CONNECTED = "sensor.volvo_xc40_charging_connection_status"
TARGET = "sensor.volvo_xc40_target_battery_charge_level"
OEM_SNAPSHOT = "sensor.volvo_charge_priority_volvo_energy_snapshot"
MAX_SENSOR_AGE_SECONDS = 20 * 60
MAX_COORDINATOR_AGE_SECONDS = 5 * 60
REST_TIMEOUT_SECONDS = 6
TARGET_SOC = 80
DEFAULT_SETTINGS = {
    "charge_mode": "deadline",
    "departure_local_time": "07:00",
    "max_buy_ore_per_kwh": None,
}
log = logging.getLogger("solinteg.ha_ev_session")


class UnsafeSession(ValueError):
    """Missing or untrustworthy source prevents generating a valid EV session."""


def _number(value: object) -> float:
    if isinstance(value, bool):
        raise UnsafeSession("boolean sensor value")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise UnsafeSession("sensor has no numeric value") from exc
    if not math.isfinite(parsed):
        raise UnsafeSession("non-finite sensor value")
    return parsed


def _utc(value: object) -> datetime:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise UnsafeSession("missing or invalid sensor timestamp") from exc
    if stamp.tzinfo is None:
        raise UnsafeSession("sensor timestamp missing timezone")
    return stamp.astimezone(UTC)


def _sensor_state(response: dict, *, name: str, now: datetime) -> tuple[str, datetime]:
    if not isinstance(response, dict) or response.get("entity_id") != name:
        raise UnsafeSession(f"{name}: missing or wrong entity")
    value = response.get("state")
    if not isinstance(value, str) or not value.strip() or value.lower() in (
        "unknown", "unavailable", "none", "null"
    ):
        raise UnsafeSession(f"{name}: unavailable")
    # Called with an internal source snapshot mapped into this shape; its
    # last_reported field here is the OEM timestamp, NOT HA's own state time.
    stamp = _utc(response.get("last_reported"))
    age = (now - stamp).total_seconds()
    if not -5 <= age <= MAX_SENSOR_AGE_SECONDS:
        raise UnsafeSession(f"{name}: stale")
    return value, stamp


def _oem_snapshot_states(snapshot: dict, *, now: datetime) -> dict[str, dict]:
    """Validate an atomic OEM energy response published by our HA component.

    All values and their individual upstream timestamps come from one Volvo
    medium-coordinator result. A fresh HA GET, another Volvo sensor or a new
    coordinator callback cannot reset the age of a cached OEM measurement.
    """
    if now.tzinfo is None:
        raise UnsafeSession("current time lacks timezone")
    if not isinstance(snapshot, dict) or snapshot.get("entity_id") != OEM_SNAPSHOT:
        raise UnsafeSession("Volvo OEM snapshot missing")
    attrs = snapshot.get("attributes")
    if not isinstance(attrs, dict):
        raise UnsafeSession("Volvo OEM snapshot attributes missing")
    if (snapshot.get("state") != "ready" or
            type(attrs.get("schema_version")) is not int or
            attrs["schema_version"] != 1 or
            attrs.get("timestamp_origin") != "volvo_energy_state_field" or
            attrs.get("oem_timestamps_complete") is not True or
            type(attrs.get("physical_writes")) is not int or
            attrs["physical_writes"] != 0):
        # OEM source may omit timestamps, or have outdated values. The
        # diagnostic sensor reports details, but only "ready" is usable.
        raise UnsafeSession("Volvo OEM snapshot unavailable: " +
                            str(attrs.get("reason", "invalid_source"))[:180])
    received = _utc(attrs.get("api_received_at_utc"))
    age = (now - received).total_seconds()
    if not -5 <= age <= MAX_COORDINATOR_AGE_SECONDS:
        raise UnsafeSession("Volvo OEM energy API response stale")
    mapping = {
        BATTERY: ("battery_soc_pct", "battery_oem_at_utc"),
        CONNECTED: ("connection", "connection_oem_at_utc"),
        TARGET: ("target_soc_pct", "target_oem_at_utc"),
    }
    states: dict[str, dict] = {}
    for entity, (value_key, timestamp_key) in mapping.items():
        if value_key not in attrs or timestamp_key not in attrs:
            raise UnsafeSession("Volvo OEM snapshot incomplete")
        stamp = _utc(attrs[timestamp_key])
        if stamp > received + timedelta(seconds=5):
            raise UnsafeSession("Volvo OEM timestamp after its API response")
        # Internal conversion only: last_reported carries the OEM field
        # timestamp, NOT the Home Assistant state last_reported.
        states[entity] = {
            "entity_id": entity,
            "state": str(attrs[value_key]),
            "last_reported": stamp.isoformat(),
        }
    return states


def read_shadow_observation(snapshot: dict, *, now: datetime) -> dict:
    """One confirmed API observation; never a verified BMS sample.

    A fresh API response can reuse a last-changed value for days. We allow
    SHADOW use with explicit quality flags, but the source is FOREVER
    advisory-only: it cannot be promoted into EV_SESSION_PATH or a device
    control signal by the caller.
    """
    if not isinstance(snapshot, dict) or snapshot.get("entity_id") != OEM_SNAPSHOT:
        raise UnsafeSession("Volvo shadow OEM snapshot missing")
    attrs = snapshot.get("attributes")
    if not isinstance(attrs, dict):
        raise UnsafeSession("Volvo shadow OEM attributes missing")
    if (snapshot.get("state") not in ("observed", "ready")
            or type(attrs.get("schema_version")) is not int
            or attrs["schema_version"] != 2
            or attrs.get("timestamp_origin") != "volvo_energy_state_field"
            or attrs.get("oem_timestamp_role") != "last_changed_not_sampled"
            or attrs.get("api_fetch_confirmed") is not True
            or attrs.get("advisory_only") is not True
            or attrs.get("physical_control_eligible") is not False
            or type(attrs.get("physical_writes")) is not int
            or attrs["physical_writes"] != 0):
        raise UnsafeSession("Volvo energy API observation unavailable: " +
                            str(attrs.get("reason", "invalid_source"))[:180])
    api_at = _utc(attrs.get("api_received_at_utc"))
    api_age = (now - api_at).total_seconds()
    if not -5 <= api_age <= MAX_COORDINATOR_AGE_SECONDS:
        raise UnsafeSession("Volvo energy API observation stale")
    soc = _number(attrs.get("battery_soc_pct"))
    target = _number(attrs.get("target_soc_pct"))
    connection = attrs.get("connection")
    if not 0 <= soc <= 100 or target != TARGET_SOC:
        raise UnsafeSession("invalid Volvo SoC or local 80-percent target")
    if connection not in ("connected", "charging", "disconnected"):
        raise UnsafeSession("invalid Volvo cable observation")

    changed: dict[str, str | None] = {}
    for key in ("battery_oem_at_utc", "connection_oem_at_utc", "target_oem_at_utc"):
        raw = attrs.get(key)
        if raw is None:
            changed[key] = None  # Explicitly unknown, NEVER last fetched.
            continue
        ts = _utc(raw)
        if ts > api_at + timedelta(seconds=5):
            raise UnsafeSession("OEM change timestamp after energy API fetch")
        changed[key] = ts.isoformat()
    # The high-confidence source tier ("recent changed value") is still not
    # a physical measurement. It merely informs the UI how long since a
    # field last changed.
    return {
        "vehicle_soc_pct": soc,
        "target_soc_pct": target,
        "connected": connection in ("connected", "charging"),
        "connection_state": connection,
        "energy_api_received_at_utc": api_at.isoformat(),
        "api_fetch_confirmed": True,
        "oem_timestamps_complete": attrs.get("oem_timestamps_complete") is True,
        "oem_timestamps_recent": attrs.get("oem_timestamps_recent") is True,
        "oem_timestamp_role": "last_changed_not_sampled",
        "timestamp_origin": "volvo_energy_state_field",
        "observation_quality": (
            "api_recent_fields_changed_recently"
            if snapshot["state"] == "ready"
            else "api_recent_field_change_unverified"
        ),
        "advisory_only": True,
        "physical_control_eligible": False,
        **changed,
    }


def shadow_session_from_observation(*, observation: dict, settings: dict,
                                    now: datetime, capacity: object,
                                    ac_efficiency: object) -> dict:
    """Price-only scenario; assumptions must be intentionally enabled.

    This JSON is categorically NOT a verified charging session and must
    never be consumed by the EVCC actuator or future physical controller.
    """
    if os.environ.get("EV_SHADOW_ASSUMPTIONS_ENABLED") != "1":
        raise UnsafeSession("shadow energy assumptions not explicitly enabled")
    if capacity is None or ac_efficiency is None:
        raise UnsafeSession("shadow energy assumptions missing")
    usable = _number(capacity)
    efficiency = _number(ac_efficiency)
    if not 45 <= usable <= 100 or not 0.7 <= efficiency <= 1:
        raise UnsafeSession("invalid shadow energy assumptions")
    soc = observation["vehicle_soc_pct"]
    remaining = round(max(0, TARGET_SOC - soc) / 100 * usable / efficiency, 3)
    session = {
        "source": "home_assistant_shadow",
        "shadow_only": True,
        "physical_control_eligible": False,
        "api_fetch_confirmed": True,
        "observation_quality": observation["observation_quality"],
        # This time means an API RESPONSE, not an OEM/BMS sample.
        "observed_at_utc": observation["energy_api_received_at_utc"],
        "observation_time_semantics": "api_fetch_not_bms_sample",
        "connected": observation["connected"],
        "charge_mode": settings["charge_mode"],
        "target_soc_pct": TARGET_SOC,
        "vehicle_soc_pct": soc,
        "remaining_ac_kwh": remaining,
        "energy_estimate_unverified": True,
        "assumed_usable_capacity_kwh": usable,
        "assumed_ac_efficiency": efficiency,
        "max_buy_ore_per_kwh": settings.get("max_buy_ore_per_kwh"),
        "battery_oem_at_utc": observation["battery_oem_at_utc"],
        "oem_timestamp_role": "last_changed_not_sampled",
    }
    if settings["charge_mode"] == "deadline":
        session["departure_at_utc"] = next_departure(
            settings["departure_local_time"], now=now).isoformat()
    return session


def load_settings(path: str) -> dict:
    """Fail-closed on malformed user intent; never guess a price ceiling."""
    raw = DEFAULT_SETTINGS.copy()
    try:
        with open(path, encoding="utf-8") as source:
            parsed = json.load(source)
        if not isinstance(parsed, dict):
            raise UnsafeSession("EV settings must be an object")
        if set(parsed) - set(DEFAULT_SETTINGS):
            raise UnsafeSession("unknown EV settings keys")
        raw.update(parsed)
    except FileNotFoundError:
        pass  # declared defaults, not an error
    except UnsafeSession:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise UnsafeSession("invalid EV settings file") from exc
    if raw["charge_mode"] not in ("deadline", "economy"):
        raise UnsafeSession("unknown charging mode")
    if not isinstance(raw["departure_local_time"], str) or not re.fullmatch(
        r"(?:[01]\d|2[0-3]):[0-5]\d", raw["departure_local_time"]
    ):
        raise UnsafeSession("invalid departure time")
    limit = raw.get("max_buy_ore_per_kwh")
    if limit is not None:
        limit = _number(limit)
        if not -200 <= limit <= 3000:
            raise UnsafeSession("invalid maximum price")
    return {"charge_mode": raw["charge_mode"],
            "departure_local_time": raw["departure_local_time"],
            "max_buy_ore_per_kwh": limit}


def next_departure(local_time: str, *, now: datetime) -> datetime:
    """Use absolute UTC departure; never manufacture an ambiguous DST time."""
    if now.tzinfo is None:
        raise UnsafeSession("naive current time")
    hour, minute = (int(part) for part in local_time.split(":"))
    here = now.astimezone(STOCKHOLM)
    for offset in (0, 1, 2):
        day = here.date() + timedelta(days=offset)
        naive = datetime.combine(day, dt_time(hour, minute))
        candidate0 = naive.replace(tzinfo=STOCKHOLM, fold=0)
        candidate1 = naive.replace(tzinfo=STOCKHOLM, fold=1)
        if candidate0.astimezone(UTC).astimezone(STOCKHOLM).replace(tzinfo=None) != naive:
            raise UnsafeSession("nonexistent departure time on DST change")
        if candidate0.utcoffset() != candidate1.utcoffset():
            raise UnsafeSession("ambiguous departure time on DST change")
        utc_value = candidate0.astimezone(UTC)
        if utc_value > now:
            if utc_value - now > timedelta(hours=48):
                raise UnsafeSession("departure outside planning horizon")
            return utc_value
    raise UnsafeSession("no departure within 48 hours")


def make_session(*, states: dict[str, dict], settings: dict, now: datetime,
                 usable_capacity_kwh: float | None,
                 ac_efficiency: float | None) -> tuple[dict, dict]:
    """Return validated pricing session and display-only status.

    usable_capacity_kwh and AC charge efficiency require independent verified
    configuration. The HA battery_capacity sensor currently exposes 81.608 kWh
    but is not independently proven to be usable capacity; never silently use
    it for operational AC energy requirements.
    """
    soc_raw, soc_at = _sensor_state(states[BATTERY], name=BATTERY, now=now)
    connection_raw, connection_at = _sensor_state(
        states[CONNECTED], name=CONNECTED, now=now)
    target_raw, target_at = _sensor_state(states[TARGET], name=TARGET, now=now)
    soc = _number(soc_raw)
    target = _number(target_raw)
    if not 0 <= soc <= 100:
        raise UnsafeSession("Volvo SoC outside 0-100")
    if target != TARGET_SOC:
        raise UnsafeSession("Volvo local charge target is not 80%")
    connected = connection_raw.lower() in ("connected", "charging")
    if not connected:
        raise UnsafeSession("Volvo charging cable disconnected")
    if usable_capacity_kwh is None or ac_efficiency is None:
        raise UnsafeSession("usable battery capacity/AC efficiency not calibrated")
    capacity = _number(usable_capacity_kwh)
    efficiency = _number(ac_efficiency)
    if not 45 <= capacity <= 100 or not 0.7 <= efficiency <= 1:
        raise UnsafeSession("invalid calibrated battery capacity/efficiency")

    remaining = round(max(0, target - soc) / 100 * capacity / efficiency, 3)
    # A record remains as old as its oldest required source. Do NOT use the
    # timestamp of our own polling invocation to refresh old Volvo values.
    source_at = min(soc_at, connection_at, target_at)
    session = {
        "source": "home_assistant",
        "connected": True,
        "observed_at_utc": source_at.isoformat(),
        "charge_mode": settings["charge_mode"],
        "target_soc_pct": TARGET_SOC,
        "vehicle_soc_pct": soc,
        "remaining_ac_kwh": remaining,
    }
    if settings["charge_mode"] == "deadline":
        session["departure_at_utc"] = next_departure(settings["departure_local_time"], now=now).isoformat()
    if settings["max_buy_ore_per_kwh"] is not None:
        session["max_buy_ore_per_kwh"] = settings["max_buy_ore_per_kwh"]
    status = {
        "battery_soc_pct": soc,
        "battery_reported_at_utc": soc_at.isoformat(),
        "connection_reported_at_utc": connection_at.isoformat(),
        "target_reported_at_utc": target_at.isoformat(),
        "connected": connected,
        "remaining_ac_kwh": remaining,
        "usable_capacity_kwh_calibrated": capacity,
        "ac_efficiency_calibrated": efficiency,
    }
    return session, status


def _ha_state(base: str, token: str, name: str) -> dict:
    if not base or not token:
        raise UnsafeSession("HA URL or token missing")
    parts = urlsplit(base)
    if (parts.scheme not in ("https", "http") or not parts.hostname
            or parts.username or parts.password or parts.path not in ("", "/")
            or parts.query or parts.fragment):
        raise UnsafeSession("invalid HA API URL")
    # Plain HTTP must never send a HA token outside a private cluster/LAN.
    # Use Python's RFC1918 test, not a dangerous prefix such as 172.*.
    if parts.scheme == "http":
        try:
            address = ipaddress.ip_address(parts.hostname)
            allowed = address.version == 4 and any(
                address in ipaddress.ip_network(net)
                for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
            )
        except ValueError:
            allowed = False
        if not allowed:
            raise UnsafeSession("plain HTTP requires a private IP address")
    req = urllib.request.Request(
        base.rstrip("/") + "/api/states/" + name,
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/json"},
        method="GET",
    )
    # Refuse redirects: never forward a sensitive HA Authorization header to
    # another host, even if HA is accidentally configured with a redirect.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            raise UnsafeSession("HA API redirect refused")

    # Direct private cluster connection, never a system-configured HTTP proxy
    # that could receive the HA bearer token.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=REST_TIMEOUT_SECONDS) as response:
        if response.status != 200:
            raise UnsafeSession("HA response not OK")
        data = response.read(16385)
        if len(data) > 16384:
            raise UnsafeSession("HA state payload too large")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise UnsafeSession("HA state payload invalid")
    return result


def _read_token() -> str:
    path = os.environ.get("HA_READONLY_TOKEN_FILE")
    if path:
        with open(path, encoding="utf-8") as source:
            return source.read().strip()
    return os.environ.get("HA_READONLY_TOKEN", "").strip()


def once(*, now: datetime | None = None) -> dict:
    """One HA GET, three local read-only records. Zero physical writes.

    The strict session remains INVALID until an independent physical sample
    trust model and EVCC/L3 safeguards have been built and verified. A
    separate scenario session may drive pure PRICE/SOLAR simulations ONLY.
    """
    now = now or datetime.now(UTC)
    session_path = os.environ.get("EV_SESSION_PATH", "/opt/solinteg/volvo-energy-session.json")
    shadow_path = os.environ.get("EV_SHADOW_SESSION_PATH",
                                 "/opt/solinteg/ev-shadow-session.json")
    status_path = os.environ.get("EV_STATUS_PATH", "/opt/solinteg/ev-session-status.json")
    settings_path = os.environ.get("EV_SETTINGS_PATH", "/opt/solinteg/ev-settings.json")
    status: dict = {
        "checked_at_utc": now.isoformat(),
        "source": "home_assistant",
        "mode": "shadow",
        "valid_for_planning": False,
        "shadow_observation_usable": False,
        "shadow_estimate_ready": False,
        "advisory_only": True,
        "physical_control_eligible": False,
        "writes_to_devices": 0,
    }
    # Never let an API fetch be promoted to a source-verified control session.
    session: dict = {
        "source": "home_assistant",
        "connected": False,
        "physical_control_eligible": False,
        "invalid_reason": "OEM last-change timestamp is not a verified BMS sample",
    }
    shadow: dict = {
        "source": "home_assistant_shadow",
        "connected": False,
        "shadow_only": True,
        "physical_control_eligible": False,
        "invalid_reason": "not validated",
    }
    try:
        settings = load_settings(settings_path)
        status.update(settings)
        token = _read_token()
        base = os.environ.get("HA_API_URL", "")
        # Exactly ONE HA GET: atomic, source-labeled API observation.
        # Neither HA's per-sensor last_reported nor the wall-clock time of
        # this GET is treated as a Volvo/BMS sample timestamp.
        observed = read_shadow_observation(
            _ha_state(base, token, OEM_SNAPSHOT), now=now)
        status.update({
            "battery_soc_pct": observed["vehicle_soc_pct"],
            "battery_reported_at_utc": observed["battery_oem_at_utc"],
            "connection_reported_at_utc": observed["connection_oem_at_utc"],
            "target_reported_at_utc": observed["target_oem_at_utc"],
            "target_soc_pct": observed["target_soc_pct"],
            "connected": observed["connected"],
            "timestamp_origin": observed["timestamp_origin"],
            "oem_timestamp_role": observed["oem_timestamp_role"],
            "observation_quality": observed["observation_quality"],
            "oem_timestamps_recent": observed["oem_timestamps_recent"],
            "energy_api_received_at_utc": observed["energy_api_received_at_utc"],
            "remaining_soc_pct": round(
                max(0, TARGET_SOC - observed["vehicle_soc_pct"]), 1),
            "shadow_observation_usable": True,
            "reason": "API-observation, not a verified physical BMS measurement",
        })
        try:
            shadow = shadow_session_from_observation(
                observation=observed, settings=settings, now=now,
                capacity=os.environ.get("EV_SHADOW_ASSUMED_USABLE_KWH"),
                ac_efficiency=os.environ.get("EV_SHADOW_ASSUMED_AC_EFFICIENCY"),
            )
            status["shadow_estimate_ready"] = observed["connected"]
            status["remaining_ac_kwh"] = shadow["remaining_ac_kwh"]
            status["energy_estimate_unverified"] = True
            status["assumed_usable_capacity_kwh"] = shadow["assumed_usable_capacity_kwh"]
            status["assumed_ac_efficiency"] = shadow["assumed_ac_efficiency"]
        except (UnsafeSession, KeyError, TypeError) as exc:
            status["shadow_reason"] = str(exc) if isinstance(exc, UnsafeSession) else type(exc).__name__
            shadow["invalid_reason"] = status["shadow_reason"]
    except (UnsafeSession, KeyError, OSError, TypeError, ValueError,
            urllib.error.URLError, json.JSONDecodeError) as exc:
        status["reason"] = str(exc) if isinstance(exc, UnsafeSession) else type(exc).__name__
        shadow["invalid_reason"] = status["reason"]
        log.warning("Volvo shadow observation unavailable: %s", status["reason"])
    # Replace invalid/failed records immediately so an old success cannot
    # remain a valid session after a later read failure.
    common.write_json_atomic(session_path, session, indent=2)
    common.write_json_atomic(shadow_path, shadow, indent=2)
    common.write_json_atomic(status_path, status, indent=2)
    return status


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true", help="periodic read-only HA snapshots")
    args = parser.parse_args()
    interval = max(30, int(os.environ.get("EV_HA_POLL_SECONDS", "60")))
    while True:
        status = once()
        print(json.dumps(status, ensure_ascii=False), flush=True)
        if not args.watch:
            return
        time.sleep(interval)


if __name__ == "__main__":
    main()
