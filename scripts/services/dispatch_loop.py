#!/usr/bin/env python3
"""
Dispatch loop: applies the web app's DP battery plan to the real inverter.

Reads the newest optimizer_runs row whose slots COVER the current instant (dispatch_json:
DispatchSlot[] — the same plan shown on the dashboard) from telemetry.db, finds the entry for the
current 15-min Stockholm slot, and drives the inverter to match it via inverter_control.py's
force_charge/force_discharge/return_to_auto. Runs continuously, re-checking every
DISPATCH_LOOP_INTERVAL_S and re-asserting an unchanged CHARGE/DISCHARGE target at least
every DISPATCH_REASSERT_S (cheap insurance — see MODBUS.md's setpoint-persistence probe,
which confirmed a written setpoint holds for a full slot on its own, so this isn't
load-bearing, just a backstop). An unchanged IDLE target is never re-asserted on a timer,
only on an actual transition into it — General mode doesn't decay on its own, and
periodically re-writing it would blindly overwrite a mode the owner set by hand via the
inverter's own app. The very first decision after a (re)start is always applied
regardless (last_target starts as None), which is what self-heals a setpoint a crashed
prior instance left forced.

CONNECTION MODEL: this process does NOT hold a persistent Modbus connection — it opens
one only to apply/re-assert a target, then closes it immediately. It runs as a fully
separate process from modbus_poller.py (which does hold a persistent connection).
Empirically (2026-07-02) the poller's connection ran undisturbed through five separate
connect/write/close cycles from other scripts, including two that held a connection for
16+ minutes each — a second, brief, on-demand connection appears safe on this dongle.
Keeping this loop's connections brief and infrequent (at most once a minute) minimizes
overlap regardless, rather than relying on that finding being airtight.

SAFETY:
  - Disarmed by default (SOLINTEG_CONTROL_ARMED unset in solinteg.env) — every register
    write is a no-op (see inverter_control.write_u16), so this is safe to run
    continuously in shadow mode: it computes and logs every decision it WOULD make
    against the live plan, without ever touching a register. Note idle-slot decisions
    never even open a connection when disarmed (return_to_auto's writes short-circuit
    before connecting); charge/discharge decisions still open a connection to read the
    real SoC for the ceiling/floor check inside force_charge/force_discharge, but write
    nothing.
  - Every decision is logged to the control_actions table (timestamp, slot, planned
    action, power, armed, outcome) — see deploy/schema.sql.
  - No optimizer plan for today, or no matching slot, is treated as 'idle'
    (return_to_auto) — never silently left on a stale forced setpoint because data is
    missing.
  - A failed apply attempts return_to_auto as its own fail-safe rather than leaving a
    half-applied setpoint — reverted before the failure is logged, so a telemetry.db
    write error (shared WAL db, contention is realistic) can never skip the revert.
    Neither error outcome records last_target (next_last_target), so the next tick retries
    instead of treating the target as handled: after 'error_revert_failed' the previous
    slot's forced setpoint may still be running, and after 'error_reverted' the inverter is
    in auto — in both cases it is NOT at the target the loop just tried to apply. Recording
    it would make needs_apply() consider the target satisfied and delay recovery by a full
    DISPATCH_REASSERT_S (added 2026-07-08 for the failed-revert half; extended to
    'error_reverted' after measuring that asymmetry live).
  - A failure in decide() itself (e.g. a corrupt dispatch_json row) also triggers a
    best-effort revert, so a run of bad data can't leave a previous slot's forced setpoint
    running indefinitely just because a new one can't be computed. Only the FIRST failure of
    a streak reverts (decide_failure_reverted, in the main loop) — once armed, a persistent
    failure (the same bad row on every tick) must not re-write the register every 60s
    forever, which would repeatedly stomp any mode the owner set by hand via the inverter's
    own app. The flag clears the moment decide() succeeds again, so a fresh failure later
    still reverts.
  - Before forcing a charge/discharge, actual live SoC is checked against the plan's
    assumed starting SoC for this slot (DISPATCH_SOC_DIVERGENCE_KWH). The plan is only as fresh as
    the last optimizer_runs row — an unexpectedly sunny/cloudy stretch can leave actual
    SoC well off its trajectory for up to an hour — and slot_power_w's calculated power is
    only correct relative to the plan's assumed baseline, not the real one. Beyond the
    threshold we no longer trust that number and fall back to auto (logged as
    'skipped_divergence') instead of forcing a target computed from a stale starting point.
    The expected/actual/drift values are logged in `detail` on EVERY such check, not just
    ones that trip the threshold — query control_actions for that history if you want to
    tune the threshold itself against real data instead of a guess.
  - Before forcing (or continuing to force) a CHARGE, the plan's funding for it is
    checked against live solar (check_solar_funding) — on EVERY loop iteration while
    the charge is active, not just at the moment it starts. decide() re-reads live.json
    and re-runs the check every LOOP_INTERVAL_S, and that result is folded into the
    target used for the apply decision (see the main loop), so a cloud rolling in
    mid-slot triggers an immediate fallback to auto rather than waiting for the slot
    boundary or the next DISPATCH_REASSERT_S. The DP plans each charge against forecast
    solar; force_charge opens grid import at the full cap, so if that solar doesn't
    materialize the inverter would silently buy the difference at full price — the one
    place a forecast miss directly costs money. From the same optimizer_runs row's
    inputs_json we compute how much of the charge the plan meant to fund from solar
    surplus vs the grid, compare with the live surplus (pv_w − house_load_w from the
    poller's live.json), and if forcing it would buy more than
    DISPATCH_SOLAR_SHORTFALL_KWH beyond the plan we fall back to auto instead (logged
    as 'skipped_solar_shortfall'; if solar recovers mid-slot, the same per-iteration
    check flips back and forcing resumes at the plan's target). Auto (self-use) still
    charges from whatever surplus actually exists, so skipping wastes no real solar. If
    live.json is missing or stale the check can't see current solar, so slots the plan
    funds mostly from solar are skipped conservatively. Funding numbers are logged in
    `detail` on EVERY forced-charge decision (like the SoC-divergence numbers) so the
    threshold can be tuned from real control_actions data.
  - Before forcing a DISCHARGE, the plan-only power target (from slot_power_w, based
    purely on the DP's own SoC trajectory) is corrected against live load/solar
    (live_discharge_power_w, gated by DISPATCH_LIVE_LOAD_TRACKING) — added 2026-07-12
    after an evening where an ordinary appliance spike (thousands of watts above the
    load model's forecast for that slot) leaked straight to the grid at full retail
    price: 50207 is a FIXED battery-power register, so holding the plan's forecast-
    based figure while real load runs above forecast means the excess is bought from
    the grid regardless of how much SoC sits unused in the battery. The correction
    targets the plan's intended NET GRID POSITION for this slot (dispatch[idx].gridKwh,
    +import/−export) rather than its fixed battery-power figure: given live pv_w/
    house_load_w, `required_w = house_load_w − pv_w − planned_grid_w` is the battery
    power that would make the REAL grid flow match what the plan intended, so a load
    spike is absorbed by the battery instead of leaking to the grid, and — symmetric,
    same formula — an over-forecast load lowers the discharge instead of over-
    exporting. Only ever overrides the plan when live.json is fresh (same
    LIVE_MAX_AGE_S staleness rule as the charge-side check); missing/stale data falls
    back to the plan-only figure, unchanged from before this existed. Still clamped to
    hardware limits (clamp_power_w) and still subject to the SoC-floor check inside
    force_discharge and the SOC_DIVERGENCE_KWH guard below — deliberately NOT given its
    own separate cap: a sustained forecast miss across many slots is exactly what
    SOC_DIVERGENCE_KWH (multi-slot) and the SoC floor (absolute) are already for, and
    every replan (hourly, or any dashboard view) re-anchors the DP to the real live SoC
    anyway, so spending more of the reserve now than the plan assumed simply means the
    next plan sees less energy available and re-optimizes the rest of the day around
    that — the same "react now, let the next replan account for it" philosophy as the
    solar-funding check above, not a new safety model. house_load_w/pv_w and the
    resulting target are logged in `detail`/`detail_json` on every discharge decision,
    live or plan-only, so this can be reviewed the same way as the other guards via control_actions. Three refinements added 2026-07-13 after the first night live:
    (1) a live-tracked target of 0 W (surplus covers the house mid-slot) demotes the
    tick to idle/auto instead of calling force_discharge(0) — EMS BattCtrl parked at
    0 W blocks self-consumption, the exact anti-pattern MODBUS.md warns about
    (inverter_control.py now also refuses 0 W as a backstop); (2) the divergence
    guard's expectation is time-interpolated within the slot (plan_expected_soc_now)
    so per-tick retargets late in a high-power slot don't read normal within-slot
    depletion as drift; (3) retargets smaller than LIVE_LOAD_DEADBAND_W wait for the
    next reassert, so load noise doesn't write the register every tick.
  - An idle decision only writes the auto/General register when the loop's own last
    confirmed write left something else set (loop_in_auto, main loop) — idle slot
    boundaries still log their control_actions row (armed-coverage measurement reads row
    cadence as "loop alive", see lib/oracle.ts's ARMED_SEGMENT_CAP_MS), but no longer
    re-poke General mode every 15 min, which would blindly overwrite a mode the owner set
    by hand via the inverter's own app.
  - SIGTERM/SIGINT and normal shutdown always revert to auto.
  - Every loop iteration — idle or not, successful or not — touches a heartbeat file
    (DISPATCH_HEARTBEAT_PATH). This is deliberately NOT the same signal as a fresh
    control_actions row: those are only written on an actual target change or the periodic
    reassert, so a long, healthy idle stretch can leave control_actions silent for hours by
    design (see the reassert comment above) — that silence must not look like a dead loop.
    scripts/services/watchdog.py, a separate process, watches this file: if it goes stale while
    armed, it forces the inverter back to auto itself, since a hard crash here (OOM kill,
    power loss) skips this script's own SIGTERM/SIGINT fail-safe entirely.

REPLAN TRIGGERS: beyond the timer-driven render cadence
  (at least hourly via solinteg-telemetry.timer, plus AutoRefresh every 5 min, plus any
  real page view), this loop can now ask the web app to compute a fresh plan the moment it
  notices the CURRENT one looks wrong, rather than only ever consuming whichever optimizer_runs
  row happens to be newest. maybe_request_replan() below is a small, debounced, fire-and-forget
  POST to DISPATCH_REPLAN_URL, called from five places: preemptive SoC drift past
  DISPATCH_REPLAN_DRIFT_KWH (deliberately tighter, and checked earlier, than
  DISPATCH_SOC_DIVERGENCE_KWH itself — the idea is that a fresh, live-anchored plan usually
  already exists by the time drift would otherwise cross that guard's own wider threshold), an
  actual skipped_divergence or skipped_solar_shortfall outcome, no plan row covering "now" at
  all, and today's plan still missing tomorrow's prices (has_tomorrow == 0) once it's past
  13:05 Stockholm — Nord Pool's day-ahead release window (see DOMAIN.md). This is a NUDGE, never
  a dependency: POST /api/replan runs the exact same producePlan() a normal dashboard render
  already calls (see lib/plan.ts), so every way this can end up doing nothing — the
  DISPATCH_REPLAN_TRIGGERS=0 kill switch, still being inside the DISPATCH_REPLAN_DEBOUNCE_S
  window, "shadow" mode (logs the decision, never calls out), or the HTTP request itself
  erroring or timing out — degrades to EXACTLY the behaviour from before this feature existed:
  the loop keeps acting on the newest optimizer_runs row and simply waits for the next
  timer-driven render to produce a fresher one. It never touches a register, a guard threshold,
  or a fail-safe path directly — it can only ever cause the WEB APP to compute (and, SoC
  permitting — see logOptimizerRun's publish gate — publish) a plan sooner than the timer
  schedule would have on its own.

Environment variables (beyond inverter_control.py's own SOLINTEG_* / SOLINTEG_CONTROL_ARMED):
  TELEMETRY_DB_PATH             SQLite path (default /opt/solinteg/telemetry.db)
  DISPATCH_LOOP_INTERVAL_S      how often to check for a slot change (default 60) — also the
                                dominant bound on how fast live_discharge_power_w can react to
                                a sudden load (e.g. a heat pump compressor starting): worst case
                                is roughly this value + POLL_INTERVAL (modbus_poller.py's own
                                sampling interval, live.json can't be fresher than that) + the
                                apply latency (~1 s now that inverter_control.py's fast path
                                skips redundant setup writes when already mid-discharge, ~8-9 s
                                on the first entry into a forced setpoint). Safe to lower well
                                below the 60 s default for this reason — the fast path means a
                                shorter interval mostly just means more (cheap, ~1 s) on-demand
                                Modbus connections rather than more long ones; still worth
                                watching journalctl for connection errors after lowering it a
                                lot, since the underlying dongle only tolerates one connection
                                at a time (see MODBUS.md) and this doesn't change that ceiling,
                                just how often it's approached.
  DISPATCH_REASSERT_S           max time between re-writes of an unchanged target (default 300)
  DISPATCH_SOC_DIVERGENCE_KWH   max |plan-assumed − actual| starting SoC (kWh) before a
                                forced target is skipped as stale (default 3.0; logged as
                                'skipped_divergence')
  DISPATCH_SOLAR_SHORTFALL_KWH  extra grid kWh a forced charge may buy vs the plan before
                                falling back to auto (default 0.5)
  DISPATCH_LIVE_LOAD_TRACKING   "0" disables live_discharge_power_w's correction, reverting
                                discharge slots to the plan's fixed forecast-based power
                                (default "1" — on). Kill switch, not a tuning knob: flip to
                                "0" and restart the service if this ever needs to be ruled
                                out while investigating something else.
  DISPATCH_LIVE_LOAD_TRACKING_CHARGE
                                the same correction on CHARGE slots (live_charge_power_w),
                                which previously had only the binary solar-funding veto:
                                "shadow" (default) computes and logs the target it would
                                command without applying it, "1" applies it, "0" disables it.
                                Shadow by default because this writes a power target during
                                grid-funded charging — review a few days of control_actions
                                rows (detail_json.tracked_charge_w vs planned_power_w) before
                                arming it.
  DISPATCH_LIVE_LOAD_ROUND_W    live-tracked discharge target is rounded to the nearest this
                                many watts (default 100) — precision of the written setpoint,
                                not the write-frequency control (that's the deadband below).
  DISPATCH_LIVE_LOAD_DEADBAND_W same-slot same-action retargets smaller than this (default
                                250) are deferred to the next REASSERT_S write instead of
                                being applied on their own tick — stops ordinary load noise
                                from writing the register every tick, without touching the
                                reaction to a genuinely large change (which clears the band
                                immediately). Must be > DISPATCH_LIVE_LOAD_ROUND_W to have
                                any effect.
  INVERTER_DATA_PATH            the poller's live.json (default /opt/solinteg/live.json) —
                                POLL_INTERVAL (modbus_poller.py, default 30) governs how fresh
                                this can ever be, independent of this loop's own interval.
  DISPATCH_FREEZE_HOLDS         "1" realises a planned hold as a REAL freeze (50207/50208/50209
                                = 0) instead of the ~220 W battery-save emulation; anything else
                                (the default) keeps the emulation. The emulation works but still
                                moves ~0.055 kWh/slot out of the battery at the CURRENT price when
                                the point of the hold is to keep it for a dearer one — ~2.6 kWh over
                                a 12 h overnight hold. Off by default: this register combination is
                                not probe-verified on the reference unit (MODBUS.md records 50209=0
                                being stored but not enforced when written pre-mode), and a stale 0
                                cap would cripple self-consumption for a whole run of idle slots.
                                Verify with scripts/tools/probe_freeze.py first.
  DISPATCH_HEARTBEAT_PATH       liveness file for scripts/services/watchdog.py (default
                                /opt/solinteg/dispatch-heartbeat.json)
  DISPATCH_IMPORT_LIMIT_W       site-level utility-meter import cap in watts, written once at
                                startup to registers 50007/50009 (default 0 = never written).
                                This is the backstop for a forced charge plus a load spike
                                exceeding the main fuse: the optimizer's own grid-cap check uses
                                FORECAST load, and charge slots have no live-load retargeting the
                                way discharge slots do. Not probe-verified on the reference unit —
                                run scripts/tools/probe_import_limit.py on yours first. A failure
                                to apply it is logged and does NOT stop the loop; the pre-existing
                                software bounds (clamp_power_w, the planner's cap) still apply.
  DISPATCH_REPLAN_TRIGGERS      "1" (default) POSTs to DISPATCH_REPLAN_URL on the five trigger
                                conditions in the REPLAN TRIGGERS section above; "0" disables
                                the feature entirely (kill switch — reverts to the pre-feature,
                                timer-only cadence); "shadow" computes the same decision and
                                logs it (INFO) but never calls out, for watching trigger
                                frequency before it's allowed to touch the web app
  DISPATCH_REPLAN_DRIFT_KWH     preemptive SoC-drift threshold in kWh for requesting a replan
                                (default 1.5 — HALF of DISPATCH_SOC_DIVERGENCE_KWH's own
                                default, so this fires before that guard would ever need to
                                skip a forced target)
  DISPATCH_REPLAN_DEBOUNCE_S    minimum seconds between replan requests, regardless of which
                                trigger(s) fired or how many times in that window (default 120)
  DISPATCH_REPLAN_URL           where to POST the replan request (default
                                http://localhost:3000/api/replan — the web app runs on the same
                                NUC, see solinteg-web.service)
"""

