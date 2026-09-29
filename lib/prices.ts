import { cacheLife } from 'next/cache';
import { request as httpsRequest } from 'node:https';
import { VAT_RATE, SUPPLIER_SURCHARGE_ORE, PRICE_ZONE, EXPORT_BONUS_ORE, FETCH_TIMEOUT_MS } from './constants';

// Raw Nord Pool day-ahead spot prices per bidding zone (SE2 by default), 15-min resolution, free & no key.
// Publishes tomorrow ~13:00 CET — reliably, unlike the Mölndal feed it replaced (which lagged by
// hours). Per-day URLs: /api/v1/prices/YYYY/MM-DD_<ZONE>.json. Prices are raw spot, excl VAT.
const PRICES_BASE_URL = 'https://www.elprisetjustnu.se/api/v1/prices';

export interface PriceSlot {
  startTime: string; // naive Stockholm local, e.g. "2026-07-01T00:00:00"
  endTime: string;
  price: number; // öre/kWh — the SELL price you actually receive: spot + EXPORT_BONUS_ORE
  priceIncludingTaxAndSurcharge: number; // öre/kWh — (spot + surcharge) × VAT (buy price, pre skatt/överföring)
  source?: 'actual' | 'forecast';
  // Half-width of the provider's own uncertainty band for this slot, in öre/kWh on the BUY side
  // (i.e. the spot band scaled by VAT, since buy = (spot + surcharge) × (1 + VAT)). Present only for
  // forecast rows from a provider that publishes one. Carried through to the optimizer inputs and
  // logged; not yet used in the cost function — see OptimizerSlot.priceUncertaintyOre.
  uncertaintyOre?: number;
}

export interface PriceData {
  today: string;
  tomorrow: string;
  hasTomorrow: boolean;
  maxForMonth: number; // öre — max/min sell price over the LOADED slots (today[+tomorrow]); unused downstream
  minForMonth: number;
  prices: PriceSlot[];
  maxAge: number;
}

interface ElprisSlot {
  SEK_per_kWh: number; // raw spot, excl VAT
  time_start: string; // ISO with offset, e.g. "2026-07-01T00:00:00+02:00"
  time_end: string;
}

interface PlaneraPriceSlot {
  time: string;
  // PlaneraElen's payload is EUR/MWh, matching the API metadata.
  price: number;
  source: 'actual' | 'forecast';
  // Forecast rows also carry an uncertainty band (EUR/MWh). `low`/`high` is the inner band;
  // `very_low`/`very_high` the outer one, which is wide enough to be useless as a planning input
  // (one observed three-day-out row spanned −28.48 to +147.46 EUR/MWh). Only the inner band is read.
  low?: number;
  high?: number;
}

interface PlaneraResponse {
  data?: PlaneraPriceSlot[];
}

/** PlaneraElen's actual day-ahead rows are a valid fallback while the primary feed lags. */
export function isUsablePlaneraSource(source: PlaneraPriceSlot['source']): boolean {
  return source === 'actual' || source === 'forecast';
}

/**
 * Convert an elprisetjustnu slot to our PriceSlot: the tax-inclusive buy price is reconstructed
 * from the raw spot; the sell price is the spot plus the flat grid-export compensation
 * (EXPORT_BONUS_ORE) that the owner actually receives per exported kWh.
 */
export function toSlot(p: ElprisSlot): PriceSlot {
  const spot = Math.round(p.SEK_per_kWh * 100 * 100) / 100; // SEK/kWh → öre/kWh (2 dp)
  const inclTax = Math.round((spot + SUPPLIER_SURCHARGE_ORE) * (1 + VAT_RATE) * 100) / 100;
  return {
    startTime: p.time_start.slice(0, 19), // drop the "+02:00" offset → naive local (matches app convention)
    endTime: p.time_end.slice(0, 19),
    price: Math.round((spot + EXPORT_BONUS_ORE) * 100) / 100, // sell = spot + export compensation
    priceIncludingTaxAndSurcharge: inclTax,
    source: 'actual',
  };
}

