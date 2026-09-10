// Regenerate lib/irradiance-data.ts from Open-Meteo's ERA5 reanalysis archive, for sites where
// the nearest measured SMHI global-irradiance station is too far to represent the local climate
// (the reference install used a real station via process-smhi-data.ts — see DOMAIN.md's
// "Adapting to a new site", step 1). ERA5 gives GHI at the EXACT coordinates instead, at the
// cost of being modelled rather than measured.
//
// Usage:
//   node scripts/tools/build-irradiance-openmeteo.mjs <lat> <lon> [startYear] [endYear] [tiltDeg] [azimuthDegFromNorth]
//   (or set SITE_LATITUDE/SITE_LONGITUDE/SOLAR_TILT_DEG/SOLAR_AZIMUTH_DEG in the environment;
//    years default to 2005..2024)
//
// Coordinates and geometry are REQUIRED, not defaulted: this script used to fall back to the
// original reference site's Goteborg coordinates and a hardcoded azimuth, and running it without
// arguments is exactly how lib/irradiance-data.ts came to hold 20 years of climatology for the
// wrong location and the wrong panel orientation (OPTIMIZER-AUDIT.md P0-1). It now refuses to
// run rather than silently generating plausible-looking data for somebody else's roof.
//
// AZIMUTH CONVENTION: pass azimuthDegFromNorth in the SAME convention lib/constants.ts's
// SOLAR_ARRAYS uses (0 = north, 180 = south). The script converts to Open-Meteo's convention
// (0 = south) itself, mirroring apiAzimuth() in lib/solar.ts. Do not pre-convert.
// Needs network access (api.open-meteo.com archive). Writes lib/irradiance-data.ts, matching
// process-smhi-data.ts's output format byte-for-byte so downstream (lib/solar.ts,
// build-solar-calibration.ts) is unaffected.
//
// Timestamp convention: Open-Meteo's hourly `global_tilted_irradiance` value at label T is the mean
// over the PRECEDING hour [T-1, T) — the same hour-ENDING convention SMHI's hourly radiation
// uses. process-smhi-data.ts buckets SMHI's label hour directly with no shift, so we do the same
// here (index h = mean panel-plane irradiance over UTC [h-1, h)); this keeps the diurnal curve aligned with how
// slot-utils.ts / solar.ts already consume the array. Do NOT add the one-hour shift that
// forecast.ts applies — that maps onto interval-START-keyed slots, a different indexing.

import * as fs from 'node:fs';
import * as path from 'node:path';

function required(name, argv, envName) {
  const raw = argv ?? process.env[envName];
  const value = raw === undefined || raw === '' ? NaN : parseFloat(raw);
  if (!Number.isFinite(value)) {
    console.error(
      `FAILED: ${name} is required — pass it as an argument or set ${envName}.\n` +
        '  usage: node scripts/tools/build-irradiance-openmeteo.mjs <lat> <lon> ' +
        '[startYear] [endYear] [tiltDeg] [azimuthDegFromNorth]',
    );
    process.exit(2);
  }
  return value;
}

const LAT = required('latitude', process.argv[2], 'SITE_LATITUDE');
const LON = required('longitude', process.argv[3], 'SITE_LONGITUDE');
const START_YEAR = parseInt(process.argv[4] ?? '2005', 10);
const END_YEAR = parseInt(process.argv[5] ?? '2024', 10);
const TILT_DEG = required('tiltDeg', process.argv[6], 'SOLAR_TILT_DEG');
// 0 = north, matching lib/constants.ts SOLAR_ARRAYS.azimuthDeg.
const AZIMUTH_DEG = required('azimuthDegFromNorth', process.argv[7], 'SOLAR_AZIMUTH_DEG');
// Open-Meteo wants 0 = south — same conversion as apiAzimuth() in lib/solar.ts.
const API_AZIMUTH = ((AZIMUTH_DEG - 180 + 540) % 360) - 180;

const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

const sums = Array.from({ length: 12 }, () =>
  Array.from({ length: 24 }, () => ({ total: 0, count: 0 })),
);
// Horizontal irradiance from the SAME request (Open-Meteo takes several hourly vars in one
// call, so this costs no extra round-trips). Only used to emit poaOverGhiByMonth below — the
// monthly plane-of-array / horizontal transposition ratio that lib/metno-thredds.ts needs,
// because MET Norway's Thredds grid publishes horizontal shortwave radiation while every other
// path in this repo now works in panel-plane terms.
const ghiSums = Array.from({ length: 12 }, () => ({ poa: 0, ghi: 0 }));

