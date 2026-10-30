"""Customer invoice unit-rate provenance must not be mistaken for current billing."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import ev_invoice_tariff as tariffs

EVIDENCE = Path(__file__).resolve().parents[2] / "docs" / "ev-invoice-rate-evidence-2026.json"


class InvoiceTariffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "source.json"
        self.data = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        self._write()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self):
        self.path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")

    def _load(self):
        self._write()
        return tariffs.load_evidence(self.path)

    def _report(self, date_str: str):
        return tariffs.report(
            self._load(), on=date.fromisoformat(date_str),
            model_supplier_markup="9.29", model_grid_variable="71.38",
            model_export_credit="1.50")

    def test_real_invoice_rate_calculations_match_source_periods(self):
        records = self._load()
        self.assertEqual(len(records), 2)
        supplier = next(x for x in records if x["role"] == "supplier")
        grid = next(x for x in records if x["role"] == "grid")
        s = supplier["rate_components"]
        g = grid["rate_components"]
        self.assertEqual(s["supplier_fixed_markup_ore_per_kwh"], "6.00")
        self.assertEqual(s["supplier_variable_purchase_ore_per_kwh"], "3.28")
        self.assertEqual(s["supplier_markup_before_vat_ore_per_kwh"], "9.28")
        self.assertEqual(s["supplier_fee_kr_incl_vat"], "49.0000")
        self.assertEqual(s["historic_consumption_weighted_spot_ore_per_kwh"], "36.61")
        self.assertEqual(g["grid_transfer_ore_per_kwh_incl_vat"], "26.38")
        self.assertEqual(g["electricity_tax_ore_per_kwh_incl_vat"], "45.00")
        self.assertEqual(g["grid_variable_import_ore_per_kwh_incl_vat"], "71.38")
        self.assertEqual(g["grid_export_credit_ore_per_kwh_incl_vat"], "1.50")
        self.assertEqual(g["fixed_grid_fee_kr_per_year_incl_vat"], "8052.50")

    def test_synthetic_consumption_uses_historical_unit_rates(self):
        kwh = Decimal("100.00")
        values = {
            "spot": (kwh * Decimal("36.61") / 100).quantize(Decimal("0.01")),
            "fixed_surcharge": (kwh * Decimal("6.00") / 100).quantize(Decimal("0.01")),
            "variable_purchase": (kwh * Decimal("3.28") / 100).quantize(Decimal("0.01")),
        }
        # Synthetic public sample: never expose a customer's billed kWh here.
        self.assertEqual(values["spot"], Decimal("36.61"))
        self.assertEqual(values["fixed_surcharge"], Decimal("6.00"))
        self.assertEqual(values["variable_purchase"], Decimal("3.28"))

    def test_october_is_not_invoice_reconciled_despite_close_model_rates(self):
        r = self._report("2026-10-28")
        self.assertEqual(r["rate_quality"], "historic_invoice_references_only")
        self.assertFalse(r["current_combined_invoice_rate_verified"])
        for k in ("supplier", "grid"):
            self.assertFalse(r["invoice_periods"][k][
                "invoice_rate_verified_for_requested_date"])
            self.assertIsNone(r["invoice_periods"][k]["rate_components"])
            self.assertFalse(r[
                "most_recent_invoices_not_automatically_carried_forward"
                ][k]["usable_for_requested_date"])
        self.assertEqual(r["model_comparisons"][
            "supplier_markup_ore_per_kwh_ex_vat"]["difference_from_most_recent_invoice"],"0.01")
        self.assertEqual(r["model_comparisons"][
            "grid_variable_ore_per_kwh_incl_vat"]["difference_from_most_recent_invoice"],"0.00")
        self.assertEqual(r["model_comparisons"][
            "grid_export_credit_ore_per_kwh_incl_vat"]["difference_from_most_recent_invoice"],"0.00")
        self.assertIsNone(r["actual_charging_cost_ore"])
        self.assertIsNone(r["cost_per_mil_kr"])
        self.assertEqual(r["device_writes"],0)

    def test_june_tibber_verified_only_in_june_not_grid_july(self):
        r = self._report("2026-06-20")
        self.assertTrue(r["invoice_periods"]["supplier"][
            "invoice_rate_verified_for_requested_date"])
        self.assertFalse(r["invoice_periods"]["grid"][
            "invoice_rate_verified_for_requested_date"])
        self.assertFalse(r["current_combined_invoice_rate_verified"])

    def test_july_grid_verified_only_in_july_not_tibber_june(self):
        r = self._report("2026-07-15")
        self.assertFalse(r["invoice_periods"]["supplier"][
            "invoice_rate_verified_for_requested_date"])
        self.assertTrue(r["invoice_periods"]["grid"][
            "invoice_rate_verified_for_requested_date"])
        self.assertFalse(r["current_combined_invoice_rate_verified"])

    def test_price_not_silently_carried_across_year(self):
        r = self._report("2027-01-02")
        self.assertFalse(r["current_combined_invoice_rate_verified"])
        self.assertTrue(r["model_comparisons"][
            "supplier_markup_ore_per_kwh_ex_vat"]["is_only_a_historic_comparison"])
        self.assertTrue(r["model_comparisons"][
            "grid_variable_ore_per_kwh_incl_vat"]["is_only_a_historic_comparison"])

    def test_invoice_not_future_used_as_reference(self):
        r = self._report("2026-05-22")
        self.assertIsNone(r["most_recent_invoices_not_automatically_carried_forward"]["supplier"])
        self.assertIsNone(r["most_recent_invoices_not_automatically_carried_forward"]["grid"])

    def test_deny_extra_private_identifiers_and_unknown_fields(self):
        self.data["records"][0]["account_number"] = "must_not_store"
        with self.assertRaisesRegex(tariffs.InvoiceEvidenceError, "fields"):
            self._load()

    def test_denies_negative_nan_and_overflow(self):
        for v in ("-1", "NaN", "Infinity", "10000000", "", 3.28):
            with self.subTest(value=v):
                self.data = json.loads(EVIDENCE.read_text(encoding="utf-8"))
                self.data["records"][0]["variable_purchase_ore_ex_vat"] = v
                with self.assertRaises(tariffs.InvoiceEvidenceError):
                    self._load()

    def test_rejects_source_filename_switch_or_forged_role(self):
        self.data["records"][0]["source_filename"] = "contract.pdf"
        with self.assertRaisesRegex(tariffs.InvoiceEvidenceError, "filename"):
            self._load()
        self.data = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        self.data["records"][0]["role"] = "grid"
        with self.assertRaisesRegex(tariffs.InvoiceEvidenceError, "fields"):
            self._load()

    def test_rejects_invalid_month_boundaries(self):
        for k,v in (
            ("period_end_local_exclusive","2026-06-30"),
            ("period_start_local","2026-06-04"),
            ("invoice_issued_on","2026-06-12"),
        ):
            with self.subTest(key=k):
                self.data=json.loads(EVIDENCE.read_text(encoding="utf-8"))
                self.data["records"][0][k]=v
                with self.assertRaises(tariffs.InvoiceEvidenceError):
                    self._load()

    def test_rejects_duplicate_records(self):
        self.data["records"].append(copy.deepcopy(self.data["records"][0]))
        with self.assertRaisesRegex(tariffs.InvoiceEvidenceError,"duplicate"):
            self._load()

    def test_rejects_overlap_same_role_even_if_new_id(self):
        r=copy.deepcopy(self.data["records"][0])
        r["id"]="tibber_duplicate_june"
        self.data["records"].append(r)
        with self.assertRaisesRegex(tariffs.InvoiceEvidenceError,"overlapping"):
            self._load()

    def test_rejects_invalid_area_and_schema(self):
        self.data["records"][1]["area"]="SE3"
        with self.assertRaisesRegex(tariffs.InvoiceEvidenceError,"area"):
            self._load()
        self.data=json.loads(EVIDENCE.read_text(encoding="utf-8"))
        self.data["schema_version"]=2
        with self.assertRaisesRegex(tariffs.InvoiceEvidenceError,"schema"):
            self._load()

    def test_missing_file_not_created(self):
        path=self.path.with_name("missing.json")
        self.assertFalse(path.exists())
        with self.assertRaises(tariffs.InvoiceEvidenceError):
            tariffs.load_evidence(path)
        self.assertFalse(path.exists())

    def test_current_model_reference_must_be_explicit(self):
        r=tariffs.report(self._load(), on=date(2026,10,28))
        self.assertEqual(r["model_comparisons"],{})
        self.assertIsNone(r["actual_charging_cost_ore"])

    def test_source_and_target_unchanged_by_readonly_report(self):
        before=self.path.read_bytes()
        r=self._report("2026-10-28")
        self.assertEqual(before,self.path.read_bytes())
        code=Path(tariffs.__file__).read_text().lower()
        for forbidden in ("requests.post","socket.create_connection",
                          "write_register","import inverter_control",
                          "import evcc_observer","create table",
                          "def apply_tariff", "fc10"):
            self.assertNotIn(forbidden,code)
        self.assertEqual(r["device_writes"],0)


if __name__=="__main__":
    unittest.main()
