import {
  BATTERY_KWH,
  BATTERY_MAX_KW,
  BATTERY_MAX_SOC_KWH,
  BATTERY_MIN_SOC_KWH,
  BATTERY_PLANNER_RESERVE_KWH,
  BATTERY_WEAR_COST_ORE_PER_KWH,
  DEFERRAL_HORIZON_HOURS,
  EFF_ONE_WAY,
  GRID_KW,
  MAX_RISK_SACRIFICE_ORE,
  SOLAR_RISK_LOOKAHEAD_HOURS,
  HOLD_MIN_PV_KW,
  HOLD_REFILL_WINDOW_H,
  HOLD_REFILL_MARGIN,
  HOLD_PRICE_DROP_MARGIN_ORE,
} from './constants';

export { BATTERY_KWH, BATTERY_MAX_KW, GRID_KW, BATTERY_MIN_SOC_KWH }; // re-export for tests and external callers

/**
 * Two DIFFERENT physical limits, deliberately separate. They used to be a single
 * `min(GRID_KW, BATTERY_MAX_KW) / 4`, used simultaneously as the battery's throughput cap, the
 * grid import cap and the grid export cap — which is only harmless while the two ratings happen
 * to be equal.
 *
 * Grid import is `battery charge + house load`, so whenever the battery is the smaller of the two
 * (7.5 kW battery behind a 10 kW connection, this site) every kW the house consumed was subtracted
 * from the charging budget even though the connection had room. Measured 2026-09-10: with a flat
 * 4 kW house load and three genuinely cheap hours, the DP banked 10.16 kWh where 13.55 kWh was
 * physically available — the missing 3.39 kWh then bought at 233 instead of 85 öre, ≈5 kr on one
 * winter day. Mirror image on the reference install (15.36 kW battery, 11 kW grid): the battery
 * was throttled to the grid's 11 kW for its own throughput.
 *
 * GRID_SLOT_KWH is the meter-level limit and must stay the real main-fuse rating (env
 * SOLINTEG_GRID_CAP_W) — the inverter enforces nothing at the meter on its own; see
 * OPTIMIZER-AUDIT.md E3.
 */
const BATTERY_SLOT_KWH = BATTERY_MAX_KW / 4; // battery charge/discharge throughput per 15-min slot
const GRID_SLOT_KWH = GRID_KW / 4; // grid import AND export per 15-min slot (house load included)
// A dawn/dusk trickle below 200 W average is not meaningful solar for premium survival.
const SURVIVAL_SOLAR_SLOT_KWH = 0.05;
// The METER-to-stored per-leg efficiency (see EFF_ONE_WAY in lib/constants.ts). This used to be
// derived from the battery-terminal DC round trip alone, while every use of it here sits between
// GRID energy and stored energy — so the inverter's own conversion losses were missing entirely:
// ~0.98 claimed against ~0.95 real per leg, worth roughly 8 öre per stored kWh at a 90 öre buy
// price, which is more than the wear cost and enough to flip marginal cycling decisions.
const ONE_WAY_EFF = EFF_ONE_WAY;
// Economic decisions may discharge down to the PLANNING reserve, never merely
// to the inverter/BMS hard floor. The executor's emergency floor stays separate.
const DEFAULT_MIN_SOC_KWH = Math.max(BATTERY_MIN_SOC_KWH, BATTERY_PLANNER_RESERVE_KWH);
const PLANNER_RESERVE_ENABLED = BATTERY_PLANNER_RESERVE_KWH > BATTERY_MIN_SOC_KWH + 1e-9;
/**
 * The highest SoC a FORCED charge may plan for. `inverter_control.force_charge` refuses to charge
 * above `SOLINTEG_SOC_CEILING_PCT` and returns to auto instead, so a plan that assumes the full pack
 * is reachable silently diverges from what the executor will do — and the divergence guard then sees
 * drift that no forecast error caused.
 *
 * It bounds forced charging only. Solar auto-charge is deliberately still allowed to run past it
 * (see the naturalFloor logic): the inverter's own self-use logic fills the battery from surplus
 * regardless of this ceiling, which gates nothing but our own EMS commands. Modelling it as a hard
 * cap on SoC would make the plan wrong about the physics in the other direction.
 *
 * At the default 100 % this is BATTERY_KWH and the constraint never binds, so nothing changes for an
 * install that has not lowered the ceiling. See OPTIMIZER-AUDIT.md E8.
 */
const MAX_FORCED_SOC_KWH = Math.min(BATTERY_MAX_SOC_KWH, BATTERY_KWH);

const SOC_LEVELS = 193; // SoC discretisation for the DP (≈0.12 kWh resolution over the usable range)

// Hold mode (gated, opt-in via holdEnabled). Lets the DP freeze SoC and EXPORT
// solar surplus instead of forcing it into the battery — but only when three gate conditions
// hold (see dpCore). Gate parameters live in lib/constants.ts (env-overridable, natural units);
// only the slot-unit conversions happen here.
const HOLD_MIN_PV_KWH = HOLD_MIN_PV_KW / 4; // gate 2: gross PV threshold → per 15-min slot
const HOLD_REFILL_WINDOW_SLOTS = Math.round(HOLD_REFILL_WINDOW_H * 4); // gates 1+3: hours → slots

export type Action = 'charge' | 'discharge' | 'idle' | 'hold';

export interface OptimizerSlot {
  startTime: string;
  buyPrice: number; // öre/kWh — priceIncludingTaxAndSurcharge + 71 öre tax/transfer
  sellPrice: number; // öre/kWh — price received per exported kWh: spot + EXPORT_BONUS_ORE (grid benefit), see DOMAIN.md
  solarKwh: number; // expected solar production for this 15-min slot
  consumptionKwh?: number; // expected household load for this 15-min slot (optional; defaults to 0)
  /**
   * The load this slot should be RESERVED against — a measured high quantile of the same hour across
   * the trailing window (lib/telemetry/readings.ts), not a forecast. When present the DP plans
   * against `max(consumptionKwh × loadFactor, this)`; when absent it falls back to the flat
   * `loadFactor` alone, which is the previous behaviour exactly.
   *
   * Why both exist. `consumptionKwh` is the honest expectation: it is what gets logged and what
   * forecast-vs-actual validation scores. The reserve is a different question — "how bad does this
   * hour get?" — and a single global margin answers it identically for 03:00 and for the dinner peak,
   * which do not have remotely the same spread. See LOAD_FORECAST_MARGIN and OPTIMIZER-AUDIT.md G4.
   */
  consumptionReserveKwh?: number;
  // Provenance tags, informational only — the DP never reads these, they exist purely so
  // logged telemetry (optimizer_runs.inputs_json) can tell a real forecast miss apart from a
  // climatology-fallback slot when validating solarKwh/consumptionKwh against actual readings
  // later. 'typical'/'baseline' means the live forecast wasn't available for that slot, so a
  // large error there reflects climatology, not Open-Meteo's forecast skill.
  // 'live' (added 2026-07-18) means consumptionKwh came from the trailing per-hour profile
  // measured from the house's own readings (lib/load.ts slotConsumptionFromLive) — the best
  // available estimator, so errors there are genuine household unpredictability.
  solarSource?: 'forecast' | 'stale' | 'typical';
  loadSource?: 'modeled' | 'baseline' | 'live';
  priceSource?: 'actual' | 'forecast';
  // The price provider's own uncertainty band for this slot, as a half-width in öre/kWh on the BUY
  // side (see lib/prices.ts). Logged, NOT read by the DP — same status as the provenance tags
  // above. It exists so the flat FORECAST_RISK_PREMIUM_ORE can eventually be replaced by a
  // per-slot, measured figure: the raw band is far too wide to use as a surcharge directly
  // (PlaneraElen's low/high reaches ±97 öre/kWh three days out), so relating band width to
  // realised forecast error needs the error distribution this field is here to collect.
  // See OPTIMIZER-AUDIT.md P1-1 and Phase M.
  priceUncertaintyOre?: number;
}

