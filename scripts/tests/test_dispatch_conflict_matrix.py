"""D03 cross-layer conflict characterization, with no physical I/O.

Compares dispatcher orchestration to the pure source policy and independent
L3 cap, surfaces known above-reserve EV gaps. This is NOT release approval.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "services")]
from fakes import install_pymodbus_stub
install_pymodbus_stub()

import dispatch_loop as dl  # noqa: E402
from energy_priority_policy import SolarState, resolve as resolve_pure  # noqa: E402
from l3_charge_guard import cap_grid_charge_w, observe_l3_house_w  # noqa: E402

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


def readings(*, soc=16.0, pv=2500, house=900, battery=-400,
             grid=1200, ages=(65, 35)):
    return [{
        "timestamp": (NOW - timedelta(seconds=age)).isoformat(),
        "soc_pct": soc, "pv_w": pv, "house_load_w": house,
        "battery_w": battery, "grid_w": grid, "work_mode_raw": 0x303,
    } for age in ages]


class PurePriorityEnvelopeTests(unittest.TestCase):
    """Runs in clean HEAD too; never depends on uncommitted dispatcher wiring."""

    def test_pure_low_soc_solar_and_ev_priority(self):
        data = readings()
        def invoke(ev):
            return resolve_pure(
                "discharge", 650, now=NOW, readings=data, state=SolarState(),
                soc_pct=16, reserve_pct=20, save_w=50,
                recovery=False, ev_charging=ev,
            )
        self.assertEqual((invoke(False).winner, invoke(False).action),
                         ("solar_capture", "idle"))
        self.assertEqual((invoke(True).winner, invoke(True).power_w),
                         ("ev_grid_priority", 50))
        self.assertEqual((invoke(None).winner, invoke(None).power_w),
                         ("unverified_ev", 50))

    def test_pure_above_reserve_ev_still_keeps_plan(self):
        out = resolve_pure(
            "discharge", 650, now=NOW, readings=readings(soc=55),
            state=SolarState(), soc_pct=55, reserve_pct=20, save_w=50,
            recovery=False, ev_charging=True,
        )
        self.assertEqual((out.winner, out.action, out.power_w),
                         ("economic_plan", "discharge", 650))

    def test_pure_missing_soc_is_no_new_safety_claim(self):
        out = resolve_pure(
            "discharge", 650, now=NOW, readings=[], state=SolarState(),
            soc_pct=None, reserve_pct=20, save_w=50,
            recovery=False, ev_charging=False,
        )
        self.assertEqual((out.winner, out.action, out.power_w),
                         ("unverified_soc", "discharge", 650))

    def test_stale_phase_cap_is_independent_of_priority_policy(self):
        self.assertEqual(cap_grid_charge_w(
            6500, house_l3_w=None, balanced_output=False)[0], 300)


class IntegratedPriorityMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not hasattr(dl, "arbitrate_energy_priority"):
            raise AssertionError(
                "required opt-in D03 resolver missing from committed dispatcher"
            )

    def arbitrate(self, *, action="discharge", watts=650, ev=False, soc=16,
                  pv=2500, house=900, battery=-400, grid=1200,
                  samples=None, recovering=False, solar_skip=False,
                  state=None, expected=7.0):
        data = readings(soc=soc, pv=pv, house=house, battery=battery, grid=grid)             if samples is None else samples
        with mock.patch.object(dl, "ENERGY_PRIORITY_ENABLED", True), \
             mock.patch.object(dl, "PLANNER_RESERVE_PCT", 20.0), \
             mock.patch.object(dl, "BATTERY_SAVE_W", 50), \
             mock.patch.object(dl, "_recent_energy_readings", return_value=data), \
             mock.patch.object(dl, "_energy_ev_charging", return_value=ev), \
             mock.patch.object(dl, "Inverter") as never_live:
            result = dl.arbitrate_energy_priority(
                None, action, watts, expected, solar_skip, "planned",
                {"soc_recovery": True} if recovering else {}, NOW,
                state or SolarState())
            never_live.assert_not_called()
            return result

    def test_default_off_is_exact_golden_master_without_extra_io(self):
        for action, power_w, expected, solar_skip, recovery in (
            ("discharge", 650, 7.0, False, False),
            ("charge", 2500, 8.0, False, False),
            ("freeze", 0, None, False, False),
            ("idle", 0, None, False, False),
            ("charge", 2200, 8.0, True, False),
            ("charge", 300, None, False, True),
        ):
            with self.subTest(action=action, recovery=recovery), \
                 mock.patch.object(dl, "ENERGY_PRIORITY_ENABLED", False), \
                 mock.patch.object(dl, "_recent_energy_readings") as samples, \
                 mock.patch.object(dl, "_energy_ev_charging") as ev, \
                 mock.patch.object(dl, "Inverter") as real:
                state = SolarState()
                numbers = {"soc_recovery": True} if recovery else {}
                output = dl.arbitrate_energy_priority(
                    None, action, power_w, expected, solar_skip, "existing",
                    numbers, NOW, state)
                self.assertEqual(
                    output,
                    (action, power_w, expected, solar_skip, "existing", numbers, state))
                samples.assert_not_called()
                ev.assert_not_called()
                real.assert_not_called()

    def test_soc_recovery_wins_policy_arbitration_but_requires_upstream_charge(self):
        out = self.arbitrate(action="charge", watts=300, recovering=True)
        self.assertEqual(out[:4], ("charge", 300, 7.0, False))
        self.assertEqual(out[5]["energy_priority_winner"], "soc_recovery")
        # An inconsistent caller is not corrected by the arbitration stage:
        inconsistent = self.arbitrate(action="discharge", recovering=True)
        self.assertEqual(inconsistent[:2], ("discharge", 650))
        self.assertEqual(inconsistent[5]["energy_priority_winner"], "soc_recovery")

    def test_explicit_planned_charge_and_solar_freeze_preserved(self):
        planned = self.arbitrate(action="charge", watts=1800)
        self.assertEqual(planned[:4], ("charge", 1800, 7.0, False))
        self.assertEqual(planned[5]["energy_priority_winner"], "planned_charge")
        frozen = self.arbitrate(action="freeze", watts=0)
        self.assertEqual(frozen[0:2], ("freeze", 0))
        self.assertEqual(frozen[5]["energy_priority_winner"], "explicit_control")

    def test_stable_surplus_below_reserve_yields_general_when_ev_off(self):
        out = self.arbitrate()
        self.assertEqual(out[:4], ("idle", 0, None, False))
        self.assertEqual(out[5]["energy_priority_winner"], "solar_capture")
        self.assertTrue(out[6].capturing)
        self.assertEqual(out[5]["energy_priority_sample_count"], 2)

    def test_at_reserve_with_solar_surplus_vs_no_surplus(self):
        boundary = self.arbitrate(soc=20, ev=False)
        self.assertEqual(boundary[:4], ("idle", 0, None, False))
        self.assertEqual(boundary[5]["energy_priority_winner"], "solar_capture")
        deficit = self.arbitrate(
            soc=20, ev=False, pv=100, house=900, battery=350, grid=-450)
        self.assertEqual(deficit[:4], ("discharge", 50, None, False))
        self.assertEqual(deficit[5]["energy_priority_winner"], "economic_reserve")

    def test_unknown_or_charging_ev_never_claims_solar_general(self):
        for ev, winner in ((None, "unverified_ev"), (True, "ev_grid_priority")):
            with self.subTest(ev=ev):
                out = self.arbitrate(ev=ev)
                self.assertEqual(out[:4], ("discharge", 50, None, False))
                self.assertEqual(out[5]["energy_priority_winner"], winner)
                self.assertFalse(out[6].capturing)

    def test_ev_charging_above_reserve_preserves_plan_not_grid_priority(self):
        out = self.arbitrate(ev=True, soc=55)
        self.assertEqual(out[:4], ("discharge", 650, 7.0, False))
        self.assertEqual(out[5]["energy_priority_winner"], "economic_plan")
        # Other L3/EV budget guards must protect actual car charging.
        # This test is evidence of an unresolved guard ordering dependency.

    def test_missing_soc_preserves_discharge_even_when_solar_data_absent(self):
        out = self.arbitrate(samples=[])
        self.assertEqual(out[:4], ("discharge", 650, 7.0, False))
        self.assertEqual(out[5]["energy_priority_winner"], "unverified_soc")
        self.assertFalse(out[6].capturing)
        # Policy requires upstream stale-SoC and broker/EMS gating.

    def test_stale_or_invalid_ct_never_fabricates_solar_capture(self):
        for samples in (readings(ages=(180, 155)),
                        readings(pv=1500, house=900, battery=-400, grid=1200)):
            with self.subTest(samples=samples):
                out = self.arbitrate(samples=samples)
                self.assertEqual(out[:2], ("discharge", 50))
                self.assertEqual(out[5]["energy_priority_winner"], "unverified_solar")
                self.assertFalse(out[6].capturing)

    def test_solar_funding_skip_is_not_converted_into_planned_grid_charge(self):
        out = self.arbitrate(action="charge", watts=3500, solar_skip=True)
        self.assertEqual(out[:4], ("idle", 0, None, False))
        self.assertEqual(out[5]["energy_priority_winner"], "solar_capture")

    def test_low_and_high_phase_load_have_independent_charge_cap(self):
        baseline = cap_grid_charge_w(6500, house_l3_w=800,
                                     balanced_output=True)[0]
        high = cap_grid_charge_w(6500, house_l3_w=4500,
                                 balanced_output=True)[0]
        unknown = cap_grid_charge_w(6500, house_l3_w=None,
                                    balanced_output=False)[0]
        self.assertGreater(baseline, 300)
        self.assertEqual(high, 300)
        self.assertEqual(unknown, 300)

    def test_unknown_phase_quality_never_becomes_zero_load(self):
        sample = {
            "timestamp": (NOW - timedelta(seconds=180)).isoformat(),
            "meter_l1_w": -100, "meter_l2_w": -100, "meter_l3_w": -3000,
            "grid_w": -3200, "inverter_ac_w": 0,
            "house_load_w": 3200, "pv_w": 0,
        }
        self.assertIsNone(observe_l3_house_w(sample, NOW))
        self.assertEqual(cap_grid_charge_w(7000, house_l3_w=None,
                                           balanced_output=True)[0], 300)

    def test_evcc_c2_b2_a1_and_stale_source_have_explicit_tri_state(self):
        base = {
            "timestamp": NOW.isoformat(),
            "read_ok": True,
            "source": "waveshare_evcc_fc03_readonly",
            "state": "C2",
            "charging_detected": True,
            "measured_current_a": 6.1,
        }
        cases = [
            ("connected-charging", base, True),
            ("disconnected", {**base, "state": "A1",
                              "charging_detected": None,
                              "measured_current_a": None}, False),
            ("paused", {**base, "state": "B2",
                        "charging_detected": None,
                        "measured_current_a": .1}, False),
            ("unknown-c2", {**base, "charging_detected": None}, None),
            ("stale", {**base, "timestamp": (NOW - timedelta(minutes=3)).isoformat()}, None),
            ("untrusted-source", {**base, "source": "unverified"}, None),
        ]
        for label, sample, expected in cases:
            with self.subTest(label=label), \
                 mock.patch.object(dl.common, "read_json", return_value=sample):
                self.assertIs(dl._energy_ev_charging(NOW), expected)

    def test_spot_valuation_is_upstream_not_a_second_hidden_priority_rule(self):
        # The economic decision is already represented by the proposed action;
        # the arbitration stage does not consume positive/negative price itself.
        for spot_sign in ("negative", "positive"):
            with self.subTest(spot_sign=spot_sign):
                buy_action = self.arbitrate(action="charge", watts=2200, soc=50)
                sell_action = self.arbitrate(action="discharge", watts=650, soc=50)
                self.assertEqual(buy_action[5]["energy_priority_winner"], "planned_charge")
                self.assertEqual(sell_action[5]["energy_priority_winner"], "economic_plan")
                self.assertEqual((buy_action[0:2], sell_action[0:2]),
                                 (("charge", 2200), ("discharge", 650)))
        # Same result for both spot labels is not a full optimizer price replay.

    def test_missing_broker_snapshot_is_inhibited_before_any_inverter_open(self):
        with mock.patch.object(dl, "BROKER_STALE_GUARD_ENABLED", True), \
             mock.patch.object(dl, "record_broker_inhibit") as recorded, \
             mock.patch.object(dl, "Inverter") as physical, \
             mock.patch.object(dl, "apply_target") as write:
            result = dl.apply_decision(
                None, "slot", "charge", 2000, None, False,
                "planned", {}, False, broker_sample=None)
            self.assertEqual(result, ("inhibited_broker_telemetry", False))
            recorded.assert_called_once()
            physical.assert_not_called()
            write.assert_not_called()

    def test_manual_auto_is_not_mistaken_for_a_supported_optimizer_action(self):
        out = self.arbitrate(action="auto", watts=0)
        self.assertEqual(out[:4], ("auto", 0, 7.0, False))
        self.assertEqual(out[5]["energy_priority_winner"], "unsupported")
        # Real manual physical Auto is outside this policy; cannot infer
        # whether the dispatcher should take over an existing manual mode.


if __name__ == "__main__":
    unittest.main()
