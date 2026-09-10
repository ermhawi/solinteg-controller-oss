#!/usr/bin/env python3
"""
Measure the battery's realized round-trip efficiency from telemetry readings and compare it
to the BATTERY_RT_EFF constant (0.96, datasheet) that BOTH the optimizer and the hindsight
oracle assume.

Why this exists: the oracle scores the controller with the DP's own physics, so an efficiency
constant that diverges from the real pack biases regret in a way the oracle cannot see from
inside (a too-optimistic eta inflates the hindsight benchmark -> phantom regret attributed to
execution; see lib/oracle.ts's "materially NEGATIVE regret is a diagnostic" note for the
mirror-image failure). This script is a measurement against that assumption: it needs nothing
but the poller's own series, so it is independent of the DP, the plans, and the oracle code.

TWO DIFFERENT EFFICIENCIES, and conflating them was a real modelling error until 2026-09-10:

  eta_rt (DC)     energy out of the CELLS over energy into them -- integrates battery_w. This is
                  the datasheet's >=96% figure and what SOLINTEG_BATTERY_RT_EFF means.
  eta_ac (AC-AC)  energy out of the inverter's AC port over energy into it -- integrates
                  inverter_ac_w. This is what the OPTIMIZER needs, because the DP prices GRID
                  energy in and out: `need = dSoC / EFF_ONE_WAY` is meter-side energy. It is
                  eta_rt times the inverter's two conversion legs, so realistically 0.88-0.90
                  against eta_rt's 0.96.

Running this script and updating only BATTERY_RT_EFF could therefore never close the gap: it
measures the DC figure, and the planner was applying it to the AC path. The AC measurement below
is the one that calibrates SOLINTEG_INVERTER_CONV_EFF (EFF_ONE_WAY = sqrt(eta_rt) x that).

The AC measurement is restricted to samples where PV is essentially zero, because with the sun up
the inverter's AC port carries PV as well as the battery and the two cannot be separated from this
series. Night-time windows are exactly the grid-arbitrage windows anyway, so this is the relevant
condition rather than a limitation.

Method, per window of --window-days (default 7) and for the whole range:
  E_in  = integral of charge power    (battery_w < 0; sign convention: -charge/+discharge)
  E_out = integral of discharge power (battery_w > 0)
  dSoC  = soc_kwh at window end - at window start (endpoints = nearest non-null reading)
  AC variant, over samples with pv_w <= --pv-idle-w only:
  A_in  = integral of inverter AC input  (inverter_ac_w < 0)
  A_out = integral of inverter AC output (inverter_ac_w > 0)
Energy balance with one unknown x = sqrt(eta_rt), same split-leg model the DP uses
(charging stores x kWh per terminal kWh, discharging yields x kWh per stored kWh):
  E_in * x - E_out / x = dSoC   ->   E_in * x^2 - dSoC * x - E_out = 0
  x = (dSoC + sqrt(dSoC^2 + 4 * E_in * E_out)) / (2 * E_in)      (positive root)
  eta_rt = x^2

Caveats (read before acting on a number):
- Standby/BMS drain while idle is attributed to cycling loss here, so the measured eta_rt is
  a slight UNDERestimate of pure cycling efficiency; windows with lots of cycling (winter
  arbitrage) are more trustworthy than idle summer weeks — hence the --min-cycle-kwh floor.
- soc_kwh is quantized (derived from soc_pct); noise shrinks as windows grow.
- The AC figure additionally absorbs the inverter's own standby draw, so it is a slight
  UNDERestimate; night windows with real cycling are the trustworthy ones.
- If measured eta_rt sits stably more than ~2-3 points from BATTERY_RT_EFF across
  well-cycled windows, update the constant (lib/constants.ts, SOLINTEG_BATTERY_RT_EFF env)
  — that corrects the planner and the oracle's benchmark in the same move. Cross-check
  against oracle_daily's diagnostics.balance / params.pvDerate first: the oracle's
  energy-balance correction (2026-07-13) already absorbs part of any model-vs-reality gap,
  and the two numbers should tell one consistent story.

Usage (on the NUC, or against a pulled copy of telemetry.db):
  python3 scripts/tools/measure-battery-rt-eff.py [--db /opt/solinteg/telemetry.db]
      [--from 2026-07-01] [--to 2026-07-31] [--window-days 7]
      [--max-gap-s 600] [--min-cycle-kwh 2.0] [--pv-idle-w 50]
"""
import argparse
import math
import sys
from datetime import datetime, timedelta, timezone

