# Sundsvall: historical evaluation of upstream risk refinements (2026-10-19)

**Decision: leave BOTH experimental optimizer refinements OFF in production.**
This is a completed experiment with a negative/insufficient economic result, not an unstarted merge task.

The upstream merge is already deployed at `6144cab`; this evaluation adds only offline
replay tooling and its findings, without changing the live optimizer or battery commands.

## Evidence and method

- Local production `/opt/solinteg/telemetry.db` (SQLite **read-only**, never modified).
- 70 days of logged history (11 August–19 October 2026), 9,704 `optimizer_runs`,
  ~197,000 Modbus readings.
- Sampled 195 logged optimizer plans at approximately **06:05, 14:05, 22:05 Stockholm time**,
  covering 65 calendar dates **13 August–16 October**. Two plans had invalid/incomplete
  trajectories, one had a stale/misaligned timestamp (log from 1 September still contained
  31 August slots), so **192** were evaluable.
- Replay each plan from the **original forecast inputs**, prices, and starting SoC. For each
  candidate, compare its full planned SoC path against later **measured** solar and house load
  using the existing `evaluateDispatch` economics, including wear and identical continuation
  value of energy remaining at the horizon. Require at least 98% observed 15-minute slots
  and no zero-filled gaps; the 192 accepted plans had 100% slot coverage.
- Four alternatives: current settings; only solar-risk survival; only deferral-until-needed;
  both. Local 12-hour deferral horizon, 12-hour solar-risk lookahead, load reserve, combined
  risk-sacrifice guard, and battery import/charge safety limits are preserved.
- Group plan results by date and use a three-day block bootstrap for exploratory confidence
  bounds. **The simulated plans overlap strongly**: the average per plan cannot be multiplied
  by the number of runs to claim realized SEK savings.
- Strong limitation: this is a counterfactual economic score of planned SoC paths under
  measured demand/PV, **not** closed-loop firmware simulation or achieved electricity-bill
  savings. The metered house load/PV may also have been affected by the real control plan.

## Results — difference relative to today's optimizer

| Variant | 192 valid plans: average (öre/plan) | Better / worse / tied | 71 high-fidelity plans: average (öre/plan) |
| --- | ---: | ---: | ---: |
| Solar-risk survival | **−3.19** | 32 / 48 / 112 | **−7.03** |
| Deferral-until-needed | **+1.09** | 11 / 9 / 172 | **−0.37** |
| Both enabled | **−2.97** | 35 / 53 / 104 | **−7.76** |

*High-fidelity* means the current optimizer reproduces at least **99%** of the first
24 hours of the original saved action sequence. There were 71 such plans from 35 dates.
Over the entire sample the mean original-plan fidelity was only 90.9%, because the
optimizer changed materially during the historical window.

The full-sample three-day-block 95% bootstrap ranges are:
solar survival [−6.18, +0.02] öre, deferral [+0.07, +2.35] öre,
both [−6.17, +0.46] öre **per representative plan**. However, the seemingly positive
deferral result is driven by older, less faithful logs, and the high-fidelity subset
reverses its sign. The reported intervals do not fix the old-code comparison problem.

The immediate action differed in just **5 of 192** simulations for solar survival and
**1 of 192** for deferral, despite some more-distant planned changes. No reliable
near-term financial or practical benefit was demonstrated.

### Decision

- `SOLINTEG_SOLAR_RISK_SURVIVAL`: **OFF**, as already configured by default.
- `SOLINTEG_DEFERRAL_UNTIL_NEEDED`: **OFF**, as already configured by default.
- Do **not** activate either on the live system solely because the upstream site measured
  a benefit. Preserve existing risk, reserve, SoC floor/ceiling and guard behavior.
- Re-evaluate after the installation has accumulated a larger, more stable **winter**
  sample under the current code. Prefer high-fidelity historical plans from *after* the
  upstream merge and confirm savings exceed both replay noise and typical execution errors.
  A shadow replay is not sufficient proof of physically achieved benefit by itself.

## Three-phase grid meter verification

