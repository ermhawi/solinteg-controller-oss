// ---- Networking ----

/** Abort an upstream fetch (elprisetjustnu, Open-Meteo) that hangs instead of erroring quickly,
 *  so a slow/unresponsive API can't stall a page render indefinitely. */
export const FETCH_TIMEOUT_MS = 10_000;

// ---- Env-var overrides ----
// Every constant below that varies by contract/site/bidding-zone (not by hardware model) can be
// overridden via the same env file the Python side reads (deploy/solinteg.env.example) — read
// once at module load, same pattern as TELEMETRY_DB_PATH/INVERTER_DATA_PATH elsewhere in the app.
// Falls back to the owner's own measured values, which stay the effective defaults either way.

function numEnv(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined || raw === '') return fallback;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : fallback;
}

// ---- Pricing ----

/** Flat buy-side charge: skatt & överföringsavgift (öre/kWh). Never applied to sell prices.
 *  Env: SKATT_OVERFORING_ORE (a national tax + a DSO-specific grid fee, lumped together for
 *  simplicity — see the value's own history if re-measuring for a different grid area). */
export const SKATT_OVERFÖRING = numEnv('SKATT_OVERFORING_ORE', 71);

/** Swedish VAT (moms) on the energy price. Env: VAT_RATE. */
export const VAT_RATE = numEnv('VAT_RATE', 0.25);

/**
 * Supplier surcharge added to the raw spot BEFORE VAT (öre/kWh) — Mölndal's påslag + elcertifikat.
 * Measured 2026-07-01 by fitting Mölndal's priceIncludingTaxAndSurcharge against Nord Pool SE3 spot:
 * `priceInclTax = (spot + 11.5) × 1.25`, exact to 0.00 öre across all 96 slots. The app fetches raw
 * spot from elprisetjustnu.se rather than Mölndal's own (unreliable) API, but this doesn't affect
 * the calibration: spot is a standardized Nord Pool SE3 value, identical regardless of which API
 * relays it, and the owner's actual supplier contract is still Mölndal Energi (confirmed 2026-07-02
 * — only the price-fetch source changed, not the contract). Re-measure only if the Mölndal contract
 * itself changes (different surcharge/elcertifikat terms), or set SUPPLIER_SURCHARGE_ORE for a
 * different supplier's own påslag+elcertifikat.
 */
export const SUPPLIER_SURCHARGE_ORE = numEnv('SUPPLIER_SURCHARGE_ORE', 11.5);

/**
 * Per-kWh compensation received for exporting to the grid, ON TOP OF the raw spot price
 * (öre/kWh) — the grid-benefit / nätnytta payment. Folded into the sell price at fetch time, so
 * every consumer (optimizer, economics, chart) values an exported kWh at `spot + this`.
 * Measured by the owner at 5.10 öre/kWh net; env EXPORT_BONUS_ORE if the contract changes (some
 * grid companies pay 0 or a %-based bonus instead of a flat rate — this formula assumes flat).
 */
export const EXPORT_BONUS_ORE = numEnv('EXPORT_BONUS_ORE', 5.1);

/** Swedish bidding zone for the Sundsvall reference installation. Env: PRICE_ZONE, any of SE1-SE4.
 * Falls back to SE2 on an unrecognised value rather than silently 404ing later. */
const VALID_PRICE_ZONES = ['SE1', 'SE2', 'SE3', 'SE4'] as const;
export type PriceZone = (typeof VALID_PRICE_ZONES)[number];
function isPriceZone(v: string): v is PriceZone {
  return (VALID_PRICE_ZONES as readonly string[]).includes(v);
}
const rawPriceZone = process.env.PRICE_ZONE;
export const PRICE_ZONE: PriceZone =
  rawPriceZone && isPriceZone(rawPriceZone) ? rawPriceZone : 'SE2';

// ---- Battery: Enershare Energy-Core 25.6 kWh ----

// One real source of truth per value, not per-language literals cross-checked after the fact:
// TS and the Python control scripts (dispatch_loop.py, modbus_poller.py, inverter_control.py)
// read the SAME env var names, with matching hardcoded fallback defaults so an install that
// never sets them (like this one) behaves identically to before this change. Two of these
// (BATTERY_MAX_KW, GRID_KW below) share their env var with values Python already reads in
// Watts (added 2026-07-10, day 2) rather than introducing a second, kW-named var for the same
// physical fact — that would just recreate the two-independently-settable-copies problem this
// is meant to close. lib/__tests__/constants-cross-language.test.ts still guards the one thing
// that genuinely can't be shared across languages: the two hardcoded fallback defaults
// themselves silently drifting apart.
export const BATTERY_KWH = numEnv('SOLINTEG_BATTERY_KWH', 25.6); // usable capacity (kWh)
export const BATTERY_MAX_KW = numEnv('SOLINTEG_BATTERY_MAX_W', 15360) / 1000; // max charge/discharge rate (kW) — env is in W, Python's native unit
/**
 * BATTERY-TERMINAL (DC) round-trip efficiency — the datasheet's ≥96 % figure, i.e. energy out of
 * the cells over energy into them. Env: SOLINTEG_BATTERY_RT_EFF.
 *
 * This is NOT what the optimizer needs on its own, and treating it as though it were was a real
 * modelling error until 2026-09-10. The DP prices GRID (AC) energy in and out, so its per-leg
 * efficiency has to be the AC-to-AC one — see EFF_ONE_WAY below. `measure-battery-rt-eff.py`
 * measures this DC figure (it integrates `battery_w`), which is why running it never closed the gap.
 */
