"""EVCC shadow integration: shared price advice must never write hardware."""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))
import evcc_service


NOW = datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc)


class ShadowTests(unittest.TestCase):
    def _tick(self, *, session=None, price=None, live_export=-300, streak=0):
        client = mock.Mock()
        client.status.return_value = SimpleNamespace(
            measured_current_a=0.0, state="B2", configured_raw=100, pilot_raw=100,
        )
        live = dict(timestamp=NOW.isoformat(), soc_pct=100, grid_w=live_export,
                    battery_w=0, pv_w=0, house_load_w=900)
        with mock.patch.dict("os.environ", {"EV_SESSION_PATH": "/ignored",
                                            "EVCC_MAX_CURRENT_A": "6"}), \
             mock.patch("evcc_service.datetime") as clock, \
             mock.patch("evcc_service.common.read_json", return_value=live), \
             mock.patch("evcc_service.read_ev_session", return_value=session), \
             mock.patch("evcc_service.read_house_price_plan", return_value=price) as read:
            clock.now.return_value = NOW
            result, next_streak = evcc_service.monitor_once(client, streak)
        client.set_limit_raw.assert_not_called()
        return result, next_streak, read

    def test_price_window_allows_grid_advice_but_not_a_physical_command(self):
        p = dict(logged_at=NOW.isoformat(),
                 inputs=[], dispatch=[])
        for i, price in enumerate((20, 120, 180, 240)):
            local = (NOW + timedelta(minutes=15*i) +
                     timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
            p["inputs"].append(dict(startTime=local, buyPrice=price,
                                    sellPrice=5, priceSource="actual"))
            p["dispatch"].append(dict(startTime=local, solarExportKwh=0))
        data, streak, read = self._tick(
            session=dict(remaining_ac_kwh=0.3,
                         departure_at_utc=NOW + timedelta(hours=1)),
            price=p,
        )
        self.assertEqual(read.call_count, 1)
        self.assertEqual(data["mode"], "shadow")
        self.assertEqual(data["solar_suggested_a"], 0)
        self.assertEqual(data["grid_topup_suggested_a"], 6)
        self.assertEqual(data["charging_suggested_a"], 6)
        self.assertEqual(data["charging_permitted_a"], 0)
        self.assertEqual(data["price_buy_ore_kwh"], 20)
        self.assertEqual(data["writes"], 0)
        self.assertEqual(streak, 0)  # Cheap grid does not fake stable solar history.

    def test_missing_ev_session_keeps_grid_off_without_reading_price_db(self):
        data, streak, read = self._tick()
        read.assert_not_called()
        self.assertEqual(data["grid_topup_suggested_a"], 0)
        self.assertEqual(data["charging_suggested_a"], 0)
        self.assertEqual(data["writes"], 0)
        self.assertEqual(streak, 0)

    def test_economy_without_departure_only_suggests_low_price_period(self):
        p = dict(logged_at=NOW.isoformat(), inputs=[], dispatch=[])
        for i, buy in enumerate((30,) + (160,) * 31):
            local = (NOW + timedelta(minutes=15 * i) +
                     timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
            p["inputs"].append(dict(startTime=local, buyPrice=buy,
                                    sellPrice=5, priceSource="actual"))
            p["dispatch"].append(dict(startTime=local, solarExportKwh=0))
        s = dict(charge_mode="economy", remaining_ac_kwh=1,
                 target_soc_pct=80, departure_at_utc=None)
        data, streak, _ = self._tick(session=s, price=p)
        self.assertEqual(data["mode"], "shadow")
        self.assertEqual(data["charge_mode"], "economy")
        self.assertIsNone(data["departure_at_utc"])
        self.assertEqual(data["grid_topup_suggested_a"], 6)
        self.assertEqual(data["charging_suggested_a"], 6)
        self.assertEqual(data["charging_permitted_a"], 0)
        self.assertEqual(data["writes"], 0)
        self.assertEqual(streak, 0)

    def test_economy_stops_at_80_without_hardware_actions(self):
        p = dict(logged_at=NOW.isoformat(), inputs=[], dispatch=[])
        for i in range(32):
            local = (NOW + timedelta(minutes=15 * i) +
                     timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
            p["inputs"].append(dict(startTime=local, buyPrice=0,
                                    sellPrice=0, priceSource="actual"))
            p["dispatch"].append(dict(startTime=local, solarExportKwh=0))
        s = dict(charge_mode="economy", remaining_ac_kwh=0,
                 target_soc_pct=80, departure_at_utc=None)
        data, _, _ = self._tick(session=s, price=p, live_export=2500)
        self.assertEqual(data["charge_mode"], "economy")
        self.assertEqual(data["grid_topup_suggested_a"], 0)
        self.assertEqual(data["charging_suggested_a"], 0)
        self.assertEqual(data["solar_suggested_a"], 0)
        self.assertEqual(data["writes"], 0)

    def test_deadline_forces_advice_at_high_price_without_actuation(self):
        s = dict(charge_mode="deadline", target_soc_pct=80,
                 remaining_ac_kwh=2.0,
                 departure_at_utc=NOW + timedelta(minutes=20))
        data, _, _ = self._tick(session=s, price=None)
        self.assertEqual(data["charge_mode"], "deadline")
        self.assertEqual(data["grid_topup_suggested_a"], 6)
        self.assertEqual(data["charging_suggested_a"], 6)
        self.assertEqual(data["charging_permitted_a"], 0)
        self.assertEqual(data["writes"], 0)


if __name__ == "__main__":
    unittest.main()
