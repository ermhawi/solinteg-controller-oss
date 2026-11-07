"""E03: Settings-revision race tests across EVCC locks, with fake FC10 only."""
from __future__ import annotations

import sys
from pathlib import Path
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_ev_price_pause_owner as existing  # noqa: E402

ctl = existing.ctl


class PriceSettingsRevisionRaceTests(unittest.TestCase):
    def setUp(self):
        self.site = existing.OwnershipTests(methodName="runTest")
        self.site.setUp()

    def tearDown(self):
        self.site.tearDown()

    def settings(self, **changes):
        return {
            "revision": "f8b489f3-0e2d-4702-a877-a22451fd32a1",
            "charge_mode": "economy", "departure_local_time": "07:00",
            "max_buy_ore_per_kwh": None,
            **changes,
        }

    def session(self):
        return {
            **existing.SESSION,
            "energy_quality": ctl.empirical.QUALITY,
            "settings_revision": self.settings()["revision"],
            "departure_local_time": "07:00",
            "max_buy_ore_per_kwh": None,
        }

    def test_unmodified_revision_one_pause_and_durable_owner(self):
        with patch.object(ctl, "load_settings", return_value=self.settings()) as loaded:
            result = self.site.invoke(session=self.session())
        self.assertEqual((result["state"], result["writes"]), ("pause", 1), result)
        self.assertEqual(self.site.driver.calls, [(existing.RAW_PAUSE, 100, True)])
        self.assertEqual(existing.ownership.read_owner(self.site.owner)["state"], "owned")
        loaded.assert_called_once()

    def test_changed_revision_blocks_even_if_all_other_settings_match(self):
        with patch.object(ctl, "load_settings", return_value=self.settings(
            revision="c8b489f3-0e2d-4702-a877-a22451fd32a1"
        )):
            result = self.site.invoke(session=self.session())
        self.assertEqual((result["state"], result["writes"]), ("inhibited", 0), result)
        self.assertIn("preference changed", result["reason"])
        self.assertEqual(self.site.driver.calls, [])
        self.assertFalse(self.site.owner.exists())

    def test_same_revision_but_modified_mode_departure_or_price_blocks(self):
        changes = (
            {"charge_mode": "deadline"},
            {"departure_local_time": "10:00"},
            {"max_buy_ore_per_kwh": 75.0},
        )
        for changed in changes:
            with self.subTest(changed=changed):
                with patch.object(ctl, "load_settings", return_value=self.settings(**changed)):
                    result = self.site.invoke(session=self.session())
                self.assertEqual((result["state"], result["writes"]), ("inhibited", 0), result)
                self.assertEqual(self.site.driver.calls, [])
                self.assertFalse(self.site.owner.exists())

    def test_settings_unreadable_between_preflight_and_fc10_blocks(self):
        with patch.object(ctl, "load_settings", side_effect=ValueError("synthetic corrupt settings")):
            result = self.site.invoke(session=self.session())
        self.assertEqual((result["state"], result["writes"]), ("inhibited", 0), result)
        self.assertIn("unreadable", result["reason"])
        self.assertEqual(self.site.driver.calls, [])

    def test_manual_app_pause_without_owner_never_auto_resumes(self):
        self.site.set_paused()
        result = self.site.invoke(price=existing.CHEAP, session=self.session())
        self.assertEqual(result["writes"], 0)
        self.assertEqual(self.site.driver.calls, [])

    def test_uncertain_fc10_persists_unresolved_owner_and_never_replays(self):
        self.site.driver.fail = True
        with self.assertRaises(existing.EvccUncertainWrite):
            with patch.object(ctl, "load_settings", return_value=self.settings()):
                self.site.invoke(session=self.session())
        state = existing.ownership.read_owner(self.site.owner)
        self.assertEqual(state["state"], "pending")
        count = len(self.site.driver.calls)
        self.site.driver.fail = False
        with patch.object(ctl, "load_settings", return_value=self.settings()):
            result = self.site.invoke(session=self.session())
        self.assertEqual(result["writes"], 0)
        self.assertEqual(len(self.site.driver.calls), count)


if __name__ == "__main__":
    unittest.main()