export function forecastToSlot(p: PlaneraPriceSlot, eurSekRate = 1): PriceSlot {
  const instant = new Date(p.time);
  const local = stockholmParts(instant);
  const end = new Date(instant.getTime() + 15 * 60_000);
  const endLocal = stockholmParts(end);
  const localTime = (parts: ReturnType<typeof stockholmParts>) =>
    `${parts.dateStr}T${String(parts.hour).padStart(2, '0')}:${String(parts.minute).padStart(2, '0')}:00`;
  const spot = Math.round(p.price * eurSekRate * 0.1 * 100) / 100; // EUR/MWh → SEK/MWh → öre/kWh
  // Band half-width in öre/kWh on the buy side. Only when the provider gives both ends and they are
  // the right way round — a malformed band must degrade to "no band", not to a negative surcharge.
  const bandOre =
    p.low != null && p.high != null && p.high >= p.low
      ? Math.round(((p.high - p.low) / 2) * eurSekRate * 0.1 * (1 + VAT_RATE) * 100) / 100
      : undefined;
  return {
    startTime: localTime(local),
    endTime: localTime(endLocal),
    price: Math.round((spot + EXPORT_BONUS_ORE) * 100) / 100,
    priceIncludingTaxAndSurcharge: Math.round((spot + SUPPLIER_SURCHARGE_ORE) * (1 + VAT_RATE) * 100) / 100,
    source: p.source,
    ...(bandOre !== undefined ? { uncertaintyOre: bandOre } : {}),
  };
}

/** Merge authoritative day-ahead prices with a bounded PlaneraElen forecast horizon. */
export function mergePriceHorizon(
  actual: PriceSlot[],
  forecast: PriceSlot[],
  horizonEndLocal: string,
): { prices: PriceSlot[]; hasTomorrow: boolean; actualSlots: number; forecastSlots: number } {
  const horizonStartLocal = actual[0]?.startTime;
  const byTime = new Map<string, PriceSlot>();
  for (const slot of forecast) {
    if (slot.startTime < horizonEndLocal && (!horizonStartLocal || slot.startTime >= horizonStartLocal)) {
      byTime.set(slot.startTime, slot);
    }
  }
  for (const slot of actual) if (slot.startTime < horizonEndLocal) byTime.set(slot.startTime, { ...slot, source: 'actual' });
  const prices = [...byTime.values()].sort((a, b) => a.startTime.localeCompare(b.startTime));
  const actualSlots = prices.filter((p) => p.source === 'actual').length;
  const firstDate = prices[0]?.startTime.slice(0, 10);
  return {
    prices,
    hasTomorrow: prices.some((p) => p.source === 'actual' && p.startTime.slice(0, 10) !== firstDate),
    actualSlots,
    forecastSlots: prices.length - actualSlots,
  };
}

/**
 * How many 15-min slots a given Stockholm calendar date really has: 96 on an ordinary day, 92 on
 * the spring-forward day (23 h), 100 on the fall-back day (25 h).
 *
 * Derived from the two real midnight instants rather than from a DST rule, for the same reason
 * `currentSlotIndexInPrices` is: the feed's transition days genuinely arrive with 92/100 slots, and
 * hand-rolled offset arithmetic is one more thing to get wrong twice a year. Lives here because
 * this is the module that owns `stockholmMidnightUtc`; lib/horizon.ts consumes it.
 */
export function slotsInStockholmDay(date: string): number {
  const [y, m, d] = date.split('-').map(Number);
  const start = stockholmMidnightUtc(y, m - 1, d);
  // Normalize the next date before passing it to stockholmMidnightUtc, which checks the
  // round-tripped calendar components. Passing d + 1 directly crosses month-end with an
  // unnormalized month/day pair; in CEST the fallback can then make a normal day look 25 h.
  const nextDate = new Date(Date.UTC(y, m - 1, d + 1));
  const next = stockholmMidnightUtc(
    nextDate.getUTCFullYear(),
    nextDate.getUTCMonth(),
    nextDate.getUTCDate(),
  );
  return Math.round((next.getTime() - start.getTime()) / 900_000);
}

/** Longest run of consecutive missing slots this will reconstruct. One hour: long enough to cover
 *  the ordinary case (a provider dropping a row or two), short enough that a real outage is
 *  reported as a shorter horizon instead of being papered over with invented prices. */
const MAX_FILL_SLOTS = 4;

