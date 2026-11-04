#!/usr/bin/env python3
"""Empirical EVCC AC-estimate per Volvo SoC point; NEVER a control calibration.

Uses the existing immutable EVCC 6 A / assumed-230 V / PF=1 intervals and
Volvo cloud-reported SoC changes. A dedicated AC kWh meter and verified
physical BMS sample timestamps are NOT available. No device/network writes,
no session files and no calibration-grade claims.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import ev_ac_quality_audit as audit  # noqa: E402
import ev_ac_sessions as sessions  # noqa: E402

AC_DB = Path("/opt/solinteg/ev-shadow-runtime/var/ev-ac-estimate.sqlite")
HISTORY_DB = Path("/opt/solinteg/ev-shadow-runtime/var/ev-history.sqlite")
MIN_SOC_SPAN = 8
MIN_STEPS = 9
MAX_INTERVALS = 20_000


def _timestamp(value: str) -> int:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return int(stamp.timestamp() * 1000)


def _intervals(path: Path, *, first_ms: int, last_ms: int) -> list[tuple[int, int, float]]:
    uri = "file:" + quote(str(path.resolve()), safe="/") + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=3) as db:
        rows = db.execute(
            "SELECT start_epoch_ms,end_epoch_ms,estimated_ac_kwh,"
            "voltage_assumed_v,power_factor_assumed,quality "
            "FROM ev_ac_estimated_intervals "
            "WHERE start_epoch_ms >= ? AND end_epoch_ms <= ? "
            "ORDER BY start_epoch_ms LIMIT ?",
            (first_ms, last_ms, MAX_INTERVALS + 1),
        ).fetchall()
    if len(rows) > MAX_INTERVALS or not rows:
        raise ValueError("insufficient or excessive AC source coverage")
    result = []
    for a, b, value, voltage, pf, quality in rows:
        energy = float(value)
        if (not a < b or not math.isfinite(energy) or energy <= 0
                or voltage != "230" or pf != "1.0"
                or quality != "estimated_voltage_pf"):
            raise ValueError("inconsistent or unrecognized AC estimate")
        if result and a != result[-1][1]:
            raise ValueError("AC observation gap: energy unavailable")
        result.append((a, b, energy))
    if result[0][0] != first_ms or result[-1][1] != last_ms:
        raise ValueError("AC source does not cover chosen fragment")
    return result


def _soc_steps(rows: list[dict]) -> list[tuple[int, float]]:
    steps = []
    last = None
    for row in rows:
        level = row.get("volvo_soc_pct")
        if (row.get("volvo_observation_usable") != 1
                or row.get("ha_record_fresh") != 1
                or row.get("volvo_connected") != 1
                or row.get("evcc_read_ok") != 1
                or row.get("evcc_charging_detected") != 1
                or row.get("evcc_state") != "C2"
                or row.get("volvo_quality") != audit.SOURCE_QUALITY
                or type(level) not in (int, float)
                or not math.isfinite(level)
                or not 0 <= level <= 100
                or not float(level).is_integer()):
            continue
        captured = row["captured_epoch_ms"]
        changed = audit._sane_timestamp(row.get("volvo_battery_last_changed_utc"))
        received = audit._sane_timestamp(row.get("volvo_api_received_at_utc"))
        if changed is None or received is None:
            continue
        received_ms = int(received.timestamp() * 1000)
        if (changed > received or received_ms > captured + 5000
                or captured - received_ms > 5 * 60_000):
            continue
        if level != last:
            steps.append((captured, float(level)))
            last = level
    return steps


def _fit(steps: list[tuple[int, float]], intervals: list[tuple[int, int, float]]) -> dict | None:
    if len(steps) < MIN_STEPS:
        return None
    starts = [a for a, _, _ in intervals]
    cumulative = [0.0]
    for _, _, energy in intervals:
        cumulative.append(cumulative[-1] + energy)

    def energy_at(t: int) -> float | None:
        index = bisect.bisect_right(starts, t) - 1
        if index < 0 or t > intervals[index][1]:
            return None
        a, b, energy = intervals[index]
        return cumulative[index] + (t - a) / (b - a) * energy

    points = [(soc, energy_at(t)) for t, soc in steps]
    if any(energy is None for _, energy in points):
        return None
    if any(next_soc - soc != 1 for (soc, _), (next_soc, _) in zip(points, points[1:])):
        return None
    delta = points[-1][0] - points[0][0]
    if delta < MIN_SOC_SPAN:
        return None
    xs = [soc for soc, _ in points]
    ys = [energy for _, energy in points]
    avgx, avgy = statistics.mean(xs), statistics.mean(ys)
    denominator = sum((x - avgx)**2 for x in xs)
    if denominator <= 0:
        return None
    slope = sum((x - avgx) * (y - avgy) for x, y in zip(xs, ys)) / denominator
    residuals = [y - (avgy + slope * (x - avgx)) for x, y in zip(xs, ys)]
    variance = sum((y - avgy)**2 for y in ys)
    if slope <= 0 or variance <= 0:
        return None
    long_pairs = sorted((y2 - y1) / (x2 - x1)
                        for i, (x1, y1) in enumerate(points)
                        for x2, y2 in points[i + 1:] if x2 - x1 >= 6)
    if not long_pairs:
        return None
    def percentile(q: float) -> float:
        return long_pairs[int(q * (len(long_pairs) - 1))]
    return {
        "observed_soc_levels": len(points),
        "first_soc_pct": xs[0],
        "last_soc_pct": xs[-1],
        "soc_delta_percentage_points": delta,
        "estimated_ac_kwh_between_soc_levels": round(ys[-1] - ys[0], 3),
        "endpoint_kwh_per_soc_pct": round((ys[-1] - ys[0]) / delta, 4),
        "linear_fit_kwh_per_soc_pct": round(slope, 4),
        "linear_fit_r_squared": round(1 - sum(r*r for r in residuals) / variance, 5),
        "linear_fit_residual_rms_kwh": round(math.sqrt(statistics.mean(r*r for r in residuals)), 3),
        "long_pair_p10_p90_kwh_per_soc_pct": [
            round(percentile(.10), 4), round(percentile(.90), 4),
        ],
    }


def inspect(ac_db: Path, history_db: Path, *, now: datetime, days: int = 1) -> dict:
    result = {
        "status": "insufficient_evidence",
        "quality": "estimated_ict2_230v_pf1_plus_cloud_soc",
        "independent_ac_meter_verified": False,
        "soc_timestamp_role": "last_changed_not_verified_bms_sample",
        "calibrated_ac_kwh_per_soc_pct": None,
        "physical_control_eligible": False,
        "device_writes": 0,
    }
    if now.tzinfo is None:
        raise ValueError("timezone-aware now required")
    report = sessions.analyze(str(ac_db), price_path=None, now=now, days=days)
    if report.get("fragments_truncated") or not report.get("observed_charging_fragments"):
        return result
    fragment = max(report["observed_charging_fragments"],
                   key=lambda item: item["covered_seconds"])
    if fragment["covered_seconds"] < 3 * 3600:
        return result
    first_ms, last_ms = (_timestamp(fragment[key]) for key in
                         ("observed_start_utc", "observed_end_utc"))
    intervals = _intervals(ac_db, first_ms=first_ms, last_ms=last_ms)
    rows = audit.read_samples(str(history_db), since_ms=first_ms, until_ms=last_ms)
    # The first observed SoC is an unknown point *within* its integer-% band,
    # not a witnessed threshold crossing. Keep only subsequent transitions as
    # regression anchors, avoiding a false full-percent step at charge start.
    steps = _soc_steps(rows)
    fit = _fit(steps[1:], intervals)
    if fit is None:
        return result
    return {
        **result,
        "status": "empirical_candidate_only",
        "fragment_start_utc": fragment["observed_start_utc"],
        "fragment_end_utc": fragment["observed_end_utc"],
        "fragment_covered_seconds": fragment["covered_seconds"],
        "fragment_estimated_ac_kwh": fragment["estimated_ac_kwh_covered_only"],
        **fit,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ac-db", type=Path, default=AC_DB)
    parser.add_argument("--history-db", type=Path, default=HISTORY_DB)
    parser.add_argument("--days", type=int, choices=range(1, 8), default=1)
    args = parser.parse_args()
    try:
        result = inspect(args.ac_db, args.history_db,
                         now=datetime.now(timezone.utc), days=args.days)
    except (OSError, sqlite3.Error, ValueError, sessions.SessionEvidenceError,
            audit.QualityError):
        result = {
            "status": "source_invalid",
            "calibrated_ac_kwh_per_soc_pct": None,
            "physical_control_eligible": False,
            "device_writes": 0,
        }
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))


if __name__ == "__main__":
    main()
