"""Invoice-provenance aware, read-only and gap-aware EV AC cost preview."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import ev_ac_capture as ac
import ev_cost_price_capture as price_capture
import ev_ac_cost_preview as cost
import ev_ac_sessions as sessions
import ev_invoice_tariff as invoice

UTC = timezone.utc
START = datetime(2026, 10, 28, 19, 14, 45, tzinfo=UTC)
INVOICE_FILE = Path(__file__).resolve().parents[2] / "docs" / "ev-invoice-rate-evidence-2026.json"


def record(start: datetime, offset: int = 0) -> dict:
    at = start + timedelta(seconds=offset)
    d = {
        "timestamp": at.isoformat(),
        "source": "waveshare_evcc_fc03_readonly",
        "read_ok": True, "state": "C2",
        "configured_pwm_raw": 100, "pilot_pwm_raw": 100,
        "channel_currents_a": [0.1, 6.0, 0.1],
        "measured_current_a": 6.2,
        "charging_detected": True,
        "physical_control_eligible": False,
        "modbus_function": "FC03", "writes": 0,
    }
    return ac.parse(d, now=at + timedelta(seconds=1))


def price_row(start: datetime, value: float = 100.) -> dict:
    epoch = int(start.timestamp() * 1000)
    return {
        "slot_start_epoch_ms": epoch,
        "slot_end_epoch_ms": epoch + 900000,
        "source_date": start.date().isoformat(),
        "source_logged_at_utc": "2026-10-28T17:00:00+00:00",
        "source_sha256": "e" * 64,
        "buy_before_grid_ore": str(value),
        "sell_model_ore": "0",
        "quality": price_capture.QUALITY,
    }


class PricePreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        p = Path(self.temp.name)
        self.ac = str(p / "ac.sqlite")
        self.price = str(p / "price.sqlite")
        self.invoice = str(p / "tariffs.json")
        Path(self.invoice).write_bytes(INVOICE_FILE.read_bytes())
        self.now = START + timedelta(hours=1)

    def tearDown(self):
        self.temp.cleanup()

    def put(self, start: datetime = START, offsets: tuple[int,...] = (0,30)):
        for seconds in offsets:
            ac.store(self.ac, record(start,seconds))

    def prices(self, start: datetime | None = None, first: float = 100,
               second: float = 300):
        start = start or START.replace(minute=0,second=0)
        price_capture.append(self.price,[
            price_row(start, first), price_row(start+timedelta(minutes=15), second)])

    def calc(self, now: datetime | None = None, *, days: int = 1,
             supplier: str | None = None):
        return cost.preview(self.ac, self.price, self.invoice,
                            now=now or self.now, days=days,
                            model_supplier_markup_ore=supplier)

    def test_customer_invoice_assumption_uses_july_not_generic_public_grid(self):
        self.put()
        self.prices()
        result = self.calc(supplier="9.29")
        self.assertEqual(result["quality"],
            "estimated_all_grid_historical_invoice_assumption_partial_coverage")
        self.assertEqual(result["estimated_ac_kwh_covered_only"], 0.0115)
        self.assertEqual(result["electricity_model_ore_for_covered_energy"],2.3)
        self.assertEqual(result["grid_tax_and_transfer_assumed_ore_for_covered_energy"],0.8209)
        self.assertEqual(result["estimated_all_grid_marginal_cost_ore_for_covered_energy"],3.1209)
        self.assertEqual(result["estimated_all_grid_marginal_cost_kr_for_covered_energy"],0.0312)
        self.assertEqual(result["fragment_count"],1)
        self.assertEqual(result["covered_intervals"],1)
        self.assertEqual(result["source_price_quality"],price_capture.QUALITY)
        self.assertTrue(result["price_model_markup_is_already_included"])
        self.assertFalse(result["network_price_covered_by_invoice_for_current_day"])
        self.assertFalse(result["supplier_price_covered_by_invoice_for_current_day"])
        self.assertFalse(result["actual_grid_vs_solar_source_verified"])
        self.assertFalse(result["fixed_supplier_and_grid_fees_included"])
        self.assertFalse(result["volvo_dc_efficiency_known"])
        self.assertIsNone(result["actual_cost_ore"])
        self.assertIsNone(result["actual_cost_per_mil_kr"])
        self.assertIsNone(result["total_charge_session_cost_ore"])
        self.assertIsNone(result["verified_vehicle_net_dc_kwh"])
        self.assertEqual(result["device_writes"],0)
        fragment=result["observed_charging_fragments"][0]
        self.assertEqual(fragment["grid_rate_ore_per_kwh_incl_vat"],71.38)
        self.assertEqual(fragment["grid_rate_source_invoice_month"],"2026-07")
        self.assertEqual(fragment["grid_provenance"],
                         "historical_invoice_rate_assumed_after_period")
        self.assertEqual(result["historic_supplier_markup_model_comparison"][
            "difference_from_most_recent_invoice"],"0.01")

    def test_cost_includes_no_double_supplier_surcharge(self):
        self.put()
        self.prices()
        a = self.calc(supplier="9.29")
        b = self.calc(supplier="6.00")
        self.assertEqual(a["estimated_all_grid_marginal_cost_ore_for_covered_energy"],
                         b["estimated_all_grid_marginal_cost_ore_for_covered_energy"])
        self.assertEqual(a["electricity_model_ore_for_covered_energy"],
                         b["electricity_model_ore_for_covered_energy"])

    def test_no_observations_does_not_estimate_zero_energy_or_cost(self):
        out = self.calc()
        self.assertEqual(out["quality"],"unavailable")
        self.assertFalse(out["fragments_all_priced"])
        self.assertIsNone(out["estimated_all_grid_marginal_cost_ore_for_covered_energy"])
        self.assertIsNone(out["estimated_ac_kwh_covered_only"])
        self.assertFalse(Path(self.ac).exists())
        self.assertFalse(Path(self.price).exists())

    def test_missing_price_keeps_energy_but_not_total_cost(self):
        self.put()
        out=self.calc()
        self.assertEqual(out["estimated_ac_kwh_covered_only"],0.0115)
        self.assertEqual(out["quality"],"unavailable")
        self.assertIsNone(out["electricity_model_ore_for_covered_energy"])
        self.assertIsNone(out["estimated_all_grid_marginal_cost_ore_for_covered_energy"])
        self.assertIsNone(out["observed_charging_fragments"][0][
            "all_grid_marginal_cost_scenario_ore"])

    def test_gaps_not_integrated_and_fragments_price_separately(self):
        self.put(offsets=(0,30,95,125))
        self.prices()
        out=self.calc()
        self.assertEqual(out["covered_seconds"],60)
        self.assertEqual(out["fragment_count"],2)
        self.assertEqual(out["estimated_ac_kwh_covered_only"],0.023)
        self.assertTrue(out["fragments_all_priced"])
        # Fragment 1 straddles 19:15, fragment 2 is fully within
        # the higher-priced next quarter; both still exclude the gap.
        self.assertEqual(out["estimated_all_grid_marginal_cost_ore_for_covered_energy"],7.3917)
        self.assertEqual(out["uncovered_charging_energy_during_gaps"],"unknown")
        self.assertIsNone(out["total_charge_session_cost_ore"])

    def test_one_unpriced_fragment_forces_null_grand_total(self):
        first = START
        second = START+timedelta(minutes=19)
        self.put(start=first,offsets=(0,30))
        self.put(start=second,offsets=(0,30))
        self.prices()
        out=self.calc()
        self.assertEqual(out["fragment_count"],2)
        self.assertFalse(out["fragments_all_priced"])
        self.assertIsNone(out["estimated_all_grid_marginal_cost_ore_for_covered_energy"])
        self.assertIsNone(out["electricity_model_ore_for_covered_energy"])
        self.assertIsNotNone(out["observed_charging_fragments"][0][
            "all_grid_marginal_cost_scenario_ore"])
        self.assertIsNone(out["observed_charging_fragments"][1][
            "all_grid_marginal_cost_scenario_ore"])

    def test_price_can_be_negative_and_source_cost_is_not_fake_zero(self):
        self.put()
        self.prices(first=-120, second=-80)
        out=self.calc()
        self.assertEqual(out["electricity_model_ore_for_covered_energy"],-1.15)
        self.assertEqual(out["estimated_all_grid_marginal_cost_ore_for_covered_energy"],-0.3291)
        self.assertIsNone(out["actual_cost_ore"])

    def test_grid_reference_expires_after_120_days(self):
        start=datetime(2026,12,20,19,14,45,tzinfo=UTC)
        self.put(start=start)
        self.prices(start=start.replace(minute=0,second=0))
        out=self.calc(now=start+timedelta(hours=1))
        self.assertEqual(out["quality"],"unavailable")
        self.assertEqual(out["observed_charging_fragments"][0]["grid_provenance"],
                         "grid_invoice_reference_too_old")
        self.assertIsNone(out["estimated_all_grid_marginal_cost_ore_for_covered_energy"])

    def test_invoice_from_previous_calendar_year_cannot_be_assumed(self):
        start=datetime(2027,1,2,19,14,45,tzinfo=UTC)
        self.put(start=start)
        self.prices(start=start.replace(minute=0,second=0))
        out=self.calc(now=start+timedelta(hours=1))
        self.assertEqual(out["quality"],"unavailable")
        self.assertEqual(out["observed_charging_fragments"][0]["grid_provenance"],
                         "grid_invoice_calendar_year_mismatch")

    def test_no_future_july_invoice_use_before_issue_date(self):
        start=datetime(2026,7,15,19,14,45,tzinfo=UTC)
        self.put(start=start)
        self.prices(start=start.replace(minute=0,second=0))
        out=self.calc(now=start+timedelta(hours=1),days=1)
        self.assertEqual(out["quality"],"unavailable")
        self.assertEqual(out["observed_charging_fragments"][0]["grid_provenance"],
                         "no_prior_grid_invoice")

    def test_july_invoiced_period_later_viewed_is_not_marked_as_october(self):
        start=datetime(2026,7,20,19,14,45,tzinfo=UTC)
        self.put(start=start)
        self.prices(start=start.replace(minute=0,second=0))
        out=self.calc(now=datetime(2026,8,12,20,0,tzinfo=UTC),days=30)
        self.assertEqual(out["quality"],
                         "estimated_all_grid_invoice_month_partial_coverage")
        self.assertEqual(out["observed_charging_fragments"][0]["grid_provenance"],
                         "invoiced_for_fragment_period_only")
        self.assertFalse(out["network_price_covered_by_invoice_for_current_day"])

    def test_explicit_bad_invoice_provenance_fails_closed(self):
        raw=json.loads(Path(self.invoice).read_text())
        raw["records"][1]["transfer_ore_incl_vat"]="NaN"
        Path(self.invoice).write_text(json.dumps(raw),encoding="utf-8")
        self.put()
        self.prices()
        with self.assertRaises(invoice.InvoiceEvidenceError):
            self.calc()

    def test_tampered_upstream_db_fails_closed_without_invented_cost(self):
        self.put()
        self.prices()
        with sqlite3.connect(self.ac) as db:
            db.execute("UPDATE ev_ac_estimated_intervals SET estimated_ac_kwh='999'")
        with self.assertRaisesRegex(sessions.SessionEvidenceError,"does not match"):
            self.calc()

    def test_truncated_fragments_are_not_silently_used_for_covered_total(self):
        with patch.object(cost.sessions,"analyze",return_value={
                "fragments_truncated": True,"observed_charging_fragments":[]}):
            with self.assertRaisesRegex(cost.CostPreviewError,"truncated"):
                self.calc()

    def test_computed_totals_must_match_underlying_fragments(self):
        start=START
        self.put()
        with patch.object(cost.sessions,"analyze",return_value={
                "observed_charging_fragments":[{
                    "observed_start_utc":start.isoformat(),
                    "observed_end_utc":(start+timedelta(seconds=30)).isoformat(),
                    "covered_seconds":30,"estimated_ac_kwh_covered_only":0.02,
                    "hypothetical_grid_only_spot_model_ore":1,
                    "full_session_energy_known":False,
                }],
                "estimated_ac_kwh_covered_only":0.999,
                "source_price_quality":price_capture.QUALITY,
            }):
            with self.assertRaisesRegex(cost.CostPreviewError,"do not reconcile"):
                self.calc()

    def test_extraneous_personal_document_metadata_never_returned(self):
        self.put()
        self.prices()
        out=json.dumps(self.calc(),ensure_ascii=False)
        for field in ("kundnummer","ocr","anläggnings","Lördagsvägen",
                      "account_number","meter_number","payment_identifier"):
            self.assertNotIn(field.lower(),out.lower())

    def test_readonly_sources_and_contract_file_are_unchanged(self):
        self.put()
        self.prices()
        before = {name:Path(name).read_bytes()
                  for name in (self.ac,self.price,self.invoice)}
        self.calc()
        self.assertEqual(before,{name:Path(name).read_bytes()
                                 for name in (self.ac,self.price,self.invoice)})
        text=Path(cost.__file__).read_text().lower()
        for forbidden in ("import inverter_control", "import evcc_observer",
                          "requests.post","write_register","fc10",
                          "create table","sqlite3.connect("):
            self.assertNotIn(forbidden,text)

    def test_too_wide_window_or_naive_now_is_rejected(self):
        with self.assertRaises(ValueError):
            self.calc(days=31)
        with self.assertRaises(ValueError):
            self.calc(now=datetime(2026,10,28,20,0))


if __name__=="__main__":
    unittest.main()
