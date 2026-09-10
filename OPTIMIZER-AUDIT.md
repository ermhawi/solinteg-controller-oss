# Charging-algorithm audit — findings and implementation plan

Audit date: 2026-09-10. Branch: `Sundsvall`. Reviewed against the *live* site configuration in
`.env.local` (battery 15.055 kWh / 7.5 kW, grid cap 10 kW, SoC floor 10 %, ceiling 100 %, SE2,
62.4396 N / 17.3611 E, single 5.4 kWp plane at 45° tilt).

**Objective assumed throughout: minimise the household's net electricity cost, under all
conditions.** Everything below is judged against that, not against "does the DP find the optimum
of the model it was given" — it does; the model is where the money leaks.

## Scope

Every component that participates in the "charge or not" decision was read end to end:

| Layer | Files |
|---|---|
| Decision | `lib/optimizer.ts` (DP + `evaluateDispatch`), `lib/constants.ts` |
| Plan assembly | `lib/plan.ts`, `lib/pipeline.ts`, `lib/horizon.ts`, `lib/slot-utils.ts` |
| Inputs | `lib/prices.ts`, `lib/forecast.ts`, `lib/metno-thredds.ts`, `lib/solar.ts`, `lib/solar-nowcast.ts`, `lib/load.ts`, `lib/consumption-data.ts`, `lib/irradiance-data.ts`, `lib/inverter.ts`, `lib/telemetry/readings.ts` |
| Publication | `lib/telemetry/dispatch.ts` (`logOptimizerRun`), `app/api/replan/route.ts` |
| Execution | `scripts/services/dispatch_loop.py`, `scripts/services/inverter_control.py`, `scripts/services/modbus_poller.py` |
| Reference | `MODBUS.md`, `DOMAIN.md`, `DESIGN-reserve.md`, `lib/__tests__/optimizer.test.ts` |

Findings marked **[measured]** were reproduced numerically; see Appendix A for how.

## Verdict

The dynamic program itself is sound: it is a correct backward-induction DP over a discretised
SoC, its physics (`computeFlows`) is self-consistent, and `evaluateDispatch` prices trajectories
with the same arithmetic. The optimality properties pinned by `optimizer.test.ts` hold.

The losses are in four places, in descending order of money:

1. **A wrong solar forecast.** The array azimuth is passed to Open-Meteo and forecast.solar in
   the wrong convention, so the site is forecast as if the panels faced *north*. Annual forecast
   solar is ~2.1× too low (1.5× in June, 6.2× in December). Three separate fudge factors have
   grown on top of it to partially hide it.
2. **A capacity model that conflates three different physical limits**, so house load steals
   battery-charging headroom that the grid connection actually has.
3. **Zero terminal value**, so every plan ends at the SoC floor and the horizon end is a cliff.
4. **Three ad-hoc risk knobs** that are horizon-length-dependent, mutually contradictory, one of
   them measured on a different problem than the one solved, and none of them validated against
   the hindsight oracle that already exists to validate them.

Nothing here is a safety defect: the executor's guards (arm gate, SoC floor/ceiling, divergence,
solar-funding, watchdog, fail-safe revert) are conservative and well reasoned. Two execution gaps
below (E2, E3) are cost/fuse risks rather than safety-model holes.

## Implementation log

Each finding below carries its own **Status** line. This is the chronological view; every entry
was shipped with `npm test`, `npm run test:py`, `npm run typecheck`, `npx eslint` and
`npm run build` all green.

