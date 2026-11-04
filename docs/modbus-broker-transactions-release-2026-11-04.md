# Driftsättning: brokerägda hela styrtransaktioner (2026-11-04)

**Status 2026-11-04:** **DRIFTSATT på Dell kl. 07:53:55 CET**.
Preprodverifiering, separat root-skyddad rollbackbackup och isolerat
återläsningsprov var godkända före bytet. Broker och dispatcher
använder nu `SOLINTEG_BROKER_TRANSACTIONS_ENABLED=1` respektive
`SOLINTEG_MODBUS_SOURCE=broker_transaction`. En broker-worker
utför hela FC03/FC06-styrsekvensen. Verklig styråtgärd och tre
påföljande pollerprover verifierade; se avsnitt 8.

Beslut:
[ADR: hela transaktioner, ingen ny fallback](architecture/adr-2026-11-04-broker-complete-transactions.md).
Befintlig driftsättning och rollback:
[broker normaldrift](modbus-broker-control-stage-2026-11-04.md).

## 1. Releaseinnehåll

**Endast dessa källfiler** ska överföras från granskad commit
på `Sundsvall` till installerad
`/opt/solinteg/app/scripts/services/`:

1. `inverter_control.py` (opt-in klientväxling och delegation)
2. `modbus_broker.py` (köad komplett transaktionsbegäran)
3. `modbus_broker_ipc.py` (IPC-argument, typade fel)
4. `modbus_broker_control_client.py` (nytt transaktions-API)
5. `modbus_broker_control_service.py` (serverns explicit opt-in)
6. `modbus_broker_transactions.py` (**ny**, worker kör gamla algoritmer)

Två **nya**, separat opt-in systemd-drop-ins:

- `deploy/solinteg-broker-transactions.conf` ->
  `/etc/systemd/system/solinteg-broker-control.service.d/30-transactions.conf`
- `deploy/solinteg-dispatch-transactions.conf` ->
  `/etc/systemd/system/solinteg-dispatch.service.d/30-transactions.conf`

Allt annat är kvar: installerad `dispatch_loop.py`,
`modbus_poller.py` (`broker_legacy`), kärnans
`modbus_broker_control_transport.py`, live-/SQLite-schema,
systemd-basenheterna, `solinteg-watchdog.*`,
`watchdog.py`, `DISPATCH_BROKER_STALE_GUARD=0`, observer,
EVCC samt native `52502/52503`. Inga manuella skrivningar
eller fysiska 25%-prov ingår.

## 2. Offline-acceptans före produktionsfönster

Kör från en ren utcheckning av `Sundsvall`:

```bash
python3 -B -m unittest discover -s scripts/tests -p 'test_*.py'
sudo /opt/solinteg/app/.venv/bin/python scripts/tests/wire_smoke_transactions.py
sudo /opt/solinteg/app/.venv/bin/python scripts/tests/wire_smoke_control.py
sudo /opt/solinteg/app/.venv/bin/python scripts/tests/wire_smoke_readonly.py
npm run lint
npm run typecheck
npm test -- --testTimeout=15000
uvx --offline --from zensical zensical build
git diff --check
```

`wire_smoke_transactions.py` använder **ENBART
`127.0.0.1`/kernelvald testport** och lokal Unix-socket, aldrig
`SOLINTEG_HOST` eller den verkliga växelriktaren.
Verifiera särskilt de nya testerna för:

- charge/discharge/hold/freeze/Auto/importbegränsning med
  samma registerordning, readback, snabbväg och SoC-skydd;
- fast 50 W-omställning och normal 300 W-skyddsladdning
  vid lägre SoC, utan att ändra befintliga beslutsregler;
- pollerbegäran **kan inte** interfolieras mellan skrivningar;
  endast en fysisk anslutning;
- `read`-rollen och rå FC06 kan **inte** styra i
  transaktionsläge; äldre broker avvisas utan direkt fallback;
- en timeout stoppar återstående delsteg i kön,
  men **är inte en ny garanterad hardware-rollback**;
- `FreezeNotEnforced` har kvar sin distinkta feltyp så
  dagens dispatcher fortsätter larma/avaktivera freeze korrekt;
- idempotens på `request_id`, 90 s IPC-kopplat timeout,
  indata-/enhets-/envvalidering och den tidigare broker-sviten.

## 3. Driftgrindar (GO/NO-GO)

