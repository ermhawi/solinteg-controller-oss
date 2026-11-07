# T01 · Datakontrakt och ägarskap i telemetri- och EV-kedjan

**2026-11-07 — source/tested, ingen ändring av installationen.** Inventeringen utgår från [kanonisk SQLite DDL](../../deploy/schema.sql), `scripts/services/common.py`, `lib/telemetry/core.ts`, dispatcher/poller och EVCC:s persistenta kontrakt. **Alla åtta SQL-tabeller** samt sex styrkritiska JSON-poster är täckta i `scripts/tests/test_telemetry_contract_inventory.py`. Detta är en källgranskning, inte en garanti att samtliga fält faktiskt är ifyllda i varje historisk rad.

## SQLite / `telemetry.db`

| Dataset och tidskontrakt | Skrivare (enda avsedda ägare) | Viktiga läsare | Fel- och konformitetsrisk |
| --- | --- | --- | --- |
| `readings` · `timestamp` UTC ISO, ~30 s | `modbus_poller.py` | `dispatch_loop.py`, `healthcheck.py`, `lib/telemetry/readings.ts`, oracle/energigrafer | Faser kan vara NULL i legacy-rader, stale/CT ogiltigt; en rad är observation, inte uppmätt kvitto på styrning |
| `weather` · observations-/hämtningstid UTC | `weather_poller.py` | `lib/telemetry/readings.ts` och prognos-/analysvägar | Moln-/stationslucka får inte vara fysisk PV-kvalitet |
| `price_snapshots` · `date` Stockholm-dag, `logged_at` UTC | `lib/telemetry/readings.ts` | Prisuppslag, `lib/plan.ts`, EV-pris- och jämförelseanalys | Dagens morgonpris kan ersättas av 13:00-snapshot med morgondagen; DST 23/25 h |
| `optimizer_runs` · `logged_at` UTC, `price_date` Stockholm | `lib/telemetry/dispatch.ts` | `dispatch_loop.py`, `ev_price_policy.py`, `healthcheck.py`, oracle och dashboard | `inputs_json` samt `dispatch_json` måste ha giltig tidsaxel, action/schema och färsk start-SoC; senast införd ≠ körbar plan |
| `solar_forecasts` · `logged_at` UTC, `date` Stockholm | `lib/telemetry/readings.ts` | Prognosrevision och kvalitetsskattning | Källprognos/typiskt dygn förväxlas inte med mätvärde |
| `control_actions` · `timestamp` UTC och slot | `dispatch_loop.py` | `healthcheck.py`, `lib/chart-utils.ts`, dispatch-kort, oracle | `applied` betyder accepterad kommando-/beslutsväg, **inte** oberoende fysiskt verifierad effekt |
| `control_readbacks` · `timestamp` UTC | `dispatch_loop.py` | Läsande webb/diagnostik och riskanalys | Registervärdet kan vara korrekt trots att verkligt batteri-W eller mode-effekt är annat; `readback_ok` är inte säkerhet |
| `oracle_daily` · `date` Stockholm, `computed_at` UTC | `lib/telemetry/oracle.ts` | Oracle/ekonomidiagram | `shadow`/`degraded` och tidsfönstrets SoC-ankare ska inte tolkas som `ok` |

**Databaskontrakt:** `deploy/schema.sql` definierar åtta tabeller och deras index. PostgreSQL används inte i denna telemetrikedja; det är SQLite med `journal_mode=WAL` och `busy_timeout=5000` i både Python `common.telemetry_connect` och Node `getDb`. Varje skrivare skapar sin tabell vid uppstart; kanonisk DDL appliceras **inte** automatiskt som en migration. `common.telemetry_connect` öppnar skrivbar fil och skapar föräldrakatalog — därför får testerna inte anropa funktionen mot `/opt`. `getDb` är best-effort/no-op utan `TELEMETRY_DB_PATH`; DB-fel får inte tolkas som framgångsrik publicering. `scripts/tools/schema_compatibility.py` kan separat jämföra verklig live-DB via read-only `mode=ro`, utan att läsa datarader.