export const BATTERY_RT_EFF = numEnv('SOLINTEG_BATTERY_RT_EFF', 0.96);

/**
 * Inverter conversion efficiency for ONE leg (AC→DC on charge, DC→AC on discharge).
 * Env: SOLINTEG_INVERTER_CONV_EFF.
 *
 * Default 0.97 is the datasheet class for this inverter family (peak ~97.5 %, EU-weighted ~97 %) —
 * a documented hardware figure, not a fitted fudge, and the honest structure to model: the DP's
 * per-leg efficiency has always been applied between the METER and stored energy, so it needs the
 * inverter's losses as well as the cells'.
 *
 * What the previous omission cost: `√0.96 = 0.9798` per leg claimed a 96 % AC round trip, against a
 * realistic 88-90 %. At a 90 öre buy price that understates the cost of a stored kWh by roughly
 * 8 öre — larger than BATTERY_WEAR_COST_ORE_PER_KWH (≈4.2 öre/leg here), so it was enough to flip
 * marginal arbitrage and marginal solar-storage decisions the wrong way, always in the direction of
 * cycling more than pays.
 *
 * Replace both this and BATTERY_RT_EFF with measured values when you have them:
 * `scripts/tools/measure-battery-rt-eff.py` now reports the DC round trip AND the AC-to-AC one
 * (from night-time windows, where the inverter's AC port carries nothing but the battery).
 */
export const INVERTER_CONV_EFF = numEnv('SOLINTEG_INVERTER_CONV_EFF', 0.97);

/**
 * The per-leg efficiency the optimizer actually uses: METER ↔ stored energy, one direction.
 * `stored = acIn × EFF_ONE_WAY` on charge, `acOut = stored × EFF_ONE_WAY` on discharge, so the
 * AC-to-AC round trip is `EFF_ONE_WAY²` ≈ 0.903 at the defaults.
 *
 * One exported definition rather than `Math.sqrt(BATTERY_RT_EFF)` repeated at each site, because
 * that repetition is exactly how the DC/AC confusion survived: lib/optimizer.ts, lib/oracle.ts,
 * dispatch_loop.py and four test files each derived it independently.
 */
export const EFF_ONE_WAY = Math.sqrt(BATTERY_RT_EFF) * INVERTER_CONV_EFF;

/** Empirical battery-save discharge used only for forecast correction; 0 disables it. */
export const BATTERY_SAVE_DRIFT_W = numEnv(
  'SOLINTEG_BATTERY_SAVE_DRIFT_W',
  numEnv('DISPATCH_BATTERY_SAVE_W', 220),
);

/** Observed physical discharge during battery-save/auto operation; distinct from the inverter setpoint. */
export const BATTERY_SAVE_FORECAST_DRAIN_W = numEnv('SOLINTEG_BATTERY_SAVE_FORECAST_DRAIN_W', 220);

const SOC_FLOOR_PCT = numEnv('SOLINTEG_SOC_FLOOR_PCT', 8);
// Physical/BMS protection floor. Do not raise this to create a planning reserve.
export const BATTERY_MIN_SOC_KWH = BATTERY_KWH * (SOC_FLOOR_PCT / 100);
/**
 * Minimum energy the ECONOMIC optimizer may plan to keep. Separate from the
 * inverter/BMS hard floor: the Sundsvall installation uses 20% for planning,
 * 12% for the physical backstop. A site without an override retains its old
 * behavior (planning reserve = physical floor).
 */
export const BATTERY_PLANNER_RESERVE_KWH = BATTERY_KWH * (
  Math.max(SOC_FLOOR_PCT, Math.min(98, numEnv('SOLINTEG_PLANNER_RESERVE_PCT', SOC_FLOOR_PCT))) / 100
);

