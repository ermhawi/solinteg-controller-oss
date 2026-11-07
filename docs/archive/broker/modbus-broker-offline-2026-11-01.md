> **Arkiverad 2026-11-07:** Historiskt experiment eller migrationssteg, inte en aktuell driftinstruktion. Kör inte gamla installations-, test- eller rollbackkommandon utan ny verifiering. [Nuvarande drift](../../current-state.md) · [Arkivöversikt](../index.md).

# Solinteg Modbus-broker: etapp 0–2 (offline)

**Status:** Implementerad i källkod, testad mot minnesbaserad fake-dongel.
**Drift:** **INTE driftsatt**; ingen aktiv broker-tjänst, ny Modbus-anslutning
eller fysisk inverter-/EVCC-skrivning. Fysisk cutover är en separat grind.
**Branch:** permanent fristående `Sundsvall`, endast `origin/Sundsvall`.

## Baslinje och klientinventering (2026-11-01)

Read-only inventering av den installerade installationen:

| Befintlig klient | Kontaktmodell | Registren / effekt | Status |
| --- | --- | --- | --- |
| `modbus_poller.py` | Persistent FC03 mot slave 255, timeout 10 s, cykel 30 s | 10994–11029 (CT/AC/PV), 30258–30259 (batteri), 33000–33003 (SoC), 50000 (mode), valfritt 50006; fallback 11000–11029 | Aktiv |
| `dispatch_loop.py` → `inverter_control.Inverter` | Kortlivad FC03/FC06, timeout 5 s | 50207, 50208, 50209, 50210, 50000, vid behov säkerhets-/limitregister; rate limit minst 2 s | Aktiv |
| `watchdog.py` → `Inverter` | Oberoende kortlivad FC03/FC06 | General/Auto-återställning när heartbeat äldre än 240 s; timer var 2 min | Aktiv |
| `inverter_control.py` CLI och `scripts/tools/probe_*.py` | Manuellt initierade direkta klienter | Kan skriva skyddade register; kräver separat kontrollplan/underhållsfönster | Verktyg, **inte migrerade** |
| EVCC via Waveshare | **Separat** RS-485/Modbus ASCII | Laddbox, inte inverter-TCP | **Utanför scope** |

Inga gamla tjänster stoppades för mätningen. Under en read-only kontroll
fanns en etablerad anslutning till TCP:502. Aktiva ordinarie
`solinteg-poller.service`, `solinteg-dispatch.service` och
`solinteg-watchdog.timer` verifierades.

**Aggregerad, privat databasbaslinje över de senaste 24 timmarna**
(`readings`, `julianday(timestamp)` för korrekt ISO-tidsjämförelse):

| Mått | Resultat |
| --- | ---: |
| Mätningar | 2 860 |
| Median mellan mätningar | 30,13 s |
| 95:e percentilen | 30,15 s |
| Luckor över 45 s / 60 s | 3 / 3 |
| Driftlägesbyten mellan intilliggande mätningar | 62 |
| CT-fassumma utanför 30 W eller 5 % av nätmätare | 0 |
| AC kontra (PV + batteri) utanför 300 W eller 15 % AC | 11 |

**Begränsningar:** Den sista energibalansregeln är en konservativ
**hypotes**, inte en verifierad on-device-kvalitetsgräns, och de
61+ mode-bytena ger ingen säker tidpunkt *inom* en enskild
FC03-serie. Historisk `readings` innehåller inte per-registerlatens,
FC03-timeouts eller mode före/efter en pollcykel. De kan **inte**
rekonstrueras från dessa poster; nästa read-only etapp måste mäta dem.
Råa hushålls-/faskurvor och privata hostadresser publiceras inte.

## Implementerad offlinekärna