| # | Finding | Status |
|---|---|---|
| 1 | P0-1 solar azimuth convention (+ the whole fudge stack, + a new P0-1b) | ✅ done |
| 2 | P0-2 grid capacity vs battery power conflated | ✅ done |
| 3 | E3 site-level import limit (fuse backstop for P0-2's raised budget) | ✅ implemented, gated on an on-device probe |
| 4 | P0-3 part 2 + G7 horizon robustness (DST days, forecast holes, visibility) | ✅ done |
| 5 | P1-3 `evaluateDispatch` priced the wrong problem + P0-3 part 1 terminal value | ✅ done (one commit — the guard fix needs the terminal-value option) |
| 6 | P1-2 deferral ramp unbounded in horizon length | ✅ done |
| 7 | P1-4 solar premium saturated over a multi-day horizon + guard covered only deferral | ✅ done |
| 8 | P1-1 forecast price premium was a hoarding incentive | ✅ done (band captured for the refit, not yet consumed) |
| 9 | P1-5 DC datasheet efficiency used on the AC path | ✅ done |
| 10 | E1 + E4 + E5 + E7 executor/plan mismatches | ✅ done (E7 was misdiagnosed in the audit — corrected below) |

---

# P0 — Correctness defects with direct cost impact

## P0-1 Solar azimuth is passed in the wrong convention (root cause of the whole solar-fudge stack)

> **Status: ✅ done.** `apiAzimuth()` added in `lib/solar.ts` and applied to all five API URLs
> (`lib/forecast.ts` — four Open-Meteo, one forecast.solar). `SOLAR_FORECAST_BIAS_FACTOR` 1.75 → 1.0
> and `solarCalibrationByMonth` → twelve 1.0s, both with the history written into their docstrings.
> `scripts/tools/build-irradiance-openmeteo.mjs` now *requires* coordinates and geometry instead of
> silently defaulting to the old Göteborg site, converts the azimuth itself, and records both
> conventions in the generated header; `lib/irradiance-data.ts` regenerated from 20 years of ERA5
> (2005–2024) at 62.4396/17.3611, tilt 45°, azimuth 153° from north. Modelled annual production
> went from 438 to **893 kWh/kWp** — the plausible figure for a 45° SSE plane at this latitude.
> Nowcast clamp `MAX_RATIO` 1.5 → 2.0, with the asymmetry now justified in a comment.
> `parseForecastSolar` stays deliberately uncalibrated, documented (applying our own GHI-model
> correction to a foreign production model would double-correct; the real fix is a per-source bias
> fitted from the `solar_forecasts` table).
> New tests: `apiAzimuth` conversion table (site array, cardinals, the reference site's three
> arrays, range invariant), the bias factor pinned at 1, the requested azimuth pinned at −27 with
> negative assertions against both the query-string and path-segment forms of the old bug.
>
> Deviation from the plan as written: step 4 said to apply `solarCalibrationByMonth` inside
> `parseForecastSolar` too. It is deliberately *not* applied — see the reason above, recorded in
> the code.

**What is wrong.** `lib/constants.ts:259` documents `SolarArray.azimuthDeg` as *"0° azimuth =
north, 180° = south"*, and the site is configured `azimuthDeg: 153` (SSE) at
`lib/constants.ts:283`. That value is then passed **verbatim** as the `azimuth=` query parameter
to Open-Meteo (`lib/forecast.ts:14,15,98,104`) and as the `:az` path segment to forecast.solar
(`lib/forecast.ts:18`). Both APIs use the opposite convention: **0 = south, −90 = east, +90 =
west, ±180 = north.** So `153` is read as *27° east of north*.

**[measured] Evidence.** Open-Meteo's convention, probed directly at the site coordinates
(45° tilt, `metno_nordic`, 2026-09-10):

| `azimuth=` | daily plane irradiance | peak | peak hour |
|---|---|---|---|
| 0 | 6033 Wh/m² | 834 W/m² | 13:00 |
| −90 | 3793 | 593 | 11:00 (morning ⇒ east) |
| +90 | 3927 | 608 | 16:00 (afternoon ⇒ west) |
| 180 | 1386 | 141 | — (north) |

So `0 = south` beyond doubt. The correct value for this array is **−27**, not 153.

ERA5 archive, full year 2024, same coordinates and tilt, converted with the site's own
`ghiToKwh()` (5.4 kWp × PR 0.78):

| Month | as sent (`az=153`) | correct (`az=−27`) | factor missing |
|---|---|---|---|
| Jan | 0.60 kWh/day | 2.09 | **3.5×** |
| Feb | 1.65 | 6.18 | 3.7× |
| Mar | 3.95 | 10.72 | 2.7× |
| Apr | 8.28 | 17.26 | 2.1× |
| May | 14.93 | 28.74 | 1.9× |
| Jun | 16.49 | 24.68 | 1.5× |
| Jul | 13.81 | 22.16 | 1.6× |
| Aug | 9.73 | 21.20 | 2.2× |
| Sep | 4.77 | 16.35 | 3.4× |
| Oct | 2.03 | 9.49 | 4.7× |
| Nov | 0.80 | 3.78 | 4.7× |
| Dec | 0.31 | 1.90 | **6.2×** |
| **Year** | **2364 kWh** | **5027 kWh** | **2.13×** |

(438 vs 931 kWh/kWp/yr — the corrected figure is the plausible one for a 45° SSE plane at 62.4 N.)

forecast.solar shows the identical error (7096 vs 23096 Wh for 2026-09-10 at `az=153` vs `az=−27`),
so *all five* ensemble members are wrong the same way, and taking the median does not help.

**Why it costs money.** An under-forecast of solar makes the DP:
- grid-charge energy the sun would have delivered free (paid at ~85–110 öre instead of 0);
- reserve too little headroom, so real PV arrives at a full battery and is exported at
  `spot + 1.5 öre` instead of displacing an 85–250 öre import;
- under-scale the solar risk premium (`lib/optimizer.ts:351-354` reads the same understated
  forecast), so the premium fails exactly when it should fire;
- under-state `planned_surplus_kwh` in `check_solar_funding`
  (`scripts/services/dispatch_loop.py:674`), so forced charges proceed on grid money.

**The compensating fudge stack this created — must be removed in the same change:**
- `SOLAR_FORECAST_BIAS_FACTOR = 1.75` (`lib/constants.ts:246`), applied in
  `calibrateForecastKwh()` to every *live* forecast but **not** to the climatology fallback
  (`getSolarProfileByMonth`). A flat 1.75 against a needed 1.5–6.2 is only right in June–July.
- `solarCalibrationByMonth = [1,1,1,1,1,1,1,1.4,1.4,1,1,1]` (`lib/consumption-data.ts:29`).
  `lib/constants.ts` claims it was "reset to uniform 1.0 (no production history yet)" — it is
  not; Aug/Sep were hand-set to 1.4. That is a 40 % step on 1 Aug and 1 Oct, and it is applied in
  `parseOpenMeteo`/`parseOpenMeteoMinutely` but **not** in `parseForecastSolar`, so ensemble
  members disagree by 40 % for two months of the year.
- The intraday nowcast (`lib/solar-nowcast.ts`) is clamped at `MAX_RATIO = 1.5` (line 8). It
  cannot correct a 2–6× under-forecast even when it measures one, and it only touches the current
  day's next 6 h.
- `lib/irradiance-data.ts` (the climatology fallback) was generated **at the old Göteborg
  coordinates** — its own header says `@ 57.64, 11.78, azimuth 153` — contradicting
  `lib/constants.ts`'s claim that it was regenerated for this site. It carries both the wrong
  location and the wrong azimuth, and gets no bias factor at all.

**Fix.**
1. Convert at the boundary, once. Keep `azimuthDeg` in the documented 0 = N convention (the
   DOMAIN.md tables use it) and add a single exported helper, e.g.
   `export const apiAzimuth = (a: number) => ((a - 180 + 540) % 360) - 180;` in `lib/solar.ts`,
   used by `lib/forecast.ts` (all four URLs), `lib/metno-thredds.ts` if it takes an azimuth, and
   `scripts/tools/build-irradiance-openmeteo.mjs`. Add a unit test asserting
   `apiAzimuth(153) === -27`, `apiAzimuth(180) === 0`, `apiAzimuth(123) === -57`,
   `apiAzimuth(303) === 123`.
2. Regenerate `lib/irradiance-data.ts` with `scripts/tools/build-irradiance-openmeteo.mjs` at the
   *site's* coordinates and the corrected azimuth. Confirm the header records 62.4396/17.3611.
3. Set `SOLAR_FORECAST_BIAS_FACTOR` back to **1.0** and `solarCalibrationByMonth` to twelve
   **1.0**s in the same commit — they exist only to mask P0-1 and will double-count once it is
   fixed. Then re-derive both honestly from telemetry (see M-2) after a few weeks.
4. Apply `solarCalibrationByMonth` in `parseForecastSolar` too, or (preferred) apply it in exactly
   one place after `combineSolarForecasts` so no member can be missed again.
5. Raise `MAX_RATIO` to ~2.0 and make the clamp symmetric with `MIN_RATIO`, but only *after*
   step 1 — with the root cause fixed the ratio should sit near 1.0 and a wide clamp is then a
   genuine sanity bound rather than a load-bearing correction.
6. `lib/forecast.ts:7` uses only `SOLAR_ARRAYS[0]` for tilt/azimuth/kWp while `ghiToKwh()` sums
   *all* arrays' kWp×PR. Harmless for this single-plane site, wrong for the reference 3-array
   site. Either fetch per array and sum, or assert `SOLAR_ARRAYS.length === 1` where the
   single-plane assumption is made.

**Watch out.** `parseOpenMeteo`'s one-hour label shift is **correct** — verified: the `az=0` peak
lands on label 13:00 and true solar noon at 17.36 E is 12:49 CEST, i.e. Open-Meteo's value at
label *T* is the mean of the *preceding* hour. Do not touch it.

**Tests.** `lib/__tests__/forecast.test.ts` will need the new URLs. Add a regression test that
pins the API azimuth for a documented-convention input.

## P0-1b The MET Norway fallback feeds horizontal irradiance into a panel-plane model

> **Status: ✅ done** (found while fixing P0-1, in the same code area).

**What was wrong.** `lib/metno-thredds.ts` — the second-tier solar fallback used when Open-Meteo
itself errors — reads MET Nordic's
`integral_of_surface_downwelling_shortwave_flux_in_air_wrt_time`, which is **horizontal** (GHI),
and passed it straight into `ghiToKwh()`, which takes **panel-plane** irradiance. A 45° plane at
62.4 N collects ~1.0× GHI in June but **~3.3× in December**, so the fallback under-forecast by the
whole tilt gain, worst in the season where solar matters most to the plan. It also skipped
`calibrateForecastKwh`, so it silently ignored `SOLAR_FORECAST_BIAS_FACTOR` that every other
source honours.

**Fix shipped.** `build-irradiance-openmeteo.mjs` now requests `shortwave_radiation` alongside
`global_tilted_irradiance` in the *same* ERA5 call (no extra round-trips) and emits
`poaOverGhiByMonth` — the monthly plane/horizontal ratio, accumulated as sum-over-sum so
negligible twilight hours can't dominate. Measured at this site:
`[2.677, 1.934, 1.53, 1.238, 1.062, 0.993, 1.027, 1.158, 1.393, 1.84, 2.429, 3.323]`.
`fetchSolarForecastDirect` transposes with it and applies `calibrateForecastKwh`. Monthly (not
hourly) resolution on purpose: this is an outage fallback, and an hour-of-day ratio would be false
precision against a cumulative-integral source. `metno-thredds.test.ts`'s expectation helper now
mirrors the transposition, plus a standalone assertion that the result exceeds the untransposed
value so a future refactor can't drop it again.

## P0-2 The DP conflates grid capacity with battery power, so house load steals charging headroom

> **Status: ✅ done.** `SLOT_MAX_KWH` is now two constants: `BATTERY_SLOT_KWH = BATTERY_MAX_KW / 4`
> (throughput: `maxDelta`, `autoChargeInputKwh`) and `GRID_SLOT_KWH = GRID_KW / 4` (import and
> export feasibility, and the export curtailment clamp) — in `lib/optimizer.ts`, and the same split
> in `lib/oracle.ts`, where the combined value had been capping the **no-battery baseline's solar
> export** at the battery's rating, a quantity that baseline has no battery for. `oracle_days`'s
> `params_json` now records `gridSlotKwh` + `batterySlotKwh` instead of one `slotMaxKwh` (nothing
> reads it; it is diagnostic metadata).
>
> Added the guard the plan asked for: `dpCore` throws if `round(BATTERY_SLOT_KWH · η / step)`
> exceeds `maxDelta`, which is the condition under which a bright slot would have no legal
> non-discharging successor state and the DP would be forced into a discharge it never chose.
>
> New file `lib/__tests__/optimizer-caps.test.ts` covers both hardware shapes — neither is
> observable with the shipped defaults, where the two ratings coincide for grid flows, so each case
> stubs the env and dynamically re-imports (same pattern `forecast.test.ts` uses). It pins: house
> load no longer eating the charging budget (2.4+ kWh imported in the cheap slot vs ~1.86 before,
> 1.4+ kWh of it into the battery vs ~0.86); the battery's own rating still binding when the grid
> has room; the mirror case (a 15.36 kW battery behind an 11 kW connection now absorbs solar at
> 3.84 kWh/slot instead of being throttled to 2.75 and exporting the difference); and export still
> bounded by the grid. `optimizer.test.ts` and `optimizer-property.test.ts` updated to assert
> against whichever of the two caps each property actually belongs to.
>
> **Note for E3:** `GRID_SLOT_KWH` is now load-bearing for fuse safety, not just cost — the DP will
> genuinely plan to fill the connection. `SOLINTEG_GRID_CAP_W=10000` must be verified against the
> real main fuse before this runs armed, and E3's inverter-side import limit is the backstop.

**What is wrong.** `lib/optimizer.ts:13-14`:

```ts
const SLOT_MAX_KW = Math.min(GRID_KW, BATTERY_MAX_KW); // 11 kW — grid is binding
const SLOT_MAX_KWH = SLOT_MAX_KW / 4;
```

That single number is then used as **all four** of: the battery's per-slot throughput limit
(`maxDelta`, line 268; `autoChargeInputKwh`, line 286), the grid *import* limit (lines 150, 382),
the grid *export* limit (lines 132-135, 370, 377). At this site `BATTERY_MAX_KW = 7.5` <
`GRID_KW = 10`, so grid import is capped at 7.5 kW instead of 10 kW — and `gImp` includes house
load (`gImp = gridToBattery + lr`). Every kW the house consumes is subtracted from the charging
budget even though the grid connection has room.

**[measured] Cost.** Winter scenario: 3 h at spot 2 öre, then 120 öre for the rest of the day,
flat 4 kW house load, battery starting at the floor.

- DP banks **10.16 kWh** during the cheap window.
- Physically possible: (10 − 4) kW × 3 h × √η = 17.6 kWh, capped by headroom 13.55 kWh ⇒
  **13.55 kWh**.
- Foregone 3.39 kWh, which is then bought at 233 instead of 85 öre ⇒ **≈5 kr on that one day**.

The error scales with house load, so it is worst on exactly the cold days when the arbitrage
matters most.

**Fix.** Split the constants:

```ts
const BATTERY_SLOT_KWH = BATTERY_MAX_KW / 4;   // battery throughput limit
const GRID_SLOT_KWH    = GRID_KW / 4;          // import AND export limit at the meter
```

- `maxDelta`, `autoChargeInputKwh` ⇒ `BATTERY_SLOT_KWH`
- `gridImport`/`gridExport` feasibility and the export curtailment clamp ⇒ `GRID_SLOT_KWH`
- keep the existing `naturalFloor` rounding guard: verify
  `Math.round(BATTERY_SLOT_KWH * ONE_WAY_EFF / step) <= maxDelta` still holds after the change
  (it does for these constants; add an assertion or a test so a future constant change cannot
  make the auto-charge floor unreachable and force a spurious discharge).

**Tests to change.** `optimizer.test.ts` `'|gridKwh| never exceeds SLOT_MAX_KWH per slot'`,
`'caps solar export at SLOT_MAX_KWH'`, `'discharges at a meaningful rate — a full-power discharge
slot moves SoC by ~SLOT_MAX'`. Export the two new constants for the tests instead of re-deriving
them there.

**Also verify (config, not code).** `SOLINTEG_GRID_CAP_W=10000` must be the *main fuse* rating
minus margin, not a guess — with P0-2 fixed this value becomes load-bearing for both cost and
fuse safety. See E3.

## P0-3 Terminal value 0 — every plan ends at the SoC floor, and the horizon can silently collapse to today

> **Status: ✅ both parts done.** Part 1 (terminal value) shipped together with P1-3, since the
> deferral guard has to price the terminal credit too.
>
> `terminalEnergyValueOre(slots)` = `min(buyPrice) × η − wear`, floored at 0 — a *derived* figure
> that moves with the plan's own prices, not a new tuning constant, passed to the DP as
> `opts.terminalValueOrePerKwh` (0/omitted is bit-identical to the old behaviour, which the oracle
> and every existing test rely on; `opts.endSoc` still overrides it completely). `lib/plan.ts`
> computes it per plan and scales it by `TERMINAL_VALUE_FACTOR` (env
> `SOLINTEG_TERMINAL_VALUE_FACTOR`, default 1, 0 = kill switch).
>
> It is deliberately the provably-safe **lower bound**, and the proof is the point:
>   - *cannot induce hoarding by buying* — storing a kWh from the cheapest slot costs `minBuy / η`
>     while the credit is `minBuy × η − wear`, and η < 1;
>   - *cannot induce dumping* — carrying beats exporting by roughly the ~80 öre VAT+skatt wedge.
>
> A first attempt floored the value at the horizon's best **sell** price ("energy is worth at least
> what exporting fetches"). Measurement killed it: on a synthetic winter horizon that credited
> 134.5 öre/kWh against a 108 öre cheapest import, and the DP promptly filled the battery from the
> grid for ~15 kr of imaginary value. The floor is gone and there is now a test pinning that sell
> prices cannot influence the credit however high they get.
>
> **Honest limitation, documented in the code:** because the credit sits below every buy price in the
> horizon by construction, it cannot stop the plan spending the battery on tonight's cheap load
> instead of tomorrow morning's dear load. What it does fix is the free give-away — a battery holding
> more energy than the remaining load can absorb used to export the surplus at whatever the last
> slots paid, because leftover energy scored zero. Going further needs a credit *above* the cheapest
> slot, i.e. trading the proof for an assumption about unseen prices; that is a Phase M (replay
> data) decision, not a taste decision.
>
> `slotsInStockholmDay()` now lives in `lib/prices.ts` (the module that owns
> `stockholmMidnightUtc`) and derives a date's real slot count from the two midnight instants: 96
> ordinarily, 92 spring-forward, 100 fall-back. `trimIncompleteTerminalDays` accepts a future day
> against *that* instead of a hardcoded 96, so the two DST days no longer truncate the horizon twice
> a year (G7), and it now also checks **contiguity** — a day with the right count but a hole is
> rejected, because `dispatch_loop.py` indexes the published plan by elapsed time from its first
> slot and a mid-plan gap would shift the executor's idea of "now" for everything after it. The
> fall-back day's genuinely repeated label (a −45 min step) is tolerated explicitly.
>
> Holes are now filled *upstream* instead of costing a day of horizon: `fillPriceGaps` in
> `lib/prices.ts` reconstructs runs of up to four missing slots by linear interpolation, but only
> when both neighbours are on the same ordinary 96-slot date — never across midnight, never on a DST
> date (01:45 → 03:00 looks exactly like four missing slots to label arithmetic). It interpolates the
> raw **spot** and re-derives both price fields from it, so DOMAIN.md's
> `sell = spot + EXPORT_BONUS_ORE` / `buy = (spot + surcharge) × (1 + VAT)` invariants hold exactly
> for a filled slot, and marks it `source: 'forecast'` — inferred, never presented as settled. A
> longer gap is deliberately left alone: a real outage should shorten the horizon, not be papered
> over with invented prices. Filling is logged at warn level.
>
> `producePlan` now logs the horizon it actually solved (slot count, hours, first/last slot,
> `hasTomorrow`) on every plan, with an explicit "SHORT: terminal-value-0 will drain the battery at
> its end" note below 24 h. Horizon length is a policy input for a DP with no terminal value, and it
> was previously invisible.
>
> Tests: new `lib/__tests__/horizon.test.ts` (day lengths incl. both DST days and month/year
> boundaries; full 92- and 100-slot days accepted; a same-count-but-holed day rejected; stops at the
> first incomplete day; today never dropped) and a `fillPriceGaps` block in `prices.test.ts` (single
> slot, a four-slot run, >1 h refused, midnight refused, both DST label shapes refused, identity
> when nothing to fill). `plan.test.ts`'s `../prices` mock became a partial mock — it was stubbing
> away the real date arithmetic `horizon.ts` needs.

### The original finding


**What is wrong.** `lib/optimizer.ts:309` starts the backward pass with `costToGo` all zeros:
stored energy is worth exactly nothing at the horizon end. **[measured]** every scenario run
during this audit — summer, winter, 1/2/3-day horizons — ended at exactly `MIN_SOC`. That is not
a bug in the DP; it is the DP correctly answering a question nobody wants answered.

It is tolerable *only* while the horizon reliably extends 2–3 days past now. That reliability is
thin:

- `fetchPricesUncached` merges PlaneraElen forecast rows to now+72 h, and
  `trimIncompleteTerminalDays` (`lib/horizon.ts`) keeps a future day **only if it has exactly 96
  slots**. One missing forecast slot, an hourly-resolution response, a PlaneraElen outage
  (caught and warned at `lib/prices.ts:344`), or a DST-transition day (92/100 slots) drops that
  day and everything after it.
- With tomorrow's day-ahead not yet published (before ~13:00) and PlaneraElen unavailable, the
  horizon *is* today — and the plan then deliberately empties the battery by midnight.

**Fix (two independent parts; do both).**
1. **Terminal value.** Give leftover energy a defensible marginal value instead of zero. Cheapest
   correct-enough form: `costToGo[level] = -(socOf(level) - MIN_SOC_KWH) * ONE_WAY_EFF * vTerm`,
   with `vTerm` a conservative estimate of what that kWh displaces after the horizon — e.g. the
   *20th percentile* of `buyPrice` over the horizon's last 24 h, minus wear, floored at the
   period's max `sellPrice` (energy is never worth less than exporting it). Expose it as
   `TERMINAL_ENERGY_VALUE_MODE` / `TERMINAL_VALUE_PERCENTILE` in `lib/constants.ts` with a
   `0`/off setting that reproduces today's behaviour bit-identically.
   **Hard constraint:** `opts.endSoc` must keep overriding it completely — `lib/oracle.ts`
   depends on the terminal-constrained run, and the regret ≥ 0 invariant must not move. Add a
   test asserting the oracle path is unchanged.
2. **Horizon robustness.** `trimIncompleteTerminalDays` should accept a day whose slot count
   matches its *real* length (92/96/100 by DST) and should tolerate a small number of missing
   forecast slots by interpolation rather than dropping a whole day. Log (once per plan) the
   horizon length actually used, so a collapse to today-only is visible in `journalctl` instead
   of silently changing dispatch policy.

---

# P1 — Systematic bias from the three risk knobs

All three are planning-only surcharges set solely by `lib/plan.ts:184-190`. Individually each has
a documented rationale; together they are unvalidated, horizon-dependent and partly contradictory.

## P1-1 `FORECAST_RISK_PREMIUM_ORE` inflates the value of hoarding energy into forecast days

> **Status: ✅ done.** `planningPrices` is gone. The premium is now a per-kWh surcharge on the
> battery's **grid-facing energy** in a forecast slot (`forecastRiskOre`), charged on
> `gridToBattery` and on `batteryToGrid` exactly like the deferral bias, while `buyPrice`/`sellPrice`
> stay at the honest point forecast. Load-covering discharges are untouched — same reasoning as the
> deferral bias, their timing is dictated by when the load happens.
>
> That is the structural fix: the DP uses `buyPrice` for two jobs, costing an import *and* — through
> the cost-to-go — valuing the energy already stored, whose worth is the import it will avoid. Shifting
> the price level could not discourage the transaction without simultaneously inflating the state.
> Re-measured after the change on the same two-day scenario that exposed it: the plan now ends
> midnight at 5.74 / 7.15 kWh (identical to the honest optimum) instead of being forced to 15.05 kWh
> (100 %), and true value is identical instead of slightly worse. It still suppresses grid-funded
> charging inside the forecast day, which is what it is for.
>
> **Band captured, not yet consumed.** `PriceSlot.uncertaintyOre` / `OptimizerSlot.priceUncertaintyOre`
> now carry PlaneraElen's own per-slot `low`/`high` band as a buy-side half-width (VAT-scaled), through
> `buildOptimizerSlots` and into `optimizer_runs.inputs_json`. The DP does **not** read it, on purpose:
> the raw band reaches ±97 öre/kWh three days out, so using it directly as a surcharge would forbid
> every forecast-slot transaction. Relating band width to *realised* forecast error is a Phase M refit,
> and this field is the data collection for it. A malformed or one-sided band degrades to "unknown"
> rather than to a negative surcharge.
>
> Tests: the premium may not make the plan carry more into a forecast day than the honest optimum, and
> must still not increase grid-funded charging there; settled-price slots stay bit-identical; and the
> band derivation covers present/absent/one-sided/inverted.

**What is wrong.** `planningPrices` (`lib/optimizer.ts:211`) adds +10 öre to `buyPrice` and
subtracts 10 öre from `sellPrice` on every `priceSource === 'forecast'` slot. That is the right
shape for discouraging *transactions* in an uncertain slot, but the DP's value of *stored energy*
is the avoided buy price. Raising `buyPrice` on forecast days therefore makes future load coverage
look **more** valuable than it really is, i.e. it is a systematic incentive to hoard and to buy
today for an inflated tomorrow.

**[measured]** Two-day horizon, day 1 = actual prices with a cheap night, day 2 = forecast with a
morning peak, no solar, load 30 kWh/day:

| day-2 peak spot | premium | SoC at 24:00 | true value |
|---|---|---|---|
| 40 öre | 0 | 5.74 kWh | −66.16 kr |
| 40 | 10 | **15.05 kWh (100 %)** | −66.24 kr |
| 60 | 0 | 7.15 | −66.05 |
| 60 | 10 | **15.05** | −66.10 |
| 90 | 0 | 7.15 | −65.93 |
| 90 | 10 | **15.05** | −65.98 |

The premium parks the battery at 100 % overnight in every case and makes real value *worse*. The
100 % parking is a second-order harm: it is the worst SoC for calendar ageing and it leaves zero
headroom for the next morning's PV.

**Fix.** Do not haircut the price level; haircut the *spread*, and only for transactions. Options
in ascending quality:
1. Shrink both prices toward their own midpoint on forecast slots
   (`buy -= k·(buy-mid)`, `sell += k·(mid-sell)`) so the arbitrage edge narrows without changing
   the level of the state value.
2. Better: use the uncertainty band the price source already returns. PlaneraElen's payload
   carries `low`, `high`, `very_low`, `very_high` per slot (verified live, 672 rows / 7 days) and
   `lib/prices.ts:33-38` throws them away. Plan buys against a pessimistic quantile and sells
   against a pessimistic quantile, per slot, instead of a flat 10 öre everywhere.
3. Best: 3-scenario DP (low/mid/high price paths) with a shared first-stage decision.

Until one of those lands, `SOLINTEG_FORECAST_RISK_PREMIUM_ORE=0` is the better setting than 10.

## P1-2 The deferral ramp scales with horizon length; it was sized for a one-day horizon

> **Status: ✅ done.** The ramp is now measured from **now** and bounded:
> `deferOre[i] = rate × max(0, DEFERRAL_HORIZON_HOURS − hoursFromNow(i))`, zero beyond the window.
> Measuring from now is what makes it horizon-independent — the plan's slot 0 *is* now, because
> `lib/plan.ts` slices the horizon at the current slot before solving, so the surcharge is a property
> of the decision instead of a property of how many days the price feed happened to return.
>
> New constant `DEFERRAL_HORIZON_HOURS` (env `SOLINTEG_DEFERRAL_HORIZON_H`, default 12 h), chosen to
> span the two shapes the bias exists for: deferring a charge a few hours to see whether the solar
> arrives, and deferring a sell from an evening peak to a near-equal next-morning one. At the default
> rate the maximum level is 4.8 öre/kWh — enough to break a near-tie, an order of magnitude below any
> real daily spread, which is the claim the feature always made for itself.
>
> All five existing risk-aware tests pass unchanged (for a horizon inside the window the two forms
> have the same slope, so the intended behaviour was never the problem — only its level). Two new
> tests pin what actually broke: appending two neutral days to a scenario must change neither the
> timing decision **nor the quantity** bought (the old form taxed the trade itself), and the
> surcharge must stop discriminating beyond its window.
>
> The magnitude is still a placed estimate, not a fit — but the feature now survives its own
> sacrifice guard instead of being rejected on every plan, so it can actually be measured.

**What is wrong.** `deferOre[i] = deferralRate * 0.25 * (n - 1 - i)` (`lib/optimizer.ts:304`) is
unbounded in `n`, and `n` grew when the horizon was extended to 3 days.

**[measured]** surcharge on a grid-funded charge in the first slot of the horizon:

| horizon | slots | surcharge at slot 0 |
|---|---|---|
| 1 day | 96 | 9.5 öre/kWh |
| 2 days | 192 | 19.1 |
| 3 days | 288 | **28.7** |

Scale check against real prices (SE2, fetched 2026-09-10, EUR/SEK ≈ 11): the buy-side spread
within a day is 64–96 öre/kWh (2026-09-10: 88–165, 09-11: 90–186, 09-12: 97–161). The *net*
margin on a night-buy/peak-use round trip is therefore roughly
`165·0.96 − 88 − 8.3 (wear)` ≈ 62 öre/kWh. A 28.7 öre deferral surcharge plus a 20 öre solar
premium (P1-4) consumes ~78 % of that. These knobs are not öre-scale nudges at a 3-day horizon;
they are the dominant term in the charge decision.

**Consequence [measured].** Shoulder-season scenario (8 kWh solar, 20 kWh load, spot 5→40), 3-day
horizon: the deferred plan gives up **152.6 öre** of real value, against
`MAX_DEFERRAL_SACRIFICE_ORE = 50`. The two-pass guard therefore throws the deferred plan away
entirely — every plan, all season. The feature's cost (two full DP solves per plan) is paid; its
benefit (option value from re-decidable actions) is not delivered.

**Fix.** Make the ramp horizon-independent and bounded:
`deferOre[i] = rate * min(hoursUntil(i), DEFERRAL_HORIZON_CAP_H)` with the cap ≈ the replan
interval times a small factor (2–6 h is the interval over which "the next replan can cancel it"
is actually true). Then re-fit `rate` against the oracle (M-1), not by inspection.

## P1-3 The deferral sacrifice guard measures the wrong problem

> **Status: ✅ done.** `evaluateDispatch` takes an options bag (`loadFactor`,
> `terminalValueOrePerKwh`) whose defaults reproduce the previous behaviour exactly, and the deferral
> guard now prices both trajectories on the problem they were actually solved against. Tests pin:
> defaults identical to the old two-argument call; each plan optimal under *its own* metric
> (margined plan wins the margined comparison, unmargined wins the unmargined one); the
> optimum-dominates property holding under a load factor, not just at 1; and the terminal credit
> being a credit on `valueOre` and not on `cashOre`.

**What is wrong.** `lib/optimizer.ts:236-242` prices both trajectories with
`evaluateDispatch(slots, …)`, and `evaluateDispatch` (line 506) reads `slots[i].consumptionKwh`
**unmargined**, while both trajectories were solved against `consumptionKwh × loadFactor` (1.15).
The guard therefore evaluates two plans on a load they were not planned for, and neither plan is
optimal under that metric — so the difference is not a "sacrifice" at all.

**[measured]** same shoulder scenario, 3-day horizon:
- guard's own number (unmargined pricing): **152.6 öre**
- the same comparison on the problem actually solved (margined): **196.4 öre**

a 44 öre discrepancy against a 50 öre cap. The verdict happened to agree here; it need not.
In another scenario the audit measured *negative* "sacrifice" for the same reason — i.e. the
guard can conclude a biased plan is better than the optimum it was compared against.

**Fix.** Give `evaluateDispatch` an explicit `loadFactor` (default 1, so the oracle and existing
tests stay bit-identical) and have the guard pass the same factor the DP used. Add a test:
`evaluateDispatch(slots, dpCore(slots, s, {loadFactor: f}), s, {loadFactor: f})` must be ≥ the
value of any other feasible trajectory under the same factor.

## P1-4 The solar risk premium sums solar over the whole horizon, not the rest of the day

> **Status: ✅ done, both halves.**
>
> **Windowed.** The premium's numerator is now forecast solar within `SOLAR_RISK_LOOKAHEAD_HOURS`
> (env `SOLINTEG_SOLAR_RISK_LOOKAHEAD_H`, default 12 h) after the slot, computed as a difference of
> the existing suffix sums so it stays O(1) per state. 12 h covers the rest of a daylight day, which
> is the horizon over which solar can plausibly arrive and make a charge redundant before the stored
> energy is needed; beyond that the buy is not competing with the same solar any more.
>
> **Guarded.** The two-pass sacrifice guard ran for the deferral bias only, which left the largest
> knob of the three (20 öre/kWh) able to give up unbounded real value with nothing measuring it. It
> now runs whenever **any** risk knob is active and compares "all knobs off" against "as configured",
> so one cap bounds the whole set. `MAX_DEFERRAL_SACRIFICE_ORE` → `MAX_RISK_SACRIFICE_ORE`, env
> `SOLINTEG_RISK_MAX_SACRIFICE_ORE` with the old `SOLINTEG_DEFERRAL_MAX_SACRIFICE_ORE` still read as
> a fallback so an existing deployment's setting keeps working; the opt is `maxRiskSacrificeOre`.
>
> Tests: a distant sunny day (past the lookahead) must not change a buy that no solar can make
> redundant before its peak — the saturation regression; and an absurd premium killing a
> far-more-valuable trade must be rejected by the guard, with the cap lifted to prove the guard is
> what decided. The existing "waits out the forecast solar" and "winter night bit-identical" tests
> pass unchanged.

**What is wrong.** `suffixSolarKwh[i+1]` (`lib/optimizer.ts:302-305, 351-354`) is the raw forecast
solar over *all* remaining slots — up to three days — divided by the battery's *current* headroom.
The constant's own docstring describes "the remaining day's forecast solar". With a 3-day horizon
the ratio saturates at 1 for any non-winter plan, so the premium becomes a flat 20 öre/kWh on
every grid-funded charge, at every hour, from March to October — a hard block on shoulder-season
arbitrage rather than the risk-scaled surcharge it is documented to be.

Note also that the premium is **not** covered by the sacrifice guard: `optimizeDispatch` runs both
passes with `solarRiskPremiumOre` intact, so unlike the deferral bias its value give-up is
unbounded and unmeasured.

**Fix.** Restrict the numerator to solar within the same *local day* as slot `i` (or a
configurable `SOLAR_RISK_LOOKAHEAD_H`, default 12), and extend the two-pass true-price sacrifice
guard to cover the premium as well (one guard over the sum of all planning surcharges, one cap).

## P1-5 Round-trip efficiency is a DC datasheet number used on the AC path

> **Status: ✅ done.** The DP's per-leg efficiency is now `EFF_ONE_WAY = √BATTERY_RT_EFF ×
> INVERTER_CONV_EFF`, exported from `lib/constants.ts` as the single definition and imported
> everywhere — `lib/optimizer.ts`, `dispatch_loop.py`'s `ONE_WAY_EFF`, and the four test files that
> each used to re-derive `Math.sqrt(BATTERY_RT_EFF)` themselves. That repetition is how the DC/AC
> confusion survived, so removing it is part of the fix.
>
> `INVERTER_CONV_EFF` defaults to **0.97** — this inverter family's datasheet class (peak ~97.5 %,
> EU-weighted ~97 %), a documented hardware figure rather than a fitted fudge — giving an AC-to-AC
> round trip of ≈0.903 against the 0.96 previously claimed. `BATTERY_RT_EFF` keeps its datasheet
> meaning (battery-terminal DC) and its docstring now says outright that it is not what the optimizer
> needs on its own. The cross-language test gained a guard on the new default *and* on the derived
> expression, since the executor's solar-funding and battery-save arithmetic has to agree with the
> plan it is executing.
>
> `scripts/tools/measure-battery-rt-eff.py` now reports **both** figures: the existing DC round trip
> from `battery_w`, and an AC-to-AC one from `inverter_ac_w` restricted to samples with
> `pv_w ≤ --pv-idle-w` (with the sun up, the inverter's AC port carries PV too and the two cannot be
> separated from this series — night windows are the grid-arbitrage windows anyway). It then backs out
> the implied per-leg `INVERTER_CONV_EFF = √(η_ac / η_dc)` and refuses to report one when the two
> figures come out physically impossible. Smoke-tested against a synthetic telemetry DB built with a
> known 0.94 conversion efficiency: the script recovered 0.942.
>
> **The correction has teeth**, which the test suite demonstrated immediately: the risk-premium test's
> scenario (buy 86 → sell 98) was written as "+3.5 öre/kWh after losses and wear" under the old
> efficiency and is a **2 öre loss** under the corrected one, so the plain DP now refuses that trade.
> The scenario was widened (sell 110 → ≈+9 öre) so the test still exercises the premium rather than
> the arithmetic of a trade that no longer exists. Two "should be zero" flow tolerances were re-tied
> to the DP's own discretisation step instead of magic numbers, and the oracle's energy-balance
> assertion re-expressed as a fraction of throughput — that residual *is* the conversion loss, so its
> absolute size necessarily moved.
>
> **Still open:** the 0.97 default is a datasheet figure, not this install's measurement. Run the
> script once there is night-time cycling in telemetry and set `SOLINTEG_INVERTER_CONV_EFF`.

**What is wrong.** `BATTERY_RT_EFF = 0.96` ("datasheet: ≥96 %"), and the DP uses
`ONE_WAY_EFF = √0.96` to convert **grid AC kWh** into stored kWh (`need = dE / η`) and back
(`out = −dE · η`). The inverter's own conversion losses (AC→DC on charge, DC→AC on discharge,
typically 3–6 % per leg) are therefore missing. A realistic AC-to-AC round trip for a hybrid
inverter is 0.85–0.90.

At a 90 öre buy price the modelling error is ≈8 öre per delivered kWh — larger than
`BATTERY_WEAR_COST_ORE_PER_KWH` (≈4.2 öre/leg at this battery size and the default 60 000 SEK replacement cost) and enough to flip marginal
arbitrage and marginal solar-storage decisions the wrong way.

`scripts/tools/measure-battery-rt-eff.py` does not close this: it integrates `battery_w`, i.e. it
measures the **battery-terminal** round trip, which is the 0.96 figure, not the path the DP prices.

**Fix.** Add an AC-to-AC measurement to that script (integrate `grid_w`/`inverter_ac_w` over
clean grid-charge and grid-discharge windows against the `soc_kwh` delta) and set
`SOLINTEG_BATTERY_RT_EFF` to the measured AC round trip, with the docstring updated to say
explicitly that this constant means *AC-to-AC* because that is how the optimizer uses it. If the
two are wanted separately, introduce `INVERTER_CONV_EFF` and use `√(BATTERY_RT_EFF) ·
INVERTER_CONV_EFF` per leg.

---

# P2 — Plan/executor mismatches

## E1 `check_solar_funding` re-derives the plan's funding split, and gets it wrong

> **Status: ✅ done.** New `planned_grid_charge_kwh()` reads `DispatchSlot.gridToBatteryKwh` — the
> plan's own attribution, computed with the same load margin as the trajectory — and falls back to
> the old forecast recomputation only for a plan row predating the field, so an in-flight upgrade
> keeps working. The value is clamped to the charge itself so a plan whose attribution disagrees with
> its own SoC trajectory cannot produce a negative solar share. Which source was used is named in the
> logged `detail` (`per plan` / `per forecast`), so the guard's own history stays interpretable.
> Tests cover the divergence directly: inputs claiming a fully solar-funded charge against a plan that
> says grid-funded, with zero live surplus — no skip with the attribution, skip without it.

`scripts/services/dispatch_loop.py:674-675` recomputes
`planned_surplus = solarKwh − consumptionKwh` from `inputs_json`, which holds the **honest,
unmargined** load — while the DP netted against `consumptionKwh × 1.15`. So the guard's
`planned_grid_kwh` is smaller than what the DP actually planned to buy, `shortfall` is
overstated, and genuine planned charges get vetoed as `skipped_solar_shortfall` (each veto also
firing a replan request).

**Fix.** Don't re-derive it. The plan already publishes the authoritative attribution:
`DispatchSlot.gridToBatteryKwh` is exactly "grid energy this slot's charge is funded from",
computed with the same margin as the trajectory. Use `dispatch[idx]["gridToBatteryKwh"]` as
`planned_grid_kwh`, falling back to the current computation only when the field is absent (older
plans). Keep the conservative `surplus_w is None` branch. Update
`scripts/tests/test_dispatch_loop.py`.

## E2 A forced charge has no live-load correction — only an all-or-nothing veto

Discharge slots get `live_discharge_power_w` (retargeted every tick against real `pv_w` /
`house_load_w`). Charge slots get only the binary `check_solar_funding` veto: force at the plan's
full power, or fall back to Auto. Two consequences:

- **Cost.** When live solar is *above* forecast during a forced charge, the fixed 50207 setpoint
  does not rise to absorb it, so the extra surplus exports at `spot + 1.5` instead of charging.
- **Import risk.** When live load is *above* forecast, grid import is `charge_power + load − pv`
  with nothing tracking it (see E3).

**Fix.** Mirror the discharge logic: compute the charge power that holds the plan's intended net
grid position, `required_w = planned_grid_w + pv_w − house_load_w`, clamp to
`[0, clamp_power_w(...)]`, apply the same `LIVE_LOAD_ROUND_W`/`LIVE_LOAD_DEADBAND_W` treatment,
and demote to Auto at 0 W exactly as the discharge path does. Keep the existing veto as the outer
bound. Gate it behind its own env kill switch (`DISPATCH_LIVE_LOAD_TRACKING_CHARGE`, default on
after validation in shadow mode).

## E3 No site-level import limit is ever set, although the register exists

> **Status: ✅ implemented, deliberately off by default.** `set_import_limit()` /
> `clear_import_limit()` in `inverter_control.py` write 50009 (value) **then** 50007 (switch) — that
> order matters, since enabling first would briefly enforce whatever an earlier session left in the
> value register. `dispatch_loop.apply_import_limit()` applies it once at startup when
> `DISPATCH_IMPORT_LIMIT_W` is set, never reverts it on shutdown (a fuse-level bound belongs to the
> installation, not to one run of the controller), and swallows any failure at ERROR level: it is an
> *extra* bound on top of `clamp_power_w` and the planner's grid-cap check, so losing it must
> degrade to the protection that already existed, not to no controller.
> Also added: a `import-limit <W|off>` CLI verb, and
> `scripts/tools/probe_import_limit.py` — forces a grid-funded charge, squeezes the limit below what
> that charge needs, and reports ENFORCED / stored-but-not-enforced / wrong-address by reading the
> registers back *and* measuring meter power, because those are three different bugs.
> Tests: `test_inverter_control.ImportLimitTests` (write order, 0.1 kW rounding, range rejection,
> a value that rounds away to zero, clear-leaves-value, disarmed no-op) and
> `test_dispatch_loop.ApplyImportLimitTests` (off when unconfigured — not even a connection opened;
> applied once and closed; a failure cannot stop startup).
>
> **Why it ships off:** 50007/50009 are the only control registers in this repo taken from a
> third-party reverse-engineering doc rather than measured on the reference unit, and the realistic
> failure mode is *stored but not enforced* — precisely what `50209 = 0` turned out to do when
> written pre-mode. Enabling it without the probe would install a guard that looks present in the
> journal and does nothing. `MODBUS.md` and `deploy/solinteg.env.example` now say so, with fuse
> ratings for the common Swedish service sizes.
>
> **Operator action required:** run the probe, then set `DISPATCH_IMPORT_LIMIT_W` to the real
> main-fuse rating. Until then `SOLINTEG_GRID_CAP_W` should stay conservative, because after P0-2
> the planner genuinely fills it.


`MODBUS.md:154-155` documents `50007` (import-limit switch) and `50009` (import-limit value,
0.1 kW) with the explicit note *"enforce ~11 kW fuse cap → 110"*, and
`inverter_control.py`'s own comment states that `50208/50209` cap only the **inverter's** AC
exchange, not the meter — house load sits downstream. So during a forced charge the meter can see
`battery charge + house load`: 7.5 kW + 4 kW = 11.5 kW against a 10 kW configured cap. Nothing in
the stack prevents that today.

**Fix.** Write `50007 = 1`, `50009 = round(GRID_CAP_W/100)` once at service start (and re-assert
it in `restore_ac_limits`), after verifying the registers on this unit with a probe in the style
of `probe_50209_pv_only.py`. This is the hardware backstop that makes P0-2's raised import budget
safe. Document the probe result in MODBUS.md's verification table.

## E4 A plan spanning three days is invisible to the executor after midnight

> **Status: ✅ done.** `load_latest_plan(con, now)` takes the newest `PLAN_LOOKBACK_ROWS` (5) rows and
> returns the first whose slots actually cover `now`, instead of filtering on `price_date`. The
> lookback is deliberately shallow: if none of the last few plans covers now, the right answer is "no
> usable plan" — which already triggers a replan request — not digging through history. `price_date`
> stays on the row for indexing and analysis. Tests: yesterday's 23:50 plan used at 00:05; a newer
> non-covering row skipped in favour of an older covering one; nothing returned when none covers now;
> and the lookback window honoured rather than searched past.

`load_latest_plan` filters `WHERE price_date = ?` with today's Stockholm date
(`dispatch_loop.py:495`), and `logOptimizerRun` stores `price_date = data.today`. A plan computed
at 23:50 covers tomorrow in full, but at 00:00 the loop cannot see it: `decide()` returns
`no_plan` → Auto (self-use, which discharges the battery to cover load) until the next
render/replan lands. That is every day, at the hour when prices are usually at their lowest and
the plan most likely wanted a hold or a charge.

**Fix.** Select the newest plan whose slot range actually covers `now`: take the last few rows
(`ORDER BY logged_at DESC LIMIT 5`) and pick the first for which `slot_index_for_instant` returns
non-`None`. Keep `price_date` for indexing/analysis only. Add a test with a plan logged on day
*d* being used at `d+1 00:05`.

## E5 Hold-band mismatch: TS plans a hold that Python declines to execute

> **Status: ✅ done.** `is_planned_hold` now tests the plan's own marker (`batterySaveDriftKwh > 0`)
> plus the SoC floor; the load figure remains a *cap* inside `battery_save_target`, not a decision.
> `HOLD_LOAD_FROM_GRID_KWH` is gone. A missing marker still means no hold, so an older plan row keeps
> the safe idle → Auto behaviour. The regression test constructs exactly the 200-420 W band: a slot
> publishing `loadFromGridKwh` 0.045 with a 0.055 kWh planned drift, which the old threshold rejected.

TS decides a battery-save hold on `loadRem > 0.05` (pre-discharge load,
`lib/optimizer.ts:60,64`); Python's `is_planned_hold` tests the emitted
`loadFromGridKwh > 0.05` (`dispatch_loop.py:280,842`), which is *post*-discharge
(`lr − 0.055`). For house load between ~200 W and ~420 W the plan carries a hold and the executor
returns to Auto instead — and Auto discharges at whatever the house draws, which is the opposite
of a hold.

**Fix.** Have Python test the plan's own marker (`batterySaveDriftKwh > 0`) as the primary
condition — it is already read three lines later — and keep the load/SoC caps as clamps only.
Add a Python test for the 200–420 W band.

## E6 A hold is emulated with a 220 W discharge although a real freeze is documented

`BATTERY_SAVE_W = 220` keeps the inverter in EMS BattCtrl at a small discharge because parking at
0 W blocks self-consumption. But `MODBUS.md:211` documents a genuine freeze:
`50207 = 0` **plus** `50208 = 0` **plus** `50209 = 0` → battery untouched, grid supplies the
house. That is exactly the planned-hold case (night, load from grid, no solar), and it removes the
modelled 220 W leak instead of forecasting it. `restore_ac_limits()` already exists precisely to
keep such a restrictive cap from leaking.

**Fix.** Implement `freeze(inv)` in `inverter_control.py` behind its own env flag, verify on-device
(does self-consumption really stop? does the setpoint persist? does `restore_ac_limits` recover
cleanly?), and only then let `battery_save_target` prefer freeze over the 220 W emulation for
slots with no solar. Keep the emulation as fallback. Set `BATTERY_SAVE_FORECAST_DRAIN_W = 0` for
frozen slots so plan and execution agree.

## E7 `slot_power_w` ignores efficiency — **the audit had this backwards**

> **Status: ✅ done, but not as written.** The original finding was wrong and is corrected here rather
> than quietly dropped.
>
> `slot_power_w` is **correct as it stands**: register 50207 commands BATTERY-side power (confirmed
> on-device, MODBUS.md) and `socAfter` is battery-side energy, so the two are already in the same
> units and no efficiency factor belongs there in either direction. A comment now says so, because the
> audit re-derived it wrong once and the next reader will too.
>
> The real instance of this defect is in `live_discharge_power_w`, which the audit did not flag:
> `required_w = house_load_w − pv_w − planned_grid_w` is an **AC-side** figure — house load, PV and
> the plan's grid position are all measured at the AC port / the meter — and it was written straight
> to the battery-side register. Delivering it at AC needs `required_w / ONE_WAY_EFF` out of the cells,
> so the battery under-delivered by the conversion loss (~5 % once `EFF_ONE_WAY` started carrying the
> inverter's legs) and that shortfall leaked to the grid at full retail price — precisely what this
> tracking exists to prevent. Fixed, with `required_ac_w` now logged alongside the battery target so
> the two quantities are distinguishable in `control_actions`. The conversion is applied only to a
> positive need; a negative one means the surplus covers the house and the caller demotes the tick to
> idle/auto rather than scaling a negative number through an efficiency.

`dispatch_loop.py:565-568` commands `|ΔSoC| / 0.25` kW. `50207` is confirmed **battery-side**
(MODBUS.md), so this is right for charge (stored = commanded × 0.25) but ~2 % low for discharge,
where the plan's ΔSoC is the *stored* draw and the AC delivery is `ΔSoC · η`. Small, but free to
fix: divide by `ONE_WAY_EFF` on the discharge leg (the live-tracking path already computes an
AC-side target and is unaffected).

## E8 SoC ceiling is not modelled by the DP

`BATTERY_MAX_SOC_KWH` exists in `lib/constants.ts` but the DP plans to full `BATTERY_KWH`
(acknowledged in that comment), while `force_charge` refuses above `SOC_CEILING_PCT`. Not binding
at this site (`SOLINTEG_SOC_CEILING_PCT=100`), but it becomes a silent plan/execution divergence
the moment anyone lowers the ceiling. Fix by clamping the DP's top SoC level to
`BATTERY_MAX_SOC_KWH` when it is below capacity.

---

# P3 — Modelling gaps and hygiene

- **G1 No curtailment option.** In `computeFlows`' charge branch surplus above the export cap is
  silently curtailed, but *below* the cap the DP is forced to export everything it cannot store —
  including at a negative `sellPrice`. Real hardware can curtail (`50208`, or the inverter's
  export-limit registers). `optimizer.test.ts` names this the "curtailment gap". Fix: allow
  `gExp` to be reduced freely (curtailment is free), which strictly dominates paying to export.
- **G2 No demand/capacity charge.** Buy price is a flat `öre/kWh` (`SKATT_OVERFÖRING = 71.38`).
  If the DSO tariff has an `effektavgift`/capacity component (the repo already has Ellevio
  history tooling), then concentrating charging into the cheapest hour at up to 7.5–10 kW can buy
  20 öre of energy and create a monthly peak worth far more. Verify the tariff first; if it
  applies, add a monthly-peak term (e.g. a soft penalty on `gridImport` above a rolling
  month-to-date peak) — this changes the DP's cost function, so it needs its own design pass.
- **G3 `buildSolarProfiles` covers only today's and tomorrow's month** (`lib/pipeline.ts:16-24`),
  so on a month boundary the third horizon day falls back to `profiles[month] === undefined` ⇒
  **zero** solar whenever the live forecast is missing. Build profiles for every date present in
  `data.prices`.
- **G4 Live load profile is a mean, not a risk measure.** `readTrailingLoadProfile` averages 14
  days per hour-of-day with no weekday/weekend split and no outlier handling;
  `LOAD_FORECAST_MARGIN = 1.15` is the only allowance for the spread. With the same SQL it is
  cheap to also compute a per-hour p75/p90 and let the DP plan the *reserve* against the quantile
  while pricing against the mean — strictly better than one global multiplier.
- **G5 Documentation contradicts code in three places** that matter for anyone tuning this:
  `SOLAR_FORECAST_MODEL` is documented as selecting the forecast model, but
  `fetchSolarForecastSources` ignores it and always queries a fixed list of five sources (it now
  only selects the Thredds fallback); `lib/constants.ts` claims
  `solarCalibrationByMonth` is uniform 1.0 (it is not); `lib/irradiance-data.ts` is documented as
  regenerated for this site (it is not — see P0-1).
- **G6 Dead code.** `applyBatterySaveForecast` (`lib/optimizer.ts:74`) is exported and tested but
  never called; the drift is modelled inside `dpCore` instead. Delete it or wire it, don't keep
  both stories.
- **G7 `trimIncompleteTerminalDays` rejects DST days** (92/100 slots) as "incomplete", truncating
  the horizon twice a year. Same fix as P0-3 part 2.
- **G8 PlaneraElen `actual` rows go through a live EUR/SEK rate** (`frankfurter.dev`, fallback
  11.5), while Nord Pool settles SEK on the ECB rate fixed the day before. Only affects slots
  elprisetjustnu does not yet have, but it is a few percent of price error in the near horizon
  and it silently degrades to a hardcoded 11.5. Prefer the previous day's ECB fixing, and log
  when the fallback is used.
- **G9 Wear cost is charged on `|ΔSoC|`**, i.e. on the *stored* delta, not on the throughput
  actually passing through the cells (`dE/η` charging, `dE·η` discharging), and it is also charged
  on the unavoidable free solar auto-charge. Both are ≈2 % effects on a ≈4.2 öre/leg charge; fix while
  touching the cost function, not before.

---

# Implementation plan

Order matters: the measurement harness first (nothing here is safely tunable blind, and several
constants exist only to cancel each other), then the root-cause fixes, then the knobs.

### Phase M — make the effect of a change measurable (do this first)

- **M-1 Offline replay harness.** New `scripts/tools/replay-plans.ts`: read `optimizer_runs`
  (`inputs_json`, `start_soc_kwh`) plus `readings` for the same days, re-run `optimizeDispatch`
  with candidate options, and score with `evaluateDispatch` **against actual** load/solar (the
  `lib/oracle.ts` machinery — `toOptimizerSlots`, `bucketActuals`, `computeOracleDay` — already
  does the actuals bucketing; reuse it, do not reimplement). Output per day: cost, regret vs the
  hindsight oracle, grid kWh bought for the battery, end-of-day SoC. Acceptance: it reproduces
  today's published plans bit-identically when given today's options.
- **M-2 Solar forecast error report.** New `scripts/tools/solar-forecast-error.ts` over
  `solar_forecasts` × `readings.pv_w`: per source, per month, per hour-of-day — bias and MAE.
  This is what replaces `SOLAR_FORECAST_BIAS_FACTOR` and `solarCalibrationByMonth` with measured
  numbers, and what proves P0-1's fix landed.
- **M-3 Guard-rate report.** Count `skipped_solar_shortfall` / `skipped_divergence` /
  `error_*` per day from `control_actions` before and after E1/E2, so an over-skipping guard is
  visible as a number.

### Phase A — root causes (highest value, lowest risk of regression)

1. **P0-1** azimuth conversion + regenerate `irradiance-data.ts` + reset `BIAS_FACTOR` to 1.0 and
   `solarCalibrationByMonth` to 1.0 + apply calibration in one place. One commit, because the
   pieces cancel each other and splitting them would ship a 2× over-forecast.
2. **P0-2** split `SLOT_MAX_KWH` into battery and grid limits; update the three pinned tests.
3. **E3** on-device probe for `50007`/`50009`, then set the site import limit at service start.
   Ship this **with or before** P0-2 — P0-2 raises the planned import budget.
4. **P0-3** part 2 (horizon robustness + logging). Cheap, and it de-risks part 1.

Acceptance for Phase A: `npm test`, `npm run test:py`, `npm run build` green; M-1 shows lower
replayed cost and non-increased oracle regret over the last ≥14 logged days; M-2 shows the solar
bias moving toward 0 across all months, not just June–July.

### Phase B — terminal value and the risk knobs

5. **P0-3** part 1 (terminal value function, off by default, oracle path untouched).
6. **P1-3** `evaluateDispatch` `loadFactor` (pure correctness fix, no behaviour change at
   `loadFactor = 1`).
7. **P1-2** bounded deferral ramp; **P1-4** day-scoped solar premium + guard extended to cover
   all planning surcharges; **P1-1** replace the symmetric forecast haircut with a
   spread-narrowing form (and wire PlaneraElen's `low`/`high` band through `lib/prices.ts`).
8. **P1-5** measure AC-to-AC efficiency, then set the constant.

Acceptance for Phase B: each knob change is accepted **only** if M-1 shows a cost improvement (or
neutrality) over the replay window; the `risk-aware planning` describe block in
`optimizer.test.ts` is updated to pin the *new* intended behaviour rather than deleted; the
regret ≥ 0 invariant in `oracle.test.ts` still holds.

### Phase C — executor alignment

9. **E1** use `gridToBatteryKwh`; **E4** plan lookup by covered slot; **E5** hold-band marker;
   **E7** discharge efficiency.
10. **E2** live-load tracking on charge slots (shadow mode first: log the computed target for a
    few days without applying it, then arm).
11. **E6** real freeze (probe first), **E8** DP ceiling clamp.

### Phase D — modelling extensions (each its own design pass)

12. **G1** curtailment as a free option; **G3** per-date solar profiles; **G4** quantile-based
    reserve; **G9** throughput-correct wear.
13. **G2** capacity/demand charge — only after confirming the actual DSO tariff.
14. **G5**, **G6**, **G8** documentation/dead-code/FX cleanup.

## Invariants that must not regress

- `SOLINTEG_CONTROL_ARMED` gating; every `write_u16` a no-op when disarmed.
- The fail-safe chain: revert-to-auto on error/signal/exit, `next_last_target`'s retry semantics,
  the watchdog heartbeat, never parking EMS BattCtrl at 0 W.
- `opts.endSoc` behaviour and the hindsight oracle's regret ≥ 0 invariant — no planning surcharge,
  terminal value or margin may ever reach the oracle path.
- `loadFactor = 1` / all knobs at 0 must reproduce the plain optimum bit-identically (existing
  tests depend on it).
- `currentSlotIndexInPrices` / `slot_index_for_instant` elapsed-time indexing (the 92/100-slot
  DST days).
- `lib/__tests__/constants-cross-language.test.ts` and `action-contract.test.ts` — any new shared
  constant or action needs its entry.
- `npm test` + `npm run test:py` + **`npm run build`** before every change touching `lib/` or
  `scripts/services/` (the build catches `cacheComponents` prerender violations the other gates
  cannot see).

---

# Appendix A — reproducing the measurements

**Open-Meteo azimuth convention** (P0-1). Compare daily plane irradiance and peak hour:

```sh
for az in 0 -90 90 180; do
  curl -s "https://api.open-meteo.com/v1/forecast?latitude=62.4396&longitude=17.3611\
&hourly=global_tilted_irradiance&tilt=45&azimuth=$az&models=metno_nordic\
&forecast_days=1&timezone=Europe%2FStockholm"
done
```
`azimuth=0` peaks at 13:00 at 834 W/m² (= south, given solar noon 12:49 CEST at 17.36 E);
`-90` peaks 11:00 (east), `+90` peaks 16:00 (west), `180` yields 141 W/m² (north).

**Monthly magnitude of the azimuth error** (P0-1): the same query against
`https://archive-api.open-meteo.com/v1/archive?...&start_date=2024-01-01&end_date=2024-12-31`
for `azimuth=153` and `azimuth=-27`, summed per month and multiplied by `5.4 × 0.78`.

**forecast.solar** (P0-1):
`curl -s https://api.forecast.solar/estimate/62.4396/17.3611/45/153/5.4` → 7096 Wh for
2026-09-10, vs 23096 Wh at `.../-27/5.4`.

**PlaneraElen payload** (P0-3, P1-1):
`curl -s 'https://api.planeraelen.se/api/price-data?area=SE2'` → 672 rows, 7 days × 96 slots,
UTC timestamps, each forecast row carrying `low`/`high`/`very_low`/`very_high`.

**DP behaviour** (P0-2, P0-3, P1-1, P1-2, P1-3): write a temporary
`lib/__tests__/_scratch_audit.test.ts` that sets the site's env vars **before**
`await import('../optimizer')` (constants are read at module load), builds synthetic
`OptimizerSlot[]` horizons, and compares `optimizeDispatch` under different `opts` with
`evaluateDispatch`. Collect output into an array and write it to a file in an `afterAll` — vitest
swallows `console.log` in this configuration. Delete the file afterwards. The scenarios used
here: shoulder (8 kWh solar / 20 kWh load / spot 5→40), winter (1 kWh / 40 kWh / spot 20→140),
cheap-window (3 h at 2 öre then 120 öre, 4 kW flat load), and a two-day actual/forecast split for
P1-1.
