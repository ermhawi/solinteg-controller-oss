#!/usr/bin/env python3
"""Single EVCC price executor; disabled until a calibrated EV session exists.

The house optimizer remains independent. EV power is measured by EVCC Ict2
and never deducted from the physical CT fuse limits. There is one writer for
the EVCC register, serialized against the existing FC03 observer lock.
Missing/old session, ambiguous price, hardware state or preflight -> NO WRITE.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ev_price_policy import decide_grid_price, read_ev_session, read_house_price_plan, PriceDecision
from evcc_ascii import EvccAscii, RAW_PAUSE, EvccError, EvccUncertainWrite

EVCC = Path("/opt/solinteg/ev-shadow-runtime/var/evcc-readonly.json")
LIVE = Path("/opt/solinteg/live.json")
LOCK = Path("/opt/solinteg/ev-shadow-runtime/var/evcc-reader.lock")
MAX_SAMPLE_AGE_S = 65
MAX_PHASE_IMPORT_W = 3000
MAX_SITE_IMPORT_W = 6000
EV_RAW_6A = 100
EV_W_6A = 1380


@dataclass(frozen=True)
class EvPriceAction:
    action: str  # pause, resume, no_change or inhibited
    expected_raw: int | None
    target_raw: int | None
    reason: str


def choose_action(*, session: dict | None, price: PriceDecision,
                  evcc: dict | None, live: dict | None,
                  now: datetime, price_pause_owned: bool = False) -> EvPriceAction:
    """Pure command selection: requires verified session and valid snapshots."""
    def no(reason: str) -> EvPriceAction:
        return EvPriceAction("inhibited", None, None, reason)

    if session is None or now.tzinfo is None:
        return no("missing calibrated vehicle energy session")
    if not isinstance(evcc, dict) or not isinstance(live, dict):
        return no("missing EVCC or grid observation")
    if session.get("remaining_ac_kwh") is None:
        return no("missing verified EV energy requirement")
    if price.charge_mode != session.get("charge_mode"):
        return no("price mode and vehicle session disagree")
    try:
        stamp = datetime.fromisoformat(str(evcc["timestamp"]).replace("Z", "+00:00"))
        live_at = datetime.fromisoformat(str(live["timestamp"]).replace("Z", "+00:00"))
        if stamp.tzinfo is None or live_at.tzinfo is None:
            raise ValueError("naive timestamp")
        if not (0 <= (now - stamp).total_seconds() <= MAX_SAMPLE_AGE_S
                and 0 <= (now - live_at).total_seconds() <= MAX_SAMPLE_AGE_S):
            return no("stale EVCC or grid snapshot")
        if (evcc.get("source") != "waveshare_evcc_fc03_readonly"
                or evcc.get("read_ok") is not True
                or evcc.get("writes") != 0
                or evcc.get("physical_control_eligible") is not False
                or evcc.get("state") not in ("B2", "C2")):
            return no("EVCC state or independent FC03 source unverified")
        raw = evcc["configured_pwm_raw"]
        if type(raw) is not int or raw != evcc.get("pilot_pwm_raw") or raw not in (RAW_PAUSE, EV_RAW_6A):
            return no("EVCC pilot changed externally; never overwrite")
        currents = evcc["channel_currents_a"]
        if (not isinstance(currents, list) or len(currents) != 3
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in currents)
                or not (0 <= currents[0] <= .3 and 0 <= currents[2] <= .3)
                or not 0 <= currents[1] <= 6.5):
            return no("EVCC phase/current mismatch")
        if (live.get("grid_unbalanced_output_raw") != 0
                or any(type(live.get(k)) not in (int, float) or not math.isfinite(live[k])
                       for k in ("grid_w", "meter_l3_w"))):
            return no("missing valid phase import measurements")
        if session["remaining_ac_kwh"] <= 0:
            return (EvPriceAction("no_change", raw, raw, "target already reached")
                    if raw == RAW_PAUSE else
                    EvPriceAction("pause", raw, RAW_PAUSE, "verified 80 percent target reached"))
        if price.buy_ore is None:
            return no("no confirmed current price decision")
        if price.grid_budget_a not in (0, 6):
            return no("unsupported EVCC pilot limit")
        if price.grid_budget_a == 0:
            if raw == RAW_PAUSE:
                return EvPriceAction("no_change", raw, raw, "already paused for expensive slot")
            return EvPriceAction("pause", raw, RAW_PAUSE, "confirmed expensive slot")
        if raw == EV_RAW_6A:
            return EvPriceAction("no_change", raw, raw, "already charging in low-price slot")
        # A manual EVCC pause must not be undone by the price timer. Only
        # a persisted, independently verified pause previously made by this
        # controller may ever be resumed automatically. That persistent owner
        # protocol is intentionally not enabled in the current release.
        if price_pause_owned is not True:
            return no("pause owner unknown; manual pause has priority")
        # A conservative grid-import start preflight; the existing electrical
        # breaker is separate hardware protection. Never use CT to infer EV SoC.
        projected_l3 = max(0, -live["meter_l3_w"]) + EV_W_6A
        projected_site = max(0, -live["grid_w"]) + EV_W_6A
        if projected_l3 > MAX_PHASE_IMPORT_W or projected_site > MAX_SITE_IMPORT_W:
            return no("projected L3 or total grid import exceeds soft budget")
        if currents[1] > .5:
            return no("EVCC is reportedly paused but substantial current is flowing")
        return EvPriceAction("resume", raw, EV_RAW_6A, "confirmed cheap slot, 6 A only")
    except (KeyError, TypeError, ValueError, OverflowError):
        return no("invalid EVCC or grid snapshot")


def run_once(*, now: datetime | None = None) -> dict:
    """One decision, at most one compare-and-write FC10; never retry errors."""
    now = now or datetime.now(timezone.utc)
    if (os.environ.get("EV_PRICE_CONTROL_ENABLED") != "1"
            or os.environ.get("EVCC_PRICE_CONTROL_ARMED") != "1"):
        return {"state": "disabled", "writes": 0, "reason": "price controller not armed"}
    session = read_ev_session(os.environ.get("EV_SESSION_PATH"), now=now)
    if session is None:
        return {"state": "inhibited", "writes": 0, "reason": "no calibrated fresh EV session"}
    price = decide_grid_price(
        read_house_price_plan(os.environ.get("TELEMETRY_DB_PATH", "/opt/solinteg/telemetry.db")),
        now=now, session=session, max_current_a=6,
        forecast_risk_ore=float(os.environ.get("SOLINTEG_FORECAST_RISK_PREMIUM_ORE", "10")))
    try:
        evcc = json.loads(Path(os.environ.get("EVCC_READONLY_PATH", str(EVCC))).read_text())
        live = json.loads(Path(os.environ.get("INVERTER_DATA_PATH", str(LIVE))).read_text())
    except (OSError, ValueError, TypeError):
        return {"state": "inhibited", "writes": 0, "reason": "unreadable input snapshot"}
    command = choose_action(session=session, price=price, evcc=evcc, live=live, now=now)
    if command.action not in ("pause", "resume"):
        return {"state": command.action, "writes": 0, "reason": command.reason}
    host = os.environ.get("EVCC_HOST")
    if not host:
        return {"state": "inhibited", "writes": 0, "reason": "EVCC address not configured"}
    # LOCK_EX|LOCK_NB is the same serial-owner contract as the FC03 observer.
    with Path(os.environ.get("EVCC_READER_LOCK", str(LOCK))).open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "inhibited", "writes": 0, "reason": "EVCC observation owns serial lock"}
        try:
            client = EvccAscii(host, port=int(os.environ.get("EVCC_PORT", "4196")),
                               scheduled_control=True)
            # A second FC03 check happens inside set_limit_raw and aborts if the
            # command raced with another writer, pilot or EVCC fault.
            after = client.set_limit_raw(command.target_raw,
                                         expected_raw=command.expected_raw, scheduled=True)
            return {"state": command.action, "writes": 1,
                    "configured_raw": after.configured_raw, "reason": command.reason}
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", required=True)
    parser.parse_args()
    try:
        result = run_once()
    except (EvccError, EvccUncertainWrite, OSError, ValueError, PermissionError) as exc:
        # No blind retry and no claims about whether the write took effect.
        result = {"state": "error", "writes": None, "reason": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
