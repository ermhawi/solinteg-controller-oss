#!/usr/bin/env python3
"""Strict read-only admission checks for a separately watchdogged EV-paused SoC trial.

Never starts services, writes inverter/EVCC registers or changes deployment state.
Historical 30-second CT values are a planning envelope, not RMS fuse protection.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

LIVE = Path("/opt/solinteg/live.json")
EVCC = Path("/opt/solinteg/ev-shadow-runtime/var/evcc-readonly.json")
DB = Path("/opt/solinteg/telemetry.db")
DISPATCH = Path("/opt/solinteg/app/scripts/services/dispatch_loop.py")
ROOT_GATE = Path("/var/lib/solinteg-ev-grid-priority/gate.json")
DROPIN_DIR = Path("/etc/systemd/system/solinteg-dispatch.service.d")
ORIGINAL_DISPATCH_SHA256 = "a2f3aba4be8d64771ed2b20c71c9ceed5c3ce25ff99cad246bc5529afd65a8b7"
TRIAL_MAX_W = 1500
MIN_SOC_PCT = 19.0
MAX_START_SOC_PCT = 24.9
MAX_PHASE_START_W = 3000
MAX_SITE_START_W = 6300
HISTORY_WINDOW_S = 300
MAX_SNAPSHOT_AGE_S = 65
MAX_HISTORY_GAP_S = 65
HISTORY_COLUMNS = ("timestamp", "soc_pct", "battery_w", "grid_w",
                   "meter_l1_w", "meter_l2_w", "meter_l3_w")


def _numeric(record: dict, name: str) -> float:
    x = record[name]
    if type(x) not in (float, int) or not math.isfinite(x):
        raise ValueError(f"invalid {name}")
    return float(x)


def _time(value: str) -> datetime:
    x = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if x.tzinfo is None:
        raise ValueError("timezone missing")
    return x.astimezone(timezone.utc)


def admit(live: dict, evcc: dict, history: list[dict], *, now: datetime,
          runtime_ok: bool, gate_absent: bool, dispatcher_sha_ok: bool,
          dropins_absent: bool) -> dict:
    """Fail closed. Successful result only permits a supervised, *separate* start."""
    errors = []
    readings = {}
    if not (runtime_ok and gate_absent and dispatcher_sha_ok and dropins_absent):
        errors.append("armed runtime/root gate/dispatcher or pre-existing drop-in not verified")
    if now.tzinfo is None:
        return {"eligible": False, "reasons": ["timezone missing"], "device_writes": 0}
    now = now.astimezone(timezone.utc)
    try:
        for data, name in ((live, "inverter"), (evcc, "EVCC")):
            age = (now - _time(data["timestamp"])).total_seconds()
            if not 0 <= age <= MAX_SNAPSHOT_AGE_S:
                errors.append(f"{name} snapshot stale")
        if not (evcc.get("source") == "waveshare_evcc_fc03_readonly"
                and evcc.get("read_ok") is True and evcc.get("state") == "B2"
                and evcc.get("configured_pwm_raw") == 1000
                and evcc.get("charging_detected") is False
                and evcc.get("paused_by_pwm") is True
                and evcc.get("writes") == 0
                and 0 <= _numeric(evcc, "measured_current_a") <= 1):
            errors.append("EVCC paused state not independently verified")
        soc = _numeric(live, "soc_pct")
        readings["soc_pct"] = soc
        if not MIN_SOC_PCT <= soc <= MAX_START_SOC_PCT:
            errors.append("house SoC outside 19%-24.9% start window")
        if not 5 <= _numeric(live, "battery_temp_c") <= 45:
            errors.append("battery temperature outside 5-45 C")
        if type(live.get("grid_unbalanced_output_raw")) is not int or live["grid_unbalanced_output_raw"] != 0:
            errors.append("balanced inverter output register 50006 not verified")
        pv = _numeric(live, "pv_w")
        ac = _numeric(live, "inverter_ac_w")
        house = _numeric(live, "house_load_w")
        battery = _numeric(live, "battery_w")
        grid = _numeric(live, "grid_w")
        phase = [_numeric(live, f"meter_l{i}_w") for i in (1, 2, 3)]
        if (pv < 0 or house < 0
                or abs(sum(phase) - grid) > max(100, abs(grid) * .05)
                or abs(ac - grid - house) > max(150, house * .1)
                or abs(ac - pv - battery) > 500 + .1 * max(abs(ac), pv, abs(battery))):
            errors.append("inconsistent inverter/CT/PV/battery energy balance")
        readings["existing_battery_charge_w"] = round(max(0, -battery))
        # A pre-existing optimizer charge may exceed the 1500W pilot clamp.
        # The new dispatcher must REDUCE it within the watchdog settle window.
        if not -3000 <= battery <= -100:
            errors.append("existing battery charging outside 100-3000W admission envelope")
        # Predict maximum additional 1.5kW charge, using balanced 50006=0;
        # guard normally derates earlier, so this is a conservative *admission*
        # check, never a substitute for the independent live watchdog/fuses.
        extra = max(0, TRIAL_MAX_W + battery)
        projected_phases = [max(0, -p) + extra / 3 for p in phase]
        projected_total = max(0, -grid) + extra
        readings["max_projected_phase_import_w"] = round(max(projected_phases))
        readings["projected_total_grid_import_w"] = round(projected_total)
        if any(p >= MAX_PHASE_START_W for p in projected_phases):
            errors.append("projected 1.5kW start exceeds 3kW phase admission budget")
        if projected_total >= MAX_SITE_START_W:
            errors.append("projected start exceeds 6.3kW site admission budget")
    except (KeyError, AttributeError, TypeError, ValueError, OverflowError) as exc:
        errors.append("missing or invalid live evidence: " + str(exc)[:100])

    try:
        window_start = now - timedelta(seconds=HISTORY_WINDOW_S)
        previous = None
        selected = [h for h in history if window_start <= _time(h["timestamp"]) <= now]
        selected.sort(key=lambda row: _time(row["timestamp"]))
        readings["history_samples_5m"] = len(selected)
        if (len(selected) < 8
                or (now - _time(selected[-1]["timestamp"])).total_seconds() > 65
                or (_time(selected[-1]["timestamp"]) - _time(selected[0]["timestamp"])).total_seconds() < 240):
            errors.append("five minutes of continuous CT history unavailable")
        for h in selected:
            stamp = _time(h["timestamp"])
            if previous and (stamp - previous).total_seconds() > MAX_HISTORY_GAP_S:
                errors.append("CT sample gap over 65 seconds")
                break
            previous = stamp
            if (abs(sum(_numeric(h, f"meter_l{i}_w") for i in (1, 2, 3))
                    - _numeric(h, "grid_w")) > max(100, abs(_numeric(h, "grid_w")) * .05)):
                errors.append("invalid recent phase CT balance")
                break
            if _numeric(h, "soc_pct") < MIN_SOC_PCT:
                errors.append("house SoC below 19% in recent history")
                break
        readings["max_historical_phase_import_w"] = round(
            max((-_numeric(h, f"meter_l{i}_w") for h in selected for i in (1, 2, 3)), default=0))
        readings["max_historical_site_import_w"] = round(
            max((-_numeric(h, "grid_w") for h in selected), default=0))
        if readings["max_historical_phase_import_w"] >= MAX_PHASE_START_W:
            errors.append("recent phase import exceeded 3kW admission budget")
        if readings["max_historical_site_import_w"] >= MAX_SITE_START_W:
            errors.append("recent grid import exceeded 6.3kW admission budget")
    except (ValueError, TypeError, KeyError, OverflowError, IndexError) as exc:
        errors.append("invalid recent CT history: " + str(exc)[:100])
    return {"eligible": not errors, "reasons": list(dict.fromkeys(errors)),
            "measurements": readings, "device_writes": 0,
            "physical_control_authorized": False}


def _unit_active(name: str) -> bool:
    return subprocess.run(["/usr/bin/systemctl", "is-active", "--quiet", name],
                          timeout=5, check=False).returncode == 0


def runtime_ok() -> bool:
    names = ("solinteg-dispatch.service", "solinteg-poller.service",
             "solinteg-watchdog.timer", "solinteg-ev-observer.service",
             "solinteg-ev-grid-priority-watchdog.timer")
    return all(_unit_active(x) for x in names) and all(
        not _unit_active(x) for x in ("solinteg-temp-soc25-watchdog.timer",
                                      "solinteg-temp-soc25-adaptive-watchdog.timer",
                                      "solinteg-temp-soc25-evpaused-watchdog.timer"))


def report() -> dict:
    now = datetime.now(timezone.utc)
    with sqlite3.connect(DB.resolve().as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        limit = (now - timedelta(minutes=6)).isoformat()
        rows = [dict(row) for row in con.execute(
            "SELECT " + ",".join(HISTORY_COLUMNS) +
            " FROM readings WHERE timestamp >= ? ORDER BY timestamp", (limit,))]
    with LIVE.open() as f:
        live = json.load(f)
    with EVCC.open() as f:
        evcc = json.load(f)
    result = admit(live, evcc, rows, now=now,
                   runtime_ok=runtime_ok(),
                   gate_absent=not (ROOT_GATE.exists() or ROOT_GATE.is_symlink()),
                   dispatcher_sha_ok=(hashlib.sha256(DISPATCH.read_bytes()).hexdigest()
                                       == ORIGINAL_DISPATCH_SHA256),
                   dropins_absent=not any((DROPIN_DIR / n).exists() or
                                          (DROPIN_DIR / n).is_symlink() for n in (
                       "45-temp-soc25-pilot.conf", "46-temp-soc25-adaptive-pilot.conf",
                       "47-temp-soc25-evpaused-pilot.conf")))
    return result


def main() -> None:
    try:
        print(json.dumps(report(), indent=2, ensure_ascii=False))
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        print(json.dumps({"eligible": False, "reasons": ["preflight input unavailable: " +
                         type(exc).__name__], "device_writes": 0}))


if __name__ == "__main__":
    main()