/**
 * Robust-planning margin on the load forecast inside the DP (dimensionless factor ≥ 1;
 * 1 disables). The optimizer PLANS against consumptionKwh × this factor — the logged
 * telemetry inputs keep the honest, unmargined forecast (see lib/plan.ts).
 *
 * Why: the DP trusts its point forecasts completely, so its optimum routinely kisses the
 * SoC floor at exactly the moment committed load ends (e.g. sunrise) — zero slack. A load
 * forecast that runs even 10-20% hot then forces grid imports at exactly the hours the
 * plan judged most expensive (that's WHY the battery was scheduled to carry the night).
 * Measured 2026-07-18: a ~25%-low overnight load model turned a planned 0.15 kWh import
 * into 2.18 kWh bought at 170-220 öre. Planning against pessimistic load keeps the plan
 * feasible in the bad case; when load comes in at forecast, the unspent margin is simply
 * re-optimized away by the next replan (hourly + triggered), costing only öre-scale spread
 * timing and ~2 öre/kWh wear. The asymmetry that justifies it: holding 1 kWh too much
 * costs the small evening/next-day sell-price difference, running 1 kWh short costs the
 * full buy-sell spread at the night's worst prices. Price-certain arbitrage (sell high
 * evening, rebuy cheap night) is NOT distorted — prices carry no margin, the DP just
 * plans to rebuy the margin too.
 *
 * **Now a floor rather than the whole story.** When the trailing telemetry window supports it, each
 * slot also carries a MEASURED high quantile for that hour of day
 * (`OptimizerSlot.consumptionReserveKwh`) and the DP reserves against
 * `max(consumptionKwh × this, that quantile)`. A single global factor necessarily says the same
 * thing about 03:00 and about the dinner peak, and those hours do not have remotely the same
 * spread — overnight load is nearly deterministic, evenings are where the variance is. This stays
 * as the floor because the quantile is measured from RECENT days: a cold snap the window has not
 * seen yet is covered by the margin on top of the weather-scaled expectation, not by the quantile.
 * See OPTIMIZER-AUDIT.md G4. Env: SOLINTEG_LOAD_FORECAST_MARGIN.
 */
export const LOAD_FORECAST_MARGIN = numEnv('SOLINTEG_LOAD_FORECAST_MARGIN', 1.15);

/**
 * Risk-aware planning knob 1 of 2: deferral bias on the battery's grid-facing actions
 * (öre per kWh per hour of earliness; 0 disables). Inside the DP, a grid-funded battery
 * charge or a battery-to-grid sell scheduled at slot i is surcharged
 * `rate × max(0, DEFERRAL_HORIZON_HOURS − hoursFromNow(i))` öre/kWh, so among near-equal-price slots the action
 * lands as LATE as physically possible (throughput caps still force multi-slot actions to
 * start early enough). Planning-only: the surcharge shapes the backward pass, never the
 * emitted flows, the logged inputs, or the hindsight oracle (which must not carry it —
 * deferral has no value in hindsight, and it would poison the regret-≥0 invariant).
 *
 * Why: the DP trusts its point forecasts, so information has no value to it — it buys in
 * the literally-cheapest slot even when a slot 5 h later costs 0.6 öre more. But plans
 * recompute hourly (+ drift-triggered replans), so a deferred action gets re-decided with
 * fresher solar/load reality, and a buy that turned out redundant is simply cancelled;
 * an executed one is sunk. Measured 2026-07-19: an 8 kWh grid-buy at 85.6-86.2 öre for an
 * evening sell (planned upside ≈ 0.8 kr) executed from 09:45 on a "6 kWh solar" forecast
 * day that actually delivered 3-5× that — battery full by 14:30 on sun alone, ≈ 6 kr lost.
 * Buying at 15:00 instead would have cost 5 öre more in spread and been cancelled by the
 * replans entirely. Sells get the same bias with a stronger rationale: a sold kWh can only
 * be re-acquired at sell + ~75-80 öre (moms + skatt/överföring wedge), so a sale is the
 * most irreversible action the system takes — at near-equal prices, later strictly
 * dominates (energy still sellable, and it can cover a surprise load at full buy-price
 * value). The linear ramp is deliberately unbounded across the horizon; the sacrifice cap
 * below is what keeps a genuinely-better early price from being deferred over.
 * The linear ramp used to be measured from the horizon's END and unbounded; see
 * DEFERRAL_HORIZON_HOURS below for why that had to change when the horizon grew past a day.
 * Env: SOLINTEG_DEFERRAL_RATE_ORE.
 */
export const DEFERRAL_RATE_ORE_PER_KWH_HOUR = numEnv('SOLINTEG_DEFERRAL_RATE_ORE', 0.4);

