# Solinteg-broker: gemensam normaldrift och oförändrad fallback (2026-11-04)

**Status:** **Driftsatt på Dell 2026-11-04 kl. 05:45:25 CET.**
Både poller och dispatcher använder nu en gemensam brokerprocess,
`solinteg-broker-control.service`, med en enda etablerad TCP:502-
anslutning. De ursprungliga registeralgoritmerna och den **befintliga
watchdoggens kod och systemd-enheter** är oförändrade.
Rootgodkännande, processlås och boot-autostart är installerade.
Rollback och originalhashar finns under den skyddade lokala
`/opt/solinteg/rollback-broker-20261104`. Den gamla
read-only-brokern förblir **inaktiv**.

**Beslut:** Fokusera på en gemensam TCP-anslutning. Ingen ny
nödfallback, förändrad watchdog, ändrat native EndSOC eller
SoC-baserad supervisorkontroll införs i detta steg.
[ADR och accepterade brister](architecture/adr-2026-11-04-broker-legacy-failure-scope.md).

## 1. Byggstenar och faktiskt dataflöde

```text
solinteg-poller.service --(read/FC03)----┐
                                         ├-- AF_UNIX broker.sock -- [EN broker-worker] -- TCP:502
solinteg-dispatch.service --(FC03/FC06)--┘                 |
            |                                      samma persistent client
            └-- befintlig dispatchlogik
solinteg-watchdog.timer --> befintlig watchdog.py --> direkt TCP:502 VID FEL
                           (avsiktligt INTE migrerad, känd brist)
```

| Ändrad källkod | Funktion |
| --- | --- |
| `modbus_broker_control_service.py` | Separat, explicit opt-in brokerprocess; rootgodkännande och `flock` före IPC/TCP |
| `modbus_broker_control_transport.py` | Verklig `pymodbus` FC03/FC06 till whitelistade register/slave 255 |
| `modbus_broker.py` / `modbus_broker_ipc.py` | Serialiserad worker, IPC, identitet, rå läs-/skrivbehörighet per roll |
| `modbus_broker_control_client.py` | `ModbusTcpClient`-kompatibel klient över Unix-socket; **ingen TCP fallback** |
| `inverter_control.py` | Default `direct`; opt-in `SOLINTEG_MODBUS_SOURCE=broker`. Alla tidigare force-/hold-/auto- och importlimit-funktioner oförändrade |
| `modbus_poller.py` | Default `direct`; opt-in `broker_legacy` använder samma `read_inverter`, CT-backoff och historikformat |
| `deploy/solinteg-broker-control.service` | **Installerad och aktiv** som `solinteg-broker-control.service`, rootmarker krävs |
| `deploy/solinteg-poller-broker-control.conf` | Poller via `broker_legacy`, `AF_UNIX` och `BindsTo` |
| `deploy/solinteg-dispatch-broker-control.conf` | Dispatcher via broker, `BindsTo`, stale-SoC-experiment avstängt |
| `deploy/solinteg-broker-ownership.conf` | **Installerad** tmpfiles-inode `root:solinteg 0440` med livslångt exklusivt `flock` |
| `deploy/solinteg-broker-control-approval.conf` | Rootägd `0600`-godkännandemarkör som återbildas vid normal hostboot när kontrollbrokern väl aktiverats |

**Viktiga garantier i normalvägen:**

- Endast brokern har en ordinarie TCP:502-klient; pollerns
  systemd-konfiguration blockerar `AF_INET/AF_INET6` helt.
  Dispatcher får endast begränsade brokerkommandon när
  `SO_PEERCRED` **och** `/proc/<pid>/cgroup` verifierar
  `solinteg-dispatch.service`. Poller får en separat, snäv
  FC03-allowlist. Operatörs-CLI och HA får ingen kontrollroll.
- Inga nya register eller effektstrategier. FC06-allowlist:
  `50000`, `50007`, `50009`, `50207`,
  `50208`, `50209`, `50210`. **Inga** skrivningar av
  `52502/52503`, identitetsregister eller fri FC06.
