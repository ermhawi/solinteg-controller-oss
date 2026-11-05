#!/usr/bin/env python3
"""Read-only, time-aligned history of Solinteg + Volvo + Waveshare observations.

Reads local JSON files ONLY, never imports a control driver, has no network
functions, and writes a separate private SQLite database in the isolated
shadow runtime. Missing or stale measurements stay NULL, never become zero.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import common
import ev_charge_end

UTC = timezone.utc
INVERTER_MAX_AGE_S = 120
EVCC_MAX_AGE_S = 90
HA_MAX_AGE_S = 150
PLAN_MAX_AGE_S = 150
DEFAULT_RETENTION_DAYS = 30
STOCKHOLM = ZoneInfo("Europe/Stockholm")
SOC_CHART_LOOKBACK_H = 48

SCHEMA = """
CREATE TABLE IF NOT EXISTS ev_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at_utc TEXT NOT NULL,
    captured_epoch_ms INTEGER NOT NULL,
    inverter_at_utc TEXT,
    inverter_age_s REAL,
    inverter_fresh INTEGER NOT NULL DEFAULT 0,
    l1_grid_w REAL,
    l2_grid_w REAL,
    l3_grid_w REAL,
    grid_w REAL,
    battery_w REAL,
    house_load_w REAL,
    pv_w REAL,
    house_soc_pct REAL,
    inverter_ac_w REAL,
    battery_temp_c REAL,
    grid_unbalanced_output_raw INTEGER,
    evcc_at_utc TEXT,
    evcc_age_s REAL,
    evcc_record_fresh INTEGER NOT NULL DEFAULT 0,
    evcc_read_ok INTEGER,
    evcc_error TEXT,
    evcc_state TEXT,
    evcc_configured_raw INTEGER,
    evcc_pilot_raw INTEGER,
    evcc_measured_a REAL,
    evcc_ict1_a REAL,
    evcc_ict2_a REAL,
    evcc_ict3_a REAL,
    evcc_charging_detected INTEGER,
    evcc_6a_limit_verified INTEGER,
    ha_checked_at_utc TEXT,
    ha_age_s REAL,
    ha_record_fresh INTEGER NOT NULL DEFAULT 0,
    volvo_api_received_at_utc TEXT,
    volvo_battery_last_changed_utc TEXT,
    volvo_soc_pct REAL,
    volvo_target_pct REAL,
    volvo_connected INTEGER,
    volvo_observation_usable INTEGER,
    volvo_quality TEXT,
    plan_at_utc TEXT,
    plan_age_s REAL,
    plan_fresh INTEGER NOT NULL DEFAULT 0,
    charge_mode TEXT,
    suggested_current_a REAL,
    plan_reported_permitted_a REAL,
    shadow_grid_topup_a REAL,
    shadow_buy_ore_kwh REAL,
    shadow_cheap_threshold_ore_kwh REAL,
    shadow_reason TEXT,
    physical_writes INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ev_samples_captured_epoch_ms_idx
  ON ev_samples(captured_epoch_ms);
