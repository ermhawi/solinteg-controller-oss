#!/usr/bin/env python3
"""Read-only hypothetical XC40 charging cost for VERIFIED EVCC coverage only.

Source: gap-aware Ict2 × assumed 230 V × PF=1 AC energy and historical
quarter-hour Solinteg *buy price model*, plus the most recent applicable
customer GRID invoice rate as an EXPLICIT carry-forward assumption.

No electricity source allocation, invoices for the charging day, AC/DC
efficiency, full-session charge totals, or actual kr/mil are inferred.
Neither input evidence DB nor any physical device is written or contacted.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

import ev_ac_sessions as sessions
import ev_invoice_tariff as invoices

UTC = timezone.utc
STOCKHOLM = ZoneInfo("Europe/Stockholm")
MAX_HISTORIC_GRID_AGE_DAYS = 120
ROUND_ORE = Decimal("0.0001")
ROUND_KWH = Decimal("0.000001")


class CostPreviewError(ValueError):
    """No private details in user-facing errors."""


def decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CostPreviewError("invalid upstream numeric evidence") from exc
    if not number.is_finite():
        raise CostPreviewError("nonfinite upstream numeric evidence")
    return number


def amount_or_none(value: Decimal | None, quantum: Decimal = ROUND_ORE) -> float | None:
    return float(value.quantize(quantum, rounding=ROUND_HALF_UP)) if value is not None else None


def _instant(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise CostPreviewError("invalid upstream UTC instant") from exc
    if result.tzinfo is None:
        raise CostPreviewError("unqualified upstream instant")
    return result.astimezone(UTC)


def _grid_reference(
    grid_records: list[dict], *, start: datetime, end: datetime, as_of: date
) -> tuple[dict | None, str, int | None]:
    """Match a whole fragment to a historically invoiced grid rate; no future/year leaps."""
    if end <= start:
        raise CostPreviewError("invalid energy fragment time range")
    local_start = start.astimezone(STOCKHOLM)
    local_last = (end - timedelta(microseconds=1)).astimezone(STOCKHOLM)
    # A single monthly invoice rate may cover the entire recorded fragment;
    # never automatically transfer an old rate into a future calendar year.
    latest = None
    for record in grid_records:
        period_start = date.fromisoformat(record["period_start_local"])
        issued = date.fromisoformat(record["invoice_issued_on"])
        if period_start <= local_start.date() and issued <= as_of:
            if latest is None or record["period_start_local"] > latest["period_start_local"]:
                latest = record
    if latest is None:
        return None, "no_prior_grid_invoice", None
    start_day = date.fromisoformat(latest["period_start_local"])
    period_end = date.fromisoformat(latest["period_end_local_exclusive"])
    if local_start.date().year != start_day.year or local_last.date().year != start_day.year:
        return None, "grid_invoice_calendar_year_mismatch", None
    if local_start.date() < start_day:
        return None, "grid_invoice_predates_covered_energy", None
    if local_last.date() < period_end and local_start.date() < period_end:
        return latest, "invoiced_for_fragment_period_only", 0
    if local_start.date() < period_end <= local_last.date():
        return None, "fragment_crosses_grid_invoice_period_boundary", None
    age_days = (local_last.date() - period_end).days
    if not 0 <= age_days <= MAX_HISTORIC_GRID_AGE_DAYS:
        return None, "grid_invoice_reference_too_old", age_days
    return latest, "historical_invoice_rate_assumed_after_period", age_days


def preview(
    ac_db: str, price_db: str, invoice_path: str, *,
    now: datetime | None = None, days: int = 1,
    model_supplier_markup_ore: str | None = None,
) -> dict:
    if not 1 <= days <= 30:
        raise ValueError("days must be 1..30")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    current_day = now.astimezone(STOCKHOLM).date()
    records = invoices.load_evidence(invoice_path)
    invoice_report = invoices.report(
        records, on=current_day, model_supplier_markup=model_supplier_markup_ore)
    grid_records = [r for r in records if r["role"] == "grid"]
    # The upstream session analyzer already verifies raw measurements against
    # persisted AC interval integrals, aligns quarter-price boundaries, and
    # refuses to assume charging over gaps.
    session = sessions.analyze(ac_db, price_db, now=now, days=days)
    if session.get("fragments_truncated") is True:
        raise CostPreviewError("charging fragments truncated; cannot price complete covered set")
    fragments = session.get("observed_charging_fragments", [])
    if not isinstance(fragments, list):
        raise CostPreviewError("untrusted AC fragment collection")

    items: list[dict] = []
    all_priced = bool(fragments)
    energy_sum = Decimal("0")
    electricity_ore = Decimal("0")
    grid_ore = Decimal("0")
    any_assumed = False
    for index, fragment in enumerate(fragments):
        if fragment.get("full_session_energy_known") is not False:
            raise CostPreviewError("upstream fragment has unexpected full-session claim")
        begin = _instant(fragment["observed_start_utc"])
        end = _instant(fragment["observed_end_utc"])
        if end <= begin or end > now:
            raise CostPreviewError("invalid future/negative charging fragment")
        energy = decimal_or_none(fragment.get("estimated_ac_kwh_covered_only"))
        if energy is None or energy < 0:
            raise CostPreviewError("missing or negative covered AC evidence")
        source_ore = decimal_or_none(fragment.get("hypothetical_grid_only_spot_model_ore"))
        ref, provenance, age = _grid_reference(
            grid_records, start=begin, end=end, as_of=current_day)
        if ref:
            grid_rate = decimal_or_none(ref["rate_components"][
                "grid_variable_import_ore_per_kwh_incl_vat"])
            assert grid_rate is not None
        else:
            grid_rate = None
        scenario_complete = source_ore is not None and grid_rate is not None
        if not scenario_complete:
            all_priced = False
        grid_component = energy * grid_rate if scenario_complete else None
        total = source_ore + grid_component if grid_component is not None else None
        if scenario_complete:
            assert source_ore is not None and grid_component is not None
            electricity_ore += source_ore
            grid_ore += grid_component
            if provenance != "invoiced_for_fragment_period_only":
                any_assumed = True
        energy_sum += energy
        items.append({
            "fragment_index": index + 1,
            "observed_start_utc": begin.isoformat(),
            "observed_end_utc": end.isoformat(),
            "covered_seconds": fragment["covered_seconds"],
            "covered_ac_kwh_estimated": amount_or_none(energy, ROUND_KWH),
            "electricity_model_ore": amount_or_none(source_ore),
            "grid_tax_and_transfer_ore_assumed": amount_or_none(grid_component),
            "all_grid_marginal_cost_scenario_ore": amount_or_none(total),
            "all_grid_marginal_cost_scenario_kr": (
                amount_or_none(total / 100, ROUND_ORE) if total is not None else None),
            "quality": ("historical_invoice_assumption_partial_coverage"
                        if scenario_complete and provenance != "invoiced_for_fragment_period_only" else
                        "invoice_rate_for_fragment_month_partial_coverage"
                        if scenario_complete else "unavailable"),
            "grid_rate_source_invoice_month": (
                ref["period_start_local"][:7] if ref else None),
            "grid_rate_ore_per_kwh_incl_vat": (
                amount_or_none(grid_rate) if grid_rate is not None else None),
            "grid_invoice_age_days": age,
            "grid_provenance": provenance,
            "source_price_quality": session.get("source_price_quality"),
            "all_energy_from_grid_assumed_not_verified": True,
            "actual_cost_ore": None,
            "whole_session_kwh": None,
        })
    reported = decimal_or_none(session.get("estimated_ac_kwh_covered_only"))
    if fragments and (reported is None or abs(energy_sum - reported) > Decimal("0.00007")):
        raise CostPreviewError("AC fragment totals do not reconcile to upstream coverage")
    if not fragments and reported is not None:
        raise CostPreviewError("upstream AC total without fragments")
    invoice_supplier = invoice_report["model_comparisons"].get(
        "supplier_markup_ore_per_kwh_ex_vat")
    total_ore = electricity_ore + grid_ore if all_priced else None
    return {
        "source": "evcc_ict2_covered_ac_and_quarter_price_with_invoice_grid_assumption",
        "as_of_utc": now.isoformat(),
        "days": days,
        "quality": (
            "estimated_all_grid_historical_invoice_assumption_partial_coverage"
            if total_ore is not None and any_assumed else
            "estimated_all_grid_invoice_month_partial_coverage"
            if total_ore is not None else "unavailable"),
        "energy_model": "EVCC_Ict2_times_assumed_230V_times_PF1",
        "energy_completeness": "observed_valid_intervals_only",
        "actual_grid_vs_solar_source_verified": False,
        "network_price_covered_by_invoice_for_current_day": invoice_report[
            "invoice_periods"]["grid"]["invoice_rate_verified_for_requested_date"],
        "supplier_price_covered_by_invoice_for_current_day": invoice_report[
            "invoice_periods"]["supplier"]["invoice_rate_verified_for_requested_date"],
        "invoice_reconciliation_quality": invoice_report["rate_quality"],
        "historic_supplier_markup_model_comparison": invoice_supplier,
        "historical_grid_invoice_max_carry_forward_days": MAX_HISTORIC_GRID_AGE_DAYS,
        "grid_rate_source": "user_invoice_historic_only_not_live_contract",
        "price_model": "stored_15m_actual_source_day_ahead_buy_price_including_model_surcharge_and_vat_excluding_grid",
        "price_model_markup_is_already_included": True,
        "fixed_supplier_and_grid_fees_included": False,
        "volvo_dc_efficiency_known": False,
        "observed_charging_fragments": items,
        "fragment_count": session.get("fragment_count"),
        "fragments_all_priced": all_priced,
        "covered_intervals": session.get("covered_intervals"),
        "covered_seconds": session.get("covered_seconds"),
        "estimated_ac_kwh_covered_only": session.get("estimated_ac_kwh_covered_only"),
        "electricity_model_ore_for_covered_energy": (
            session.get("hypothetical_grid_only_spot_model_ore")),
        "grid_tax_and_transfer_assumed_ore_for_covered_energy": (
            amount_or_none(grid_ore) if all_priced else None),
        "estimated_all_grid_marginal_cost_ore_for_covered_energy": amount_or_none(total_ore),
        "estimated_all_grid_marginal_cost_kr_for_covered_energy": (
            amount_or_none(total_ore / 100, ROUND_ORE) if total_ore is not None else None),
        "charging_observed_recently": session.get("charging_observed_recently", False),
        "source_price_quality": session.get("source_price_quality"),
        "missing_energy_before_after_observations": "unknown",
        "uncovered_charging_energy_during_gaps": "unknown",
        "total_charge_session_cost_ore": None,
        "actual_cost_ore": None,
        "actual_cost_per_mil_kr": None,
        "verified_vehicle_net_dc_kwh": None,
        "device_writes": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ac-db", default="/opt/solinteg/ev-shadow-runtime/var/ev-ac-estimate.sqlite")
    parser.add_argument("--price-db", default="/opt/solinteg/ev-shadow-runtime/var/ev-cost-price-evidence.sqlite")
    parser.add_argument("--invoice-evidence", default="/opt/solinteg/ev-shadow-runtime/docs/ev-invoice-rate-evidence-2026.json")
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--model-supplier-markup", type=str, default=None)
    args = parser.parse_args()
    try:
        print(json.dumps(preview(
            args.ac_db, args.price_db, args.invoice_evidence, days=args.days,
            model_supplier_markup_ore=args.model_supplier_markup,
        ), ensure_ascii=False))
    except (CostPreviewError, invoices.InvoiceEvidenceError,
            sessions.SessionEvidenceError, OSError) as exc:
        print(json.dumps({"quality": "unavailable",
                          "reason": str(exc) if isinstance(
                              exc, (CostPreviewError, invoices.InvoiceEvidenceError,
                                   sessions.SessionEvidenceError))
                          else type(exc).__name__,
                          "actual_cost_ore": None,
                          "device_writes": 0}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