/**
 * Reconstruct short gaps in the merged price horizon by linear interpolation.
 *
 * Why this exists: `trimIncompleteTerminalDays` (lib/horizon.ts) accepts a future day only if it is
 * complete and contiguous, and the DP has zero terminal value — so ONE missing forecast slot used
 * to cost a whole day of horizon, and an artificially short horizon makes the optimizer deliberately
 * empty the battery at the end of it (OPTIMIZER-AUDIT.md P0-3). Interpolating a 15-minute price
 * between its two neighbours is a far smaller error than losing a day of look-ahead.
 *
 * Filled slots are marked `source: 'forecast'`, never 'actual': they are inferred, and the planner
 * already prices forecast slots more cautiously than settled ones.
 *
 * Deliberately conservative about WHICH gaps it will fill:
 *   - both neighbours on the same local date, so nothing is invented across a midnight boundary;
 *   - the label gap must be exactly `(missing + 1) x 15` minutes AND the date must be an ordinary
 *     96-slot day: on the two DST-transition days the naive labels genuinely jump (01:45 -> 03:00)
 *     or repeat, and an interpolated label there would be a lie about which hour it is;
 *   - at most MAX_FILL_SLOTS in a row.
 * Anything else is left as a gap for the horizon trimmer to shorten the plan at.
 *
 * The interpolation runs on the raw SPOT price and re-derives both fields from it, rather than
 * interpolating the two derived numbers independently — that keeps `sell = spot + EXPORT_BONUS_ORE`
 * and `buy = (spot + SUPPLIER_SURCHARGE_ORE) x (1 + VAT_RATE)` exactly true for the filled slot, which
 * is an invariant the rest of the app relies on (see DOMAIN.md).
 */
export function fillPriceGaps(prices: PriceSlot[]): { prices: PriceSlot[]; filled: number } {
  if (prices.length < 2) return { prices, filled: 0 };
  const spotOf = (p: PriceSlot) => p.price - EXPORT_BONUS_ORE;
  const out: PriceSlot[] = [prices[0]];
  let filled = 0;

  for (let i = 1; i < prices.length; i++) {
    const prev = prices[i - 1];
    const next = prices[i];
    const date = prev.startTime.slice(0, 10);
    const sameDay = date === next.startTime.slice(0, 10);
    // Never on a DST-transition date. A one-hour label jump there is the clock changing, not a
    // hole, and 01:45 -> 03:00 looks exactly like four missing slots to plain label arithmetic.
    const ordinaryDay = sameDay && slotsInStockholmDay(date) === 96;
    const stepMin = minutesOfDay(next.startTime) - minutesOfDay(prev.startTime);
    const missing = stepMin / 15 - 1;
    if (ordinaryDay && Number.isInteger(missing) && missing >= 1 && missing <= MAX_FILL_SLOTS) {
      const fromSpot = spotOf(prev);
      const toSpot = spotOf(next);
      for (let k = 1; k <= missing; k++) {
        const spot = fromSpot + ((toSpot - fromSpot) * k) / (missing + 1);
        const startMin = minutesOfDay(prev.startTime) + k * 15;
        out.push(interpolatedSlot(prev.startTime.slice(0, 10), startMin, spot));
        filled++;
      }
    }
    out.push(next);
  }
  // Identity when nothing was filled: callers can then treat an unchanged array as unchanged,
  // and the common case allocates nothing.
  return filled > 0 ? { prices: out, filled } : { prices, filled: 0 };
}

function minutesOfDay(startTime: string): number {
  return parseInt(startTime.slice(11, 13), 10) * 60 + parseInt(startTime.slice(14, 16), 10);
}

function interpolatedSlot(date: string, startMin: number, spot: number): PriceSlot {
  const label = (min: number) =>
    `${date}T${String(Math.floor(min / 60)).padStart(2, '0')}:${String(min % 60).padStart(2, '0')}:00`;
  const rounded = Math.round(spot * 100) / 100;
  return {
    startTime: label(startMin),
    endTime: label(startMin + 15),
    price: Math.round((rounded + EXPORT_BONUS_ORE) * 100) / 100,
    priceIncludingTaxAndSurcharge:
      Math.round((rounded + SUPPLIER_SURCHARGE_ORE) * (1 + VAT_RATE) * 100) / 100,
    source: 'forecast',
  };
}

/**
 * Fetch PlaneraElen without Next's patched fetch implementation. This is deliberately a direct
 * Node HTTPS request: the forecast changes during the day, and the normal fetch cache returned an
 * older forecast even when the route itself was dynamic and marked no-store.
 */