- En worker serialiserar transport och begränsar FC06 till
  minst två sekunder emellan. Ingen andra TCP-anslutning
  öppnas vid IPC-/Modbusfel.
- Under `broker_legacy` fortsätter samma gamla CT-blockfallback,
  `live.json`, SQLite `readings`, SoC-avkodning och
  felretry; den stränga snapshot-/SoC-supervisorpolicyn
  från tidigare PoC **är inte aktiverad** i denna migration.
- Hårdvaruskydd och befintlig driftlogik ändras inte.
  Home Assistants avaktiverade `solax_modbus` förblir avstängd
  och HA fortsätter via REST.

## 2. Kända brister – **medvetet uppskjutna**

1. **Normal exklusivitet gäller inte alla fel.** Om dispatcher
   dör men broker/poller fortfarande kör kan dagens watchdog
   ansluta direkt på TCP:502 samtidigt som brokern.
2. **Brokerbortfall:** `BindsTo` i de nu **installerade**
   drop-ins stoppar poller/dispatcher och låter gamla
   heartbeat-watchdoggen upptäcka uteblivna heartbeats.
   Dess direkta `return_to_auto` kan misslyckas liksom tidigare.
   Ingen ny privilegierad failover eller automatisk garantifunktion.
3. **Kvarhängande EMS-läge och SoC:** Last `50207` kan överleva
   ett avbrott; native EndSOC omkring 10 % är lägre än ekonomiska
   20 %-reserven. General är ingen garanterat reservbevarande fallback.
4. **Extern Modbus och nätfencing:** En extern/manuell TCP-klient
   kan fortfarande ansluta om nätverket tillåter det;
   Linux-`flock` gäller bara samarbetsvilliga lokala processer.
5. **Dell som gemensam fel-domän**, ingen fysisk återkopplingsgaranti,
   och ingen ny notifiering vid brokerhaveri.
6. **Firmware och fysisk fallback-verifiering återstår.**
   Grundläggande live-readback har visat EMS BattCtrl efter
   första `applied`-åtgärden, men varken samtliga effekter,
   fysisk återställning efter fel eller enhetens
   firmware-/BMS-specifika skyddsverkan är slutverifierade.

Detta är ett **accepterat riskregister**, inte lösta fel.
Ingenting i `watchdog.py`, `solinteg-watchdog.service`,
`solinteg-watchdog.timer`, `52502/52503` eller batteriets
fysiska skydd ändras.

## 3. Offlineverifiering

```bash
python3 -B -m unittest discover -s scripts/tests -p 'test_modbus_broker_*.py'
python3 -B -m unittest discover -s scripts/tests -p 'test_*.py'
sudo /opt/solinteg/app/.venv/bin/python scripts/tests/wire_smoke_control.py
systemd-analyze verify deploy/solinteg-broker-control.service
uvx --offline --from zensical zensical build
```

`wire_smoke_control.py` binder **enbart 127.0.0.1** med
kernelvald port, och kontrollerar riktig `pymodbus`, FC03,
två FC06, Unix-socket och en enda TCP-session. Ingen
växelriktare eller extern Modbusport kontaktas. Fakes täcker
gammal registerordning, SoC-skydd, 300 W-laddning, fast
50 W-urladdning, importtak, pollerdata, behörighetsavslag
och att klienten **aldrig** återgår till direkt nätverksanrop
när brokern försvinner.

## 4. Genomförd cutover på Dell

**Genomförd 2026-11-04 05:45:14–05:45:25 CET** med kontrollerad
rollback och utan ändring av den installerade watchdoggen.
Nedan dokumenteras både utförda steg och bevarad rollbackordning:

1. Ta versions-, drift- och backupbaslinje. Stäm av den
   **installerade** pollern/dispatchern med källkoden före
   kopiering; aktuell dispatcherkod skiljer sig från Git.
   Kopiera **inte** hela checkouten över aktiv installation.
   Lagra reversibla, verifierade kopior av de exakta berörda
   filerna och systemd-konfigurationerna.
2. Installera endast de granskade brokermodulerna,
   nödvändiga minimala befintliga klientuppdateringar
   och separata systemd-mallar. **Ingen marker** skapas än.
   Kontrollera att varken read-only- eller kontrollbroker
   har aktiv TCP-session.