import json
import logging
import os
import signal
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import common  # sibling module (scripts/services/) — script dir is sys.path[0]
from l3_battery_policy import observe_l3, suggest_battery_target
from inverter_control import (
    ARMED,
    FreezeNotEnforced,
    Inverter,
    force_charge,
    force_discharge,
    force_hold,
    freeze_battery,
    return_to_auto,
    clamp_power_w,
    set_import_limit,
    SOC_FLOOR_RECOVERY_W,
    REG_BATT_POWER_TARGET,
    target_raw_for_action,
)

log = logging.getLogger("solinteg.dispatch")

DB_PATH = os.environ.get("TELEMETRY_DB_PATH", "/opt/solinteg/telemetry.db")
LOOP_INTERVAL_S = int(os.environ.get("DISPATCH_LOOP_INTERVAL_S", "60"))
REASSERT_S = int(os.environ.get("DISPATCH_REASSERT_S", "300"))
STOCKHOLM = ZoneInfo("Europe/Stockholm")
UTC = ZoneInfo("UTC")
SLOT_HOURS = 0.25

# ACTION CONTRACT with the TypeScript optimizer's `Action` union (lib/optimizer.ts).
#
# apply_target() below forces a setpoint for FORCED_ACTIONS and returns to auto for everything
# else. That "everything else" is the hazard: an action type added on the TS side but unknown
# here does not fail - it silently takes the auto path, and auto CHARGES from solar surplus.
# For a planner `hold` action ("don't charge, export the surplus") that is the exact opposite of
# what was planned, and nothing in the plan-vs-actual telemetry would name the cause. Planner `hold`
# therefore maps explicitly to Auto as a legacy-plan fallback; physical freeze is named `freeze`.
#
# So the vocabulary is declared here rather than left implicit, and
# lib/__tests__/action-contract.test.ts asserts these two tuples together cover every member of
# the TS union. Adding an action there fails that test until it is either given a branch below
# or listed in AUTO_ACTIONS as a deliberate auto mapping. See CONTRIBUTING.md.
FORCED_ACTIONS = ("charge", "discharge")
AUTO_ACTIONS = ("hold", "idle")
# 'freeze' is an EXECUTOR-ONLY action and deliberately not part of the TS Action union: the optimizer
# says "idle" and marks the slot with batterySaveDriftKwh, and this loop decides HOW to realise that
# — Auto, the ~220 W battery-save emulation, or a real freeze (see FREEZE_HOLDS). Keeping it out of
# the shared vocabulary is what stops lib/__tests__/action-contract.test.ts from demanding a TS
# counterpart for a purely local execution strategy.
EXECUTOR_ACTIONS = ("freeze", "reserve_hold")

# One real source of truth: same env var name lib/constants.ts reads, matching hardcoded
# fallback default (kept in sync by lib/__tests__/constants-cross-language.test.ts).
BATTERY_KWH = float(os.environ.get("SOLINTEG_BATTERY_KWH", "25.6"))  # usable capacity
SOC_FLOOR_PCT = float(os.environ.get("SOLINTEG_SOC_FLOOR_PCT", "8"))
BATTERY_FLOOR_KWH = BATTERY_KWH * SOC_FLOOR_PCT / 100
# The hold DECISION now comes from the plan's own batterySaveDriftKwh marker (see is_planned_hold),
# not from a re-derived load threshold that disagreed with the optimizer's own in a 200-420 W band.
# loadFromGridKwh still caps the target in battery_save_target -- a hold may not discharge more than
# the house is actually taking from the grid.
# The forecast drain is measured inverter behaviour, not a reserve margin. The SoC cap below
# still prevents the target from crossing the operational floor.
# How far actual SoC may drift from the plan's assumed starting point for this slot before
# we stop trusting the plan's calculated power and fall back to auto instead. The plan is
# only as fresh as the last optimizer_runs row (recomputed on a dashboard view, or hourly
# via solinteg-telemetry.timer) — an unexpectedly sunny/cloudy stretch can leave actual SoC
# meaningfully off the plan's trajectory for up to that long. A stale plan's power target
# is calculated from the WRONG baseline (see slot_power_w), so beyond this threshold we no
# longer trust it enough to force it blindly.
SOC_DIVERGENCE_KWH = float(os.environ.get("DISPATCH_SOC_DIVERGENCE_KWH", "3.0"))
# Solar-funding guard (see check_solar_funding): how much MORE grid energy a forced charge may
# buy than the plan intended before we fall back to auto instead. 0.5 kWh/slot = a sustained
# 2 kW of missing solar — small enough to cap the cost of a forecast bust, large enough that
# ordinary forecast noise doesn't constantly veto planned charges.
SOLAR_SHORTFALL_KWH = float(os.environ.get("DISPATCH_SOLAR_SHORTFALL_KWH", "0.5"))
# Kill switch for live_discharge_power_w (see module docstring) — "0" reverts discharge
# slots to the plan's fixed forecast-based power, same as before this existed.
LIVE_LOAD_TRACKING_ENABLED = os.environ.get("DISPATCH_LIVE_LOAD_TRACKING", "1") != "0"
# The same idea on the CHARGE side, which had no live correction at all — only the binary
# solar-funding veto (force at the plan's full power, or fall back to Auto). "shadow" (the default)
# computes the target and logs it without applying it, "1" applies it, "0" disables it entirely.
# Shadow by default on purpose: this writes a power target during grid-funded charging, so it should
# be reviewed against a few days of real control_actions rows before it is allowed to act.
LIVE_LOAD_TRACKING_CHARGE = os.environ.get("DISPATCH_LIVE_LOAD_TRACKING_CHARGE", "shadow")
# Round the live-tracked target to the nearest this-many watts — keeps the written
# setpoint from carrying meaningless single-watt precision. The write-frequency control is
# the DEADBAND below, not this rounding (first night's data: 100 W rounding alone produced
# a write every ~81 s, ordinary load noise crosses one bucket almost every tick).
LIVE_LOAD_ROUND_W = int(os.environ.get("DISPATCH_LIVE_LOAD_ROUND_W", "100"))
# An unchanged-slot, unchanged-action retarget is only applied when it moves the power by
# at least this much; smaller moves wait for the next REASSERT_S write (bounded cost:
# 250 W held wrong for 300 s ≈ 0.02 kWh ≈ 4 öre — noise). A heat-pump-scale change
# (thousands of W) clears this immediately, so reaction latency to the events this
# tracking exists for is unaffected. Must be > LIVE_LOAD_ROUND_W to do anything.
LIVE_LOAD_DEADBAND_W = int(os.environ.get("DISPATCH_LIVE_LOAD_DEADBAND_W", "250"))
# Optional battery-save emulation for qualified planned hold slots. A positive value keeps the
# inverter in EMS BattCtrl with a small fixed discharge target instead of returning to Auto,
# whose self-use logic would discharge as much as the house can consume. Zero leaves the
# existing idle -> Auto behaviour unchanged. Use a multiple of 10 W because register 50207
# has 10 W resolution (see MODBUS.md).
BATTERY_SAVE_W = max(0, int(os.environ.get(
    "SOLINTEG_BATTERY_SAVE_DRIFT_W",
    os.environ.get("DISPATCH_BATTERY_SAVE_W", "220"),
))) // 10 * 10
# Realise a planned hold as a REAL freeze (50207/50208/50209 = 0, see inverter_control.freeze_battery)
# instead of the ~220 W battery-save emulation. "1" enables it; anything else (the default) keeps the
# emulation. Off by default because the three registers' behaviour in this combination is not
# probe-verified on the reference unit — MODBUS.md records 50209=0 being stored but not enforced when
# written pre-mode — and because a stale 0 cap would cripple self-consumption for a whole run of idle
# slots if restore_ac_limits ever failed to run. Verify with scripts/tools/probe_freeze.py first.
FREEZE_HOLDS = os.environ.get("DISPATCH_FREEZE_HOLDS", "0") == "1"
# Set once a commanded freeze is measured NOT to be enforced (inverter_control raises
# FreezeNotEnforced when the battery is still moving after the caps went in). From then on this
# process falls back to the battery-save emulation instead of re-attempting a freeze every hold slot:
# the failure is a property of the inverter/firmware, not a transient, so retrying it just churns
# registers and logs. Cleared only by a restart, which is the right granularity for "someone changed
# something".
_freeze_unavailable = False
# SoC-floor guard: when live SoC drops below BATTERY_FLOOR_KWH + this headroom during an
# idle slot, escalate to a small recovery charge so the optimizer — not the BMS — owns the
# timing and can avoid buying at peak price. SOC_FLOOR_RECOVERY_W is chosen to raise SoC
# gently (~0.28 kWh/h) without grid-import spikes. Must be a positive multiple of 10 W.
SOC_FLOOR_GUARD_HEADROOM_KWH = float(os.environ.get("DISPATCH_SOC_FLOOR_GUARD_HEADROOM_KWH", "0.5"))
# Keep charging past the entry threshold: Sunwoda's discharge restart may require a higher SoC
# than the inverter's minimum. This is an operational target, not a claimed BMS setting.
SOC_RECOVERY_EXIT_PCT = float(os.environ.get("DISPATCH_SOC_RECOVERY_EXIT_PCT", str(SOC_FLOOR_PCT + 8)))
SOC_RECOVERY_ENTRY_PCT = (BATTERY_FLOOR_KWH + SOC_FLOOR_GUARD_HEADROOM_KWH) / BATTERY_KWH * 100
# This is a PLANNING reserve, never a safety-floor substitute. Disabled (equal to the
# normal floor) unless the site explicitly enables it in BOTH web and dispatch services.
PLANNER_RESERVE_PCT = float(os.environ.get("SOLINTEG_PLANNER_RESERVE_PCT", str(SOC_FLOOR_PCT)))
# The open-cap 0-W hold is documented as UNVERIFIED on Sundsvall (MODBUS.md).
# Activation requires a separate explicit result of a disarmed hardware probe.
RESERVE_HOLD_VERIFIED = os.environ.get("DISPATCH_RESERVE_HOLD_VERIFIED", "0") == "1"
if PLANNER_RESERVE_PCT > SOC_FLOOR_PCT and not RESERVE_HOLD_VERIFIED:
    raise ValueError("Planner reserve requires verified Sundsvall open-cap hold (DISPATCH_RESERVE_HOLD_VERIFIED=1)")
