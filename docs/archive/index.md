# Historiskt dokumentationsarkiv

**Arkiverat 2026-11-07.** Här finns äldre experiment, ersatta migrationssteg och tidsbundna driftinventeringar. Dokumenten bevaras oförkortade för spårbarhet, men **ska inte användas som instruktioner för dagens driftsättning**. De ursprungliga länkarna finns kvar som hänvisningar.

För nuläge, använd [Driftöversikten](../current-state.md) och [Kända brister](../known-issues.md). ADR:er bevaras separat i [arkitekturindexet](../architecture/index.md), även när ett beslut ersatts.

## Batteri – temporära 25 %-prov

- [Första 20→25 %-provet](battery/temporary-soc25-pilot-2026-10-31.md)
- [Adaptivt 1,5 kW-prov](battery/temporary-soc25-adaptive-pilot-2026-10-31.md)
- [EV-pauserat återhämtningsprov](battery/temporary-soc25-evpaused-pilot-2026-11-01.md) – originalrapporten saknar verifierat terminalt provutfall
- [Tillfälligt plan-bypass](battery/temporary-soc25-plan-bypass-2026-11-01.md)
- [Föreslagen 25–26 %-shadowöverlämning](battery/soc25-handoff-shadow-2026-11-01.md) – aldrig driftsatt
- [25 %-reserv med frånkopplad bil](battery/soc25-reserve-no-ev-2026-11-01.md)
- [Andra 26→28 %-provet](battery/soc25-recovery-retry-2026-11-01.md)
- [Tredje 25–26→28 %-provet](battery/soc25-recovery-third-2026-11-01.md)

## EV – tidigare försök och shadowlösningar

- [Fysiskt L3/EV-nätprioriteringsprov](ev/ev-grid-priority-trial-2026-10-28.md)
- [Tidigare releaseförberedelse för L3](ev/ev-grid-priority-permanent-2026-10-29.md)
- [Kod-/driftinventering före live-EV](ev/ev-l3-code-drift-review-2026-11-04.md)
- [Read-only EVCC-driftsättning](ev/evcc-readonly-rollout-2026-10-28.md)
- [Två laddlägen i EVCC-shadow](ev/evcc-solar-shadow.md)

## Broker – passerade migrationssteg

- [Offlineetapper 0–2](broker/modbus-broker-offline-2026-11-01.md)
- [Read-only-etapp 3](broker/modbus-broker-readonly-stage3-2026-11-02.md)

Det aktuella brokerläget finns i [transaktionsdriftsättningen](../modbus-broker-transactions-release-2026-11-04.md). Äldre release- och rollbackdokument som fortfarande används som jämförelse eller återställningsunderlag ligger tills vidare kvar utanför arkivet och ska alltid kontrolleras mot faktiskt installerade filhashar.

## Arkivpolicy

Historisk dokumentation är evidens, inte aktiv instruktion. Behåll datum, ursprungligt utfall och länkbarhet. Ändra aldrig en gammal rapport så att ett föreslaget eller avbrutet prov ser driftsatt eller lyckat ut. Flytt av Markdown till detta **Git-arkiv är inte samma sak** som krypterad arkivering av privata drifts-/hårdvaruartefakter enligt [separat säkerhetsrunbook](../solinteg-archive-and-retired-artifacts-2026-11-01.md).
