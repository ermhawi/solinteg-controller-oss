"""EVCC economy executor: price-driven one-shot commands require verified inputs."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import ev_price_controller as ctl  # noqa: E402
from ev_price_policy import PriceDecision  # noqa: E402
from evcc_ascii import RAW_PAUSE  # noqa: E402

NOW = datetime(2026, 11, 4, 14, 10, tzinfo=timezone.utc)
SESSION = {"charge_mode": "economy", "remaining_ac_kwh": 11.3}
EVCC = {
    "timestamp": NOW.isoformat(), "source": "waveshare_evcc_fc03_readonly",
    "read_ok": True, "physical_control_eligible": False,
    "writes": 0, "state": "C2", "configured_pwm_raw": 100,
    "pilot_pwm_raw": 100, "channel_currents_a": [0.1, 6.0, 0.1],
}
LIVE = {
    "timestamp": NOW.isoformat(), "grid_w": -400, "meter_l3_w": -300,
    "grid_unbalanced_output_raw": 0,
}
CHEAP = PriceDecision(6, 80.2, 14.3, "cheap", charge_mode="economy")
EXPENSIVE = PriceDecision(0, 180.2, 20.1, "expensive", charge_mode="economy")


class PriceControllerTests(unittest.TestCase):
    def choose(self, price=CHEAP, evcc=None, live=None, session=SESSION,
               price_pause_owned=False):
        return ctl.choose_action(
            session=session, price=price,
            evcc=EVCC if evcc is None else evcc,
            live=LIVE if live is None else live,
            now=NOW, price_pause_owned=price_pause_owned)

    def test_expensive_price_pauses_once_with_cas(self):
        action = self.choose(price=EXPENSIVE)
        self.assertEqual((action.action, action.expected_raw, action.target_raw),
                         ("pause", 100, RAW_PAUSE))

    def test_cheap_price_resumes_only_6a_and_in_budget(self):
        paused = {**EVCC, "state": "B2", "configured_pwm_raw": RAW_PAUSE,
                  "pilot_pwm_raw": 267, "channel_currents_a": [0.1, 0.1, 0.1]}
        self.assertEqual(self.choose(evcc=paused).action, "inhibited")
        action = self.choose(evcc=paused, price_pause_owned=True)
        self.assertEqual((action.action, action.expected_raw, action.target_raw),
                         ("resume", RAW_PAUSE, 100))
        self.assertEqual(self.choose().action, "no_change")
        self.assertEqual(self.choose(price=EXPENSIVE, evcc=paused).action, "no_change")
        self.assertEqual(self.choose(evcc=paused, price_pause_owned=False).reason,
                         "pause owner unknown; manual pause has priority")

    def test_paused_state_requires_exact_pilot_and_low_current(self):
        paused = {**EVCC, "state": "B2", "configured_pwm_raw": RAW_PAUSE,
                  "pilot_pwm_raw": 267, "channel_currents_a": [0.1, 0.1, 0.1]}
        self.assertEqual(self.choose(evcc=paused, price_pause_owned=True).action, "resume")
        for updated in (
            {"pilot_pwm_raw": RAW_PAUSE},
            {"pilot_pwm_raw": 266},
            {"state": "C2"},
            {"state": "A1"},
            {"channel_currents_a": [0.1, 5.6, 0.1]},
            {"channel_currents_a": [None, 0.1, None]},
        ):
            with self.subTest(updated=updated):
                row = {**paused, **updated}
                self.assertEqual(
                    self.choose(evcc=row, price_pause_owned=True).action, "inhibited")

    def test_no_verified_session_never_writes(self):
        self.assertEqual(self.choose(session=None).action, "inhibited")
        self.assertEqual(self.choose(price=PriceDecision(0, None, None, "missing", charge_mode="economy")).action,
                         "inhibited")
        self.assertEqual(self.choose(price=PriceDecision(6, 70, 9, "other", charge_mode="deadline")).action,
                         "inhibited")

    def test_reached_80_percent_favors_pause_even_if_price_missing(self):
        reached = {**SESSION, "remaining_ac_kwh": 0.0}
        stale_price = PriceDecision(0, None, None, "80 percent reached",
                                    charge_mode="economy")
        self.assertEqual(self.choose(price=stale_price, session=reached).action, "pause")

    def test_stale_and_corrupt_hardware_never_writes(self):
        for bad in [
            {**EVCC, "timestamp": (NOW-timedelta(seconds=70)).isoformat()},
            {**EVCC, "source": "unknown"},
            {**EVCC, "read_ok": False},
            {**EVCC, "writes": 1},
            {**EVCC, "pilot_pwm_raw": RAW_PAUSE},
            {**EVCC, "state": "F2"},
            {**EVCC, "channel_currents_a": [1.1, 6, .1]},
        ]:
            with self.subTest(bad=bad):
                self.assertEqual(self.choose(evcc=bad).action, "inhibited")

    def test_fuse_preflight_is_physical_not_house_ex_ev_budget(self):
        paused = {**EVCC, "state": "B2", "configured_pwm_raw": RAW_PAUSE,
                  "pilot_pwm_raw": 267, "channel_currents_a": [0.1, .1, .1]}
        for measurement in [{**LIVE, "meter_l3_w": -2200},
                            {**LIVE, "grid_w": -5200},
                            {**LIVE, "grid_unbalanced_output_raw": 1},
                            {**LIVE, "timestamp": (NOW-timedelta(seconds=70)).isoformat()}]:
            with self.subTest(measurement=measurement):
                self.assertEqual(self.choose(evcc=paused, live=measurement).action, "inhibited")

    def test_solar_only_price_is_not_grid_permission(self):
        solar = PriceDecision(0, 180, -5, "solar opportunity", solar_allowed=True,
                              charge_mode="economy")
        action = self.choose(price=solar)
        self.assertEqual(action.action, "pause")

    def test_controller_disabled_no_hardware_reads_or_writes(self):
        with patch.dict(os.environ, {"EV_PRICE_CONTROL_ENABLED": "0"}, clear=True), \
             patch.object(ctl, "read_ev_session") as read_session, \
             patch.object(ctl, "EvccAscii") as driver:
            result = ctl.run_once(now=NOW)
        self.assertEqual(result["writes"], 0)
        self.assertEqual(result["state"], "disabled")
        read_session.assert_not_called()
        driver.assert_not_called()

    def test_controller_missing_calibrated_session_never_connects_to_evcc(self):
        with patch.dict(os.environ, {
            "EV_PRICE_CONTROL_ENABLED": "1", "EVCC_PRICE_CONTROL_ARMED": "1",
            "EV_SESSION_PATH": "/inexistent-session.json",
        }, clear=True), patch.object(ctl, "read_ev_session", return_value=None), \
             patch.object(ctl, "EvccAscii") as driver:
            result = ctl.run_once(now=NOW)
        self.assertEqual(result["writes"], 0)
        self.assertEqual(result["state"], "inhibited")
        driver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
