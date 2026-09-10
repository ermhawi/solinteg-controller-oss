#!/usr/bin/env python3
"""
Forecast-vs-actual error for every solar source, per month and per hour of day.

This is the measurement that should decide the solar constants, in place of the hand-set values
that accumulated instead: SOLAR_FORECAST_BIAS_FACTOR, solarCalibrationByMonth, and which source
the ensemble should weight or drop. Until 2026-09-10 the repo carried a flat 1.75 multiplier and a
pair of 1.40 monthly factors, both of which turned out to be masking a geometry bug — precisely the
failure mode a standing error report makes impossible to repeat, because a bias you can see the
shape of is a bias you can attribute.

The data is already being collected: `logSolarForecasts` writes every source's raw forecast to
`solar_forecasts` on each plan, and the poller writes measured `pv_w` to `readings`. Nothing read
them together until now.

METHOD, per (source, month) and per (source, hour-of-day):
  forecast  the FIRST forecast logged for that date — i.e. the one made furthest ahead, which is
            what planning actually used overnight. Later same-day forecasts are much easier and
            averaging them in would flatter every source. Use --last to score the final forecast
            instead, which measures the nowcast-ish end of the range.
  actual    measured pv_w integrated over the same 15-min slot.
  bias      mean(forecast - actual), in kWh/slot. POSITIVE = over-forecast.
  MAE       mean |forecast - actual|. The headline number for ranking sources.
  nMAE      MAE / mean(actual), so months of different size are comparable.

Only slots with real production or a real forecast are scored (both zero is a winter night, and
including them makes every source look excellent).

Caveats:
  * `pv_w` is DC-side inverter production; the forecast models panel-plane irradiance through
    ghiToKwh(). A systematic gap between them is exactly what performanceRatio is for, so read a
    uniform all-month bias as "PR is wrong", and a seasonally-varying one as "the geometry or the
    climatology is wrong".
  * CURTAILMENT reads as over-forecast. If the battery was full and the price negative, actual pv_w
    is low because the inverter was told to back off, not because the forecast missed. There is no
    curtailment flag in `readings` to exclude those slots, so treat a bias concentrated in sunny
    midday slots on negative-price days with suspicion.
  * A few weeks is not a season. Do not set a monthly constant from one month of data.

Usage:
  python3 scripts/tools/solar-forecast-error.py [--db PATH] [--from YYYY-MM-DD] [--to YYYY-MM-DD]
      [--last] [--by hour|month|both]
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import common  # sibling module (scripts/tools/) — script dir is sys.path[0]

SLOT_MS = 15 * 60 * 1000
SLOTS_PER_DAY = 96


def parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=common.UTC)


def actual_slot_kwh(con, date_str: str) -> dict[int, float]:
    """Measured pv_w integrated into that Stockholm date's 15-min slots, keyed by slot index.

    Indexed by elapsed time from the date's real Stockholm midnight, matching how the forecast
    arrays are keyed and staying correct on the 92/100-slot DST days (CLAUDE.md key invariants).
    """
    start_local = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=common.STOCKHOLM)
    start = start_local.astimezone(common.UTC)
    end = start + timedelta(days=1, hours=1)  # generous; slot index bounds the result anyway
    rows = con.execute(
        "SELECT timestamp, pv_w FROM readings WHERE timestamp >= ? AND timestamp < ? "
        "AND pv_w IS NOT NULL ORDER BY timestamp",
        (start.isoformat(), end.isoformat()),
    ).fetchall()

    total: dict[int, float] = defaultdict(float)
    count: dict[int, int] = defaultdict(int)
    for ts, pv_w in rows:
        idx = int((parse_ts(ts) - start).total_seconds() // (15 * 60))
        if 0 <= idx < SLOTS_PER_DAY + 4:
            total[idx] += float(pv_w)
            count[idx] += 1
    # Mean W over the slot -> kWh in that slot.
    return {i: total[i] / count[i] / 1000 * 0.25 for i in total if count[i] > 0}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    common.add_db_arg(ap)
    ap.add_argument("--from", dest="date_from", default=None)
    ap.add_argument("--to", dest="date_to", default=None)
    ap.add_argument("--last", action="store_true",
                    help="score the LAST forecast logged for each date instead of the first "
                         "(measures the near-term end of the range rather than what planning used)")
    ap.add_argument("--by", choices=["hour", "month", "both"], default="both")
    args = ap.parse_args(argv[1:])

    where, params = [], []
    if args.date_from:
        where.append("date >= ?")
        params.append(args.date_from)
    if args.date_to:
        where.append("date <= ?")
        params.append(args.date_to)
    sql = "SELECT date, source, logged_at, forecast_json FROM solar_forecasts"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY date, source, logged_at"

    con = common.connect_ro(args.db)
    rows = con.execute(sql, params).fetchall()
    if not rows:
        print("no solar_forecasts rows in range — nothing to score yet.", file=sys.stderr)
        return 1

    # Pick one forecast per (date, source): the first logged, or the last with --last.
    chosen: dict[tuple[str, str], str] = {}
    for date, source, _logged_at, forecast_json in rows:
        key = (date, source)
        if args.last or key not in chosen:
            chosen[key] = forecast_json

    actuals_cache: dict[str, dict[int, float]] = {}
    # (source, bucket) -> [sum_err, sum_abs_err, sum_actual, n]
    by_month: dict[tuple[str, int], list] = defaultdict(lambda: [0.0, 0.0, 0.0, 0])
    by_hour: dict[tuple[str, int], list] = defaultdict(lambda: [0.0, 0.0, 0.0, 0])

    for (date, source), forecast_json in sorted(chosen.items()):
        if date not in actuals_cache:
            actuals_cache[date] = actual_slot_kwh(con, date)
        actual = actuals_cache[date]
        if not actual:
            continue
        try:
            # forecast_json is date -> 96 slot kWh; the row's own date is the one being scored.
            per_date = json.loads(forecast_json)
        except (ValueError, TypeError):
            continue
        slots = per_date.get(date)
        if not isinstance(slots, list):
            continue
        month = int(date[5:7])
        for idx, forecast_kwh in enumerate(slots):
            if forecast_kwh is None:
                continue
            actual_kwh = actual.get(idx)
            if actual_kwh is None:
                continue
            if forecast_kwh <= 0 and actual_kwh <= 0:
                continue  # winter night: scoring it flatters every source
            err = float(forecast_kwh) - actual_kwh
            for bucket, table in ((month, by_month), (idx // 4, by_hour)):
                acc = table[(source, bucket)]
                acc[0] += err
                acc[1] += abs(err)
                acc[2] += actual_kwh
                acc[3] += 1
    con.close()

    def report(title: str, table: dict, label: str) -> None:
        if not table:
            return
        print(f"\n{title}")
        print(f"{'source':<22}{label:>7}{'n':>7}{'bias':>9}{'MAE':>9}{'nMAE':>8}{'actual':>9}")
        print("─" * 71)
        for (source, bucket) in sorted(table):
            sum_err, sum_abs, sum_actual, n = table[(source, bucket)]
            if n == 0:
                continue
            n_mae = (sum_abs / n) / (sum_actual / n) if sum_actual > 0 else float("nan")
            print(f"{source:<22}{bucket:>7}{n:>7}{sum_err / n:>9.3f}{sum_abs / n:>9.3f}"
                  f"{n_mae:>8.2f}{sum_actual / n:>9.3f}")

    scored = "last" if args.last else "first"
    print(f"Solar forecast error — scoring the {scored} forecast logged per date, kWh per 15-min slot")
    print("bias > 0 = over-forecast.  nMAE = MAE / mean actual.")
    if args.by in ("month", "both"):
        report("By month", by_month, "month")
    if args.by in ("hour", "both"):
        report("By hour of day (Stockholm)", by_hour, "hour")
    print("\nReminder: a uniform all-month bias points at performanceRatio; a seasonally-varying one")
    print("at the geometry or the climatology. Curtailed slots read as over-forecast — see the")
    print("caveats in this script's docstring before setting any constant from these numbers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