async function fetchPlaneraPrices(): Promise<PriceSlot[]> {
  const url = new URL(`https://api.planeraelen.se/api/price-data?area=${PRICE_ZONE}`);
  url.searchParams.set('_', `${Date.now()}-${Math.random().toString(36).slice(2)}`);
  const signal = AbortSignal.timeout(FETCH_TIMEOUT_MS);
  const payload = await new Promise<PlaneraResponse>((resolve, reject) => {
    const req = httpsRequest(
      url,
      {
        method: 'GET',
        headers: {
          'User-Agent': 'solinteg-controller',
          'Cache-Control': 'no-cache',
          Pragma: 'no-cache',
          Accept: 'application/json',
        },
        signal,
      },
      (res) => {
        const chunks: Buffer[] = [];
        res.on('data', (chunk: Buffer) => chunks.push(chunk));
        res.on('end', () => {
          const body = Buffer.concat(chunks).toString('utf8');
          if ((res.statusCode ?? 500) < 200 || (res.statusCode ?? 500) >= 300) {
            reject(new Error(`PlaneraElen forecast failed: HTTP ${res.statusCode ?? 500}`));
            return;
          }
          try {
            resolve(JSON.parse(body) as PlaneraResponse);
          } catch {
            reject(new Error('PlaneraElen forecast returned invalid JSON'));
          }
        });
        res.on('error', reject);
      },
    );
    req.on('error', reject);
    req.end();
  });
  if (!Array.isArray(payload.data)) throw new Error('PlaneraElen forecast response has no data array');
  // Keep both kinds of rows. The primary Nord Pool-derived feed remains authoritative when it
  // has a slot, but PlaneraElen may publish tomorrow as `actual` before that feed does. Dropping
  // those rows leaves a hole in the chart and falsely reports that tomorrow is unavailable.
  const usable = payload.data.filter((p) => isUsablePlaneraSource(p.source));
  let eurSekRate = EURSEK_FALLBACK;
  try {
    // The auction for tomorrow's delivery ran today, so today's fixing is the relevant one.
    eurSekRate = await fetchEurSekRate(new Date());
  } catch (err) {
    console.warn(
      `EUR/SEK rate unavailable; using fallback ${EURSEK_FALLBACK} — every forecast price is scaled by this:`,
      err,
    );
  }
  return usable.map((p) => forecastToSlot(p, eurSekRate));
}

/** Fallback EUR/SEK used only when the rate service is unreachable. A wrong rate scales every
 *  forecast price, so this being hit is worth seeing in the log — see the warn at the call site. */
const EURSEK_FALLBACK = 11.5;

/**
 * EUR/SEK for converting PlaneraElen's EUR/MWh forecast to öre/kWh.
 *
 * Asks for the rate on the AUCTION date (the day before delivery) rather than "latest". Nord Pool
 * runs the day-ahead auction in EUR and converts to the local currency at the ECB reference rate
 * fixed for that auction, so the auction date's fixing is the rate the settled price will actually
 * have used — "latest" is a different day's rate whenever a plan is computed after the auction, and
 * on a weekend or holiday it is a stale one regardless.
 *
 * Frankfurter serves ECB reference rates and, for a date with no fixing (weekend/holiday), returns
 * the most recent prior one — which is also what the market uses, so no special-casing is needed.
 * The response's own `date` is logged when it differs from the request, so a stale rate is visible
 * rather than assumed.
 */
async function fetchEurSekRate(auctionDate: Date): Promise<number> {
  const day = auctionDate.toISOString().slice(0, 10);
  const res = await fetch(`https://api.frankfurter.dev/v1/${day}?from=EUR&to=SEK`, {
    headers: { Accept: 'application/json' },
    cache: 'no-store',
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
  });
  if (!res.ok) throw new Error(`EUR/SEK rate failed: HTTP ${res.status}`);
  const payload = (await res.json()) as { rates?: { SEK?: number }; date?: string };
  const rate = payload.rates?.SEK;
  if (typeof rate !== 'number' || !Number.isFinite(rate) || rate <= 0) throw new Error('EUR/SEK response has no valid rate');
  if (payload.date && payload.date !== day) {
    console.info(`EUR/SEK: no ECB fixing for ${day}, using ${payload.date}'s (weekend/holiday)`);
  }
  return rate;
}

/** Fetch one Stockholm day of 15-min slots, or null if not published yet (404 before release). */
export async function fetchDay(dateStr: string): Promise<PriceSlot[] | null> {
  const url = `${PRICES_BASE_URL}/${dateStr.slice(0, 4)}/${dateStr.slice(5)}_${PRICE_ZONE}.json`;
  const res = await fetch(url, {
    headers: { 'User-Agent': 'solinteg-controller' },
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
  });
  if (!res.ok) return null;
  const raw = (await res.json()) as ElprisSlot[];
  if (!Array.isArray(raw) || raw.length === 0) return null;
  return raw.map(toSlot);
}