async function fetchYear(year) {
  const url =
    `https://archive-api.open-meteo.com/v1/archive?latitude=${LAT}&longitude=${LON}` +
    `&start_date=${year}-01-01&end_date=${year}-12-31` +
    `&hourly=global_tilted_irradiance,shortwave_radiation&tilt=${TILT_DEG}&azimuth=${API_AZIMUTH}&timezone=GMT`;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      const times = json?.hourly?.time;
      const irradiance = json?.hourly?.global_tilted_irradiance;
      const horizontal = json?.hourly?.shortwave_radiation;
      if (!Array.isArray(times) || !Array.isArray(irradiance)) {
        throw new Error('unexpected response shape (no hourly.global_tilted_irradiance)');
      }
      if (!Array.isArray(horizontal)) {
        throw new Error('unexpected response shape (no hourly.shortwave_radiation)');
      }
      let used = 0;
      for (let i = 0; i < times.length; i++) {
        const v = irradiance[i];
        if (v == null || Number.isNaN(v)) continue;
        const month = parseInt(times[i].slice(5, 7), 10) - 1;
        const hourUtc = parseInt(times[i].slice(11, 13), 10);
        if (month < 0 || month > 11 || hourUtc < 0 || hourUtc > 23) continue;
        sums[month][hourUtc].total += v;
        sums[month][hourUtc].count += 1;
        // Ratio is accumulated as a sum-over-sum, not a mean of per-hour ratios: the point is to
        // convert an energy total, and averaging ratios would let near-zero twilight hours (where
        // the ratio is large and the energy negligible) dominate.
        const h = horizontal[i];
        if (h != null && !Number.isNaN(h)) {
          ghiSums[month].poa += v;
          ghiSums[month].ghi += h;
        }
        used++;
      }
      return used;
    } catch (err) {
      if (attempt === 3) throw err;
      await new Promise((r) => setTimeout(r, 1500 * attempt));
    }
  }
}

const run = async () => {
  console.log(`Open-Meteo ERA5 panel plane (tilt ${TILT_DEG}°, azimuth ${AZIMUTH_DEG}° from north = ${API_AZIMUTH}° API) @ ${LAT}, ${LON} — years ${START_YEAR}..${END_YEAR}`);
  for (let year = START_YEAR; year <= END_YEAR; year++) {
    const used = await fetchYear(year);
    console.log(`  ${year}: ${used} hourly samples`);
  }

  const avgGhiByMonthHour = sums.map((monthSums) =>
    monthSums.map(({ total, count }) => (count > 0 ? Math.round((total / count) * 10) / 10 : 0)),
  );
  const avgDailyGhiByMonth = avgGhiByMonthHour.map((hours) =>
    Math.round(hours.reduce((s, v) => s + v, 0) * 10) / 10,
  );
  const poaOverGhiByMonth = ghiSums.map(({ poa, ghi }) =>
    ghi > 0 ? Math.round((poa / ghi) * 1000) / 1000 : 1,
  );

  const lines = [
    '// Generated by scripts/tools/build-irradiance-openmeteo.mjs — do not edit by hand',
    `// Source: Open-Meteo ERA5 reanalysis archive @ ${LAT}, ${LON}, ${START_YEAR}–${END_YEAR} (hourly mean panel-plane irradiance, tilt ${TILT_DEG}°, azimuth ${AZIMUTH_DEG}° from north = ${API_AZIMUTH}° in Open-Meteo's 0=south convention)`,
    '// avgGhiByMonthHour[month-1][hourUtc] = average panel-plane irradiance (W/m²)',
    '',
    'export const avgGhiByMonthHour: number[][] = [',
    ...avgGhiByMonthHour.map((hours, mi) => `  [${hours.join(', ')}], // ${MONTH_NAMES[mi]}`),
    '];',
    '',
    '// Average daily panel-plane irradiance total (Wh/m²/day) by month',
    `export const avgDailyGhiByMonth: number[] = [${avgDailyGhiByMonth.join(', ')}];`,
    `// Months: ${MONTH_NAMES.join(', ')}`,
    '',
    '// Panel-plane / horizontal irradiance ratio per month, from the same ERA5 window: how much',
    '// more (or less) this plane collects than a flat surface. Used to transpose a HORIZONTAL',
    '// irradiance source into panel-plane terms — lib/metno-thredds.ts, whose MET Nordic grid',
    '// publishes horizontal shortwave radiation while everything else here is already panel-plane.',
    `export const poaOverGhiByMonth: number[] = [${poaOverGhiByMonth.join(', ')}];`,
    `// Months: ${MONTH_NAMES.join(', ')}`,
    '',
  ];

  const outPath = path.join(process.cwd(), 'lib', 'irradiance-data.ts');
  fs.writeFileSync(outPath, lines.join('\n'), 'utf8');
  console.log(`\nWritten: ${outPath}`);
  console.log('\nSanity checks:');
  console.log(`  Jun UTC 10 (≈ noon Stockholm):  ${avgGhiByMonthHour[5][10]} W/m²  (expect ~300–600)`);
  console.log(`  Dec UTC 11 (≈ noon Stockholm):  ${avgGhiByMonthHour[11][11]} W/m²  (expect ~20–80)`);
  console.log(`  Jun daily total: ${avgDailyGhiByMonth[5]} Wh/m²/day  (expect ~4000–6000)`);
  console.log(`  Dec daily total: ${avgDailyGhiByMonth[11]} Wh/m²/day  (expect ~200–500)`);
  console.log(`  POA/GHI Jun: ${poaOverGhiByMonth[5]}  Dec: ${poaOverGhiByMonth[11]}  (a tilted plane gains most in winter)`);
};

run().catch((err) => {
  console.error('FAILED:', err.message);
  process.exit(1);
});
