"""Operator-accepted empirical EV charging need, never independently calibrated.

Uses HA's single Volvo Energy API-derived status plus the live EVCC cable
tracker. This explicitly estimates kWh from Ict2/230 V/PF1 versus cloud SoC;
it does NOT claim verified BMS samples or an independent AC energy meter.
Only the dedicated, explicitly opted-in EV price executor may consume it.
"""
from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, NAMESPACE_URL, uuid5

from ha_ev_session import load_settings, next_departure
from evcc_cable_tracker import read_fresh_connected, CableObservationError
from evcc_ascii import observed_evcc_mode
import ev_price_pause_owner as ownership

KWH_PER_SOC_PCT = 0.8902
QUALITY = "operator_accepted_evcc_ict2_230v_pf1_cloud_soc_v1"
MAX_STATUS_AGE_S = 5 * 60
MAX_SNAPSHOT_AGE_S = 5 * 60
MAX_SOC_LAST_CHANGE_AGE_S = 12 * 3600
MAX_FILE_BYTES = 16 * 1024


class EmpiricalSessionError(ValueError):
    """Invalid/unverifiable empirical EV charging intent; no FC10 allowed."""


def _instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise EmpiricalSessionError("missing observation timestamp")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EmpiricalSessionError("invalid observation timestamp") from exc
    if dt.tzinfo is None:
        raise EmpiricalSessionError("naive observation timestamp")
    return dt.astimezone(timezone.utc)


def _fresh(value: object, *, now: datetime, limit_s: int) -> bool:
    age = (now - _instant(value)).total_seconds()
    return 0 <= age <= limit_s


def _json(path: Path) -> dict:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES
            or info.st_mode & 0o002):
        raise EmpiricalSessionError("untrusted source file")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise EmpiricalSessionError("source is not an object")
    return data


def _ledger_read(path: Path) -> dict | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
            or info.st_size > MAX_FILE_BYTES or info.st_uid != os.geteuid()):
        raise EmpiricalSessionError("unsafe persisted empirical session")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("schema_version") != 1
            or data.get("source") != QUALITY
            or data.get("session_id") is None
            or data.get("connection_epoch") is None
            or data.get("cable_tracker_id") is None):
        raise EmpiricalSessionError("invalid persisted empirical session")
    return data


def _ledger_write_once(path: Path, record: dict) -> None:
    if path.is_symlink() or not path.parent.is_dir():
        raise EmpiricalSessionError("unsafe empirical session directory")
    # First creation only. A different EVCC cable epoch must be qualified
    # separately; do not carry a prior pause into a new cable session.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(record, out, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        path.unlink(missing_ok=True)
        raise



def _can_replace_ledger(*, old: dict, cable: dict, now: datetime,
                        evcc_path: Path, owner_path: Path,
                        settings_revision: str | None = None) -> bool:
    """Only witnessed replug + already-running 6 A can open a new mission."""
    try:
        if (old.get("source") != QUALITY
                or old.get("cable_tracker_id") != cable["tracking_id"]
                or type(old.get("connection_epoch")) is not int
                or cable["observed_state"] != "C2"):
            return False
        replug = (cable["connection_epoch"] > old["connection_epoch"]
                  and cable.get("confirmed_replug") is True)
        if replug:
            replugged = _instant(cable["last_reconnected_at_utc"])
            if not 0 <= (now - replugged).total_seconds() <= 3600:
                return False
        settings_change = (settings_revision is not None
                           and settings_revision != old.get("settings_revision")
                           and cable["connection_epoch"] == old["connection_epoch"])
        if not (replug or settings_change):
            return False
        prior_owner = ownership.read_owner(owner_path)
        if prior_owner is not None and prior_owner.get("state") != "released":
            return False
        evcc = _json(evcc_path)
        return (
            evcc.get("source") == "waveshare_evcc_fc03_readonly"
            and evcc.get("read_ok") is True
            and evcc.get("writes") == 0
            and evcc.get("physical_control_eligible") is False
            and _fresh(evcc.get("timestamp"), now=now, limit_s=65)
            and observed_evcc_mode(
                evcc.get("state"), evcc.get("configured_pwm_raw"),
                evcc.get("pilot_pwm_raw"), evcc.get("channel_currents_a")
            ) == "charging_6a"
        )
    except (OSError, ValueError, TypeError, KeyError,
            ownership.OwnershipError):
        return False


