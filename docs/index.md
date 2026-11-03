# Solinteg – teknisk dokumentation

**Status:** Se varje länkad guide; planerade funktioner är inte automatiskt driftsatta.

## Läs vidare

- [Projektöversikt och användning](https://github.com/ermhawi/solinteg-controller-oss/blob/Sundsvall/README.md)
- [Repositorylayout](repository-layout.md)
- [Plan: Modbus-broker, ensam TCP-ägare och säker migrering](plans/modbus-broker-2026-11-01.md)
- [Modbus-broker etapp 0–2: offlineimplementation och baslinje](modbus-broker-offline-2026-11-01.md)
- [Modbus-broker etapp 3: read-only-kod och grindar inför driftsättning](modbus-broker-readonly-stage3-2026-11-02.md)
- [Föreslagen ADR: en Modbus-broker med oberoende nödfallback](architecture/adr-2026-11-01-single-owner-modbus-broker.md)
- [Föreslagen ADR: stale brokertelemetri och SoC-medveten dispatch (enbart offline)](architecture/adr-2026-11-02-stale-telemetry-soc.md)
- [Arkiv och avveckling av gamla drifts-/testartefakter](solinteg-archive-and-retired-artifacts-2026-11-01.md)
- [ADR: krypterat USB-arkiv och bortstädning efter prover](architecture/adr-2026-11-01-solinteg-usb-archive.md)
- [EV-laddning och prioritering](ev-grid-priority-permanent-2026-10-29.md)
- [Tillfälligt husbatteriprov 20–25 %](temporary-soc25-pilot-2026-10-31.md)
- [Adaptivt 1,5 kW-prov till 25 %](temporary-soc25-adaptive-pilot-2026-10-31.md)
- [Separat EV-pauserat 25 %-prov med watchdog](temporary-soc25-evpaused-pilot-2026-11-01.md)
- [25 %-reserv utan inkopplad Volvo – tidsbegränsat prov](soc25-reserve-no-ev-2026-11-01.md)
- [Fristående 26→28 %-återhämtningsprov med rättad watchdog](soc25-recovery-retry-2026-11-01.md)
- [Tredje fristående 25–26→28 %-laddprov](soc25-recovery-third-2026-11-01.md)
- [ADR: tredje prov med egen identitet och verifierad återställning](architecture/adr-2026-11-01-soc25-recovery-third.md)
- [ADR: nytt 26→28 %-prov utan återanvändning av gammal status](architecture/adr-2026-11-01-soc25-recovery-retry.md)
- [Tillfällig planändring: använd batteriet fram till återhämtningsprovet](temporary-soc25-plan-bypass-2026-11-01.md)
- [ADR: tidsbegränsat undantag från batterisparläge](architecture/adr-2026-11-01-temp-soc25-plan-bypass.md)
- [ADR: 25 %-återhämtning utan inkopplad bil](architecture/adr-2026-11-01-soc25-reserve-no-ev.md)
- [ADR: EV-pauserad återhämtning med strikt förkontroll](architecture/adr-2026-11-01-soc25-evpaused-pilot.md)
- [Föreslaget 25–26 % återhämtningsskydd (enbart skuggmodell)](soc25-handoff-shadow-2026-11-01.md)
- [ADR: 25–26 % skuggmodell, utan fysisk aktivering](architecture/adr-2026-11-01-soc25-handoff-shadow.md)
- [Beslut: fasberoende tillfällig återhämtningsladdning](architecture/adr-2026-10-31-adaptive-soc25-recovery.md)
- [Beslut: tidsbegränsat fysiskt SoC-återhämtningsprov](architecture/adr-2026-10-31-temporary-soc25-recovery.md)
- [Beslut: solmedveten EV/L3-shadow](architecture/adr-2026-10-31-ev-solar-shadow.md)
- [ADR: solöverskott under batterisparläge, skrivskyddad skuggmodell](architecture/adr-2026-11-02-battery-save-solar-shadow.md)
- [ADR: EMS kontra General, skydd, watchdog och bortfall av Dell](architecture/adr-2026-11-02-ems-general-failsafe.md)
- [ADR: återställd prisstyrd mjuk 20 %-reserv och separat SoC-skydd](architecture/adr-2026-11-02-economic-reserve-recovery.md)
- [EVCC – read-only rollout](evcc-readonly-rollout-2026-10-28.md)
- [Elprisprognoser](xc40-price-forecast.md)
- [Energikostnadsjournal](ev-cost-ledger.md)
- [Modbus](https://github.com/ermhawi/solinteg-controller-oss/blob/Sundsvall/MODBUS.md)
- [Driftsinstruktioner](https://github.com/ermhawi/solinteg-controller-oss/blob/Sundsvall/deploy/README.md)
- [Domänkunskap](https://github.com/ermhawi/solinteg-controller-oss/blob/Sundsvall/DOMAIN.md)

## Dokumentationsstandard

Projektet följer [engineering-standards v1.3.0](https://github.com/ermhawi/engineering-standards/blob/v1.3.0/docs/policy.md), med obligatorisk [dokumentationspreflight](https://github.com/ermhawi/engineering-standards/blob/v1.3.0/docs/documentation-hygiene.md), [Git-hygien](https://github.com/ermhawi/engineering-standards/blob/v1.3.0/docs/git-hygiene.md) och [artefaktstädning](https://github.com/ermhawi/engineering-standards/blob/v1.3.0/docs/artifact-hygiene.md). Före utveckling ska arkitektur/design, kända brister och planernas status jämföras med aktuell kod, konfiguration och tester. Före Definition of Done uppdateras berörda dokument, kända brister och planstatus med verifierat resultat. Git/Markdown är källan till sanningen; se [lokal arkiv- och städrutin](solinteg-archive-and-retired-artifacts-2026-11-01.md). En commit på `Sundsvall` innebär inte driftsättning.
