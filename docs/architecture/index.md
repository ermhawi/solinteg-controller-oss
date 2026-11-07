# Arkitektur och ADR-index

**Senast strukturerat:** 2026-11-07. En ADR bevarar ett beslut **vid en tidpunkt**; den är inte automatiskt bevis på dagens installerade tjänster. För aktiv systembild, se [Driftöversikt](../current-state.md), för kvarstående begränsningar [Kända brister](../known-issues.md). Historiska engångsrunbooks finns i [Arkivet](../archive/index.md).

**Översiktsdiagram:** [Komponenter och dataflöden](system-overview.md) · [Styrbeslut och felvägar](control-flows.md).

## Nuvarande utformning / relevanta driftsbeslut

| Område | Beslut och underlag |
| --- | --- |
| Energi och återhämtning | [Central prioriteringsarbiter 5/11](adr-2026-11-05-energy-priority-arbiter.md), [ekonomisk reserv](adr-2026-11-02-economic-reserve-recovery.md), [EMS/General-failsafe](adr-2026-11-02-ems-general-failsafe.md) |
| Broker | [Hela broker-transaktioner](adr-2026-11-04-broker-complete-transactions.md), [accepterad gammal felväg](adr-2026-11-04-broker-legacy-failure-scope.md), [brokerobservatör på Dell](adr-2026-11-03-dell-broker-observer.md) |
| EV/L3 | [Live grid-first och Systemstatus](adr-2026-11-04-ev-l3-live-grid-first-and-ui.md), [EVCC-pris med empirisk modell](adr-2026-11-04-ev-price-empirical-live.md), [laddläge i webb](adr-2026-11-05-ev-web-live-mode-selection.md), [avslutad laddsession](adr-2026-11-05-ev-unplug-end-and-price-status.md) |
| Webb/data | [GitHub OAuth2 / nätgräns](adr-2026-11-05-solinteg-github-oauth-internal-access.md), [vardag/helg-lastprofil](adr-2026-11-04-weekday-ev-load-profile.md), [prisplan och diagramhistorik](adr-2026-11-04-ev-price-plan-and-chart.md) |
| Operativ arkivering | [USB-arkiv och avveckling av tillfälliga resurser](adr-2026-11-01-solinteg-usb-archive.md) |

## Öppna förslag och osäkra antaganden

- [Stale brokertelemetri och SoC](adr-2026-11-02-stale-telemetry-soc.md) – ursprungligt offlineförslag; jämför med senare brokerrelease innan åtgärd.
- [Solöverskott under batterisparläge](adr-2026-11-02-battery-save-solar-shadow.md) – innehåller historisk shadowanalys samt senare ändringar; läs ihop med central arbiter.
- [Single-owner-broker med oberoende fallback](adr-2026-11-01-single-owner-modbus-broker.md) – ursprungligt föreslaget målläge, **inte** ett påstående om att ny fallback finns driftsatt.

## Historiska ADR:er och ersatta försök

- [Adaptiv SoC25-återhämtning](adr-2026-10-31-adaptive-soc25-recovery.md), [tillfälligt fysiskt återhämtningsprov](adr-2026-10-31-temporary-soc25-recovery.md), [EV-pauserat SoC25-prov](adr-2026-11-01-soc25-evpaused-pilot.md)
- [25 %-reserv utan bil](adr-2026-11-01-soc25-reserve-no-ev.md), [andra provet](adr-2026-11-01-soc25-recovery-retry.md), [tredje provet](adr-2026-11-01-soc25-recovery-third.md)
- [Tillfälligt plan-bypass](adr-2026-11-01-temp-soc25-plan-bypass.md), [shadow-överlämning](adr-2026-11-01-soc25-handoff-shadow.md)
- [EV sol-shadow](adr-2026-10-31-ev-solar-shadow.md), [historisk EVCC Ict2-shadow](adr-2026-11-04-evcc-ict2-metered-priority.md)

Dokument som fått senare driftsättningsbeslut ska **inte** retroaktivt skrivas om till `deployed` eller `approved`. Lägg en tydlig ersättningshänvisning eller ett nytt daterat ADR-beslut. Se även [modellen för Git-publicering](../repository-layout.md): `Sundsvall` mergas aldrig till `main`.
