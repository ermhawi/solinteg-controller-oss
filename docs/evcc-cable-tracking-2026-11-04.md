# EVCC-kabeln som sessionsgräns – urkoppling/återanslutning

**Datum:** 2026-11-04

**Status:** Driftsatt som tillägg till befintlig läsande
`solinteg-ev-observer.service` på Dell; anslutning bekräftad.
**Statusuppdatering 2026-11-04:** Automatisk EVCC-prisstyrning är
**nu driftsatt**, med strikt FC10-ägarproveniens, 6 A-gräns och
empiriskt accepterat energibehov. `A1` och nya kabel-epoker
får fortfarande aldrig återstarta en gammal prispaus.
Se [live-ADR](architecture/adr-2026-11-04-ev-price-empirical-live.md).
Äldre avsnitt nedan beskriver driftsituationen före aktiveringen.

## Krav och observerat faktum

Befintlig `solinteg-ev-observer.service` gör endast två FC03-läsningar
var cirka 30:e sekund. Styrpilotens EVCC-tillstånd tolkar vi som:

| FC03 CP-state | Fysisk tolkning | Priscontroller |
| --- | --- | --- |
| `A1`, `A2` | Ingen bil ansluten | Ingen skrivning |
| `B1`, `B2` | Bil ansluten, ej bekräftad 6 A-laddning | Ingen automatisk start utan övriga grindar |
| `C1`, `C2` | Bil ansluten, `C2` kan ha fysisk laddström | Kontrollera Ict2 och L3 separat |
| Övrigt, timeout, ogiltigt svar | Okänt/fel | Ingen skrivning |

Historikern gav faktiska `A1`→`B2`-prover 2026-11-04.
**Vid detta tillfälle var `configured_pwm_raw=1000` och
`pilot_pwm_raw=267`.** Fysisk kabelstatus kommer från EVCC:s
CP-state, inte av att dessa PWM-register råkar vara lika.
**Senare offlinekorrigering 2026-11-04:** Strikt, tillståndsberoende
kontroll kan verifiera en ansluten paus som `B2/1000/267`
**om** alla tre Ict-kanaler är färska och Ict2 är högst 0,5 A.
Detta är **inte** en återstartsrätt: verifierad egen
FC10-prispaus, aktuell kabel-/observationskontinuitet och
fysisk kvalificering krävs fortsatt. En separat
Volvo-appstoppdetektor ingår inte i aktuell omfattning. Se [uppdaterad pris-ADR](architecture/adr-2026-11-04-ev-price-plan-and-chart.md).

## Implementation

`scripts/services/evcc_cable_tracker.py` konsumerar samma lokala
FC03-observation som den befintliga `evcc_observer.py --watch`.
Inga ytterligare nätverksanrop, inga nya systemd-tjänster, inga
EVCC-/bil-/inverterkommandon. Endast när
`EVCC_CABLE_TRACK_PATH` är satt aktiveras den lokala uppföljningen.

- En tillståndsändring kräver **två successiva skilda** aktuella
  observationer. Det skapar `disconnected`, `connected` eller
  `unknown`, med tidsstämplar från *FC03-läsningen*, inte tidpunkten
  då användaren rent fysiskt drog ur kontakten.
- Återanslutningen `disconnected`→`connected` ökar en
  **persistent, monoton** `connection_epoch`. Första synliga
  `connected` efter tjänstestart är inte en verifierad återanslutning.
- FC03-fel, oväntade CP-states, gammal data och mellanrum över 100 s
  sätter kontinuiteten till `unknown`. Två nya anslutna prover
  utan observerad urkoppling skapar **inte** en falsk reconnect.
- Filer skrivs atomiskt med fsync och `0600`; ogiltig tidigare
  state överges inte automatiskt. `confirmed_replug`,
  `lossless=false`, `physical_control_eligible=false` och
  `device_writes=0` uttrycker begränsningarna. Läsaren
  `read_fresh_connected()` avvisar även en pågående enprovstransition.
- Priscontrollern kräver nu filen som **obligatorisk interlock**:
  frånkoppling/stale/pending stoppar kommandon och återkallar
  ägarskap; pausägaren får `evcc_cable_epoch`,
  `cable_coverage_epoch` och `cable_tracker_id` vid skapandet.
  Den kontinuerliga observatören ökar `coverage_epoch` efter
  FC03-fel eller mätuppehåll, och en ny initierad statusfil får
  ny `tracking_id`. Inga av dessa fält är förlustfri fysisk
  urkopplingsdetektion.
  Ändrad epoch efter ur-/inkoppling **återkallar** den gamla
  prispausens möjlighet till återstart. Driftmallen kräver
  en färsk kabelfil när priscontrollern en dag aktiveras.

