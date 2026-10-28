#!/usr/bin/env python3
"""Read-only EVCC solar monitor and explicitly gated single-action field probe.

EV_ENABLED=0: no network connection. --watch does not call write methods;
EVCC_CONTROL_ARMED remains 0 by default. No contactor control or inverter
writes. Physical commands are deliberately separate CLI actions.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone

import common
from ev_solar_policy import decide_solar
from ev_price_policy import decide_grid_price, read_ev_session, read_house_price_plan
from evcc_ascii import EvccAscii, EvccError, RAW_PAUSE, amps_to_raw

log = logging.getLogger("solinteg.evcc")


def monitor_once(client: EvccAscii, streak: int) -> tuple[dict, int]:
    now = datetime.now(timezone.utc)
    status = client.status()
    live = common.read_json(os.environ.get("INVERTER_DATA_PATH", "/opt/solinteg/live.json"))
    running = status.measured_current_a is not None and status.measured_current_a > 1
    max_current_a = int(os.environ.get("EVCC_MAX_CURRENT_A", "6"))
    session = read_ev_session(os.environ.get("EV_SESSION_PATH"), now=now)
    price = decide_grid_price(
        read_house_price_plan(os.environ.get("TELEMETRY_DB_PATH", "/opt/solinteg/telemetry.db"))
        if session else None, now=now, session=session, max_current_a=max_current_a,
        forecast_risk_ore=float(os.environ.get("SOLINTEG_FORECAST_RISK_PREMIUM_ORE", "10")),
    )
    params = dict(live=live, now=now, ev_measured_a=status.measured_current_a,
                  ev_running=running, phase_guard_verified=False,
                  max_current_a=max_current_a, grid_topup_a=price.grid_budget_a)
    solar_params = {**params, "grid_topup_a": 0}
    solar_ready = decide_solar(**solar_params, stable_surplus_samples=3)
    streak = streak + 1 if solar_ready.suggested_amps >= 6 else 0
    solar_only = decide_solar(**solar_params, stable_surplus_samples=streak)
    decision = decide_solar(**params, stable_surplus_samples=streak)
    return {
        "timestamp": now.isoformat(),
        "mode": "shadow",
        "state": status.state,
        "configured_pwm_raw": status.configured_raw,
        "pilot_pwm_raw": status.pilot_raw,
        "measured_current_a": status.measured_current_a,
        "solar_suggested_a": solar_only.suggested_amps,
        "solar_permitted_a": solar_only.permitted_amps,
        "charging_suggested_a": decision.suggested_amps,
        "charging_permitted_a": decision.permitted_amps,
        "solar_surplus_w": decision.surplus_w,
        "price_buy_ore_kwh": price.buy_ore,
        "price_sell_ore_kwh": price.sell_ore,
        "grid_topup_suggested_a": price.grid_budget_a,
        "grid_topup_planned_kwh": price.planned_grid_kwh,
        "price_reason": price.reason,
        "reason": decision.reason,
        "writes": 0,
    }, streak


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true", help="read-only polling + shadow JSON")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--pause", action="store_true", help="manual FC10 pause field test")
    action.add_argument("--resume-amps", type=float, help="manual FC10 current field test")
    parser.add_argument("--expected-raw", type=lambda s: int(s, 0),
                        help="mandatory known pre-test limit, e.g. 0x010B")
    parser.add_argument("--confirm", action="store_true",
                        help="operator supervised with verified phase/load and rollback")
    args = parser.parse_args()

    if os.environ.get("EV_ENABLED") != "1":
        print(json.dumps({"mode": "disabled", "reason": "EV_ENABLED != 1", "writes": 0}))
        return

    host = os.environ.get("EVCC_HOST", "")
    client = EvccAscii(host, port=int(os.environ.get("EVCC_PORT", "4196")),
                       manual_test=args.confirm)
    if args.pause or args.resume_amps is not None:
        if not args.confirm or args.expected_raw is None:
            parser.error("manual physical action requires --confirm and --expected-raw")
        raw = RAW_PAUSE if args.pause else amps_to_raw(args.resume_amps)
        after = client.set_limit_raw(raw, expected_raw=args.expected_raw)
        print(json.dumps({"mode": "manual_test", "requested_raw": raw,
                          "configured_raw": after.configured_raw,
                          "pilot_raw": after.pilot_raw,
                          "state": after.state, "requires_physical_check": True}))
        return

    output = os.environ.get("EVCC_SHADOW_PATH", "/opt/solinteg/evcc-shadow.json")
    interval = max(10, int(os.environ.get("EVCC_POLL_SECONDS", "30")))
    streak = 0
    while True:
        try:
            record, streak = monitor_once(client, streak)
        except (OSError, EvccError, ValueError) as exc:
            streak = 0
            record = {"timestamp": datetime.now(timezone.utc).isoformat(),
                      "mode": "shadow", "state": "unknown", "reason": type(exc).__name__,
                      "solar_suggested_a": 0, "solar_permitted_a": 0, "writes": 0}
        if args.watch:
            common.write_json_atomic(output, record)
        else:
            print(json.dumps(record, ensure_ascii=False))
            return
        time.sleep(interval)


if __name__ == "__main__":
    main()
