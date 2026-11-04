# Plan: Modbus-broker med ensam ägare och säker migrering

**Aktuell drift 2026-11-04:** Kontrollbrokern är **driftsatt på Dell**
sedan kl. **05:45 CET**, med en gemensam TCP:502-session för poller och
dispatcher. Första telemetri- och kontrollcyklerna är verifierade.
**Gamla watchdoggen är oförändrad**; dess brister under brokerhaveri
är dokumenterade som accepterade begränsningar, inte lösta fel.
[Driftrapport, rollback och riskregister](../modbus-broker-control-stage-2026-11-04.md)
samt [ADR](../architecture/adr-2026-11-04-broker-legacy-failure-scope.md).
Nästa steg är **uppföljning av normaldrift**, inte en ny fallback.

**Nästa transaktionsversion 2026-11-04 (endast källkod/test):** [Hela normala FC03/FC06-styrsekvensen flyttad till broker-worker](../modbus-broker-transactions-release-2026-11-04.md) med befintlig `inverter_control`-logik; `broker_transaction` och serverflaggan `SOLINTEG_BROKER_TRANSACTIONS_ENABLED` är **inte aktiverade på Dell**. Den [nya arkitektur-ADR:n](../architecture/adr-2026-11-04-broker-complete-transactions.md) behåller dagens watchdog/felåterställning som tidigare. Produktionen fortsätter `SOLINTEG_MODBUS_SOURCE=broker`; separat fysiskt release-GO återstår.

**Historisk status till och med 2026-11-03:** Etapp **0–2 klar offline**, etapp **3 kod och systemd-templates förberedda/testade**, inklusive pollerns replaykontroll och **opt-in SoC-/stale-grind i dispatcher (2026-11-03, endast offline)**. **Observationswatchdog driftsatt separat på Dell 2026-11-03, utan fysisk behörighet. SoC-/BMS-gated privilegierad fallback testad som syntetisk offlinekedja (ingen aktivering; native 10 % ligger under önskad 20 %-reserv).** Fysisk broker-cutover fortfarande **NO-GO**; etapp 4–6 planerade. **Ingen broker installerad och ingen fysisk styrning ändrad.** Se [offlinerapport](../modbus-broker-offline-2026-11-01.md), [stale-SoC ADR](../architecture/adr-2026-11-02-stale-telemetry-soc.md), [Dell-observatör](../modbus-broker-watchdog-dell-2026-11-03.md) och [etapp 3-grindar](../modbus-broker-readonly-stage3-2026-11-02.md).
**Gemensam normalbroker 2026-11-04:** [FC03/FC06 via en serialiserad worker, kompatibilitetsadapter till dagens poller/dispatcher, offlineintegrations- och rollbackplan](../modbus-broker-control-stage-2026-11-04.md). **Källa och testsvit verifierade; driftsatt 2026-11-04 kl. 05:45 CET.** [ADR](../architecture/adr-2026-11-04-broker-legacy-failure-scope.md): dagens watchdog/failure-försök behålls oförändrade; split-brain/okänd EMS-state dokumenteras som accepterade kända brister. Staged read-only-broker är inte längre tänkt att startas parallellt med kontrollbrokern. **Fysisk normaldriftscutover är genomförd och verifierad; ny säkerhetsfallback ingår inte.**
**Skrivskyddad inventering 2026-11-03:** [aktuell drift, senaste verifierade `52502/52503`, nattens två misslyckade revert, ej identifierad firmware och verifieringsmatris](../solinteg-fallback-readonly-inventory-2026-11-03.md). Inget nyintrång i TCP:502 och ingen aktivering; fysisk fallback fortsatt **NO-GO**.
**Nätverksuppföljning 2026-11-03:** [ASUS-schemalagd daglig 03:00-omstart](../solinteg-network-incident-2026-11-03.md) identifierades som rotorsak till nattligt Wi-Fi-bortfall under sju dagar; reboot-schemat avaktiverades i router-NVRAM med verifierad readback utan att starta om nätet. Första nattens utfall 4 november är separat verifierat nedan. WAN-/Modbusproblemet vid 03:13 den 3 november behöver fortsatt separat verifiering; ingen broker-cutover.
**Första nattverifiering 2026-11-04:** [ASUS omstart uteblev kl. 03:00 och nattens Modbuskorssvar dokumenterade](../solinteg-modbus-overnight-2026-11-04.md). 99 giltiga 30-samples under 02:45–03:35 CET, men två separata 60-s luckor under natten med `FC06 WriteSingleRegisterResponse` för `50207` i pollerns FC03-svar, direkt korrelerat med dispatcherns börvärden. Flerklientsrisken är nu konkret på Sundsvall: **prioritera broker/ensam TCP-ägare men ingen skarp cutover utan separat Go/No-Go**.
**Tillverkare:** [modell-/firmwarespecifika frågor förberedda, inte skickade](../solinteg-vendor-ems-questions-2026-11-03.md).
**Repository/branch:** `ermhawi/solinteg-controller-oss` / `Sundsvall` – får aldrig mergas till `main`/upstream.
**Omfattning:** Solintegs Modbus-TCP-dongel. **EVCC/Waveshare RS-485 är ett separat system.**
**Historiskt beslut före 4 november:** Planen godkände ursprungligen endast analys och dokumentation. **Senare beslut 2026-11-04:** användaren prioriterade gemensam normalbroker och valde att behålla befintligt failure-/watchdogbeteende; det genomfördes som en återställbar cutover. Inget ändrat SoC-golv, inga nya SoC25-prov eller ny nödfallback.

