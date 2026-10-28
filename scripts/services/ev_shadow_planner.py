#!/usr/bin/env python3
"""Independent EV economic + solar SHADOW planner; absolutely no device I/O.

This process may read local Solinteg live/price snapshots and a separate
UNVERIFIED Volvo price-scenario file. It has no EVCC, Modbus, Volvo control,
battery dispatch, HA REST or inverter write imports. It NEVER publishes a
control-grade session, and permits exactly 0 A physically.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone

import common
from ev_price_policy import decide_grid_price, read_house_price_plan, read_shadow_ev_session
from ev_solar_policy import decide_solar


def monitor_once(*, now: datetime | None = None, streak: int = 0) -> tuple[dict, int]:
    now = now or datetime.now(timezone.utc)
    session = read_shadow_ev_session(
        os.environ.get("EV_SHADOW_SESSION_PATH",
                       "/opt/solinteg/ev-shadow-session.json"), now=now)
    price_plan = read_house_price_plan(
        os.environ.get("TELEMETRY_DB_PATH", "/opt/solinteg/telemetry.db")
    ) if session else None
    max_current_a = min(6, int(os.environ.get("EV_SHADOW_MAX_CURRENT_A", "6")))
    price = decide_grid_price(
        price_plan, now=now, session=session, max_current_a=max_current_a,
        forecast_risk_ore=float(
            os.environ.get("SOLINTEG_FORECAST_RISK_PREMIUM_ORE", "10")),
    )
    live = common.read_json(os.environ.get("INVERTER_DATA_PATH",
                                           "/opt/solinteg/live.json"))
    params = {
        "live": live,
        "now": now,
        # No EVCC connection: use 0A *in the calculation*, NEVER claim
        # to know actual EV current. Existing EV load may reduce observed
        # export; omitting it is conservative and may understate surplus.
        "ev_measured_a": 0,
        "ev_running": False,
        "phase_guard_verified": False,
        "max_current_a": max_current_a,
        "solar_allowed": price.solar_allowed,
        "target_charge_allowed": (
            session is not None and session["remaining_ac_kwh"] > 0),
    }
    solar_params = {**params, "grid_topup_a": 0}
    potential = decide_solar(**solar_params, stable_surplus_samples=3)
    streak = streak + 1 if potential.suggested_amps >= 6 else 0
    solar = decide_solar(**solar_params, stable_surplus_samples=streak)
    decision = decide_solar(
        **{**params, "grid_topup_a": price.grid_budget_a},
        stable_surplus_samples=streak,
    )
    # Belt and braces: never export a nonzero physical allowance from an
    # observational scenario, even if pure policy changes in future.
    return {
        "timestamp": now.isoformat(),
        "mode": "shadow",
        "source": "ha_price_scenario_no_evcc",
        "charge_mode": session["charge_mode"] if session else None,
        "target_soc_pct": 80 if session else None,
        "remaining_ev_ac_kwh": session["remaining_ac_kwh"] if session else None,
        "departure_at_utc": (
            session["departure_at_utc"].isoformat()
            if session and session["departure_at_utc"] else None
        ),
        "observation_quality": session["observation_quality"] if session else None,
        "energy_estimate_unverified": True if session else None,
        "phase_guard_verified": False,
        "ev_measured_current_available": False,
        "state": "no_evcc_link_read",
        "solar_suggested_a": solar.suggested_amps,
        "solar_permitted_a": 0,
        "charging_suggested_a": decision.suggested_amps,
        "charging_permitted_a": 0,
        "solar_surplus_w": decision.surplus_w,
        "price_buy_ore_kwh": price.buy_ore,
        "price_sell_ore_kwh": price.sell_ore,
        "price_cheap_threshold_ore_kwh": price.cheap_threshold_ore,
        "grid_topup_suggested_a": price.grid_budget_a,
        "grid_topup_planned_kwh": price.planned_grid_kwh,
        "price_reason": price.reason,
        "reason": decision.reason,
        "writes": 0,
    }, streak


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true", help="repeat read-only scenario")
    args = parser.parse_args()
    if os.environ.get("EV_SHADOW_PLANNER_ENABLED") != "1":
        print(json.dumps({
            "mode": "disabled", "reason": "EV_SHADOW_PLANNER_ENABLED != 1",
            "writes": 0,
        }))
        return
    interval = max(30, int(os.environ.get("EV_SHADOW_POLL_SECONDS", "60")))
    path = os.environ.get("EV_SHADOW_PLAN_PATH",
                          "/opt/solinteg/ev-shadow-plan.json")
    streak = 0
    while True:
        try:
            record, streak = monitor_once(streak=streak)
        except (OSError, ValueError, TypeError) as exc:
            streak = 0
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "mode": "shadow", "source": "ha_price_scenario_no_evcc",
                "reason": type(exc).__name__, "solar_suggested_a": 0,
                "solar_permitted_a": 0, "charging_suggested_a": 0,
                "charging_permitted_a": 0, "writes": 0,
            }
        if args.watch:
            common.write_json_atomic(path, record)
        else:
            print(json.dumps(record, ensure_ascii=False))
            return
        time.sleep(interval)


if __name__ == "__main__":
    main()