/**
 * How far ahead the deferral bias above still discriminates between slots (hours; the surcharge
 * is `rate × max(0, this − hoursFromNow)`, so its maximum level is `rate × this`).
 *
 * Why a cap exists at all. The ramp used to be measured from the horizon's END and left unbounded:
 * `rate × hoursUntilHorizonEnd`. That is fine while the horizon is one day, which is what it was
 * designed against — but the horizon later grew to three days, and the surcharge grew with it:
 * measured 2026-09-10, 9.5 öre/kWh at a 1-day horizon, 19.1 at two days, **28.7 at three**. Because
 * the surcharge multiplies the ENERGY charged, not just its timing, that stopped being a
 * tie-breaker and became a flat tax on grid charging: against a real SE2 buy-side spread of
 * 64-96 öre and a ~62 öre net round-trip margin, a 28.7 öre bias plus the 20 öre solar premium
 * consumed ~78 % of the trade. And it self-destructed — on a three-day shoulder-season horizon the
 * deferred plan gave up 152.6 öre of real value against the 50 öre sacrifice cap, so the two-pass
 * guard threw it away on every plan, paying the cost of the feature (two full DP solves) while
 * delivering none of its benefit.
 *
 * Measuring from NOW instead of from the horizon's end is what makes it horizon-independent: the
 * plan's slot 0 IS now (lib/plan.ts slices the horizon at the current slot before solving), so
 * "hours from now" is a property of the decision rather than of how much data the price feed
 * happened to return. Beyond this window the surcharge is 0 — a far-off action is maximally
 * cancellable already, and has nothing to be nudged about.
 *
 * 12 h is chosen to span the two shapes the bias exists for: deferring a charge by a few hours to
 * see whether the solar arrives, and deferring a sell from an evening peak to a near-equal
 * next-morning one. At the default rate the maximum level is then 4.8 öre/kWh — enough to break a
 * near-tie, an order of magnitude below any real daily spread, which is exactly the claim the
 * feature has always made for itself. The MAGNITUDE is still a placed estimate, not a fit; refit it
 * against replay data (OPTIMIZER-AUDIT.md Phase M) rather than by inspection.
 * Env: SOLINTEG_DEFERRAL_HORIZON_H.
 */
export const DEFERRAL_HORIZON_HOURS = numEnv('SOLINTEG_DEFERRAL_HORIZON_H', 12);

/** Premium applied only to forecast slots while optimizing; logged prices remain untouched. */
export const FORECAST_RISK_PREMIUM_ORE = numEnv('SOLINTEG_FORECAST_RISK_PREMIUM_ORE', 10);

/**
 * Hard cap (öre, whole-plan total) on the real value ALL the planning-risk knobs together may give
 * up: the deferral bias, the solar risk premium and the forecast price premium.
 *
 * optimizeDispatch runs the DP twice whenever any of them is active — once with every risk knob at
 * zero, once as configured — prices both trajectories at TRUE prices (evaluateDispatch, on the same
 * load margin and terminal value the plans were solved with) and keeps the risk-aware plan only if
 * it costs at most this much. So caution can never silently trade away more than ~0.5 kr per plan:
 * a sell peak tonight that genuinely beats tomorrow's by 10+ öre/kWh stays tonight (10 öre × a
 * battery-scale sell blows this cap), while an evening-vs-next-morning sell at a few öre difference
 * defers.
 *
 * It covered the deferral bias ONLY until 2026-09-10, which left the solar premium — the largest of
 * the three at 20 öre/kWh — able to give up unbounded real value with nothing measuring it.
 *
 * All-or-nothing by design: a plan mixing good caution with one expensive piece of it falls back
 * entirely. Safety first at öre-scale stakes, and it keeps the guard a comparison between two real
 * plans rather than an attribution problem.
 * Env: SOLINTEG_RISK_MAX_SACRIFICE_ORE (falls back to the older
 * SOLINTEG_DEFERRAL_MAX_SACRIFICE_ORE so an existing deployment's setting keeps working).
 */
export const MAX_RISK_SACRIFICE_ORE = numEnv(
  'SOLINTEG_RISK_MAX_SACRIFICE_ORE',
  numEnv('SOLINTEG_DEFERRAL_MAX_SACRIFICE_ORE', 50),
);

/**
 * Risk-aware planning knob 2 of 2: uncertainty premium on grid-funded battery charging
 * (öre/kWh at full scale; 0 disables), scaled per decision by how redundant the buy could
 * turn out: `premium × min(1, futureRawForecastSolar / batteryHeadroom)`. RAW forecast solar,
 * not load-netted surplus: the premium prices forecast ERROR, which scales with how much solar
 * is still in play — the 2026-07-19 forecast netted to zero surplus (6 kWh solar vs ~12 kWh
 * load) yet actual delivery filled the battery by 14:30. When the remaining day's forecast
 * solar alone could (nearly) fill the battery's headroom,
 * a grid-buy to fill that same headroom is exactly the trade that a modest solar
 * over-delivery turns into a pure loss — the DP should demand a spread that pays for that
 * risk, not take any thin positive edge. Winter nights (no future solar) scale to zero, so
 * genuine overnight arbitrage is untouched; a modest-solar shoulder day scales in between.
 * Sized so the 2026-07-19 incident is killed: ratio was 6.0/7.6 ≈ 0.79 → ≈16 öre premium
 * vs the trade's ~11-15 öre post-cost margin. 20 is a placed estimate, not a fit — your own
 * forecast-vs-actual telemetry (optimizer_runs inputs_json vs actual readings) is the data
 * to refit it from once a few weeks of solar-forecast error distribution exist. Like the
 * deferral bias and LOAD_FORECAST_MARGIN, planning-only: never in logged inputs or the
 * hindsight oracle. Env: SOLINTEG_SOLAR_RISK_PREMIUM_ORE.
 */