export interface DispatchSlot {
  startTime: string;
  action: Action;
  gridKwh: number; // net grid exchange: +ve = import (load + battery charge), -ve = export
  solarExportKwh: number; // solar surplus exported to grid this slot
  // Grid-flow attribution: which part of the slot's grid exchange the BATTERY is responsible
  // for, vs the house. Net gridKwh alone can't distinguish "buying for the battery" from "load
  // import during a solar-funded charge", and the chart's decision zones
  // (lib/chart-utils.ts classifyBand) need exactly that distinction. Like gridKwh and socAfter
  // these describe the margined plan (see opts.loadFactor below).
  batteryToGridKwh: number; // battery energy exported to grid — a deliberate sell
  gridToBatteryKwh: number; // grid energy charged into the battery — a deliberate buy
  batteryToLoadKwh: number; // battery energy covering house load — default self-use behaviour
  loadFromGridKwh: number; // house load left for the grid to cover
  socAfter: number; // battery SoC in kWh after slot completes
  batterySaveDriftKwh?: number; // empirical save-policy drift applied to the forecast
}

// Even a ~450 W household load is enough to observe the calibrated ~220 W auto drain.
const BATTERY_SAVE_HOLD_LOAD_KWH = 0.05;


function batterySaveHoldOutputKwh(soc: number, loadRem: number, driftW: number, minSocKwh: number): number {
  if (!(driftW > 0) || loadRem <= BATTERY_SAVE_HOLD_LOAD_KWH) return 0;
  if (soc <= minSocKwh) return 0;
  return Math.min(driftW / 4000, loadRem, Math.max(0, soc - minSocKwh) * ONE_WAY_EFF);
}

/**
 * Wear cost (öre) for moving SoC by `dE` this slot, charged on the energy that actually passes
 * through the CELLS.
 *
 * Two corrections over the previous `wear × |dE|`:
 *
 *  1. **Throughput, not stored delta.** Charging `dE` into the pack pushes `dE / η` through the
 *     conversion path and discharging it yields `dE · η`, so |dE| is the one quantity that is
 *     neither. Charging was undercharged and discharging overcharged, each by the per-leg loss.
 *  2. **`freeSolarKwh` is exempt.** Solar the inverter absorbs on its own (the auto-charge floor)
 *     is not a decision the DP can decline, so pricing it as wear taxes an action the model does not
 *     control — which biased the DP against holding a battery that free solar was filling anyway.
 *     Only the portion above that floor is a real choice.
 *
 * Both are ~2 % effects on a ≈4.2 öre/leg charge, so this changes no headline behaviour; it is here
 * because a cost function that charges for what the model cannot choose is a trap for whoever tunes
 * BATTERY_WEAR_COST_ORE_PER_KWH next. See OPTIMIZER-AUDIT.md G9.
 */
function wearOreFor(dE: number, freeSolarKwh = 0): number {
  if (dE >= 0) {
    const chargeable = Math.max(0, dE - freeSolarKwh);
    return BATTERY_WEAR_COST_ORE_PER_KWH * (chargeable / ONE_WAY_EFF);
  }
  return BATTERY_WEAR_COST_ORE_PER_KWH * -dE * ONE_WAY_EFF;
}

interface Flows {
  gridImport: number;
  gridExport: number;
  solarExport: number; // solar (not battery) exported to grid — for the chart
  batteryToGrid: number; // battery-attributed share of gridExport
  gridToBattery: number; // battery-attributed share of gridImport
  batteryToLoad: number; // battery energy delivered to house load
  loadFromGrid: number; // load-attributed share of gridImport
  feasible: boolean;
}

/**
 * Physical grid flows + feasibility for moving SoC from `soc` to `socNext` across one slot.
 * Solar serves the house first (free); then the battery charges/discharges; the grid makes up the
 * balance. Excess solar that neither fits the battery nor the export cap is curtailed. The battery
 * may discharge to cover load AND export surplus to the grid (up to the grid cap).
 */
function computeFlows(
  soc: number,
  socNext: number,
  solarRem: number,
  loadRem: number,
  /** Curtail solar rather than export it — see the `sellPrice < 0` note in dpCore. Defaults to
   *  false so every existing caller and the oracle keep their exact behaviour. */
  curtailSolar = false,
): Flows {
  const dE = socNext - soc;
  let gridImport: number;
  let gridExport: number;
  let solarExport: number;
  let batteryToGrid: number;
  let gridToBattery: number;
  let batteryToLoad: number;
  let loadFromGrid: number;

  if (dE >= 0) {
    // charge (or idle): store dE, needs dE / η at the battery input
    const need = dE / ONE_WAY_EFF;
    const fromSolar = Math.min(solarRem, need);
    gridToBattery = need - fromSolar;
    loadFromGrid = loadRem;
    batteryToGrid = 0;
    batteryToLoad = 0;
    gridImport = gridToBattery + loadRem;
    solarExport = solarRem - fromSolar;
    // Curtailment is FREE and always available: an inverter can simply produce less. So surplus the
    // battery cannot absorb is exported only when exporting actually pays — at a negative sell price
    // the DP used to be forced to pay to dump it, which is not a real constraint, just a missing
    // option. (The cap branch below was already a curtailment; this generalises it from "more than
    // the grid can take" to "more than it is worth exporting".)
    if (curtailSolar) solarExport = 0;
    gridExport = solarExport;
    if (gridExport > GRID_SLOT_KWH) {
      // curtail solar we can neither store nor export within the grid cap
      solarExport = GRID_SLOT_KWH;
      gridExport = GRID_SLOT_KWH;
    }
  } else {
    // discharge: deliver (−dE)·η out of the battery — to load first, then export
    const out = -dE * ONE_WAY_EFF;
    const toLoad = Math.min(out, loadRem);
    batteryToLoad = toLoad;
    batteryToGrid = out - toLoad;
    gridToBattery = 0;
    loadFromGrid = loadRem - toLoad;
    gridImport = loadFromGrid;
    // Same option on the discharge side: the SOLAR share is curtailable, the battery's is not (that
    // is a deliberate sell the DP chose, and it would not choose it at a price worth curtailing).
    solarExport = curtailSolar ? 0 : solarRem;
    gridExport = solarExport + batteryToGrid;
  }

  const feasible = gridImport <= GRID_SLOT_KWH + 1e-9 && gridExport <= GRID_SLOT_KWH + 1e-9;
  return { gridImport, gridExport, solarExport, batteryToGrid, gridToBattery, batteryToLoad, loadFromGrid, feasible };
}