"""

# Added observations are nullable. Older private SQLite histories must migrate
# in place without inferring missing Ict2, phase balance, or battery temperature.
ADDED_METER_COLUMNS = {
    "inverter_ac_w": "REAL",
    "battery_temp_c": "REAL",
    "grid_unbalanced_output_raw": "INTEGER",
    "evcc_ict1_a": "REAL",
    "evcc_ict2_a": "REAL",
    "evcc_ict3_a": "REAL",
}


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if math.isfinite(v) else None


def _flag(value):
    return int(value) if isinstance(value, bool) else None


def _text(value, max_len=140):
    return value[:max_len] if isinstance(value, str) else None


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _snapshot(path: str, *, timestamp_key: str, now: datetime,
              limit_s: int) -> tuple[dict, str | None, float | None, bool]:
    data = common.read_json(path)
    if not isinstance(data, dict):
        return {}, None, None, False
    parsed = _time(data.get(timestamp_key))
    if parsed is None:
        return data, None, None, False
    seconds = round((now - parsed).total_seconds(), 2)
    return data, parsed.isoformat(), seconds, -5 <= seconds <= limit_s


def capture(*, now: datetime | None = None,
            inverter_path: str | None = None,
            evcc_path: str | None = None,
            ha_path: str | None = None,
            plan_path: str | None = None) -> dict:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("capture requires UTC-aware timestamp")
    now = now.astimezone(UTC)

    inverter, it, ia, ifresh = _snapshot(
        inverter_path or os.environ.get("INVERTER_DATA_PATH", "/opt/solinteg/live.json"),
        timestamp_key="timestamp", now=now, limit_s=INVERTER_MAX_AGE_S)
    evcc, et, ea, efresh = _snapshot(
        evcc_path or os.environ.get("EVCC_READONLY_PATH",
                                  "/opt/solinteg/evcc-readonly.json"),
        timestamp_key="timestamp", now=now, limit_s=EVCC_MAX_AGE_S)
    ha, ht, haage, hfresh = _snapshot(
        ha_path or os.environ.get("EV_STATUS_PATH",
                                "/opt/solinteg/ev-session-status.json"),
        timestamp_key="checked_at_utc", now=now, limit_s=HA_MAX_AGE_S)
    plan, pt, pa, pfresh = _snapshot(
        plan_path or os.environ.get("EV_SHADOW_PLAN_PATH",
                                  "/opt/solinteg/ev-shadow-plan.json"),
        timestamp_key="timestamp", now=now, limit_s=PLAN_MAX_AGE_S)

    # A successful FC03 poll is evidence of the EVCC registers. A failed poll
    # (even if recently written) is explicitly NOT a 0 A current measurement.
    evcc_source = evcc.get("source") == "waveshare_evcc_fc03_readonly"
    read_ok = efresh and evcc_source and evcc.get("read_ok") is True
    config = _number(evcc.get("configured_pwm_raw")) if read_ok else None
    pilot = _number(evcc.get("pilot_pwm_raw")) if read_ok else None
    current = _number(evcc.get("measured_current_a")) if read_ok else None
    valid_current = current is not None and 0 <= current <= 32
    # Preserve Ict2 separately for future analysis. The old history stores only
    # the channel sum, which cannot prove which phase carried the EV current.
    raw_channels = evcc.get("channel_currents_a") if read_ok and valid_current else None
    channels = None
    if type(raw_channels) is list and len(raw_channels) == 3:
        parsed = [_number(a) if type(a) in (int, float) else None
                  for a in raw_channels]
        if (all(a is not None and 0 <= a <= 32 for a in parsed)
                and abs(sum(parsed) - current) <= .21):
            channels = parsed
    # The pilot cap persists only while the hardware actually reports it.
    # When readback fails, NULL rather than false (not a known mismatch).
    cap6 = int(config == 100 and pilot == 100) if config is not None and pilot is not None else None

    ha_source_ok = hfresh and ha.get("source") == "home_assistant"
    # api_received_at_utc is distinct from the OEM last-changed timestamp.
    api_time = _time(ha.get("energy_api_received_at_utc")) if ha_source_ok else None
    if api_time is not None and not -5 <= (now - api_time).total_seconds() <= 5 * 60:
        api_time = None
    ha_observation = (ha_source_ok and api_time is not None
                      and ha.get("shadow_observation_usable") is True
                      and ha.get("physical_control_eligible") is False)
    quality = _text(ha.get("observation_quality")) if ha_observation else None

    plan_source_ok = (pfresh and plan.get("mode") == "shadow"
                      and plan.get("source") in (
                          "ha_price_scenario_with_evcc",
                          "ha_price_scenario_no_evcc"))
    # We log what the shadow reported, but it cannot grant physical control.
    permitted = _number(plan.get("charging_permitted_a")) if plan_source_ok else None
    return {
        "captured_at_utc": now.isoformat(),
        "captured_epoch_ms": int(now.timestamp() * 1000),
        "inverter_at_utc": it,
        "inverter_age_s": ia,
        "inverter_fresh": int(ifresh),
        "l1_grid_w": _number(inverter.get("meter_l1_w")) if ifresh else None,
        "l2_grid_w": _number(inverter.get("meter_l2_w")) if ifresh else None,
        "l3_grid_w": _number(inverter.get("meter_l3_w")) if ifresh else None,
        "grid_w": _number(inverter.get("grid_w")) if ifresh else None,
        "battery_w": _number(inverter.get("battery_w")) if ifresh else None,
        "house_load_w": _number(inverter.get("house_load_w")) if ifresh else None,
        "pv_w": _number(inverter.get("pv_w")) if ifresh else None,
        "house_soc_pct": _number(inverter.get("soc_pct")) if ifresh else None,
        "inverter_ac_w": _number(inverter.get("inverter_ac_w")) if ifresh else None,
        "battery_temp_c": _number(inverter.get("battery_temp_c")) if ifresh else None,
        "grid_unbalanced_output_raw": (
            inverter.get("grid_unbalanced_output_raw")
            if ifresh and type(inverter.get("grid_unbalanced_output_raw")) is int
            and 0 <= inverter["grid_unbalanced_output_raw"] <= 65535 else None),
        "evcc_at_utc": et,
        "evcc_age_s": ea,
        "evcc_record_fresh": int(efresh),
        "evcc_read_ok": _flag(evcc.get("read_ok")) if efresh and evcc_source else None,
        "evcc_error": _text(evcc.get("reason")) if efresh and evcc_source and not read_ok else None,
        "evcc_state": _text(evcc.get("state")) if read_ok else None,
        "evcc_configured_raw": int(config) if config is not None else None,
        "evcc_pilot_raw": int(pilot) if pilot is not None else None,
        "evcc_measured_a": current if valid_current else None,
        "evcc_ict1_a": channels[0] if channels is not None else None,
        "evcc_ict2_a": channels[1] if channels is not None else None,
        "evcc_ict3_a": channels[2] if channels is not None else None,
        "evcc_charging_detected": (
            _flag(evcc.get("charging_detected")) if read_ok and valid_current else None),
        "evcc_6a_limit_verified": cap6,
        "ha_checked_at_utc": ht,
        "ha_age_s": haage,
        "ha_record_fresh": int(hfresh),
        "volvo_api_received_at_utc": api_time.isoformat() if ha_observation else None,
        "volvo_battery_last_changed_utc": (
            _text(ha.get("battery_reported_at_utc")) if ha_observation else None),
        "volvo_soc_pct": _number(ha.get("battery_soc_pct")) if ha_observation else None,
        "volvo_target_pct": _number(ha.get("target_soc_pct")) if ha_observation else None,
        "volvo_connected": _flag(ha.get("connected")) if ha_observation else None,
        "volvo_observation_usable": int(ha_observation),
        "volvo_quality": quality,
        "plan_at_utc": pt,
        "plan_age_s": pa,
        "plan_fresh": int(plan_source_ok),
        "charge_mode": (_text(plan.get("charge_mode")) if plan_source_ok else None),
        "suggested_current_a": (_number(plan.get("charging_suggested_a"))
                                if plan_source_ok else None),
        "plan_reported_permitted_a": permitted,
        "shadow_grid_topup_a": (_number(plan.get("grid_topup_suggested_a"))
                                if plan_source_ok else None),
        "shadow_buy_ore_kwh": (_number(plan.get("price_buy_ore_kwh"))
                               if plan_source_ok else None),
        "shadow_cheap_threshold_ore_kwh": (
            _number(plan.get("price_cheap_threshold_ore_kwh"))
            if plan_source_ok else None),
        "shadow_reason": (_text(plan.get("price_reason"), 180)
                          if plan_source_ok else None),
        "physical_writes": 0,
    }


def append(path: str, record: dict, *, retention_days: int = DEFAULT_RETENTION_DAYS,
           prune: bool = False) -> None:
    if not 1 <= retention_days <= 90:
        raise ValueError("history retention must be between 1 and 90 days")
    with sqlite3.connect(path, timeout=3) as con:
        con.execute("PRAGMA busy_timeout=3000")
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        columns = {row[1] for row in con.execute("PRAGMA table_info(ev_samples)")}
        for name, kind in ADDED_METER_COLUMNS.items():
            if name not in columns:
                con.execute(f"ALTER TABLE ev_samples ADD COLUMN {name} {kind}")
        fields = tuple(record)
        con.execute(
            "INSERT INTO ev_samples (" + ",".join(fields) + ") VALUES ("
            + ",".join("?" for _ in fields) + ")",
            tuple(record.values()),
        )
        if prune:
            threshold = record["captured_epoch_ms"] - retention_days * 86400 * 1000
            con.execute("DELETE FROM ev_samples WHERE captured_epoch_ms < ?", (threshold,))
        con.commit()


def build_soc_timeline(db_path: str, *, now: datetime | None = None) -> dict:
    """Summarise Volvo's *reported* level per local 15-min chart slot.

    Captured/API reception is NOT a new OEM BMS sample. Keep the last
    credible API observation in each slot, retain the separate last-changed
    timestamp, and never interpolate gaps or promote this to control input.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    since_ms = int((now - timedelta(hours=SOC_CHART_LOOKBACK_H)).timestamp() * 1000)
    until_ms = int(now.timestamp() * 1000)
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3) as db:
        rows = db.execute(
            "SELECT captured_at_utc, volvo_soc_pct, volvo_battery_last_changed_utc, "
            "volvo_api_received_at_utc "
            "FROM ev_samples WHERE captured_epoch_ms BETWEEN ? AND ? "
            "AND ha_record_fresh = 1 AND volvo_observation_usable = 1 "
            "AND volvo_quality IN (?, ?) AND volvo_soc_pct BETWEEN 0 AND 100 "
            "ORDER BY captured_epoch_ms ASC",
            (since_ms, until_ms,
             "api_recent_field_change_unverified", "api_recent_fields_changed_recently"),
        )
        # Each slot keeps the last actually observed value; never claim a
        # repeated poll refreshes the upstream Volvo/BMS measurement time.
        per_slot: dict[str, dict] = {}
        for captured, soc, last_changed, api_received in rows:
            sample_at = _time(captured)
            source_at = _time(last_changed)
            api_at = _time(api_received)
            if (sample_at is None or api_at is None or source_at is None
                    or api_at > sample_at + timedelta(seconds=5)
                    or source_at > api_at + timedelta(seconds=5)):
                continue
            local = sample_at.astimezone(STOCKHOLM)
            quarter = local.replace(minute=local.minute // 15 * 15, second=0,
                                    microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")
            if local.fold:
                quarter += "#2"
            per_slot[quarter] = {
                "time": quarter,
                "socPct": float(soc),
                "capturedAtUtc": sample_at.isoformat(),
                "reportedAtUtc": source_at.isoformat(),
            }
    points = list(per_slot.values())
    return {
        "version": 1,
        "generatedAtUtc": now.isoformat(),
        "source": "volvo_energy_api_observation",
        "quality": "api_reported_not_verified_bms_sample",
        "points": points,
        "latest": points[-1] if points else None,
    }


def publish_soc_timeline(db_path: str, output_path: str, *,
                         now: datetime | None = None) -> dict:
    """Export only rounded SoC, not the private 30-day EV telemetry DB.

    Use an atomic group-readable 0640 JSON in the historian's existing
    0750 runtime directory so the Solinteg web process can display it.
    The SQLite database itself stays 0600 and never becomes web-readable.
    """
    value = build_soc_timeline(db_path, now=now)
    tmp = f"{output_path}.{secrets.token_hex(6)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, separators=(",", ":"))
            output.flush()
            os.fchmod(output.fileno(), 0o640)
            os.fsync(output.fileno())
        os.replace(tmp, output_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return value


def build_ev_load_hourly(db_path: str, *, now: datetime | None = None,
                         days: int = 14) -> dict:
    """Only full, independently observed EVCC hours may correct house load.

    Uses captured UTC hours (matching the existing poller SQL aggregation) and
    distinct fresh FC03 timestamps. A partial or uncertain hour is OMITTED,
    not guessed, extrapolated or treated as zero EV demand.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    since_ms = int((now - timedelta(days=days)).timestamp() * 1000)
    end_ms = int(now.replace(minute=0, second=0, microsecond=0).timestamp() * 1000)
    hours: dict[str, dict] = {}
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3) as db:
        rows = db.execute(
            "SELECT captured_at_utc, evcc_at_utc, evcc_record_fresh, "
            "evcc_read_ok, evcc_state, evcc_charging_detected, "
            "evcc_ict1_a, evcc_ict2_a, evcc_ict3_a "
            "FROM ev_samples WHERE captured_epoch_ms >= ? AND captured_epoch_ms < ? "
            "ORDER BY captured_epoch_ms ASC",
            (since_ms, end_ms),
        )
        for captured, sampled, fresh, ok, state, charging, l1, l3, other in rows:
            captured_at = _time(captured)
            sampled_at = _time(sampled)
            if captured_at is None:
                continue
            hour = captured_at.strftime("%Y-%m-%dT%H:00:00Z")
            group = hours.setdefault(hour, {"total": 0, "samples": {}})
            group["total"] += 1
            if (fresh != 1 or ok != 1 or state not in ("A1", "B2", "C2")
                    or charging not in (0, 1) or sampled_at is None
                    or not -5 <= (captured_at - sampled_at).total_seconds() <= EVCC_MAX_AGE_S
                    or any(type(a) not in (int, float) or not math.isfinite(a)
                           for a in (l1, l3, other))
                    or not (0 <= l1 <= .5 and 0 <= other <= .5 and 0 <= l3 <= 16.5)
                    or (charging == 1 and state != "C2")
                    or (charging == 1 and l3 < 1)):
                continue
            group["samples"][sampled_at] = (l3 * 230 if charging else 0.0)

    complete: list[dict] = []
    for utc_hour, group in sorted(hours.items()):
        samples = sorted(group["samples"].items())
        # Historian nominally polls every 30 s. A full hour has 120 captures.
        # Require >=100 valid distinct FC03 observations, a >=50-min span,
        # and >=85% valid samples so gaps cannot bias the inferred EV power.
        if (len(samples) < 100 or len(samples) / max(1, group["total"]) < .85
                or (samples[-1][0] - samples[0][0]).total_seconds() < 3000):
            continue
        avg_w = round(sum(w for _, w in samples) / len(samples), 1)
        complete.append({"utcHour": utc_hour, "evAvgW": avg_w,
                         "samples": len(samples)})
    return {"version": 1, "source": "waveshare_evcc_fc03_ict2",
            "quality": "full_hour_measured_current_nominal_230v",
            "generatedAtUtc": now.isoformat(), "hours": complete}


def publish_ev_load_hourly(db_path: str, output_path: str, *,
                           now: datetime | None = None) -> dict:
    """Publish sanitized hourly load without exposing the private EV database."""
    value = build_ev_load_hourly(db_path, now=now)
    tmp = f"{output_path}.{secrets.token_hex(6)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, separators=(",", ":"))
            output.flush()
            os.fchmod(output.fileno(), 0o640)
            os.fsync(output.fileno())
        os.replace(tmp, output_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true", help="capture periodically")
    args = parser.parse_args()
    if os.environ.get("EV_HISTORY_ENABLED") != "1":
        print(json.dumps({"mode": "disabled", "writes_to_devices": 0}))
        return
    seconds = max(20, min(300, int(os.environ.get("EV_HISTORY_POLL_SECONDS", "30"))))
    days = int(os.environ.get("EV_HISTORY_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS)))
    path = os.environ.get(
        "EV_HISTORY_DB_PATH", "/opt/solinteg/ev-shadow-runtime/var/ev-history.sqlite")
    iteration = 0
    while True:
        record = capture()
        append(path, record, retention_days=days, prune=iteration % 120 == 0)
        try:
            ev_charge_end.record_and_publish(
                path,
                os.environ.get("EVCC_CABLE_TRACK_PATH",
                    "/opt/solinteg/ev-shadow-runtime/var/evcc-cable-state.json"),
                os.environ.get("EV_LAST_CHARGE_SESSION_PATH",
                    "/opt/solinteg/ev-shadow-runtime/var/ev-last-charge-session.json"),
                now=datetime.now(UTC))
        except (OSError, sqlite3.Error, ValueError) as exc:
            # Optional UI/history event; never interrupt the existing sampler.
            print(f"EV charge-end export unavailable: {type(exc).__name__}", flush=True)
        if iteration % 4 == 0:
            try:
                publish_soc_timeline(path, os.environ.get(
                    "EV_SOC_TIMELINE_PATH",
                    "/opt/solinteg/ev-shadow-runtime/var/ev-soc-timeline.json"),
                    now=datetime.now(UTC))
                publish_ev_load_hourly(path, os.environ.get(
                    "EV_LOAD_HOURLY_PATH",
                    "/opt/solinteg/ev-shadow-runtime/var/ev-load-hourly.json"),
                    now=datetime.now(UTC))
            except (OSError, sqlite3.Error, ValueError) as exc:
                # The optional UI export must not interrupt the private historian.
                print(f"EV SoC chart export unavailable: {type(exc).__name__}", flush=True)
        if not args.watch:
            print(json.dumps({
                "captured_at_utc": record["captured_at_utc"],
                "inverter_fresh": record["inverter_fresh"],
                "evcc_read_ok": record["evcc_read_ok"],
                "evcc_6a_limit_verified": record["evcc_6a_limit_verified"],
                "volvo_observation_usable": record["volvo_observation_usable"],
                "plan_fresh": record["plan_fresh"],
                "writes_to_devices": 0,
            }))
            return
        iteration += 1
        time.sleep(seconds)


if __name__ == "__main__":
    main()
