"""Tests for the decision-math and the apply half of scripts/services/dispatch_loop.py —
slot indexing, SoC-drift interpolation, the solar-funding/live-load-tracking guards, and (since
2026-08-07) the write-gating and apply/recover path that used to be inline in main().

Still deliberately does NOT test decide()/main() end-to-end: decide() needs a populated
telemetry.db and main() is an infinite loop over wall-clock time. What changed is that the
non-trivial logic main() used to hold inline — effective_target / needs_apply /
check_soc_divergence / apply_decision — is now extracted and testable, so the guards and the
two-level failure recovery are no longer verified only by reading them. Verify the
live-behaviour side against your deployment's own control_actions rows.

Run: python3 -m unittest scripts.tests.test_dispatch_loop -v   (from the repo root)
     or: python3 -m unittest discover -s scripts/tests
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))

from fakes import install_pymodbus_stub  # noqa: E402

install_pymodbus_stub()

import dispatch_loop as dl  # noqa: E402
import inverter_control as ic  # noqa: E402

STOCKHOLM = ZoneInfo("Europe/Stockholm")
UTC = ZoneInfo("UTC")


def make_dispatch(n: int, start: str = "2026-01-15T00:00:00", soc_start: float = 10.0, soc_step: float = -0.5):
    soc = soc_start
    slots = []
    for i in range(n):
        soc += soc_step
        slots.append({"startTime": start, "socAfter": round(soc, 3), "gridKwh": 0.1 * i})
    return slots


def stockholm(iso: str) -> datetime:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=STOCKHOLM)


class SlotIndexForInstantTests(unittest.TestCase):
    def test_at_the_first_slot_start(self):
        dispatch = make_dispatch(4)
        now = stockholm("2026-01-15T00:00:00").astimezone(UTC)
        self.assertEqual(dl.slot_index_for_instant(dispatch, now), 0)

    def test_partway_into_a_later_slot(self):
        dispatch = make_dispatch(4)
        now = stockholm("2026-01-15T00:00:00").astimezone(UTC) + timedelta(minutes=16)
        self.assertEqual(dl.slot_index_for_instant(dispatch, now), 1)

    def test_before_the_plan_starts_returns_none(self):
        dispatch = make_dispatch(4)
        now = stockholm("2026-01-15T00:00:00").astimezone(UTC) - timedelta(minutes=1)
        self.assertIsNone(dl.slot_index_for_instant(dispatch, now))

    def test_past_the_end_of_the_plan_returns_none(self):
        dispatch = make_dispatch(4)  # covers 1 hour
        now = stockholm("2026-01-15T00:00:00").astimezone(UTC) + timedelta(hours=2)
        self.assertIsNone(dl.slot_index_for_instant(dispatch, now))

    def test_empty_dispatch_returns_none(self):
        self.assertIsNone(dl.slot_index_for_instant([], datetime.now(UTC)))

    def test_anchors_on_elapsed_time_across_a_dst_spring_forward_gap(self):
        # 2026-03-29 is a Stockholm spring-forward day (02:00 -> 03:00 doesn't exist).
        # dispatch[0] starts just before the gap; real elapsed time must still land on
        # slot 1 rather than crashing or misindexing on the missing wall-clock hour.
        dispatch = make_dispatch(4, start="2026-03-29T01:45:00")
        now = stockholm("2026-03-29T01:45:00").astimezone(UTC) + timedelta(minutes=20)
        self.assertEqual(dl.slot_index_for_instant(dispatch, now), 1)


class ExpectedPrevSocKwhTests(unittest.TestCase):
    def test_first_slot_uses_the_plans_own_start_soc(self):
        dispatch = make_dispatch(4)
        self.assertEqual(dl.expected_prev_soc_kwh(dispatch, 0, start_soc_kwh=12.34), 12.34)

    def test_later_slot_uses_the_previous_slots_socafter(self):
        dispatch = make_dispatch(4)
        self.assertEqual(dl.expected_prev_soc_kwh(dispatch, 2, start_soc_kwh=12.34), dispatch[1]["socAfter"])


class PlanExpectedSocNowTests(unittest.TestCase):
    def setUp(self):
        self.dispatch = make_dispatch(4, soc_start=10.0, soc_step=-1.0)  # slot idx0 socAfter=9.0
        self.slot_start_utc = stockholm("2026-01-15T00:00:00").astimezone(UTC)

    def test_at_slot_start_returns_prev_soc(self):
        got = dl.plan_expected_soc_now(self.dispatch, 0, prev_soc=10.0, now=self.slot_start_utc)
        self.assertAlmostEqual(got, 10.0)

    def test_at_slot_end_returns_socafter(self):
        now = self.slot_start_utc + timedelta(minutes=15)
        got = dl.plan_expected_soc_now(self.dispatch, 0, prev_soc=10.0, now=now)
        self.assertAlmostEqual(got, self.dispatch[0]["socAfter"])

    def test_midpoint_interpolates_linearly(self):
        now = self.slot_start_utc + timedelta(minutes=7.5)
        got = dl.plan_expected_soc_now(self.dispatch, 0, prev_soc=10.0, now=now)
        self.assertAlmostEqual(got, (10.0 + self.dispatch[0]["socAfter"]) / 2, places=3)

    def test_before_slot_start_clamps_to_prev_soc(self):
        now = self.slot_start_utc - timedelta(minutes=5)
        got = dl.plan_expected_soc_now(self.dispatch, 0, prev_soc=10.0, now=now)
        self.assertAlmostEqual(got, 10.0)

    def test_past_slot_end_clamps_to_socafter(self):
        now = self.slot_start_utc + timedelta(minutes=45)
        got = dl.plan_expected_soc_now(self.dispatch, 0, prev_soc=10.0, now=now)
        self.assertAlmostEqual(got, self.dispatch[0]["socAfter"])


class SlotPowerWTests(unittest.TestCase):
    def test_discharge_magnitude_matches_the_planned_soc_delta(self):
        dispatch = [{"socAfter": 9.75}]
        # 0.25 kWh drop over a 0.25 h slot = 1000 W
        self.assertEqual(dl.slot_power_w(dispatch, 0, prev_soc=10.0), 1000)

    def test_clamps_to_the_hardware_power_limits(self):
        dispatch = [{"socAfter": -900.0}]  # absurd delta, must clamp not overflow
        self.assertEqual(dl.slot_power_w(dispatch, 0, prev_soc=10.0), min(ic.BATTERY_MAX_W, ic.GRID_CAP_W))


class CheckSolarFundingTests(unittest.TestCase):
    def test_no_inputs_for_slot_reports_unable_to_check(self):
        skip, detail, numbers = dl.check_solar_funding([], 0, charge_kwh=2.0, surplus_w=500)
        self.assertFalse(skip)
        self.assertEqual(numbers, {})

    def test_no_live_surplus_skips_conservatively_past_the_threshold(self):
        inputs = [{"solarKwh": 3.0, "consumptionKwh": 0.0}]  # plan expects the full charge from solar
        skip, _detail, numbers = dl.check_solar_funding(inputs, 0, charge_kwh=3.0, surplus_w=None)
        self.assertTrue(skip)
        self.assertGreater(numbers["solar_shortfall_kwh"], numbers["solar_shortfall_limit_kwh"])

    def test_no_live_surplus_but_plan_barely_uses_solar_does_not_skip(self):
        inputs = [{"solarKwh": 0.05, "consumptionKwh": 0.0}]
        skip, _detail, _numbers = dl.check_solar_funding(inputs, 0, charge_kwh=3.0, surplus_w=None)
        self.assertFalse(skip)

    def test_live_surplus_matching_the_plans_own_solar_assumption_does_not_skip(self):
        # plan assumed 3.0 kWh of solar surplus funds this charge (the rest of need_kwh,
        # inflated by round-trip losses, was always going to come from the grid) — live
        # surplus matching that SAME 3.0 kWh means reality matched the plan exactly.
        inputs = [{"solarKwh": 3.0, "consumptionKwh": 0.0}]
        surplus_w = 3.0 / dl.SLOT_HOURS * 1000
        skip, _detail, numbers = dl.check_solar_funding(inputs, 0, charge_kwh=3.0, surplus_w=surplus_w)
        self.assertFalse(skip)
        self.assertAlmostEqual(numbers["solar_shortfall_kwh"], 0.0, places=2)

    def test_live_surplus_far_below_plan_skips(self):
        inputs = [{"solarKwh": 3.0, "consumptionKwh": 0.0}]  # plan assumed solar covers it all
        skip, _detail, numbers = dl.check_solar_funding(inputs, 0, charge_kwh=3.0, surplus_w=0.0)
        self.assertTrue(skip)
        self.assertGreater(numbers["solar_shortfall_kwh"], 0)

    # ── The plan's own funding attribution (OPTIMIZER-AUDIT.md E1) ────────────────────────────────

    def test_prefers_the_plans_own_grid_attribution_over_re_deriving_it(self):
        # The DP solved against consumptionKwh x LOAD_FORECAST_MARGIN while inputs_json records the
        # HONEST unmargined forecast, so netting solar against inputs overstates the planned solar
        # surplus and therefore OVERSTATES the shortfall -- vetoing charges the plan had already
        # accounted for as grid-funded. Here inputs claim the charge is fully solar-funded while the
        # plan says it is entirely grid-funded; with zero live surplus that is reality matching the
        # plan exactly, so there is nothing to skip.
        inputs = [{"solarKwh": 3.5, "consumptionKwh": 0.0}]
        dispatch_slot = {"gridToBatteryKwh": 3.0 / dl.ONE_WAY_EFF}
        skip, detail, numbers = dl.check_solar_funding(
            inputs, 0, charge_kwh=3.0, surplus_w=0.0, dispatch_slot=dispatch_slot)
        self.assertFalse(skip)
        self.assertAlmostEqual(numbers["solar_shortfall_kwh"], 0.0, places=2)
        self.assertIn("per plan", detail)
        # …and without the attribution the same situation trips the guard, which is the bug.
        skip_rederived, _d, _n = dl.check_solar_funding(inputs, 0, charge_kwh=3.0, surplus_w=0.0)
        self.assertTrue(skip_rederived)

    def test_falls_back_to_the_forecast_when_the_plan_row_predates_the_field(self):
        inputs = [{"solarKwh": 3.0, "consumptionKwh": 0.0}]
        skip, detail, _numbers = dl.check_solar_funding(
            inputs, 0, charge_kwh=3.0, surplus_w=0.0, dispatch_slot={"socAfter": 5.0})
        self.assertTrue(skip)
        self.assertIn("per forecast", detail)

    def test_clamps_a_plan_attribution_larger_than_the_charge_itself(self):
        # A plan whose attribution disagrees with its own SoC trajectory must not produce a grid
        # share bigger than the charge it is part of, or planned_solar_kwh would go negative.
        inputs = [{"solarKwh": 0.0, "consumptionKwh": 0.0}]
        skip, _detail, numbers = dl.check_solar_funding(
            inputs, 0, charge_kwh=1.0, surplus_w=0.0, dispatch_slot={"gridToBatteryKwh": 99.0})
        self.assertFalse(skip)
        self.assertLessEqual(numbers["solar_shortfall_kwh"], 0.0)

    def test_no_attribution_and_no_inputs_reports_unable_to_check(self):
        skip, detail, numbers = dl.check_solar_funding([], 0, charge_kwh=2.0, surplus_w=500,
                                                      dispatch_slot={"socAfter": 5.0})
        self.assertFalse(skip)
        self.assertEqual(numbers, {})
        self.assertIn("not possible", detail)


class LiveDischargePowerWTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.live_json_path = Path(self.tmpdir.name) / "live.json"
        self._orig_path = dl.LIVE_JSON_PATH
        self._orig_enabled = dl.LIVE_LOAD_TRACKING_ENABLED
        dl.LIVE_JSON_PATH = str(self.live_json_path)
        dl.LIVE_LOAD_TRACKING_ENABLED = True

    def tearDown(self):
        dl.LIVE_JSON_PATH = self._orig_path
        dl.LIVE_LOAD_TRACKING_ENABLED = self._orig_enabled
        self.tmpdir.cleanup()

    def write_live_json(self, now: datetime, pv_w: float, house_load_w: float):
        self.live_json_path.write_text(json.dumps({
            "timestamp": now.isoformat(),
            "pv_w": pv_w,
            "house_load_w": house_load_w,
        }))

    def test_disabled_falls_back_to_planned_power_unchanged(self):
        dl.LIVE_LOAD_TRACKING_ENABLED = False
        dispatch = [{"gridKwh": 0.0}]
        power_w, detail, numbers = dl.live_discharge_power_w(dispatch, 0, datetime.now(UTC), planned_power_w=1200)
        self.assertEqual(power_w, 1200)
        self.assertEqual(numbers, {})
        self.assertIn("disabled", detail)

    def test_missing_live_json_falls_back_to_planned_power(self):
        dispatch = [{"gridKwh": 0.0}]
        power_w, detail, numbers = dl.live_discharge_power_w(dispatch, 0, datetime.now(UTC), planned_power_w=1200)
        self.assertEqual(power_w, 1200)
        self.assertEqual(numbers, {})
        self.assertIn("missing/stale", detail)

    def test_load_spike_beyond_plan_increases_discharge(self):
        now = datetime.now(UTC)
        # plan assumed net export of 0 (gridKwh=0 over 0.25h); live load is much higher than
        # live PV, so the battery must cover the extra load beyond what was planned.
        self.write_live_json(now, pv_w=500, house_load_w=3500)
        dispatch = [{"gridKwh": 0.0}]
        power_w, _detail, numbers = dl.live_discharge_power_w(dispatch, 0, now, planned_power_w=1000)
        # AC need = house_load - pv - planned_grid_w = 3500 - 500 - 0 = 3000 W. Register 50207 is
        # BATTERY-side, so the cells must give up 3000 / ONE_WAY_EFF to land 3000 W at the AC port;
        # writing the AC figure straight through under-delivered by the conversion loss, and that
        # shortfall leaks to the grid at full retail price -- the exact thing this tracking exists
        # to prevent. Rounded to LIVE_LOAD_ROUND_W.
        expected = round(3000 / dl.ONE_WAY_EFF / dl.LIVE_LOAD_ROUND_W) * dl.LIVE_LOAD_ROUND_W
        self.assertEqual(power_w, expected)
        self.assertGreater(power_w, 3000)
        self.assertEqual(numbers["required_ac_w"], 3000)
        self.assertEqual(numbers["live_house_load_w"], 3500)
        self.assertEqual(numbers["live_pv_w"], 500)

    def test_an_over_forecast_load_lowers_the_target_symmetrically(self):
        # The mirror case, and the reason the AC->battery conversion is only applied to a positive
        # need: a negative required_ac_w means the surplus covers the house, and the caller demotes
        # the tick to idle/auto rather than scaling a negative number through an efficiency.
        now = datetime.now(UTC)
        self.write_live_json(now, pv_w=4000, house_load_w=500)
        dispatch = [{"gridKwh": 0.0}]
        power_w, _detail, numbers = dl.live_discharge_power_w(dispatch, 0, now, planned_power_w=1000)
        self.assertEqual(numbers["required_ac_w"], -3500)
        self.assertEqual(power_w, 0)  # clamp_power_w floors at 0; decide() then demotes to idle


class LiveChargePowerWTests(unittest.TestCase):
    """The charge-side mirror of live-load tracking (OPTIMIZER-AUDIT.md E2).

    Charge slots had only the binary solar-funding veto — the plan's full power, or Auto — so live
    solar above forecast exported instead of charging, and live load above forecast landed on top of
    the commanded charge at the meter.
    """

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.live_json_path = Path(self.tmpdir.name) / "live.json"
        self._orig_path = dl.LIVE_JSON_PATH
        self._orig_mode = dl.LIVE_LOAD_TRACKING_CHARGE
        dl.LIVE_JSON_PATH = str(self.live_json_path)
        dl.LIVE_LOAD_TRACKING_CHARGE = "1"

    def tearDown(self):
        dl.LIVE_JSON_PATH = self._orig_path
        dl.LIVE_LOAD_TRACKING_CHARGE = self._orig_mode
        self.tmpdir.cleanup()

    def write_live_json(self, now: datetime, pv_w: float, house_load_w: float):
        self.live_json_path.write_text(json.dumps({
            "timestamp": now.isoformat(), "pv_w": pv_w, "house_load_w": house_load_w,
        }))

    def test_ships_in_shadow_mode_by_default(self):
        # The default must not change any deployment's behaviour on upgrade: compute, log, apply
        # nothing.
        self.assertEqual(
            os.environ.get("DISPATCH_LIVE_LOAD_TRACKING_CHARGE", "shadow"), "shadow"
        )
        dl.LIVE_LOAD_TRACKING_CHARGE = "shadow"
        now = datetime.now(UTC)
        self.write_live_json(now, pv_w=0, house_load_w=0)
        dispatch = [{"gridKwh": 1.0}]  # plan imports 4000 W
        power_w, detail, numbers = dl.live_charge_power_w(dispatch, 0, now, planned_power_w=1000)
        self.assertEqual(power_w, 1000)  # unchanged
        self.assertIn("shadow", detail)
        self.assertGreater(numbers["tracked_charge_w"], 0)  # but it says what it would have done

    def test_disabled_falls_back_to_planned_power_unchanged(self):
        dl.LIVE_LOAD_TRACKING_CHARGE = "0"
        dispatch = [{"gridKwh": 1.0}]
        power_w, detail, numbers = dl.live_charge_power_w(dispatch, 0, datetime.now(UTC), planned_power_w=1000)
        self.assertEqual(power_w, 1000)
        self.assertEqual(numbers, {})
        self.assertIn("disabled", detail)

    def test_missing_live_json_falls_back_to_planned_power(self):
        dispatch = [{"gridKwh": 1.0}]
        power_w, detail, numbers = dl.live_charge_power_w(dispatch, 0, datetime.now(UTC), planned_power_w=1000)
        self.assertEqual(power_w, 1000)
        self.assertEqual(numbers, {})
        self.assertIn("missing/stale", detail)

    def test_extra_solar_is_absorbed_instead_of_exported(self):
        now = datetime.now(UTC)
        # Plan intended zero net grid exchange; live PV runs 3000 W above the house. Holding the
        # plan's grid position means charging with all of it rather than exporting the difference.
        self.write_live_json(now, pv_w=3500, house_load_w=500)
        dispatch = [{"gridKwh": 0.0}]
        power_w, _detail, numbers = dl.live_charge_power_w(dispatch, 0, now, planned_power_w=1000)
        self.assertEqual(numbers["required_ac_w"], 3000)
        # Charging: stored = AC x eta, and 50207 is battery-side — the inverse of the discharge leg.
        expected = round(3000 * dl.ONE_WAY_EFF / dl.LIVE_LOAD_ROUND_W) * dl.LIVE_LOAD_ROUND_W
        self.assertEqual(power_w, expected)
        self.assertLess(power_w, 3000)

    def test_a_load_spike_shrinks_the_charge_instead_of_importing_over_plan(self):
        now = datetime.now(UTC)
        # Plan intended 4000 W of import (1.0 kWh over the slot) with no PV; the house is suddenly
        # drawing 3000 W. Charging at the plan's full power would import 7000 W at the meter.
        self.write_live_json(now, pv_w=0, house_load_w=3000)
        dispatch = [{"gridKwh": 1.0}]
        power_w, _detail, numbers = dl.live_charge_power_w(dispatch, 0, now, planned_power_w=4000)
        self.assertEqual(numbers["required_ac_w"], 1000)
        self.assertLess(power_w, 4000)

    def test_zero_requirement_reports_zero_so_the_caller_can_demote_to_auto(self):
        now = datetime.now(UTC)
        # The house is already drawing exactly the planned import on its own: nothing left to charge
        # with. EMS BattCtrl parked at 0 W blocks self-consumption, so the caller demotes to Auto.
        self.write_live_json(now, pv_w=0, house_load_w=4000)
        dispatch = [{"gridKwh": 1.0}]
        power_w, _detail, _numbers = dl.live_charge_power_w(dispatch, 0, now, planned_power_w=4000)
        self.assertEqual(power_w, 0)


class PlannerHoldDecisionTests(unittest.TestCase):
    def test_legacy_planner_hold_becomes_idle_before_executor_guards(self):
        now = stockholm("2026-01-15T00:00:00").astimezone(UTC)
        dispatch = [{
            "startTime": "2026-01-15T00:00:00",
            "action": "hold",
            "socAfter": 10.0,
            "gridKwh": 0.0,
        }]
        con = sqlite3.connect(":memory:")
        with mock.patch.object(dl, "load_latest_plan", return_value=(10.0, dispatch, [], 1)), \
             mock.patch.object(dl, "read_live_soc_kwh", return_value=10.0), \
             mock.patch.object(dl, "read_live_solar_surplus_w", return_value=0.0):
            decision = dl.decide(con, now)
        self.assertEqual(decision[1], "idle")
        self.assertIsNone(decision[3])
        con.close()


class ApplyTargetRouting(unittest.TestCase):
    """apply_target() is the whole executor half of the ACTION CONTRACT (see dispatch_loop.py's
    note beside FORCED_ACTIONS, and lib/__tests__/action-contract.test.ts for the TS half).
    It previously had no coverage at all, which is how an unknown action silently resolving to
    auto stayed invisible."""

    def setUp(self):
        self.calls = []
        self._orig = (dl.force_charge, dl.force_discharge, dl.return_to_auto, dl.freeze_battery)
        self._orig_latch = dl._freeze_unavailable
        dl._freeze_unavailable = False
        dl.force_charge = lambda inv, w: self.calls.append(("charge", w))
        dl.force_discharge = lambda inv, w: self.calls.append(("discharge", w))
        dl.return_to_auto = lambda inv: self.calls.append(("auto", None))
        dl.freeze_battery = lambda inv: self.calls.append(("freeze", None))

    def tearDown(self):
        (dl.force_charge, dl.force_discharge, dl.return_to_auto,
         dl.freeze_battery) = self._orig
        dl._freeze_unavailable = self._orig_latch

    def test_charge_forces_a_charge_setpoint(self):
        dl.apply_target(object(), "charge", 1500)
        self.assertEqual(self.calls, [("charge", 1500)])

    def test_discharge_forces_a_discharge_setpoint(self):
        dl.apply_target(object(), "discharge", 2000)
        self.assertEqual(self.calls, [("discharge", 2000)])

    def test_battery_save_replaces_idle_with_small_fixed_discharge(self):
        original = dl.BATTERY_SAVE_W
        dl.BATTERY_SAVE_W = 50
        try:
            hold_slot = {"loadFromGridKwh": 0.3, "socAfter": dl.BATTERY_FLOOR_KWH + 1.5,
                         "batterySaveDriftKwh": 0.013}
            # Derived, not a literal: the target is the plan's SoC drop converted back to the
            # inverter's output side, so it moves with ONE_WAY_EFF -- which now carries the
            # inverter's conversion losses as well as the cells' (see EFF_ONE_WAY in
            # lib/constants.ts). A literal here would have to be re-tuned every time that changes.
            expected = int(min(dl.BATTERY_SAVE_W, 0.013 / dl.SLOT_HOURS * 1000 * dl.ONE_WAY_EFF) // 10 * 10)
            self.assertEqual(dl.battery_save_target("idle", 0, hold_slot), ("discharge", expected))
        finally:
            dl.BATTERY_SAVE_W = original

    def test_battery_save_target_is_capped_by_load_and_headroom(self):
        original = dl.BATTERY_SAVE_W
        dl.BATTERY_SAVE_W = 5000
        try:
            hold_slot = {"loadFromGridKwh": 0.15, "socAfter": dl.BATTERY_FLOOR_KWH + 1.0,
                         "batterySaveDriftKwh": 0.15}
            # The load cap (0.15 kWh/slot = 600 W) binds here, not BATTERY_SAVE_W; the planned
            # target (drift x ONE_WAY_EFF) is what it is compared against, so both track the
            # constant rather than a re-tuned literal.
            expected = int(min(0.15 / dl.SLOT_HOURS * 1000,
                               0.15 / dl.SLOT_HOURS * 1000 * dl.ONE_WAY_EFF) // 10 * 10)
            self.assertEqual(dl.battery_save_target("idle", 0, hold_slot), ("discharge", expected))
        finally:
            dl.BATTERY_SAVE_W = original

    def test_battery_save_target_follows_optimizer_drift_decision(self):
        original = dl.BATTERY_SAVE_W
        dl.BATTERY_SAVE_W = 220
        try:
            hold_without_drift = {"loadFromGridKwh": 0.5, "socAfter": dl.BATTERY_FLOOR_KWH + 1.0,
                                  "batterySaveDriftKwh": 0}
            self.assertEqual(dl.battery_save_target("idle", 0, hold_without_drift), ("idle", 0))
        finally:
            dl.BATTERY_SAVE_W = original

    def test_battery_save_leaves_solar_idle_unchanged(self):
        original = dl.BATTERY_SAVE_W
        dl.BATTERY_SAVE_W = 50
        try:
            solar_idle = {"loadFromGridKwh": 0.0, "socAfter": dl.BATTERY_FLOOR_KWH + 1.5}
            self.assertEqual(dl.battery_save_target("idle", 0, solar_idle), ("idle", 0))
        finally:
            dl.BATTERY_SAVE_W = original

    def test_battery_save_leaves_legacy_idle_unchanged(self):
        original = dl.BATTERY_SAVE_W
        dl.BATTERY_SAVE_W = 50
        try:
            self.assertEqual(dl.battery_save_target("idle", 0, {}), ("idle", 0))
        finally:
            dl.BATTERY_SAVE_W = original

    def test_battery_save_leaves_non_idle_targets_unchanged(self):
        original = dl.BATTERY_SAVE_W
        dl.BATTERY_SAVE_W = 50
        try:
            self.assertEqual(dl.battery_save_target("discharge", 2000, {}), ("discharge", 2000))
            self.assertEqual(dl.battery_save_target("charge", 1000, {}), ("charge", 1000))
        finally:
            dl.BATTERY_SAVE_W = original

    def test_idle_returns_to_auto_without_warning(self):
        # 'idle' is a declared AUTO_ACTION, so auto is intended and must stay quiet — otherwise
        # the warning below would fire on every idle slot and become noise nobody reads.
        with self.assertLogs("solinteg.dispatch", level="WARNING") as caught:
            dl.log.warning("sentinel")  # assertLogs requires at least one record
            dl.apply_target(object(), "idle", 0)
        self.assertEqual(self.calls, [("auto", None)])
        self.assertEqual([r for r in caught.output if "sentinel" not in r], [])

    def test_unknown_action_still_fails_safe_but_warns_loudly(self):
        # The scenario this exists for: the optimizer emits an action this executor does not
        # implement.
        # Auto is still the right fail-safe, but it must not be silent — auto CHARGES from
        # surplus, which for a hold-style action is the opposite of what was planned.
        # ("hold" used to be the example here; it is now a real executor action, so this uses a
        # genuinely unimplemented one.)
        with self.assertLogs("solinteg.dispatch", level="WARNING") as caught:
            dl.apply_target(object(), "curtail", 0)
        self.assertEqual(self.calls, [("auto", None)])
        joined = "\n".join(caught.output)
        self.assertIn("unrecognised dispatch action", joined)
        self.assertIn("curtail", joined)

    def test_freeze_routes_to_a_real_freeze(self):
        self.assertEqual(dl.apply_target(object(), "freeze", 0), None)
        self.assertEqual(self.calls, [("freeze", None)])

    def test_planner_hold_falls_back_to_auto_with_explicit_warning(self):
        with self.assertLogs("solinteg.dispatch", level="WARNING") as caught:
            dl.apply_target(object(), "hold", 0)
        self.assertEqual(self.calls, [("auto", None)])
        self.assertIn("planner hold", "\n".join(caught.output).lower())

    def test_an_unenforced_freeze_latches_off_and_does_not_retry(self):
        # A freeze the inverter accepts but does not enforce is the documented pre-mode failure
        # shape (MODBUS.md) — a property of the firmware, not a transient. freeze_battery has
        # already reverted to auto by the time it raises, so retrying every hold slot would just
        # churn registers. It must latch off and let the emulation take over.
        dl.freeze_battery = lambda inv: (_ for _ in ()).throw(
            ic.FreezeNotEnforced("battery still at 900 W after freeze"))
        self.assertFalse(dl._freeze_unavailable)
        with self.assertLogs("solinteg.dispatch", level="ERROR"):
            dl.apply_target(object(), "freeze", 0)
        self.assertTrue(dl._freeze_unavailable)

        # …and with the latch set, a marked hold slot no longer asks for a freeze.
        with mock.patch.object(dl, "FREEZE_HOLDS", True), mock.patch.object(dl, "BATTERY_SAVE_W", 220):
            hold_slot = {"loadFromGridKwh": 0.3, "socAfter": dl.BATTERY_FLOOR_KWH + 1.5,
                         "batterySaveDriftKwh": 0.013}
            action, _power = dl.battery_save_target("idle", 0, hold_slot)
        self.assertEqual(action, "discharge")  # the emulation, not a freeze

    def test_declared_vocabularies_are_disjoint(self):
        # An action in both tuples would make the contract test's union check pass while
        # apply_target's actual routing stayed ambiguous.
        self.assertEqual(set(dl.FORCED_ACTIONS) & set(dl.AUTO_ACTIONS), set())


class EffectiveTargetTests(unittest.TestCase):
    """The solar-funding guard folded into the target actually applied."""

    def test_charge_that_cannot_be_solar_funded_becomes_idle_at_zero(self):
        self.assertEqual(dl.effective_target("charge", 4000, solar_skip=True), ("idle", 0, True))

    def test_charge_that_can_be_funded_passes_through(self):
        self.assertEqual(dl.effective_target("charge", 4000, solar_skip=False), ("charge", 4000, False))

    def test_the_guard_only_applies_to_charges(self):
        # solar_skip is meaningless for a discharge — check_solar_funding never even runs for
        # one — so a stale True must not silently cancel a planned discharge.
        self.assertEqual(dl.effective_target("discharge", 3000, solar_skip=True), ("discharge", 3000, False))

    def test_idle_passes_through(self):
        self.assertEqual(dl.effective_target("idle", 0, solar_skip=False), ("idle", 0, False))


class NeedsApplyTests(unittest.TestCase):
    """Write-gating: the three rules in tension (change / reassert / deadband)."""

    def test_first_decision_after_restart_always_applies(self):
        # last_target is None on startup — this is what self-heals a setpoint a crashed prior
        # instance left forced, so it must apply even for a plain idle.
        self.assertTrue(dl.needs_apply(("t0", "idle", 0), None, "idle", 0, due_for_reassert=False))

    def test_unchanged_idle_target_does_not_rewrite(self):
        t = ("t0", "idle", 0)
        self.assertFalse(dl.needs_apply(t, t, "idle", 0, due_for_reassert=False))

    def test_unchanged_idle_target_is_never_reasserted_on_a_timer(self):
        # The load-bearing one: General mode doesn't decay, so a timed re-poke would blindly
        # overwrite a work mode the owner set by hand in the inverter's own app.
        t = ("t0", "idle", 0)
        self.assertFalse(dl.needs_apply(t, t, "idle", 0, due_for_reassert=True))

    def test_unchanged_forced_target_is_reasserted_on_the_timer(self):
        t = ("t0", "discharge", 3000)
        self.assertFalse(dl.needs_apply(t, t, "discharge", 3000, due_for_reassert=False))
        self.assertTrue(dl.needs_apply(t, t, "discharge", 3000, due_for_reassert=True))

    def test_a_new_slot_always_applies(self):
        self.assertTrue(dl.needs_apply(("t1", "idle", 0), ("t0", "idle", 0), "idle", 0, False))

    def test_retarget_below_the_deadband_waits_for_the_next_reassert(self):
        last = ("t0", "discharge", 3000)
        small = 3000 + dl.LIVE_LOAD_DEADBAND_W - 1
        self.assertFalse(dl.needs_apply(("t0", "discharge", small), last, "discharge", small, False))

    def test_retarget_at_or_above_the_deadband_applies_immediately(self):
        # A heat-pump-scale change must not be delayed by the noise filter.
        last = ("t0", "discharge", 3000)
        big = 3000 + dl.LIVE_LOAD_DEADBAND_W
        self.assertTrue(dl.needs_apply(("t0", "discharge", big), last, "discharge", big, False))

    def test_a_suppressed_small_move_still_applies_once_it_is_due_for_reassert(self):
        last = ("t0", "discharge", 3000)
        small = 3000 + dl.LIVE_LOAD_DEADBAND_W - 1
        self.assertTrue(dl.needs_apply(("t0", "discharge", small), last, "discharge", small, True))

    def test_an_action_change_within_a_slot_is_never_deadband_suppressed(self):
        # charge -> discharge is a direction reversal, not load noise, however small the delta.
        last = ("t0", "charge", 100)
        self.assertTrue(dl.needs_apply(("t0", "discharge", 100), last, "discharge", 100, False))

    def test_battery_save_discharge_is_not_replaced_by_charge_mid_slot(self):
        # A replan may change the plan while the current slot is already executing.
        # Keep the applied battery-save direction until the next slot boundary.
        last = ("t0", "discharge", 50)
        self.assertFalse(dl.needs_apply(("t0", "charge", 2100), last, "charge", 2100, False))

    def test_due_reassert_does_not_override_mid_slot_discharge_guard(self):
        # Periodic reassertions must not push a mid-slot reversal through the same guard.
        last = ("t0", "discharge", 50)
        self.assertFalse(dl.needs_apply(("t0", "charge", 2100), last, "charge", 2100, True))

    def test_discharge_to_charge_is_allowed_at_the_next_slot(self):
        last = ("t0", "discharge", 50)
        self.assertTrue(dl.needs_apply(("t1", "charge", 2100), last, "charge", 2100, False))

    def test_soc_recovery_overrides_mid_slot_discharge_block_even_when_reassert_not_due(self):
        last = ("t0", "discharge", 50)
        target = ("t0", "charge", dl.SOC_FLOOR_RECOVERY_W)
        self.assertTrue(dl.needs_apply(target, last, "charge", target[2], False, soc_recovery=True))

    def test_soc_recovery_switches_from_regular_discharge_in_same_slot(self):
        last = ("t0", "discharge", 3800)
        target = ("t0", "charge", dl.SOC_FLOOR_RECOVERY_W)
        self.assertTrue(dl.needs_apply(target, last, "charge", target[2], True, soc_recovery=True))


class NextLastTargetTests(unittest.TestCase):
    """What gets recorded as "the inverter is now at this target" after an apply.

    `last_target` is the input needs_apply() gates on, so recording a target the inverter is NOT
    at silently delays recovery by a full DISPATCH_REASSERT_S.
    """

    TARGET = ("t0", "discharge", 3000)

    def test_a_successful_apply_records_the_target(self):
        self.assertEqual(dl.next_last_target("applied", self.TARGET), self.TARGET)

    def test_error_reverted_records_nothing(self):
        """The fail-safe revert succeeded, so the inverter is in AUTO — recording the forced
        target claims otherwise. This path used to fall through to the success branch, and on the
        reference deployment that cost the full reassert interval (294-309 s measured) against
        16-21 s for the failed-revert path, i.e. the milder failure recovered slower."""
        self.assertIsNone(dl.next_last_target("error_reverted", self.TARGET))

    def test_error_revert_failed_records_nothing(self):
        self.assertIsNone(dl.next_last_target("error_revert_failed", self.TARGET))

    def test_both_error_paths_now_agree(self):
        self.assertEqual(dl.next_last_target("error_reverted", self.TARGET),
                         dl.next_last_target("error_revert_failed", self.TARGET))

    def test_the_guard_skips_deliberately_keep_the_target(self):
        """NOT failures: the guard chose auto on purpose and has already asked for a fresh plan,
        so retrying every tick would re-trip the same guard and log a row every LOOP_INTERVAL_S.
        These wait for a changed target — a new plan, or solar recovering."""
        for outcome in ("skipped_divergence", "skipped_solar_shortfall"):
            with self.subTest(outcome=outcome):
                self.assertEqual(dl.next_last_target(outcome, self.TARGET), self.TARGET)

    def test_a_reverted_error_makes_the_very_next_tick_re_apply(self):
        """The two functions composed — the property that actually matters. Same slot, same
        target, no reassert due: it must still apply, because the inverter is in auto."""
        last = dl.next_last_target("error_reverted", self.TARGET)
        self.assertTrue(dl.needs_apply(self.TARGET, last, "discharge", 3000,
                                       due_for_reassert=False))

    def test_a_successful_apply_still_suppresses_the_next_identical_tick(self):
        """The other side of it — the fix must not turn every steady discharge tick into a write."""
        last = dl.next_last_target("applied", self.TARGET)
        self.assertFalse(dl.needs_apply(self.TARGET, last, "discharge", 3000,
                                        due_for_reassert=False))


class FakeInverter:
    """Stands in for inverter_control.Inverter in apply_decision tests."""

    def __init__(self, soc_pct=50.0, fail_on=None, battery_w=0):
        self._soc_pct = soc_pct
        self._battery_w = battery_w
        self.closed = False
        self.fail_on = fail_on or set()

    def battery_power_w(self):
        return self._battery_w

    def soc_pct(self):
        if "soc" in self.fail_on:
            raise RuntimeError("modbus read failed")
        return self._soc_pct

    def close(self):
        self.closed = True


class ApplyDecisionTests(unittest.TestCase):
    """The apply/recover path lifted out of main() on 2026-08-07. Until then this was the one
    part of the executor with no unit coverage — including both failure branches, where the
    ordering (revert BEFORE logging) and the loop_in_auto bookkeeping are load-bearing."""

    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute("""
            CREATE TABLE control_actions (
                id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, slot_time TEXT,
                planned_action TEXT NOT NULL, power_w INTEGER, armed INTEGER NOT NULL,
                outcome TEXT NOT NULL, detail TEXT, detail_json TEXT)
        """)
        self.applied = []
        self.replans = []
        self.inverters = []
        self.revert_fails = False

        def _make_inverter():
            inv = FakeInverter(soc_pct=self.soc_pct, fail_on=self.inverter_fail_on,
                               battery_w=self.battery_w)
            self.inverters.append(inv)
            return inv

        self.soc_pct = 50.0
        self.battery_w = 0
        self.inverter_fail_on = set()
        self._orig = (dl.Inverter, dl.apply_target, dl.return_to_auto, dl.maybe_request_replan)
        dl.Inverter = _make_inverter
        dl.apply_target = lambda inv, a, w: self.applied.append((a, w))
        dl.return_to_auto = lambda inv: (_ for _ in ()).throw(RuntimeError("revert failed")) \
            if self.revert_fails else self.applied.append(("auto", 0))
        dl.maybe_request_replan = lambda reason: self.replans.append(reason)

    def tearDown(self):
        dl.Inverter, dl.apply_target, dl.return_to_auto, dl.maybe_request_replan = self._orig
        self.con.close()

    def rows(self):
        return self.con.execute(
            "SELECT planned_action, power_w, outcome, detail FROM control_actions").fetchall()

    def test_readback_database_failure_does_not_revert_applied_discharge(self):
        original = dl.log_control_readback
        dl.log_control_readback = lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked"))
        original_sign = ic.SIGN
        ic.SIGN = "neg_charge"
        try:
            outcome, in_auto = dl.apply_decision(
                self.con, "t0", "discharge", 3000, expected_soc_kwh=None,
                solar_skipped_now=False, detail="save", numbers={"battery_save_w": 3000},
                loop_in_auto=False,
            )
        finally:
            dl.log_control_readback = original
            ic.SIGN = original_sign
        self.assertEqual(outcome, "applied")
        self.assertFalse(in_auto)
        self.assertEqual(self.applied, [("discharge", 3000)])

    def test_recovery_charge_is_pending_when_modbus_accepts_but_battery_has_zero_power(self):
        with mock.patch.object(dl, "log_control_readback"):
            outcome, in_auto = dl.apply_decision(
                self.con, "t0", "charge", dl.SOC_FLOOR_RECOVERY_W, expected_soc_kwh=None,
                solar_skipped_now=False, detail="recovery", numbers={"soc_recovery": True},
                loop_in_auto=False)
        self.assertEqual(outcome, "accepted_pending_physical_confirmation")
        self.assertFalse(in_auto)
        self.assertEqual(self.applied, [("charge", dl.SOC_FLOOR_RECOVERY_W)])
        self.assertEqual(self.rows()[0][2], outcome)  # not misleadingly "applied"

    def test_recovery_charge_with_actual_battery_input_is_confirmed(self):
        self.battery_w = -300
        with mock.patch.object(dl, "log_control_readback"):
            outcome, in_auto = dl.apply_decision(
                self.con, "t0", "charge", 300, expected_soc_kwh=None,
                solar_skipped_now=False, detail="recovery", numbers={"soc_recovery": True},
                loop_in_auto=False)
        self.assertEqual(outcome, "applied")
        self.assertFalse(in_auto)

    def test_normal_charge_applies_and_logs_one_row(self):
        # expected SoC matches actual (50% of 25.6 = 12.8 kWh) — no drift, no guard.
        outcome, in_auto = dl.apply_decision(
            self.con, "t0", "charge", 4000, expected_soc_kwh=12.8, solar_skipped_now=False,
            detail="", numbers={}, loop_in_auto=False)
        self.assertEqual(outcome, "applied")
        self.assertFalse(in_auto)
        self.assertEqual(self.applied, [("charge", 4000)])
        self.assertEqual(len(self.rows()), 1)
        self.assertTrue(all(inv.closed for inv in self.inverters))  # connection never leaked

    def test_solar_shortfall_demotes_to_auto_and_requests_a_replan(self):
        outcome, in_auto = dl.apply_decision(
            self.con, "t0", "charge", 4000, expected_soc_kwh=12.8, solar_skipped_now=True,
            detail="shortfall 1.2 kWh", numbers={}, loop_in_auto=False)
        self.assertEqual(outcome, "skipped_solar_shortfall")
        self.assertTrue(in_auto)
        # Demoted to idle, which apply_target routes to return_to_auto (see ApplyTargetRouting).
        # The point here is that the planned charge was never applied.
        self.assertEqual(self.applied, [("idle", 0)])
        self.assertIn("solar_shortfall", self.replans)

    def test_soc_divergence_demotes_to_auto_and_requests_a_replan(self):
        # plan expected 20 kWh, live SoC is 12.8 -> 7.2 kWh drift, way past the 3.0 guard
        outcome, in_auto = dl.apply_decision(
            self.con, "t0", "discharge", 3000, expected_soc_kwh=20.0, solar_skipped_now=False,
            detail="", numbers={}, loop_in_auto=False)
        self.assertEqual(outcome, "skipped_divergence")
        self.assertTrue(in_auto)
        self.assertEqual(self.applied, [("idle", 0)])  # not the planned discharge
        self.assertIn("divergence_skip", self.replans)

    def test_idle_while_already_in_auto_logs_a_row_but_writes_no_register(self):
        # The row still matters: armed-coverage measurement reads control_actions cadence as
        # "the loop is alive" (lib/oracle.ts ARMED_SEGMENT_CAP_MS).
        outcome, in_auto = dl.apply_decision(
            self.con, "t0", "idle", 0, expected_soc_kwh=None, solar_skipped_now=False,
            detail="", numbers={}, loop_in_auto=True)
        self.assertEqual(outcome, "applied")
        self.assertTrue(in_auto)
        self.assertEqual(self.applied, [])          # no register poked
        self.assertEqual(len(self.rows()), 1)       # but the heartbeat row is there
        self.assertEqual(self.inverters, [])        # and no connection was even opened

    def test_idle_when_the_loop_is_not_known_to_be_in_auto_does_write(self):
        outcome, in_auto = dl.apply_decision(
            self.con, "t0", "idle", 0, expected_soc_kwh=None, solar_skipped_now=False,
            detail="", numbers={}, loop_in_auto=False)
        self.assertEqual(outcome, "applied")
        self.assertTrue(in_auto)
        self.assertEqual(self.applied, [("idle", 0)])

    def test_legacy_planner_hold_records_auto_state(self):
        outcome, in_auto = dl.apply_decision(
            self.con, "t0", "hold", 0, expected_soc_kwh=None, solar_skipped_now=False,
            detail="legacy planner hold", numbers={}, loop_in_auto=False)
        self.assertEqual(outcome, "applied")
        self.assertTrue(in_auto)
        self.assertEqual(self.applied, [("hold", 0)])

    def test_a_failed_apply_reverts_to_auto_and_still_logs(self):
        self.inverter_fail_on = {"soc"}  # blow up inside the divergence check
        outcome, in_auto = dl.apply_decision(
            self.con, "t0", "discharge", 3000, expected_soc_kwh=12.8, solar_skipped_now=False,
            detail="", numbers={}, loop_in_auto=False)
        self.assertEqual(outcome, "error_reverted")
        self.assertTrue(in_auto)
        self.assertEqual(self.applied, [("auto", 0)])
        self.assertEqual(self.rows()[0][2], "error_reverted")

    def test_a_failed_apply_whose_revert_also_fails_reports_unconfirmed_state(self):
        # Both failed, so the inverter may still be running the previous slot's setpoint.
        # loop_in_auto MUST go False: the next idle tick has to write rather than assume auto.
        self.inverter_fail_on = {"soc"}
        self.revert_fails = True
        outcome, in_auto = dl.apply_decision(
            self.con, "t0", "discharge", 3000, expected_soc_kwh=12.8, solar_skipped_now=False,
            detail="", numbers={}, loop_in_auto=False)
        self.assertEqual(outcome, "error_revert_failed")
        self.assertFalse(in_auto)
        row = self.rows()[0]
        self.assertEqual(row[2], "error_revert_failed")
        self.assertIn("revert also failed", row[3])

    def test_a_telemetry_write_failure_cannot_skip_the_fail_safe_revert(self):
        # telemetry.db is a shared WAL file and contention is realistic. The revert must
        # already have happened by the time logging is attempted.
        self.inverter_fail_on = {"soc"}
        self.con.close()  # every log_action from here on raises
        outcome, in_auto = dl.apply_decision(
            self.con, "t0", "discharge", 3000, expected_soc_kwh=12.8, solar_skipped_now=False,
            detail="", numbers={}, loop_in_auto=False)
        self.assertEqual(outcome, "error_reverted")
        self.assertEqual(self.applied, [("auto", 0)])  # reverted despite the logging failure
        self.con = sqlite3.connect(":memory:")         # keep tearDown happy


class CheckSocDivergenceTests(unittest.TestCase):
    def setUp(self):
        self.replans = []
        self._orig = dl.maybe_request_replan
        dl.maybe_request_replan = lambda reason: self.replans.append(reason)

    def tearDown(self):
        dl.maybe_request_replan = self._orig

    def test_small_drift_records_the_numbers_without_skipping_or_replanning(self):
        numbers = {}
        # 50% of 25.6 = 12.8 kWh actual; expect 12.9 -> 0.1 kWh drift, under both thresholds.
        skip, detail = dl.check_soc_divergence(FakeInverter(50.0), 12.9, "t0", "discharge", numbers)
        self.assertFalse(skip)
        self.assertAlmostEqual(numbers["soc_drift_kwh"], 0.1, places=2)
        self.assertEqual(numbers["soc_drift_limit_kwh"], dl.SOC_DIVERGENCE_KWH)
        self.assertEqual(self.replans, [])
        self.assertIn("drift", detail)

    def test_control_readback_is_persisted_with_command_and_actual_registers(self):
        with tempfile.TemporaryDirectory() as td:
            con = dl.init_db(Path(td) / "telemetry.db")
            class ReadbackFake:
                def soc_pct(self): return 50.0
                def battery_power_w(self): return 228
                def work_mode(self): return 0x303
                def read_s16(self, addr): return 5
            original_sign = ic.SIGN
            ic.SIGN = "neg_charge"
            try:
                dl.log_control_readback(
                    con, "2026-08-30T06:15:00", "discharge", 50, ReadbackFake(),
                )
            finally:
                ic.SIGN = original_sign
            row = con.execute(
                "SELECT slot_time, requested_power_w, target_raw, target_readback_raw, mode_raw, battery_w, soc_pct "
                "FROM control_readbacks"
            ).fetchone()
            self.assertEqual(row, ("2026-08-30T06:15:00", 50, 5, 5, 0x303, 228, 50.0))
            con.close()

    def test_drift_past_the_replan_threshold_asks_for_a_plan_but_still_applies(self):
        # Between REPLAN_DRIFT_KWH (1.5) and SOC_DIVERGENCE_KWH (3.0): nudge, don't skip.
        numbers = {}
        skip, _ = dl.check_soc_divergence(FakeInverter(50.0), 12.8 + 2.0, "t0", "discharge", numbers)
        self.assertFalse(skip)
        self.assertEqual(self.replans, ["drift"])

    def test_drift_past_the_divergence_guard_skips(self):
        numbers = {}
        skip, _ = dl.check_soc_divergence(FakeInverter(50.0), 12.8 + 5.0, "t0", "discharge", numbers)
        self.assertTrue(skip)
        self.assertEqual(self.replans, ["drift"])  # the caller adds 'divergence_skip'

    def test_the_numbers_are_recorded_even_when_the_guard_does_not_trip(self):
        # This is what eventually lets the threshold be tuned from the real everyday
        # distribution in your own control_actions rows instead of a guess.
        numbers = {}
        dl.check_soc_divergence(FakeInverter(50.0), 12.8, "t0", "charge", numbers)
        self.assertIn("soc_drift_kwh", numbers)
        self.assertIn("soc_drift_limit_kwh", numbers)


class LoadLatestPlanTests(unittest.TestCase):
    """Plan selection by COVERAGE, not by price_date (OPTIMIZER-AUDIT.md E4).

    A plan spans up to three days, so one computed at 23:50 covers tomorrow in full — but it is
    logged under yesterday's price_date, and the old date filter therefore stopped finding it at
    00:00. Every single day the loop reported no_plan and fell back to Auto (which discharges the
    battery to cover load) until a fresh row appeared, at the hour when prices are usually lowest.
    """

    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute(
            """CREATE TABLE optimizer_runs (id INTEGER PRIMARY KEY, logged_at TEXT,
               price_date TEXT, has_tomorrow INTEGER, start_soc_kwh REAL,
               inputs_json TEXT, dispatch_json TEXT)"""
        )

    def tearDown(self):
        self.con.close()

    def insert(self, logged_at: str, price_date: str, first_slot: str, slots: int, has_tomorrow=1):
        start = datetime.strptime(first_slot, "%Y-%m-%dT%H:%M:%S")
        dispatch = [
            {"startTime": (start + timedelta(minutes=15 * i)).strftime("%Y-%m-%dT%H:%M:%S"),
             "action": "idle", "socAfter": 8.0}
            for i in range(slots)
        ]
        self.con.execute(
            "INSERT INTO optimizer_runs (logged_at, price_date, has_tomorrow, start_soc_kwh, "
            "inputs_json, dispatch_json) VALUES (?, ?, ?, ?, ?, ?)",
            (logged_at, price_date, has_tomorrow, 8.0, json.dumps([]), json.dumps(dispatch)),
        )
        self.con.commit()

    def test_uses_yesterdays_plan_just_after_midnight_when_it_covers_now(self):
        # Logged 2026-02-10 23:50 under price_date 2026-02-10, covering into the 11th.
        self.insert("2026-02-10T22:50:00+00:00", "2026-02-10", "2026-02-10T23:45:00", 96)
        now = datetime(2026, 2, 11, 0, 5, tzinfo=STOCKHOLM).astimezone(UTC)
        start_soc, dispatch, inputs, has_tomorrow = dl.load_latest_plan(self.con, now)
        self.assertIsNotNone(dispatch)
        self.assertEqual(start_soc, 8.0)
        self.assertEqual(has_tomorrow, 1)
        self.assertIsNotNone(dl.slot_index_for_instant(dispatch, now))

    def test_skips_a_newer_row_that_does_not_cover_now(self):
        # A newer plan for a future day plus an older one that actually covers now: coverage wins.
        self.insert("2026-02-10T10:00:00+00:00", "2026-02-10", "2026-02-10T12:00:00", 8)
        self.insert("2026-02-10T11:00:00+00:00", "2026-02-10", "2026-02-12T00:00:00", 96)
        now = datetime(2026, 2, 10, 12, 30, tzinfo=STOCKHOLM).astimezone(UTC)
        _soc, dispatch, _inputs, _ht = dl.load_latest_plan(self.con, now)
        self.assertIsNotNone(dispatch)
        self.assertEqual(dispatch[0]["startTime"], "2026-02-10T12:00:00")

    def test_returns_nothing_when_no_recent_plan_covers_now(self):
        self.insert("2026-02-10T10:00:00+00:00", "2026-02-10", "2026-02-10T12:00:00", 8)
        now = datetime(2026, 2, 10, 20, 0, tzinfo=STOCKHOLM).astimezone(UTC)
        self.assertEqual(dl.load_latest_plan(self.con, now), (None, None, None, None))

    def test_looks_no_further_back_than_the_lookback_window(self):
        # A covering plan buried behind PLAN_LOOKBACK_ROWS newer, non-covering ones is deliberately
        # NOT found: the right answer then is "no usable plan" (and a replan request), not archaeology.
        self.insert("2026-02-10T09:00:00+00:00", "2026-02-10", "2026-02-10T12:00:00", 8)
        for i in range(dl.PLAN_LOOKBACK_ROWS):
            self.insert(f"2026-02-10T1{i}:00:00+00:00", "2026-02-10", "2026-02-12T00:00:00", 8)
        now = datetime(2026, 2, 10, 12, 30, tzinfo=STOCKHOLM).astimezone(UTC)
        self.assertEqual(dl.load_latest_plan(self.con, now), (None, None, None, None))


class IsPlannedHoldTests(unittest.TestCase):
    """The hold decision comes from the plan's own marker (OPTIMIZER-AUDIT.md E5).

    It used to be re-derived from `loadFromGridKwh > 0.05`, which disagreed with the optimizer's own
    condition (that tests the load BEFORE the hold discharge; the plan publishes what is left after
    it) for a house load between roughly 200 and 420 W. In that band the plan carried a hold the
    executor declined, falling back to Auto — which discharges at whatever the house draws, the exact
    opposite of a hold.
    """

    def test_the_narrow_band_that_used_to_fall_back_to_auto(self):
        # loadRem 0.10 kWh/slot (400 W) minus a ~0.055 kWh hold leaves 0.045 published — under the
        # old 0.05 threshold, so the executor declined a hold the optimizer had planned and modelled.
        slot = {"loadFromGridKwh": 0.045, "socAfter": dl.BATTERY_FLOOR_KWH + 1.0,
                "batterySaveDriftKwh": 0.055}
        self.assertTrue(dl.is_planned_hold(slot))

    def test_an_unmarked_idle_slot_is_not_a_hold(self):
        slot = {"loadFromGridKwh": 0.5, "socAfter": dl.BATTERY_FLOOR_KWH + 1.0}
        self.assertFalse(dl.is_planned_hold(slot))
        self.assertFalse(dl.is_planned_hold({**slot, "batterySaveDriftKwh": 0}))

    def test_never_a_hold_at_or_below_the_floor(self):
        slot = {"loadFromGridKwh": 0.5, "socAfter": dl.BATTERY_FLOOR_KWH,
                "batterySaveDriftKwh": 0.055}
        self.assertFalse(dl.is_planned_hold(slot))

    def test_malformed_fields_degrade_to_no_hold(self):
        self.assertFalse(dl.is_planned_hold({"batterySaveDriftKwh": "x", "socAfter": 5.0}))
        self.assertFalse(dl.is_planned_hold({"batterySaveDriftKwh": 0.05}))  # no socAfter


class ApplyImportLimitTests(unittest.TestCase):
    """The startup hook for the site-level import limit (registers 50007/50009).

    The bound itself is tested in test_inverter_control.ImportLimitTests; what matters here is
    that it is OFF unless configured, and that a failure to apply it cannot stop the controller —
    it is an extra bound on top of clamp_power_w and the planner's own grid-cap check, so losing
    it must degrade to the protection that already existed, not to no controller at all."""

    def test_does_nothing_when_unconfigured(self):
        with mock.patch.object(dl, "IMPORT_LIMIT_W", 0), \
             mock.patch.object(dl, "Inverter") as inverter, \
             mock.patch.object(dl, "set_import_limit") as setter:
            dl.apply_import_limit()
        inverter.assert_not_called()  # not even a connection is opened
        setter.assert_not_called()

    def test_applies_the_configured_limit_once_and_closes_the_connection(self):
        inv = mock.MagicMock()
        with mock.patch.object(dl, "IMPORT_LIMIT_W", 10_000), \
             mock.patch.object(dl, "Inverter", return_value=inv), \
             mock.patch.object(dl, "set_import_limit") as setter:
            dl.apply_import_limit()
        setter.assert_called_once_with(inv, 10_000)
        inv.close.assert_called_once()

    def test_a_failure_is_swallowed_so_the_loop_still_starts(self):
        inv = mock.MagicMock()
        with mock.patch.object(dl, "IMPORT_LIMIT_W", 10_000), \
             mock.patch.object(dl, "Inverter", return_value=inv), \
             mock.patch.object(dl, "set_import_limit", side_effect=RuntimeError("no route to host")):
            dl.apply_import_limit()  # must not raise
        inv.close.assert_called_once()


class SocFloorGuardTests(unittest.TestCase):
    """read_live_soc_kwh and soc_floor_recovery_target guard correct SoC-floor recovery.

    The inverter's BMS triggers a forceful charge when SoC reaches its own (lower) hardware
    floor — bypassing the optimizer completely and buying energy at whatever the current spot
    price is. Our floor guard catches this earlier: when live SoC from live.json drops below
    BATTERY_FLOOR_KWH + SOC_FLOOR_GUARD_HEADROOM_KWH while the plan says idle, escalate to a
    small recovery charge so the optimizer owns the timing (and can pick a cheaper slot).
    """

    def setUp(self):
        self._live_json = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        )
        self._orig_path = dl.LIVE_JSON_PATH
        dl.LIVE_JSON_PATH = self._live_json.name

    def tearDown(self):
        dl.LIVE_JSON_PATH = self._orig_path
        Path(self._live_json.name).unlink(missing_ok=True)

    def _write_live(self, soc_pct: float) -> None:
        now = datetime.now(UTC)
        data = {
            "timestamp": now.isoformat(),
            "soc_pct": soc_pct,
            "pv_w": 0,
            "house_load_w": 500,
        }
        Path(self._live_json.name).write_text(json.dumps(data))

    def test_read_live_soc_kwh_returns_value_from_live_json(self):
        self._write_live(soc_pct=10.0)
        now = datetime.now(UTC)
        soc = dl.read_live_soc_kwh(now)
        self.assertIsNotNone(soc)
        expected = round(10.0 / 100 * dl.BATTERY_KWH, 3)
        self.assertAlmostEqual(soc, expected, places=2)

    def test_read_live_soc_kwh_returns_none_when_file_missing(self):
        dl.LIVE_JSON_PATH = "/tmp/does_not_exist_xyz.json"
        self.assertIsNone(dl.read_live_soc_kwh(datetime.now(UTC)))

    def test_read_live_soc_kwh_returns_none_when_stale(self):
        old_ts = (datetime.now(UTC) - timedelta(seconds=200)).isoformat()
        Path(self._live_json.name).write_text(
            json.dumps({"timestamp": old_ts, "soc_pct": 10.0, "pv_w": 0, "house_load_w": 0})
        )
        self.assertIsNone(dl.read_live_soc_kwh(datetime.now(UTC)))

    def test_soc_floor_recovery_idle_below_headroom_escalates_to_charge(self):
        # SoC is at the floor — idle must escalate to a small recovery charge.
        low_soc_kwh = dl.BATTERY_FLOOR_KWH + 0.1  # inside the guard band
        action, power_w = dl.soc_floor_recovery_target("idle", 0, low_soc_kwh)
        self.assertEqual(action, "charge")
        self.assertEqual(power_w, dl.SOC_FLOOR_RECOVERY_W)

    def test_soc_floor_recovery_idle_above_headroom_leaves_idle(self):
        # SoC comfortably above the guard band — no escalation.
        safe_soc_kwh = dl.BATTERY_FLOOR_KWH + dl.SOC_FLOOR_GUARD_HEADROOM_KWH + 0.5
        action, power_w = dl.soc_floor_recovery_target("idle", 0, safe_soc_kwh)
        self.assertEqual(action, "idle")
        self.assertEqual(power_w, 0)

    def test_soc_floor_recovery_overrides_all_optimizer_actions(self):
        # Safety recovery wins over economical charge, discharge, save and freeze.
        low_soc_kwh = dl.BATTERY_FLOOR_KWH
        for action, power_w in (("charge", 3000), ("discharge", 2000), ("freeze", 0), ("idle", 0)):
            with self.subTest(action=action):
                self.assertEqual(
                    dl.soc_floor_recovery_target(action, power_w, low_soc_kwh),
                    ("charge", dl.SOC_FLOOR_RECOVERY_W),
                )

    def test_soc_floor_recovery_with_none_soc_leaves_action_unchanged(self):
        # live.json missing/stale — cannot read SoC, must not escalate blind.
        action, power_w = dl.soc_floor_recovery_target("idle", 0, None)
        self.assertEqual(action, "idle")
        self.assertEqual(power_w, 0)

    def test_soc_floor_recovery_exactly_at_headroom_boundary_leaves_idle(self):
        # Boundary: exactly at BATTERY_FLOOR_KWH + SOC_FLOOR_GUARD_HEADROOM_KWH is safe.
        boundary_soc_kwh = dl.BATTERY_FLOOR_KWH + dl.SOC_FLOOR_GUARD_HEADROOM_KWH
        action, power_w = dl.soc_floor_recovery_target("idle", 0, boundary_soc_kwh)
        self.assertEqual(action, "idle")
        self.assertEqual(power_w, 0)

    def test_soc_floor_recovery_uses_surplus_when_larger_than_minimum(self):
        # When live PV surplus exceeds SOC_FLOOR_RECOVERY_W, use it as the charge target
        # (quantised to 10 W), not the minimum.
        low_soc_kwh = dl.BATTERY_FLOOR_KWH + 0.1  # inside the guard band
        surplus_w = 2000  # clearly larger than SOC_FLOOR_RECOVERY_W (300 W default)
        action, power_w = dl.soc_floor_recovery_target("idle", 0, low_soc_kwh, surplus_w=surplus_w)
        self.assertEqual(action, "charge")
        # Must use surplus (quantised to 10 W) rather than the minimum floor.
        self.assertGreater(power_w, dl.SOC_FLOOR_RECOVERY_W)
        # Must be quantised to 10 W and not exceed the inverter cap.
        self.assertEqual(power_w % 10, 0)
        from inverter_control import clamp_power_w
        self.assertLessEqual(power_w, clamp_power_w(surplus_w))

    def test_soc_floor_recovery_falls_back_to_minimum_when_surplus_small(self):
        # When surplus is below SOC_FLOOR_RECOVERY_W, still use the minimum.
        low_soc_kwh = dl.BATTERY_FLOOR_KWH + 0.1
        surplus_w = 50  # smaller than SOC_FLOOR_RECOVERY_W
        action, power_w = dl.soc_floor_recovery_target("idle", 0, low_soc_kwh, surplus_w=surplus_w)
        self.assertEqual(action, "charge")
        self.assertEqual(power_w, dl.SOC_FLOOR_RECOVERY_W)

    def test_soc_floor_recovery_without_surplus_uses_minimum(self):
        # No surplus provided — backward-compatible default: use SOC_FLOOR_RECOVERY_W.
        low_soc_kwh = dl.BATTERY_FLOOR_KWH + 0.1
        action, power_w = dl.soc_floor_recovery_target("idle", 0, low_soc_kwh)
        self.assertEqual(action, "charge")
        self.assertEqual(power_w, dl.SOC_FLOOR_RECOVERY_W)

    def test_soc_floor_recovery_surplus_none_uses_minimum(self):
        # surplus_w=None (live data unavailable) — use minimum, do not error.
        low_soc_kwh = dl.BATTERY_FLOOR_KWH + 0.1
        action, power_w = dl.soc_floor_recovery_target("idle", 0, low_soc_kwh, surplus_w=None)
        self.assertEqual(action, "charge")
        self.assertEqual(power_w, dl.SOC_FLOOR_RECOVERY_W)

    def test_soc_floor_recovery_surplus_ignored_when_soc_safe(self):
        # Even with a large surplus, no escalation when SoC is above the guard band.
        safe_soc_kwh = dl.BATTERY_FLOOR_KWH + dl.SOC_FLOOR_GUARD_HEADROOM_KWH + 0.5
        action, power_w = dl.soc_floor_recovery_target("idle", 0, safe_soc_kwh, surplus_w=2000)
        self.assertEqual(action, "idle")
        self.assertEqual(power_w, 0)


class RecoveryEnforcementTests(unittest.TestCase):
    """The actual-power check uses multiple poller readings, not register readback."""

    def setUp(self):
        self.con = dl.init_db(":memory:")
        self.con.execute("CREATE TABLE readings (timestamp TEXT, battery_w INTEGER)")
        self.previous_report = dl._last_recovery_nonresponse_report
        dl._last_recovery_nonresponse_report = 0
        self.now = datetime.now(UTC)
        self.target = ("t0", "charge", 300)

    def tearDown(self):
        dl._last_recovery_nonresponse_report = self.previous_report
        self.con.close()

    def samples(self, battery_w: int):
        for seconds_ago in (100, 75, 50, 25):
            self.con.execute("INSERT INTO readings VALUES (?, ?)",
                             ((self.now - timedelta(seconds=seconds_ago)).isoformat(), battery_w))
        self.con.commit()

    def check(self, elapsed=150):
        with mock.patch.object(dl, "ARMED", True), \
             mock.patch.object(dl.time, "monotonic", return_value=1000):
            return dl.monitor_soc_recovery_enforcement(
                self.con, self.now, self.target, self.target, 1000 - elapsed,
                {"soc_recovery": True})

    def test_three_or_more_noncharging_samples_log_a_distinct_failure(self):
        self.samples(0)
        with self.assertLogs("solinteg.dispatch", level="ERROR"):
            self.assertTrue(self.check())
        row = self.con.execute(
            "SELECT outcome, detail_json FROM control_actions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row[0], "command_accepted_but_not_enforced")
        self.assertEqual(json.loads(row[1])["sample_count"], 4)
        self.assertFalse(self.check())  # cooldown; one alert per reassert interval

    def test_actual_charging_is_never_reported_as_unenforced(self):
        self.samples(-300)
        self.assertFalse(self.check())
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM control_actions").fetchone()[0], 0)

    def test_waits_two_minutes_after_write_before_deciding(self):
        self.samples(0)
        self.assertFalse(self.check(elapsed=60))

    def test_insufficient_samples_are_not_claimed_as_hardware_failure(self):
        self.con.execute("INSERT INTO readings VALUES (?, 0)", (self.now.isoformat(),))
        self.assertFalse(self.check())

    def test_nonrecovery_and_disarmed_targets_do_not_trigger_the_monitor(self):
        self.samples(0)
        with mock.patch.object(dl, "ARMED", False), \
             mock.patch.object(dl.time, "monotonic", return_value=1000):
            self.assertFalse(dl.monitor_soc_recovery_enforcement(
                self.con, self.now, self.target, self.target, 800, {"soc_recovery": True}))
        with mock.patch.object(dl, "ARMED", True), \
             mock.patch.object(dl.time, "monotonic", return_value=1000):
            self.assertFalse(dl.monitor_soc_recovery_enforcement(
                self.con, self.now, self.target, self.target, 800, {}))


class PrioritizeSocRecoveryTests(unittest.TestCase):
    """Regression for the low-SoC incident: plan/dispatch must not undo recovery."""

    def setUp(self):
        self.previous_active = dl._soc_recovery_active
        dl._soc_recovery_active = True  # restart conservatively in the hysteresis band

    def tearDown(self):
        dl._soc_recovery_active = self.previous_active

    def run_decision(self, soc_pct: float, action: str = "discharge", power_w: int = 3800,
                     *, solar_skip: bool = False, expected_soc_kwh=9.0, surplus_w=0):
        with mock.patch.object(dl, "read_live_soc_kwh", return_value=soc_pct / 100 * dl.BATTERY_KWH), \
             mock.patch.object(dl, "read_live_solar_surplus_w", return_value=surplus_w):
            return dl.prioritize_soc_recovery(action, power_w, expected_soc_kwh, solar_skip,
                                               "planned", {}, datetime.now(UTC))

    def test_plan_discharge_is_overridden_and_bypasses_stale_plan_guard(self):
        a, w, expected, skip, detail, numbers = self.run_decision(dl.SOC_RECOVERY_ENTRY_PCT - 1)
        self.assertEqual((a, w, expected, skip), ("charge", dl.SOC_FLOOR_RECOVERY_W, None, False))
        self.assertTrue(numbers["soc_recovery"])
        self.assertIn("forcing", detail)

    def test_battery_save_does_not_win_over_recovery(self):
        a, w, *_ = self.run_decision(dl.SOC_RECOVERY_ENTRY_PCT - 1,
                                     action="discharge", power_w=50)
        self.assertEqual((a, w), ("charge", dl.SOC_FLOOR_RECOVERY_W))

    def test_live_load_demoted_charge_cannot_return_to_auto_at_low_soc(self):
        a, w, expected, skip, *_ = self.run_decision(
            dl.SOC_RECOVERY_ENTRY_PCT - 1, action="idle", power_w=0, solar_skip=True)
        self.assertEqual((a, w, expected, skip), ("charge", dl.SOC_FLOOR_RECOVERY_W, None, False))

    def test_recovery_runs_even_when_optimizer_plan_is_missing(self):
        with mock.patch.object(dl, "load_latest_plan", return_value=(None, None, None, None)), \
             mock.patch.object(dl, "maybe_request_replan"):
            no_plan = dl.decide(object(), datetime.now(UTC))
        self.assertEqual(no_plan[1], "idle")
        a, w, expected, skip, *_ = self.run_decision(
            dl.SOC_RECOVERY_ENTRY_PCT - 1, action=no_plan[1], power_w=no_plan[2])
        self.assertEqual((a, w, expected, skip), ("charge", dl.SOC_FLOOR_RECOVERY_W, None, False))

    def test_latched_recovery_continues_above_entry_until_exit(self):
        mid = (dl.SOC_RECOVERY_ENTRY_PCT + dl.SOC_RECOVERY_EXIT_PCT) / 2
        a, w, *_ = self.run_decision(mid, action="idle", power_w=0)
        self.assertEqual((a, w), ("charge", dl.SOC_FLOOR_RECOVERY_W))
        self.assertTrue(dl._soc_recovery_active)

        a, w, *_ = self.run_decision(dl.SOC_RECOVERY_EXIT_PCT, action="idle", power_w=0)
        self.assertEqual((a, w), ("idle", 0))
        self.assertFalse(dl._soc_recovery_active)

        a, w, *_ = self.run_decision(mid, action="discharge", power_w=500)
        self.assertEqual((a, w), ("discharge", 500))
        self.assertFalse(dl._soc_recovery_active)

    def test_recovery_absorbs_solar_surplus_instead_of_grid_minimum(self):
        a, w, *_ = self.run_decision(dl.SOC_RECOVERY_ENTRY_PCT - 1,
                                     action="freeze", power_w=0, surplus_w=2000)
        self.assertEqual((a, w), ("charge", 2000))

    def test_stale_poller_read_uses_direct_modbus_soc_when_armed(self):
        inv = FakeInverter(soc_pct=dl.SOC_RECOVERY_ENTRY_PCT - 1)
        with mock.patch.object(dl, "ARMED", True), \
             mock.patch.object(dl, "read_live_soc_kwh", return_value=None), \
             mock.patch.object(dl, "read_live_solar_surplus_w", return_value=0), \
             mock.patch.object(dl, "Inverter", return_value=inv):
            a, w, *_ = dl.prioritize_soc_recovery(
                "idle", 0, None, False, "missing live.json", {}, datetime.now(UTC))
        self.assertEqual((a, w), ("charge", dl.SOC_FLOOR_RECOVERY_W))
        self.assertTrue(inv.closed)

    def test_shadow_mode_does_not_open_modbus_for_stale_soc(self):
        with mock.patch.object(dl, "ARMED", False), \
             mock.patch.object(dl, "read_live_soc_kwh", return_value=None), \
             mock.patch.object(dl, "Inverter") as inv:
            a, w, *_ = dl.prioritize_soc_recovery(
                "idle", 0, None, False, "", {}, datetime.now(UTC))
        self.assertEqual((a, w), ("idle", 0))
        inv.assert_not_called()

    def test_realistic_recovery_transition_is_immediate_in_the_same_slot(self):
        a, w, expected, skip, detail, numbers = self.run_decision(
            dl.SOC_RECOVERY_ENTRY_PCT - 1, action="idle", power_w=0, solar_skip=True)
        effective_a, effective_w, _ = dl.effective_target(a, w, skip)
        self.assertTrue(dl.needs_apply(("18:15", effective_a, effective_w),
                                       ("18:15", "discharge", 50), effective_a, effective_w, False,
                                       soc_recovery=bool(numbers.get("soc_recovery"))))


if __name__ == "__main__":
    unittest.main()
