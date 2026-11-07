# O06 · Opt-in säker publicering av optimizer_runs

**2026-11-07 — `tested/source`, ej installerad eller aktiverad.** Planens solekonomiska optimering, graf och normal dispatch är oförändrade när `SOLINTEG_STRICT_PLAN_PUBLICATION` saknas eller är `0`. Ett explicit värde `1` aktiverar en ny fail-closed grind före varje `logOptimizerRun`; andra icke-tomma värden ger **NO-PUBLISH** och `invalid-configuration`.

## Säkerhetskontrakt och verifiering

| Given / testad risk | Then vid opt-in | Gräns |
| --- | --- | --- |
| `live.json` saknas eller fallback-SoC | `missing-live-soc` → inga nya optimizer_runs | Äldre writer avstår redan vid `socIsLive=false`, men ett färskt publik UI är inget fysisk kvitto |
| Äldre än **120 s**, mer än **5 s i framtiden**, timestamp utan zon eller invalid | `stale-live-soc` → NO-PUBLISH | Kontrollerar timestampens färskhet, inte mätarens oberoende BMS-kalibrering |
| SoC `NaN`, utanför 0–100 % eller 0–15 kWh | `invalid-soc` → NO-PUBLISH | Batteriets verkliga användbara kapacitet kan avvika; bedöms inte här |
| SQLite `optimizer_runs` saknas, PK/NOT NULL obligatoriska kolumner avviker, DB otillgänglig | `incompatible-schema` → NO-PUBLISH | `getDb()` kan skapa schema vid första anslutning; check är metadata, **inte** ett kvitto att INSERT lyckas eller att dispatcher kan läsa varje framtida rad |
| Tom, ändrad ordning, duplikat, 15-minuterslucka, felaktigt format | `invalid-time-axis`/ `invalid-inputs` → NO-PUBLISH | Normal Stockholm 23h/25h-dag verifieras med faktisk sista söndagen i mars/oktober; övriga 01:45→03:00 och 02:45→02:00 blockeras |
| Prognos utan ändliga pris/PV/lastvärden, negativ PV/last | `invalid-inputs` → NO-PUBLISH | Negativa spot-/säljpriser är **giltiga** och tillåts |
| Optimerarutdata saknar slot, har fel tidsnyckel, icke-ändlig W/kWh eller SoC utanför 0–15 | `invalid-dispatch` → NO-PUBLISH | Ingen ny kontrollplan, men tidigare plan kvarstår |
| Godkända kontroller | `valid` → exakt befintlig `logOptimizerRun(...,socIsLive)` | Koden verifierar **inte** att fysiskt batteri följde plan |

`lib/plan-publication-safety.ts` är ren och injiceras efter `optimizeDispatch` i `lib/plan.ts`, före persistens. `optimizerRunsSchemaCompatible()` i `lib/telemetry/dispatch.ts` läser SQLite `PRAGMA table_info` endast när opt-in är `1`. Annars görs **ingen extra DB-kontroll**. Vid NO-PUBLISH loggas endast en sanerad orsak och `dispatchSchedule/controlPlanEndTime` lämnas `null`; priser och solprognos kan fortfarande visas.

**Testat:** `lib/__tests__/plan-publication-safety.test.ts` 9/9, `optimizer-schema-contract.test.ts` 4/4 på **in-memory SQLite**, samt `plan.test.ts` inklusive 5/5 nya orchestrationsfall med mockad DB och enhetsdata; totalt **39/39** fokuserade Vitest. Strikt TypeScript och ESLint godkända utan fysisk I/O.

## Kvar för att avsluta O06

- **Driftparitet:** samma source/installerad `plan.ts` och `telemetry/dispatch.ts`, effektiv env, exakta SQLite-kolumner och aktiv dispatcher måste verifieras i en läsande R04/V02-grind.
- **Stale tidigare plan:** underkänd ny plan återkallar **inte** tidigare `optimizer_runs`. Dispatchern kan fortfarande läsa ett äldre slot med giltig tidsstämpel. Det behövs ett **separat, fail-closed och testat** plan-age/lease-veto på dispatcherns läsväg (D06/S08/O06) innan fysisk GO. Ingen blind General eller automatisk SoC-ändring.
- **TOCTOU:** SQLite-metadata och senare `INSERT` är inte atomära; `logOptimizerRun` sväljer DB-fel med logg. För fysisk drift krävs verifierad producerad rad/readback, inte bara `schemaCompatible=true`.
- **Aktiveringsförutsättning:** testad strategi för vinter/sommar, 23/25 h, 12h-horisont, verkliga sitepriser och korrekt rollback samt separat operatörs-GO. Den nya flaggan **ska inte** sättas i produktion i denna iteration.

**Status: O06 fortfarande öppen (källgrind klar; driftsatt spärr och rollback för gammal plan saknas).** [O04 horisontgrind](optimizer-seasonal-replay-2026-11-07.md), [T01 datakontrakt](telemetry-contracts-2026-11-07.md), [fysisk plan V01](physical-qualification-plan-2026-11-07.md).