export function stockholmParts(date: Date) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Europe/Stockholm',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(date);

  const get = (type: string) =>
    parseInt(parts.find((p) => p.type === type)?.value ?? '0', 10);

  const year = get('year');
  const month = get('month'); // 1–12
  const day = get('day');
  const hour = get('hour') % 24; // normalize: some engines return 24 for midnight
  const minute = get('minute');
  const utcOffset = (hour - date.getUTCHours() + 24) % 24;

  return {
    dateStr: `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`,
    hour,
    minute,
    utcOffset,
    year,
    month0: month - 1,
    day,
  };
}

/**
 * Index into a today[+tomorrow] PriceSlot/OptimizerSlot array (PriceData.prices —
 * today's 96 slots, optionally followed by tomorrow's 96 once published) that
 * covers `now`. Used to slice that array down to "from now onward" before handing
 * it to the optimizer, so its own slot 0 lines up with the live SoC reading taken
 * at the same moment — optimizeDispatch's forward pass always treats its index 0
 * as the start of the optimization horizon, so feeding it the whole day (including
 * already-elapsed slots) makes it anchor "now's real SoC" to a fictitious midnight
 * instead of to now, which is what caused a mid-day SoC-divergence bug (fixed
 * 2026-07-03; see dispatch_loop.py's slot_index_for_instant for the other half).
 *
 * Can return a negative index (now is before todayDateStr — stale cache or clock
 * skew) or an index at/beyond the array length (now is past the last loaded day).
 * Callers must clamp before slicing; this only does the date-arithmetic + slot math.
 *
 * Computed as elapsed 15-min slots since todayDateStr's Stockholm midnight, NOT as
 * wall-clock `hour*4 + quarter`: the feed's DST-transition days really have 92
 * (spring) or 100 (fall) slots, so wall-clock arithmetic is off by ±4 array
 * positions for the rest of those days. Elapsed time matches the array layout on
 * every day, because the slots are contiguous 15-min intervals from midnight.
 */
export function currentSlotIndexInPrices(todayDateStr: string, now: Date): number {
  const [ty, tm, td] = todayDateStr.split('-').map(Number);
  const midnight = stockholmMidnightUtc(ty, tm - 1, td);
  return Math.floor((now.getTime() - midnight.getTime()) / 900_000);
}

/**
 * True UTC instant of Stockholm midnight on the given local date. Stockholm is
 * always UTC+1 (CET) or UTC+2 (CEST), and DST transitions happen at 02:00/03:00
 * local — never at midnight — so exactly one of the two candidate offsets
 * round-trips back to 00:00 on that date via stockholmParts.
 * Exported for lib/oracle.ts, whose day windows and elapsed-time slot bucketing
 * are all anchored to this instant (same anchor as currentSlotIndexInPrices).
 */
export function stockholmMidnightUtc(year: number, month0: number, day: number): Date {
  for (const offset of [1, 2]) {
    const candidate = new Date(Date.UTC(year, month0, day, -offset));
    const p = stockholmParts(candidate);
    if (p.year === year && p.month0 === month0 && p.day === day && p.hour === 0 && p.minute === 0) {
      return candidate;
    }
  }
  // Unreachable for real Stockholm dates; keep the CET guess rather than throwing.
  return new Date(Date.UTC(year, month0, day, -1));
}

// Convert a Stockholm clock time on a given Stockholm date to UTC.
// Date.UTC handles overflow (e.g. h=24 wraps to next day correctly).
export function stockholmToUtc(
  year: number,
  month0: number,
  day: number,
  utcOffset: number,
  h: number,
  min: number,
): Date {
  return new Date(Date.UTC(year, month0, day, h - utcOffset, min, 0));
}

/**
 * How long a "tomorrow isn't published yet" answer stays cached once past the 13:05 release
 * window. 20 min pins first sight of the day-ahead prices to a 13:05 + 20 min grid, well after
 * the feed actually has them, and starves any replan trigger that re-asks on a shorter cycle —
 * every re-ask lands in this same cache entry and gets the same pre-release answer back. 3 min
 * costs at most a couple of extra ~100 ms fetches per day against a free feed.
 */
const AWAITING_TOMORROW_RECHECK_S = 3 * 60;