/**
 * Optimal battery dispatch via dynamic programming over a discretised state of charge.
 *
 * Unlike a greedy per-slot rule, this sees the whole horizon in order: it charges from the
 * cheapest hours (and free solar), holds energy only where it beats every alternative use, and
 * discharges — to cover load or export to the grid — where it is worth the most. Because it plans
 * the full SoC trajectory it never sells cheaply then rebuys dear, and never over-holds a full
 * battery it can't use. Terminal value is 0 (leftover energy above the floor is spent down by the
 * end of the known horizon), with the hard MIN_SOC floor enforced throughout.
 *
 * 'idle' does NOT mean "SoC holds steady" — it means "leave the inverter on auto/self-use,"
 * and a real hybrid inverter always charges the battery from surplus solar before exporting it
 * (see autoChargeInputKwh below). So an idle slot's socAfter still rises whenever there's solar
 * surplus beyond load; only a rise ABOVE that free/automatic amount gets labeled 'charge', since
 * only that portion requires actively forcing extra input (typically from the grid). Fixed
 * 2026-07-03 — the previous version assumed idle meant zero charge / 100% export, which
 * understated real SoC growth on sunny days and was the main driver of the SoC-divergence guard
 * (dispatch_loop.py) firing far more than genuine plan staleness ever would.
 *
 * @param slots     per-slot prices/solar/load
 * @param startSoc  current battery energy (kWh); defaults to half capacity
 * @param opts.endSoc  optional terminal constraint: the trajectory must END within one
 *   discretisation step of this SoC instead of enjoying the free terminal-value-0 drain.
 *   Exists for the hindsight-oracle comparison (lib/oracle.ts): constraining the oracle to the
 *   achieved end-of-day SoC isolates *within-day* dispatch quality from the *inter-day* choice
 *   of what SoC to hand the next morning — without it the oracle "wins" by spending energy the
 *   real system deliberately carried overnight, which is a horizon artifact, not real regret.
 *   Throws if the target is unreachable from startSoc (cannot happen for a target taken from a
 *   real trajectory obeying the same physics, but a caller-supplied fantasy target can).
 * @param opts.loadFactor  robust-planning margin: every slot's consumptionKwh is multiplied
 *   by this factor BEFORE the solar-first netting, so the plan stays feasible when real load
 *   runs hotter than forecast (see LOAD_FORECAST_MARGIN in lib/constants.ts for the full
 *   rationale — a point-forecast optimum has zero slack by construction, and the cost of
 *   running short is far larger than the cost of over-holding). Default 1 (no margin): the
 *   hindsight oracle re-dispatches ACTUAL load and must never carry a robustness margin, and
 *   existing tests pin the unmargined optimum. The emitted gridKwh/socAfter describe the
 *   margined plan — the honest forecast is what gets logged to telemetry (lib/plan.ts), so
 *   forecast-vs-actual validation is not polluted by the deliberate margin.
 * @param opts.deferralRateOrePerKwhHour  risk-aware planning (added 2026-07-19, see
 *   DEFERRAL_RATE_ORE_PER_KWH_HOUR in lib/constants.ts for the incident + full rationale):
 *   surcharge the battery's grid-facing actions (grid-funded charge, battery-to-grid sell) by
 *   `rate × max(0, deferralHorizonHours − hoursFromNow)` öre/kWh in the backward pass, so among
 *   near-equal prices the action lands as late as possible and stays cancellable by the next
 *   replan. Bounded and measured from NOW rather than from the horizon's end — see
 *   DEFERRAL_HORIZON_HOURS for why that distinction cost the feature its usefulness. When set, the DP
 *   runs twice — with and without the bias — and the deferred plan is kept only if it gives up
 *   at most opts.maxRiskSacrificeOre (default MAX_RISK_SACRIFICE_ORE) of TRUE-priced value vs the
 *   risk-free optimum, so a genuinely better early price is never deferred over. That guard covers
 *   ALL the risk knobs together, not just this one.
 *   Load-covering discharges are deliberately NOT biased: their timing is dictated by when the
 *   load occurs, and biasing them would fight the reserve logic for no option-value gain.
 * @param opts.solarRiskPremiumOre  risk-aware planning (see SOLAR_RISK_PREMIUM_ORE_PER_KWH):
 *   surcharge grid-funded charging by `premium × min(1, forecastSolarInTheNextFewHours /
 *   batteryHeadroom)` öre/kWh, so a buy that a plausible solar over-delivery would make
 *   redundant must clear a real risk margin, not any thin positive edge. Zero whenever no
 *   forecast solar is ahead within the lookahead window (winter nights), so genuine arbitrage is
 *   untouched. The window (opts.solarRiskLookaheadHours) matters: summed over a multi-day horizon
 *   the ratio saturates and the premium becomes a flat tax on charging.
 *   Both knobs are planning-only and default OFF: the oracle re-dispatches actual data in
 *   hindsight, where deferral/uncertainty have no meaning — biasing it would poison the
 *   regret-≥0 invariant (lib/oracle.ts). Only lib/plan.ts sets them, for live plans.
 */
/**
 * What a kWh still sitting in the battery at the end of the horizon is worth (öre per STORED kWh).
 *
 * The DP's terminal value used to be a flat 0, which is not a small simplification — it means
 * stored energy is worthless the moment the known prices run out, so the optimum spends the battery
 * down to the floor at the end of every horizon. Measured 2026-09-10: every scenario, every season,
 * every horizon length ended at exactly MIN_SOC. That is tolerable only while the horizon reliably
 * reaches 2-3 days ahead, and it does not: a PlaneraElen outage, an unreleased day-ahead before
 * 13:00, or (until the previous commit) a DST day or one missing forecast slot collapses it to
 * today, at which point "terminal value 0" means "empty the battery tonight".
 *
 * The value here is derived, not tuned: a stored kWh will, after the horizon, displace an import
 * somewhere. Valued at the CHEAPEST import in view, converted at the discharge leg's efficiency and
 * net of the wear that discharge will cost:
 *
 *     value = min(buyPrice over the horizon) × η − wear,  floored at 0
 *
 * This is deliberately the provably-safe LOWER BOUND on what the energy is worth, and the two
 * properties that make it safe are worth stating, because both are easy to break by "improving"
 * the formula:
 *
 *  1. **It cannot induce hoarding by buying.** Storing a kWh bought in the cheapest slot costs
 *     `minBuy / η`; the credit is `minBuy × η − wear`. Since η < 1, `minBuy / η > minBuy × η`, so a
 *     grid buy made purely to collect the credit is always a loss. The DP will still carry energy
 *     it already has, and still buy for a real future need — never for the credit itself. Any
 *     higher figure (an average, a percentile, or — as this function briefly did — a floor at the
 *     horizon's best SELL price) breaks this: measured on a synthetic winter horizon, a
 *     `max(minBuy, maxSell)` variant credited 134.5 öre/kWh against a 108 öre cheapest import and
 *     promptly filled the battery from the grid for 15 kr of imaginary value.
 *  2. **It cannot induce dumping.** Exporting a stored kWh earns `sell × η`; carrying it earns
 *     `minBuy × η − wear`. At this site the buy/sell wedge (VAT + skatt/överföring) is ~80 öre, so
 *     carrying wins comfortably — which is the actual fix here. With a zero terminal value, a
 *     battery holding more energy than the horizon's remaining load could absorb exported the
 *     surplus at whatever the last slots' sell price happened to be, for free.
 *
 * **What it does NOT fix**, and should not be expected to: it cannot stop the plan spending the
 * battery on tonight's cheap load instead of tomorrow morning's expensive load, because by
 * construction the credit is below every buy price in the horizon, so covering load always wins.
 * Fixing that needs a value ABOVE the cheapest slot, which means giving up property 1 in exchange
 * for an assumption about prices nobody has seen — a trade to make from replay data (see
 * OPTIMIZER-AUDIT.md Phase M), not from taste.
 *
 * Planning-only, like loadFactor and the risk premiums: the hindsight oracle re-dispatches actual
 * data and must never carry it, or the regret ≥ 0 invariant stops meaning anything.
 */