The new per-phase Modbus read is deployed and works on the actual unit. A six-hour
read-only check on 19 October found **414 samples** with valid per-phase measurements.
The sum of L1+L2+L3 differed from total grid power by at most **2 W**
(median **1 W**). While at least one other phase exceeded 100 W, exact-zero
fractions were: L1 0/388, L2 6/414 (1.45%), L3 0/406. No dead-CT condition
was detected. The poller still falls back to the old block if an extended read fails.

A separate operational observation: four `error_revert_failed` control events on
14 and 18 October around 02:00 UTC arose because the Modbus TCP endpoint was
unreachable for both initial command and recovery. This predates the upstream merge;
service, live poller and dispatch were healthy at the later check. Investigate recurrence
independently; this evaluation did not alter any inverter command.

## Energy-tax refund (Skatteverket 739): do not activate yet

Skatteverket confirms that private individuals may apply once a year to reclaim tax
on **grid-imported** electricity stored in a battery and later fed back into the **same**
concession-required grid, excluding storage losses. Legal basis: 11 kap. 13 § lagen
(1994:1776) om skatt på energi. See:
- https://www.skatteverket.se/privat/skatter/skattpabransleochelforprivatpersoner/aterbetalningavskattpaelefterbatterilagring.4.5fb2fa71a0b0adb74a23d8.html
- https://www4.skatteverket.se/rattsligvagledning/edition/2026.4/323435.html
- https://www4.skatteverket.se/rattsligvagledning/edition/2026.12/323428.html

2026 general tax: **36.0 öre/kWh**. Sundsvall is not among the Västernorrland
municipalities eligible for the **9.6 öre/kWh northern reduction** (Sollefteå,
Ånge, Örnsköldsvik only). Verify the *actual paid* tax on an electricity network invoice.

As a scale check, 11 Aug–19 Oct readings contain about **15.25 kWh** of positive
grid export while PV was below 50 W and the battery reported discharging. At 36 öre,
this would correspond to only **5.49 SEK even if every exported kWh had grid origin**.
Actual eligible quantity may be lower because stored solar can also be exported.
This is a **conditional upper bound for no-PV hours only**, **not** a total annual claim,
not proof of grid-origin provenance, and it excludes daytime exports. At this scale,
automated refund-driven trading is not presently worth enabling, particularly without
auditable stateful origin bookkeeping across replans and year boundaries.

`SOLINTEG_REFUND_CREDIT` and `SOLINTEG_REFUND_TAX_PAID_ORE` remain unset; refund
credit therefore defaults to **0**.

## Reproducing the backtest without touching the live database

The exporter creates a small *private* SQLite copy of only representative plans and
meter readings. It refuses to overwrite existing output.

```bash
sudo -u solinteg python3 scripts/tools/export-feature-backtest.py \
  --source /opt/solinteg/telemetry.db \
  --out /tmp/solinteg-feature-replay.db --start 2026-08-13 --end 2026-10-16

# Give your own user read access to the COPY only; NEVER alter permissions on telemetry.db.
sudo chown "$USER":"$USER" /tmp/solinteg-feature-replay.db

# In the dev checkout, provide the SAME non-secret numeric site config as the actual systemd
# environment, e.g. SOLINTEG_BATTERY_KWH / SOLINTEG_BATTERY_MAX_W / SOLINTEG_GRID_CAP_W /
# SOLINTEG_SOC_FLOOR_PCT / SOLINTEG_SOC_CEILING_PCT. Do not copy credentials.
SOLINTEG_BACKTEST=1 TELEMETRY_DB_PATH=/tmp/solinteg-feature-replay.db \
  npm test -- scripts/tools/upstream-feature-backtest.manual.test.ts --testTimeout=600000

# Then securely remove the temporary household telemetry copy.
rm /tmp/solinteg-feature-replay.db
```

Test script: `scripts/tools/upstream-feature-backtest.manual.test.ts`. It is skipped
during normal `npm test` and writes only its aggregate/per-run evaluation JSON under
`/tmp`, not to the live DB or repository. Sample export script:
`scripts/tools/export-feature-backtest.py`.