export function computeMaxAge(
  hasTomorrow: boolean,
  now: Date,
  parts: ReturnType<typeof stockholmParts>,
): number {
  if (hasTomorrow) {
    const midnight = stockholmToUtc(parts.year, parts.month0, parts.day, parts.utcOffset, 24, 0);
    return Math.max(60, Math.floor((midnight.getTime() - now.getTime()) / 1000));
  }
  if (parts.hour < 13) {
    // No point fetching before 13:05 — prices for tomorrow are released around 13:00
    const release = stockholmToUtc(parts.year, parts.month0, parts.day, parts.utcOffset, 13, 5);
    return Math.max(60, Math.floor((release.getTime() - now.getTime()) / 1000));
  }
  return AWAITING_TOMORROW_RECHECK_S;
}

/**
 * The real fetch. **No 'use cache' here, deliberately** — this is what lib/plan.ts calls, and a
 * plan is a dispatch input: a stale price horizon is not a slightly-old number, it is the
 * optimizer solving yesterday's problem.
 *
 * On the reference deployment this cost two failed fix attempts before the cause was found. With
 * the price read behind 'use cache', a GET page render and a POST route handler resolved the same
 * argument-less entry differently, so the newest published plan alternated between horizon-aware
 * and today-only for the better part of an hour after each day-ahead release. Tuning cacheLife
 * does not fix it; taking the plan's price read out of the cache does.
 *
 * Cost of not caching: two ~100 ms fetches per plan against a free, unmetered feed, at whatever
 * cadence you render.
 */
export async function fetchPricesUncached(): Promise<PriceData> {

  const now = new Date();
  const parts = stockholmParts(now);

  const [y, m, d] = parts.dateStr.split('-').map(Number);
  const tomorrowStr = new Date(Date.UTC(y, m - 1, d + 1)).toISOString().slice(0, 10);

  const todaySlots = await fetchDay(parts.dateStr);
  if (!todaySlots) throw new Error(`No spot prices for ${parts.dateStr} (${PRICE_ZONE})`);

  // Tomorrow returns null until Nord Pool releases it (~13:00); forecast fills the remainder.
  const tomorrowSlots = await fetchDay(tomorrowStr);
  const actualPrices = tomorrowSlots ? [...todaySlots, ...tomorrowSlots] : todaySlots;
  // Keep three calendar days in view so the day after tomorrow is complete at ordinary times.
  // The horizon helper in lib/plan.ts removes any partial terminal day before optimization.
  const horizonEnd = new Date(now.getTime() + 72 * 60 * 60_000);
  const horizonParts = stockholmParts(horizonEnd);
  const horizonEndLocal = `${horizonParts.dateStr}T${String(horizonParts.hour).padStart(2, '0')}:${String(horizonParts.minute).padStart(2, '0')}:00`;
  let prices = actualPrices;
  let hasTomorrow = tomorrowSlots !== null;
  try {
    const merged = mergePriceHorizon(actualPrices, await fetchPlaneraPrices(), horizonEndLocal);
    prices = merged.prices;
    hasTomorrow = merged.hasTomorrow;
  } catch (err) {
    console.warn('PlaneraElen forecast unavailable; using actual prices only:', err);
  }
  // A single missing slot used to cost a whole day of optimizer horizon (see fillPriceGaps).
  // Logged rather than silent: invented prices reaching the DP is worth being able to see in
  // journalctl, and a persistently gappy provider should be visible as a pattern.
  const gapFilled = fillPriceGaps(prices);
  if (gapFilled.filled > 0) {
    console.warn(
      `fetchPricesUncached: interpolated ${gapFilled.filled} missing price slot(s) to keep the horizon contiguous`,
    );
    prices = gapFilled.prices;
  }
  const maxAge = computeMaxAge(hasTomorrow, now, parts);

  const sells = prices.map((p) => p.price);
  return {
    today: parts.dateStr,
    tomorrow: tomorrowStr,
    hasTomorrow,
    maxForMonth: Math.max(...sells),
    minForMonth: Math.min(...sells),
    prices,
    maxAge,
  };
}

/**
 * Cached wrapper, for display-only callers (app/api/prices/route.ts). **Never call this from
 * anything that feeds dispatch** — see fetchPricesUncached above for why.
 *
 * expire hugs revalidate rather than doubling it: maxAge means "seconds until this answer can be
 * wrong", so a stale-while-revalidate window past it serves an answer already known to be
 * outdated. The +1 is only because cacheLife validates "expire must be greater than revalidate".
 */
export async function fetchPrices(): Promise<PriceData> {
  'use cache';
  const data = await fetchPricesUncached();
  cacheLife({ revalidate: data.maxAge, expire: data.maxAge + 1 });
  return data;
}
