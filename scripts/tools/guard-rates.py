#!/usr/bin/env python3
"""
How often each dispatch guard fires, per day — the standing check that a safety guard has not
quietly turned into a policy.

Every guard in dispatch_loop.py is conservative by design: on doubt it falls back to Auto. That is
right for one tick and wrong as a steady state, because Auto is not neutral — it discharges the
battery at whatever the house draws, which on a night the plan meant to hold is the opposite of the
plan. A guard firing occasionally is insurance; the same guard firing every slot is the controller
silently not being in control, and nothing in the system says so out loud.

Two of the guards had exactly that failure available to them before 2026-09-10:
  * skipped_solar_shortfall — check_solar_funding recomputed the plan's funding split from the
    UNMARGINED forecast while the DP had planned against load x 1.15, which overstates the shortfall
    and vetoes charges the plan had already accounted for (OPTIMIZER-AUDIT.md E1);
  * no plan at all — load_latest_plan filtered on price_date, so a plan spanning midnight became
    invisible at 00:00 every single day (E4).
Run this before and after any change to a guard or its thresholds; that is what the tool is for.

What the outcomes mean (see dispatch_loop.py):
  applied                    the plan was executed
  skipped_divergence         live SoC too far from the plan's expectation — plan treated as stale
  skipped_solar_shortfall    a forced charge would have bought more grid than the plan intended
  error_reverted             the apply failed; the fail-safe revert to Auto succeeded
  error_revert_failed        the apply failed AND the revert failed — inverter state unconfirmed

Usage:
  python3 scripts/tools/guard-rates.py [--db PATH] [--days 14] [--by-slot]
"""
import argparse
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import common  # sibling module (scripts/tools/) — script dir is sys.path[0]

# Outcomes where the loop did NOT do what the plan asked. Kept explicit rather than "everything
# that is not applied", so a new outcome added to the loop shows up as unclassified below instead
# of being silently counted as healthy.
GUARD_OUTCOMES = ("skipped_divergence", "skipped_solar_shortfall")
ERROR_OUTCOMES = ("error_reverted", "error_revert_failed")


def parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=common.UTC)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    common.add_db_arg(ap)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--by-slot", action="store_true",
                    help="also break the guard rate down by hour of day — a guard that only fires "
                         "in the small hours is a different problem from one that fires all day")
    args = ap.parse_args(argv[1:])

    since = (datetime.now(common.UTC) - timedelta(days=args.days)).isoformat()
    con = common.connect_ro(args.db)
    rows = con.execute(
        "SELECT timestamp, slot_time, planned_action, outcome, armed FROM control_actions "
        "WHERE timestamp >= ? ORDER BY timestamp",
        (since,),
    ).fetchall()
    con.close()
    if not rows:
        print(f"no control_actions rows in the last {args.days} days.", file=sys.stderr)
        return 1

    per_day: dict[str, Counter] = defaultdict(Counter)
    per_hour: dict[int, Counter] = defaultdict(Counter)
    totals: Counter = Counter()
    armed_rows = 0
    idle_rows = 0
    for timestamp, slot_time, planned_action, outcome, armed in rows:
        local = parse_ts(timestamp).astimezone(common.STOCKHOLM)
        armed_rows += 1 if armed else 0
        # Idle rows are excluded from BOTH the numerator and the denominator. An idle decision is
        # the loop agreeing with the plan, not a guard firing, so counting it in the denominator
        # would make every guard rate look tiny on a quiet winter night — and counting its
        # `applied` in the numerator produced percentages over 100, which is worse than useless.
        if planned_action == "idle":
            idle_rows += 1
            continue
        per_day[local.strftime("%Y-%m-%d")][outcome] += 1
        per_hour[local.hour][outcome] += 1
        totals[outcome] += 1
        per_day[local.strftime("%Y-%m-%d")]["_decisions"] += 1
        per_hour[local.hour]["_decisions"] += 1
        totals["_decisions"] += 1

    print(f"control_actions over the last {args.days} days: {len(rows)} rows "
          f"({armed_rows} armed, {len(rows) - armed_rows} shadow)")
    print(f"idle rows excluded: {idle_rows}   non-idle decisions scored below: {totals['_decisions']}")

    print(f"\n{'outcome':<28}{'count':>8}{'% of decisions':>16}")
    print("─" * 52)
    decisions = max(1, totals["_decisions"])
    for outcome, n in totals.most_common():
        if outcome == "_decisions":
            continue
        marker = "  <-- guard" if outcome in GUARD_OUTCOMES else "  <-- ERROR" if outcome in ERROR_OUTCOMES else ""
        print(f"{outcome:<28}{n:>8}{100.0 * n / decisions:>15.1f}%{marker}")

    unclassified = [o for o in totals if o not in GUARD_OUTCOMES and o not in ERROR_OUTCOMES
                    and o not in ("applied", "_decisions")]
    if unclassified:
        print(f"\nUNCLASSIFIED outcomes (this tool has not been taught about them): {unclassified}")

    print(f"\n{'day':<12}{'decisions':>10}{'divergence':>12}{'shortfall':>11}{'errors':>8}")
    print("─" * 53)
    for day in sorted(per_day):
        c = per_day[day]
        print(f"{day:<12}{c['_decisions']:>10}{c['skipped_divergence']:>12}"
              f"{c['skipped_solar_shortfall']:>11}{sum(c[o] for o in ERROR_OUTCOMES):>8}")

    if args.by_slot:
        print(f"\n{'hour':<6}{'decisions':>10}{'divergence':>12}{'shortfall':>11}")
        print("─" * 39)
        for hour in sorted(per_hour):
            c = per_hour[hour]
            print(f"{hour:<6}{c['_decisions']:>10}{c['skipped_divergence']:>12}"
                  f"{c['skipped_solar_shortfall']:>11}")

    guard_total = sum(totals[o] for o in GUARD_OUTCOMES)
    rate = 100.0 * guard_total / decisions
    print()
    if rate > 25.0:
        print(f"=> {rate:.0f}% of non-idle decisions were vetoed by a guard. That is a POLICY, not "
              "insurance: the plan is largely not being executed. Check the guard's own numbers in "
              "control_actions.detail_json before adjusting a threshold — the cause is more often a "
              "plan/executor disagreement than a genuinely wrong threshold.")
    elif rate > 5.0:
        print(f"=> {rate:.0f}% of non-idle decisions vetoed. Worth understanding, not alarming.")
    else:
        print(f"=> {rate:.1f}% of non-idle decisions vetoed — guards are behaving as insurance.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
