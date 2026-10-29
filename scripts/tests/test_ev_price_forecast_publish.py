"""Strict, read-only SE2 forecast publisher contract for XC40 dashboard."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import ev_price_forecast_publish as writer


NOW = datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc)
EXAMPLE = {
    "schema": "xc40_se2_price_forecast_input_v1",
    "area": "SE2",
    "local_date": "2026-10-29",
    "recommendation": "wait",
    "reason": "Helgen kan bli billigare än fredagen, men prognosen är osäker.",
    "best_charge_date": "2026-10-31",
    "savings_kr_20kwh": 3.50,
    "savings_kr_40kwh": 7.00,
    "days": [
        {"date":"2026-10-29","type":"published","price_ore_kwh":30,"low_ore_kwh":30,"high_ore_kwh":30},
        {"date":"2026-10-30","type":"published","price_ore_kwh":50,"low_ore_kwh":50,"high_ore_kwh":50},
        {"date":"2026-10-31","type":"forecast","price_ore_kwh":20,"low_ore_kwh":12,"high_ore_kwh":40},
        {"date":"2026-11-01","type":"forecast","price_ore_kwh":25,"low_ore_kwh":15,"high_ore_kwh":45},
    ],
    "sources": [
        {"name":"Nord Pool","url":"https://www.nordpoolgroup.com/"},
        {"name":"Open-Meteo","url":"https://open-meteo.com/en/docs"},
    ],
}


class ForecastTests(unittest.TestCase):
    def make(self, changes=None):
        value = copy.deepcopy(EXAMPLE)
        if changes:
            value.update(changes)
        return writer.validate(value, now=NOW)

    def test_success_and_sanitized_fields(self):
        result = self.make()
        self.assertEqual(result["schema"], writer.SCHEMA)
        self.assertEqual(result["device_writes"], 0)
        self.assertEqual(result["days"][2]["type"], "forecast")
        self.assertEqual(result["savings_basis"], "indicative_spot_difference_with_25pct_vat_excludes_network_and_energy_tax")
        self.assertEqual(result["generated_at_utc"], NOW.isoformat(timespec="seconds"))
        self.assertEqual(len(result["sources"]), 2)

    def test_rejects_expired_wrong_area_and_unknown_fields(self):
        for changes in (
            {"area":"SE3"},
            {"local_date":"2026-10-28"},
            {"schema":"other"},
            {"vin":"secret"},
            {"device_writes":1},
        ):
            with self.subTest(changes=changes), self.assertRaises(writer.ForecastError):
                self.make(changes)

    def test_rejects_fabricated_known_and_bad_prices(self):
        for fields in (
            {"type":"published"},
            {"price_ore_kwh":1000,"low_ore_kwh":10,"high_ore_kwh":20},
            {"price_ore_kwh":float("nan")},
            {"price_ore_kwh":True},
        ):
            with self.subTest(fields=fields):
                days = copy.deepcopy(EXAMPLE["days"])
                days[2].update(fields)
                with self.assertRaises(writer.ForecastError):
                    self.make({"days":days})

    def test_rejects_source_urls_with_tokens_or_unknown_hosts(self):
        for url in (
            "http://www.nordpoolgroup.com",
            "https://elprisetjustnu.se/prices?token=secret",
            "https://evil.example/data",
            "https://user:password@www.nordpoolgroup.com/",
            "https://www.nordpoolgroup.com.evil.example",
        ):
            with self.subTest(url=url), self.assertRaises(writer.ForecastError):
                self.make({"sources":[{"name":"source","url":url}]})

    def test_rejects_unrealistic_recommendation_and_savings(self):
        for changes in (
            {"recommendation":"urgent"},
            {"best_charge_date":"2026-10-29"},
            {"savings_kr_20kwh":1,"savings_kr_40kwh":5},
            {"reason":"token=private"},
            {"reason":"<script>"},
        ):
            with self.subTest(changes=changes), self.assertRaises(writer.ForecastError):
                self.make(changes)

    def test_publication_is_private_history_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            private = root/"var"
            public = root/"public"
            private.mkdir()
            public.mkdir()
            db = private/"forecasts.sqlite"
            target = public/"forecast.json"
            payload = self.make()
            with patch.dict(os.environ, {"XC40_FORECAST_TEST_OUTPUT":"1"}):
                self.assertTrue(writer.publish(payload, database=db, output=target))
                self.assertFalse(writer.publish(payload, database=db, output=target))
            self.assertEqual(json.loads(target.read_text()), payload)
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)
            self.assertEqual(db.stat().st_mode & 0o777, 0o600)
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM forecast_runs").fetchone()[0], 1)
            self.assertFalse(any("vin" in p.name for p in root.rglob("*")))

    def test_invalid_publication_does_not_mutate_prior_data(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/"var").mkdir()
            (root/"public").mkdir()
            output = root/"public"/"forecast.json"
            output.write_text("keep previous valid forecast")
            symlink = root/"public"/"new.json"
            symlink.symlink_to(output)
            with patch.dict(os.environ, {"XC40_FORECAST_TEST_OUTPUT":"1"}):
                with self.assertRaises(writer.ForecastError):
                    writer.publish(self.make(), database=root/"var"/"db.sqlite", output=symlink)
            self.assertEqual(output.read_text(), "keep previous valid forecast")

    def test_disallows_production_path_redirect(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(writer.ForecastError):
                writer.publish(self.make(), database=Path(temp)/"db", output=Path(temp)/"file")

    def test_demo_is_explicitly_synthetic_and_has_no_fake_sources(self):
        demo = writer.make_demo(now=NOW)
        self.assertEqual(demo["schema"], writer.SCHEMA)
        self.assertEqual(demo["area"], "SE2")
        self.assertEqual(demo["quality"], "demo_synthetic")
        self.assertEqual(demo["sources"], [])
        self.assertIn("TESTDATA", demo["reason"])
        self.assertEqual(demo["device_writes"], 0)
        self.assertEqual(len(demo["days"]), 7)
        self.assertEqual(demo["days"][0]["type"], "published")
        self.assertEqual(demo["days"][2]["type"], "forecast")
        self.assertEqual(demo["best_charge_date"], "2026-11-01")

    def test_demo_can_be_published_without_modifying_real_input_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/"var").mkdir()
            (root/"public").mkdir()
            with patch.dict(os.environ, {"XC40_FORECAST_TEST_OUTPUT":"1"}):
                self.assertTrue(writer.publish(
                    writer.make_demo(now=NOW),
                    database=root/"var"/"forecast.sqlite",
                    output=root/"public"/"forecast.json",
                ))
            self.assertEqual(
                json.loads((root/"public"/"forecast.json").read_text())["quality"],
                "demo_synthetic",
            )
            self.assertNotIn("quality", EXAMPLE)

    def test_no_hardware_or_network_imports(self):
        text = Path(writer.__file__).read_text()
        for forbidden in ("requests", "httpx", "socket", "modbus", "subprocess",
                          "write_register", "charging_start", "os.system"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
