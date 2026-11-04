# ADR 2026-11-04: färdigställ gemensam Modbus-broker, behåll befintlig fallback

**Beslut:** Accepterat med dokumenterade brister och **driftsatt för
normal kommunikation på Dell 2026-11-04 kl. 05:45 CET**.
Ingen ny nödfallback är aktiverad.
**Branch:** Permanent `Sundsvall`, aldrig merge till `main`.

## Kontext

Under natten 4 november fick pollerns FC03-läsningar vid två tillfällen
dispatcherns FC06-svar för register 50207, med matchande börvärden.
Dagens poller har persistent TCP-anslutning medan dispatcher och
watchdog öppnar egna, korta Modbus-TCP-sessioner.
Home Assistants egna `solax_modbus` är avaktiverad och inte orsaken.

En tidigare större riskanalys har föreslagit en privilegierad,
oberoende säkerhetsfallback med egen SoC-/BMS-verifiering.
Denna utredning ska **inte** blockera normalbrokerintegrationen.

## Beslut

1. En **enda brokerprocess** ska äga ordinarie TCP:502 från både
   `solinteg-poller` och `solinteg-dispatch`. Transporten arbetar
   sekventiellt i en worker med befintligt skrivintervall.
2. För normal styrning återanvänds `inverter_control.Inverter`,
   `force_charge`, `force_discharge`, `force_hold`,
   `return_to_auto` och dess befintliga SoC-/fas-/importgränser.
   En begränsad IPC-transport ersätter bara nätverksklienten när
   `SOLINTEG_MODBUS_SOURCE=broker` är **opt-in** på dispatchen.
   Dispatchexekveringens felhantering ändras inte i detta steg.
3. Pollerns `broker_legacy`-läge kör **exakt samma** tidigare
   `read_inverter`/extended-CT-backoff, `live.json` och SQLite
   som den direkta pollern. Endast transporten ersätts; det strängare
   `broker`-snapshotläget från etapp 3 används inte vid denna cutover.
4. IPC använder `SO_PEERCRED` och cgroupidentitet:
   poller får FC03-allowlist, dispatcher får exakt allowlistad
   FC03/FC06. Root-säkrad cutover-marker, OS-`flock` och
   systemd-beroenden krävs innan kontrollbrokern kan startas.
   Inga skrivbara SoC-skyddsregister exponeras.
5. Den **installerade** `solinteg-watchdog.service` och
   `solinteg-watchdog.timer`, inklusive dagens försök att
   återställa General/Auto, **ändras inte**.
   `DISPATCH_BROKER_STALE_GUARD` lämnas uttryckligen `0`
   för denna integrationsväg. Offlineprojektets separata
   privilegierade supervisor driftsätts **inte**.
6. Brokertransporten är aktiv **endast** via de granskade
   systemd-överläggen på Dell. `direct` är fortsatt default i
   källkoden om inget explicit brokerläge valts.

## Accepterade brister (kända och uppskjutna)

- Om dispatcher dör medan broker och poller fortfarande kör kan
  befintlig watchdog öppna sin **egen** TCP-session och konkurrera
  med brokern. Exklusivitet är då inte absolut under felläge.
- Vid brokerhaveri kan dispatcherns IPC-baserade revert misslyckas.
  De installerade `BindsTo`-beroendena stoppar då poller/dispatcher
  och låter **samma gamla** heartbeat-watchdog pröva sin direkta
  Auto-återställning. Framgång är **inte garanterad**.
- Om växelriktaren ligger kvar i EMS BattCtrl kan ett gammalt
  börvärde kvarstå; General kan förbruka under den ekonomiska
  20 %-reserven med inverterns aktuella ~10 % native EndSOC.
- En host-lokal `flock` hindrar endast andra samarbetsvilliga
  processer, inte externa eller manuella TCP-klienter. Brandväggs-
  och failoverfencing är uppskjutna.
- Strömavbrott på Dell kan inte upptäckas av Dell-lokal övervakning.
  Detta steg inför **ingen** automatisk alternativ säkerhetsnod.

Dessa brister är **inte en garanti om säker fallback** och ska
riskbedömas på nytt innan man förlitar sig på brokerdrift vid
fel. Förändra inte gamla watchdoggen som en del av denna ADR.

## Validering och driftsättning

Koden testas med en strikt enkelsessions-fake, exakt gammal
registerskrivordning, befintliga SoC-/återhämtningsvägar,
behörighetsavslag och verklig `pymodbus` mot **127.0.0.1**.
**Fysisk cutover genomfördes 2026-11-04 och är återställbar**:
gamla pollern/dispatchern stoppades innan markör/brokerstart.
Efteråt verifierades att endast brokerprocessen ägde TCP:502,
att `live.json` och SQLite fortsatte uppdateras och att dispatchern
tillämpade en normal 50 W-åtgärd. Därefter gav två nya pollcykler
EMS BattCtrl-readback och fortsatta mätningar. Utvärdering av
fel-/fallbackscenarier är **medvetet uppskjuten**.

Se [brokerintegrationsplan och rollback](../modbus-broker-control-stage-2026-11-04.md).