export const SOLAR_RISK_PREMIUM_ORE_PER_KWH = numEnv('SOLINTEG_SOLAR_RISK_PREMIUM_ORE', 20);

/**
 * Survival form of the solar-risk premium (optimizeDispatch's opts.solarRiskSurvival): charge
 * the premium only on the part of a grid buy still in the battery when the next solar arrives,
 * after self-use has drawn the forecast house deficit out of it. The plain form charges every
 * bought kWh, so a winter night buy that the heat pump uses up before dawn pays a premium
 * against tomorrow's solar it can never overlap with. Daytime buys are priced exactly as
 * before; a summer night buy (little night load, so it survives to sunrise) keeps the full
 * premium, which is the case the premium exists for.
 *
 * Disabled by default on the Sundsvall installation until local replay validates it.
 * Set SOLINTEG_SOLAR_RISK_SURVIVAL=1 to opt in.
 */
export const SOLAR_RISK_SURVIVAL = process.env.SOLINTEG_SOLAR_RISK_SURVIVAL === '1';

/**
 * Need-window form of the deferral bias (optimizeDispatch's opts.deferralUntilNeeded): a
 * grid-funded charge's earliness is measured to when the bought energy would be NEEDED (the
 * first slot where the forecast house deficit since the buy exceeds what the battery already
 * holds above the floor), not to the horizon end. The slope that makes the planner prefer the
 * later of near-equal slots is unchanged; what goes is the LEVEL, which on a long plan reaches
 * many öre/kWh and taxed buys that cannot be deferred at all (a winter night buy feeding the
 * heat pump within the hour paid as if it could wait until tomorrow night). A morning buy for
 * an evening sell on a solar day is not needed until evening, so it keeps its protection.
 * Sells keep the full ramp.
 *
 * Disabled by default here until tested against the local 12-hour deferral horizon.
 * Set SOLINTEG_DEFERRAL_UNTIL_NEEDED=1 to opt in.
 */
export const DEFERRAL_UNTIL_NEEDED = process.env.SOLINTEG_DEFERRAL_UNTIL_NEEDED === '1';

/**
 * Swedish battery energy-tax refund (Skatteverket refund type 739, "Inmatad el efter lagring",
 * from 2026-01-01): the energy tax paid on grid electricity is refunded when that electricity
 * is stored in a battery and fed back to the grid. Excl. VAT, storage losses not refundable,
 * solar-origin energy earns nothing. Claimed per calendar year.
 *
 * öre per exported kWh, by calendar year of the export. A year missing here earns NO credit
 * (fail closed): the rate or the rule itself can change, so each new year must be added by
 * hand once its rate is confirmed. 2026: 36.0 öre (Skatteverket "Skatt på el"). Outside Sweden,
 * or without a refund claim, set SOLINTEG_REFUND_CREDIT=0.
 */
export const ENERGY_TAX_REFUND_ORE_BY_YEAR: Record<number, number> = { 2026: 36.0 };

/**
 * Refund-aware planning (optimizeDispatch's opts.refundCreditOre): the plan prices grid
 * charging at the full energy tax, so a night buy re-sold before sunrise looks ~36 öre/kWh
 * worse than it is. When on, lib/plan.ts lets the optimizer try a second plan with the refund
 * credited on battery exports in no-solar slots only (so solar-origin sales are never
 * rewarded, and every credited kWh is easy to document: grid meter exporting with PV at zero),
 * scores both plans on true cash plus the refund each would actually earn, and keeps the
 * credited one only if it really wins.
 *
 * Opt-in only. The rate paid by this household must be documented separately (regional
 * energy-tax reductions can apply); default paid-tax value 0 prevents speculative credits.
 * Enable with SOLINTEG_REFUND_CREDIT=1 AND SOLINTEG_REFUND_TAX_PAID_ORE=<verified öre/kWh>,
 * only when eligible for and intending to submit the annual Skatteverket 739 claim.
 */
export const REFUND_CREDIT_ENABLED = process.env.SOLINTEG_REFUND_CREDIT === '1';
const REFUND_TAX_PAID_ORE = Math.max(0, numEnv('SOLINTEG_REFUND_TAX_PAID_ORE', 0));

/** Refund credit never exceeds either confirmed general rate or actual site tax paid. */
export function energyTaxRefundOre(startTime: string): number {
  return Math.min(ENERGY_TAX_REFUND_ORE_BY_YEAR[+startTime.slice(0, 4)] ?? 0, REFUND_TAX_PAID_ORE);
}

