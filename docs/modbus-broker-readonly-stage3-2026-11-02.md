# Solinteg Modbus-broker: etapp 3 – read-only integrationsförberedelse

**Status:** Källkod och installationstemplates förberett **2026-11-02**.
**Drift:** **INTE godkänd eller driftsatt**. Ordinarie direktpoller,
dispatcher, watchdog och EV-vakt kör oförändrat.
**Branch:** `Sundsvall`, aldrig merge till `main` eller upstream.
**Avgränsning:** Solintegs TCP:502. EVCC/Waveshare RS-485 är separat.

## Levererat, men inte installerat

| Fil | Uppgift |
| --- | --- |
| `scripts/services/modbus_broker_readonly_transport.py` | Faktisk `pymodbus>=3.13,<4`-adapter med exakt vitlistade FC03-block, slave 255, explicit connect/reconnect och **all FC06 nekad** |
| `scripts/services/modbus_broker_readonly_service.py` | Service-entrypoint med dubbla aktiveringsgrindar `--read-only` och `SOLINTEG_BROKER_READONLY_ENABLED=1`. Ignorerar `SOLINTEG_CONTROL_ARMED`; `Broker(runtime_mode='readonly')` vägrar styrkontroll |
| `scripts/services/modbus_poller.py` | Nytt **valfritt** `SOLINTEG_POLL_SOURCE=broker`, default oförändrat `direct`. Läser skyddad UDS med `read_snapshot`, översätter till samma `live.json`-/SQLite-fält som tidigare |
| `deploy/solinteg-broker-readonly.service` | **Ej installerad** systemd-template med `RuntimeDirectoryMode=0700`, en enda lokal Unix-socket och `ConditionPathExists=/run/solinteg-broker-readonly-approved` |
| `deploy/solinteg-poller-broker-readonly.conf` | **Ej installerad** poller-drop-in. `Requires/After` broker; `RestrictAddressFamilies=AF_UNIX` förbjuder direkt TCP från migrerad poller |
| `scripts/tests/test_modbus_broker_readonly.py` | Fakebaserade FC03-allowlist, FC06-denial, IPC-åtkomst, poller main, fel, stale/reject, SQLite-kontrakt, ingen extra TCP |
| `scripts/services/modbus_broker_lease.py` och `deploy/solinteg-broker-ownership.conf` | Processlivslångt, kernel-exklusivt `flock` på en skyddad **root-ägd** `/run`-fil; boot-template **inte installerad** |

### Kontrakt och begränsningar

- Den enda tillåtna läsytan är exakt
  `(50000,1)`, `(10994,36)`, `(11000,30)`,
  `(30258,2)`, `(33000,4)`, `(50006,1)`, samtliga
  **FC03 till slave 255**. Ingen godtycklig adressläsning.
- En verklig TCP-klient skapas endast när den nya read-only-servicen
  uttryckligen har startats, har en root-ägd `0600`-marker
  `/run/solinteg-broker-readonly-approved` från en godkänd cutover
  och den första godkända FC03-begäran kommer.
  `ConditionPathExists` gör att installationen inte startar utan
  denna marker. Den skapas efter explicit go och verifierad
  frigöring av den gamla TCP-sessionen, och tas bort vid rollback.
  Att importera adaptermodulen eller committa källkod ansluter inte.
- Broker-processen accepterar enbart `read` från nuvarande peer-UID.
  `SO_PEERCRED` används; rollen kan inte väljas genom JSON.
  `armed=False` och read-only-transportens `write_register()` som
  alltid kastar `PermissionError` ger flera oberoende spärrar mot FC06.
- Default `direct` ändras inte i pollern. I `broker`-läge finns
  **ingen automatisk fallback till direkt Modbus**. Vid IPC-bortfall
  eller `status=rejected` uppdateras **inte** `live.json` och
  ingen SQLite-`readings`-rad skapas; den gamla tidsstämpeln
  förblir gammal så konsumenter kan upptäcka ett avbrott.
- Godkända broker-samples mappar till alla 16 befintliga
  `live.json`-fält och gamla SQL-kolumner, plus
  `sample_id`, `sample_quality` och `sample_duration_s`.
  De extra fälten är additiva; kvalitet och tid verifieras innan
  filen eller databasen uppdateras.

## Offlineverifiering