| Källfil | Funktion |
| --- | --- |
| `scripts/services/modbus_broker.py` | En worker, en injicerad transportanslutning, prioriterad och serialiserad transaktionskö, arming, idempotens, safety latch/generation, FC03-tidsstämplade snapshotblock och konservativ mätkvalitet |
| `scripts/services/modbus_broker_ipc.py` | UDS-baserad JSON request/reply, Linux `SO_PEERCRED`, gräns på meddelandestorlek, strikta metoder och default **read-only** för lokal UID |
| `scripts/tests/test_modbus_broker.py` | Minnesbaserad fake med **en** tillåten anslutning, kontrollerade register och faults, samt riktig lokal tillfällig UDS-socket |

Det finns avsiktligt **ingen importerad `pymodbus`-klient**, inget
`SOLINTEG_HOST`-baserat startflöde, ingen produktionssocket till
växelriktaren, ingen systemd-unit och ingen automatisk deployment i
denna leverans.

Testfaken är en **in-memory transport**, inte en Modbus TCP-server
med wire-level-framing. Pymodbus-adapter, verkliga TCP-framingtester,
service-ACL med olika Unix-UID och oberoende host-fencing återstår
före live migrering.

### Snapshotmodell

En sample läser `50000` **före och efter** registerblocket och
registrerar tidsintervall för varje FC03-läsning. `sample_id`,
`mode_before`, `mode_after`, `duration_s` och `blocks` kompletterar
befintliga pollerfält. Råvärden kan inte göras fysiskt atomära
av mjukvara; ett stabilt mode mellan läsningarna utesluter inte
internt transientvärde.

En sample får bara `status=valid` efter kvalitetskontroll;
`mode_changed`, `ct_invalid`, `stale`,
`energy_balance_invalid` och `timeout` returnerar
`status=rejected` och `sample=None`. Senast giltig timestamp
**uppdateras inte** när en senare sample förkastas.
En separat `rejection_sink` kan få enbart tid, registerblock och
avvisningsorsak (ingen privat effekt/SoC-payload). **Ingen driftsatt
diagnostikdatabas eller persistent reject-logg** ingår ännu.
För höga avvisningsandelar måste leda till mätning/kalibrering,
inte tyst ökning av godkännandetoleranserna.

### Säkerhets-/åtkomstmodell

- `armed=False` som default; semantiska
  `charge`/`discharge`/`reserve_hold` kräver explicit arming,
  unik `request_id` och en separat injicerad fasguard.
  `charge`/`discharge` kräver också explicit verifierad
  `charge_sign=neg_charge|pos_charge`; utan den är all
  tvångsladdning/urladdning blockerad. Okänd rå
  FC06 via IPC tillåts inte. `reserve_hold` är dessutom avslagen
  tills just detta driftläge validerats på lokal enhet.
- I fake-kontroll utförs hela skrivsekvenser av **en enda worker**,
  med 2 s minsta skrivintervall och FC06-readback. Resultat märks
  `registers_verified_only`, **aldrig** fysisk effekt verifierad.
  Vid fel görs best-effort Auto; misslyckad återställning rapporteras
  uttryckligen. Denna kod är **inte** godkänd att styra live hårdvara.
- `safety_auto` har högst köprioritet och invaliderar äldre
  generationer. Safety-latch förbjuder nya kontrollkommandon.
  `auto` utan tidigare brokerägt tvångsläge är en no-op
  för att inte skriva över operatörens manuella läge. Vid ordnat
  workerstopp återställs brokerägt tvångsläge till Auto och
  misslyckad återställning rapporteras som fel.
  Ett redan påbörjat I/O-anrop kan inte fysiskt preemptas:
  worker kontrollerar generationen **mellan** operationerna,
  och försöker Auto vid partiellt avbrutet kommando.
- Peer-roll bestäms av OS-kredentialer, inte klientens JSON.
  Default-rullen är endast read. **Nuvarande poller, dispatcher och
  watchdog delar Unix-UID `solinteg`**: oskiljbara roller är en
  **blockerare** för produktion, kräver nya systemd-UID/ACL och
  verifierad oberoende nödfallback med fencing av TCP-ägaren.