## 1. Verifierade nulägesproblem

- `scripts/services/modbus_poller.py` har en persistent `ModbusTcpClient` med **10 s timeout**. Varje 30 s hämtas CT/AC/PV (10994–11029, annars 11000–11029), sedan batterieffekt (30258), SoC/temperatur (33000), driftläge (50000) och valfritt 50006. `timestamp` skapas **efter** läsningarna. De är inte en atomär ögonblicksbild.
- `scripts/services/dispatch_loop.py` använder `inverter_control.Inverter` för separata korta anslutningar och femstegs-kontrollsekvenser. Kontrollklientens timeout är **5 s**, skrivningarna begränsas till minst **2 s** mellan FC06. `scripts/services/watchdog.py` öppnar en **egen** anslutning för `return_to_auto` när dispatcherns heartbeat är äldre än **240 s** (systemd-timer var **2 min**).
- `MODBUS.md` beskriver att dongeln kan neka samtidiga anslutningar och att leverantörens molnkommunikation ibland blockerar lokal Modbus **4–8 s**. Flera direktklienter är därför ett känt riskområde, även när ett enskilt kortlivat skrivförsök har fungerat.
- Vid tredje SoC25-provet visade en pollermätning batteri **−1692 W**, inverter-AC **+978 W**, PV **0 W**, nät **−11 W** och huslast **989 W**. CT/AC/hus var sinsemellan konsekventa, men batteri-/AC-värdena motsade varandra med 2670 W. Watchdoggen avbröt korrekt. **En lägesväxling mellan registerläsningar är en hypotes, inte bevisad rotorsak.**
- De nuvarande skydden måste bevaras: `SOLINTEG_CONTROL_ARMED`, verifierad 50207-polaritet, SoC-/fasgränser, korrekt ordning på mode/caps/target, readback och en separat process med återställning till General/Auto.

## 2. Föreslagen arkitektur

```text
poller (read) ────────────────┐
dispatcher (policy/control) ──┤    Unix-domain socket, begränsade API-roller
watchdog (safety_auto) ───────┼─────────────────┐
underhållsverktyg (särskild roll)┘                 │
                                     ┌───────────▼─────────┐
                                     │ solinteg-modbus-broker│
                                     │ 1 ägare, 1 TCP-klient│
                                     │ serialiserade writes│
                                     │ snapshots + kvalitet│
                                     └───────────┬─────────┘
                                       Solinteg TCP:502
```