// ---- Hold mode (opt-in, shadow-only — see lib/plan.ts's holdEnabled wiring) ----
// Gate parameters for the DP's 'hold' action: freeze SoC and export the solar surplus instead
// of storing it. Slot-unit conversions (kW → kWh/slot, hours → slots) happen at the use site
// in lib/optimizer.ts; the values here stay in natural units like everything else in this file.

/**
 * Gate 2: minimum gross PV (kW) for a slot to be hold-eligible — below this the surplus is
 * too small for exporting it to beat storing it once round-trip losses and wear are in.
 * Env: SOLINTEG_HOLD_MIN_PV_KW.
 */
export const HOLD_MIN_PV_KW = numEnv('SOLINTEG_HOLD_MIN_PV_KW', 3);

/**
 * Gates 1+3: look-ahead window (hours) for both the price-drop test (cheapest buy ahead)
 * and the refill guarantee (forecast solar available to re-fill what hold declines to store).
 * Env: SOLINTEG_HOLD_REFILL_WINDOW_H.
 */
export const HOLD_REFILL_WINDOW_H = numEnv('SOLINTEG_HOLD_REFILL_WINDOW_H', 12);

/**
 * Gate 3: refillable solar within the window must cover the battery's remaining headroom
 * times this margin (1.1 = full + 10 %) before hold may decline to store — the buffer absorbs
 * forecast optimism so hold can't strand the battery below full ahead of an expensive evening.
 * Env: SOLINTEG_HOLD_REFILL_MARGIN.
 */
export const HOLD_REFILL_MARGIN = numEnv('SOLINTEG_HOLD_REFILL_MARGIN', 1.1);

/**
 * Gate 1: sell price now must beat the cheapest buy price within the window by at least this
 * many öre before a slot is hold-eligible. 0 = any non-negative spread qualifies; raise it to
 * demand a real profit margin per exported kWh. Env: SOLINTEG_HOLD_PRICE_DROP_ORE.
 */
export const HOLD_PRICE_DROP_MARGIN_ORE = numEnv('SOLINTEG_HOLD_PRICE_DROP_ORE', 0);

/**
 * How far ahead the solar risk premium counts forecast solar (hours).
 *
 * The premium scales with `futureRawForecastSolar / batteryHeadroom`, capped at 1. That ratio was
 * computed over the ENTIRE remaining horizon, which was fine while the horizon was one day and
 * meaningless once it reached three: three days of solar exceeds any battery's headroom several
 * times over, so `min(1, …)` saturated and the premium became a flat 20 öre/kWh on every
 * grid-funded charge, at every hour, from roughly March to October. The constant's own docstring
 * describes "the remaining day's forecast solar" — this is the window that makes that true.
 *
 * 12 h covers the rest of a daylight day, which is the horizon over which solar can plausibly
 * arrive and make a charge redundant before the stored energy is needed. Beyond that the buy is not
 * competing with the same solar any more; it is competing with tomorrow's plan, which will be
 * re-decided anyway. Env: SOLINTEG_SOLAR_RISK_LOOKAHEAD_H.
 */
export const SOLAR_RISK_LOOKAHEAD_HOURS = numEnv('SOLINTEG_SOLAR_RISK_LOOKAHEAD_H', 12);

/**
 * Multiplier on the DP's terminal energy value (0..1; 0 disables and restores the historical
 * terminal-value-0 behaviour exactly).
 *
 * The value itself is DERIVED per plan, not configured — `terminalEnergyValueOre` in
 * lib/optimizer.ts takes the cheapest import price in the horizon, converts it at the discharge
 * leg's efficiency and nets off wear. That is provably too low to make a grid buy worthwhile purely
 * to bank energy past the horizon, and far too high to let the battery be dumped at the horizon's
 * end, which is what a zero terminal value invited: measured 2026-09-10, every plan in every
 * scenario and season ended at exactly the SoC floor, and a horizon that collapses to today (an
 * unreleased day-ahead, a price-forecast outage) therefore meant "empty the battery tonight".
 *
 * This knob exists only as a kill switch and as a way to dial the confidence down (e.g. 0.8) if
 * your own replay shows the plan carrying more than you want overnight. Planning-only: it never
 * reaches the hindsight oracle. Env: SOLINTEG_TERMINAL_VALUE_FACTOR.
 */
export const TERMINAL_VALUE_FACTOR = numEnv('SOLINTEG_TERMINAL_VALUE_FACTOR', 1);

/**
 * Trailing window (days) for the live per-hour load profile read from telemetry readings,
 * which replaces the static Ellevio-fitted hour shape whenever enough data exists (see
 * lib/telemetry/readings.ts readTrailingLoadProfile / lib/load.ts slotConsumptionFromLive). 0 disables
 * the live profile entirely (static model only). Env: SOLINTEG_LIVE_LOAD_DAYS.
 */
export const LIVE_LOAD_PROFILE_DAYS = numEnv('SOLINTEG_LIVE_LOAD_DAYS', 14);