import common  # sibling module (scripts/tools/) — script dir is sys.path[0]

ASSUMED_RT_EFF = 0.96  # keep in sync with lib/constants.ts BATTERY_RT_EFF default
ASSUMED_INVERTER_CONV_EFF = 0.97  # keep in sync with lib/constants.ts INVERTER_CONV_EFF default


def parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def solve_rt_eff(e_in: float, e_out: float, d_soc: float) -> float | None:
    """Positive root of E_in*x^2 - dSoC*x - E_out = 0; eta_rt = x^2. None if degenerate."""
    if e_in <= 0:
        return None
    disc = d_soc * d_soc + 4.0 * e_in * e_out
    x = (d_soc + math.sqrt(disc)) / (2.0 * e_in)
    if x <= 0:
        return None
    return x * x


def main() -> int:
    ap = argparse.ArgumentParser()
    common.add_db_arg(ap)
    ap.add_argument("--from", dest="date_from", default=None, help="UTC date, inclusive")
    ap.add_argument("--to", dest="date_to", default=None, help="UTC date, inclusive")
    ap.add_argument("--window-days", type=int, default=7)
    ap.add_argument("--max-gap-s", type=int, default=600,
                    help="ignore integration across reading gaps longer than this")
    ap.add_argument("--min-cycle-kwh", type=float, default=2.0,
                    help="windows with less charge OR discharge energy than this are reported "
                         "but not solved — too little cycling for the SoC noise floor")
    ap.add_argument("--pv-idle-w", type=float, default=50.0,
                    help="the AC-to-AC measurement only uses samples with pv_w at or below this; "
                         "with the sun up the inverter's AC port carries PV as well as the battery "
                         "and the two cannot be separated from this series")
    args = ap.parse_args()

    where, params = [], []
    if args.date_from:
        where.append("timestamp >= ?")
        params.append(f"{args.date_from}T00:00:00")
    if args.date_to:
        where.append("timestamp < ?")
        params.append((datetime.fromisoformat(args.date_to) + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00"))
    sql = "SELECT timestamp, battery_w, soc_kwh, inverter_ac_w, pv_w FROM readings"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY timestamp"

    con = common.connect_ro(args.db)
    rows = [(parse_ts(t), bw, soc, ac, pv) for t, bw, soc, ac, pv in con.execute(sql, params)]
    con.close()
    if len(rows) < 2:
        print("not enough readings in range", file=sys.stderr)
        return 1

    start = rows[0][0]
    win_len = timedelta(days=args.window_days)

    def window_index(ts: datetime) -> int:
        return int((ts - start) // win_len)

    # Per-window accumulators: [e_in, e_out, first_soc, last_soc, covered_s]
    windows: dict[int, list] = {}
    ac_in = ac_out = 0.0       # AC-side energies, night samples only
    ac_soc_first = ac_soc_last = None
    for (t1, bw1, soc1, ac1, pv1), (t2, *_rest) in zip(rows, rows[1:]):
        dt_s = (t2 - t1).total_seconds()
        w = windows.setdefault(window_index(t1), [0.0, 0.0, None, None, 0.0])
        if soc1 is not None:
            if w[2] is None:
                w[2] = soc1
            w[3] = soc1
        if bw1 is None or dt_s <= 0 or dt_s > args.max_gap_s:
            continue
        kwh = abs(bw1) * dt_s / 3_600_000.0
        if bw1 < 0:
            w[0] += kwh
        elif bw1 > 0:
            w[1] += kwh
        w[4] += dt_s
        # AC-to-AC leg, night only. The SoC endpoints are tracked over the SAME samples, so the
        # quadratic below is solved on a self-consistent energy balance rather than on a dSoC that
        # includes daytime charging the AC integrals never saw.
        if ac1 is not None and pv1 is not None and pv1 <= args.pv_idle_w:
            ac_kwh = abs(ac1) * dt_s / 3_600_000.0
            if ac1 < 0:
                ac_in += ac_kwh
            elif ac1 > 0:
                ac_out += ac_kwh
            if soc1 is not None:
                if ac_soc_first is None:
                    ac_soc_first = soc1
                ac_soc_last = soc1
    # Last row's SoC closes its window's endpoint.
    t_last, _, soc_last, _, _ = rows[-1]
    if soc_last is not None:
        w = windows.setdefault(window_index(t_last), [0.0, 0.0, None, None, 0.0])
        if w[2] is None:
            w[2] = soc_last
        w[3] = soc_last

    print(f"readings: {len(rows)}  range: {rows[0][0]:%Y-%m-%d %H:%M} .. {t_last:%Y-%m-%d %H:%M} UTC")
    print(f"{'window':<24}{'E_in':>8}{'E_out':>8}{'dSoC':>8}{'cov%':>6}{'eta_rt':>8}")
    tot_in = tot_out = 0.0
    solved: list[tuple[float, float]] = []  # (eta, weight = min(E_in, E_out))
    for idx in sorted(windows):
        e_in, e_out, soc_a, soc_b, cov_s = windows[idx]
        w_start = start + idx * win_len
        label = f"{w_start:%Y-%m-%d} +{args.window_days}d"
        cov = 100.0 * cov_s / win_len.total_seconds()
        tot_in += e_in
        tot_out += e_out
        if soc_a is None or soc_b is None or min(e_in, e_out) < args.min_cycle_kwh:
            print(f"{label:<24}{e_in:>8.2f}{e_out:>8.2f}{'n/a':>8}{cov:>6.0f}{'(skip)':>8}")
            continue
        eta = solve_rt_eff(e_in, e_out, soc_b - soc_a)
        if eta is None or not (0.5 < eta < 1.1):
            print(f"{label:<24}{e_in:>8.2f}{e_out:>8.2f}{soc_b - soc_a:>8.2f}{cov:>6.0f}{'(degen)':>8}")
            continue
        solved.append((eta, min(e_in, e_out)))
        print(f"{label:<24}{e_in:>8.2f}{e_out:>8.2f}{soc_b - soc_a:>8.2f}{cov:>6.0f}{eta:>8.3f}")

    # Whole-range solve: endpoints from the first/last non-null SoC anywhere in range.
    soc_first = next((r[2] for r in rows if r[2] is not None), None)
    soc_final = next((r[2] for r in reversed(rows) if r[2] is not None), None)
    print()
    if soc_first is not None and soc_final is not None and min(tot_in, tot_out) >= args.min_cycle_kwh:
        eta_all = solve_rt_eff(tot_in, tot_out, soc_final - soc_first)
        if eta_all:
            delta_pts = 100.0 * (eta_all - ASSUMED_RT_EFF)
            print(f"whole range: E_in {tot_in:.2f} kWh, E_out {tot_out:.2f} kWh, "
                  f"dSoC {soc_final - soc_first:+.2f} kWh -> eta_rt = {eta_all:.3f}")
            print(f"assumed BATTERY_RT_EFF = {ASSUMED_RT_EFF:.3f}  (measured - assumed = {delta_pts:+.1f} points)")
            if abs(delta_pts) > 3.0 and min(tot_in, tot_out) >= 20.0:
                print("=> stable gap > 3 points on real cycling: consider updating BATTERY_RT_EFF "
                      "(lib/constants.ts / SOLINTEG_BATTERY_RT_EFF) -- fixes planner AND oracle together. "
                      "Cross-check oracle_daily diagnostics first (see docstring).")
            elif abs(delta_pts) > 3.0:
                print("=> gap > 3 points but little total cycling -- wait for more charge/discharge "
                      "throughput before acting (see --min-cycle-kwh caveat).")
            else:
                print("=> within noise of the assumed constant; no action.")
    else:
        print("whole range: insufficient cycling or missing SoC endpoints — nothing to conclude yet.")

    if solved:
        wsum = sum(w for _, w in solved)
        wavg = sum(e * w for e, w in solved) / wsum
        print(f"cycling-weighted mean of {len(solved)} solved windows: eta_rt = {wavg:.3f}")

    # ── AC-to-AC: the figure the OPTIMIZER actually needs ────────────────────────────────────────
    # Same quadratic, same unknown x = one-way efficiency, but with the inverter's AC port as the
    # boundary instead of the battery terminals. EFF_ONE_WAY in lib/constants.ts is exactly this x,
    # because the DP prices GRID energy: `need = dSoC / EFF_ONE_WAY` is meter-side energy. Updating
    # only BATTERY_RT_EFF could never close that gap -- it is the DC figure.
    print()
    eta_ac = (
        solve_rt_eff(ac_in, ac_out, ac_soc_last - ac_soc_first)
        if ac_soc_first is not None and ac_soc_last is not None and min(ac_in, ac_out) >= args.min_cycle_kwh
        else None
    )
    if eta_ac is None or not (0.5 < eta_ac < 1.1):
        print(f"AC-to-AC: not enough usable night-time cycling (A_in {ac_in:.2f} kWh, "
              f"A_out {ac_out:.2f} kWh, pv <= {args.pv_idle_w:.0f} W) -- nothing to conclude yet.")
        return 0

    assumed_ac = ASSUMED_RT_EFF * ASSUMED_INVERTER_CONV_EFF ** 2
    print(f"AC-to-AC (night, pv <= {args.pv_idle_w:.0f} W): A_in {ac_in:.2f} kWh, "
          f"A_out {ac_out:.2f} kWh, dSoC {ac_soc_last - ac_soc_first:+.2f} kWh -> eta_ac = {eta_ac:.3f}")
    print(f"assumed eta_ac = BATTERY_RT_EFF x INVERTER_CONV_EFF^2 = {assumed_ac:.3f}  "
          f"(measured - assumed = {100.0 * (eta_ac - assumed_ac):+.1f} points)")

    # Back out the inverter's per-leg efficiency from the measured pair: BATTERY_RT_EFF stays the
    # datasheet DC figure, INVERTER_CONV_EFF carries the two conversion legs.
    eta_dc_ref = (
        solve_rt_eff(tot_in, tot_out, soc_final - soc_first)
        if soc_first is not None and soc_final is not None and min(tot_in, tot_out) >= args.min_cycle_kwh
        else None
    )
    if eta_dc_ref is None:
        print("=> no usable DC figure over the same range, so the inverter's own share cannot be "
              "separated. eta_ac above is still the number EFF_ONE_WAY squared should match.")
    elif eta_ac >= eta_dc_ref:
        print("=> measured eta_ac >= eta_rt, which is physically impossible: the night-only AC window "
              "and the whole-range DC figure cover different periods, or pv_w is under-reported at "
              "night. Widen the range or lower --pv-idle-w before trusting either figure.")
    else:
        implied_conv = math.sqrt(eta_ac / eta_dc_ref)
        print(f"=> implied INVERTER_CONV_EFF (per leg) = sqrt(eta_ac / eta_rt) = {implied_conv:.3f} "
              f"(assumed {ASSUMED_INVERTER_CONV_EFF:.3f}); set SOLINTEG_INVERTER_CONV_EFF if the gap "
              "is stable across windows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