Brokerprocessen äger enda **ordinarie** TCP-sessionen. IPC över lokal Unix-domain socket; ingen HTTP-tjänst eller port exponerad. Poller och dispatcher anropar semantiska operationer, inte fria registerskrivningar. Ägarskap måste också **tekniskt upprätthållas** efter migrering, annars ger ytterligare en process ingen garanti.

**Föreslaget lokalt kontrakt:**

| API | Syfte och krav |
| --- | --- |
| `read_snapshot()` | Read-only FC03, per-blocktid och kvalitet, ingen fysisk aktivering |
| `status()/health()` | Connection state, queue depth, mode, fel och senast giltigt sample |
| `charge(w, request_id, lease)`, `discharge(...)`, `reserve_hold(...)` | En hel serialiserad, verifierad kontrolltransaktion med bibehållen säkerhetspolicy |
| `auto(reason)` | Säker ordinarie återställning |
| `safety_auto(reason)` | Oberoende watchdogs **högsta prioritet**, töm/fence:a äldre styrkommandon |

**Transaktioner:** Inga FC06 eller delvisa femstegssekvenser får exponeras till klienterna. Broker äger rate limit, mode-/cap-ordning, readback och den interna kontrollgenerationen. `request_id`/generation/lease gör en äldre köad laddning ogiltig efter `safety_auto`. En hårdvarusekvens är **inte atomär**: lägg till säkra avbrottspunkter och fail-closed rollback om den bara hinner halvvägs. Ingen godtycklig FC06 för normal klient.

**Behörighet:** UDS-filsystemrättigheter och/eller `SO_PEERCRED`; poller/read, dispatcher/control och watchdog/safety måste skiljas åt. De kör i dag med samma `User=solinteg`, så separata roller kräver ändrade systemd-identiteter eller separat skyddat safety-gränssnitt. Skydda även mot manuella/probe-klienter som fortfarande ansluter direkt till TCP:502.

**Snapshotkohärens:** Läs mode **före och efter** andra registerblock; samla `sample_id`, monotona block-`start/end`, processens skrivgeneration och total samplingsduration. Kontrollera fasernas summa mot nätmätaren, CT/house/AC och rimlig batterieffekt/PV. Markera `valid`, `mode_changed`, `stale`, `ct_invalid`, `energy_balance_invalid` och `timeout`; kassera uppenbart inkonsekventa samplar. Serien av registerläsningar blir **inte** fysisk atomär ens med en broker eftersom invertern ändrar sig autonomt. Använd observationer för att kalibrera toleranser, inte en enda misslyckad sample.

**Kompatibilitet:** `live.json` och SQLite-`readings` förblir kompatibla. Undvik att skriva en **färsk timestamp på ogiltiga mätvärden**; bevara senaste giltiga sample med dess ursprungliga tid och spara rejects separat. Granska dispatcherns fallback vid stale live-data: ingen ny tvingad effekt ska beslutas utifrån en ogiltig/äldre snapshot. Tydliga kvalitetsfält får adderas utan att existerande fält byter innebörd.

**Oberoende watchdog:** Den ska fortfarande vara en separat process och inte förlita sig på att dispatchern lever. Vid broker-haveri räcker inte ett IPC-kommando som aldrig når fram; säker reservväg måste först **fence:a/stoppa brokern, verifiera att den släppt TCP**, sedan ensam försöka återställa General/Auto med readback och larm. Ingen samtidig broker-/fallback-skrivare. native BMS-skydd och befintliga hårdvarugränser ändras inte.

## 3. Etapper, leverabler och go/no-go