export function terminalEnergyValueOre(slots: OptimizerSlot[]): number {
  if (slots.length === 0) return 0;
  let minBuy = Infinity;
  for (const slot of slots) if (slot.buyPrice < minBuy) minBuy = slot.buyPrice;
  return Math.max(0, minBuy * ONE_WAY_EFF - BATTERY_WEAR_COST_ORE_PER_KWH);
}

/**
 * Per-kWh surcharge on the battery's GRID-FACING energy in a slot whose price is only a forecast.
 *
 * This replaced `planningPrices`, which added the premium to `buyPrice` and subtracted it from
 * `sellPrice` on forecast slots. That looks like the same thing and is not, because the DP uses
 * `buyPrice` for two different jobs: costing an import, and — through the cost-to-go — valuing the
 * energy already in the battery, whose worth IS the import it will avoid. Raising `buyPrice` on
 * forecast days therefore made carrying energy into them look *better* than it is, i.e. the
 * "conservative" knob was a hoarding incentive. Measured 2026-09-10 on a two-day horizon (day 1
 * settled, day 2 forecast): with the premium on, the plan parked the battery at 100 % at midnight in
 * every price variant tried instead of ending at 5.7-7.2 kWh, and the true value came out slightly
 * WORSE each time. 100 % overnight is also the worst SoC for calendar ageing and leaves no headroom
 * for the next morning's PV.
 *
 * As a surcharge on the transaction it discourages exactly what uncertainty should discourage —
 * buying and selling on a price nobody has settled yet — while leaving the value of stored energy at
 * the honest point forecast. Load-covering discharges are untouched, same reasoning as the deferral
 * bias: their timing is dictated by when the load happens.
 *
 * The MAGNITUDE is still a flat constant per forecast slot. `OptimizerSlot.priceUncertaintyOre`
 * carries the provider's own per-slot band for the refit that should replace it (see
 * OPTIMIZER-AUDIT.md P1-1 / Phase M); it is logged, not yet consumed, because the raw band is far
 * too wide to use as a surcharge directly (PlaneraElen's low/high reaches ±97 öre/kWh on a
 * three-day-out slot) and calibrating it needs the forecast-error distribution, not a guess.
 */
function forecastRiskOre(slot: OptimizerSlot, premiumOre: number): number {
  return slot.priceSource === 'forecast' && premiumOre > 0 ? premiumOre : 0;
}

export function optimizeDispatch(
  slots: OptimizerSlot[],
  startSoc: number = BATTERY_KWH / 2,
  opts?: {
    endSoc?: number;
    loadFactor?: number;
    deferralRateOrePerKwhHour?: number;
    /** Hours from now over which the deferral bias still discriminates; beyond it the surcharge is
     *  0. Defaults to DEFERRAL_HORIZON_HOURS. */
    deferralHorizonHours?: number;
    /** Cap (öre, whole plan) on the TRUE value all the risk knobs together may give up. Defaults to
     *  MAX_RISK_SACRIFICE_ORE. */
    maxRiskSacrificeOre?: number;
    solarRiskPremiumOre?: number;
    solarRiskSurvival?: boolean;
    deferralUntilNeeded?: boolean;
    holdEnabled?: boolean;
    /** Hours ahead over which the solar risk premium counts forecast solar. Defaults to
     *  SOLAR_RISK_LOOKAHEAD_HOURS. */
    solarRiskLookaheadHours?: number;
    forecastRiskPremiumOre?: number;
    batterySaveDriftW?: number;
    /** Site-specific economic reserve. Never lowers the physical safety minimum. */
    minSocKwh?: number;
    /** öre per stored kWh above the floor at the horizon's end (see terminalEnergyValueOre).
     *  0/omitted keeps the historical terminal-value-0 behaviour bit-identically, which is what
     *  the hindsight oracle and every existing test rely on. Ignored when endSoc is set — an
     *  explicit terminal constraint already says what the trajectory must end at. */
    terminalValueOrePerKwh?: number;
    /** Annual energy-tax refund credit, only when eligibility and the paid rate are verified. */
    refundCreditOre?: (startTime: string) => number;
  },
): DispatchSlot[] {
  const refundOre = opts?.refundCreditOre;
  if (!refundOre || slots.length === 0) return planWithRiskGuard(slots, startSoc, opts);

  // Keep our site's risk-sacrifice guard, physical constraints, forecast premium, and
  // terminal-value economics in BOTH candidates. A notional credit must never change the
  // plan unless the conservatively-attributable actual refund improves total value.
  const credit = slots.map((s) => (s.solarKwh < SURVIVAL_SOLAR_SLOT_KWH ? Math.max(0, refundOre(s.startTime)) : 0));
  const plain = planWithRiskGuard(slots, startSoc, opts);
  if (credit.every((c) => c === 0)) return plain;
  const creditedSlots = slots.map((s, i) => (
    credit[i] > 0 ? { ...s, sellPrice: s.sellPrice + credit[i] } : s
  ));
  const credited = planWithRiskGuard(creditedSlots, startSoc, opts);
  const pricing = {
    loadFactor: opts?.loadFactor,
    terminalValueOrePerKwh: opts?.terminalValueOrePerKwh,
    minSocKwh: opts?.minSocKwh,
    useConsumptionReserve: true,
  };
  const score = (d: DispatchSlot[]) =>
    evaluateDispatch(slots, d, startSoc, pricing).valueOre +
      refundEarnedOre(d, credit, Math.max(BATTERY_MIN_SOC_KWH, opts?.minSocKwh ?? BATTERY_MIN_SOC_KWH));
  return score(credited) > score(plain) + 1e-6 ? credited : plain;
}

/** Conservative grid-origin tracking for *potential* energy-tax refunds.
 *  Starting energy and solar are NOT credited; grid import is credited net of
 *  charging loss; household discharge consumes credited stock before any export.
 *  This is deliberately conservative until measured provenance can be audited.
 */
export function refundEarnedOre(
  dispatch: DispatchSlot[], creditOre: number[], minSocKwh: number = BATTERY_MIN_SOC_KWH,
): number {
  let gridPoolDcKwh = 0;
  let earnedOre = 0;
  for (let i = 0; i < dispatch.length; i++) {
    const slot = dispatch[i];
    gridPoolDcKwh += Math.max(0, slot.gridToBatteryKwh) * ONE_WAY_EFF;
    // Assume household consumption draws grid-origin energy first, to avoid over-crediting.
    gridPoolDcKwh = Math.max(0, gridPoolDcKwh - Math.max(0, slot.batteryToLoadKwh) / ONE_WAY_EFF);
    const eligibleDcKwh = Math.min(gridPoolDcKwh, Math.max(0, slot.batteryToGridKwh) / ONE_WAY_EFF);
    gridPoolDcKwh -= eligibleDcKwh;
    earnedOre += eligibleDcKwh * ONE_WAY_EFF * (creditOre[i] ?? 0);
    gridPoolDcKwh = Math.min(gridPoolDcKwh, Math.max(0, slot.socAfter - minSocKwh));
  }
  return earnedOre;
}