3. Stoppa **både** dispatcher och direktpoller och verifiera
   att deras tidigare TCP-sessioner är borta. **Starta inte
   en parallell broker**. Den gamla watchdog-timern
   ändras inte och dess heartbeat-timeout måste beaktas
   i tidsbudget/rollback.
4. Installera rootägda tmpfiles-mallar
   `deploy/solinteg-broker-ownership.conf` och
   `deploy/solinteg-broker-control-approval.conf`;
   skapa därefter den skyddade lås-inoden och **root:root
   0600**-markören `/run/solinteg-broker-control-approved`.
   Markören återbildas vid hostboot **först efter denna
   godkända installation**, så normal styrning kommer tillbaka
   liksom dagens autostart. Först efter verifierad tom
   TCP:502-lista får kontrollbroker startas.
   Läs-only-servicen får aldrig köras samtidigt.
5. Starta först `solinteg-broker-control.service`, sedan
   migrerad poller och dispatcher. Verifiera med `ss`,
   journal och SQLite att exakt en ordinarie TCP-klient
   finns, samma SoC/effekt/plan, färsk telemetry, och att
   `control_actions` inte tillför nya fel eller
   förändrat fysisk import-/fasbeteende.
6. Om en kontroll fallerar: stoppa nya dispatch/poller,
   stoppa broker, avaktivera/remove **även tmpfiles-mallen
   för godkännandemarkören** (annars återkommer den efter
   omstart), ta bort den nuvarande markören och drop-ins,
   verifiera att TCP-sessionen frigjorts, återställ exakt
   tidigare klientfiler och starta **gamla** direktpollern
   samt dispatchern. Lämna gamla watchdoggen orörd.
   Återstarta aldrig båda TCP-ägarna samtidigt.
7. Dokumentera verkliga mått, anslutningsägare, fel,
   mätvärden och rollbackstatus. **Genomfört:** En broker-PID
   äger TCP:502; `applied` och flera färska pollerprover
   verifierade. Fysisk energikostnad/återställning vid fel
   är inte färdigverifierade och omfattas av bristregistret.

**Verifierad driftsättning:** Ursprungliga `inverter_control.py`
och `modbus_poller.py`, samt dispatcher-/poller-/watchdog-
systemd-enheter, sparades med SHA-256-hashar i en **root-skyddad**
rollbackkatalog. Endast de berörda tjänstemodulerna
ersattes; installerad `dispatch_loop.py` och dess
platsanpassade beteende lämnades oförändrad.
Dispatcher och poller stoppades kontrollerat; TCP:502
verifierades ledig innan brokern fick öppna anslutningen.
Rootmarker, OS-lås, två nya systemd-drop-ins och broker-enheten
installerades. Efter uppstart verifierades **exakt en**
etablerad TCP-session med brokerprocessens PID, färsk
`live.json`, tre nya SQLite-mätningar och `applied` för
första 50 W-batterisparkommandot.
Nästa två pollcykler bekräftade `0x303` (EMS BattCtrl) och
SoC/effektdata. Äldre read-only-broker är fortfarande inaktiv.
Detta är bevis för **normalvägen**, inte de uttryckligen
uppskjutna failure-fallen.

**Installerad rollback:** Vid driftproblem, använd rootägda
`/opt/solinteg/rollback-broker-20261104/rollback.sh`.
Skriptet stoppar migrerad dispatcher/poller/broker, avaktiverar
brokerautostart och dess godkännandemarkör, återställer
originalfilerna och startar direktpoller/dispatcher.
Den gamla watchdoggen är inte med i stop-/ändringssekvensen.
**Ingen rollback har behövt utföras hittills.**
Verifiera först nuvarande servicestatus/SoC samt incidentbild
innan man använder skriptet.

Se [riskbeslutet](architecture/adr-2026-11-04-broker-legacy-failure-scope.md),
[Modbus-felens nattbevis](solinteg-modbus-overnight-2026-11-04.md) och
[ursprunglig brokerplan](plans/modbus-broker-2026-11-01.md).