| Etapp | Implementation | Klart när |
| --- | --- | --- |
| **0 – Baslinje** ✅ offline | Inventera alla direkta FC03/FC06-konsumenter (inkl. `inverter_control` CLI och fysiska prober), kör read-only mätning av pollcykler, svarstider, lägesbyten och observerade fel. Fastställ installerade service-/kodhachar och rollbackväg. | Inventeringsmatris och uppmätta basvärden, inga writes |
| **1 – Broker offline** ✅ prototyp | Ny separat broker + smal IPC-client, serialiseringskö och fake Modbus-TCP-dongel. Hela kontrollsekvenser testas enbart mot fake; `ARMED=0` är default. | Testad kod, inga nya driftprocesser |
| **2 – Telemetrikvalitet offline** ✅ fake-tester | Reproducera alla tre SoC25-felbilder och lägesbyte mitt i FC03-serie. Testa kvalitet, samplingslatens, 4–8 s cloud-stall, backoff och separata reject-loggar. | Osäkra samplar avvisas, ingen CT-/temperatur-/fassäkerhet försvagas |
| **3 – Läs-only driftsättning** ⚠️ adapter klar, ej aktiverad | Separat godkänd cutover: stäng gamla pollerns TCP-session **innan** en read-only broker tar dess plats. Pollern får data via UDS; ingen extra poller parallellt. Dispatcher/watchdog har ännu sin tidigare kortlivade reservväg, så full exklusivitet påstås inte här. | Samma `live.json`/SQLite-resultat, acceptabel latens/bortfall och verifierad enkel rollback |
| **4 – Styrväg** ✅ normaldrift 2026-11-04; **nödfallback** uppskjuten | Dispatcher använder nu begränsad broker-FC03/FC06 med ursprunglig styrlogik; watchdog och dess direkta felåtergång lämnas oförändrade och dokumenterade som brist. | En broker-TCP-ägare, färska mätningar, normal styrning verifierad; nya säkerhetsfallbackgarantier ingår inte |
| **5 – Tvinga exklusivitet** ◐ normalväg klar | Pollerns systemd-tjänst förbjuder AF_INET, dispatchern skickar FC03/FC06 via UDS och brokern äger ensam normal-TCP. Watchdog och externa klienter kan ännu ansluta direkt vid fel (accepterad brist). | Normal anslutningsägare verifierad, men absolut nätverksfencing och säker reservväg uppskjutna |
| **6 – Uppföljning** pågår | Jämför normaldriftens mätintervall, protokollfel, latens, aktiv broker och dispatchutfall mot baslinjen. Felvägens utveckling skjuts upp enligt ADR. | Flera normala driftcykler utan FC03/FC06-svarsförväxling; senare separat felanalys |

**Etapp 0–2 levererad och etapp 3 förberedd:** offlinekärna med fake-dongel, verifierad UDS, kvalitet och felinjektionstester; dessutom en riktig allowlistad **FC03-only** pymodbus-adapter, skyddad read-only service-entrypoint, opt-in brokerläge för pollern, opt-in SoC-/stale-grind i dispatchern med **armed NO-GO** och **ej installerade** systemd-templates. En **lokal** loopback fake-Modbus-TCP-server har nu dessutom verifierat
riktig `pymodbus 3.15.0` och 12 FC03 utan writes; ingen fysisk
dongelkommunikation eller driftsättning. Baslinjen kommer från historiskt SQLite-material; tidsupplösning per FC03-block och fysisk rollback återstår. Se [offlinegranskning](../modbus-broker-offline-2026-11-01.md) och [NO-GO-grindar för live cutover](../modbus-broker-readonly-stage3-2026-11-02.md).

## 4. Obligatoriska fel- och acceptanstester

