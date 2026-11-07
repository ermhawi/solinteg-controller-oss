# T02 · Fem separata kontrollbevis, ingen falsk säkerhetskvittens

**2026-11-07, `tested/source` och läsande API/UI, ej driftsatt.** `lib/control-evidence.ts` har fem oberoende statusfält. `GET /api/control-evidence` läser endast redan lagrad `control_actions` och `control_readbacks` i SQLite samt färsk `live.json` genom befintlig inverterserverläsare. `app/components/ControlEvidencePanel.tsx` visas under **Dispatch → Visa detaljer** med ett explicit besked att fysisk styrsäkerhet **inte** är verifierad. Huvudets `Live` anger enbart färsk telemetri, aldrig säkerhet (tooltip i `LiveStatus.tsx`).

| Separat nivå | Tillgänglig källa och TTL | Rapporterad verklig kvalitet |
| --- | --- | --- |
| **Process** | I dag finns ingen oberoende systemd-/extern övervakningssignal i webb-API | **`unknown` alltid**, även om HTTP 200 eller heartbeat är färskt |
| **Beslut** | Sista `control_actions.outcome`, 15 minuter, `armed` synligt | `observed` (loggat beslut), `failed` (error-outcome), `stale` eller `unknown`; loggat `applied` betyder **inte** fysisk verkställighet |
| **Accepterat registerkommando** | Ingen säker, separat ACK-/fencingkedja i historisk `control_actions` | **`unknown` alltid** tills broker serialiserad/kvitterad kommandokedja S06–S08 kopplats in |
| **Registerreadback** | `control_readbacks.readback_ok` med UTC `timestamp`, max 5 minuter | `observed`, `failed`, `stale` eller `unknown`; korrekt registervärde är inte batteriets uppmätta effekt |
| **Fysisk effekt** | `live.json.battery_w` med UTC `timestamp`, max 120 s, ändamålsenligt mätvärde | Batteri-W visas som **observation**; `physicalEffect=unknown`, `physicalControlVerified=false` **alltid**, eftersom mätt W saknar oberoende attribution till kommandot |

**Fail-closed-beteende:** Korrupt/saknad DB/tabell, HTTP-/proxyfel, svar utan explicit `physicalControlVerified:false`, framtidsdaterade eller för gamla prover ger okänt eller för gammalt. UI ställer inte ut en positiv status utan entydig evidens. Även `armed=false` och `applied` klassas bara som loggad simulering. Inga nya skriva-API:er eller externa enhetsanrop har lagts till. `scripts/services/safety_alert_contract.py` (T03) kan inte kvittera ett aktivt larm vid `unknown`; den är fortfarande **source-only**, inte kopplad till live-notifieringar.

**Tester:** `lib/__tests__/control-evidence.test.ts`, **6/6** inklusive `applied + läsbar batteri-W` men fortfarande okänd acceptans/fysik, avväpnad simulering, `error_revert_failed`, readback-fel och stale/framtidsdatum, och explicit spärr mot fysisk `verified` trots positiva observationer. `npm run typecheck`, ESLint och Next-build verifierar källans API och panel. **Statusgräns:** source+tested, ingen `installed`/fysisk kvalificering. Process-/brokerkvittens och oberoende fysisk sensor saknas fortfarande; detta får inte användas som automatisk GO-grind.