function planWithRiskGuard(
  slots: OptimizerSlot[],
  startSoc: number,
  opts?: Parameters<typeof dpCore>[2] & { maxRiskSacrificeOre?: number },
): DispatchSlot[] {
  // Two-pass sacrifice guard. Every risk knob is a planning fiction — a surcharge or a price
  // haircut that exists to buy robustness, not because the money is real — so measure what the
  // whole set of them actually costs: solve once with all of them off, once as configured, price
  // both with the DP's own true-price arithmetic and refuse the cautious plan beyond the cap.
  //
  // It covered the DEFERRAL BIAS ONLY until 2026-09-10, which left the two larger knobs
  // unmeasured: a 20 öre/kWh solar premium and a 10 öre price haircut could give up any amount of
  // real value with nothing checking. See MAX_RISK_SACRIFICE_ORE.
  const riskKnobsActive =
    (opts?.deferralRateOrePerKwhHour ?? 0) > 0 ||
    (opts?.solarRiskPremiumOre ?? 0) > 0 ||
    (opts?.forecastRiskPremiumOre ?? 0) > 0;
  if (riskKnobsActive && slots.length > 0) {
    const fearless = dpCore(slots, startSoc, {
      ...opts,
      deferralRateOrePerKwhHour: 0,
      solarRiskPremiumOre: 0,
      forecastRiskPremiumOre: 0,
    });
    const cautious = dpCore(slots, startSoc, opts);
    const cap = opts?.maxRiskSacrificeOre ?? MAX_RISK_SACRIFICE_ORE;
    // Priced on the problem the two plans were actually SOLVED against — same load margin, same
    // terminal value. Pricing them at unmargined load (as this did until 2026-09-10) meant neither
    // trajectory was optimal under the comparison metric, so the "sacrifice" measured accounting
    // drift as much as risk cost, and could even come out negative. See evaluateDispatch.
    const pricing = {
      loadFactor: opts?.loadFactor,
      terminalValueOrePerKwh: opts?.terminalValueOrePerKwh,
      useConsumptionReserve: true,
    };
    const sacrifice =
      evaluateDispatch(slots, fearless, startSoc, pricing).valueOre -
      evaluateDispatch(slots, cautious, startSoc, pricing).valueOre;
    return sacrifice <= cap ? cautious : fearless;
  }
  return dpCore(slots, startSoc, opts);
}