```bash
python3 -m unittest discover -s scripts/tests -p test_modbus_broker_readonly.py
# Kräver äkta pymodbus>=3.13, exempelvis installerad 3.15.0:
python3 scripts/tests/wire_smoke_readonly.py
sudo systemd-analyze verify deploy/solinteg-broker-readonly.service
```

TCP-smoketestet använder **enbart 127.0.0.1 med en
syntetisk Modbus-TCP-server och kernelvald port**,
aldrig `SOLINTEG_HOST`. Det skickar två fulla läsomgångar
med **12 FC03 och 0 FC06** via faktisk pymodbus, testar
slave 255, read-only `Broker` och registermodell. Det
installerar inga tjänster och ändrar ingen energistyrning.

**Verifierat 2026-11-02:** 21 nya read-only-integrationstester,
**1 100 Python-tester totalt**, **697 Vitest-tester** godkända
(1 överhoppat), Zensical-dokumentationsbygge utan anmärkningar,
`systemd-analyze verify` för broker-enheten godkänt med root,
och riktigt `pymodbus 3.15.0` över syntetisk localhost-TCP
godkänt. **Inga fysiska Modbusoperationer eller produktions-
systemd-ändringar** gjordes.

## Fencing-grind testad offline (2026-11-02)

`scripts/services/modbus_broker_fencing.py` tillför ett **rent
injekterat säkerhetsprotokoll**: stoppa broker, verifiera att processen
är död, att både broker-/andra TCP-sessioner till dongeln är noll och
att broker-IPC inte längre lyssnar, kontrollera samma tillstånd igen
direkt före överlämning, och anropa **högst ett** nödförsök till Auto
som måste ge verifierad readback. Vid okänt ägarskap, misslyckat stopp,
tidsgräns, exception, konkurrerande anslutning eller verifieringsfel:
**ingen ny nödförbindelse** och tydligt larm/resultat.

`scripts/tests/test_modbus_broker_fencing.py` verifierar tio
offlinefall, inklusive att en ny anslutning mellan två kontrollpunkter
blockerar nödförsöket. Protokollet har **ingen** systemd-, `ss`- eller
Modbusimplementation och är inte anslutet till existerande watchdog.
En verklig privilegierad supervisor måste dessutom **maskera återstart**
av brokern under fallback, göra oberoende ägarkontroll mot OS och
skydda check/use-fönstret med hostnivå-policy innan cutover kan
godkännas. Två observationer utan sådan hostpolicy ger **inte**
tillräcklig exklusivitet.

### Läs-only hostobservatör – en separat, liten delgrind

`scripts/services/modbus_broker_host_observer.py` kan nu skapa en
`FenceObservation` med hjälp av **endast** `systemctl show`,
`ss --numeric --tcp --all` och filstatus för broker-IPC.
För att markera release som verifierad krävs root, en laddad
stoppad `solinteg-*.service` med `KillMode=control-group`,
utan privat network namespace, `MainPID=ControlPID=0`,
**inga** TCP-socketar på *denna host* till exakt konfigurerad
numerisk inverter-IP/port och **ingen kvarvarande IPC-socketväg**.
Okänd status, otillåtna behörigheter, kommandofel eller ofullständig
socketlista avvisas. En `TIME-WAIT`-anslutning räknas konservativt
som kvarvarande. Elva isolerade tester med simulerade OS-svar är
godkända.

Detta **startar eller stoppar ingen tjänst och öppnar inte TCP:502**.
Observatören är inte ansluten till watchdog/systemd eller
`fenced_emergency_auto`. Den bevisar **inte** att andra nätverksvärdar
saknar anslutningar, att processer har flyttat till andra
nätverksnamespace eller att brokern inte kan återstarta direkt efter
kontrollen. En privilegierad supervisor som hindrar återstart och
upprätthåller exklusivitet under hela överlämningen återstår.
**Fysisk cutover är fortsatt NO-GO.**

### Återstartsspärr med livstidslås – offline verifierad

Den förberedda read-only-brokerprocessen håller nu
`flock(LOCK_EX|LOCK_NB)` från **före** broker-/IPC-start tills
transporten stängts. Låset är en vanlig, förskapad, rotägd
`/run/solinteg-broker-ownership.lock` (föreslagen mode `0440`, ägare `root:solinteg`);
den är **inte** placerad under systemds `RuntimeDirectory`
som annars kan tas bort och återbildas med en *annan inode*.
Processen skapar, byter eller raderar aldrig låsfilen och
kontrollerar markören `/run/solinteg-broker-readonly-approved`
**ytterligare en gång under låset**. Saknad/otillförlitlig
inode, en befintlig låsägare eller indragen markör gör att
starten avbryts **innan TCP-klienten skapas**.