def _ledger_replace(path: Path, record: dict) -> None:
    """Replace a private ledger only after a verified new cable session."""
    if _ledger_read(path) is None:
        raise EmpiricalSessionError("missing prior ledger during rotation")
    fd, tmp = tempfile.mkstemp(prefix=".ev-price-session-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            os.fchmod(out.fileno(), 0o600)
            json.dump(record, out, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def read_session(*, status_path: Path, settings_path: Path,
                 cable_path: Path, ledger_path: Path, now: datetime,
                 expected_vin_sha256: str | None,
                 evcc_path: Path | None = None,
                 owner_path: Path | None = None) -> dict | None:
    """Use empirical factor ONLY when explicitly accepted by this operator."""
    if os.environ.get("EV_PRICE_ACCEPT_EMPIRICAL_ENERGY") != "1":
        return None
    if (now.tzinfo is None or not isinstance(expected_vin_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_vin_sha256)):
        return None
    try:
        cable = read_fresh_connected(cable_path, now=now)
        if cable is None:
            return None
        data = _json(status_path)
        if (data.get("source") != "home_assistant"
                or data.get("mode") != "shadow"
                or data.get("advisory_only") is not True
                or data.get("physical_control_eligible") is not False
                or data.get("writes_to_devices") != 0
                or data.get("shadow_observation_usable") is not True
                or data.get("connected") is not True
                or data.get("target_soc_pct") != 80
                or data.get("oem_timestamp_role") != "last_changed_not_sampled"
                or data.get("timestamp_origin") != "volvo_energy_state_field"
                or data.get("observation_quality") not in (
                    "api_recent_field_change_unverified",
                    "api_recent_fields_changed_recently")
                or not _fresh(data.get("checked_at_utc"), now=now,
                              limit_s=MAX_STATUS_AGE_S)
                or not _fresh(data.get("energy_api_received_at_utc"), now=now,
                              limit_s=MAX_SNAPSHOT_AGE_S)
                or not _fresh(data.get("battery_reported_at_utc"), now=now,
                              limit_s=MAX_SOC_LAST_CHANGE_AGE_S)):
            return None
        soc = data.get("battery_soc_pct")
        if (type(soc) not in (float, int) or not math.isfinite(soc)
                or not 0 <= soc <= 100):
            return None
        settings = load_settings(str(settings_path))
        if (data.get("charge_mode") != settings["charge_mode"]
                or data.get("departure_local_time") != settings["departure_local_time"]):
            return None
        mode = settings["charge_mode"]
        revision = settings.get("revision")
        key = (expected_vin_sha256 + ":" + cable["tracking_id"] + ":"
               + str(cable["connection_epoch"]) + ":" + mode)
        if revision is not None:
            key += ":ui:" + revision
        session_id = str(uuid5(NAMESPACE_URL, "solinteg-ev-price:" + key))
        stored = _ledger_read(ledger_path)
        if stored is not None and (stored.get("session_id") != session_id):
            # Only a *witnessed new cable connection* already charging at
            # 6 A may start a new mission. An old price pause can never be
            # inherited across a reconnect, polling reset or manual stop.
            if not _can_replace_ledger(
                    old=stored, cable=cable, now=now,
                    evcc_path=evcc_path or status_path.parent / "evcc-readonly.json",
                    owner_path=owner_path or status_path.parent / "ev-price-pause-owner.json",
                    settings_revision=revision):
                return None
            stored = None
            rotate = True
        else:
            rotate = False
        if stored is None:
            anchor = now
            if (not rotate and cable.get("confirmed_replug") is True
                    and cable.get("last_reconnected_at_utc")):
                anchor = _instant(cable["last_reconnected_at_utc"])
                if (now - anchor).total_seconds() < 0 or (now - anchor).total_seconds() > 3600:
                    return None
            deadline = (next_departure(settings["departure_local_time"], now=anchor)
                        if mode == "deadline" else None)
            record = {
                "schema_version": 1,
                "source": QUALITY,
                "session_id": session_id,
                "cable_tracker_id": cable["tracking_id"],
                "connection_epoch": cable["connection_epoch"],
                "charge_mode": mode,
                "settings_revision": revision,
                "departure_at_utc": deadline.isoformat() if deadline else None,
                "created_at_utc": now.isoformat(),
                "factor_kwh_per_soc_pct": KWH_PER_SOC_PCT,
                "independent_ac_meter_verified": False,
                "observation_time_semantics": "cloud_last_changed_not_bms_sample",
            }
            if rotate:
                _ledger_replace(ledger_path, record)
            else:
                _ledger_write_once(ledger_path, record)
            stored = record
        if (stored.get("session_id") != session_id
                or stored.get("cable_tracker_id") != cable["tracking_id"]
                or stored.get("connection_epoch") != cable["connection_epoch"]
                or stored.get("charge_mode") != mode
                or stored.get("settings_revision") != revision
                or stored.get("factor_kwh_per_soc_pct") != KWH_PER_SOC_PCT
                or stored.get("independent_ac_meter_verified") is not False
                or stored.get("observation_time_semantics") != "cloud_last_changed_not_bms_sample"):
            return None
        UUID(stored["session_id"])
        if mode == "deadline":
            departure = _instant(stored.get("departure_at_utc"))
            if not now < departure <= now + timedelta(hours=48):
                return None
        elif stored.get("departure_at_utc") is not None:
            return None
        else:
            departure = None
        return {
            "session_id": session_id,
            "connection_epoch": cable["connection_epoch"],
            "charge_mode": mode,
            "departure_local_time": settings["departure_local_time"],
            "settings_revision": revision,
            "departure_at_utc": departure,
            "remaining_ac_kwh": round(max(0, 80 - soc) * KWH_PER_SOC_PCT, 3),
            "target_soc_pct": 80,
            "max_buy_ore_per_kwh": settings["max_buy_ore_per_kwh"],
            "energy_quality": QUALITY,
            "physical_sample_verified": False,
            "independent_ac_meter_verified": False,
        }
    except (OSError, ValueError, TypeError, KeyError, OverflowError,
            CableObservationError, EmpiricalSessionError):
        return None
