#!/usr/bin/env python3
"""Publish only a sanitized, estimated XC40 electricity cost rate for HA charts.

Input is existing *read-only* EVCC AC coverage + price evidence + dated invoice
metadata via ev_ac_cost_preview. Output is one small local JSON file, never
raw journey, VIN, location, account, API credential or full source payload.

Assumed 90% charging efficiency at the observed low charging power.
The result is a comparison tariff for recent driving history, NOT the cost
of each individual past trip nor confirmed battery DC energy/invoice amount.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import ev_ac_cost_preview as cost

SCHEMA = "xc40_electricity_cost_rate_v1"
MIN_COVERED_AC_KWH = 0.5
MIN_COVERED_SECONDS = 1200
DEFAULT_CHARGING_EFFICIENCY = 0.90
MAX_AGE_DAYS = 30
PUBLIC_OUTPUT = Path("/opt/solinteg/ev-shadow-runtime/public/xc40-cost-rate.json")


class PublishError(ValueError):
    """Safe, source-independent publication failure."""


def make_public_rate(report: dict, *, efficiency: float = DEFAULT_CHARGING_EFFICIENCY) -> dict:
    if not 0.75 <= efficiency <= 1.0 or not math.isfinite(efficiency):
        raise PublishError("invalid documented charging efficiency assumption")
    if (report.get("quality") !=
            "estimated_all_grid_historical_invoice_assumption_partial_coverage"
            and report.get("quality") !=
            "estimated_all_grid_invoice_month_partial_coverage"):
        raise PublishError("AC / tariff evidence quality is insufficient")
    if report.get("fragments_all_priced") is not True:
        raise PublishError("at least one charging fragment lacks a price")
    if (report.get("actual_grid_vs_solar_source_verified") is not False
            or report.get("device_writes") != 0
            or report.get("actual_cost_ore") is not None):
        raise PublishError("unsafe or misclassified cost evidence")
    energy = report.get("estimated_ac_kwh_covered_only")
    cost_kr = report.get("estimated_all_grid_marginal_cost_kr_for_covered_energy")
    covered = report.get("covered_seconds")
    if (type(energy) not in (float, int) or type(cost_kr) not in (float, int)
            or type(covered) not in (float, int)
            or not all(map(math.isfinite, (energy, cost_kr, covered)))
            or energy < MIN_COVERED_AC_KWH or energy > 2000
            or covered < MIN_COVERED_SECONDS
            or covered > MAX_AGE_DAYS * 86400):
        raise PublishError("not enough valid charging coverage")
    if not report.get("observed_charging_fragments"):
        raise PublishError("no source charging fragments")
    fragments = report["observed_charging_fragments"]
    latest = max(str(f["observed_end_utc"]) for f in fragments)
    parsed = datetime.fromisoformat(latest.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise PublishError("source has no UTC time")
    latest_epoch = parsed.timestamp()
    report_time = datetime.fromisoformat(report["as_of_utc"]).timestamp()
    if not 0 <= report_time - latest_epoch <= MAX_AGE_DAYS * 86400:
        raise PublishError("source charging observations expired")
    per_ac = cost_kr / energy
    per_battery = per_ac / efficiency
    if not all(math.isfinite(v) and -3 <= v <= 15 for v in (per_ac, per_battery)):
        raise PublishError("implausible modelled electricity price")
    return {
        "schema": SCHEMA,
        "state": "estimated",
        "source": "DEFA_EVCC_Ict2_and_Solinteg_15m_model",
        "price_kr_per_ac_kwh": round(per_ac, 4),
        "price_kr_per_reported_vehicle_kwh": round(per_battery, 4),
        "assumed_charging_efficiency": efficiency,
        "estimated_covered_ac_kwh": round(energy, 3),
        "observed_charging_seconds": round(covered, 2),
        "source_last_observed_utc": parsed.astimezone(timezone.utc).isoformat(),
        "source_model_quality": report["quality"],
        "source_grid_tariff": "Sundsvall_Elnat_July_2026_as_undated_October_assumption",
        "source_energy_scope": "partial_observed_AC_charge_only",
        "applies_to_trips_as": "comparison_unit_rate_only_not_historical_charge_inventory",
        "vehicle_energy_basis": "reported_electrical_consumption_not_net_battery_dc",
        "actual_cost_kr_per_mil": None,
        "device_writes": 0,
    }


def publish(path: Path, payload: dict) -> bool:
    """Fail closed, atomic local write. Never modifies source databases."""
    if path.parent != PUBLIC_OUTPUT.parent and os.environ.get("EV_COST_RATE_TEST_OUTPUT") != "1":
        raise PublishError("publication path not in dedicated sanitized directory")
    if not path.parent.is_dir() or path.is_symlink():
        raise PublishError("sanitized publication directory unavailable/unsafe")
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    if len(text.encode("utf-8")) > 4096:
        raise PublishError("public rate exceeded bounded size")
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    fd, temp = tempfile.mkstemp(prefix=".xc40-cost-rate-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp, 0o644)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--ac-db",
        default="/opt/solinteg/ev-shadow-runtime/var/ev-ac-estimate.sqlite")
    parser.add_argument("--price-db",
        default="/opt/solinteg/ev-shadow-runtime/var/ev-cost-price-evidence.sqlite")
    parser.add_argument("--invoice-evidence",
        default="/opt/solinteg/ev-shadow-runtime/docs/ev-invoice-rate-evidence-2026.json")
    parser.add_argument("--output", type=Path, default=PUBLIC_OUTPUT)
    args = parser.parse_args()
    if os.environ.get("EV_COST_RATE_PUBLISH_ENABLED") != "1":
        raise SystemExit("EV cost rate publication disabled by default")
    report = cost.preview(args.ac_db, args.price_db, args.invoice_evidence, days=args.days)
    try:
        payload = make_public_rate(report)
        written = publish(args.output, payload)
    except PublishError as exc:
        # Preserve last valid rate (possibly stale); do NOT change to zero.
        print(json.dumps({"quality":"unavailable","reason":str(exc),"device_writes":0}))
        raise SystemExit(0)
    print(json.dumps({"state":"estimated", "written":written,
                      "price_kr_per_ac_kwh":payload["price_kr_per_ac_kwh"],
                      "source_last_observed_utc":payload["source_last_observed_utc"],
                      "device_writes":0}))


if __name__ == "__main__":
    main()