## Automatisk fortsättning efter att kabeln ansluts

1. **EVCC redan på `raw=100` (6 A):** bilen kan börja ladda när
   CP går till `C2`, om den själv accepterar laddning.
   Ingen FC10-start behövs eller görs av tracker.
2. **EVCC pausad på `raw=1000`:** återanslutning i `B2`
   bekräftar kabeln men **startar inte laddningen**.
   Priscontrollern får inte ärva ett tidigare pausägarskap.
   För att i framtiden återstarta måste den skapa en **ny**
   kontrollerad session med känt energibehov, bekräftad
   laddningsavsikt och tillåtet pris-/fasfönster.
3. **Volvo Cars-appen (användarverifierat 2026-11-04):**
   Den aktuella XC40-appen har ingen stoppknapp för hemmaladdning.
   Smart Charging och manuellt schema är avstängda och hanteras
   inte som separata styrfunktioner i denna etapp.
   Den äldre tanken om förlustfri *Volvo-appstoppsignal*
   är inte längre ett produktkrav. Den generiska
   `verified_manual_intent_audit`-grinden är nu ersatt
   **i källkoden**, inte driftsatt priscontroller, med en
   beständig verifierad FC10-prispausägare och kabelsessions-
   identitet. Ingen `lossless=true`-händelse fabriceras.