1. En dongel som bara accepterar **en** klient, nekar samtidiga sockets och låser Modbus i **4–8 s**: broker återansluter, köordning/timeout tydlig och inga dubblettkommandon.
2. Fem FC03-block med `0x101→0x303` mitt i sekvensen: snapshot **ogiltig**. Exakta −1692/+978/0/−11/989 W från tredje incidenten får inte bli ny användbar fysisk mätning. Ett koherent kontrollprov ska accepteras.
3. Avbruten FC06 **efter varje registersteg** (50000, 50207, 50208, 50209, 50210): ingen falsk ”lyckad laddning”, Auto/revert best effort, larm och dokumenterad reststatus om readback inte kan verifieras.
4. Parallell charge/discharge, köad äldre generation och watchdog-`safety_auto`: inget gammalt laddkommando får utföras **efter** säkerhetsrevert.
5. Hängd eller död broker samt död dispatcher: watchdog förblir självständig, fence:ar sessionen före direkt fallback, larmar när Auto inte kan verifieras; ingen konkurrerande Modbus-ägare.
6. `ARMED=0`, ogiltiga request-id, felaktiga UDS-behörigheter och obehörig rå FC06: **noll fysiska writes**.
7. Både Python- och relevanta frontendtester fortsatt gröna; `live.json`-/SQLite-kontrakt och skyddsvakter oförändrade. Testa ren återställning till nuvarande systemd-konfiguration och befintlig kod utan att båda vägar kan köra samtidigt.

## 5. Operativ förkontroll, rollback och DoD

- **Källa ≠ drift:** commit/push av brokerkoden aktiverar inte enheten; varje live-etapp behöver egen serviceinstallationsplan och uttryckligt godkännande.
- **Före cutover:** verifiera fysisk status/SoC, BMS-/fas-/EV-skydd, aktuell drift och en byteverifierad känd rollback. Spara den gamla servicekonfigurationen och exakta kontrollregistrens mode/target/cap-readback.
- **Vid avbrott:** stoppa och fence:a den ena ägaren innan den andra får startas. Återställ säkert Auto och sedan originaltjänster i beprövad ordning; verifiera fysisk effekt, ordinarie poller och aktiv watchdog. Om återställning inte kan bekräftas: **stoppa ytterligare styrning och larma**.
- **Mät och larma:** `request_id`, kö-/FC03-latenser, modeepoch, snapshot-spann/kvalitet, reconnects, command readback, fysisk effekt, time-to-safe-Auto och överhoppade kommandon – utan hemligheter, privata IP-adresser eller privata förbrukningsvärden i Git.
- **DoD:** obligatorisk [dokumentationspreflight](https://github.com/ermhawi/engineering-standards/blob/v1.3.0/docs/documentation-hygiene.md) före implementation, därefter verifierad kod/test/dokumentation/ADR, uppdaterad planstatus och agentägd temporär systemd-/script-/loggstädning enligt [v1.3.0](https://github.com/ermhawi/engineering-standards/blob/v1.3.0/docs/artifact-hygiene.md). Endast `origin/Sundsvall`, inga opushade commits, omergade grenar eller extra worktrees.

### Beslut att låsa före drift

1. Vilka UDS-roller/Unix-identiteter används för read/control/safety? `solinteg` är i dag delad; offline-UDS ger endast read som standard och kan inte ge starka producerade roller förrän separata UID införts.
2. Hur garanteras **fenced nödfallback** om broker tappar respons mitt under en fysisk multiwrite? Finns en verifierad säker exklusiv stoppsekvens?
3. Vilken verifierad kvalitets-/tidsgräns ska gälla för datakohärens, och hur ska dispatcherns stale-data-fallback förbjudas från att starta farlig styrning?
4. Är kontroll-latens tillräcklig trots 4–8s cloud-stall och minst 2s skrivintervall? Mät på fake, sedan read-only innan något skarpt.

Se [föreslagen ADR](../architecture/adr-2026-11-01-single-owner-modbus-broker.md), [MODBUS.md](https://github.com/ermhawi/solinteg-controller-oss/blob/Sundsvall/MODBUS.md) och [senaste avbrutna prov](../soc25-recovery-third-2026-11-01.md).