function dpCore(
  slots: OptimizerSlot[],
  startSoc: number,
  opts?: {
    endSoc?: number;
    loadFactor?: number;
    deferralRateOrePerKwhHour?: number;
    deferralHorizonHours?: number;
    solarRiskPremiumOre?: number;
    solarRiskSurvival?: boolean;
    deferralUntilNeeded?: boolean;
    holdEnabled?: boolean;
    solarRiskLookaheadHours?: number;
    forecastRiskPremiumOre?: number;
    batterySaveDriftW?: number;
    minSocKwh?: number;
    terminalValueOrePerKwh?: number;
  },
): DispatchSlot[] {
  const n = slots.length;
  if (n === 0) return [];
  const MIN_SOC_KWH = Math.max(BATTERY_MIN_SOC_KWH, Math.min(BATTERY_KWH, opts?.minSocKwh ?? DEFAULT_MIN_SOC_KWH));

  const floorSoc = Math.min(MIN_SOC_KWH, startSoc); // allow a start below the floor; never discharge below it
  const step = (BATTERY_KWH - floorSoc) / (SOC_LEVELS - 1);
  const socOf = (i: number) => floorSoc + i * step;
  const idxOf = (soc: number) =>
    Math.max(0, Math.min(SOC_LEVELS - 1, Math.round((soc - floorSoc) / step)));
  const minSocLevel = Math.ceil((MIN_SOC_KWH - floorSoc) / step - 1e-9); // lowest level that is ≥ MIN_SOC
  const maxDelta = Math.max(1, Math.floor(BATTERY_SLOT_KWH / step)); // reachable SoC levels per slot
  // The auto-charge floor below rounds `soc + autoInput·η` to the nearest level, which must stay
  // within maxDelta of `soc` or the search would find NO legal j ≥ s on a bright slot and be
  // forced into a discharge it never wanted. η < 1 guarantees it for any sane constants; assert
  // rather than trust, since both sides move with BATTERY_MAX_KW and SOC_LEVELS.
  if (Math.round((BATTERY_SLOT_KWH * ONE_WAY_EFF) / step) > maxDelta) {
    throw new Error(
      `optimizeDispatch: auto-charge floor (${BATTERY_SLOT_KWH} kWh/slot at eff ${ONE_WAY_EFF}) is unreachable within maxDelta=${maxDelta} at step ${step} kWh`,
    );
  }

  // Solar-first netting per slot, plus the energy input the battery gets for free from
  // solar surplus whenever the inverter is left on auto/self-use (i.e. NOT actively
  // forced into a discharge) — a real hybrid inverter always prioritizes charging the
  // battery from surplus over exporting it, so "idle" cannot mean "zero charge, export
  // everything" the way the DP used to assume. autoChargeInputKwh is capped the same way
  // any other charge/discharge flow is (BATTERY_SLOT_KWH) — the battery/inverter can't absorb
  // faster than its own throughput limit regardless of how much solar is available.
  const loadFactor = opts?.loadFactor ?? 1;
  const solarRem = new Float64Array(n);
  const loadRem = new Float64Array(n);
  const autoChargeInputKwh = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    // The greater of the flat margin and the measured high quantile. `max` rather than replacing the
    // margin: the quantile is measured from RECENT days, so a cold snap the trailing window has not
    // seen yet is still covered by the margin on top of the weather-scaled expectation.
    const marginedLoad = (slots[i].consumptionKwh ?? 0) * loadFactor;
    const reserve = slots[i].consumptionReserveKwh;
    const load = reserve !== undefined ? Math.max(marginedLoad, reserve) : marginedLoad;
    const s2l = Math.min(slots[i].solarKwh, load);
    solarRem[i] = slots[i].solarKwh - s2l;
    loadRem[i] = load - s2l;
    autoChargeInputKwh[i] = Math.min(solarRem[i], BATTERY_SLOT_KWH);
  }

  // Risk-aware planning surcharges (see optimizeDispatch's opts docs; both default 0 = off,
  // and a 0 rate contributes exactly 0.0 öre to every transition, so the plain DP is
  // bit-identical to before these existed). deferOre[i] is the per-kWh earliness surcharge on
  // grid-facing battery actions at slot i; suffixSolarKwh[i] is the RAW forecast solar still
  // to come STRICTLY AFTER slot i (own-slot solar can't surprise a slot that grid-buys — a
  // buying slot has already consumed its surplus, need > sr). Raw, NOT load-netted surplus,
  // deliberately: the premium prices FORECAST ERROR, and error scales with how much solar the
  // forecast still has in play — a gloomy 6 kWh-day forecast can net to zero surplus and still
  // deliver 3-5× and fill the battery (the 2026-07-19 incident did exactly that); netted
  // surplus would price that day as risk-free, blind on precisely the highest-error days.
  const deferralRate = opts?.deferralRateOrePerKwhHour ?? 0;
  const deferralHorizonH = opts?.deferralHorizonHours ?? DEFERRAL_HORIZON_HOURS;
  const solarRiskPremiumOre = opts?.solarRiskPremiumOre ?? 0;
  const deferOre = new Float64Array(n);
  // Forecast solar strictly AFTER slot i, but only within the lookahead window — not the whole
  // horizon. Over the entire (now up to 3-day) horizon the ratio below saturates its own min(1, …)
  // several times over, so the premium degenerated into a flat 20 öre/kWh on every grid-funded
  // charge from roughly March to October, which is not what "prices forecast error" means. See
  // SOLAR_RISK_LOOKAHEAD_HOURS.
  const windowSolarKwh = new Float64Array(n + 1);
  const suffixSolarKwh = new Float64Array(n + 1);
  for (let i = n - 1; i >= 0; i--) {
    // Earliness measured from NOW (slot 0 — lib/plan.ts slices the horizon at the current slot
    // before solving), and zero beyond deferralHorizonH. Measuring it from the horizon's END, as
    // this did until 2026-09-10, made the surcharge grow with however many days of prices the feed
    // happened to return: 9.5 öre/kWh at one day, 28.7 at three. Since it multiplies the ENERGY
    // charged and not just its timing, that turned a tie-breaker into a flat tax on grid charging
    // — and one the sacrifice guard then rejected wholesale on every plan. See
    // DEFERRAL_HORIZON_HOURS in lib/constants.ts.
    deferOre[i] = deferralRate * Math.max(0, deferralHorizonH - 0.25 * i);
    suffixSolarKwh[i] = suffixSolarKwh[i + 1] + slots[i].solarKwh;
  }
  const solarWindowSlots = Math.max(
    1,
    Math.round((opts?.solarRiskLookaheadHours ?? SOLAR_RISK_LOOKAHEAD_HOURS) * 4),
  );
  for (let i = 0; i <= n; i++) {
    windowSolarKwh[i] = suffixSolarKwh[i] - suffixSolarKwh[Math.min(n, i + solarWindowSlots)];
  }

  // Need-window deferral: cap by our existing 12-hour horizon, even when the forecast
  // deficit that needs this energy would happen much later.
  const needWindow = deferralRate > 0 && opts?.deferralUntilNeeded === true;
  const prefixDeficit = new Float64Array(n + 1);
  if (needWindow) {
    for (let i = 0; i < n; i++) prefixDeficit[i + 1] = prefixDeficit[i] + loadRem[i];
  }
  const hoursUntilNeeded = (i: number, held: number): number => {
    const target = prefixDeficit[i + 1] + held;
    if (prefixDeficit[n] <= target) return 0.25 * (n - 1 - i);
    let lo = i + 1;
    let hi = n - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (prefixDeficit[mid + 1] > target) hi = mid;
      else lo = mid + 1;
    }
    return 0.25 * (lo - i);
  };

  // A grid-funded buy competes with the next solar only if still stored at its arrival.
  const survival = solarRiskPremiumOre > 0 && opts?.solarRiskSurvival === true;
  const deficitToSolarKwh = new Float64Array(n);
  if (survival) {
    let acc = 0;
    for (let i = n - 1; i >= 0; i--) {
      deficitToSolarKwh[i] = acc;
      acc = slots[i].solarKwh >= SURVIVAL_SOLAR_SLOT_KWH ? 0 : acc + loadRem[i];
    }
  }

  // Hold gates (opt-in). Precompute per-slot whether the DP MAY freeze SoC and export solar
  // surplus at slot i instead of forcing it into the battery. Gate 1 (prices heading down) + gate 2
  // (gross PV > 3 kW) are pure slot conditions → holdSlotEligible[i]. Gate 3 (refill guarantee) also
  // depends on SoC and is tested in the s-loop against refillableSolarKwh[i]. No-op when holdEnabled=false
  // (default): the arrays are zeros and holdOK short-circuits, so the DP is bit-identical to before.
  const holdEnabled = opts?.holdEnabled ?? false;
  const holdSlotEligible = new Uint8Array(n);
  const refillableSolarKwh = new Float64Array(n);
  if (holdEnabled) {
    for (let i = 0; i < n; i++) {
      const pvOk = slots[i].solarKwh > HOLD_MIN_PV_KWH; // gate 2 (gross PV)
      // gate 1: sell price now ≥ cheapest BUY within the refill window ahead + margin
      let minBuyAhead = Infinity;
      const g1End = Math.min(n, i + 1 + HOLD_REFILL_WINDOW_SLOTS);
      for (let k = i + 1; k < g1End; k++) if (slots[k].buyPrice < minBuyAhead) minBuyAhead = slots[k].buyPrice;
      const priceDropOk =
        Number.isFinite(minBuyAhead) && slots[i].sellPrice - minBuyAhead >= HOLD_PRICE_DROP_MARGIN_ORE;
      holdSlotEligible[i] = pvOk && priceDropOk ? 1 : 0;
      // gate 3 (left-hand side): chargeable solar over the next 12 h — load-netted, capped to the
      // battery throughput limit, efficiency-corrected. The cap prevents curtailed energy from
      // overestimating refill.
      let refill = 0;
      const g3End = Math.min(n, i + HOLD_REFILL_WINDOW_SLOTS);
      for (let k = i; k < g3End; k++) refill += Math.min(solarRem[k], BATTERY_SLOT_KWH) * ONE_WAY_EFF;
      refillableSolarKwh[i] = refill;
    }
  }

  // Backward pass: costToGo[level] = min öre cost from the next slot onward at that SoC level.
  // Terminal value: a credit (negative cost) for energy left above the floor. 0 = the historical
  // "leftover energy is worthless" behaviour. See terminalEnergyValueOre for why the figure is
  // derived from the horizon's own cheapest import rather than tuned.
  let costToGo = new Float64Array(SOC_LEVELS);
  const terminalValue = opts?.terminalValueOrePerKwh ?? 0;
  if (opts?.endSoc === undefined && terminalValue > 0) {
    for (let level = 0; level < SOC_LEVELS; level++) {
      costToGo[level] = -Math.max(0, socOf(level) - MIN_SOC_KWH) * terminalValue;
    }
  }
  if (opts?.endSoc !== undefined) {
    // Terminal constraint: only the target's own grid level may end the horizon (Infinity
    // elsewhere propagates backward and prunes every path that misses it). Exactly ONE level,
    // not a ±1 band: transitions land exactly on levels, so any single level is reachable, and
    // a band would hand the constrained run one grid step of free extra drain — ~a step×buyPrice
    // of phantom "intraday regret" on every scored day. The target snaps to the grid (≤ half a
    // step from the requested SoC) and clamps to the enforceable range: never below the
    // discharge floor, never above capacity.
    const target = Math.max(minSocLevel, Math.min(SOC_LEVELS - 1, idxOf(opts.endSoc)));
    costToGo.fill(Infinity);
    costToGo[target] = 0;
  }
  const choice: Int16Array[] = new Array(n);
  const driftChoice: Uint8Array[] = new Array(n);

  for (let i = n - 1; i >= 0; i--) {
    const cur = new Float64Array(SOC_LEVELS);
    const ch = new Int16Array(SOC_LEVELS);
    const driftCh = new Uint8Array(SOC_LEVELS);
    // Honest point forecasts. The forecast-price risk is charged on the TRANSACTION below
    // (forecastRiskOre), never folded into these — see forecastRiskOre for why the difference is
    // the whole point.
    const buy = slots[i].buyPrice;
    const sell = slots[i].sellPrice;
    const forecastOre = forecastRiskOre(slots[i], opts?.forecastRiskPremiumOre ?? 0);

    const sr = solarRem[i];
    const lr = loadRem[i];
    const autoInput = autoChargeInputKwh[i];
    // Exporting at a negative price costs money, and curtailing is free, so the surplus is simply
    // not exported. Without this the DP had no way to decline: `optimizer.test.ts` called it "the
    // curtailment gap" and pinned the old, forced-loss behaviour. Only solar is curtailable — a
    // battery-to-grid flow is a deliberate sell the DP would not choose at a negative price anyway.
    const curtail = sell < 0;

    for (let s = 0; s < SOC_LEVELS; s++) {
      const soc = socOf(s);
      const lo = Math.max(0, s - maxDelta);
      const hi = Math.min(SOC_LEVELS - 1, s + maxDelta);
      // The lowest reachable non-discharge level: the battery cannot end up BELOW what
      // free solar auto-charges it to unless something actively discharges it instead
      // (handled separately, j < s below) — see autoChargeInputKwh's definition above.
      const naturalFloor = idxOf(Math.min(soc + autoInput * ONE_WAY_EFF, BATTERY_KWH));
      // Hold allowed at (i, s)? gate 1+2 (slot level) AND gate 3 (refill ≥ 1.1× headroom).
      // When true the DP may choose j ∈ [s, naturalFloor) = freeze/partially store and export the surplus.
      const holdOK =
        holdEnabled &&
        holdSlotEligible[i] === 1 &&
        refillableSolarKwh[i] >= HOLD_REFILL_MARGIN * Math.max(0, BATTERY_KWH - soc);
      // Solar-redundancy premium for grid-buying AT THIS state: scales with how much of the
      // remaining headroom the strictly-future raw forecast solar could fill on its own (see
      // suffixSolarKwh above for why raw, not netted). Headroom is this slot's proxy for
      // "what the buy competes with" — planned discharges between now and the solar's arrival
      // can open more room, so this slightly UNDER-counts the risk; acceptable for a heuristic
      // priced in öre. Denominator floored at one grid step to keep a near-full battery from
      // dividing by ~0 (ratio caps at 1 regardless).
      const solarRiskOre =
        solarRiskPremiumOre > 0
          ? solarRiskPremiumOre *
            Math.min(1, windowSolarKwh[i + 1] / Math.max(BATTERY_KWH - soc, step))
          : 0;
      let best = Infinity;
      let bestJ = s;
      // An initially depleted battery must actively climb back toward the
      // PLANNING reserve, even on a flat-price day. The old guard only
      // prevented j<s below the floor: it allowed j=s forever at 12%, so the
      // planner displayed a permanently empty battery and fought recovery.
      // If the reserve cannot be reached within this 15-minute slot, require
      // only the maximum FEASIBLE charge after house load, never a fake target.
      const availableChargeInput = Math.min(
        BATTERY_SLOT_KWH, Math.max(0, GRID_SLOT_KWH - lr) + sr,
      );
      const maxChargeLevel = Math.min(
        SOC_LEVELS - 1,
        Math.floor((soc + availableChargeInput * ONE_WAY_EFF - floorSoc) / step + 1e-9),
      );
      const minAllowedLevel = s < minSocLevel
        ? (PLANNER_RESERVE_ENABLED
          ? Math.min(minSocLevel, Math.max(s, maxChargeLevel))
          : s) // legacy behavior: never discharge below physical floor, but don't force a buy
        : minSocLevel;
      for (let j = lo; j <= hi; j++) {
        if (j < minAllowedLevel) continue; // protect reserve, including when starting below it
        if (j >= s && j < naturalFloor && !holdOK) continue; // auto-charge floor; relaxed when hold is gated ON
        // No FORCED charge above the safety ceiling — force_charge would refuse it and return to
        // auto. Anything up to naturalFloor is free solar the inverter absorbs on its own, so it is
        // exempt: this bounds what we command, not what the battery can physically hold.
        if (j > naturalFloor && socOf(j) > MAX_FORCED_SOC_KWH) continue;
        const dE = socOf(j) - soc;
        let gImp: number;
        let gExp: number;
        let riskOre: number; // deferral + redundancy surcharge on the battery's grid-facing share
        if (dE >= 0) {
          const need = dE / ONE_WAY_EFF;
          const fromSolar = need < sr ? need : sr;
          const gridToBattery = need - fromSolar;
          gImp = gridToBattery + lr;
          gExp = curtail ? 0 : sr - fromSolar;
          if (gExp > GRID_SLOT_KWH) gExp = GRID_SLOT_KWH;
          let premiumKwh = gridToBattery;
          if (survival && gridToBattery > 0) {
            const after = socOf(j) - deficitToSolarKwh[i];
            const without = after - gridToBattery * ONE_WAY_EFF;
            const survives = Math.max(MIN_SOC_KWH, after) - Math.max(MIN_SOC_KWH, without);
            premiumKwh = Math.min(gridToBattery, Math.max(0, survives) / ONE_WAY_EFF);
          }
          const buyDeferOre =
            needWindow && gridToBattery > 0
              ? deferralRate * Math.min(
                  Math.max(0, deferralHorizonH - 0.25 * i),
                  hoursUntilNeeded(i, Math.max(0, soc - MIN_SOC_KWH)),
                )
              : deferOre[i];
          riskOre = gridToBattery * (buyDeferOre + forecastOre) + premiumKwh * solarRiskOre;
        } else {
          const out = -dE * ONE_WAY_EFF;
          const toLoad = out < lr ? out : lr;
          gImp = lr - toLoad;
          gExp = (curtail ? 0 : sr) + (out - toLoad);
          if (gExp > GRID_SLOT_KWH + 1e-9) continue; // over-export infeasible
          // Only the battery-to-grid share is deferral-biased: covering load (toLoad) happens
          // when the load happens — there is no "later" to defer it to.
          riskOre = (out - toLoad) * (deferOre[i] + forecastOre);
        }
        if (gImp > GRID_SLOT_KWH + 1e-9) continue; // over-import infeasible
        // Wear on the energy that really passes through the cells, exempting the free solar the
        // inverter absorbs regardless — see wearOreFor. Zero when j === s (idle, dE = 0).
        const wear = wearOreFor(dE, autoInput * ONE_WAY_EFF);
        const cost = gImp * buy - gExp * sell + wear + riskOre + costToGo[j];
        if (cost < best) {
          best = cost;
          bestJ = j;
        }
      }
      const holdOutput = batterySaveHoldOutputKwh(soc, lr, opts?.batterySaveDriftW ?? 0, MIN_SOC_KWH);
      if (holdOutput > 0) {
        const requestedSocAfter = soc - holdOutput / ONE_WAY_EFF;
        const holdJ = Math.max(0, Math.floor((requestedSocAfter - floorSoc) / step));
        const holdSocAfter = socOf(holdJ);
        const actualHoldOutput = Math.max(0, soc - holdSocAfter) * ONE_WAY_EFF;
        const holdF = computeFlows(soc, holdSocAfter, sr, lr, curtail);
        // The battery-save hold is an empirical correction for physical inverter behaviour.
        // It is not an optional arbitrage action: represent it whenever the economic choice is
        // idle, while a genuinely better active charge/discharge decision still wins.
        if (holdJ >= minSocLevel && holdJ < s && actualHoldOutput > 0 && holdF.feasible && bestJ === s) {
          best =
            holdF.gridImport * buy -
            holdF.gridExport * sell +
            wearOreFor(holdSocAfter - soc) +
            costToGo[holdJ];
          bestJ = holdJ;
          driftCh[s] = 1;
        }
      }
      cur[s] = best;
      ch[s] = bestJ;
    }
    choice[i] = ch;
    driftChoice[i] = driftCh;
    costToGo = cur;
  }

  // After the backward pass costToGo holds slot-0 values; an infinite cost at the start state
  // means no feasible path reaches the terminal constraint (only possible with opts.endSoc).
  if (opts?.endSoc !== undefined && !Number.isFinite(costToGo[idxOf(startSoc)])) {
    throw new Error(
      `optimizeDispatch: endSoc ${opts.endSoc} kWh is unreachable from startSoc ${startSoc} kWh in ${n} slots`,
    );
  }

  // Forward pass: walk the optimal SoC path from startSoc and emit dispatch.
  const result: DispatchSlot[] = new Array(n);
  let s = idxOf(startSoc);
  for (let i = 0; i < n; i++) {
    const j = choice[i][s];
    const soc = socOf(s);
    const socNext = socOf(j);
    const driftOutput = driftChoice[i][s]
      ? Math.max(0, soc - socNext) * ONE_WAY_EFF
      : 0;
    const actualSocNext = socNext;
    const f = computeFlows(soc, actualSocNext, solarRem[i], loadRem[i], slots[i].sellPrice < 0);
    const dE = actualSocNext - soc;
    // 'idle' means "leave the inverter on auto/self-use" — free, no grid-funded CHARGING,
    // whenever the chosen socNext is reachable from solar alone. Deliberately NOT based on
    // f.gridImport: that also counts grid power drawn purely to cover house load (lr), which
    // has nothing to do with whether the battery is being actively charged — a load-only
    // slot with dE = 0 must stay 'idle' even though gridImport > 0. gridForCharging isolates
    // just the charging-side grid draw (need beyond what solarRem covers).
    //
    // Also deliberately NOT a level-index comparison against the natural floor used for
    // feasibility above: SOC_LEVELS discretises the state space at ~0.12 kWh resolution, so
    // the search can land one grid point above the true continuous natural amount purely
    // from rounding, pulling in a sub-resolution sliver of "charging" that is not a real
    // decision — tolerate up to one discretization step before calling it an actively forced
    // charge, since a genuine arbitrage import is always much larger than the model's own
    // snap-to-grid noise.
    let action: Action;
    if (driftOutput > 0) {
      action = 'idle';
    } else if (dE < -1e-6) {
      action = 'discharge';
    } else {
      const need = dE / ONE_WAY_EFF;
      const gridForCharging = Math.max(0, need - Math.min(solarRem[i], need));
      // 'hold' = solar surplus that auto/idle WOULD have stored but is exported here: SoC stayed
      // below the natural floor despite surplus. Can only happen when the auto-charge block has
      // been relaxed (holdOK), so the label is unambiguous. '- step' guards against discretization noise.
      const naturalFloorSoc = Math.min(soc + Math.min(solarRem[i], BATTERY_SLOT_KWH) * ONE_WAY_EFF, BATTERY_KWH);
      if (gridForCharging > step) action = 'charge';
      else if (f.solarExport > 1e-6 && socNext < naturalFloorSoc - step) action = 'hold';
      else action = 'idle';
    }
    result[i] = {
      startTime: slots[i].startTime,
      action,
      gridKwh: f.gridImport - f.gridExport,
      solarExportKwh: f.solarExport,
      batteryToGridKwh: f.batteryToGrid,
      gridToBatteryKwh: f.gridToBattery,
      batteryToLoadKwh: f.batteryToLoad,
      loadFromGridKwh: f.loadFromGrid,
      socAfter: actualSocNext,
      batterySaveDriftKwh: driftOutput > 0 ? driftOutput / ONE_WAY_EFF : undefined,
    };
    s = j;
  }
  return result;
}

