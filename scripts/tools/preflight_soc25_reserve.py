#!/usr/bin/env python3
"""Read-only, fail-closed admission for the current-boot SoC25 reserve guard.

No device connection or write. The live dispatcher keeps its original source;
the root-owned process-local wrapper adjusts only reserve recovery logic.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
from soc25_reserve_policy import verified_evcc_safe

DISPATCH = Path("/opt/solinteg/app/scripts/services/dispatch_loop.py")
ORIGINAL_SHA = "a2f3aba4be8d64771ed2b20c71c9ceed5c3ce25ff99cad246bc5529afd65a8b7"
LIVE = Path("/opt/solinteg/live.json")
EVCC = Path("/opt/solinteg/ev-shadow-runtime/var/evcc-readonly.json")
DB = Path("/opt/solinteg/telemetry.db")
GATE = Path("/var/lib/solinteg-ev-grid-priority/gate.json")
DROPINS = Path("/etc/systemd/system/solinteg-dispatch.service.d")
REQUIRED_UNIT_NAMES = ("solinteg-dispatch.service", "solinteg-poller.service",
                       "solinteg-watchdog.timer", "solinteg-ev-observer.service",
                       "solinteg-ev-grid-priority-watchdog.timer")


def _stamp(value: str) -> datetime:
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError("missing timezone")
    return t.astimezone(timezone.utc)


def _finite(d: dict, key: str) -> float:
    x = d[key]
    if type(x) not in (float, int) or not math.isfinite(x):
        raise ValueError(f"invalid {key}")
    return float(x)


def admit(live: dict, evcc: dict, history: list[dict], *, now: datetime,
          installed_sha_ok: bool, units_ok: bool, no_conflicts: bool) -> dict:
    failures = []
    measurements = {}
    if not (installed_sha_ok and units_ok and no_conflicts):
        failures.append("original dispatcher / safety units / independent overlays not verified")
    if now.tzinfo is None:
        return {"eligible": False, "reasons": ["clock timezone missing"], "device_writes": 0}
    try:
        for obj, name in ((live, "inverter"), (evcc, "EVCC")):
            if not 0 <= (now - _stamp(obj["timestamp"])).total_seconds() <= 65:
                failures.append(f"{name} observation not fresh")
        soc = _finite(live, "soc_pct")
        if not 24 <= soc <= 90:
            failures.append("SoC outside 24-90% deployment window")
        measurements["soc_pct"] = soc
        if not 5 <= _finite(live, "battery_temp_c") <= 45:
            failures.append("temperature outside supervised window")
        if (type(live.get("grid_unbalanced_output_raw")) is not int
                or live["grid_unbalanced_output_raw"] != 0):
            failures.append("phase-balanced inverter register 50006 unverified")
        grid = _finite(live, "grid_w")
        phases = [_finite(live, f"meter_l{i}_w") for i in (1, 2, 3)]
        ac = _finite(live, "inverter_ac_w")
        house = _finite(live, "house_load_w")
        pv = _finite(live, "pv_w")
        battery = _finite(live, "battery_w")
        if (house < 0 or pv < 0 or abs(sum(phases)-grid) > max(100,abs(grid)*.05)
                or abs(ac-grid-house) > max(150,house*.1)
                or abs(ac-pv-battery) > 500 + .1*max(abs(ac),pv,abs(battery))):
            failures.append("inverter phase/energy measurements inconsistent")
        extra = 1500 + max(0, battery)  # worst case: discharge -> planned charge
        predicted = [max(0, -p) + extra/3 for p in phases]
        measurements["max_predicted_phase_import_w"] = round(max(predicted))
        measurements["predicted_site_import_w"] = round(max(0, -grid) + extra)
        measurements["solar_surplus_w"] = round(max(0, pv - house))
        if any(p >= 3000 for p in predicted) or max(0, -grid) + extra >= 6300:
            failures.append("predicted 1500W recovery exceeds conservative grid admission")
        evcc_mode = verified_evcc_safe(evcc)
        measurements["evcc_no_charge_mode"] = evcc_mode
        if evcc_mode is None:
            failures.append("EVCC neither unplugged A1 nor independently paused B2")
    except (ValueError, KeyError, AttributeError, TypeError, OverflowError) as exc:
        failures.append("live input invalid: "+str(exc)[:90])

    try:
        begin = now - timedelta(minutes=5)
        rows = [r for r in history if begin <= _stamp(r["timestamp"]) <= now]
        rows.sort(key=lambda r: _stamp(r["timestamp"]))
        measurements["history_samples_5m"] = len(rows)
        if (len(rows) < 8
                or (_stamp(rows[-1]["timestamp"]) - _stamp(rows[0]["timestamp"])).total_seconds() < 240
                or (now-_stamp(rows[-1]["timestamp"])).total_seconds() > 65):
            failures.append("five minutes of CT history incomplete")
        for previous, current in zip(rows, rows[1:]):
            if (_stamp(current["timestamp"]) - _stamp(previous["timestamp"])).total_seconds() > 65:
                failures.append("CT history gap >65s")
                break
        if rows:
            min_soc = min(_finite(r, "soc_pct") for r in rows)
            phase_peak = max(-_finite(r, f"meter_l{i}_w") for r in rows for i in (1, 2, 3))
            site_peak = max(-_finite(r, "grid_w") for r in rows)
            measurements.update(history_min_soc_pct=min_soc,
                                history_phase_import_max_w=round(phase_peak),
                                history_site_import_max_w=round(site_peak))
            if min_soc < 24 or phase_peak >= 3000 or site_peak >= 6300:
                failures.append("recent SoC/phase/site history outside commissioning envelope")
    except (ValueError, KeyError, AttributeError, TypeError, OverflowError, IndexError) as exc:
        failures.append("CT history invalid: "+str(exc)[:90])
    return {"eligible": not failures, "reasons": list(dict.fromkeys(failures)),
            "measurements": measurements, "physical_control_authorized": False,
            "device_writes": 0}


def _active(unit: str) -> bool:
    return subprocess.run(["/usr/bin/systemctl", "is-active", "--quiet", unit],
                          timeout=5, check=False).returncode == 0


def report() -> dict:
    now = datetime.now(timezone.utc)
    with sqlite3.connect(DB.resolve().as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            "SELECT timestamp,soc_pct,grid_w,meter_l1_w,meter_l2_w,meter_l3_w "
            "FROM readings WHERE timestamp >= ? ORDER BY timestamp",
            ((now-timedelta(minutes=6)).isoformat(),))]
    dropin_names = ("45-temp-soc25-pilot.conf",
                    "46-temp-soc25-adaptive-pilot.conf",
                    "47-temp-soc25-evpaused-pilot.conf",
                    "48-soc25-reserve.conf")
    no_conflicts = not (GATE.exists() or GATE.is_symlink()
                        or any((DROPINS/n).exists() or (DROPINS/n).is_symlink()
                               for n in dropin_names))
    return admit(json.loads(LIVE.read_text()),json.loads(EVCC.read_text()), rows,
                 now=now, installed_sha_ok=(hashlib.sha256(DISPATCH.read_bytes()).hexdigest() == ORIGINAL_SHA),
                 units_ok=all(_active(u) for u in REQUIRED_UNIT_NAMES),
                 no_conflicts=no_conflicts)


def main() -> None:
    try:
        print(json.dumps(report(), indent=2, ensure_ascii=False))
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        print(json.dumps({"eligible": False, "reasons": [type(exc).__name__],
                          "device_writes": 0, "physical_control_authorized": False}))


if __name__ == "__main__":
    main()