**Historisk GO/NO-GO-lista före bytet 07:53:55 CET.**
Innan en operatör godkänner en framtida omdriftsättning:

- Nulägets broker/poller/dispatcher/observer/watchdog är
  aktiva utan omstarter; `solinteg-broker-readonly.service`
  är avstängd, TCP:502 har **endast en ägare**.
- Normala mätcykler och kontrollåtgärder uppmätta mot
  senaste baslinjen, utan nya protokollfel/svarsförväxling.
  Ett längre driftsintervall är bra men inte en alternativ
  hårdvarusäkerhetsgaranti.
- Skapa rootskyddad separat release-/rollbackkatalog med
  exakt installerade sex originalfiler (en kan saknas i
  gamla versionen), SHA-256-verifiering, och **två**
  systemd-drop-in-backuper (inklusive notering om de saknas).
  Bevara även den tidigare fulla återställningen under
  `/opt/solinteg/rollback-broker-20261104`.
  Varken källkods- eller systemd-ändringar från andra uppgifter
  får skrivas över.
- Säkerställ att de 6 nya filerna och de 2 drop-in-filerna
  matchar den granskade commitens SHA-256 innan uppstart.
  **Ingen ändring av site-`.env` krävs.**
- Dokumentera klockslag, broker-PID, SoC, aktivt mode,
  senast lyckat kontrollutfall, backup- och rollback-ID
  före bytet. Stoppa vid färskhets-/SoC-/nätfel.

## 4. Produktionsbyte – genomfört 2026-11-04 kl. 07:53:45–07:53:55 CET

1. Installera de verifierade sex Pythonfilerna som
   `solinteg:solinteg 0644` utan att röra aktiv
   `dispatch_loop.py` eller watchdog.
2. Stoppa först `solinteg-dispatch.service` och därefter
   `solinteg-broker-control.service`. Den installerade
   `BindsTo`-kopplingen stoppar då också pollern;
   verifiera detta, att gamla brokerprocessen slutat och
   att dess TCP:502-socket är stängd. Ingen ny direktklient.
3. Installera de två `30-transactions.conf`-drop-ins och
   kör `systemctl daemon-reload`. Rör **inte** existerande
   `20-broker-control.conf`, lås-/markörfil,
   `solinteg-watchdog.timer` eller `SOLINTEG_HOST`.
4. Starta `solinteg-broker-control.service`, sedan
   `solinteg-poller.service`, sedan
   `solinteg-dispatch.service`.
   Den gamla watchdogen förblir aktiv/orörd.
5. Kontrollera via `systemctl show -p Environment` att
   brokern har `SOLINTEG_BROKER_TRANSACTIONS_ENABLED=1`
   och dispatchern `SOLINTEG_MODBUS_SOURCE=broker_transaction`.
   Broker ska ha **en** etablerad TCP:502,
   inga andra lokala skrivare. I loggen ska komplett
   registertransaktion synas, och dispatchern ska logga
   utfallet `applied` för en naturligt planerad åtgärd.
6. Läs **endast existerande lokal telemetri** i
   `live.json`, SQLite och journal. Minst tre på varandra
   följande giltiga pollerprover, färsk dispatcher-heartbeat
   och observer `healthy` utan nya `FC03/FC06`-fel.
   Om ingen naturlig styråtgärd infaller under fönstret:
   avvakta en; skicka **inte** ett extra provkommando
   bara för att skapa ett lyckat utfall.
7. Följ 24–72 h fördröjning, mätluckor, dubbla FC06,
   restart och utfall. Definiera separat operativ GO
   efter första verifierade naturliga styrtransaktion;
   annars använd rollback.

**Viktig begränsning:** En komplett brokertransaktion
kan blockera pollern under en längre skrivsekvens.
En misstänkt 30-s mätlucka under tung styrning ska
värderas i journal, inte automatiskt tolkas som ett
förstört Modbus-svar.

## 5. Rollback till den nu fungerande brokern

1. Stoppa **endast** migrerad dispatcher och broker;
   `BindsTo` stoppar pollern. Se till att gamla
   TCP-socket verkligen stängts.
2. Ta bort enbart de **två egna**
   `30-transactions.conf`-drop-ins. Återställ de sex
   filer som berördes till SHA-256-verifierade kopior
   från releasekatalogen. Om
   `modbus_broker_transactions.py` inte fanns tidigare,
   ta bort **endast den versionsskapade filen**.