// SoC ceiling: enforced entirely inside inverter_control.py's force_charge (refuses to charge
// past it rather than silently capping) — the DP optimizer does NOT model this today, it plans
// as if the full BATTERY_KWH were reachable. Exported here for documentation/future use (e.g. a
// dashboard "distance to safety ceiling" indicator), not yet wired into optimizer.ts/oracle.ts.
// Making the DP itself ceiling-aware would be a real behavior change — out of scope for this
// config-consolidation pass, which only touches how values are SOURCED, not what they do.
const SOC_CEILING_PCT = numEnv('SOLINTEG_SOC_CEILING_PCT', 98);
export const BATTERY_MAX_SOC_KWH = BATTERY_KWH * (SOC_CEILING_PCT / 100);

/**
 * Marginal wear cost per kWh of battery throughput (öre), charged on both charge and
 * discharge in the optimizer's cost function. LFP cells rated ~6000 cycles before
 * ~10-15% capacity fade — that's NOT end of life, the pack keeps working well past it —
 * so this deliberately prices only the fractional pack value at risk over the rated life,
 * not a full replacement write-off, spread over total lifetime throughput (charge +
 * discharge, hence the ×2). Kept small on purpose: it should discourage only genuinely
 * marginal cycling (a slot barely worth doing after round-trip losses), never real
 * arbitrage — a typical daily price spread is tens of öre, an order of magnitude more
 * than this. REPLACEMENT_COST_SEK should be what you paid for inverter+battery combined —
 * using the whole system cost (a battery-only sub-price usually isn't itemized) is
 * intentionally conservative, slightly overstating the true battery-only value. The default
 * below is just an illustrative round number, not any particular real installation's price.
 * Env: BATTERY_REPLACEMENT_COST_SEK — set this to your own purchase price.
 */
const REPLACEMENT_COST_SEK = numEnv('BATTERY_REPLACEMENT_COST_SEK', 60_000);
const ASSUMED_CAPACITY_LOSS_AT_RATED_CYCLES = 0.125; // 12.5%, midpoint of ~10-15%
const RATED_CYCLES = 6000;
export const BATTERY_WEAR_COST_ORE_PER_KWH =
  (REPLACEMENT_COST_SEK * 100 * ASSUMED_CAPACITY_LOSS_AT_RATED_CYCLES) /
  (2 * RATED_CYCLES * BATTERY_KWH); // ≈2.3 öre/kWh leg, ≈4.7 öre per full round-tripped kWh

// ---- Grid: 3×16A fuses ----

export const GRID_KW = numEnv('SOLINTEG_GRID_CAP_W', 11_000) / 1000; // ~11 kW cap — binding constraint over battery's 15.36 kW; env is in W, Python's native unit

// ---- Site location: Göteborg archipelago ----
// The one real exported source of truth for these coordinates — previously just a comment here
// (not an export), while lib/forecast.ts and two offline scripts each independently hardcoded the
// same literal, an unforced 4-way duplication/drift risk with no functional reason behind it.
// Env: SITE_LATITUDE / SITE_LONGITUDE (decimal degrees).
export const SITE_LATITUDE = numEnv('SITE_LATITUDE', 57.64);
export const SITE_LONGITUDE = numEnv('SITE_LONGITUDE', 11.78);

/**
 * **What this actually controls, which is narrower than it reads.** Since the forecast became a
 * 5-source median ensemble (`fetchSolarForecastSources`: metno_nordic, ecmwf_ifs025, icon_seamless,
 * Open-Meteo's best_match blend, and forecast.solar), this no longer selects the planning forecast —
 * every source is fetched regardless and `combineSolarForecasts` takes the per-slot median. It now
 * governs exactly two things:
 *   1. whether the second-tier MET Norway Thredds fallback is used when Open-Meteo fails entirely
 *      (`lib/plan.ts`; that fallback only mirrors `metno_nordic`, so it is skipped for any other
 *      value), and
 *   2. which single model the display-only current-hour reading comes from
 *      (`fetchCurrentHourGhiForecast`), including whether that request is hourly or minutely_15.
 * The historical text below describes the backtest that chose `metno_nordic` when it WAS the single
 * planning source; it is kept because the reasoning still applies to picking a fallback, and because
 * the ensemble's per-source skill is the thing `solar_forecasts` telemetry exists to measure.
 *
 * Open-Meteo `models=` value for the solar forecast (lib/forecast.ts), or '' for the
 * default best_match blend. Reference install (57.6°N, Nordic) backtested `metno_nordic`
 * against 4.3 months of its own measured GHI (scripts/tools/compare-metno-solar.mjs) and
 * found it 14% better MAE than best_match, particularly fixing a systematic morning
 * over-forecast — but this is a REGIONAL result: metno_nordic only covers
 * Norway/Sweden/Denmark/Finland, and Open-Meteo's dedicated regional models differ by
 * territory (e.g. its default blend already leans on ICON-D2 for Central Europe, HRRR for
 * the US). If you're outside the Nordics, don't assume this default transfers — run the
 * backtest script against your own station history and your region's candidate models
 * (Open-Meteo's model list: open-meteo.com/en/docs) before trusting either choice. Also
 * note: unlike best_match, none of the regional models expose a `minutely_15` variant, so
 * picking one switches the forecast to hourly (see forecast.ts's slot-filling comment).
 * Env: SOLAR_FORECAST_MODEL.
 */
