#!/usr/bin/env python3
"""Validate anonymized, period-scoped Tibber and Sundsvall Elnat invoice rates.

No invoices, customer details, meter IDs, API credentials or PDF contents are
bundled. Only independently visible, date-scoped rate line items are stored.
The evidence is for the INVOICED MONTH, not for a later calendar month.
This read-only CLI never estimates charge energy, money actually paid, or kr/mil;
it never accesses hardware or changes tariffs or the live optimizer.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

VERSION = 1
MAX_FILE_BYTES = 32 * 1024
STOCKHOLM = ZoneInfo("Europe/Stockholm")
SUPPLIER_FIELDS = frozenset({
    "id", "role", "source_filename", "source_type", "invoice_issued_on",
    "period_start_local", "period_end_local_exclusive", "area",
    "import_kwh", "spot_weighted_average_ore_ex_vat",
    "fixed_surcharge_ore_ex_vat", "variable_purchase_ore_ex_vat",
    "monthly_fee_sek_ex_vat", "vat_rate", "note",
})
GRID_FIELDS = frozenset({
    "id", "role", "source_filename", "source_type", "invoice_issued_on",
    "period_start_local", "period_end_local_exclusive", "area",
    "import_kwh", "transfer_ore_incl_vat", "electricity_tax_ore_incl_vat",
    "export_grid_credit_ore_incl_vat",
    "annual_fixed_grid_fee_sek_incl_vat", "note",
})


class InvoiceEvidenceError(ValueError):
    """Fail-closed diagnostic without exposing invoice identifiers."""


def _date(s: object) -> date:
    if not isinstance(s, str):
        raise InvoiceEvidenceError("missing invoice calendar date")
    try:
        parsed = date.fromisoformat(s)
    except ValueError as exc:
        raise InvoiceEvidenceError("invalid invoice calendar date") from exc
    if parsed.isoformat() != s:
        raise InvoiceEvidenceError("non-canonical invoice calendar date")
    return parsed


def _decimal(v: object, *, label: str, maximum: str = "100000") -> Decimal:
    if not isinstance(v, str) or not v or len(v) > 24:
        raise InvoiceEvidenceError(f"{label}: expected decimal string")
    try:
        n = Decimal(v)
    except InvalidOperation as exc:
        raise InvoiceEvidenceError(f"{label}: not decimal") from exc
    if not n.is_finite() or not Decimal("0") <= n <= Decimal(maximum):
        raise InvoiceEvidenceError(f"{label}: outside valid nonnegative range")
    return n


def _validate_record(r: object) -> dict:
    if not isinstance(r, dict):
        raise InvoiceEvidenceError("invoice record must be object")
    role = r.get("role")
    fields = SUPPLIER_FIELDS if role == "supplier" else (
        GRID_FIELDS if role == "grid" else None)
    if fields is None or set(r) != fields:
        raise InvoiceEvidenceError("invoice record role or fields invalid")
    if (not isinstance(r["id"], str)
            or not 5 <= len(r["id"]) <= 80
            or not all(c.isalnum() or c == "_" for c in r["id"])):
        raise InvoiceEvidenceError("invoice record id is invalid")
    if r["source_type"] != "user_uploaded_invoice":
        raise InvoiceEvidenceError("source is not a user-uploaded invoice")
    filename = r["source_filename"]
    if (not isinstance(filename, str) or not 5 <= len(filename) <= 120
            or "/" in filename or "\\" in filename or ".." in filename
            or not filename.lower().endswith(".pdf")
            or (("tibber" if role == "supplier" else "elnät")
                not in filename.lower())):
        raise InvoiceEvidenceError("invoice filename/source mismatch")
    if r["area"] != "SE2":
        raise InvoiceEvidenceError("invoice area mismatch")
    if not isinstance(r["note"], str) or not 3 <= len(r["note"]) <= 400:
        raise InvoiceEvidenceError("invoice provenance note invalid")

    issued = _date(r["invoice_issued_on"])
    start = _date(r["period_start_local"])
    end = _date(r["period_end_local_exclusive"])
    if not start < end <= issued:
        raise InvoiceEvidenceError("invoice dates out of order")
    if not 27 <= (end - start).days <= 32:
        raise InvoiceEvidenceError("expected a full monthly invoice period")
    if start.day != 1 or end.day != 1:
        raise InvoiceEvidenceError("invoice period not calendar-month aligned")
    consumption = _decimal(r["import_kwh"], label="import_kwh")
    if consumption <= 0:
        raise InvoiceEvidenceError("zero or negative historical invoice kWh")

    if role == "supplier":
        fixed = _decimal(r["fixed_surcharge_ore_ex_vat"], label="supplier_fixed", maximum="1000")
        variable = _decimal(r["variable_purchase_ore_ex_vat"], label="supplier_variable", maximum="1000")
        _decimal(r["spot_weighted_average_ore_ex_vat"],
                 label="historic_weighted_spot", maximum="3000")
        _decimal(r["monthly_fee_sek_ex_vat"], label="supplier_fixed_fee")
        vat = _decimal(r["vat_rate"], label="vat_rate", maximum="1")
        if vat != Decimal("0.25"):
            raise InvoiceEvidenceError("unexpected VAT rate in June invoice")
        components = {
            "supplier_markup_before_vat_ore_per_kwh": str(fixed + variable),
            "supplier_fixed_markup_ore_per_kwh": str(fixed),
            "supplier_variable_purchase_ore_per_kwh": str(variable),
            "supplier_fee_kr_incl_vat": str(
                _decimal(r["monthly_fee_sek_ex_vat"], label="monthly_fee") * (1 + vat)),
            "historic_consumption_weighted_spot_ore_per_kwh": str(
                _decimal(r["spot_weighted_average_ore_ex_vat"], label="weighted_spot")),
        }
    else:
        transfer = _decimal(r["transfer_ore_incl_vat"], label="transfer", maximum="1000")
        tax = _decimal(r["electricity_tax_ore_incl_vat"], label="tax", maximum="1000")
        credit = _decimal(r["export_grid_credit_ore_incl_vat"],
                          label="export_credit", maximum="1000")
        fixed_annual = _decimal(r["annual_fixed_grid_fee_sek_incl_vat"],
                                label="fixed_annual", maximum="50000")
        components = {
            "grid_variable_import_ore_per_kwh_incl_vat": str(transfer + tax),
            "grid_transfer_ore_per_kwh_incl_vat": str(transfer),
            "electricity_tax_ore_per_kwh_incl_vat": str(tax),
            "grid_export_credit_ore_per_kwh_incl_vat": str(credit),
            "fixed_grid_fee_kr_per_year_incl_vat": str(fixed_annual),
        }
    return {
        "id": r["id"],
        "role": role,
        "source_file_label": r["source_filename"],
        "source_type": r["source_type"],
        "invoice_issued_on": issued.isoformat(),
        "period_start_local": start.isoformat(),
        "period_end_local_exclusive": end.isoformat(),
        "source_quality": "invoice_unit_rate_for_period_only",
        "rate_components": components,
    }


def load_evidence(path: str | Path) -> list[dict]:
    src = Path(path)
    try:
        if src.stat().st_size > MAX_FILE_BYTES:
            raise InvoiceEvidenceError("invoice evidence file too large")
        raw = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InvoiceEvidenceError("invoice evidence not readable/valid") from exc
    if (not isinstance(raw, dict) or set(raw) != {
            "schema_version", "currency", "rate_unit", "records"}
            or type(raw["schema_version"]) is not int
            or raw["schema_version"] != VERSION
            or raw["currency"] != "SEK" or raw["rate_unit"] != "ore_per_kwh"
            or not isinstance(raw["records"], list)
            or not 1 <= len(raw["records"]) <= 100):
        raise InvoiceEvidenceError("invoice evidence schema invalid")
    validated = [_validate_record(r) for r in raw["records"]]
    seen_ids = set()
    for item in validated:
        if item["id"] in seen_ids:
            raise InvoiceEvidenceError("duplicate invoice record")
        seen_ids.add(item["id"])
    for role in ("supplier", "grid"):
        by_role = sorted((r for r in validated if r["role"] == role),
                         key=lambda r: r["period_start_local"])
        for prev, next_item in zip(by_role, by_role[1:]):
            if prev["period_end_local_exclusive"] > next_item["period_start_local"]:
                raise InvoiceEvidenceError("overlapping invoice months for role")
    return validated


def report(records: list[dict], *, on: date,
           model_supplier_markup: str | None = None,
           model_grid_variable: str | None = None,
           model_export_credit: str | None = None) -> dict:
    comparisons = {}
    observed_latest = {}
    at_date = {}
    for role in ("supplier", "grid"):
        rows = sorted((r for r in records if r["role"] == role),
                      key=lambda r: r["period_start_local"])
        matches = [r for r in rows
                   if r["period_start_local"] <= on.isoformat()
                   < r["period_end_local_exclusive"]]
        applicable = matches[0] if matches else None
        at_date[role] = {
            "invoice_rate_verified_for_requested_date": applicable is not None,
            "source_period": (
                f"{applicable['period_start_local']}/{applicable['period_end_local_exclusive']}"
                if applicable else None),
            "rate_components": applicable["rate_components"] if applicable else None,
        }
        previous = [r for r in rows if r["period_start_local"] <= on.isoformat()]
        latest = previous[-1] if previous else None
        observed_latest[role] = ({
            "invoice_period": f"{latest['period_start_local']}/{latest['period_end_local_exclusive']}",
            "source_file_label": latest["source_file_label"],
            "source_quality": latest["source_quality"],
            "rate_components": latest["rate_components"],
            "usable_for_requested_date": bool(applicable),
        } if latest else None)
    specs = {
        "supplier_markup_ore_per_kwh_ex_vat": (
            model_supplier_markup, "supplier",
            "supplier_markup_before_vat_ore_per_kwh"),
        "grid_variable_ore_per_kwh_incl_vat": (
            model_grid_variable, "grid",
            "grid_variable_import_ore_per_kwh_incl_vat"),
        "grid_export_credit_ore_per_kwh_incl_vat": (
            model_export_credit, "grid",
            "grid_export_credit_ore_per_kwh_incl_vat"),
    }
    for name, (current, role, component) in specs.items():
        if current is None:
            continue
        model = _decimal(current, label=name, maximum="1000")
        latest = observed_latest[role]
        reference = (Decimal(latest["rate_components"][component])
                     if latest is not None else None)
        comparisons[name] = {
            "configured_value": str(model),
            "most_recent_invoice_value": str(reference) if reference is not None else None,
            "difference_from_most_recent_invoice": str(model - reference)
                if reference is not None else None,
            "invoice_period_verifies_this_config_on_requested_date": (
                bool(latest and latest["usable_for_requested_date"]
                     and model == reference)),
            "is_only_a_historic_comparison": not (
                latest and latest["usable_for_requested_date"]),
        }
    complete = all(at_date[r]["invoice_rate_verified_for_requested_date"]
                   for r in ("supplier", "grid"))
    return {
        "source": "anonymized_user_uploaded_invoice_rate_evidence",
        "as_of_local_date": on.isoformat(),
        "rate_quality": ("both_supplier_and_grid_invoices_cover_requested_date"
                         if complete else "historic_invoice_references_only"),
        "invoice_periods": at_date,
        "most_recent_invoices_not_automatically_carried_forward": observed_latest,
        "model_comparisons": comparisons,
        "current_combined_invoice_rate_verified": complete,
        "invoiceable_charging_energy_kwh": None,
        "actual_charging_cost_ore": None,
        "cost_per_mil_kr": None,
        "device_writes": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence", default="/opt/solinteg/ev-shadow-runtime/docs/ev-invoice-rate-evidence-2026.json")
    parser.add_argument(
        "--on", help="Stockholm local calendar date (default: today)")
    parser.add_argument("--model-supplier-markup", type=str)
    parser.add_argument("--model-grid-variable", type=str)
    parser.add_argument("--model-export-credit", type=str)
    args = parser.parse_args()
    try:
        as_of = (_date(args.on) if args.on
                 else datetime.now(STOCKHOLM).date())
        print(json.dumps(report(
            load_evidence(args.evidence), on=as_of,
            model_supplier_markup=args.model_supplier_markup,
            model_grid_variable=args.model_grid_variable,
            model_export_credit=args.model_export_credit,
        ), ensure_ascii=False))
    except InvoiceEvidenceError as exc:
        print(json.dumps({"rate_quality": "unavailable", "reason": str(exc),
                          "actual_charging_cost_ore": None, "device_writes": 0}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
