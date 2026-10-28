import { costAuthFailure, costResponse, readEvCostReport } from '@/lib/ev-cost';


export async function GET(request: Request) {
  const denial = costAuthFailure(request);
  if (denial) return denial;
  const report = await readEvCostReport();
  if (!report) return costResponse({ error: 'EV cost report unavailable' }, 503);
  return costResponse({
    source: report.source,
    schema_version: report.schema_version,
    as_of_utc: report.as_of_utc,
    ledger_revision: report.ledger_revision,
    model: report.model,
    inventory: report.inventory,
    summary: report.summary,
    aux_consumed_dc_kwh: report.aux_consumed_dc_kwh,
    direct_aux_ac_kwh: report.direct_aux_ac_kwh,
    direct_aux_known_cost_ore: report.direct_aux_known_cost_ore,
    direct_aux_cost_ore: report.direct_aux_cost_ore,
    direct_aux_unpriced_ac_kwh: report.direct_aux_unpriced_ac_kwh,
    charged_ac_kwh_by_source: report.charged_ac_kwh_by_source,
    event_count: report.event_count,
    writes_to_devices: 0,
  });
}
