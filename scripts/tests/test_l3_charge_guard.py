"""Replayable charge-cap tests: no Modbus writes, Volvo or EVCC calls."""

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))
from fakes import install_pymodbus_stub
install_pymodbus_stub()

import dispatch_loop as dl
from l3_charge_guard import observe_l3_house_w, cap_grid_charge_w

NOW = datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)


def reading(l3=-1200):
    l1, l2, inverter = -500, -500, -300
    grid = l1 + l2 + l3
    return {
        "timestamp": NOW.isoformat(),
        "meter_l1_w": l1,
        "meter_l2_w": l2,
        "meter_l3_w": l3,
        "grid_w": grid,
        "inverter_ac_w": inverter,
        "house_load_w": inverter - grid,
        "pv_w": 0,
    }


class L3GuardPureTests(unittest.TestCase):
    def test_fresh_self_consistent_ct(self):
        self.assertAlmostEqual(observe_l3_house_w(reading(), NOW), 1100)

    def test_ev_like_load_takes_budget_away(self):
        l3_house = observe_l3_house_w(reading(-4800), NOW)
        self.assertGreater(l3_house, 4000)
        capped, detail = cap_grid_charge_w(6600, house_l3_w=l3_house, balanced_output=True)
        self.assertEqual(capped, 300)
        self.assertIn("4000", detail)

    def test_under_budget_allows_economic_charge(self):
        capped, _ = cap_grid_charge_w(
            6600, house_l3_w=observe_l3_house_w(reading(), NOW),
            balanced_output=True,
        )
        self.assertEqual(capped, 6600)

    def test_partial_headroom_reduces_charge(self):
        capped, _ = cap_grid_charge_w(7000, house_l3_w=2900, balanced_output=True)
        self.assertGreater(capped, 300)
        self.assertLess(capped, 7000)

    def test_stale_readings_fail_closed(self):
        stale = reading()
        stale["timestamp"] = (NOW - timedelta(seconds=120)).isoformat()
        self.assertIsNone(observe_l3_house_w(stale, NOW))

    def test_missing_or_inconsistent_ct_fail_closed(self):
        bad = reading()
        bad["meter_l2_w"] = None
        self.assertIsNone(observe_l3_house_w(bad, NOW))
        bad = reading()
        bad["grid_w"] += 600
        self.assertIsNone(observe_l3_house_w(bad, NOW))

    def test_balanced_register_unverified_fails_closed(self):
        capped, _ = cap_grid_charge_w(7000, house_l3_w=1100, balanced_output=False)
        self.assertEqual(capped, 300)
        capped, _ = cap_grid_charge_w(7000, house_l3_w=None, balanced_output=True)
        self.assertEqual(capped, 300)

    def test_low_power_recovery_always_preserved(self):
        capped, _ = cap_grid_charge_w(300, house_l3_w=None, balanced_output=False)
        self.assertEqual(capped, 300)


class L3GuardIntegrationTests(unittest.TestCase):
    def guard(self, *, mode="1", l3=-4800, balance=0, missing=False):
        fake = mock.Mock()
        fake.read_u16.return_value = balance
        with mock.patch.object(dl, "L3_CHARGE_GUARD_MODE", mode), \
             mock.patch.object(dl, "L3_CHARGE_BUDGET_W", 4000), \
             mock.patch.object(dl, "Inverter", return_value=fake), \
             mock.patch.object(dl.common, "read_json", return_value=None if missing else reading(l3)):
            result = dl.guard_l3_charge("charge", 6600, "planned cheap charge", {}, NOW)
        return result, fake

    def test_high_l3_when_armed_reduces_charge(self):
        (power, detail, numbers, clipped), fake = self.guard()
        self.assertEqual(power, 300)
        self.assertTrue(clipped)
        self.assertEqual(numbers["l3_charge_guard_suggested_w"], 300)
        fake.read_u16.assert_called_once_with(50006)
        fake.close.assert_called_once()

    def test_unbalanced_output_reduces_charge(self):
        (power, _, _, clipped), _ = self.guard(l3=-1000, balance=1)
        self.assertEqual(power, 300)
        self.assertTrue(clipped)

    def test_missing_ct_does_not_read_modbus_and_reduces_charge(self):
        (power, _, _, clipped), fake = self.guard(missing=True)
        self.assertEqual(power, 300)
        self.assertTrue(clipped)
        fake.read_u16.assert_not_called()

    def test_shadow_never_changes_physical_target(self):
        (power, detail, numbers, clipped), _ = self.guard(mode="shadow")
        self.assertEqual(power, 6600)
        self.assertFalse(clipped)
        self.assertEqual(numbers["l3_charge_guard_suggested_w"], 300)
        self.assertIn("SHADOW", detail)

    def test_recovery_keeps_cheap_grid_charge_when_not_solar_vetoed(self):
        low_soc = dl.BATTERY_KWH * 0.12
        with mock.patch.object(dl, "read_live_soc_kwh", return_value=low_soc), \
             mock.patch.object(dl, "read_live_solar_surplus_w", return_value=0), \
             mock.patch.object(dl, "_soc_recovery_active", True):
            action, power, expected, veto, _, numbers = dl.prioritize_soc_recovery(
                "charge", 6600, 2.0, False, "", {}, NOW)
        self.assertEqual((action, power, expected, veto), ("charge", 6600, None, False))
        self.assertTrue(numbers["soc_recovery_kept_planned_charge"])

    def test_recovery_respects_solar_veto(self):
        low_soc = dl.BATTERY_KWH * 0.12
        with mock.patch.object(dl, "read_live_soc_kwh", return_value=low_soc), \
             mock.patch.object(dl, "read_live_solar_surplus_w", return_value=0), \
             mock.patch.object(dl, "_soc_recovery_active", True):
            action, power, expected, veto, _, numbers = dl.prioritize_soc_recovery(
                "charge", 6600, 2.0, True, "", {}, NOW)
        self.assertEqual((action, power, expected, veto), ("charge", 300, None, False))
        self.assertNotIn("soc_recovery_kept_planned_charge", numbers)

    def test_19_to_21_hysteresis_prevents_12_to_16_recovery_loop(self):
        with mock.patch.object(dl, "SOC_RECOVERY_ENTRY_PCT", 19.5), \
             mock.patch.object(dl, "SOC_RECOVERY_EXIT_PCT", 21.0), \
             mock.patch.object(dl, "read_live_solar_surplus_w", return_value=0), \
             mock.patch.object(dl, "_soc_recovery_active", False):
            for soc, expected_recovery in ((19, True), (20, True), (21, False)):
                with mock.patch.object(dl, "read_live_soc_kwh", return_value=dl.BATTERY_KWH * soc / 100):
                    _, power, _, _, _, numbers = dl.prioritize_soc_recovery(
                        "discharge", 1000, None, False, "", {}, NOW)
                self.assertEqual(numbers.get("soc_recovery", False), expected_recovery)
                self.assertEqual(power, 300 if expected_recovery else 1000)


if __name__ == "__main__":
    unittest.main()