if not SOC_FLOOR_PCT <= PLANNER_RESERVE_PCT < 98:
    raise ValueError("SOLINTEG_PLANNER_RESERVE_PCT must be between the safety floor and 98%")
if PLANNER_RESERVE_PCT > SOC_FLOOR_PCT and PLANNER_RESERVE_PCT <= SOC_RECOVERY_EXIT_PCT:
    raise ValueError("Planner reserve must be above the recovery exit, or cycling may continue")
if not SOC_RECOVERY_ENTRY_PCT < SOC_RECOVERY_EXIT_PCT < 98:
    raise ValueError("DISPATCH_SOC_RECOVERY_EXIT_PCT must exceed the recovery entry and be below 98%")
# Start conservatively: after a restart the old process may have been recovering between the
# entry and exit levels; do not silently abandon that charge.
_soc_recovery_active = True
RECOVERY_RESPONSE_TOL_W = 60
RECOVERY_RESPONSE_CHECK_S = 120
_last_recovery_nonresponse_report = 0.0
# Site-level (utility-meter) import limit, applied ONCE at startup via registers 50007/50009 --
# the only cap that counts house load together with a forced charge. Unset/0 = never written,
# which is the default because these two registers are not probe-verified on the reference unit
# (see set_import_limit's docstring and scripts/tools/probe_import_limit.py). Set it to the real
# main-fuse rating in watts once your own probe confirms the inverter honours it; it is a bound,
# not a setpoint, so it deliberately outlives this process rather than being reverted on exit.
IMPORT_LIMIT_W = int(os.environ.get("DISPATCH_IMPORT_LIMIT_W", "0"))
LIVE_JSON_PATH = os.environ.get("INVERTER_DATA_PATH", "/opt/solinteg/live.json")
LIVE_MAX_AGE_S = 120  # same staleness rule as lib/inverter.ts
# House battery L3 economic coordination, never EVCC control or a fuse safety device.
# "shadow" computes/logs proposed battery targets but writes NOTHING differently.
# "1" may change battery-only setpoints. Before every active interval it verifies
# inverter register 50006==0 (balanced output) independently through read-only Modbus.
L3_BATTERY_MODE = os.environ.get("DISPATCH_L3_BATTERY_MODE", "0").lower()
if L3_BATTERY_MODE not in ("0", "shadow", "1"):
    raise ValueError("DISPATCH_L3_BATTERY_MODE must be 0, shadow, or 1")
L3_CHARGE_BUDGET_W = int(os.environ.get("DISPATCH_L3_CHARGE_BUDGET_W", "4000"))
L3_EV_LIKE_THRESHOLD_W = int(os.environ.get("DISPATCH_L3_EV_LIKE_THRESHOLD_W", "2500"))
L3_BASELINE_W = int(os.environ.get("DISPATCH_L3_NON_EV_BASELINE_W", "600"))
L3_EV_EST_MAX_W = int(os.environ.get("DISPATCH_L3_EST_EV_MAX_W", "4300"))
L3_BALANCED_CHECK_INTERVAL_S = 600
_l3_checked_until = 0.0
_l3_balanced_output = False
# Same env vars lib/constants.ts reads (kept in sync by the cross-language test). ONE_WAY_EFF must
# mirror EFF_ONE_WAY there exactly: it is the METER-to-stored per-leg efficiency, so it carries the
# inverter's conversion losses as well as the cells'. Using the battery-terminal DC figure alone
# (which is what SOLINTEG_BATTERY_RT_EFF is) overstated it by ~3 points per leg — see
# INVERTER_CONV_EFF in lib/constants.ts.
BATTERY_RT_EFF = float(os.environ.get("SOLINTEG_BATTERY_RT_EFF", "0.96"))
INVERTER_CONV_EFF = float(os.environ.get("SOLINTEG_INVERTER_CONV_EFF", "0.97"))
ONE_WAY_EFF = BATTERY_RT_EFF ** 0.5 * INVERTER_CONV_EFF
HEARTBEAT_PATH = os.environ.get("DISPATCH_HEARTBEAT_PATH", "/opt/solinteg/dispatch-heartbeat.json")


# See the module docstring's REPLAN TRIGGERS section for the full design. "0"/"1"/"shadow"
# (not a bare bool) so a rollout can watch the trigger rate in journalctl in shadow mode before
# it's allowed to actually reach the web app.
REPLAN_TRIGGERS = os.environ.get("DISPATCH_REPLAN_TRIGGERS", "1")
REPLAN_DRIFT_KWH = float(os.environ.get("DISPATCH_REPLAN_DRIFT_KWH", "1.5"))
REPLAN_DEBOUNCE_S = int(os.environ.get("DISPATCH_REPLAN_DEBOUNCE_S", "120"))
REPLAN_URL = os.environ.get("DISPATCH_REPLAN_URL", "http://localhost:3000/api/replan")

# Module-level: last time (time.monotonic(), immune to wall-clock jumps) a replan was actually
# requested — real POST or shadow-logged, either counts as "a request" for debounce purposes, so
# shadow mode's log line is itself rate-limited the same way the real call would be, rather than
# firing on every tick. 0.0 ("never") means the very first qualifying trigger always goes through.
_last_replan_request_monotonic = 0.0


