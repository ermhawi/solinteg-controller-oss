"""Cost ledger tests: prices, source uncertainty, battery inventory and read-only export."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import ev_cost_ledger as cost

T = "2026-10-28T10:00:00Z"


def opening(kwh="30", value="4500", *, quality="metered"):
    return dict(type="opening", event_id="opening-1", occurred_at_utc=T,
                stored_dc_kwh=kwh, inventory_value_ore=value, quality=quality)


def charge(event_id="charge-1", *, dt="2026-10-28T11:00:00Z",
           stored="20", source="grid", ac="20", price="200",
           quality="estimated", aux="0"):
    return dict(type="charge", event_id=event_id, occurred_at_utc=dt,
                stored_dc_kwh=stored, direct_aux_ac_kwh=aux, quality=quality,
                tariff_version="2026-10", ac_sources=[
                    dict(source=source, ac_kwh=ac, price_ore_per_kwh=price)])


def drive(event_id="trip-1", *, dt="2026-10-28T12:00:00Z",
          energy="2.2", distance="10", quality="metered"):
    return dict(type="drive", event_id=event_id, occurred_at_utc=dt,
                consumed_dc_kwh=energy, distance_km=distance, quality=quality)


class CostLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "cost.sqlite")

    def tearDown(self):
        self.temp.cleanup()

    def test_weighted_price_and_kronor_ore_per_mil(self):
        self.assertTrue(cost.add(self.db, opening()))
        self.assertTrue(cost.add(self.db, charge(quality="metered")))
        before = cost.report(self.db)
        self.assertEqual(before["inventory"]["stored_dc_kwh"], 50)
        self.assertEqual(before["inventory"]["inventory_value_ore"], 8500)
        self.assertEqual(before["inventory"]["average_ore_per_dc_kwh"], 170)
        self.assertTrue(cost.add(self.db, drive()))
        result = cost.report(self.db)
        self.assertEqual(result["summary"]["cost_ore"], 374)
        self.assertEqual(result["summary"]["kr_per_mil"], 3.74)
        self.assertEqual(result["summary"]["quality"], "metered")
        self.assertEqual(result["inventory"]["average_ore_per_dc_kwh"], 170)
        self.assertEqual(result["inventory"]["stored_dc_kwh"], 47.8)

    def test_unknown_opening_value_stays_partial_not_free_energy(self):
        cost.add(self.db, opening(value=None))
        cost.add(self.db, charge())
        cost.add(self.db, drive())
        result = cost.report(self.db)
        self.assertEqual(result["summary"]["quality"], "partial")
        self.assertIsNone(result["summary"]["cost_ore"])
        self.assertIsNone(result["summary"]["kr_per_mil"])
        self.assertAlmostEqual(result["summary"]["unpriced_dc_kwh"], 1.32)
        self.assertAlmostEqual(result["summary"]["known_cost_ore"], 176)
        self.assertIsNone(result["inventory"]["average_ore_per_dc_kwh"])

    def test_solar_and_grid_blend_including_negative_export(self):
        cost.add(self.db, opening(kwh="0", value="0"))
        event = charge(stored="18", ac="20", price="150")
        event["ac_sources"] = [
            dict(source="grid", ac_kwh="12", price_ore_per_kwh="150"),
            dict(source="solar", ac_kwh="8", price_ore_per_kwh="-20"),
        ]
        cost.add(self.db, event)
        r = cost.report(self.db)
        self.assertEqual(r["inventory"]["stored_dc_kwh"], 18)
        self.assertEqual(r["inventory"]["known_value_ore"], 1640)
        self.assertEqual(r["inventory"]["average_ore_per_dc_kwh"], round(1640 / 18, 4))
        self.assertEqual(r["charged_ac_kwh_by_source"]["grid"], 12)
        self.assertEqual(r["charged_ac_kwh_by_source"]["solar"], 8)

    def test_aux_during_charge_and_parked_consumption_not_drive(self):
        cost.add(self.db, opening(kwh="0", value="0"))
        cost.add(self.db, charge(stored="18", ac="22", price="100", aux="2"))
        cost.add(self.db, dict(type="aux", event_id="preheat-1",
                               occurred_at_utc="2026-10-28T11:30:00Z",
                               consumed_dc_kwh="2", reason="preconditioning",
                               quality="estimated"))
        cost.add(self.db, drive(energy="2", distance="10"))
        r = cost.report(self.db)
        self.assertEqual(r["inventory"]["known_value_ore"], round(14000 / 9, 4))
        self.assertEqual(r["aux_consumed_dc_kwh"], 2)
        self.assertEqual(r["direct_aux_ac_kwh"], 2)
        self.assertEqual(r["direct_aux_cost_ore"], 200)
        self.assertEqual(r["direct_aux_unpriced_ac_kwh"], 0)
        self.assertEqual(r["summary"]["cost_ore"], round(2000 / 9, 4))
        self.assertEqual(r["summary"]["kr_per_mil"], 2.22)
        self.assertEqual(r["summary"]["distance_km"], 10)

    def test_zero_dc_charging_cannot_allocate_cost_to_empty_inventory(self):
        with self.assertRaisesRegex(ValueError, "zero DC gain"):
            cost.validate(charge(stored="0", ac="5", price="100"))
        event = charge(stored="0", ac="5", price="100", aux="5")
        cost.add(self.db, opening(kwh="0", value="0"))
        cost.add(self.db, event)
        result = cost.report(self.db)
        self.assertEqual(result["inventory"]["known_value_ore"], 0)
        self.assertEqual(result["direct_aux_cost_ore"], 500)

    def test_unpriced_external_session_never_uses_grid_price(self):
        cost.add(self.db, opening(kwh="0", value="0"))
        cost.add(self.db, charge(source="public", ac="10", stored="9", price=None))
        cost.add(self.db, drive(energy="3", distance="12"))
        r = cost.report(self.db)
        self.assertEqual(r["summary"]["quality"], "partial")
        self.assertIsNone(r["driving"][0]["cost_ore"])

    def test_public_charge_with_receipt_and_session_fee(self):
        cost.add(self.db, opening(kwh="0", value="0"))
        event = charge(source="public", ac="10", stored="9", price="300")
        event["ac_sources"][0]["fixed_cost_ore"] = "500"
        cost.add(self.db, event)
        self.assertEqual(cost.report(self.db)["inventory"]["known_value_ore"], 3500)

    def test_same_event_id_idempotent_but_changed_evidence_rejected(self):
        event = opening()
        self.assertTrue(cost.add(self.db, event))
        self.assertFalse(cost.add(self.db, dict(event)))
        with self.assertRaisesRegex(ValueError, "event_id conflict"):
            cost.add(self.db, dict(event, inventory_value_ore="100"))
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM ev_cost_events")
                             .fetchone()[0], 1)

    def test_reject_impossible_consumption_and_rollback(self):
        cost.add(self.db, opening(kwh="5", value="500"))
        with self.assertRaisesRegex(ValueError, "exceeds"):
            cost.add(self.db, drive(energy="6"))
        self.assertEqual(cost.report(self.db)["event_count"], 1)

    def test_zero_drive_consumption_cannot_be_misread_as_free_trip(self):
        with self.assertRaisesRegex(ValueError, "drive energy cannot be zero"):
            cost.validate(drive(energy="0"))

    def test_private_ledger_permissions_and_reject_unexpected_personal_data(self):
        self.assertTrue(cost.add(self.db, opening()))
        self.assertEqual(Path(self.db).stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(ValueError, "unexpected event fields"):
            cost.validate(dict(drive(), vin="hidden-car-identifier"))

    def test_late_dated_charge_replays_before_trip(self):
        cost.add(self.db, opening(kwh="0", value="0"))
        cost.add(self.db, charge())
        cost.add(self.db, drive())
        r = cost.report(self.db)
        self.assertEqual(r["summary"]["cost_ore"], 440)
        self.assertEqual(r["summary"]["kr_per_mil"], 4.4)

    def test_adjustment_cannot_fabricate_known_cost(self):
        cost.add(self.db, opening(kwh="5", value="500"))
        cost.add(self.db, dict(type="adjust", event_id="adjust-1",
                               occurred_at_utc="2026-10-28T10:30:00Z",
                               delta_dc_kwh="3", quality="estimated",
                               reason="unexplained higher SoC"))
        self.assertIsNone(cost.report(self.db)["inventory"]["inventory_value_ore"])

    def test_strict_time_decimal_validation_and_duplicate_order(self):
        cases = [
            dict(opening(), occurred_at_utc="2026-10-25T02:15:00"),
            dict(opening(), stored_dc_kwh="-1"),
            dict(opening(), inventory_value_ore="nan"),
            dict(opening(kwh="0", value="4500")),
            dict(opening(), quality="unknown"),
        ]
        for bad in cases:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cost.validate(bad)
        cost.add(self.db, opening())
        with self.assertRaisesRegex(ValueError, "exactly one opening"):
            cost.add(self.db, dict(opening(), event_id="opening-2",
                                   occurred_at_utc="2026-10-28T11:00:00Z"))

    def test_write_export_private_valid_json_and_no_devices(self):
        cost.add(self.db, opening(kwh="0", value="0"))
        target = str(Path(self.temp.name) / "report.json")
        report = cost.report(self.db, now=datetime(2026, 10, 28, tzinfo=timezone.utc))
        cost.write_export(target, report)
        saved = json.loads(Path(target).read_text())
        self.assertEqual(saved["schema_version"], 1)
        self.assertEqual(saved["writes_to_devices"], 0)
        self.assertEqual(saved["summary"]["quality"], "unavailable")
        self.assertEqual(Path(target).stat().st_mode & 0o777, 0o600)
        source = Path(cost.__file__).read_text()
        for forbidden in ("import evcc_ascii", "import inverter_control",
                          "socket.create_connection", "requests.post"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
