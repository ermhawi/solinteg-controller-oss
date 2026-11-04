# ADR 2026-11-04: hela styrsekvenser i Modbus-brokerns worker

**Beslut:** Implementerat, offlineverifierat och **DRIFTSATT** på Dell
**2026-11-04 kl. 07:53:55 CET**. Dispatchern kör nu
`SOLINTEG_MODBUS_SOURCE=broker_transaction`, med hela
styrsekvensen i en broker-worker. Första verkliga styrning,
registerreadback och tre efterföljande pollerprover verifierade;
se [driftrapport](../modbus-broker-transactions-release-2026-11-04.md).

## Bakgrund

Normalbrokern från 2026-11-04 äger en TCP-anslutning, men
`inverter_control` kör ännu registersekvenser i dispatcherprocessen.
En pollerläsning kan då läggas mellan två FC06-anrop i samma
laddnings-, urladdnings-, hold- eller Auto-åtgärd.

Målet är att låta **en enda broker-worker slutföra hela den befintliga
styrsekvensen före nästa pollerbegäran**, utan att förändra ekonomi,
planering, hårdvarugränser eller failure-scenario.

## Beslutad arkitektur

- **Dispatcher beslutar och behåller sina befintliga guards.**
  `Inverter` väljer transport via `SOLINTEG_MODBUS_SOURCE`:
  `direct` (oförändrad standard), `broker` (dagens produktion),
  `broker_transaction` (nytt, opt-in).
- **Samma styrkod, ingen kopierad registeralgoritm:** Funktionen
  `force_charge`, `force_discharge`, `force_hold`,
  `freeze_battery`, `return_to_auto`, `set_import_limit` eller
  `clear_import_limit` skickar EN IPC-begäran i nytt läge. I brokern
  anropas **exakt samma `inverter_control`-funktion**, nu mot en
  injicerad vy på brokerns fysiska transport. Ingen ny TCP-klient.
- **Brokern äger serialisering och write-rate-limit.** En enda worker
  behandlar hela anropet innan nästa read-/writebegäran.
  `Inverter.RateLimiter` återanvänds mellan transaktioner och
  behåller befintlig 2-sekunders minimitid mellan skrivningar.
  Registerordning, `SETTLE_S`, verification/readback, snabbvägen
  för redan aktiv riktning, SoC-gränser och fysisk `freeze`-
  verifiering kommer från den befintliga koden.
- **Behörighet och opt-in:** Brokerprocessen kräver separat
  `SOLINTEG_BROKER_TRANSACTIONS_ENABLED=1` för att acceptera
  `transaction_*`. UDS-rollen `control` kommer som tidigare från
  `SO_PEERCRED` + systemd-cgroup; `read` kan aldrig begära styrning.
  I nytt läge nekas den äldre råa FC06-IPC-metoden; FC03 för poller
  och diagnostiska dispatcherläser finns kvar. Den nya klienten
  vägrar ansluta till en broker utan aktiverad transaktionsfunktion.
- **Hårdvaru-allowlist kvarstår:** Inga ändringar av `52502/52503`,
  nya register eller enhetens BMS-skydd. Manuella
  `set_soc_floor` tillhör inte den normala dispatchvägen och
  migreras **inte**; ett sådant anrop nekas via transaktions-IPC.
- **Oförändrad fallback:** `dispatch_loop.py`, `watchdog.py`,
  `solinteg-watchdog.service/timer`, gamla
  `revert_best_effort` och `DISPATCH_BROKER_STALE_GUARD=0`
  lämnas orörda. Inga nya timeout-/SoC-supervisors eller
  privilegierade nödfallbacks installeras.

## Begreppet transaktion

**Logiskt odelbar i brokerns kö, inte hårdvaruatomär.**
En annan lokal IPC-klient kan inte blanda in läsningar mellan
delstegen. Ett kommunikationsavbrott efter exempelvis tredje
FC06 kan däremot lämna de tre första redan genomförda.
Befintlig dispatch-felhantering försöker återställa som tidigare;
ny felgaranti är **inte** del av detta arbete.

## Kända brister / uppskjutet

1. Befintlig watchdog kan fortfarande öppna direkt TCP:502
   vid fel och därmed konkurrera med brokern.
2. Brokerstopp, Dellbortfall, tvetydig FC06-timeout och
   kvarhängande `0x0303` garanterar inte neutral Auto.
3. Långa transaktioner kan fördröja pollern. Den befintliga
   30-sekunders timeouten och observationsgränserna är
   **inte** en ny felpolicy; mätluckor måste följas efter release.
4. Lås och behörigheter skyddar mot lokala samarbetsvilliga
   klienter men inte externa nätverksanslutningar.
5. Laddning/energiflöde efter cutover, drift efter reboot och
   automatiskt recovery vid hostfel är inte provade för
   transaktionsversionen.

## Releasebeslut

`broker_transaction` levereras **avstängt** och ska aktiveras
**samtidigt** på broker och dispatcher, inte på en i taget
under aktiv drift. Behåll `broker_legacy`-pollern och
watchdoggen. Genomför först ett separerat, reversibelt
driftsättningsfönster enligt
[releasechecklistan](../modbus-broker-transactions-release-2026-11-04.md).

**Verifiering:** Offline-fake med flera samtidiga klienter,
SoC-/hold-/freeze-/Auto-/importlimitfall, idempotens,
behörighetsavslag, timeout och en riktig `pymodbus`
FC03/FC06-loopback på `127.0.0.1`, utöver befintlig
full regressionssvit.
