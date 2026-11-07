"""D02 executable Given/When/Then behavior contract; 0 physical I/O.

These are the *currently evidenced* pure-policy decisions, not a replacement
for the installed 13-stage dispatch pipeline or a claim of physical safety.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "services"))
from energy_priority_policy import resolve, SolarState  # noqa: E402
import ev_l3_metered_policy as ev_l3  # noqa: E402
import ev_grid_priority_idle_strategy as idle  # noqa: E402
import evcc_manual_lock as manual  # noqa: E402
import test_ev_l3_metered_policy as ev_fixture  # noqa: E402
import test_ev_grid_priority_idle_strategy as idle_fixture  # noqa: E402
import test_energy_priority_conflicts as priority_fixture  # noqa: E402

NOW = datetime(2026, 11, 7, 9, tzinfo=timezone.utc)


class InstalledIntentGivenWhenThenTests(unittest.TestCase):
    def test_recovery_wins_without_implicitly_applying_battery_power(self):
        # Given low SoC and recovery already latched; when resolver executes,
        # then it preserves the pre-existing recovery decision.
        decision = resolve(
            "charge", 500, now=NOW, readings=[], state=SolarState(),
            soc_pct=13, reserve_pct=20, save_w=50,
            recovery=True, ev_charging=None,
        )
        self.assertEqual((decision.winner, decision.action, decision.power_w),
                         ("soc_recovery", "charge", 500))

    def test_economic_reserve_pv_surplus_uses_verified_ev_off_only(self):
        # Given fresh inverter samples and below-reserve SoC, verified EV off.
        decision = priority_fixture.decision(
            "discharge", 650, soc=16, ev=False,
        )
        self.assertEqual((decision.winner, decision.action, decision.power_w),
                         ("solar_capture", "idle", 0))
        self.assertEqual(decision.sample_count, 2)

    def test_unknown_ev_does_not_claim_it_is_off(self):
        result = priority_fixture.decision("discharge", 650, soc=16, ev=None)
        self.assertEqual((result.winner, result.action, result.power_w),
                         ("unverified_ev", "discharge", 50))
        self.assertFalse(result.state.capturing)

    def test_price_optimizer_decision_above_reserve_is_retained_by_pure_policy(self):
        # This is observed behavior, NOT proof of EV grid-first in the dispatcher.
        result = priority_fixture.decision("discharge", 650, soc=55, ev=True)
        self.assertEqual((result.winner, result.action, result.power_w),
                         ("economic_plan", "discharge", 650))

    def test_grid_first_pv_priority_budget_is_not_an_executable_measurement(self):
        budget = ev_l3.solar_priority_budget(
            ev_w=1380, pv_w=4000, non_ev_house_w=1000,
            battery_full_verified=False,
        )
        self.assertEqual(budget["ev_grid_target_w_est"], 1380)
        self.assertEqual(budget["ev_solar_allowance_w_est"], 0)
        self.assertEqual(budget["house_battery_solar_charge_headroom_w_est"], 3000)
        self.assertTrue(budget["energy_allocation_is_control_target_not_proven_flow"])
        full = ev_l3.solar_priority_budget(
            ev_w=1380, pv_w=4000, non_ev_house_w=1000,
            battery_full_verified=True,
        )
        self.assertEqual(full["ev_solar_allowance_w_est"], 1380)
        self.assertEqual(full["ev_grid_target_w_est"], 0)

    def test_l3_hard_limit_remains_independent_of_economic_ev_proposal(self):
        live, evcc = ev_fixture.charger(16, pwm_raw=267)
        advice = ev_l3.assess(
            "discharge", 2300, copy.deepcopy(live), copy.deepcopy(evcc),
            now=ev_fixture.NOW,
        )
        self.assertEqual(advice.state, "phase_import_bound")
        self.assertIsNone(advice.candidate_power_w)
        self.assertFalse(advice.measurements["physical_control_authorized"])
        self.assertEqual(advice.measurements["device_writes"], 0)

    def test_manual_auto_intervention_disables_idle_model_in_non_auto_mode(self):
        live = {**idle_fixture.BASE, "work_mode_raw": 0x303}
        advice = idle.evaluate(
            "idle", live, idle_fixture.ADVICE, idle_fixture.window(),
            now=idle_fixture.NOW, age_of_dispatch_s=300,
        )
        self.assertEqual(advice.state, "dispatch_mode_mismatch")
        self.assertIsNone(advice.hypothetical_mode)
        self.assertEqual(advice.measurements["device_writes"], 0)

    def test_owned_ev_price_pause_blocks_manual_resumption(self):
        for state in ("owned", "pending", "releasing"):
            with self.subTest(state=state):
                with patch.object(manual.ownership, "read_owner",
                                  return_value={"state": state}):
                    with self.assertRaises(manual.ManualWriterBlocked):
                        manual._assert_no_active_price_pause(Path("/synthetic-not-accessed"))


if __name__ == "__main__":
    unittest.main()