30 sekunders polling kan missa en mycket kort ur-/inkoppling,
därför används **aldrig** `lossless=true`. Volvos egen
[XC40-laddningsmanual](https://www.volvocars.com/se/support/car/xc40-recharge-pure-electric/21w22/article/47d2c97fd33effd3c0a8cc3718c999b7-a27aa276ce5ec813c0a8b04a2b2eb5e5-8664b2fa77a7e089c0a8296870d1a409/47d2c97fd33effd3c0a8cc3718c999b7-455a36edef2fc9f4c0a8b08a64080ac4-8664b2fa77a7e089c0a8296870d1a409/9270e2c3a351b0f1c0a8b04a16c5119e-0bd61e02a350d231c0a8b04a7cae8b2b-8664b2fa77a7e089c0a8296870d1a409/)
beskriver dessutom **ur-/återkoppling inom tre sekunder** för
att åsidosätta ett aktivt laddningsschema. En sådan övergång
kan inte garanterat observeras med vår 30-sekunderspollning.
Säkrare framtida implementation kan behöva fysisk händelsekälla
eller ett explicit ny-session-samtycke för att hantera
kabelstyrd återanslutning och fysisk/manuell EVSE-paus.
Ett separat Volvo-appstopp ingår inte i aktuell omfattning.

## Verifiering och driftsättning

Offline: 1 522 Python-tester passerade efter införandet av
kabelspårning (inklusive tracker, observerintegration,
persistens, fel, utebliven observation, ägarskap och GO/NO-GO).
Skrivskyddad replay från befintlig EVCC-historik gav
`2026-11-04T09:51:47Z: reconnected`, `connection_epoch=1`,
`B2`, `raw=1000`, `pilot=267`. Ingen FC10 skrevs.

Installera endast den matchande observerkällan +
`evcc_cable_tracker.py` i befintlig EVCC-observatör och sätt
`EVCC_CABLE_TRACK_PATH=/opt/solinteg/ev-shadow-runtime/var/evcc-cable-state.json`
via en separat ägd systemd-drop-in. Verifiera två distinkta
FC03-prover, filens `0600`, `device_writes=0`, och att
`solinteg-ev-observer.service` fortfarande är aktiv. Bevara
byteexakt original och manifest för rollback. Ta inte bort
någon annan observationstjänst, dispatcher, broker eller watchdog.

**Rollback:** stoppa endast EVCC-observatören, återställ den
byteexakta tidigare `evcc_observer.py`, ta bort den specifikt
ägda drop-in-filen och eventuellt den nyinstallerade
tracker-modulen **enbart efter verifiering av ägarskap och hash**.
`systemctl daemon-reload`, starta samma observatör igen och
bekräfta FC03-status. Bevara spårbarhet och den senaste giltiga
rollbackkopian. Ingen priscontroller-enhet installeras.

## Faktisk Dell-release och rollbackbevis 2026-11-04

**Driftsatt, enbart läsning:** Kl. 20:37 CET ersattes den
byteexakt verifierade föregående `evcc_observer.py` efter att
originalets SHA-256
`03af4734c26449091a4874524588554e1c89b5eb154dea5d10908f44361d906f`
kontrollerats. Ny `evcc_observer.py` SHA-256:
`7c4e7916667fd03873be3de7d866aef2e308330edf6109e60663371e8f2beeb6`.
Ny `evcc_cable_tracker.py` SHA-256:
`9d19151378691babd5e682d91d9353ca0721fafd1f46ca0fa754bac58d38690a`.
Båda ligger som `root:solinteg`, `0440`, under den
**redan befintliga** EVCC-observatörens skriptkatalog.

Endast denna nya drop-in installerades:
`/etc/systemd/system/solinteg-ev-observer.service.d/95-cable-track.conf`,
för `EVCC_CABLE_TRACK_PATH`. Efter `daemon-reload` startades
**enbart** `solinteg-ev-observer.service` om. Verifierat
`ActiveState=active`, `SubState=running`, `NRestarts=0`,
och befintlig drop-in läses. Den första observationen var
`unknown` med `candidate=connected` efter ett `C2`-prov,
vilket är väntad tvåprovsgrind. Följande färska prov gav
`status=connected`, `observed_state=C2`, `connection_epoch=0`,
`confirmed_replug=false` (inget tidigare urkopplingsbevis i
**den nya** trackerfilen). Privat statusfil `0600`,
`device_writes=0`, ägd av observerprocessens konto.
Ingen aktiv EVCC-priscontroller-service/timer finns.

Byteexakt återställningsunderlag och SHA-manifest:
`/var/lib/solinteg-evshadow-rollback/evcc-cable-tracker-20261104/`
(root `0700`). Rollback gäller endast de två av releasen
ägda skripten och den specifika `95-cable-track.conf`-
drop-in-filen. Bevara trackerstatus och backup som driftbevis
om ingen separat godkänd arkiv-/städrutin har körts.
Inget annat i Modbus-broker, dispatcher, poller, inverter
eller ordinarie watchdog ändrades.

**GO/NO-GO:** Kabelstatus är GO för observerande drift. Automatisk
prispaus och återstart från `PWM=1000` är fortfarande **NO-GO**
eftersom oberoende kalibrerad kWh-session, fysiskt kvalificerad
prispaus-/kabelsessionsmodell och verkliga paus-/startprov
fortfarande saknas. Källkodsmodellen är nu offline-testad,
men inte installerad som priscontroller. **Ingen särskild Volvo-appstoppdetektor
krävs för denna installation**, eftersom appen saknar stoppknapp.

## Efterföljande read-only uppgradering: observationskontinuitet

Den befintliga `solinteg-ev-observer.service` kör nu en ny version av
enbart `evcc_cable_tracker.py` (SHA-256
`761782941d607c59fb3f922aebf58f3093d34d50b9ecc15fe1656d73b9111e09`).
En äldre trackerfil utan de nya fälten får inte ge ny FC10-
behörighet förrän observatören skrivit nytt prov.
`connection_epoch` ökar vid verifierad ur-/inkoppling;
`coverage_epoch` ökar vid avbrott över 100 s eller FC03-
fel/okänt tillstånd; `tracking_id` är beständig inom en
trackerfil men ändras vid filförlust och ny initiering.
En ny `tracking_id` förhindrar att äldre pausposter blir
giltiga när kabel- och täckningsepok båda återställts till 0.

Live efter uppgraderingen: `connected / C2`, epoch `0/0`,
giltig unik trackeridentitet, provålder 8 s, `0600`,
`device_writes=0`. EVCC-observatören, dispatchern, pollern
och Volvo-status-timern aktiva; inga omstarter efter den
planerade omstarten av observatören (`NRestarts=0`).
Den separata automatiska priscontrollern är fortsatt
`LoadState=not-found`.

Byteexakt rollback och SHA-manifest finns i två rootägda
`0700`-kataloger:
`/var/lib/solinteg-evshadow-rollback/evcc-price-owner-continuity-20261104/`
och
`/var/lib/solinteg-evshadow-rollback/evcc-price-owner-tracking-id-20261104/`.
Återställ endast denna avgränsade moduls föregående 0440-kopia
efter checksummeverifiering och starta om befintlig EVCC-
observatör. Äldre trackerprotokoll får då inte användas som
underlag för automatisk återstart. Ingen annan observer,
dispatcher, broker, timer eller watchdog tillkom eller togs bort.
