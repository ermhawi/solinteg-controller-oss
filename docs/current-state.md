# Aktuellt systemläge – Dell / Sundsvall

**Senast kontrollerat:** 2026-11-07 cirka 05:46–05:47 CET. Kontroll av `systemctl list-units`, timers, installerade dispatcher-drop-ins samt källkod/dokumentation; **inga styrkommandon eller konfigurationsändringar utfördes**. Detta är en tidsstämplad driftinventering, inte en garanti för framtida status.

För en sammanhängande bild av komponenter och kopplingar, se [systemarkitekturen](architecture/system-overview.md) och [styrflödena](architecture/control-flows.md).

## Snabböversikt

| Del | Senast verifierat | System och underlag |
| --- | --- | --- |
| Solinteg Modbus-broker | `solinteg-broker-control.service` **active/running** | En broker som normal TCP-ägare, hela styrtransaktioner i worker enligt [driftsättningen 4/11](modbus-broker-transactions-release-2026-11-04.md). Äldre read-only-migreringsetapper är [arkiverade](archive/index.md). |
| Batteripoller och dispatcher | Båda **active/running** | `solinteg-poller.service`, `solinteg-dispatch.service`. Dispatcher har installerade `20-broker-control`, `30-transactions`, `40-energy-priority` och `ev-grid-live-20261104`-drop-ins. Faktiska ENV-värden och inverterregister återlästes inte här. |
| Prioritering: sol / ekonomisk reserv / EV | `40-energy-priority.conf` finns bland aktiva drop-ins; [ADR rapporterar aktivering 5/11](architecture/adr-2026-11-05-energy-priority-arbiter.md) | Källkodsflaggan `DISPATCH_ENERGY_PRIORITY` har standardvärde `0`; den driftsatta inställningen måste bedömas i installationskontexten, inte från Git ensamt. |
| EVCC-prisstyrning | `solinteg-ev-price-controller.timer` **active/waiting**, senast utlöst 05:46 | Schemalagd varje minut; `.service` är en **oneshot** och `inactive/dead` mellan körningarna är normalt. [Aktiverings-ADR](architecture/adr-2026-11-04-ev-price-empirical-live.md) och [valbara laddlägen](architecture/adr-2026-11-05-ev-web-live-mode-selection.md). Ingen ny fysisk EVCC-skrivning gjordes vid granskningen. |
| EVCC-observation och Volvo | `solinteg-ev-observer`, `solinteg-ev-history`, `solinteg-ev-ha` **active/running**; Volvo-status-timer **active/waiting** | Läsande mät- och historikkedja. [Avslutade laddsessioner](architecture/adr-2026-11-05-ev-unplug-end-and-price-status.md), [empirisk AC-/SoC-modell](ev-ac-soc-empirical-2026-11-04.md). |
| Webb och åtkomst | `solinteg-web.service` **active/running** och `solinteg-web-internal-only.service` **active/exited** | [GitHub OAuth2 externt och internt nätfilter](architecture/adr-2026-11-05-solinteg-github-oauth-internal-access.md) är dokumenterad driftsättning. E2E-inloggning och brandväggens faktiska regeltäckning testades inte på nytt här. |
| Driftwatchdog och brokerobservatör | Bådas timers **active/waiting** | Watchdog är periodisk oneshot; `inactive/dead` mellan körningar betyder inte avaktiverad timer. [Kända felvägsbegränsningar](architecture/adr-2026-11-04-broker-legacy-failure-scope.md). |
| Wi-Fi-återhämtningswatchdog | `wifi-recovery-watchdog.timer` **active/waiting** med körning varje minut | Installerad utanför detta Git-repo. Servicefilens standard är `WIFI_WATCHDOG_MODE=observe`; privat `EnvironmentFile` kan ändra effektivt läge och har **inte** verifierats. Avgränsningar och rollback saknar ännu repo-länkad runbook, se [kända brister](known-issues.md). |

## Styrprinciper och säkerhetsavgränsning

- **Husbatteri:** den ekonomiska 20 %-reserven är en mjuk styrningsgräns, inte bekräftat firmware-/BMS-skydd. EMS-börvärde 50 W kan ge högre faktisk urladdning. Solprioritets-arbitern hanterar främst konflikter **under** den ekonomiska reserven; den ersätter inte optimeraren i hela SoC-området. [Energiprioritets-ADR](architecture/adr-2026-11-05-energy-priority-arbiter.md).
- **Bil:** två driftval finns: `deadline` (mål-SoC vid avresetid) och `economy` (billigaste tillåtna perioder utan deadline). Den aktiva prisstyrningen är *inte* ett bevis för oberoende uppmätt kWh, perfekt manuell stoppdetektion eller fullständig fassäkring. [EVCC-live-ADR](architecture/adr-2026-11-04-ev-price-empirical-live.md).
- **Nät/broker:** normalvägen är serialiserad, men watchdog och nödfallback har kvar dokumenterade felvägsrisker. Påstå inte absolut TCP-exklusivitet i alla fellägen. [Broker-felväg](architecture/adr-2026-11-04-broker-legacy-failure-scope.md).
- **Soft-hold/0 W:** ännu bara förberett hårdvaruprov, **inte aktiv produktionsfunktion**. [Provgrindar](solinteg-broker-hold-probe-2026-11-05.md).

## Källkod kontra installerad drift

Den permanenta utvecklingskällan är `~/dev/github.com/ermhawi/solinteg-controller-oss` på **`Sundsvall`**. Produktionskoden under `/opt/solinteg/app` är en separat och platsanpassad installation, som tidigare ändrats selektivt och inte är byteidentisk med repots dispatcher. **En commit, ett lyckat test eller en fil i `deploy/` bevisar inte driftsättning.** Se [repositorybeslutet](repository-layout.md).

Konkreta aktuella frågetecken finns i [Kända brister](known-issues.md). Historik och avbrutna prov är flyttade till [Arkivet](archive/index.md). Driftanvisningarna i [`deploy/README.md`](https://github.com/ermhawi/solinteg-controller-oss/blob/Sundsvall/deploy/README.md) innehåller även kronologiska äldre releasenoteringar; kör aldrig ett äldre provkommando som om det vore en aktiv standardprocedur.
