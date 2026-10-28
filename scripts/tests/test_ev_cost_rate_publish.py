"""Sanitized rate publisher used by the XC40 HA cost-per-mile chart."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import ev_cost_rate_publish as writer

VALID = {
    "quality":"estimated_all_grid_historical_invoice_assumption_partial_coverage",
    "fragments_all_priced":True,
    "actual_grid_vs_solar_source_verified":False,
    "device_writes":0,
    "actual_cost_ore":None,
    "estimated_ac_kwh_covered_only":1.5,
    "estimated_all_grid_marginal_cost_kr_for_covered_energy":1.65,
    "covered_seconds":3900.0,
    "as_of_utc":"2026-10-28T20:25:00+00:00",
    "observed_charging_fragments":[
        {"observed_end_utc":"2026-10-28T20:22:45+00:00"}],
}


class PublishTests(unittest.TestCase):
    def test_derived_battery_rate_has_losses_explicit(self):
        result = writer.make_public_rate(VALID)
        self.assertEqual(result["price_kr_per_ac_kwh"],1.1)
        self.assertEqual(result["price_kr_per_reported_vehicle_kwh"],1.2222)
        self.assertEqual(result["assumed_charging_efficiency"],.9)
        self.assertEqual(result["state"],"estimated")
        self.assertEqual(result["schema"],writer.SCHEMA)
        self.assertIsNone(result["actual_cost_kr_per_mil"])
        self.assertEqual(result["device_writes"],0)
        data=json.dumps(result,ensure_ascii=False).lower()
        for forbidden in ("vin","token","gps","ocr","customer","payment"):
            self.assertNotIn(forbidden,data)

    def test_missing_price_or_insufficient_kwh_never_publish(self):
        for overrides in (
            {"estimated_ac_kwh_covered_only":.2},
            {"covered_seconds":100},
            {"fragments_all_priced":False},
            {"estimated_all_grid_marginal_cost_kr_for_covered_energy":None},
            {"quality":"unavailable"},
            {"device_writes":1},
            {"actual_cost_ore":0},
            {"actual_grid_vs_solar_source_verified":True},
            {"estimated_ac_kwh_covered_only":float("nan")},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(writer.PublishError):
                    writer.make_public_rate({**VALID,**overrides})

    def test_no_time_travel_or_older_than_thirty_days(self):
        for changed in (
            {"as_of_utc":"2026-12-28T20:25:00+00:00"},
            {"as_of_utc":"2026-10-28T20:21:00+00:00"},
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(writer.PublishError):
                    writer.make_public_rate({**VALID,**changed})

    def test_negative_tariff_is_not_automatically_zero(self):
        d={**VALID,
           "estimated_all_grid_marginal_cost_kr_for_covered_energy":-.15}
        result=writer.make_public_rate(d)
        self.assertEqual(result["price_kr_per_ac_kwh"],-.1)

    def test_atomic_output_is_idempotent_and_bounded(self):
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/"public.json"
            d=writer.make_public_rate(VALID)
            with patch.dict(os.environ,{"EV_COST_RATE_TEST_OUTPUT":"1"}):
                self.assertTrue(writer.publish(path,d))
                self.assertFalse(writer.publish(path,d))
                self.assertEqual(json.loads(path.read_text()),d)
                self.assertEqual(path.stat().st_mode&0o777,0o644)
                self.assertTrue(writer.publish(path,{**d,"assumed_charging_efficiency":0.88}))
                self.assertEqual(json.loads(path.read_text())["assumed_charging_efficiency"],0.88)
                self.assertEqual(len(list(Path(t).iterdir())),1)

    def test_no_nonpublic_write_without_explicit_test_switch(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/"rate.json"
            with self.assertRaisesRegex(writer.PublishError,"publication path"):
                writer.publish(p,writer.make_public_rate(VALID))
            self.assertFalse(p.exists())

    def test_symlink_path_cannot_be_written(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            secret=root/"target"
            secret.write_text("preserve")
            p=root/"rate.json"
            p.symlink_to(secret)
            with patch.dict(os.environ,{"EV_COST_RATE_TEST_OUTPUT":"1"}):
                with self.assertRaises(writer.PublishError):
                    writer.publish(p,writer.make_public_rate(VALID))
            self.assertEqual(secret.read_text(),"preserve")

    def test_code_has_no_hardware_mutations_or_network(self):
        text=Path(writer.__file__).read_text().lower()
        for forbidden in ("socket.", "requests.", "httpx", "aiohttp",
                          "write_register", "fc10", "import inverter_control",
                          "import evcc_observer", "charging_start", "modbus_client"):
            self.assertNotIn(forbidden,text)
        self.assertIn("EV_COST_RATE_PUBLISH_ENABLED",Path(writer.__file__).read_text())


if __name__=="__main__":
    unittest.main()
