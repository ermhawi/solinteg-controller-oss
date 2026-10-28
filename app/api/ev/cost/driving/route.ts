import {
  costAuthFailure, costResponse, drivingSummary, readEvCostReport, timeRange,
} from '@/lib/ev-cost';


export async function GET(request: Request) {
  const denial = costAuthFailure(request);
  if (denial) return denial;
  const range = timeRange(request);
  if (!range) return costResponse({ error: 'Invalid UTC from/to range' }, 400);
  const report = await readEvCostReport();
  if (!report) return costResponse({ error: 'EV cost report unavailable' }, 503);
  const driving = report.driving.filter((row) => {
    const time = Date.parse(row.occurred_at_utc);
    return time >= range.from && time < range.to;
  });
  return costResponse({
    source: report.source,
    schema_version: report.schema_version,
    as_of_utc: report.as_of_utc,
    ledger_revision: report.ledger_revision,
    model: report.model,
    summary: drivingSummary(driving),
    driving,
    writes_to_devices: 0,
  });
}