En framtida privilegierad nödfallback ska (1) dra tillbaka
godkännandemarkören, (2) stoppa brokerprocessen, (3) ta
**samma** lås och hålla det under **hela** självständiga
TCP-ägarkontrollen och Auto-återställningen samt (4)
lämna markören borttagen tills ett nytt manuellt go.
Om brokern försöker starta medan fallbacken håller låset
nekas uppstarten; får brokern låset först måste fallbacken
**avbryta utan Modbus-skrivning**.

`deploy/solinteg-broker-ownership.conf` är endast en
**ej installerad systemd-tmpfiles-template** för att skapa
samma root-ägda inode vid boot. `ConditionPathExists`
i den ännu ej installerade broker-enheten kräver filen.
Tester med *separata processer* verifierar konkurrens,
releasen vid undantag och att hela broker-IPC-livscykeln
ligger under låset. Detta ersätter **inte** hostsupervisorn,
brandväggs-/UID-isolering av direkta klienter eller
oberoende fysisk readback. Brokern och fallbacken är
**ännu inte sammankopplade**. Linux låset släpper vid
processkrasch: spärren mot återstart därefter bygger också
på att root-markören har dragits tillbaka.

**Inga** `/run`-filer, driftenheter eller Modbusförbindelser
har skapats som del av denna källkodsändring.
**Live-cutover är fortfarande NO-GO.**

### Koordinerad nödfallback – offline, inte host-ansluten

`scripts/services/modbus_broker_supervisor.py` kopplar nu ihop
den tidigare fencing-kontrollen med **samma livstidslås** som brokern
har, men endast med **injicerade callbacks**; ingen direkt
`systemctl`-, `/run`-, TCP- eller fysisk Modbus-implementation finns.

Ordningen är strikt: **dra tillbaka godkännandemarkören → verifiera
att den är borta → stoppa brokern → ta exklusivt `flock` → verifiera
markören och två oberoende OS-observationer → kontrollera markören
omedelbart före ett enda Auto-försök → släpp låset först när
readback är klar**. Under hela övertagandet kan en återstartande
broker som följer samma låsprotokoll inte få Modbusägarskap.
Om markören återkommer, brokern inte stannar, låset är upptaget,
socketägarskap är oklart eller Auto-readback misslyckas rapporteras
explicit fel och **inga nya skriverförsök** görs. Denna kod
**återaktiverar aldrig godkännandemarkören**.

`scripts/tests/test_modbus_broker_supervisor.py` testar
övertagandet med **18 offlinefall** inklusive konkurrerande
underprocess, lås över simulerad fysisk återställning, förlorade
godkännanden och misslyckade readbacks. En separat skrivspärr
`before_write` i `modbus_broker_fencing.py` gör att en återkommen
godkännandemarkör stoppar försöket **även efter dubbel observation**.

**Fortfarande saknas en privilegierad OS-adapter** som faktiskt
drar tillbaka root-markören, stoppar rätt systemd-unit, tar det
root:solinteg-ägda produktionslåset, verifierar faktisk process- och
nätverksägare samt försöker ett verifierat General/Auto.
Säkerhet måste också upprätthållas efter fallbackens retur;
oanslutna direktklienter måste blockeras och aktiv dispatcher
hantera stale-data. Ingen produktionsservice eller markerfil
har installerats eller ändrats. **Live-cutover: NO-GO.**

## No-go: kvarstående säkerhetsgrindar före någon live-cutover

1. **Stale plan fallback i aktiv dispatcher:** `read_live_reading`
   underkänner `live.json` över **120 s**, men
   `live_discharge_power_w` och `live_charge_power_w` kan
   ändå behålla `planned_power_w`. `prioritize_soc_recovery`
   öppnar också en **egen** kortlivad Modbusanslutning vid stale
   SoC. Ny brokerväg som kastar fler samples kan öka risken för
   föråldrade styrbeslut. Fail-closed-policy måste först designas,
   testas och godkännas i kombination med BMS-/SoC-återhämtningen.
   Att blint slå om till Auto vid lågt SoC kan vara lika skadligt
   och är **inte** beslutat.