3. `systemctl daemon-reload`; starta tidigare
   `solinteg-broker-control.service`, poller och dispatcher.
   Verifiera `SOLINTEG_MODBUS_SOURCE=broker`, en TCP:502
   och färsk telemetri. **Ingen** direktpoller eller
   omprogrammerad watchdog.
4. Låt återställningskatalog, gamla watchdog och tidigare
   full-brokerrollback finnas kvar. Dokumentera tidpunkt,
   orsak och resultat; radera inte okända resurser.

Om **även gamla normalbrokern** slutar fungera finns
dess redan dokumenterade **separata** fullrollback till
direktclient i
`/opt/solinteg/rollback-broker-20261104/rollback.sh`.
Använd inte den automatiskt vid ett vanligt
transaktions-releaseproblem.

## 6. DoD och uppskjutna brister

Transaktionskoden kan kallas **releaseklar i källkod**
när alla teststeg är gröna, dokumentation uppdaterad och
Git-hygienen ren. **Driftsatt** får stå först efter att
produktionsgrindar och ett verkligt kontrollutfall är verifierade.

Watchdog/BMS-/SoC-/stale-EMS-failure-scenariot förblir
uttryckligen **utanför omfattningen**. Det är en känd
brist i [normalbroker-ADR](architecture/adr-2026-11-04-broker-legacy-failure-scope.md)
och blir inte löst av att registersekvenserna körs odelat.

## 7. Historisk preprodverifiering före kl. 07:53 – **ingen aktivering i detta steg**

**Källbas:** `ea2b490242bfecb6529f1bb6cf164382218e0f9c`,
`origin/Sundsvall`. Separata verifieringskommandon slutfördes
med exitkod 0:

| Grind | Resultat |
| --- | --- |
| Full Python-regression | **1 366 tester OK**, inga fel |
| Frontend, ESLint och TypeScript | **705 tester OK, 1 överhoppat**, lint och typecheck OK |
| Riktig pymodbus mot `127.0.0.1` | **Tre** smoke-tester OK: hela transaktioner, ordinarie kontroll och read-only |
| Dokumentation och systemd | Zensical-build OK, `systemd-analyze verify` för basenheten **och båda sammansatta kandidatoverläggen** OK, `git diff --check` OK |
| Aktuell driftbaslinje | Broker, poller, dispatcher, watchdog och observer aktiva; **en TCP:502-ägare**; färsk telemetri |

**Förberedda, separat från aktiv produktion:**

- Root-skyddad katalog
  `/opt/solinteg/preprod-broker-transactions-ea2b490-20261104`
  (endast root, `0700`) med **fem** bytekopierade originalmoduler,
  dokumenterad frånvaro av den sjätte nya modulen, baslinjekopior av
  fem systemd-enheter och två befintliga `20-broker-control.conf`.
  Båda nya `30-transactions.conf` saknades i produktion och är
  markerade som **frånvarande** i backupens metadata.
- Kandidatpaket med **sex** Pythonmoduler och **två** separata opt-in
  systemd-överlägg. `SHA256SUMS` verifierar kopiorna; de befintliga
  originalfilerna och `20-...`-överläggen jämfördes byte för byte
  mot den oförändrade produktionen.
- **Återläsningsprov utan driftpåverkan:** originalen kopierades till
  en *isolerad tillfällig katalog* och jämfördes med både backuper
  och installerat innehåll. Även kandidaterna kontrollerades efter
  kopiering. Den tillfälliga katalogen städades.
- `preflight.sh` kontrollerar **endast läsande**: samtliga
  hashvärden, att den gamla koden/konfigurationen fortfarande gäller,
  exakt en brokerägd TCP:502-session, aktiva ordinarie enheter,
  färsk telemetri och `healthy`-observer.
  **Preflight: GODKÄND.**
- `rollback.sh --check` verifierar artefakter utan sidoeffekter.
  **Syntax, checksumma och torrkörning godkända**.
  `rollback.sh --execute` är **avsiktligt inte körd**; verklig
  återställning och all systemd-/Modbuspåverkan får först provas
  efter separat beslutat produktionsfönster.
- Den tidigare fulla brokerns rollback i
  `/opt/solinteg/rollback-broker-20261104` bevaras. Den nya
  preprodbackupen finns **lokalt på Dell**, inte på en separat
  återställningsvärd; det är en release-/rollbackbackup, inte
  en oberoende katastrofbackup.