def maybe_request_replan(reason: str) -> None:
    """Ask the web app to compute (and, live-SoC permitting, publish — see logOptimizerRun's
    publish gate) a fresh plan right now, instead of leaving this loop to act on a plan that's
    up to an hour stale until the next timer-driven render. See the module docstring's REPLAN
    TRIGGERS section for the full design and why every failure mode here is safe by construction.

    reason is one of "drift" / "divergence_skip" / "solar_shortfall" / "no_plan" /
    "awaiting_tomorrow" (see the five call sites) — carried only for the log line, so journalctl
    shows which condition is driving replan traffic.

    Never raises: disabled (DISPATCH_REPLAN_TRIGGERS=0) and still-debounced both return silently
    since neither is noteworthy; "shadow" logs at INFO and returns without calling out; a real
    request logs its outcome at INFO (success) or WARNING (any exception) but never propagates
    the exception — a dead/unreachable web app must degrade to the pre-feature timer cadence,
    not take down the tick that happened to notice the problem. Never retries on its own either;
    the debounce window is what stands in for a retry/backoff policy here. The only way this can
    delay the calling tick is the bounded 10 s request timeout below — an accepted cost, since
    the debounce keeps it to at most one attempt per DISPATCH_REPLAN_DEBOUNCE_S no matter how
    many ticks' worth of trigger conditions are true in that window.
    """
    global _last_replan_request_monotonic
    if REPLAN_TRIGGERS == "0":
        return
    if time.monotonic() - _last_replan_request_monotonic < REPLAN_DEBOUNCE_S:
        return
    _last_replan_request_monotonic = time.monotonic()

    if REPLAN_TRIGGERS == "shadow":
        log.info("replan trigger (shadow) reason=%s — DISPATCH_REPLAN_TRIGGERS=shadow, not calling %s", reason, REPLAN_URL)
        return

    try:
        req = urllib.request.Request(REPLAN_URL, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info("replan trigger reason=%s -> POST %s HTTP %s", reason, REPLAN_URL, resp.status)
    except Exception as exc:  # noqa: BLE001 — a replan nudge must never affect the tick itself
        log.warning("replan trigger reason=%s failed (%s) — continuing on the existing plan", reason, exc)


def init_db(path: str) -> sqlite3.Connection:
    con = common.telemetry_connect(path)
    # Canonical schema for all telemetry.db tables: deploy/schema.sql — keep this in sync.
    con.execute("""
        CREATE TABLE IF NOT EXISTS control_actions (
            id             INTEGER PRIMARY KEY,
            timestamp      TEXT NOT NULL,
            slot_time      TEXT,
            planned_action TEXT NOT NULL,
            power_w        INTEGER,
            armed          INTEGER NOT NULL,
            outcome        TEXT NOT NULL,
            detail         TEXT,
            detail_json    TEXT
        )
    """)
    # Additive migration for DBs created before detail_json existed (2026-07-03) — same
    # pattern as modbus_poller.py's soh_pct/battery_temp_c migration: CREATE TABLE IF NOT
    # EXISTS is a no-op on an existing table, so old columns must be added explicitly.
    # Safe to run repeatedly: ignores "duplicate column" if already applied.
    try:
        con.execute("ALTER TABLE control_actions ADD COLUMN detail_json TEXT")
    except sqlite3.OperationalError:
        pass  # already applied
    con.execute("""
        CREATE TABLE IF NOT EXISTS control_readbacks (
            id                   INTEGER PRIMARY KEY,
            timestamp            TEXT NOT NULL,
            slot_time            TEXT,
            planned_action       TEXT NOT NULL,
            requested_power_w    INTEGER NOT NULL,
            target_raw           INTEGER,
            target_readback_raw  INTEGER,
            mode_raw             INTEGER,
            battery_w            INTEGER,
            soc_pct              REAL,
            readback_ok          INTEGER NOT NULL,
            error                TEXT
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_readback_ts ON control_readbacks(timestamp)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_control_ts ON control_actions(timestamp)")
    con.commit()
    return con


def log_control_readback(con: sqlite3.Connection, slot_time, planned_action: str,
                         requested_power_w: int, inv) -> None:
    """Persist the requested save target beside the inverter's readback.

    This is deliberately separate from `readings`: it records the control boundary, while
    readings remains the regular poller's time series. A failed read is retained as a row so
    missing verification is visible rather than looking like a healthy zero.
    """
    target_raw = target_raw_for_action(planned_action, requested_power_w)
    target_readback_raw = mode_raw = battery_w = soc_pct = None
    error = None
    try:
        target_readback_raw = inv.read_s16(REG_BATT_POWER_TARGET)
        mode_raw = inv.work_mode()
        battery_w = inv.battery_power_w()
        soc_pct = inv.soc_pct()
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
    con.execute(
        """INSERT INTO control_readbacks
           (timestamp, slot_time, planned_action, requested_power_w, target_raw,
            target_readback_raw, mode_raw, battery_w, soc_pct, readback_ok, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (datetime.now(UTC).isoformat(), slot_time, planned_action, int(requested_power_w),
         target_raw, target_readback_raw, mode_raw, battery_w, soc_pct,
         1 if error is None else 0, error),
    )
    con.commit()


def log_action(con: sqlite3.Connection, slot_time, planned_action, power_w, outcome, detail="",
                detail_json: dict | None = None) -> None:
    """detail_json carries the exact figures behind this decision (buy/sell price,
    solar-shortfall, SoC-drift, each with its threshold) for the dashboard's Dispatch
    card gauges — see decide()'s docstring for where each field comes from. detail
    (free text) stays the human-readable log message; detail_json is never parsed
    from it, so future wording changes to `detail` can't silently break the UI."""
    con.execute(
        """INSERT INTO control_actions
           (timestamp, slot_time, planned_action, power_w, armed, outcome, detail, detail_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (datetime.now(UTC).isoformat(), slot_time, planned_action, power_w,
         1 if ARMED else 0, outcome, detail,
         json.dumps(detail_json) if detail_json is not None else None),
    )
    con.commit()


def stockholm_date(now: datetime) -> str:
    return now.astimezone(STOCKHOLM).strftime("%Y-%m-%d")


# How many recent optimizer_runs rows load_latest_plan will consider. Small on purpose: this is a
# "the newest usable plan" lookup, not a search — if none of the last few rows covers now, the right
# answer is no plan (and a replan request), not digging further into history.
PLAN_LOOKBACK_ROWS = 5


def load_latest_plan(con: sqlite3.Connection, now: datetime):
    """The newest logged optimizer run whose slots actually COVER `now`, as
    (start_soc_kwh, dispatch slots, optimizer input slots, has_tomorrow), or
    (None, None, None, None).

    Selected by coverage rather than by price_date, which is what it used to filter on. A plan spans
    up to three days (lib/prices.ts merges a forecast horizon), so a plan computed at 23:50 covers
    tomorrow in full — but it is logged under yesterday's price_date, so at 00:00 the date filter
    stopped finding it. Every single day, decide() then reported no_plan and the loop fell back to
    Auto (self-use, which discharges the battery to cover load) until a render or the replan trigger
    produced a fresh row — at the hour when prices are usually lowest and a hold or charge is most
    likely what the plan wanted.

    price_date stays on the row for indexing and analysis; it is simply not what "is this plan
    usable right now" means. inputs_json rides along for the solar-funding check: it holds the
    per-slot forecast solarKwh/consumptionKwh the dispatch plan was computed from. has_tomorrow is
    the plan's own record of whether tomorrow's day-ahead prices were released when it was computed,
    read here so the awaiting_tomorrow replan trigger in decide() doesn't need a second query.
    """
    rows = con.execute(
        """SELECT start_soc_kwh, dispatch_json, inputs_json, has_tomorrow FROM optimizer_runs
           ORDER BY logged_at DESC LIMIT ?""",
        (PLAN_LOOKBACK_ROWS,),
    ).fetchall()
    for start_soc_kwh, dispatch_json, inputs_json, has_tomorrow in rows:
        dispatch = json.loads(dispatch_json)
        if slot_index_for_instant(dispatch, now) is not None:
            return start_soc_kwh, dispatch, json.loads(inputs_json), has_tomorrow
    return None, None, None, None


def slot_index_for_instant(dispatch: list, now: datetime):
    """Which array index in the plan covers `now`, found by REAL elapsed time since the
    plan's OWN FIRST SLOT rather than by matching DispatchSlot.startTime as a string.

    The web app slices the optimizer's input down to "from now onward" before solving
    (see lib/slot-utils.ts's currentSlotIndexInPrices) so optimizeDispatch's own index 0
    lines up with the live SoC reading it's given, instead of always being midnight —
    fixed 2026-07-03 after a mid-day SoC divergence bug traced to the old full-day
    version anchoring a live "right now" SoC reading to a fictitious midnight. That
    means dispatch[0] is whatever slot was current AT PLAN-COMPUTATION TIME, not
    today's midnight, so the elapsed-time anchor here has to be dispatch[0]'s own
    startTime, not price_date's midnight.

    DispatchSlot.startTime is a naive Stockholm local string (the UTC offset is stripped
    in lib/prices.ts) — on the autumn DST fall-back day, 02:00-03:00 local occurs twice,
    so two genuinely different array entries can share an identical string. Anchoring
    elapsed time off dispatch[0] (rather than matching each slot by string) sidesteps
    that the same way the old midnight-anchored version did, with one narrow residual
    edge case: if the plan itself was computed during that repeated hour, interpreting
    dispatch[0]'s own label is briefly ambiguous (Python's zoneinfo resolves to the
    first occurrence, fold=0) — at most one hour, one day a year, and self-correcting
    on the next replan (the web app recomputes on every page view and hourly via
    solinteg-telemetry.timer; this loop rechecks every LOOP_INTERVAL_S regardless).
    """
    if not dispatch:
        return None
    first_start = datetime.strptime(dispatch[0]["startTime"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=STOCKHOLM)
    first_start_utc = first_start.astimezone(UTC)
    idx = int((now - first_start_utc).total_seconds() // (15 * 60))
    return idx if 0 <= idx < len(dispatch) else None


def expected_prev_soc_kwh(dispatch: list, idx: int, start_soc_kwh: float) -> float:
    """SoC the plan expected at the START of slot idx (its baseline for this slot's delta)."""
    return start_soc_kwh if idx == 0 else dispatch[idx - 1]["socAfter"]


def plan_expected_soc_now(dispatch: list, idx: int, prev_soc: float, now: datetime) -> float:
    """The plan's expected SoC at THIS MOMENT: the start-of-slot baseline advanced linearly
    toward this slot's socAfter by the fraction of the slot already elapsed.

    Added 2026-07-13. The divergence guard used to compare live SoC against the START-of-
    slot expectation, which was fine when checks only ran at slot boundaries and occasional
    reasserts — but live-load tracking (2026-07-12) re-checks on every retarget, i.e.
    potentially every tick, all the way through the slot. Against a start-of-slot baseline,
    a perfectly-on-plan 11 kW discharge reads as ~2.7 kWh of "drift" by the end of its own
    slot — pure within-slot depletion, nearly the whole 3.0 kWh threshold — so a late-slot
    retarget could false-trip skipped_divergence with zero real forecast error. Interpolating
    the expectation to "now" makes drift mean what the guard intends: deviation FROM the
    plan's trajectory, not progress ALONG it. (Anchor arithmetic matches
    slot_index_for_instant — elapsed time since dispatch[0]'s own start, DST-safe the same
    way.) `expected`/`drift` in control_actions rows changed meaning at this date — don't
    mix pre/post distributions when tuning the threshold.
    """
    first_start = datetime.strptime(dispatch[0]["startTime"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=STOCKHOLM)
    slot_start_utc = first_start.astimezone(UTC) + timedelta(seconds=idx * 15 * 60)
    frac = (now - slot_start_utc).total_seconds() / (15 * 60)
    frac = min(1.0, max(0.0, frac))
    return prev_soc + (dispatch[idx]["socAfter"] - prev_soc) * frac


def slot_power_w(dispatch: list, idx: int, prev_soc: float) -> int:
    """Battery power needed this slot to realize the planned socAfter transition.

    No efficiency factor here, deliberately, in EITHER direction: register 50207 commands
    BATTERY-side power (confirmed on-device, MODBUS.md) and socAfter is battery-side energy, so the
    two are already in the same units. The efficiency lives between the battery and the METER, which
    is where ONE_WAY_EFF is applied — see live_discharge_power_w, whose target is an AC-side figure
    and therefore does need converting before it reaches this register.
    """
    delta_kwh = dispatch[idx]["socAfter"] - prev_soc
    return clamp_power_w(abs(delta_kwh) / SLOT_HOURS * 1000)


def verify_l3_balanced_output() -> bool:
    """Periodically verify register 50006==0; fail closed on errors or unbalance.

    This is an economic *battery-only* policy, but the algebra for recovering
    house L3 demand relies on balanced inverter power. Never apply a suggestion
    just because we happened to read 50006=0 on a previous day.
    """
    global _l3_checked_until, _l3_balanced_output
    if time.monotonic() < _l3_checked_until:
        return _l3_balanced_output
    _l3_checked_until = time.monotonic() + L3_BALANCED_CHECK_INTERVAL_S
    inv = None
    try:
        inv = Inverter()
        setting = inv.read_u16(50006)
        _l3_balanced_output = setting == 0
        if not _l3_balanced_output:
            log.warning("L3 battery coordination disarmed: unbalanced inverter output (50006=%s)", setting)
    except Exception as exc:  # noqa: BLE001
        _l3_balanced_output = False
        log.warning("L3 battery coordination disarmed: cannot verify register 50006 (%s)", exc)
    finally:
        if inv is not None:
            inv.close()
    return _l3_balanced_output


def coordinate_l3_battery(action: str, power_w: int, expected_soc_kwh, solar_skip: bool,
                          detail: str, numbers: dict, now: datetime):
    """Optional post-plan, post-recovery battery setpoint adjustment.

    Existing SoC recovery and hardware guards have priority. This never alters
    inverter registers on its own, never talks to the EVSE, and returns the exact
    original decision when off, shadow, invalid/old phase data, or uncertain
    inverter balance. Active changes intentionally set expected_soc_kwh=None:
    this policy *deliberately* deviates from the plan's SoC trajectory, while
    force_discharge/force_charge still enforce the physical SoC floor.
    """
    if L3_BATTERY_MODE == "0" or numbers.get("soc_recovery"):
        return action, power_w, expected_soc_kwh, solar_skip, detail, numbers

    reading = common.read_json(LIVE_JSON_PATH)
    observation = observe_l3(reading or {}, now)
    if observation is None:
        numbers["l3_policy_status"] = "unavailable"
        return action, power_w, expected_soc_kwh, solar_skip, detail, numbers

    current_action, current_power, _ = effective_target(action, power_w, solar_skip)
    suggested = suggest_battery_target(
        current_action, current_power, observation,
        l3_charge_budget_w=L3_CHARGE_BUDGET_W,
        ev_like_threshold_w=L3_EV_LIKE_THRESHOLD_W,
        non_ev_baseline_w=L3_BASELINE_W,
        estimated_ev_max_w=L3_EV_EST_MAX_W,
    )
    numbers.update(suggested.metrics)
    numbers["l3_policy_mode"] = L3_BATTERY_MODE
    numbers["l3_policy_suggested_action"] = suggested.action
    numbers["l3_policy_suggested_power_w"] = suggested.power_w
    if (suggested.action, suggested.power_w) == (current_action, current_power):
        return action, power_w, expected_soc_kwh, solar_skip, detail, numbers

    if L3_BATTERY_MODE == "shadow":
        log.info("L3 battery coordination SHADOW: %s %dW -> %s %dW (%s)",
                 current_action, current_power, suggested.action, suggested.power_w, suggested.reason)
        return action, power_w, expected_soc_kwh, solar_skip, detail, numbers

    # A '1' mode cannot use the symmetric inverter/3 inference unless register
    # 50006 is currently OFF. This check is completely independent of live.json.
    if not verify_l3_balanced_output():
        numbers["l3_policy_status"] = "inverter_balance_unknown_or_on"
        return action, power_w, expected_soc_kwh, solar_skip, detail, numbers

    numbers["l3_policy_status"] = "applied"
    detail = (f"L3 battery coordination: {suggested.reason}; L3 inferred "
              f"{observation.house_l3_w:.0f} W; {detail}")
    return suggested.action, suggested.power_w, None, False, detail, numbers


def read_live_reading(now: datetime):
    """Live (pv_w, house_load_w) from the poller's live.json, or None if the file is
    missing, unreadable, or stale (> LIVE_MAX_AGE_S). The poller rewrites it every 30 s,
    so stale means the poller is down and we have no trustworthy picture of current
    conditions."""
    data = common.read_json(LIVE_JSON_PATH)
    if data is None:
        return None
    try:
        age_s = (now - datetime.fromisoformat(data["timestamp"])).total_seconds()
        if age_s > LIVE_MAX_AGE_S:
            return None
        return float(data["pv_w"]), float(data["house_load_w"])
    except (ValueError, KeyError, TypeError):
        return None


def read_live_solar_surplus_w(now: datetime):
    """Live PV surplus (pv_w − house_load_w, floored at 0), or None — see read_live_reading."""
    live = read_live_reading(now)
    if live is None:
        return None
    pv_w, house_load_w = live
    return max(0.0, pv_w - house_load_w)


def read_live_soc_kwh(now: datetime):
    """Live battery SoC in kWh from the poller's live.json, or None if missing/stale.

    Reads soc_pct written by modbus_poller.py and converts to kWh using BATTERY_KWH.
    Returns None conservatively when live.json is unavailable so callers treat the
    absence of data as 'unknown' rather than escalating blindly.
    """
    data = common.read_json(LIVE_JSON_PATH)
    if data is None:
        return None
    try:
        age_s = (now - datetime.fromisoformat(data["timestamp"])).total_seconds()
        if age_s > LIVE_MAX_AGE_S:
            return None
        return float(data["soc_pct"]) / 100 * BATTERY_KWH
    except (ValueError, KeyError, TypeError):
        return None


def live_discharge_power_w(dispatch: list, idx: int, now: datetime, planned_power_w: int):
    """Battery discharge power that holds this slot's REAL grid exchange at what the plan
    intended (dispatch[idx].gridKwh, +import/−export), given live pv_w/house_load_w —
    instead of blindly holding the plan's fixed forecast-based power while a load spike
    (or a solar miss) leaks straight to/from the grid at full price. See the module
    docstring's SAFETY section for the reasoning and the bounds this is still subject to.

    Returns (power_w, detail, numbers). Falls back to planned_power_w — unchanged from
    before this existed — when live.json is missing/stale or tracking is disabled via
    DISPATCH_LIVE_LOAD_TRACKING; detail/numbers still report why in that case.
    """
    if not LIVE_LOAD_TRACKING_ENABLED:
        return planned_power_w, "live load tracking disabled (DISPATCH_LIVE_LOAD_TRACKING=0)", {}

    live = read_live_reading(now)
    if live is None:
        return planned_power_w, "live.json missing/stale — holding plan-only power target", {}

    pv_w, house_load_w = live
    grid_kwh = dispatch[idx].get("gridKwh")
    planned_grid_w = (float(grid_kwh) / SLOT_HOURS * 1000) if grid_kwh is not None else 0.0
    # required_w is an AC-SIDE figure: it is derived from house load, PV and the plan's grid
    # position, all measured at the inverter's AC port / the meter. Register 50207 commands
    # BATTERY-side power, so delivering required_w at AC needs required_w / ONE_WAY_EFF out of the
    # cells. Writing the AC figure straight to the register under-delivered by the conversion loss
    # (~5 % once ONE_WAY_EFF started carrying the inverter's own legs), and that shortfall is
    # precisely what this tracking exists to stop leaking to the grid at full retail price.
    required_ac_w = house_load_w - pv_w - planned_grid_w
    required_battery_w = required_ac_w / ONE_WAY_EFF if required_ac_w > 0 else required_ac_w
    power_w = clamp_power_w(round(required_battery_w / LIVE_LOAD_ROUND_W) * LIVE_LOAD_ROUND_W)
    detail = (
        f"live load {house_load_w:.0f} W, pv {pv_w:.0f} W, planned grid {planned_grid_w:.0f} W "
        f"(+import/-export) -> AC need {required_ac_w:.0f} W -> battery {power_w} W "
        f"(plan-only would be {planned_power_w} W)"
    )
    numbers = {
        "live_house_load_w": round(house_load_w),
        "live_pv_w": round(pv_w),
        "planned_grid_w": round(planned_grid_w),
        "required_ac_w": round(required_ac_w),
        "planned_power_w": planned_power_w,
    }
    return power_w, detail, numbers


def planned_grid_charge_kwh(dispatch_slot: dict, inputs_slot: dict | None, need_kwh: float):
    """How much of this slot's charge the PLAN intended to buy from the grid, in kWh.

    Read straight off the plan (`DispatchSlot.gridToBatteryKwh`) rather than recomputed from the
    forecast, because the plan is the authority and a recomputation cannot reproduce it: the DP
    solves against `consumptionKwh x LOAD_FORECAST_MARGIN` (1.15) while inputs_json deliberately
    records the HONEST, unmargined forecast. Netting solar against the unmargined load overstates
    the planned solar surplus, understates the planned grid share, and therefore OVERSTATES the
    shortfall — so the guard vetoed charges the plan had already accounted for as grid-funded, each
    veto also firing a replan request. See OPTIMIZER-AUDIT.md E1.

    Falls back to the old recomputation for a plan row predating the field, so an in-flight upgrade
    keeps working; returns None when neither source is available.
    """
    raw = dispatch_slot.get("gridToBatteryKwh")
    if raw is not None:
        try:
            # Clamped to the charge itself: a plan whose attribution disagrees with its own SoC
            # trajectory must not produce a grid share larger than the charge it is part of.
            return max(0.0, min(float(raw), need_kwh)), "plan"
        except (TypeError, ValueError):
            pass
    if inputs_slot is None:
        return None, "none"
    planned_surplus_kwh = max(
        0.0,
        float(inputs_slot.get("solarKwh") or 0.0) - float(inputs_slot.get("consumptionKwh") or 0.0),
    )
    return need_kwh - min(planned_surplus_kwh, need_kwh), "forecast"


def live_charge_power_w(dispatch: list, idx: int, now: datetime, planned_power_w: int):
    """Charge power that holds this slot's REAL grid exchange at what the plan intended, given live
    pv_w/house_load_w — the charge-side mirror of live_discharge_power_w.

    Why it exists. Discharge slots have been live-tracked since 2026-07-12; charge slots had only the
    binary solar-funding veto, so between "the plan's full power" and "Auto" there was nothing. That
    costs money in both directions:
      - live solar ABOVE forecast: the fixed setpoint does not rise to absorb it, so the extra
        surplus exports at spot + nätnytta instead of charging;
      - live load ABOVE forecast: it lands on top of the commanded charge, so the meter imports more
        than the plan priced (and more than the fuse assumes — see DISPATCH_IMPORT_LIMIT_W).

    The algebra is the same identity the discharge side uses, solved for the other direction:
    `grid = house_load + battery_ac_in − pv`, so holding grid at the plan's intended position needs
    `battery_ac_in = planned_grid_w + pv_w − house_load_w`. That is an AC-side figure and register
    50207 is battery-side, so the command is `battery_ac_in × ONE_WAY_EFF` (charging: stored = AC ×
    η — the inverse of the discharge conversion).

    Returns (power_w, detail, numbers). A non-positive requirement means the plan's grid position is
    already met without charging, and the caller demotes the tick to idle/auto — Auto still absorbs
    whatever surplus exists, which is strictly better than parking EMS at 0 W.

    This does NOT replace check_solar_funding: the veto is the coarse guard against a forecast bust
    (fall back to Auto entirely), this is the fine correction within a slot the plan still wants.
    """
    if LIVE_LOAD_TRACKING_CHARGE == "0":
        return planned_power_w, "live charge tracking disabled (DISPATCH_LIVE_LOAD_TRACKING_CHARGE=0)", {}

    live = read_live_reading(now)
    if live is None:
        return planned_power_w, "live.json missing/stale — holding plan-only charge target", {}

    pv_w, house_load_w = live
    grid_kwh = dispatch[idx].get("gridKwh")
    planned_grid_w = (float(grid_kwh) / SLOT_HOURS * 1000) if grid_kwh is not None else 0.0
    required_ac_w = planned_grid_w + pv_w - house_load_w
    required_battery_w = required_ac_w * ONE_WAY_EFF if required_ac_w > 0 else required_ac_w
    tracked_w = clamp_power_w(round(required_battery_w / LIVE_LOAD_ROUND_W) * LIVE_LOAD_ROUND_W)
    numbers = {
        "live_house_load_w": round(house_load_w),
        "live_pv_w": round(pv_w),
        "planned_grid_w": round(planned_grid_w),
        "required_ac_w": round(required_ac_w),
        "planned_power_w": planned_power_w,
        "tracked_charge_w": tracked_w,
    }
    if LIVE_LOAD_TRACKING_CHARGE != "1":
        # Shadow: report what it WOULD do, change nothing.
        detail = (
            f"live charge tracking (shadow): live load {house_load_w:.0f} W, pv {pv_w:.0f} W, "
            f"planned grid {planned_grid_w:.0f} W -> would command {tracked_w} W "
            f"(plan-only {planned_power_w} W)"
        )
        return planned_power_w, detail, numbers
    detail = (
        f"live load {house_load_w:.0f} W, pv {pv_w:.0f} W, planned grid {planned_grid_w:.0f} W "
        f"(+import/-export) -> AC need {required_ac_w:.0f} W -> battery {tracked_w} W "
        f"(plan-only would be {planned_power_w} W)"
    )
    return tracked_w, detail, numbers


def check_solar_funding(inputs: list, idx: int, charge_kwh: float, surplus_w,
                        dispatch_slot: dict | None = None):
    """Should a forced charge fall back to auto because live solar can't fund it as planned?

    The DP prices each charge against the slot's forecast solar surplus; whatever that
    doesn't cover it accounts as a grid buy. force_charge opens grid import at the full
    cap and commands battery power, so if the forecast solar doesn't show up the inverter
    silently buys the difference at full price — the one place a forecast miss directly
    costs money. Compare the plan's intended grid share of this charge against what the
    live surplus implies, and skip when the extra buy would exceed SOLAR_SHORTFALL_KWH.
    Skipping wastes no real solar: auto (self-use) still charges from any actual surplus.

    The plan's intended grid share comes from `planned_grid_charge_kwh` — off the plan itself when
    the row carries the attribution, which is what makes this guard agree with what the DP actually
    decided rather than with a recomputation the load margin makes wrong.

    surplus_w=None (no live data) skips conservatively when the plan funds more than the
    threshold from solar. Returns (skip, detail, numbers): detail always carries the
    numbers as human-readable text so the threshold can be tuned from telemetry; numbers
    carries the SAME figures structured (solar_shortfall_kwh/solar_shortfall_limit_kwh)
    for the dashboard's gauge — see log_action's detail_json. Empty dict when the check
    couldn't run at all (no plan attribution and no optimizer inputs for this slot).
    """
    inputs_slot = inputs[idx] if inputs and idx < len(inputs) else None
    need_kwh = charge_kwh / ONE_WAY_EFF  # battery input energy incl. one-way losses
    planned_grid_kwh, source = planned_grid_charge_kwh(dispatch_slot or {}, inputs_slot, need_kwh)
    if planned_grid_kwh is None:
        return False, "no plan attribution or optimizer inputs for slot — solar-funding check not possible", {}
    planned_solar_kwh = max(0.0, need_kwh - planned_grid_kwh)

    if surplus_w is None:
        skip = planned_solar_kwh > SOLAR_SHORTFALL_KWH
        detail = (
            f"live solar unknown (live.json missing/stale); plan funds "
            f"{planned_solar_kwh:.2f} of {need_kwh:.2f} kWh from solar (per {source})"
        )
        # No live surplus reading to compare against, so the "shortfall" here is the
        # whole plan-assumed solar share — the most conservative reading of the risk.
        numbers = {"solar_shortfall_kwh": round(planned_solar_kwh, 3), "solar_shortfall_limit_kwh": SOLAR_SHORTFALL_KWH}
        return skip, detail, numbers

    live_surplus_kwh = surplus_w / 1000.0 * SLOT_HOURS  # if the current surplus held all slot
    projected_grid_kwh = need_kwh - min(live_surplus_kwh, need_kwh)
    shortfall_kwh = projected_grid_kwh - planned_grid_kwh
    detail = (
        f"charge {need_kwh:.2f} kWh input: planned grid {planned_grid_kwh:.2f} kWh (per {source}), "
        f"live surplus {surplus_w:.0f} W -> projected grid {projected_grid_kwh:.2f} kWh, "
        f"solar shortfall {shortfall_kwh:.2f} kWh (threshold {SOLAR_SHORTFALL_KWH})"
    )
    numbers = {"solar_shortfall_kwh": round(shortfall_kwh, 3), "solar_shortfall_limit_kwh": SOLAR_SHORTFALL_KWH}
    return shortfall_kwh > SOLAR_SHORTFALL_KWH, detail, numbers


def decide(con: sqlite3.Connection, now: datetime):
    """Returns (slot_time, action, power_w, expected_soc_kwh, solar_skip, detail, numbers).

    expected_soc_kwh is the plan's expected SoC AT THIS MOMENT (time-interpolated within
    the slot — see plan_expected_soc_now; the start-of-slot value alone would conflate
    normal within-slot depletion with real drift once live-load tracking re-checks every
    tick) — None for idle/no-plan cases, where it's meaningless. The caller compares it
    against the actual live SoC before committing to a forced charge/discharge; see
    SOC_DIVERGENCE_KWH.

    solar_skip is True when a planned charge should fall back to auto because live solar
    can't fund it the way the plan assumed (see check_solar_funding); detail carries the
    funding numbers on every charge decision, tripped or not.

    numbers is a dict of the exact figures behind this decision — buy_ore/sell_ore for
    the current slot; solar_shortfall_kwh/solar_shortfall_limit_kwh for a charge action;
    grid_kwh (the plan's net grid exchange, +import/−export) for a discharge action, so
    the dashboard can tell a grid sale from covering the house load;
    next_action/next_action_time (the next non-idle slot's action + startTime) for an
    idle action, so the dashboard can say e.g. "next charge at 14:45" — for the
    dashboard's Dispatch card (see log_action's detail_json). Always a dict, never None;
    a key is simply absent when that particular check didn't run this iteration or there
    is no more non-idle activity left in the loaded plan. The caller (main()) adds
    soc_drift_kwh/soc_drift_limit_kwh to this SAME dict after the SoC-divergence check,
    since that needs a live inverter connection decide() doesn't hold.
    """
    price_date = stockholm_date(now)
    start_soc_kwh, dispatch, inputs, has_tomorrow = load_latest_plan(con, now)

    if dispatch is None:
        # No plan row at all for today — the loop is about to sit idle for lack of data, not
        # because that's what the plan calls for. Ask the web app to compute one now rather
        # than waiting for the next timer-driven render (see the module docstring's REPLAN
        # TRIGGERS section).
        maybe_request_replan("no_plan")
        return None, "idle", 0, None, False, f"no logged optimizer plan covers now ({price_date})", {}

    # load_latest_plan only returns a plan that covers `now`, so this cannot be None — kept as a
    # belt-and-braces guard rather than an assert, because the fail-safe for "no usable plan" is
    # idle/auto and that must stay true however we got here.
    idx = slot_index_for_instant(dispatch, now)
    if idx is None:  # pragma: no cover — unreachable via load_latest_plan
        maybe_request_replan("no_plan")
        return None, "idle", 0, None, False, f"now falls outside the {len(dispatch)}-slot plan logged for {price_date}", {}

    # Once per tick (this is the only place in decide() reached once a valid plan+slot match is
    # found): if today's plan predates Nord Pool's day-ahead release and it's now past that
    # ~13:00 release window, ask for a replan so the DP can extend its horizon into tomorrow
    # instead of waiting for the 13:22/13:42 Stockholm timer entries
    # (see deploy/solinteg-telemetry.timer) to pick the new prices up on their own.
    now_stockholm = now.astimezone(STOCKHOLM)
    if has_tomorrow == 0 and (now_stockholm.hour, now_stockholm.minute) >= (13, 5):
        maybe_request_replan("awaiting_tomorrow")


    slot_time = dispatch[idx]["startTime"]  # the plan's own label, for logging only
    planned_action = dispatch[idx]["action"]
    prev_soc = expected_prev_soc_kwh(dispatch, idx, start_soc_kwh)
    planned_power_w = slot_power_w(dispatch, idx, prev_soc) if planned_action != "idle" else 0
    action, power_w = battery_save_target(planned_action, planned_power_w, dispatch[idx])
    legacy_planner_hold = planned_action == "hold"
    if legacy_planner_hold:
        # Old persisted plans may still contain the planner's semantic hold (export surplus).
        # Auto cannot realize that trajectory, but routing it as idle keeps the no-forced-write,
        # no-divergence, and no-reassert guards consistent; physical freeze is a distinct action.
        log.warning("legacy planner hold at slot=%s cannot be realized — falling back to idle/auto", slot_time)
        action, power_w = "idle", 0
    save_mode = planned_action == "idle" and action in ("discharge", "freeze")
    # Recovery is applied AFTER all planner/live-load corrections in main(), including
    # no-plan and battery-save paths, so none of them can demote its charge to Auto.

    numbers: dict = {}
    if inputs and idx < len(inputs):
        slot_inputs = inputs[idx]
        if slot_inputs.get("buyPrice") is not None:
            numbers["buy_ore"] = round(float(slot_inputs["buyPrice"]), 2)
        if slot_inputs.get("sellPrice") is not None:
            numbers["sell_ore"] = round(float(slot_inputs["sellPrice"]), 2)

    if planned_action == "idle":
        # For the dashboard's "next action" framing on an idle slot — cheap to compute
        # since dispatch[] is already loaded; first non-idle slot at or after this one.
        for future in dispatch[idx + 1:]:
            if future["action"] != "idle":
                numbers["next_action"] = future["action"]
                numbers["next_action_time"] = future["startTime"]
                break

    solar_skip, detail = False, (
        "legacy planner hold cannot be realized — falling back to idle/auto" if legacy_planner_hold else ""
    )
    if action == "freeze":
        # A real freeze (see inverter_control.freeze_battery). No power target to compute and no
        # divergence guard: the plan's own trajectory for this slot is "SoC does not move", which is
        # exactly what the freeze delivers — closer to the plan than the battery-save emulation it
        # replaces, which deliberately drifts.
        detail = "planned battery-save policy realised with executor freeze (50207/50208/50209 = 0)"
        numbers["freeze_mode"] = "freeze"
    if action == "discharge":
        # The plan's net grid exchange for this slot (+import/−export, DispatchSlot.gridKwh)
        # — lets the dashboard say whether this discharge sells to the grid or just covers
        # the house load, instead of labelling every discharge a grid sale.
        grid_kwh = dispatch[idx].get("gridKwh")
        if grid_kwh is not None:
            numbers["grid_kwh"] = round(float(grid_kwh), 3)
        # A bug here must degrade to the plan-only figure (the old behaviour), not throw —
        # same reasoning as the charge-side guard below.
        if save_mode:
            detail = f"battery-save mode: fixed {power_w} W discharge target instead of Auto"
            numbers["battery_save_w"] = power_w
        else:
            try:
                power_w, detail, live_numbers = live_discharge_power_w(dispatch, idx, now, power_w)
                numbers.update(live_numbers)
            except Exception as exc:  # noqa: BLE001
                log.warning("live-load discharge tracking failed (%s) — using plan-only power", exc)
                detail = f"live-load tracking failed: {exc}"
        if power_w == 0:
            # Live pv/load says no battery discharge is needed right now (e.g. the sun broke
            # through mid-slot and the surplus covers the house). force_discharge(0) would
            # park the inverter in EMS BattCtrl holding 0 W, which BLOCKS self-consumption —
            # MODBUS.md is explicit: idle means return to auto, never 0x303 with 50207=0.
            # Demote this tick to idle/auto (auto charges the surplus into the battery, which
            # is strictly better than parking); the per-tick retarget re-promotes to a real
            # discharge the moment load returns.
            action = "idle"
            detail = f"live surplus covers the planned discharge — auto instead of EMS 0 W ({detail})"
    if action == "charge":
        # A bug in this advisory guard must degrade to "no check" (the old behaviour),
        # not throw — an exception here would put the outer loop into its
        # revert-every-iteration error path over what is only insurance.
        try:
            surplus_w = read_live_solar_surplus_w(now)
            charge_kwh = dispatch[idx]["socAfter"] - prev_soc
            solar_skip, detail, solar_numbers = check_solar_funding(
                inputs, idx, charge_kwh, surplus_w, dispatch[idx])
            numbers.update(solar_numbers)
        except Exception as exc:  # noqa: BLE001
            log.warning("solar-funding check failed (%s) — proceeding without it", exc)
            solar_skip, detail = False, f"solar-funding check failed: {exc}"
        # Fine correction on top of that coarse veto (see live_charge_power_w). Same
        # degrade-to-the-old-behaviour discipline: a bug here must leave the plan-only figure.
        try:
            power_w, charge_detail, charge_numbers = live_charge_power_w(dispatch, idx, now, power_w)
            numbers.update(charge_numbers)
            detail = f"{detail}; {charge_detail}" if detail else charge_detail
        except Exception as exc:  # noqa: BLE001
            log.warning("live-load charge tracking failed (%s) — using plan-only power", exc)
        if power_w == 0:
            # The plan's grid position is already met without charging (e.g. the house is drawing
            # more than forecast, so the planned import is already happening on its own). Auto still
            # absorbs any real surplus, which beats parking EMS BattCtrl at 0 W — the anti-pattern
            # MODBUS.md warns about, and the same demotion the discharge side makes.
            action = "idle"
            detail = f"live charge target is 0 W — auto instead of EMS 0 W ({detail})"
    # A fixed save target deliberately deviates from an idle plan's same-SoC trajectory. Do not
    # apply the stale-plan divergence guard to it; the fixed target is the explicit policy here.
    expected_soc_now = (
        None if save_mode else plan_expected_soc_now(dispatch, idx, prev_soc, now) if action != "idle" else None
    )
    return slot_time, action, power_w, expected_soc_now, solar_skip, detail, numbers


def is_planned_hold(slot: dict) -> bool:
    """Return true only for an idle slot the PLAN itself marked as a battery-save hold.

    The marker is `batterySaveDriftKwh > 0`, which the optimizer sets on exactly the slots it modelled
    the hold on. This used to re-derive the condition from `loadFromGridKwh > 0.05`, and the two
    disagreed in a narrow band: the optimizer decides on the load BEFORE the hold discharge
    (loadRem), the plan publishes what is left AFTER it (loadRem − ~0.055 kWh). For a house load
    between roughly 200 and 420 W the plan therefore carried a hold that the executor declined —
    falling back to Auto, whose self-use logic discharges at whatever the house draws, which is the
    exact opposite of a hold. See OPTIMIZER-AUDIT.md E5.

    A missing marker still means no hold, so a plan row from before the field existed keeps the safe
    idle → Auto behaviour. The load and SoC conditions remain as clamps in battery_save_target; only
    the DECISION moved to the plan.
    """
    try:
        planned_drift = float(slot.get("batterySaveDriftKwh") or 0)
        soc_after = float(slot["socAfter"])
    except (KeyError, TypeError, ValueError):
        return False
    return planned_drift > 0 and soc_after > BATTERY_FLOOR_KWH


def battery_save_target(action: str, power_w: int, slot: dict | None = None):
    """Decide HOW to realise a planned hold.

    The optimizer says "idle" and marks the slot (batterySaveDriftKwh); this picks the mechanism:
      - FREEZE_HOLDS on  -> ("freeze", 0): a real freeze, the optimizer-marked idle policy.
      - otherwise        -> ("discharge", small): the battery-save emulation, which keeps the
        inverter out of Auto (whose self-use logic would discharge at whatever the house draws) at
        the cost of moving ~0.055 kWh/slot out of the battery at the CURRENT price.
      - no marker        -> unchanged idle, i.e. Auto.
    """
    if (action == "idle" and FREEZE_HOLDS and not _freeze_unavailable
            and slot is not None and is_planned_hold(slot)):
        return "freeze", 0
    if action == "idle" and BATTERY_SAVE_W > 0 and slot is not None and is_planned_hold(slot):
        # is_planned_hold already established that this is > 0 (it is the marker it tests), so this
        # only re-reads the magnitude.
        planned_drift = float(slot.get("batterySaveDriftKwh") or 0)
        load_cap = float(slot.get("loadFromGridKwh") or 0) / SLOT_HOURS * 1000
        soc_cap = max(0.0, float(slot["socAfter"]) - BATTERY_FLOOR_KWH) / SLOT_HOURS * 1000
        # batterySaveDriftKwh is the optimizer's planned SoC drop. Convert it back to the
        # inverter-side output target so executor and plan use the same quantized transition.
        planned_target = planned_drift / SLOT_HOURS * 1000 * ONE_WAY_EFF
        target = int(min(BATTERY_SAVE_W, planned_target, load_cap, soc_cap) // 10 * 10)
        return ("discharge", target) if target > 0 else ("idle", 0)
    return action, power_w


def soc_floor_recovery_target(action: str, power_w: int, live_soc_kwh, surplus_w=None,
                              *, recovery_active: bool = False):
    """Override ANY planned action below the recovery threshold, with exit hysteresis.

    Charging must take priority over discharge, freeze, idle and normal optimizer charging.
    When recovery_active, do not resume optimization until SOC_RECOVERY_EXIT_PCT is reached;
    otherwise a BMS discharge-restart interlock can leave the battery stranded at low SoC.
    """
    if live_soc_kwh is None:
        return action, power_w
    entry_kwh = BATTERY_FLOOR_KWH + SOC_FLOOR_GUARD_HEADROOM_KWH
    exit_kwh = BATTERY_KWH * SOC_RECOVERY_EXIT_PCT / 100
    if live_soc_kwh >= entry_kwh and (not recovery_active or live_soc_kwh >= exit_kwh):
        return action, power_w
    target_w = SOC_FLOOR_RECOVERY_W
    if surplus_w is not None and surplus_w > target_w:
        target_w = max(target_w, int(clamp_power_w(surplus_w) // 10 * 10))
    return "charge", target_w


def prioritize_soc_recovery(action: str, power_w: int, expected_soc_kwh, solar_skip: bool,
                            detail: str, numbers: dict, now: datetime):
    """Last-stage safety override, after all optimizer and live-load corrections.

    Also runs for a missing plan. Recovery cannot be vetoed by solar-funding, SoC-drift,
    battery-save or the charge-side grid-position tracker. Live SoC comes from the poller;
    on a stale sample, read the inverter directly rather than letting stale data trigger
    an unsafe Auto/discharge decision.
    """
    global _soc_recovery_active
    live_soc_kwh = read_live_soc_kwh(now)
    if live_soc_kwh is None and ARMED:
        inv = None
        try:
            inv = Inverter()
            live_soc_kwh = inv.soc_pct() / 100 * BATTERY_KWH
        except Exception as exc:  # noqa: BLE001
            log.warning("SoC recovery: live sample stale and direct Modbus read failed: %s", exc)
        finally:
            if inv is not None:
                inv.close()
    if live_soc_kwh is None:
        return action, power_w, expected_soc_kwh, solar_skip, detail, numbers

    entry_kwh = BATTERY_FLOOR_KWH + SOC_FLOOR_GUARD_HEADROOM_KWH
    exit_kwh = BATTERY_KWH * SOC_RECOVERY_EXIT_PCT / 100
    was_active = _soc_recovery_active
    _soc_recovery_active = live_soc_kwh < entry_kwh or (was_active and live_soc_kwh < exit_kwh)
    if not _soc_recovery_active:
        if was_active:
            log.info("SoC recovery complete: %.2f%% (exit %.2f%%)", live_soc_kwh / BATTERY_KWH * 100,
                     SOC_RECOVERY_EXIT_PCT)
        # Prevent the *normal* optimizer, including its idle->Auto and battery-save
        # fallbacks, from draining the battery right back into emergency recovery.
        # Real zero-power hold (caps OPEN) is a separate executor-only action.
        # Keep Auto when there is live PV surplus so the battery can charge freely.
        live_pct = live_soc_kwh / BATTERY_KWH * 100
        if PLANNER_RESERVE_PCT > SOC_FLOOR_PCT and live_pct <= PLANNER_RESERVE_PCT:
            if action == "charge" and not solar_skip:
                return action, power_w, expected_soc_kwh, solar_skip, detail, numbers
            if action == "freeze":
                return action, power_w, expected_soc_kwh, solar_skip, detail, numbers
            surplus = read_live_solar_surplus_w(now)
            if surplus is not None and surplus > 100:
                numbers["reserve_auto_solar_w"] = round(surplus)
                return "idle", 0, None, False, detail, numbers
            numbers["reserve_hold"] = True
            numbers["soc_pct"] = round(live_pct, 2)
            numbers["planner_reserve_pct"] = PLANNER_RESERVE_PCT
            detail = (f"Planner reserve hold at {live_pct:.2f}% <= {PLANNER_RESERVE_PCT:.1f}%: "
                      f"grid covers house, no discharge; previously {action} {power_w} W; {detail}")
            return "reserve_hold", 0, None, False, detail, numbers
        return action, power_w, expected_soc_kwh, solar_skip, detail, numbers

    surplus_w = read_live_solar_surplus_w(now)
    _, recovery_w = soc_floor_recovery_target("idle", 0, live_soc_kwh, surplus_w,
                                              recovery_active=True)
    soc_pct = live_soc_kwh / BATTERY_KWH * 100
    numbers["soc_recovery"] = True
    numbers["soc_pct"] = round(soc_pct, 2)
    numbers["soc_recovery_exit_pct"] = SOC_RECOVERY_EXIT_PCT
    if not was_active:
        log.warning("SoC recovery activated at %.2f%%; charging until %.2f%%", soc_pct,
                    SOC_RECOVERY_EXIT_PCT)
    detail = (f"SoC recovery {soc_pct:.2f}% -> {SOC_RECOVERY_EXIT_PCT:.1f}%: "
              f"forcing {recovery_w} W charge regardless of optimizer ({action} {power_w} W); {detail}")
    return "charge", recovery_w, None, False, detail, numbers


def apply_target(inv: Inverter, action: str, power_w: int) -> None:
    """Apply one dispatch decision. See the ACTION CONTRACT note beside FORCED_ACTIONS.

    Auto stays the fall-through for anything unrecognised - it is the fail-safe, and guessing
    at an unknown action against a live inverter would be worse. But an unrecognised action is
    still a bug (the plan wanted something this executor cannot do), so it is logged loudly
    instead of passing silently.
    """
    if action == "hold":
        # A planner hold means "do not charge; export the surplus", not physical freeze. Auto
        # cannot preserve that exact intent, but is safer than pretending hold means freeze.
        # New armed plans must not emit planner hold; this explicit fallback handles legacy rows.
        log.warning("legacy planner hold is not a physical freeze — falling back to auto")
        return_to_auto(inv)
    elif action == "charge":
        force_charge(inv, power_w)
    elif action == "discharge":
        force_discharge(inv, power_w)
    elif action == "reserve_hold":
        # Open-cap zero-power EMS hold, probed on reference firmware; unlike Auto it
        # does not drain the battery to serve the house during a no-solar deficit.
        # The physical 12% safety guard remains independent and takes priority.
        if not force_hold(inv, SOC_FLOOR_PCT):
            force_charge(inv, SOC_FLOOR_RECOVERY_W)
    elif action == "freeze":
        # A freeze that the inverter accepts but does not enforce is not a transient: it is the
        # documented pre-mode failure shape (MODBUS.md), i.e. this firmware does not do what the
        # recipe says. freeze_battery has already reverted to auto by the time it raises, so the
        # inverter is in a safe, known state — latch it off and let the next hold slot use the
        # battery-save emulation instead of re-attempting every 15 minutes.
        global _freeze_unavailable
        try:
            freeze_battery(inv)
        except FreezeNotEnforced as exc:
            _freeze_unavailable = True
            log.error("freeze not enforced on this inverter (%s) — falling back to the battery-save "
                      "emulation for the rest of this run; set DISPATCH_FREEZE_HOLDS=0 to stop trying "
                      "entirely, and see scripts/tools/probe_freeze.py", exc)
    else:
        if action not in AUTO_ACTIONS:
            log.warning(
                "unrecognised dispatch action %r - falling back to auto. The plan expected "
                "behaviour this executor does not implement; auto CHARGES from solar surplus, "
                "which may be the opposite of what was planned. See the ACTION CONTRACT note "
                "in this module.",
                action,
            )
        return_to_auto(inv)


def write_heartbeat(now: datetime) -> None:
    """Touched every loop iteration regardless of outcome — see the module docstring's
    SAFETY note on why this, not control_actions freshness, is watchdog.py's liveness
    signal. Atomic write (temp file + rename) matching modbus_poller.py's live.json pattern."""
    try:
        common.write_json_atomic(HEARTBEAT_PATH, {"timestamp": now.isoformat()})
    except OSError as exc:
        # Swallowed on purpose: failing to write the heartbeat must not kill the tick. The
        # resulting staleness is exactly what watchdog.py is for — it will force auto itself.
        log.error("failed to write heartbeat: %s", exc)


def revert_best_effort() -> None:
    try:
        inv = Inverter()
        try:
            return_to_auto(inv)
        finally:
            inv.close()
    except Exception as exc:  # noqa: BLE001
        log.error("revert_best_effort failed: %s", exc)


# ── The apply half ───────────────────────────────────────────────────────────────────────────
#
# decide() has always been a separate, testable function; everything BELOW the decision — fold
# in the solar guard, decide whether this tick warrants a register write at all, run the
# SoC-staleness guard, apply, log, and recover from a failed apply — used to live inline in
# main()'s loop body: ~165 lines at five levels of nesting, and (per this module's test file)
# the one part of the executor with no unit coverage at all. Split out 2026-08-07 with the
# logic bit-identical; scripts/tests/test_dispatch_loop.py now covers each piece.


def effective_target(action: str, power_w: int, solar_skip: bool):
    """Fold the solar-funding guard into the target actually applied.

    Returns (action, power_w, solar_skipped). decide() re-runs check_solar_funding against a
    fresh live.json on EVERY call, so folding the result into the target here — rather than
    treating it as a side note — is what makes a mid-slot cloud pass (or a mid-slot solar
    recovery) register as a real target CHANGE and get applied immediately, instead of sitting
    on the previously-applied setpoint until the slot boundary or the next REASSERT_S. During a
    forced charge that gap would be up to 5 minutes of uncapped grid import at full price.
    """
    solar_skipped = action == "charge" and solar_skip
    return ("idle" if solar_skipped else action, 0 if solar_skipped else power_w, solar_skipped)


def needs_apply(target, last_target, effective_action: str, effective_power: int,
                due_for_reassert: bool, *, soc_recovery: bool = False) -> bool:
    """Does this tick warrant actually writing a register?

    Three rules, in tension:
      - A genuine target change always applies — including the very first decision after a
        restart, since last_target starts as None. That is what self-heals a setpoint a crashed
        prior instance left forced.
      - An unchanged target is re-asserted on REASSERT_S, but ONLY when it is non-idle. General
        mode doesn't decay on its own, so re-poking it on a timer would blindly overwrite a mode
        the owner set by hand in the inverter's own app.
      - A same-slot, same-action retarget smaller than LIVE_LOAD_DEADBAND_W waits for the next
        reassert. Live-load tracking moves the bucketed power on almost every iteration (first
        night on the reference install: a write every ~81 s) and chasing ±100-200 W of ordinary
        load noise buys nothing. A real event (a heat pump starting: thousands of W) clears the
        deadband immediately, so reaction latency to the events that tracking exists for is
        unaffected. The caller keeps the APPLIED power in last_target while suppressing, so slow
        creep accumulates against it and still applies once it sums past the band.
    """
    # Keep the deliberate battery-save direction through an economic mid-slot replan,
    # but NEVER block a recovery charge when the live SoC has crossed the safety threshold.
    mid_slot_drift_to_charge = (
        last_target is not None
        and target[0] == last_target[0]
        and last_target[1] == "discharge"
        and target[1] == "charge"
    )
    if mid_slot_drift_to_charge and not soc_recovery:
        return False

    small_power_move = (
        last_target is not None
        and target[:2] == last_target[:2]
        and effective_action != "idle"
        and abs(effective_power - last_target[2]) < LIVE_LOAD_DEADBAND_W
    )
    return (target != last_target and not small_power_move) or (
        due_for_reassert and effective_action != "idle"
    )


# Outcomes after which the inverter is NOT at `target` — the apply failed and the loop either
# reverted to auto or could not confirm what state it left behind. Recording `target` for these
# would tell needs_apply() the target is already satisfied.
_UNAPPLIED_OUTCOMES = ("error_reverted", "error_revert_failed")


def next_last_target(outcome: str, target):
    """What to record as the last-applied target, given how the apply actually turned out.

    `last_target` means "what the inverter is currently set to", and needs_apply() reads it that
    way. So it may only be recorded when the apply really landed.

    Both error outcomes therefore record None, which makes the next tick retry:
      - error_revert_failed: the revert failed too, so the inverter may still be running the
        PREVIOUS slot's forced setpoint. Recording an unchanged idle target here would leave that
        stale setpoint running until the next slot boundary — or forever when slot_time is None.
      - error_reverted: the fail-safe revert SUCCEEDED, so the inverter is in auto — which is
        precisely why `target` is the wrong thing to record. This path used to fall through to the
        success branch, and the bug is worth describing because it is invisible until the error
        rate rises: the loop believed a forced target was live while the inverter sat in auto, and
        since needs_apply() only re-asserts an unchanged non-idle target on REASSERT_S, recovery
        waited the FULL reassert interval. Measured on the reference deployment during a period of
        Modbus link degradation: error_reverted rows recovered in 294-309 s while
        error_revert_failed rows recovered on the next tick at 16-21 s — the milder failure
        recovering slower than the worse one. Both now retry on the next tick.

    The SKIP outcomes deliberately keep `target` and must not be added here. skipped_divergence
    and skipped_solar_shortfall are not failures: the guard chose auto on purpose and has already
    asked for a fresh plan (maybe_request_replan), so retrying every tick would just re-trip the
    same guard and log a row every LOOP_INTERVAL_S. They wait for a changed target — a new plan,
    or solar recovering — which is the intended behaviour.

    NOTE the cost of retrying on the next tick during a SUSTAINED outage: one control_actions row
    per LOOP_INTERVAL_S rather than per REASSERT_S. Accepted because that is already
    error_revert_failed's behaviour, and because a transient Modbus blip typically clears on the
    very next tick. healthcheck.py's alert de-duplication is what keeps a long outage from
    becoming a push storm.
    """
    return None if outcome in _UNAPPLIED_OUTCOMES else target


def check_soc_divergence(inv: Inverter, expected_soc_kwh: float, slot_time, action: str,
                          numbers: dict):
    """Compare live SoC against the plan's expectation for THIS MOMENT; returns (skip, detail).

    Records the drift figures in `numbers` in place on EVERY charge/discharge decision, not just
    ones that trip SOC_DIVERGENCE_KWH — that everyday distribution is what eventually lets the
    threshold be tuned from real control_actions data rather than a guess. decide()'s docstring
    names main() as the writer of these two keys; this is that writer.

    Two thresholds fire here, deliberately different: REPLAN_DRIFT_KWH (tighter) just ASKS for a
    fresh plan, so that by the time drift would reach SOC_DIVERGENCE_KWH a live-anchored plan
    usually already exists; SOC_DIVERGENCE_KWH (wider) is the one that actually refuses to force
    a power target computed from a stale baseline.
    """
    actual_soc_kwh = inv.soc_pct() / 100 * BATTERY_KWH
    drift = abs(actual_soc_kwh - expected_soc_kwh)
    detail = (f"expected {expected_soc_kwh:.2f} kWh, actual "
              f"{actual_soc_kwh:.2f} kWh, drift {drift:.2f} kWh")
    numbers["soc_drift_kwh"] = round(drift, 3)
    numbers["soc_drift_limit_kwh"] = SOC_DIVERGENCE_KWH
    if abs(drift) > REPLAN_DRIFT_KWH:
        maybe_request_replan("drift")
    if drift > SOC_DIVERGENCE_KWH:
        log.warning(
            "slot=%s action=%s planned from %.2f kWh but actual is %.2f kWh "
            "(drift %.2f kWh > %.2f kWh) — plan is stale, falling back to "
            "auto instead of forcing a power target computed from the wrong "
            "starting point", slot_time, action, expected_soc_kwh,
            actual_soc_kwh, drift, SOC_DIVERGENCE_KWH,
        )
        return True, detail
    return False, detail


def apply_decision(con: sqlite3.Connection, slot_time, action: str, power_w: int,
                    expected_soc_kwh, solar_skipped_now: bool, detail: str, numbers: dict,
                    loop_in_auto: bool):
    """Run the guards, write the register, log the control_actions row. Returns
    (outcome, loop_in_auto).

    Never raises: an apply failure reverts to auto as its own fail-safe and is reported through
    `outcome`, because the caller's own error path is for a failed DECISION, not a failed apply.
    Ordering inside the failure path matters and is load-bearing — revert FIRST, log second, so
    a telemetry.db write error (shared WAL, contention is realistic) can never skip the revert.
    """
    inv = None  # opened lazily — an idle→idle slot boundary needs no connection
    applied_action, applied_power = action, power_w
    outcome = "applied"
    try:
        if solar_skipped_now:
            # Live solar can't fund this charge the way the plan assumed — forcing it would buy
            # the difference from the grid at full price. Auto still charges from whatever
            # surplus actually exists, so skipping wastes no real solar.
            log.warning("slot=%s planned charge can't be solar-funded as "
                        "planned (%s) — falling back to auto instead of "
                        "buying the difference", slot_time, detail)
            applied_action, applied_power = "idle", 0
            outcome = "skipped_solar_shortfall"
            maybe_request_replan("solar_shortfall")
        elif action != "idle" and expected_soc_kwh is not None:
            inv = Inverter()
            # Appended to the funding detail (charge slots) rather than replacing it, so one
            # row carries both guards.
            skip, soc_detail = check_soc_divergence(inv, expected_soc_kwh, slot_time, action, numbers)
            detail = f"{detail}; {soc_detail}" if detail else soc_detail
            if skip:
                applied_action, applied_power = "idle", 0
                outcome = "skipped_divergence"
                maybe_request_replan("divergence_skip")
        # Never stomp an owner-set mode: when the loop's own last write already left the
        # inverter in auto, an idle decision (planned, or a guard demotion above) needs no
        # register write. The row below is STILL logged — it carries the skip outcomes, and
        # armed-coverage measurement reads row cadence as "loop alive" (silence >
        # ARMED_SEGMENT_CAP_MS counts as down — lib/oracle.ts).
        if not (applied_action == "idle" and loop_in_auto):
            if inv is None:
                inv = Inverter()
            apply_target(inv, applied_action, applied_power)
            # Readback on the two paths whose register behaviour is least certain: the battery-save
            # emulation, and a freeze (whose three-register combination is not probe-verified — the
            # expected failure is "stored but not enforced", which only a readback plus the measured
            # battery power can distinguish).
            if (applied_action in ("freeze", "reserve_hold")
                    or (applied_action == "discharge" and "battery_save_w" in numbers)
                    or numbers.get("soc_recovery")):
                try:
                    log_control_readback(con, slot_time, applied_action, applied_power, inv)
                except Exception as exc:  # noqa: BLE001
                    # Diagnostic-only: never undo a successful write because SQLite is busy.
                    log.warning("control readback logging failed for slot=%s: %s", slot_time, exc)
            if numbers.get("soc_recovery") and applied_action == "charge":
                # Modbus setpoint readback is not proof that the battery is actually charging.
                # Give the BMS time to respond; monitor_soc_recovery_enforcement checks the
                # following two minutes of independent poller readings before reporting failure.
                try:
                    battery_w = inv.battery_power_w()
                    numbers["battery_w_after_apply"] = battery_w
                    if battery_w >= -RECOVERY_RESPONSE_TOL_W:
                        outcome = "accepted_pending_physical_confirmation"
                except Exception as exc:  # noqa: BLE001 — feedback must not cancel recovery
                    numbers["battery_feedback_error"] = str(exc)
                    outcome = "accepted_pending_physical_confirmation"
        loop_in_auto = applied_action in AUTO_ACTIONS
        log_action(con, slot_time, applied_action, applied_power, outcome, detail, detail_json=numbers)
        log.info("slot=%s action=%s power=%dW armed=%s -> %s%s",
                  slot_time, applied_action, applied_power, ARMED, outcome,
                  f" ({detail})" if detail else "")
    except Exception as exc:  # noqa: BLE001
        log.error("apply failed for slot=%s action=%s power=%dW: %s — "
                  "falling back to auto", slot_time, applied_action, applied_power, exc)
        try:
            if inv is None:
                inv = Inverter()
            return_to_auto(inv)
            outcome, detail = "error_reverted", str(exc)
            loop_in_auto = True
        except Exception as exc2:  # noqa: BLE001
            log.error("fail-safe return_to_auto also failed: %s", exc2)
            outcome, detail = "error_revert_failed", f"{exc} | revert also failed: {exc2}"
            loop_in_auto = False  # inverter state unconfirmed — next idle must write
        try:
            log_action(con, slot_time, applied_action, applied_power, outcome, detail, detail_json=numbers)
        except Exception as exc3:  # noqa: BLE001
            log.error("log_action failed after apply error: %s", exc3)
    finally:
        if inv is not None:
            inv.close()
    return outcome, loop_in_auto


def monitor_soc_recovery_enforcement(con: sqlite3.Connection, now: datetime, target,
                                     last_target, last_write_monotonic: float,
                                     numbers: dict) -> bool:
    """Report a charge that was register-accepted but physically absent for two minutes.

    Three independent poller samples are required, not one instantaneous read immediately
    after mode switching. This is observational only: the commanded protective charge stays
    active and may start later when the BMS releases its interlock.
    """
    global _last_recovery_nonresponse_report
    if (not ARMED or not numbers.get("soc_recovery") or target != last_target
            or target[1] != "charge"):
        return False
    elapsed = time.monotonic() - last_write_monotonic
    if elapsed < RECOVERY_RESPONSE_CHECK_S:
        return False
    if time.monotonic() - _last_recovery_nonresponse_report < REASSERT_S:
        return False
    try:
        since = (now - timedelta(seconds=RECOVERY_RESPONSE_CHECK_S)).isoformat()
        count, min_power = con.execute(
            """SELECT COUNT(*), MIN(battery_w) FROM readings
               WHERE timestamp >= ? AND timestamp <= ? AND battery_w IS NOT NULL""",
            (since, now.isoformat()),
        ).fetchone()
        if count < 3 or min_power is None or min_power < -RECOVERY_RESPONSE_TOL_W:
            return False
        _last_recovery_nonresponse_report = time.monotonic()
        detail = (f"recovery charge {target[2]} W accepted but {count} poller samples over "
                  f"{RECOVERY_RESPONSE_CHECK_S}s show no charging (min battery {min_power} W)")
        log.error("slot=%s %s", target[0], detail)
        log_action(con, target[0], "charge", target[2], "command_accepted_but_not_enforced",
                   detail, detail_json={**numbers, "battery_w_min": min_power,
                                       "sample_count": count})
        return True
    except Exception as exc:  # noqa: BLE001 — diagnostics must never stop dispatch
        log.warning("SoC recovery physical verification failed: %s", exc)
        return False


def apply_import_limit() -> None:
    """Write the site import limit once at startup, if one is configured. Never raises.

    Deliberately best-effort and non-fatal: this is an EXTRA bound on top of bounds that already
    exist (clamp_power_w on the battery's own power, the planner's grid-cap feasibility check), so
    failing to set it must not stop the controller from running with the protection it already had.
    A failure is logged at ERROR precisely because it is silent otherwise — the registers are not
    probe-verified on the reference unit, and "stored but not enforced" is a realistic outcome.

    Not reverted on shutdown, unlike the forced setpoint: a fuse-level cap is a property of the
    installation, not of this process, and clearing it on exit would remove the protection exactly
    when nothing is left supervising the inverter. Use `inverter_control.py import-limit off` to
    take it away deliberately.
    """
    if IMPORT_LIMIT_W <= 0:
        return
    log.info("applying site import limit %d W (registers 50007/50009), armed=%s", IMPORT_LIMIT_W, ARMED)
    inv = None
    try:
        inv = Inverter()
        set_import_limit(inv, IMPORT_LIMIT_W)
    except Exception as exc:  # noqa: BLE001 — an advisory extra bound must never block startup
        log.error("failed to apply site import limit %d W: %s — continuing with the software "
                  "bounds only (clamp_power_w + the planner's grid cap)", IMPORT_LIMIT_W, exc)
    finally:
        if inv is not None:
            inv.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    con = init_db(DB_PATH)
    log.info("Dispatch loop starting. ARMED=%s interval=%ds reassert=%ds",
              ARMED, LOOP_INTERVAL_S, REASSERT_S)
    if not ARMED:
        log.warning("Running DISARMED — decisions are computed and logged to "
                     "control_actions, but no register is actually written. "
                     "Safe for shadow-mode validation against the live plan.")
    apply_import_limit()

    last_target = None  # (slot_time, effective_action, effective_power) — post-guard, as applied
    # True when the loop's own last confirmed write/revert left the inverter in auto (or an
    # idle decision needed no write because of this flag). Gates idle register writes: an
    # idle decision while this is True logs its control_actions row but writes nothing, so
    # a mode the owner set by hand is never stomped by idle slot boundaries. Starts False
    # (inverter state unknown) so the first idle decision after a restart still writes auto
    # once — that's the self-heal for a setpoint a crashed prior instance left forced.
    loop_in_auto = False
    last_write_monotonic = 0.0
    # Set the moment decide() fails and we've reverted for it; cleared the moment decide()
    # succeeds again. Without this, a PERSISTENT decide() failure (e.g. a corrupt
    # dispatch_json row that never gets fixed) would call revert_best_effort() — and thus
    # write registers — on every single 60s tick forever, which once armed means repeatedly
    # stomping any mode the owner set by hand via the inverter's own app. One revert per
    # failure streak is enough; the fail-safe state doesn't need re-asserting on a timer any
    # more than a normal idle slot does (see the reassert comment below).
    decide_failure_reverted = False
    stop = {"flag": False}

    def _request_shutdown(*_args):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    while not stop["flag"]:
        iteration_now = datetime.now(UTC)
        try:
            slot_time, action, power_w, expected_soc_kwh, solar_skip, detail, numbers = decide(con, iteration_now)
            # decide() just succeeded — any prior failure streak is over. Reset last_target
            # to None too, matching the restart self-heal behaviour: the failure streak's
            # revert already forced auto, so whatever decide() computes now (even if it's the
            # exact same target as before the streak) must still be applied, not skipped
            # because it happens to equal a last_target from before the forced revert.
            if decide_failure_reverted:
                decide_failure_reverted = False
                last_target = None
            action, power_w, expected_soc_kwh, solar_skip, detail, numbers = prioritize_soc_recovery(
                action, power_w, expected_soc_kwh, solar_skip, detail, numbers, iteration_now)
            # Battery-only L3 coordination sits AFTER the SoC recovery decision so a
            # low-SoC recovery charge always wins; default 0 means no behaviour change.
            try:
                action, power_w, expected_soc_kwh, solar_skip, detail, numbers = coordinate_l3_battery(
                    action, power_w, expected_soc_kwh, solar_skip, detail, numbers, iteration_now)
            except Exception as exc:  # noqa: BLE001 — advisory battery policy, never crash dispatch
                log.warning("L3 battery coordination failed (%s) — original dispatch retained", exc)
            effective_action, effective_power, solar_skipped_now = effective_target(
                action, power_w, solar_skip)
            target = (slot_time, effective_action, effective_power)
            due_for_reassert = (time.monotonic() - last_write_monotonic) >= REASSERT_S
            if needs_apply(target, last_target, effective_action, effective_power, due_for_reassert,
                           soc_recovery=bool(numbers.get("soc_recovery"))):
                outcome, loop_in_auto = apply_decision(
                    con, slot_time, action, power_w, expected_soc_kwh, solar_skipped_now,
                    detail, numbers, loop_in_auto)
                # Only record the target when the inverter is actually AT it — see
                # next_last_target() for why both error outcomes (not just the failed revert)
                # must leave it unset so the next tick retries.
                last_target = next_last_target(outcome, target)
                last_write_monotonic = time.monotonic()
            monitor_soc_recovery_enforcement(con, iteration_now, target, last_target,
                                             last_write_monotonic, numbers)
        except Exception as exc:  # noqa: BLE001
            # decide() itself failed (e.g. a corrupt dispatch_json row) — we don't know
            # the intended action, but we must not leave a previous slot's forced setpoint
            # running indefinitely just because this iteration couldn't compute a new one.
            # Only revert on the FIRST failure of a streak — see decide_failure_reverted's
            # definition above for why a persistent failure must not re-revert every tick.
            if not decide_failure_reverted:
                log.error("loop iteration failed: %s — falling back to auto", exc)
                revert_best_effort()
                decide_failure_reverted = True
            else:
                log.error("loop iteration still failing (%s) — already reverted, not re-writing", exc)

        # Always touched, success or failure — see write_heartbeat's docstring.
        write_heartbeat(datetime.now(UTC))

        for _ in range(LOOP_INTERVAL_S):
            if stop["flag"]:
                break
            time.sleep(1)

    log.info("Shutting down — reverting to auto.")
    revert_best_effort()


if __name__ == "__main__":
    main()