## Styrkritisk JSON

| Post | Skrivare / konsument | Tidsgräns och atomiskhet | Vid saknad/korrupt/stale |
| --- | --- | --- | --- |
| `live.json` | Poller `modbus_poller.py` via `common.write_json_atomic`; dispatcher, EV och webb läser | UTC ISO `timestamp`; poller ungefär 30 s, läsarnas TTL måste kontrolleras separat. Syskonfil `.tmp` + `os.replace`, men **utan fsync** i generisk helper | `common.read_json` ger `None`; verklig fas/SoC blir okänd, inte verifierad |
| `dispatch-heartbeat.json` | Dispatcher `write_heartbeat`; `watchdog.py`, health | UTC ISO; watchdog stale efter standard **240 s**; generic atomic `os.replace` utan fsync | Legacy watchdog med saknat/korrupt timestamp **avstår från revert** – separat healthsignal krävs |
| `evcc-readonly.json` | `evcc_observer.py` (endast FC03) → `ev_price_controller.py`, kabeltracker | UTC ISO, controller max ålder **65 s**, `common.write_json_atomic` utan fsync; aktiv observer tar delat reader-flock | Ogiltig proveniens, `read_ok` eller ålder ⇒ inga nya FC10 |
| `evcc-cable-state.json` | `ev_history.py` / `evcc_cable_tracker.py` → priscontroller + pause-owner | UTC ISO, **90 s** prover, observationslucka 100 s, två stabiliserade prover; `store` skapar privat 0600, fsync(fil+dir) och `os.replace` | Korrupt/oväntad epoch ⇒ innehav `unknown`/revoke, ingen ny automatisk resume. Kort ur-/inkoppling mellan polls kan missas |
| `ev-price-pause-owner.json` | `ev_price_controller.py` via `ev_price_pause_owner.write_owner`; nu också manuell CLI för veto | UTC ISO, **24 h** max ägarålder, kabelprov 90 s; 0600 owner-only och atomisk fsync(fil+dir) | Ogiltig schema/proveniens/UID/mode ⇒ **inhibited**, `pending/releasing/revoked` tillåter inte automatisk resume |
| `ev-price-controller-status.json` | `ev_price_controller.publish_controller_status` → webb/status | UTC ISO, temp+replace, publik sanerad status utan owner-/sessions-ID | Status `writes=1` eller HTTP 200 betyder inte att fysisk 6 A-styrning är stabil och av användaren accepterad |

**Ytterligare JSON** (EV HA-energisession/status, Smart Charging-status, empiri-ledger, optimerarens `inputs_json`/`dispatch_json` i SQLite, kostnads-/pristariffer) är separata producentled och ska inte användas som ersättning för kabelns event-epoch eller verifierad pau­sägare. Ovanstående sex poster är de kritiska kontrollövergångarna; andra analysfiler hanteras av sina befintliga kontrakt/tester. Styrningen får aldrig kalla korrupt JSON för `safe` eller behandla ålderslösa `timestamp` som färska.

## Risker och DoD

- **Atomicitet ≠ durabilitet.** Generisk `common.write_json_atomic` använder tempnamn och `os.replace` utan fsync/dirfd; privat kabel-/owner-state fsyncar. Detta är en dokumenterad skillnad, inte en omedelbar migrering.
- **Samtidighet:** SQLite WAL/busy minskar contention men gör inte en sammansatt läsning av 30-sekundersregister fysiskt atomär. Monitorera write failures; ingen strömstyrning på SQLite-felet.
- **Schema/TTL:** DST, gammal optimerarhorisont, pollerlucka och korrupt owner ska ge okänd/kvalitetsflagga, inte extern FC10 eller felaktig `General`. Särskilda T02/O06/EVCC-tester återstår.
- **Verifiering 2026-11-07:** `scripts/tests/test_telemetry_contract_inventory.py`: käll-DDL 8/8 tabeller matchar ägare, Node/Python WAL+5 s busy, sex JSON-källor och kända sample-/owner-TTL. Inga fysiska I/O eller privata datarader.