- Läsning via UDS ger inga råa fysiska writes. Detta ersätter
  **inte** BMS-/native SoC-floorskydd, fysisk CT-övervakning eller
  reservwatchdog. Inte heller säkerställs att inverterläget överlever
  uppstarts-/stoppsekvenser säkert förrän separat hardware-test.

## Testning utan fysisk anslutning

```bash
python3 -m unittest discover -s scripts/tests -p test_modbus_broker.py -v
python3 -m unittest discover -s scripts/tests -p 'test_*.py'
```

Offline regressioner täcker bland annat: överlappande klienter,
samtida förfrågningar som ändå använder bara **en** fake-transport,
en 8 s cloud-stall, stale sampling vid 25 s stall, lägesväxling
under FC03, felaktig CT-summa, exakt **−1692 W batteri / +978 W AC /
0 W PV / −11 W nät** från tredje SoC25-provet, kvalitetssink utan
rådata, olika FC06-felpositioner och rollback, 2 s skrivintervall,
idempotens, `safety_auto` som fence:ar både köade och pågående
kommandon utan att det senaste säkerhetskommandot fence:as av en
äldre avbruten write, no-op för obegärd Auto, återställning vid
kontrollerat workerstopp, request-timeout, explicit 50207-polaritet,
disarming, SoC-gränser samt
`SO_PEERCRED`-/IPC-rollkontroll.

**Testerna verifierar en simulerad broker, inte fysisk
fusesäkerhet eller fysisk Auto efter brokerprocessens död.**
Ingen driftsatt service använder nya modulerna.

**Verifiering 2026-11-01:** 38 nya broker/IPC-tester godkända,
inklusive tre upprepade körningar av felinjektionssviten.
Totalt **1 079 Python-tester godkända**.
Frontendregressionerna (**Vitest: 697 godkända, 1 överhoppad**)
påverkades inte. Dokumentationskontroll och Zensical-bygge godkända.
Ingen ny serverstart eller fysisk Modbusanslutning.

## Öppna design- och leveransgrindar

1. Inför riktig, testbar pymodbus-adapter endast bakom separat
   `read_only`-godkännande; extra FC03-klient parallellt med
   dagens poller vore olämpligt. Tillämpa single-owner cutover.
2. Välj UDS-rollmodell med **olika** systemd-UID/grupper för
   read, control och safety; Socket-ACL måste valideras mot
   faktisk `SO_PEERCRED`.
3. Bygg en separat systemd-superviserad watchdog och en
   **host-fencead** reservväg som först stoppar broker/TCP-ägarskapet
   och sedan ensam försöker General/Auto. Testa brokerhaveri,
   fastlåst I/O, SSH-bortfall och nätfel på fake före driftsättning.
4. Kalibrera per-blocklatens och energibalans via read-only
   observerbarhet och verklig tidsinformation. **Förkasta**
   modeflip/inkonsekvens; gör inte gränsen godtyckligt tolerant.
5. Bevara `live.json`/SQLite-kontraktet och säkra
   dispatcherns fallback vid invalid/stale data innan någon
   kontrollväg migreras.
6. Designa hel transaktionsägarskap över policy/phaseguard,
   fysisk readback och nödkontroll; kopiera inte bara kodvägen
   till en ny process. Behåll original release/rollback.

## Avveckling och drift

Inget nytt installerades på servern. Tester skapar enbart
temporära lokala UDS-filer under en testägd
`tempfile.TemporaryDirectory`, som stängs och tas bort.
Den befintliga dispatcher-, poller- och watchdog-stacken ska
**inte** startas om eller stoppas som del av etapp 0–2.

[Planen](../../plans/modbus-broker-2026-11-01.md) och
[ADR:n](../../architecture/adr-2026-11-01-single-owner-modbus-broker.md)
fortsätter styra varje separat godkänd cutover.
