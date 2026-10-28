#!/usr/bin/env python3
"""Read-only EVCC↔Volvo SoC/L3 quality audit; NEVER a battery-efficiency calibration.

Combines isolated EV AC fragment evidence with existing 30-second house/Volvo
historian. SoC is rounded/cloud-reported, not a calibrated DC energy meter.
Pre/post L3 import is a counterfactual indication, not proof of source mix.
Never derives invoices, kWh of stored DC, kr/mil or physical control permission.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import ev_ac_sessions as sessions

UTC = timezone.utc
MAX_ROWS = 30000
MAX_AGE_HOURS = 72
SOURCE_QUALITY = "api_recent_field_change_unverified"


class QualityError(ValueError):
    """Diagnostic with no raw VIN, route, IP or secrets."""


def _ro(path: str) -> sqlite3.Connection | None:
    p = Path(path)
    if not p.is_file():
        return None
    uri = "file:" + quote(str(p.resolve()), safe="/") + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=3)


def read_samples(path: str, *, since_ms: int, until_ms: int) -> list[dict]:
    db = _ro(path)
    if db is None:
        return []
    fields = (
        "captured_epoch_ms", "captured_at_utc", "inverter_fresh",
        "l3_grid_w", "battery_w", "house_load_w", "pv_w",
        "evcc_read_ok", "evcc_charging_detected", "evcc_state",
        "evcc_configured_raw", "evcc_pilot_raw",
        "ha_record_fresh", "volvo_observation_usable",
        "volvo_quality", "volvo_soc_pct", "volvo_connected",
        "volvo_battery_last_changed_utc", "volvo_api_received_at_utc",
        "physical_writes",
    )
    try:
        query = ("SELECT " + ",".join(fields) + " FROM ev_samples "
                 "WHERE captured_epoch_ms BETWEEN ? AND ? "
                 "ORDER BY captured_epoch_ms LIMIT ?")
        rows = db.execute(query, (since_ms, until_ms, MAX_ROWS + 1)).fetchall()
    except sqlite3.Error as exc:
        raise QualityError("historian schema/read error") from exc
    finally:
        db.close()
    if len(rows) > MAX_ROWS:
        raise QualityError("requested historian window exceeds safety limit")
    data = [dict(zip(fields, r)) for r in rows]
    if any(row["physical_writes"] != 0 for row in data):
        raise QualityError("historian write flag is not read-only")
    if any(b["captured_epoch_ms"] <= a["captured_epoch_ms"]
           for a,b in zip(data,data[1:])):
        raise QualityError("historian observations not chronologically unique")
    return data


def _sane_timestamp(value: object) -> datetime | None:
    if not isinstance(value,str):
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z","+00:00"))
    except ValueError:
        return None
    return t.astimezone(UTC) if t.tzinfo else None


def soc_evidence(rows: list[dict], *, start_ms: int, end_ms: int) -> dict:
    """Evidence of *reported* SoC steps only; no battery capacity/efficiency."""
    usable = []
    for row in rows:
        if not start_ms <= row["captured_epoch_ms"] <= end_ms:
            continue
        if row["volvo_observation_usable"] != 1 or row["ha_record_fresh"] != 1:
            continue
        if row["volvo_quality"] != SOURCE_QUALITY or row["volvo_connected"] != 1:
            continue
        level = row["volvo_soc_pct"]
        if type(level) not in (float,int) or not math.isfinite(level) or not 0 <= level <= 100:
            continue
        changed = _sane_timestamp(row["volvo_battery_last_changed_utc"])
        received = _sane_timestamp(row["volvo_api_received_at_utc"])
        captured = datetime.fromtimestamp(row["captured_epoch_ms"]/1000,UTC)
        if (changed is None or received is None
                or not changed <= received <= captured + timedelta(seconds=5)
                or captured - received > timedelta(minutes=5)):
            continue
        usable.append((row["captured_epoch_ms"], level,changed))
    changes = []
    for at, level, changed in usable:
        if not changes or level != changes[-1]["soc_pct"]:
            changes.append({
                "observed_at_utc": datetime.fromtimestamp(at/1000,UTC).isoformat(),
                "reported_last_changed_utc": changed.isoformat(),
                "soc_pct": level,
            })
    points = changes[-80:]
    delta = round(changes[-1]["soc_pct"]-changes[0]["soc_pct"],3) if len(changes)>1 else 0.0
    # Display only; even 5% doesn't prove usable kWh or charger DC efficiency.
    quantized = len(changes)>1 and all(float(x["soc_pct"]).is_integer() for x in changes)
    sufficient_for_trend = len(changes)>1 and delta >= 5 and quantized
    return {
        "source": "home_assistant_volvo_soc_history",
        "timestamp_role": "last_changed_not_verified_bms_sample",
        "observation_quality": SOURCE_QUALITY if usable else "unavailable",
        "usable_historian_rows": len(usable),
        "distinct_reported_soc_levels": len(changes),
        "first_soc_pct": changes[0]["soc_pct"] if changes else None,
        "last_soc_pct": changes[-1]["soc_pct"] if changes else None,
        "delta_soc_percentage_points": delta if len(changes)>1 else None,
        "soc_change_observations": points,
        "soc_changes_truncated": len(changes)>len(points),
        "minimum_trend_resolution_met": sufficient_for_trend,
        "reason": ("insufficient_soc_steps_and_unverified_usable_capacity"
                   if not sufficient_for_trend else
                   "unverified_usable_capacity_and_report_timestamps"),
        "calibrated_usable_capacity_kwh": None,
        "calibrated_ac_dc_efficiency": None,
        "verified_net_battery_dc_kwh": None,
    }


def _median(rows: list[dict], key: str) -> float | None:
    values = [float(x[key]) for x in rows
              if type(x.get(key)) in (int,float) and math.isfinite(x[key])]
    return statistics.median(values) if len(values)>=6 else None


def l3_transition_evidence(rows: list[dict]) -> dict:
    """Observational night-time power step around first 6A C2, NOT source metering."""
    verified = [r for r in rows
                if r["evcc_read_ok"] == 1
                and r["evcc_state"] == "C2"
                and r["evcc_charging_detected"] == 1
                and r["evcc_configured_raw"] == r["evcc_pilot_raw"] == 100]
    if not verified:
        return {"quality":"unavailable", "reason":"no_confirmed_c2_6a"}
    first = verified[0]["captured_epoch_ms"]
    # Baseline excludes final 3 min before first confirmed C2: the EVCC
    # might have lost its serial connection while the car actually started.
    baseline = [r for r in rows
                if first-8*60000 <= r["captured_epoch_ms"] < first-3*60000
                and r["inverter_fresh"] == 1]
    during = [r for r in rows
              if first <= r["captured_epoch_ms"] <= first+5*60000
              and r["inverter_fresh"] == 1
              and r["evcc_read_ok"] == 1
              and r["evcc_charging_detected"] == 1]
    pv = [_median(p,"pv_w") for p in (baseline,during)]
    l3 = [_median(p,"l3_grid_w") for p in (baseline,during)]
    batt = [_median(p,"battery_w") for p in (baseline,during)]
    house = [_median(p,"house_load_w") for p in (baseline,during)]
    observed_pre_evcc_idle = sum(
        r["evcc_read_ok"]==1 and r["evcc_charging_detected"]==0
        for r in baseline)
    if (len(baseline)<6 or len(during)<6
            or any(v is None for v in pv+l3+batt+house)):
        return {"quality":"unavailable", "reason":"insufficient_synced_baseline_or_active_data"}
    # Existing inverter sign convention: negative L3 = net grid import.
    l3_increment = l3[0]-l3[1]
    discharge_change = batt[1]-batt[0]
    return {
        "quality":"indicative_only",
        "evcc_confirmed_c2_utc": datetime.fromtimestamp(first/1000,UTC).isoformat(),
        "baseline_samples":len(baseline),
        "charging_samples":len(during),
        "precharge_evcc_idle_readback_confirmed":observed_pre_evcc_idle>=3,
        "baseline_l3_import_w":round(-l3[0],1),
        "during_l3_import_w":round(-l3[1],1),
        "incremental_l3_import_w":round(l3_increment,1),
        "reference_6a_at_assumed_230v_w":1380,
        "baseline_house_battery_discharge_w":round(batt[0],1),
        "during_house_battery_discharge_w":round(batt[1],1),
        "change_house_battery_discharge_w":round(discharge_change,1),
        "baseline_house_load_w":round(house[0],1),
        "during_house_load_w":round(house[1],1),
        "baseline_pv_w":round(pv[0],1),
        "during_pv_w":round(pv[1],1),
        "l3_delta_consistent_with_6a_230v":abs(l3_increment-1380)<=350,
        "house_battery_discharge_not_increasing":discharge_change<=150,
        "source_allocation_verified":False,
        "independent_fuse_rms_verified":False,
        "notes":"other_house_load_can_change_and_evcc_was_not_necessarily_readable_before_c2",
    }


def audit(ac_db: str, historian_db: str, *, now: datetime | None = None,
          hours: int = 24) -> dict:
    if hours not in (24, 48, 72):
        raise ValueError("hours must be 24, 48 or 72 (aligned to complete days)")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now requires timezone")
    now = now.astimezone(UTC)
    end_ms = int(now.timestamp()*1000)
    since_ms = end_ms - hours * 3600000
    rows = read_samples(historian_db,since_ms=since_ms,until_ms=end_ms)
    session = sessions.analyze(ac_db,price_path=None,now=now,days=hours//24)
    if session.get("fragments_truncated") is True:
        raise QualityError("AC coverage fragments truncated; refuse SoC correlation")
    fragments = session.get("observed_charging_fragments") or []
    if fragments:
        earliest = int(datetime.fromisoformat(fragments[0]["observed_start_utc"]).timestamp()*1000)
        latest = int(datetime.fromisoformat(fragments[-1]["observed_end_utc"]).timestamp()*1000)
        soc = soc_evidence(rows,start_ms=earliest,end_ms=latest)
    else:
        soc = soc_evidence(rows,start_ms=end_ms+1,end_ms=end_ms+2)
    return {
        "source":"read_only_evcc_inverter_volvo_qualification",
        "as_of_utc":now.isoformat(),
        "quality":"diagnostic_not_calibration",
        "ac_observation_quality":session["quality"],
        "ac_kwh_covered_only":session["estimated_ac_kwh_covered_only"],
        "ac_covered_seconds":session["covered_seconds"],
        "historian_samples":len(rows),
        "soc_evidence":soc,
        "l3_transition_evidence":l3_transition_evidence(rows),
        "vehicle_net_dc_kwh":None,
        "calibrated_ac_dc_efficiency":None,
        "actual_charging_cost_ore":None,
        "cost_per_mile_kr":None,
        "device_writes":0,
    }


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ac-db",default="/opt/solinteg/ev-shadow-runtime/var/ev-ac-estimate.sqlite")
    parser.add_argument("--history-db",default="/opt/solinteg/ev-shadow-runtime/var/ev-history.sqlite")
    parser.add_argument("--hours",type=int,default=24)
    args=parser.parse_args()
    try:
        print(json.dumps(audit(args.ac_db,args.history_db,hours=args.hours),ensure_ascii=False))
    except (QualityError,sessions.SessionEvidenceError,sqlite3.Error,OSError) as exc:
        print(json.dumps({"quality":"unavailable",
                          "reason":str(exc) if isinstance(exc,QualityError) else type(exc).__name__,
                          "device_writes":0}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