2. **Endast *poller* äger persistent TCP i etapp 3.**
   Dispatcher och watchdog använder fortfarande direkta
   kortlivade TCP-förbindelser. Exklusivt TCP-ägarskap för
   hela systemet är därför **inte** uppnått. Ingen säker
   multi-write-/fenced watchdog-migrering är aktiverad.

3. **Provade telemetritrösklar saknas.** Historik gav
   **11/2 860** stora batteri-/AC-avvikelser och **62**
   driftlägesbyten över ett dygn; detta är preliminär
   heuristik och bevisar inte Modbus-race. Brokern kan med
   avsikt avvisa felaktig CT/phase när en gammal poller
   hade returnerat core-telemetri utan fassummor. Dessutom
   saknas ännu uppmätt read-only Modbuslatens per registerblock
   **mot den fysiska dongeln**. En riktig `pymodbus 3.15.0`
   har däremot verifierats mot en **lokal** fake-TCP-server
   (12 FC03, inga FC06), vilket testar protokollframing
   men inte leverantörsdongelns beteende.

4. **Host-fencing/fallback har offlineorkestrering men ingen privilegierad driftadapter.**
   Broker-krasch och ny brokerrestart får aldrig leda till
   två samtidiga ägare. Fysisk watchdog, UPS/BMS och
   tidigare fungerande dispatcher ska behållas under en
   separat kontrollerad migrering. Systemd-rolluppdelning
   för framtida control/safety kräver olika UID (nu samma
   `solinteg`).

5. **Återställning och godkännande:** Godkänd
   snapshot-backup av installerad app och service-konfiguration,
   verifierade `solinteg-poller`-/watchdogprocesser,
   manuell backup/rollback, övervakning och ett uttryckligt
   **go** krävs; dokumentations- eller källkodspush
   är inte ett sådant godkännande.

## Förberedd men inte exekverad manuell cutover

**Detta är en granskningschecklista, inte en körorder.**
Före varje systemd-ändring ska blockerarna ovan vara
åtgärdade och ett separat go/no-go upprättat. Bevara
ursprunglig dispatcher, watchdog, originalkällkod och
samtliga säkerhetsinställningar.

1. Verifiera live SoC, nät och batteri W, pollerfrekvens,
   service- och TCP-ägarskap och aktuellt styr-/planläge.
   Kontrollera att tid för en misslyckad polling inte gör
   att dispatcher väljer olämplig forced power.
2. Ta en kontrollerad, **verifierad** känd-god backup och
   dokumentera ursprungliga env-filer/systemd-enheter.
   De två nya templatefilerna i `deploy/` är **inte**
   installerade via commit/push.
3. **Stoppa först gamla pollern och verifiera att dess
   TCP:502-session är avslutad** innan read-only-broker
   över huvud taget får öppna en TCP-session. Efter ett
   **separat godkänt** go ska en root-ägd `0600`-marker
   `/run/solinteg-broker-readonly-approved` skapas;
   startvägen vägrar annars öppna brokeranslutningen.
   Koppla pollerns nya `broker`-drop-in till brokern och
   starta tjänsterna i rätt ordning under övervakning.
4. Jämför hela `live.json`-kontraktet och två+ kompletta
   pollingcykler mot verifierad baslinje. Kontrollera
   att ogiltiga samples inte gör live-tidsstämpeln
   färsk och att `readings` inte får skräp, och att
   fysisk watchdog och dispatcher förblir effektiva.
5. **Rollback omedelbart** om datum/SoC/faser/lagg
   eller drift-/säkerhetslarm avviker: stoppa nya
   pollern och brokern, verifiera att broker-TCP
   är släppt, ta bort den root-ägda cutover-markören
   **och** det explicit installerade drop-in-tillägget,
   `daemon-reload`, starta
   den tidigare direktpollern och verifiera
   färsk telemetri, fysisk batteri-/näteffekt,
   dispatcher och watchdog. **Inga dubbla pollers.**
6. Följ DoD enligt central
   [artefakthygien](https://github.com/ermhawi/engineering-standards/blob/v1.2.2/docs/artifact-hygiene.md).
   Avinstallera endast egna **inaktiva** provresurser,
   bevara installerad originalkod och driftbevis,
   dokumentera kvarstående risker.

**Implementationens status är fortfarande *NO-GO för fysisk
cutover*.** Nästa nödvändiga arbetssteg är en verifierad,
SoC-medveten stale-data-policy och därefter host-fencead
rollback/failoverdesign med offline-tester.