export const SOLAR_FORECAST_MODEL = process.env.SOLAR_FORECAST_MODEL ?? 'metno_nordic';

/**
 * Flat multiplier on every LIVE solar forecast (lib/solar.ts `calibrateForecastKwh`), 1 = off.
 *
 * Now 1.0. It shipped at 1.75 as an undocumented "conservative multiplier calibrated from recent
 * site telemetry" — in reality it was compensating for the azimuth-convention bug fixed
 * 2026-09-10 (see `apiAzimuth` in lib/solar.ts and OPTIMIZER-AUDIT.md P0-1), which forecast this
 * SSE array as if it faced north. That error needed 1.5x in June and 6.2x in December, so a flat
 * 1.75 was only ever right for two months of the year, and it applied to the live forecast but
 * not to the climatology fallback, putting the two on different scales.
 *
 * If your own forecast-vs-actual telemetry shows a residual site-wide bias after the geometry is
 * right, fix it where it belongs — `performanceRatio` (a level error, all year) or
 * `solarCalibrationByMonth` (a seasonal error) — both of which have generator scripts behind
 * them. Reach for this only for a genuinely flat, all-season correction you can point at data
 * for. Env: SOLAR_FORECAST_BIAS_FACTOR.
 */
export const SOLAR_FORECAST_BIAS_FACTOR = numEnv('SOLAR_FORECAST_BIAS_FACTOR', 1.0);

// ---- Solar installation ----
// tiltDeg/azimuthDeg are sent to Open-Meteo's global_tilted_irradiance forecast. They are also
// used when documenting and calibrating the array. performanceRatio now represents the remaining
// system losses (inverter, cables, temperature, dirt and mismatch), not panel orientation —
// orientation is already present in the irradiance input. A new adopter without production
// history should start around 0.7-0.8 for an unshaded array and refine it against real production
// data (see solarCalibrationByMonth's generator script, scripts/tools/process-inverter-data.ts,
// for the same kind of per-installation calibration).
//
// To adapt for a different site: replace this array with your own — kWp is nameplate power
// per string/array (from your panel spec sheet), tiltDeg/azimuthDeg from your install's own
// documentation (0° azimuth = north, 180° = south).
//
// IMPORTANT — azimuthDeg is stored in THIS convention (0 = north) and must be converted before
// it reaches any irradiance API: Open-Meteo's `azimuth=` and forecast.solar's `:az` both use
// 0 = south. `apiAzimuth()` in lib/solar.ts is the one place that conversion happens; never pass
// azimuthDeg to an API directly. Passing it raw is exactly the bug fixed 2026-09-10, which put
// the annual solar forecast 2.13x low for a year (OPTIMIZER-AUDIT.md P0-1).
export interface SolarArray {
  label: string;
  kWp: number;
  tiltDeg: number;
  azimuthDeg: number;
  performanceRatio: number;
}

// This site: a single plane, taken from the owner's forecast.solar config / Home Assistant
// home zone (62.44°N, 17.36°E — see SITE_LATITUDE/LONGITUDE): 5.4 kWp, 45° tilt, azimuth 153°
// (SSE; forecast.solar's -27 in the 0°=N/180°=S convention used here).
// performanceRatio is an UNCALIBRATED estimate — an unshaded, near-south array, mid of the
// documented 0.7–0.8 band; refine it against real production once a season of data exists.
// NOTE — the two data sources paired with this array now reflect THIS site, with one caveat:
//   • lib/irradiance-data.ts avgGhiByMonthHour — regenerated from Open-Meteo ERA5 at these
//     coordinates (scripts/tools/build-irradiance-openmeteo.mjs, 20-yr climatology); modelled
//     reanalysis rather than a measured station, since the nearest SMHI irradiance station is
//     170+ km away.
//   • lib/consumption-data.ts solarCalibrationByMonth — uniform 1.0 (no production history
//     yet), so the raw GHI×kWp×PR level is still uncorrected. Regenerate it with
//     build-solar-calibration.ts once you have a season of your own production. (It briefly
//     carried hand-set 1.4s for Aug/Sep while this comment still claimed uniform 1.0; both the
//     values and the claim were reconciled 2026-09-10 — see OPTIMIZER-AUDIT.md P0-1.)
// See DOMAIN.md "Adapting to a new site".
export const SOLAR_ARRAYS: SolarArray[] = [
  { label: 'Single plane: SSE (153°), 45° tilt', kWp: 5.4, tiltDeg: 45, azimuthDeg: 153, performanceRatio: 0.78 },
];
