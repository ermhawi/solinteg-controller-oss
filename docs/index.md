# Solinteg – teknisk dokumentation

**Utgå från faktisk drift, inte dokumentdatum eller Git-commit.** Den här startsidan skiljer aktiva system, öppna risker, planerad funktion och historiska experiment. Senaste samlade läsgranskning: **2026-11-07**.

## Börja här

- [Aktuellt driftläge: Dell, broker, batteri, EVCC och webb](current-state.md)
- [Läsande driftverifiering med kontroller och begränsningar](operations/verification.md)
- [Kända brister, osäkerheter och verifieringsluckor](known-issues.md)
- [Systemarkitektur med Mermaid-diagram](architecture/system-overview.md)
- [Styrflöden: batteri, EVCC-pris och brokerfel](architecture/control-flows.md)
- [Arkitekturbeslut och deras status](architecture/index.md)
- [Historiskt arkiv – avbrutna, ersatta och tillfälliga prov](archive/index.md)
- [Bygga, kontrollera och förhandsgranska dokumentationen](development.md)
- [Drift- och installationsrunbook](https://github.com/ermhawi/solinteg-controller-oss/blob/Sundsvall/deploy/README.md) – omfattar även äldre releasenoteringar; kontrollera ovanstående nuläge först
- [Git, utveckling kontra produktion och permanent `Sundsvall`-branch](repository-layout.md)

## Aktiv arkitektur och uppföljning

### Husbatteri, sol och säkerhet

- [Central prioritering: SoC-återhämtning, solupptagning, EMS och ekonomi](architecture/adr-2026-11-05-energy-priority-arbiter.md) – driftsättning dokumenterad, fysisk långtidsuppföljning kvarstår
- [EMS BattCtrl kontra General och watchdog-failsafe](architecture/adr-2026-11-02-ems-general-failsafe.md)
- [Ekonomisk reserv och SoC-återhämtning](architecture/adr-2026-11-02-economic-reserve-recovery.md)
- [Native On-grid EndSOC 20 %: ej verifierat i EMS](solinteg-native-endsoc-review-2026-11-05.md)
- [Frågor till Solinteg om firmware, EMS och skydd](solinteg-vendor-ems-questions-2026-11-03.md)
- [Soft-hold-hårdvaruprov – endast förberett, inte aktiverat](solinteg-broker-hold-probe-2026-11-05.md)

### Modbus och driftssäkerhet

- [Broker: hela FC03-/FC06-transaktioner i normaldrift](modbus-broker-transactions-release-2026-11-04.md)
- [Broker-baslinje och selektiv rollback, äldre release](modbus-broker-control-stage-2026-11-04.md)
- [Broker-watchdog och observer](modbus-broker-watchdog-dell-2026-11-03.md)
- [Nödfallback – endast offlinepolicy, inte garanterad i drift](modbus-broker-emergency-policy-offline-2026-11-03.md)
- [Stale-data-/SoC-frågor och fysiska skydd: läsande inventering](solinteg-fallback-readonly-inventory-2026-11-03.md)
- [Äldre migrationsplan med öppna uppföljningspunkter](plans/modbus-broker-2026-11-01.md)
- [Modbus-registerreferens](https://github.com/ermhawi/solinteg-controller-oss/blob/Sundsvall/MODBUS.md)

### Volvo/EVCC och energikostnad

- [L3-nätprioritet och bilen i Systemstatus](architecture/adr-2026-11-04-ev-l3-live-grid-first-and-ui.md)
- [Driftsatt EVCC-priscontroller med accepterad empirisk energimodell](architecture/adr-2026-11-04-ev-price-empirical-live.md)
- [Välj `deadline`/`economy` och avresetid i webben](architecture/adr-2026-11-05-ev-web-live-mode-selection.md)
- [Avslutade sessioner och verifierad kabelurkoppling](architecture/adr-2026-11-05-ev-unplug-end-and-price-status.md)
- [EVCC-kabelspårning och pausägarskap](evcc-cable-tracking-2026-11-04.md)
- [Empirisk AC-kWh per Volvo SoC, ej oberoende mätning](ev-ac-soc-empirical-2026-11-04.md)
- [Vardag/helg och avräkning av EV-lasten](architecture/adr-2026-11-04-weekday-ev-load-profile.md)
- [Energikostnadsjournal – read-only utvecklingsläge](ev-cost-ledger.md)
- [Elpris- och solprognoser](xc40-price-forecast.md)
- [GitHub OAuth2 utifrån, intern nätgräns](architecture/adr-2026-11-05-solinteg-github-oauth-internal-access.md)

## Incidenter, bevis och arkiv

- [ASUS/routeravbrott 3 november: orsak och åtgärd](solinteg-network-incident-2026-11-03.md)
- [Nattverifiering 4 november: kvarvarande protokollsymtom](solinteg-modbus-overnight-2026-11-04.md)
- [Avveckling, krypterad bevisarkivering och reversibilitet](solinteg-archive-and-retired-artifacts-2026-11-01.md)
- [Historiska EV/shadow-, SoC25- och brokerförsök](archive/index.md)

## Dokumentations- och Git-hygien

Följ [engineering-standards v1.3.0](https://github.com/ermhawi/engineering-standards/blob/v1.3.0/docs/policy.md): före ändringar läs relevant arkitektur, begränsningar, planstatus och aktiv drift. Efter ändringar håll denna översikt och [kända brister](known-issues.md) uppdaterade. Märk tydligt **föreslagen**, **implementerad i källkod**, **driftsatt** och **verifierad fysiskt** som olika tillstånd. Historiska ADR:er och experiment får bevaras men får inte framstå som dagens instruktion. `Sundsvall` mergas aldrig till `main`; inga ändringar i detta Git-repo driftsätts automatiskt.
