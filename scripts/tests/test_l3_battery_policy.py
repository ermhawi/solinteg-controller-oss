"""Battery-only L3 coordination: offline, deterministic and dispatch-wiring tests.

The controller must never need Waveshare/EVCC and must retain its original
actions unless the independent configuration flag is explicitly enabled.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))

from fakes import install_pymodbus_stub  # noqa: E402

install_pymodbus_stub()

import dispatch_loop as dl  # noqa: E402
from l3_battery_policy import L3Observation, observe_l3, suggest_battery_target  # noqa: E402

NOW = datetime(2026, 10, 23, 14, 0, 20, tzinfo=timezone.utc)


def before_car():
    # Real recorded phase reading: 2026-10-23 13:58 UTC, before EV start.
    return {
        "timestamp": (NOW - timedelta(seconds=30)).isoformat(),
        "meter_l1_w": -244,
        "meter_l2_w": -381,
        "meter_l3_w": -799,
        "grid_w": -1425,
        "inverter_ac_w": -200,
        "house_load_w": 1225,
        "pv_w": 0,
    }


def during_car():
    # Real recorded phase reading: 2026-10-23 14:00 UTC while Volvo was charging.
    return {
        "timestamp": (NOW - timedelta(seconds=5)).isoformat(),
        "meter_l1_w": -218,
        "meter_l2_w": -334,
        "meter_l3_w": -4291,
        "grid_w": -4844,
        "inverter_ac_w": -121,
        "house_load_w": 4723,
        "pv_w": 0,
    }


class PhaseEstimationTests(unittest.TestCase):
    def test_car_start_is_visible_in_derived_house_l3_not_only_total_house(self):
        pre = observe_l3(before_car(), NOW)
        car = observe_l3(during_car(), NOW)
        self.assertIsNotNone(pre)
        self.assertIsNotNone(car)
        self.assertAlmostEqual(pre.house_l3_w, 799 - 200 / 3, places=2)
        self.assertAlmostEqual(car.house_l3_w, 4291 - 121 / 3, places=2)
        self.assertGreater(car.house_l3_w - pre.house_l3_w, 3400)

    def test_missing_phase_means_unknown_not_zero(self):
        d = during_car()
        d["meter_l3_w"] = None
        self.assertIsNone(observe_l3(d, NOW))

    def test_inconsistent_ct_sum_is_not_used_for_control(self):
        d = during_car()
        d["meter_l2_w"] = -2000
        self.assertIsNone(observe_l3(d, NOW))

    def test_inconsistent_house_total_is_rejected(self):
        d = during_car()
        d["house_load_w"] = 500
        self.assertIsNone(observe_l3(d, NOW))

    def test_stale_or_future_phase_data_is_unknown(self):
        d = during_car()
        d["timestamp"] = (NOW - timedelta(seconds=120)).isoformat()
        self.assertIsNone(observe_l3(d, NOW))
        d["timestamp"] = (NOW + timedelta(seconds=30)).isoformat()
        self.assertIsNone(observe_l3(d, NOW))

    def test_nan_phase_reading_is_invalid(self):
        d = during_car()
        d["meter_l3_w"] = float("nan")
        self.assertIsNone(observe_l3(d, NOW))

    def test_raw_export_is_not_confused_with_ev_demand(self):
        d = before_car()
        d["meter_l1_w"] = 1000
        d["meter_l2_w"] = 1000
        d["meter_l3_w"] = 1000
        d["grid_w"] = 3000
        d["inverter_ac_w"] = 4000
        d["house_load_w"] = 1000
        sample = observe_l3(d, NOW)
        self.assertIsNotNone(sample)
        self.assertAlmostEqual(sample.house_l3_w, 333.3, delta=0.1)


class BatteryCoordinationTests(unittest.TestCase):
    def setUp(self):
        self.car = observe_l3(during_car(), NOW)
        self.base = observe_l3(before_car(), NOW)

    def test_grid_charging_is_capped_when_volvo_starts_on_l3(self):
        suggestion = suggest_battery_target("charge", 7500, self.car)
        self.assertEqual((suggestion.action, suggestion.power_w), ("charge", 300))
        self.assertTrue(suggestion.metrics["l3_ev_like"])

    def test_regular_phase_load_does_not_limit_battery_charging(self):
        suggestion = suggest_battery_target("charge", 7500, self.base)
        self.assertEqual((suggestion.action, suggestion.power_w), ("charge", 7500))

    def test_existing_small_recovery_charge_is_never_increased(self):
        suggestion = suggest_battery_target("charge", 200, self.car)
        self.assertEqual((suggestion.action, suggestion.power_w), ("charge", 200))

    def test_adequate_solar_surplus_preserves_solar_battery_charging(self):
        car_with_pv = L3Observation(
            house_l3_w=4250, grid_l3_w=0, inverter_ac_w=6000, pv_surplus_w=7800, age_s=10)
        suggestion = suggest_battery_target("charge", 6000, car_with_pv)
        self.assertEqual(suggestion.power_w, 6000)

    def test_car_load_is_subtracted_from_existing_battery_discharge(self):
        suggestion = suggest_battery_target("discharge", 5000, self.car)
        self.assertEqual(suggestion.action, "discharge")
        self.assertEqual(suggestion.power_w, 1300)  # 5000 - (4251 - 600), rounded to 100W

    def test_small_discharge_is_kept_at_safe_nonzero_target_not_auto(self):
        suggestion = suggest_battery_target("discharge", 1800, self.car)
        self.assertEqual((suggestion.action, suggestion.power_w), ("discharge", 50))

    def test_idle_under_ev_like_load_avoids_automatic_self_consumption(self):
        suggestion = suggest_battery_target("idle", 0, self.car)
        self.assertEqual((suggestion.action, suggestion.power_w), ("discharge", 50))

    def test_idle_without_ev_load_remains_auto(self):
        suggestion = suggest_battery_target("idle", 0, self.base)
        self.assertEqual((suggestion.action, suggestion.power_w), ("idle", 0))

    def test_a_planned_freeze_or_hold_is_never_changed(self):
        for action in ("freeze", "reserve_hold"):
            with self.subTest(action=action):
                suggestion = suggest_battery_target(action, 0, self.car)
                self.assertEqual((suggestion.action, suggestion.power_w), (action, 0))


class DispatchWiringTests(unittest.TestCase):
    def _coordinate(self, *, mode="shadow", action="discharge", power=5000,
                    balance=True, missing=False, recovery=False, solar_skip=False):
        value = None if missing else during_car()
        with mock.patch.object(dl, "L3_BATTERY_MODE", mode), \
             mock.patch.object(dl.common, "read_json", return_value=value), \
             mock.patch.object(dl, "verify_l3_balanced_output", return_value=balance) as checked:
            output = dl.coordinate_l3_battery(
                action, power, 5.0, solar_skip, "original detail",
                {"soc_recovery": True} if recovery else {}, NOW)
        return output, checked

    def test_off_mode_preserves_entire_tuple_without_reading_telemetry(self):
        output, checked = self._coordinate(mode="0")
        self.assertEqual(output[:4], ("discharge", 5000, 5.0, False))
        self.assertEqual(output[4], "original detail")
        checked.assert_not_called()

    def test_shadow_mode_does_not_modify_battery_command(self):
        output, checked = self._coordinate(mode="shadow")
        self.assertEqual(output[:4], ("discharge", 5000, 5.0, False))
        self.assertEqual(output[5]["l3_policy_suggested_power_w"], 1300)
        checked.assert_not_called()

    def test_balanced_mode_unverified_refuses_active_changes(self):
        output, checked = self._coordinate(mode="1", balance=False)
        self.assertEqual(output[:4], ("discharge", 5000, 5.0, False))
        self.assertEqual(output[5]["l3_policy_status"], "inverter_balance_unknown_or_on")
        checked.assert_called_once()

    def test_active_verified_mode_prevents_car_from_draining_house_battery(self):
        output, checked = self._coordinate(mode="1", balance=True)
        self.assertEqual(output[:4], ("discharge", 1300, None, False))
        self.assertEqual(output[5]["l3_policy_status"], "applied")
        checked.assert_called_once()

    def test_missing_phase_data_does_not_change_any_existing_command(self):
        output, checked = self._coordinate(mode="1", missing=True)
        self.assertEqual(output[:4], ("discharge", 5000, 5.0, False))
        checked.assert_not_called()

    def test_existing_soc_floor_recovery_has_absolute_priority(self):
        output, checked = self._coordinate(mode="1", action="charge", power=300, recovery=True)
        self.assertEqual(output[:4], ("charge", 300, 5.0, False))
        checked.assert_not_called()

    def test_solar_shortfall_auto_demotion_is_corrected_for_active_ev(self):
        output, checked = self._coordinate(
            mode="1", action="charge", power=7500, solar_skip=True)
        self.assertEqual(output[:4], ("discharge", 50, None, False))
        checked.assert_called_once()

    def test_active_charge_has_300w_minimum_not_zero_auto(self):
        output, checked = self._coordinate(mode="1", action="charge", power=7500)
        self.assertEqual(output[:4], ("charge", 300, None, False))
        checked.assert_called_once()


class BalanceReadbackTests(unittest.TestCase):
    def setUp(self):
        self.old_until = dl._l3_checked_until
        self.old_balanced = dl._l3_balanced_output
        dl._l3_checked_until = 0
        dl._l3_balanced_output = False

    def tearDown(self):
        dl._l3_checked_until = self.old_until
        dl._l3_balanced_output = self.old_balanced

    def test_read_only_register_50006_off_allows_and_caches_result(self):
        class Fake:
            def __init__(self):
                self.closed = False
                self.reads = []

            def read_u16(self, addr):
                self.reads.append(addr)
                return 0

            def close(self):
                self.closed = True

        inv = Fake()
        with mock.patch.object(dl, "Inverter", return_value=inv) as cls, \
             mock.patch.object(dl.time, "monotonic", side_effect=[1000, 1000, 1001, 1002]):
            self.assertTrue(dl.verify_l3_balanced_output())
            self.assertTrue(dl.verify_l3_balanced_output())
        self.assertEqual(inv.reads, [50006])
        self.assertTrue(inv.closed)
        cls.assert_called_once()

    def test_unbalanced_output_register_prevents_active_changes(self):
        inv = mock.Mock()
        inv.read_u16.return_value = 1
        with mock.patch.object(dl, "Inverter", return_value=inv), \
             mock.patch.object(dl.time, "monotonic", return_value=100):
            with self.assertLogs("solinteg.dispatch", level="WARNING"):
                self.assertFalse(dl.verify_l3_balanced_output())
        inv.read_u16.assert_called_once_with(50006)
        inv.close.assert_called_once()

    def test_error_reading_balance_fails_closed_and_closes_connection(self):
        inv = mock.Mock()
        inv.read_u16.side_effect = OSError("inverter unavailable")
        with mock.patch.object(dl, "Inverter", return_value=inv), \
             mock.patch.object(dl.time, "monotonic", return_value=100):
            with self.assertLogs("solinteg.dispatch", level="WARNING"):
                self.assertFalse(dl.verify_l3_balanced_output())
        inv.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