export interface DispatchEconomics {
  cashOre: number; // öre — grid income minus grid cost over the horizon; positive = net income
  wearOre: number; // öre — BATTERY_WEAR_COST_ORE_PER_KWH × total |ΔSoC| (battery throughput)
  valueOre: number; // cashOre − wearOre — the exact quantity the DP maximises (negated cost)
}

/**
 * Price a dispatch trajectory with the DP's own arithmetic: the same solar-first netting and
 * computeFlows physics the backward pass costs transitions with, so
 * `evaluateDispatch(slots, optimizeDispatch(slots, s, o), s, o)` is the optimum's value and any
 * other feasible trajectory over the same slots evaluates ≤ it. Exists for the hindsight-oracle
 * comparison (lib/oracle.ts), where oracle/constrained/continuation trajectories must all be
 * valued on one identical basis — pricing them with separate ad-hoc arithmetic would make the
 * regret numbers reflect accounting drift, not dispatch quality.
 *
 * **`opts` must describe the same problem the trajectory was PLANNED against**, and that is not a
 * detail. The deferral sacrifice guard in `optimizeDispatch` compares two trajectories both solved
 * against `consumptionKwh × loadFactor` (1.15 in production) while pricing them here at unmargined
 * load, so neither was optimal under the metric being applied and the difference was not a
 * "sacrifice" at all. Measured 2026-09-10 on a three-day shoulder-season horizon: the guard's own
 * number was 152.6 öre against the 196.4 öre it costs on the problem actually solved — a 44 öre
 * discrepancy against a 50 öre cap, and in another scenario the same mismatch produced a *negative*
 * sacrifice, i.e. the biased plan scoring better than the optimum it was compared with. See
 * OPTIMIZER-AUDIT.md P1-3.
 *
 * Defaults reproduce the previous behaviour exactly: `loadFactor` 1 and no terminal value. The
 * hindsight oracle relies on that — it re-dispatches ACTUAL load and must never carry a planning
 * margin or a terminal credit, or the regret ≥ 0 invariant stops meaning anything.
 *
 * `dispatch` may be a prefix of a longer trajectory's slots (e.g. the first-day slice of a
 * 48 h oracle run); only `dispatch.length` slots are valued.
 *
 * Only `socAfter` is read, and the parameter says so: the oracle also prices the trajectory the
 * battery ACTUALLY followed (reconstructed from measured SoC — there are no planned flow
 * attributions for a real day), and inventing zeroed DispatchSlot fields to satisfy the type
 * would be a lie about data we don't have.
 */