**Oförändrad drift:** Inga filer under aktiv
`/opt/solinteg/app`, `/etc/systemd/system` eller
`/run` har skrivits av denna preprodkontroll. Inga tjänster
har stoppats/startats, ingen ny TCP:502-anslutning eller fysisk
registerskrivning har genomförts. Den driftsatta dispatchern
använder fortsatt `SOLINTEG_MODBUS_SOURCE=broker`,
inte `broker_transaction`. Watchdoggen och de tidigare
accepterade failure-brister har inte ändrats.

**Vid denna tidpunkt återstod:** separat godkänd cutover,
naturlig transaktionsreadback och uppföljning 24–72 h.
Cutover och första liveverifieringen är nu utförda enligt
avsnitt 8. En isolerad backupåterläsning är inte liktydig med
testad fysisk systemd-rollback eller hårdvaruatomära writes.

## 8. Genomförd produktionssättning och första liveverifieringen

**Start:** 2026-11-04 kl. **07:53:45 CET**.
**Slutförd första cutovergrind:** kl. **07:53:55 CET**.
**Källbas:** `ea2b490` på `Sundsvall`, separat
preprod-/rollbackkatalog
`/opt/solinteg/preprod-broker-transactions-ea2b490-20261104`.

1. Preflight, SHA256 och `rollback.sh --check` godkändes på
   aktiv originalinstallation; SoC **20 %**, mode `0x0303`.
2. Den gamla dispatchern stoppades först och körde befintlig
   Auto-återställning. Därefter stoppades broker och poller;
   **TCP:502 var stängd** före att ny broker startades.
3. Exakt **sex** verifierade Pythonmoduler kopierades till
   aktiv tjänstekatalog samt en ny `30-transactions.conf`
   till var och en av broker och dispatcher.
   `systemctl daemon-reload` och startordningen
   **broker → poller → dispatcher** genomfördes.
   `dispatch_loop.py`, pollerns `broker_legacy`,
   `watchdog.py`, `solinteg-watchdog.*`, observer och
   native `52502/52503` lämnades orörda.
4. Broker, poller, dispatcher och watchdog/observer var
   aktiva utan omstarter efter bytet. **En** etablerad
   TCP:502-anslutning ägdes av broker-PID **4054941**.
   Konfigurationen bekräftades som
   `SOLINTEG_BROKER_TRANSACTIONS_ENABLED=1` och
   `SOLINTEG_MODBUS_SOURCE=broker_transaction`,
   med `DISPATCH_BROKER_STALE_GUARD=0` oförändrad.
5. **07:53:57** loggade brokern fullföljd
   `transaction_import_limit` (**10 500 W**).
   **07:54:07** loggade den fullföljd
   `transaction_charge` (**300 W**), och dispatcherns
   första naturliga `charge`-åtgärd fick `applied`.
   Den befintliga kodens registerreadback utfördes inne
   i en och samma broker-worker.
6. **Tre** efterföljande mätningar, kl. **07:54:27–07:55:27**,
   visade samtliga mode `771` (`0x0303`) med **30,1 s**
   mellan mätningarna. `live.json`, SQLite och
   dispatcher-heartbeat var färska; observer var `healthy`.
   Inga nya Modbus- eller pollerfel observerades vid
   kontrollen. **Ingen rollback behövdes.**

**Återställning:** `rollback.sh --execute` i den skyddade
preprod-/rollbackkatalogen är redo för ett kontrollerat
återställningsbeslut och var endast syntax-/torrkörningstestad
före produktionsbytet. Den har **inte** exekverats mot aktiv
produktion. Den föregående kompletta brokerrollbacken under
`/opt/solinteg/rollback-broker-20261104` är också bevarad.
Den root-skyddade releasen behåller `cutover.sh`, terminal
`CUTOVER_STATUS` och verifierade SHA-256 för dessa samt
watchdoggens oförändrade systemd-filer.

**Återstår:** 24–72 timmars uppföljning av faktisk normaldrift,
inklusive väntetider för pollern under längre transaktioner,
förändringar i energiflöde, nya protokollfel, återstarter och
kontrollåtgärder. Liveverifieringen bevisar inte framtida
felfrihet eller att halvutförda FC06 kan återställas atomärt.
Befintliga dokumenterade fel-/watchdogbrister är oförändrade.
