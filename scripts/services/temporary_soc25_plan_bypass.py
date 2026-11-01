#!/usr/bin/env python3
"""ONE-OFF battery-save bypass in logged optimizer slots; never writes Modbus.

While Sunwoda SoC >26.5%, temporarily clear only the active plan slot's
batterySaveDriftKwh marker. The unmodified dispatcher interprets that idle
slot as General/self-use, and the house (not deliberate export) uses battery.
A root-only journal restores exact original rows on stop. New optimizer
runs are independently patched at the next 15-second tick. End <=26.5%,
on any failed safety input, or by 2026-11-01 21:45 CET.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

UTC = timezone.utc
STOCKHOLM = ZoneInfo("Europe/Stockholm")
EXPIRES = datetime(2026, 11, 1, 20, 45, tzinfo=UTC)
STATE = Path("/var/lib/solinteg-soc25-plan-bypass/status.json")
ROOT_CODE = Path("/usr/local/libexec/solinteg-soc25-plan-bypass/temporary_soc25_plan_bypass.py")
ROOT_RESERVE = Path("/var/lib/solinteg-soc25-reserve/status.json")
LIVE = Path("/opt/solinteg/live.json")
EVCC = Path("/opt/solinteg/ev-shadow-runtime/var/evcc-readonly.json")
DATABASE = Path("/opt/solinteg/telemetry.db")
TIMER = "solinteg-soc25-plan-bypass.timer"
DEADLINE_TIMER = "solinteg-soc25-plan-bypass-deadline.timer"
STAMP = "%Y-%m-%dT%H:%M:%S"


def utc_now() -> datetime:
    return datetime.now(UTC)


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def stamp(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timezone required")
    return dt.astimezone(UTC)


def sha(data: bytes | str) -> str:
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if type(data) is not dict:
        raise ValueError("JSON root must be object")
    return data


def store(state: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".soc25-bypass-", dir=STATE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, 0o600)
        os.replace(name, STATE)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def safe_now(now: datetime) -> tuple[bool, str, dict]:
    """Fail closed on inactive reserve, unverified A1, PV, CT or a stale sample."""
    if now >= EXPIRES:
        return False, "21:45 scheduled plan resumes", {}
    try:
        r, l, e = read_json(ROOT_RESERVE), read_json(LIVE), read_json(EVCC)
        if (r["state"] != "running" or r["physical_control_authorized"] is not True
                or not 0 <= (now-stamp(r["checked_at_utc"])).total_seconds() <= 45
                or now >= stamp(r["expires_at_utc"])):
            return False, "independent root reserve not active/fresh", {}
        for x in (l,e):
            if not 0 <= (now-stamp(x["timestamp"])).total_seconds() <= 65:
                return False, "stale live/EVCC observation", {}
        # A1 has null current, NOT measured 0A. Never bypass saves with EV
        # plugged in, even if a B2 pause appears to be verified.
        if (e.get("source") != "waveshare_evcc_fc03_readonly"
                or e.get("read_ok") is not True or e.get("state") != "A1"
                or e.get("configured_pwm_raw") != 1000 or e.get("paused_by_pwm") is not True
                or e.get("charging_detected") is not False or e.get("writes") != 0
                or e.get("measured_current_a", "missing") is not None
                or e.get("channel_currents_a") != [None, None, None]):
            return False, "EVCC disconnected A1 no longer verified", {}
        soc = float(l["soc_pct"])
        battery = float(l["battery_w"])
        temp = float(l["battery_temp_c"])
        pv = float(l["pv_w"])
        house = float(l["house_load_w"])
        grid = float(l["grid_w"])
        ac = float(l["inverter_ac_w"])
        phases = [float(l["meter_l"+str(i)+"_w"]) for i in (1,2,3)]
        values = (soc,battery,temp,pv,house,grid,ac,*phases)
        import math
        if any(not math.isfinite(v) for v in values):
            return False, "nonfinite inverter observation", {}
        metrics = {"soc_pct": soc, "battery_w": battery,
                   "grid_w": grid, "pv_w": pv,
                   "max_phase_import_w": round(max(0,*(-p for p in phases)))}
        if soc <= 26.5:
            return False, "26.5% threshold reached; enable normal reserve recovery", metrics
        if not 27 <= soc <= 90 or not 5 <= temp <= 45 or pv > 100:
            return False, "SoC, battery temperature or PV outside bypass scope", metrics
        if (type(l.get("grid_unbalanced_output_raw")) is not int
                or l["grid_unbalanced_output_raw"] != 0
                or house < 0 or pv < 0 or
                abs(sum(phases)-grid) > max(100,abs(grid)*.05)
                or abs(ac-grid-house) > max(150, house*.1)
                or abs(ac-pv-battery) > 500+.1*max(abs(ac),pv,abs(battery))):
            return False, "phase/energy measurements unverified", metrics
        if any(-p >= 3000 for p in phases) or -grid >= 6300:
            return False, "site/phase load above conservative test envelope", metrics
        return True, "verified unplugged evening self-use bypass", metrics
    except (OSError,ValueError,KeyError,TypeError,OverflowError) as exc:
        return False, "unverified safety input: "+type(exc).__name__, {}


def active_slot(slots: list, now: datetime) -> int | None:
    if not isinstance(slots,list) or not slots:
        return None
    try:
        first = datetime.strptime(slots[0]["startTime"], STAMP).replace(
            tzinfo=STOCKHOLM).astimezone(UTC)
    except (KeyError,ValueError,TypeError,AttributeError):
        return None
    idx = int((now-first).total_seconds()//900)
    return idx if 0 <= idx < len(slots) else None


def latest_covering(con: sqlite3.Connection, now: datetime):
    for plan_id, raw in con.execute(
        "SELECT id, dispatch_json FROM optimizer_runs ORDER BY logged_at DESC LIMIT 5"):
        slots=json.loads(raw)
        idx=active_slot(slots,now)
        if idx is not None:
            return plan_id,raw,slots,idx
    return None


def _disable_timer() -> None:
    for name in (TIMER, DEADLINE_TIMER):
        subprocess.run(("/usr/bin/systemctl","disable","--now","--no-block",name),
                       timeout=20,capture_output=True,check=False)


def stop(state: dict, why: str) -> dict:
    """Restore only rows with our exact patched bytes; never stomp new plans."""
    state["state"] = "restoring"
    state["restore_reason"] = why
    store(state)
    count,skipped=0,0
    try:
        with sqlite3.connect(DATABASE,timeout=10) as con:
            con.execute("BEGIN IMMEDIATE")
            for key,entry in state["originals"].items():
                row=con.execute("SELECT dispatch_json FROM optimizer_runs WHERE id=?",
                                (int(key),)).fetchone()
                if row is None or sha(row[0]) != entry["patched_sha256"]:
                    skipped+=1
                    continue
                con.execute("UPDATE optimizer_runs SET dispatch_json=? "
                            "WHERE id=? AND dispatch_json=?",
                            (entry["original"],int(key),row[0]))
                count+=1
        state.update(state="stopped", stopped_at_utc=datetime.now(UTC).isoformat(),
                     reason=why, restored_runs=count, changed_runs_skipped=skipped,
                     physical_override_authorized=False)
        store(state)
        _disable_timer()
        return {"state":"stopped","reason":why,"restored_runs":count,
                "changed_runs_skipped":skipped}
    except (sqlite3.Error,OSError) as exc:
        state["state"]="restore_blocked"
        state["reason"]=type(exc).__name__
        store(state)
        raise


def tick(*, force_stop: bool=False) -> dict:
    if not STATE.exists():
        raise RuntimeError("one-off plan bypass has no root status")
    state=read_json(STATE)
    if state.get("schema_version") != 1 or state.get("test_id") != "soc25-bypass-20261101":
        raise RuntimeError("unexpected status file")
    if state["state"] == "stopped":
        return {"state":"stopped","reason":state.get("reason")}
    if state["state"] in ("restoring","restore_blocked"):
        return stop(state,state.get("restore_reason","retry interrupted rollback"))
    if state["state"] not in ("prepared","running"):
        raise RuntimeError("invalid lifecycle")
    if force_stop:
        return stop(state,"operator requested stop")
    now=utc_now()
    if not ROOT_CODE.exists() or sha(ROOT_CODE.read_bytes()) != state.get("code_sha256"):
        return stop(state,"root-owned one-off source changed")
    if boot_id() != state.get("boot_id"):
        return stop(state,"cannot continue after reboot")
    ok,reason,metrics=safe_now(now)
    if not ok:
        return stop(state,reason)
    try:
        with sqlite3.connect(DATABASE,timeout=10) as con:
            con.execute("BEGIN IMMEDIATE")
            plan=latest_covering(con,now)
            if plan is None:
                raise ValueError("no current optimizer plan")
            plan_id,raw,slots,idx=plan
            slot=slots[idx]
            marker=slot.get("batterySaveDriftKwh")
            if slot.get("action") != "idle" or marker is None or float(marker) <= 0:
                # The planner already wants ordinary discharge/Auto or charging.
                state.update(state="running",checked_at_utc=now.isoformat(),
                             measurements=metrics,last_reason="no save marker in current plan")
                store(state)
                return {"state":"running","modified":False,"reason":"already not saving"}
            key=str(plan_id)
            if key in state["originals"]:
                # For this same current slot, the patched plan must stay intact.
                if sha(raw) != state["originals"][key]["patched_sha256"]:
                    raise ValueError("previously patched plan changed unexpectedly")
                state.update(state="running",checked_at_utc=now.isoformat(),
                             measurements=metrics,last_reason="existing active slot already patched")
                store(state)
                return {"state":"running","modified":False,"reason":"already patched"}
            updated=json.loads(raw)
            updated[idx].pop("batterySaveDriftKwh")
            patched=json.dumps(updated,ensure_ascii=False,separators=(",",":"))
            # Journal FIRST. If a crash occurs after SQLite commit, the
            # timer can restore from this root-owned original snapshot.
            state["originals"][key]={"original":raw,"patched_sha256":sha(patched),
                                     "slot_index":idx,"slot_start":slot["startTime"]}
            state.update(state="running",checked_at_utc=now.isoformat(),
                         measurements=metrics,last_reason="battery-save marker bypassed",
                         patched_run_id=plan_id)
            store(state)
            con.execute("UPDATE optimizer_runs SET dispatch_json=? "
                        "WHERE id=? AND dispatch_json=?",(patched,plan_id,raw))
            if con.total_changes != 1:
                raise RuntimeError("optimizer plan update did not match exact original")
        return {"state":"running","modified":True,"run_id":plan_id,"slot":slot["startTime"],
                "soc_pct":metrics["soc_pct"],"device_writes":0}
    except (sqlite3.Error,OSError,ValueError,KeyError,TypeError,RuntimeError) as exc:
        return stop(state,"plan bypass failed: "+type(exc).__name__)


def activate() -> dict:
    if os.geteuid() != 0:
        raise PermissionError("root required")
    if STATE.exists() or STATE.is_symlink():
        raise RuntimeError("one-off bypass already exists; never reactivate")
    now=utc_now()
    ok,reason,metrics=safe_now(now)
    if not ok:
        return {"state":"blocked","reason":reason,"device_writes":0}
    if not ROOT_CODE.is_file():
        raise RuntimeError("root-owned code not installed")
    import subprocess
    if subprocess.run(["systemctl","is-active","--quiet","solinteg-dispatch.service"],
                      timeout=8).returncode != 0:
        return {"state":"blocked","reason":"original dispatcher not active","device_writes":0}
    with sqlite3.connect("file:"+str(DATABASE)+"?mode=ro",uri=True) as con:
        p=latest_covering(con,now)
        if p is None or p[2][p[3]].get("action") != "idle" or (
                float(p[2][p[3]].get("batterySaveDriftKwh") or 0) <= 0):
            return {"state":"blocked","reason":"current plan is not saving battery",
                    "device_writes":0}
    STATE.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    info=STATE.parent.stat()
    if info.st_uid != 0 or info.st_mode & 0o077:
        raise RuntimeError("root-only status directory not secure")
    store({"schema_version":1,"test_id":"soc25-bypass-20261101",
           "state":"prepared","started_at_utc":now.isoformat(),
           "expires_at_utc":EXPIRES.isoformat(),
           "code_sha256":sha(ROOT_CODE.read_bytes()),
           "boot_id":boot_id(),
           "originals":{},"physical_override_authorized":True,
           "measurements":metrics})
    return {"state":"prepared","expires_at_utc":EXPIRES.isoformat(),
            "device_writes":0}


def main() -> None:
    p=argparse.ArgumentParser(description=__doc__)
    x=p.add_mutually_exclusive_group(required=True)
    x.add_argument("--check",action="store_true")
    x.add_argument("--start",action="store_true")
    x.add_argument("--tick",action="store_true")
    x.add_argument("--stop",action="store_true")
    x.add_argument("--status",action="store_true")
    p.add_argument("--confirm",action="store_true")
    a=p.parse_args()
    if a.start and not a.confirm:
        p.error("--start requires --confirm")
    if a.check:
        ok,reason,metrics=safe_now(datetime.now(UTC))
        out={"eligible":ok,"reason":reason,"measurements":metrics,"device_writes":0}
    elif a.start:
        out=activate()
    elif a.status:
        status=read_json(STATE) if STATE.exists() else {"state":"not_installed"}
        out={k:status.get(k) for k in ("state","started_at_utc","expires_at_utc",
               "checked_at_utc","last_reason","reason","patched_run_id",
               "restored_runs","changed_runs_skipped","measurements")}
        out["patched_runs_count"]=len(status.get("originals",{}))
    else:
        out=tick(force_stop=a.stop)
    print(json.dumps(out,ensure_ascii=False,sort_keys=True))
    if out.get("state")=="blocked":
        raise SystemExit(2)


if __name__=="__main__":
    main()