export function evaluateDispatch(
  slots: OptimizerSlot[],
  dispatch: Pick<DispatchSlot, 'socAfter'>[],
  startSoc: number,
  opts?: {
    /** Multiply every slot's consumptionKwh by this before netting — pass the same factor the
     *  trajectory was planned against. Default 1. */
    loadFactor?: number;
    /** öre per stored kWh left above the floor at the end of `dispatch`, credited to the value.
     *  Pass the same figure the plan was solved with, or omit for the raw cash-and-wear value. */
    terminalValueOrePerKwh?: number;
    minSocKwh?: number;
    /** Use measured high-quantile load reserve ONLY when comparing two planning candidates.
     *  Default false preserves honest forecast valuation and hindsight-oracle scoring. */
    useConsumptionReserve?: boolean;
  },
): DispatchEconomics {
  const loadFactor = opts?.loadFactor ?? 1;
  let cashOre = 0;
  let wearOre = 0;
  let soc = startSoc;
  for (let i = 0; i < dispatch.length; i++) {
    // Default: honest forecast valuation. For a risk/refund candidate comparison, explicitly
    // use the same high-quantile planning reserve the DP was solved against.
    const marginedLoad = (slots[i].consumptionKwh ?? 0) * loadFactor;
    const reserve = opts?.useConsumptionReserve ? slots[i].consumptionReserveKwh : undefined;
    const load = reserve !== undefined ? Math.max(marginedLoad, reserve) : marginedLoad;
    const s2l = Math.min(slots[i].solarKwh, load);
    const solarRem = slots[i].solarKwh - s2l;
    const f = computeFlows(soc, dispatch[i].socAfter, solarRem, load - s2l, slots[i].sellPrice < 0);
    cashOre += f.gridExport * slots[i].sellPrice - f.gridImport * slots[i].buyPrice;
    // Same basis as the DP's own cost function, or the optimum would not evaluate as the optimum.
    wearOre += wearOreFor(
      dispatch[i].socAfter - soc,
      Math.min(solarRem, BATTERY_SLOT_KWH) * ONE_WAY_EFF,
    );
    soc = dispatch[i].socAfter;
  }
  const terminalOre =
    Math.max(0, soc - Math.max(BATTERY_MIN_SOC_KWH, opts?.minSocKwh ?? BATTERY_MIN_SOC_KWH)) *
    (opts?.terminalValueOrePerKwh ?? 0);
  return { cashOre, wearOre, valueOre: cashOre - wearOre + terminalOre };
}
