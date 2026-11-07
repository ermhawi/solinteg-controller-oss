# ADR 2026-11-02: EMS, General, skydd och Dell-bortfall

**Status:** Historisk riskbedömning från 2026-11-02. **Uppdatering 2026-11-05:**
En selektiv, användargodkänd General/EMS-solhandoff har nu aktiverats
med central hysteresisreglering, utan oberoende ny fallbackfunktion.
Se [live-release-ADR](adr-2026-11-05-energy-priority-arbiter.md).
Tillverkarens EMS-timeout och fysisk native EndSOC-prioritet är fortsatt
**overifierade** och tidigare riskbedömning gäller oförändrat för dem.
**Omfattning:** Sundsvalls Solinteg MHT-10K-25, Sunwoda (3 × 5 kWh) och styrning på Dell. Baslinje 2026-11-02; ingen garanti för andra enheter eller firmware.

## Beslut i korthet

- **General (0x0101)** använder inverterens egen självförbrukningsreglering. **EMS BattCtrl (0x0303)** tar extern batterieffekt från Dell/Modbus och respekterar fortfarande inverter- och BMS-skydd, i den utsträckning dessa verkligen verkställs.
- **Anta inte att Solinteg återtar kontrollen automatiskt om Dell försvinner.** Ingen dokumenterad inställningsbar TTL/lease för `50000=0x303` och `50207` har verifierats för vår modell/firmware. På projektets **referensinstallation (annan MHT-modell/firmware)** har ett börvärde observerats ligga kvar minst **16 minuter** utan uppdateringar; detta är inte ett lokalt bortfallsprov på Sundsvall.
- **20 %** är en ekonomisk mjukvarureserv, **12 %** vår operativa mjukvarugräns och **10 %** inverterens lästa och aktiverade OnGrid EndSOC. Fysisk skyddsverkan av 10 % med kvarhängande EMS-börvärde är **inte bevisad**.
- Dell-baserad watchdog är inte oberoende av Dell-strömförsörjning. General som blind fallback kan fortsätta urladda mot ett lägre SoC-golv. Nya fysiska övergångar kräver skriftlig tillverkarspecifikation, oberoende supervision och verifierad readback.
- Den solmedvetna modellen och nya Modbus-brokern är **endast read-only/offline**; ingen produktionsaktivering godkänns här.

### Uppföljning 2026-11-03: säkerhetsgrind endast offline

Den framtida privilegierade broker-fallbackens samordnare kräver nu
syntetiskt testad, **oberoende post-fence** SoC-/BMS-verifiering,
bekräftat exklusivt TCP-ägarskap och en native EndSOC-gräns **minst
lika hög** som den begärda mjukvarureserven. Med dokumenterade
10 % native kontra 20 % ekonomisk reserv är automatisk General
fortsatt **NO-GO**. Detta är inte en implementerad fysisk nödfallback:
ett tidigare EMS-börvärde kan kvarstå, verifierad neutralisering
och oberoende datakälla saknas. Se
[offlinegrinden](../modbus-broker-emergency-policy-offline-2026-11-03.md).

## 1. General kontra EMS

| `50000` | Namn | Styrprincip / status |
| --- | --- | --- |
| `0x0101` = 257 | General/self-use | Solinteg reglerar batteriet efter PV och huslast: hus först, laddning från överskott, sedan export; vid underskott batteri till hus tills skydd/gräns. |
| `0x0301` | EMS AC Control | Extern AC-effektstyrning, används inte i denna installation. |
| `0x0302` | EMS General | Separat EMS-läge, **inte verifierat** som likvärdigt med eller säkrare fallback än vanliga General. |
| `0x0303` = 771 | EMS Battery Control | Extern batterieffekt via `50207`. Solinteg sköter fortfarande konvertering, MPPT, nätdrift och kommunikation med BMS. |
| `0x0400` | ToU | Förlagrat tidsschema i inverter, stöd beroende på modell/firmware. **Ingen automatisk EMS-fallback** bara för att ToU finns. |

Register `50207` är ett **batteri**-börvärde, inte total AC-effekt. Ett råsteg motsvarar **10 W**: `-200` = begär laddning 2 000 W, `+200` = begär urladdning 2 000 W och `0` = begär noll batterieffekt. Ett EMS-nollbörvärde är **inte General** och kan hindra batteriet från att använda solöverskott. `50210` anger PV-/batteriprioritet; `50208/50209` styr inverterens AC-utbyte och är **inte** ett direkt tak för husets totala import per fas. Se [MODBUS.md](https://github.com/ermhawi/solinteg-controller-oss/blob/Sundsvall/MODBUS.md).

Förenklat med 3 000 W sol och 1 000 W huslast:

| Driftläge | Typiskt batteribeteende | Konsekvens |
| --- | --- | --- |
| General | Cirka 2 000 W laddning | Överskott lagras innan export. |
| EMS `50207=0` | Försöker hålla 0 W | Solöverskott kan exporteras i stället för att lagras. |
| EMS `50207=+5` | Begär 50 W urladdning | Batteriet kan fortfarande i praktiken urladda mer på grund av regler- och mätavvikelser. |
| EMS `50207=-100` | Begär 1 000 W laddning | Ytterligare solel kan exporteras. |

Värden är pedagogiska, inte fysikaliska garantier (BMS-gränser, förluster, prioritering och aktuell topologi påverkar).

## 2. Faktiskt verifierad installation (2 november 2026)

### Konfiguration från aktiv `solinteg-dispatch.service`

| Parameter / skydd | Värde | Evidens och begränsning |
| --- | --- | --- |
| `SOLINTEG_CONTROL_ARMED` | `1` | Dispatchern är armerad för registerskrivning. |
| `SOLINTEG_BATTERY_KWH` | 15,055 kWh | Modellens beräkningskapacitet. |
| `SOLINTEG_BATTERY_MAX_W` | **7 500 W** | Statisk **mjukvarugräns** för begärd batterieffekt. |
| `SOLINTEG_GRID_CAP_W` | 10 000 W | Ytterligare soft-cap i dispatcher. |
| `DISPATCH_IMPORT_LIMIT_W` | **10 500 W** | Site-importgräns skickad vid start via `50007/50009`. Varken certifierad 20 A/fas-gräns eller instantant L3-skydd. |
| `SOLINTEG_SOC_FLOOR_PCT` | **12 %** | Mjukvarans nedre arbetsgräns för normal urladdning; inte inverterns native lägsta. |
| `SOLINTEG_PLANNER_RESERVE_PCT` | **20 %** | Prisoptimerarens ekonomiska reserv. |
| Recovery | **under 19,5 % → minst 21 %** | Hysteres i mjukvara, normal recovery-effekt 300 W om inte annat anges. |
| `DISPATCH_L3_CHARGE_GUARD_MODE` | `1` | Aktiv laddningsbudget för husbatteri på L3; inte ett certifierat säkringsskydd. |
| `DISPATCH_L3_BATTERY_MODE` | `shadow` | Beräknar rekommendationer utan fysiska ändringar. |
| `DISPATCH_RESERVE_HOLD_VERIFIED` | ej satt (default av) | Ingen lokal verifiering/aktivering av separat 0 W soft hold. |

**Drift kontra utvecklingskod:** Installerade `/opt/solinteg/app/scripts/services/dispatch_loop.py` och Git-branchens `scripts/services/dispatch_loop.py` hade olika checksumma vid läsning 2 november. Nya branchfunktioner **får inte antas vara driftsatta**.

### Direkta, skrivskyddade FC03-registerläsningar

| Register | Uppmätt | Slutsats |
| --- | --- | --- |
| `52502` | `1` | **On-grid SoC protection ON.** |
| `52503` | `100` (0,1 %-steg) | **OnGrid EndSOC = 10,0 %.** |
| `33021` | `144` (0,1 A-steg) | Dynamisk BMS-**laddströmsgräns 14,4 A** vid mättillfället. |
| `33023` | `186` (0,1 A-steg) | Dynamisk BMS-**urladdningsgräns 18,6 A** vid mättillfället. |

Vid ungefär 400 V innebär 14,4 A cirka 5,8 kW. Det är lägre än vårt statiska börvärdestak 7,5 kW. Inverter och BMS måste reglera den **faktiska** effekten, men readback av deras strömgränser är inte bevis att det sker korrekt i alla EMS-fellägen. Exakt installerad BMS-modell/firmware återstår att identifiera.

Skrivskyddad `telemetry.db`-kontroll över cirka två dygn till 2026-11-02 kl. 17:32 UTC: **5 714 prover**, SoC **18–42 %**, batteri-W från **−7 429 (laddning)** till **+7 182 (urladdning)**. Historiska observationer, **inte** certifierade gränser. Dispatcher, poller och watchdogtimer var aktiva vid slutkontrollen; inverterläge observerades både som General och EMS BattCtrl under dagen.

### Skyddslager – de är inte utbytbara

1. **BMS och inverter:** över-/underspänning, överström, övertemperatur, kortslutnings-/isolations- och nätfunktioner; installationens fysiska säkringar (husets huvudsäkring 20 A, billaddarens krets 16 A). Dessa skydd kan lösa ut eller blockera drift, men garanterar inte skadefritt förlopp i varje felkombination.
2. **Native inverter-SoC:** faktiskt läst `52502=1` och `52503=10 %`. **Det är öppet** om detta begränsar ett kvarhängande `50207`-börvärde i EMS vid Dell-bortfall.
3. **Dispatcherns mjukvaruskydd:** 7,5 kW, 12 % mjukvarugolv, ekonomiska 20 %, recovery 19,5–21 %, 10,5 kW site-import och L3-guard. De upphör att aktivt reagera om Dell/processen stannar.
4. **Dell-baserad watchdog:** separat process och timer, men **samma dator och Modbus-transport**; skyddar mot tappad heartbeat, inte mot alla felaktiga beslut.
5. **Fysisk feedback:** läsning av inställda register räcker inte alltid. På en referensinstallation har en AC-gräns lästs tillbaka rätt men **inte verkställts** p.g.a. registerskrivordningen. Använd uppmätt batteri-W, faser, SoC och BMS-larm som oberoende observationsdata.

Sunwodas [SunESS H 15 kWh](https://www.sunwodaenergy.com/residential/residential-ess-suness-h-series.html) anges med **7,5 kW nominell** ladd-/urladdning och runt **400 V**; effekt beror på SoC och temperatur. Det är en **familjereferens**, inte fysisk identifikation av vår BMS. Uppgiften från samtalet att batteriet skulle låsa efter exakt *tre* överströmsutlösningar kunde **inte beläggas för vår Sunwoda-variant** och får inte behandlas som verifierad skyddslogik. Skyddsstopp, larm, behov av omstart och förtida slitage är ändå reella riskklasser.

## 3. Bortfall – vad som händer och vad som inte är säkrat

| Situation | Befintlig mekanism | Begränsning |
| --- | --- | --- |
| Normal `SIGTERM`/avslut | Dispatcher försöker `return_to_auto`. | Best effort; kommunikationsfel kan förhindra skrivningen. |
| Process kraschar, Dell fungerar | `systemd Restart=always`, `RestartSec=15`; dispatcher försöker korrigera på start. | Omstart och verklig readback saknar hård realtidsdeadline. |
| Dispatchern hänger/dör, Dell fungerar | `solinteg-watchdog.timer` varannan minut. Om heartbeat är **>240 s** gammal försöker den sätta `50000=0x101`; akut larm vid misslyckande. | Normalt **cirka 4–6 minuter** från senaste heartbeat till upptäckt, ingen garanterad skyddsreaktionstid. |
| Heartbeat saknas/är oläsbar | `heartbeat_age_s()` returnerar `None` och watchdog utför **ingen** återställning på den signalen. | Healthcheck måste täcka annan status; ingen generell fail-closed. |
| Dispatcher kör men skickar fel börvärde | Heartbeat fortsätter vara färsk. | Watchdog upptäcker inte felbeslut utifrån heartbeat. |
| Hela Dell stängs av eller nätförbindelse bryts | Varken systemd, dispatcher eller watchdog på Dell kan skriva till inverter. | **Kvarhängande EMS-börvärde möjligt**; ingen verifierad native timeout/fallback. |
| `return_to_auto` från låg SoC | Solinteg börjar självförbruka i General. | Kan sänka SoC under **ekonomiska 20 %**, mot native lägre gränser. |
| Modbus-fel | Återställningsförsök använder också Modbus. | Fysisk slutstatus kan vara okänd om läsning/skrivning inte går. |

**Börvärdespersistens:** Ett tidigare verifierat test på **projektets referensinstallation, inte Sundsvalls fysiska inverter**, såg `50207=-150` (begärd 1 500 W laddning) ligga kvar under **16 minuter**, 63 prover, utan automatisk mode-/effektåtergång. Det bevisar inte oändlig varaktighet, men ingen snabb automatisk timeout kan antas. Modbus-TCP-klientens kommunikationstimeout är **inte** en giltighetstid för batteribörvärdet.

**Kommande skyddsmodell:** att bara inhibera *nya* kommandon stoppar inte nödvändigtvis en redan aktiv EMS-urladdning. Att alltid skicka General kan samtidigt innebära fortsatt urladdning under reservnivån. En oberoende supervisor måste hantera båda riskerna. Se [stale/SoC-ADR](adr-2026-11-02-stale-telemetry-soc.md).

## 4. Undersökt inbyggd timeout och möjliga alternativ

| Funktion | Dokumentation / bedömning |
| --- | --- |
| **`CommsWatchDog`** | MHT-4~20K-manual har ON/OFF för förlorad kommunikation med **master**; en nyare Solinteg-manual för annan serie preciserar master/slav-**parallellsystem** och att invertern **stannar**, inte går till General. **Ej verifierad som Dell/Modbus EMS-watchdog; aktivera inte på den premissen.** |
| **Export Control Hard/Soft** | Rör kommunikationsfel mot elmätare/datalogger under exportbegränsning, inte timeout för `50207`. |
| **Solinteg OpenAPI EMS** | Dokumenterad realtidsstyrning och rekommenderad uppdatering 5–10 sekunder; leverantören varnar för chip-slitage vid alltför hög kommandofrekvens. Detta är **ingen dokumenterad lokal Modbus lease/TTL**. |
| **Ställbar EMS-fallback** | Ingen känd, verifierad inställning eller registerparameter som växlar från `0x303` till `0x101` efter en viss tid på **vår firmware**. Frånvaro av dokumentation är inte bevis för frånvaro av funktion. |
| **ToU (Time of Use)** | Enligt Solinteg OpenAPI kan dagens och morgondagens schema laddas upp; obesatta tidsluckor kör General och lagrade scheman kan repeteras efter dygnsskifte. Firmware/modell och eventuellt molncache påverkar. **Inte** automatisk fallback från ett redan aktivt EMS BattCtrl. Lokalt stöd på MHT-10K-25 är **inte verifierat**. |
| **EMS General `0x302`** | Registervärde känt; fysisk effektreglering, SoC-begränsning och beteende vid bortfall **inte verifierade**. |
| **Register från andra märken** | Exempel på watchdog-register hos andra tillverkare får **aldrig** överföras till Solinteg utan bekräftad registerkarta för vår modell. |

## 5. Risker och empiriskt solfall

**Kan vi förstöra något?** Inverter/BMS har grundskydd och ett enskilt rimligt börvärde inom tillåtna effektgränser bör normalt begränsas. Men felaktig riktning/effekt, kvarhängande urladdning, upprepade BMS-trippar, felaktig registerkombination eller belastning av en enskild fas kan ge säkringsutlösning, driftstopp, accelererat slitage och i ogynnsamma felkombinationer hårdvarurisk. Skydden är **inte** liktydiga med en garanti mot skador. Inga avsiktliga överlast-/låg-SoC-tester ska göras för att framkalla ett fel.

Det isolerade [solar-save replay-provet](adr-2026-11-02-battery-save-solar-shadow.md) visade **81** provenance-verifierade historiska 50 W-batterisparprover, **0** strikt verifierade solhandoff-fall eftersom historian saknar fysiska inverterinställningen `50006`, och **18 hypotetiska** fall om `50006=0` antas. Vid faktisk export >1 kW fortsatte batteriet urladda runt **150–290 W**; därefter laddade det vid observerad General/Auto-övergång. Detta är **inte** bevis att övergången är säker om PV plötsligt försvinner.

Hypotetisk riskanalys: observerade **5 363 W** urladdning, 22 % SoC, granskningsgräns 21,5 %, **antagen 120 s** sammanlagd detektions-/reaktionstid gav nominellt **~50,5 s** till gränsen för vissa prov; **12/18** hypotetiska kandidater uppfyllde inte antagen tidsbudget. Grovt avrundad SoC gör den verkliga marginalen osäker. Ingen av dessa siffror är mätt oberoende supervisor-latens eller certifierad kapacitet.

### Uppföljning 2026-11-03: skrivskyddad inventering, ingen ny Modbus-session

[Driftinventering och fysisk verifieringsmatris](../solinteg-fallback-readonly-inventory-2026-11-03.md)
visar att live-läget var EMS BattCtrl vid kontrollen, medan
SoC protection ON och EndSOC 10 % är **senast verifierade genom FC03
2026-11-02**. Firmware/BMS-revision är fortfarande okänd och gamla
`100xx`-identityprober har orsakat avbrott på referensinstallationen.
Under de senaste 24 timmarna noterades två `error_revert_failed`
med 132–140 sekunders telemetrigap trots senare återhämtning.
Ingen separat FC03-session utfördes; aktuell firmware kan hämtas via
OEM System Info/monitorering. **Beteende med kvarhängande `0x303`
vid nätbortfall är fortfarande NO-GO för automatisk fallback.**

### Uppföljning: nätverksorsak och tillverkarunderlag 2026-11-03

[Separat nätverksutredning](../solinteg-network-incident-2026-11-03.md)
identifierade ASUS-routerns **dagliga 03:00-omstart** som orsak till
Dells Wi-Fi-bortfall sju nätter i följd. Routerns omstartsschema
är nu avaktiverat och persistensverifierat, utan routeromstart eller
Modbus-/EMS-förändring. Ett andra fel 03:13 sammanfaller med
WAN-återställning men exakt lokal Modbuspåverkan är ännu okänd.
[Tillverkarspecifika frågor](../solinteg-vendor-ems-questions-2026-11-03.md)
är förberedda men ännu inte skickade. ARM-/DSP-/BMS-versionerna
är fortfarande okända. Detta ändrar inte **NO-GO** för fysisk fallback.

## 6. Beslutade nästa steg (ingen fysisk ändring godkänd)

1. Få **skriftligt svar från Solinteg för MHT-10K-25 och exakta ARM-/DSP-/dataloggerversioner**: finns en lokal EMS-börvärdes-TTL eller watchdog? Är den inställbar? Vilka paket/register återställer klockan? Vilken mode/effekt används vid timeout? Fungerar `CommsWatchDog` bara master/slav? Respekteras `52502/52503` i EMS med ett gammalt `50207`? Hur fungerar BMS-current-limits i samma felläge?
2. Identifiera BMS-modell och verifiera fysisk skyddskedja, larm, säkrings-/fasbeteende och inverterregister genom **läsning och säkra tillverkaruppgifter**, inte genom att provocera fram urladdnings-/överströmsfel.
3. Bedöm **ToU-stöd** och eventuellt `EMS General` på denna firmware. För ToU: kontrollera gammalt schema, överlappning, dagens/morgondagens giltighet, dagsskifte, utfall och lokalt readback; molncache är inte fysisk readback.
4. För kommande fysisk styrning: **ensam Modbus-ägare**, oberoende/privilegierad supervisor, isolerad testhärva, verifierad reaktionstid, SoC-/BMS-/CT-/L3-grindar, fysisk readback och säkert avbrott av ett redan aktivt tvångsbörvärde. Enbart Dell-baserad watchdog räcker inte vid Dell-strömavbrott.
5. **NO-GO** för automatisk solhandoff till General, 0 W-soft-hold/fullfrysning och broker-cutover tills respektive acceptanskriterier har verifierats och separat godkänts. Bevara befintliga services, skydd och 10 % native inställning.

## 7. Källor, proveniens, gränser

**Lokal evidens:** aktiv `systemd`-konfiguration/processmiljö; skrivskyddad SQLite `mode=ro`; Modbus **FC03 läsningar** av `52502/52503` och `33021/33023`; historiskt persistensprov; [SOC recovery](https://github.com/ermhawi/solinteg-controller-oss/blob/Sundsvall/SOC-RECOVERY-2026-10-23.md); [Modbusreferens](https://github.com/ermhawi/solinteg-controller-oss/blob/Sundsvall/MODBUS.md); [driftsinstruktion](https://github.com/ermhawi/solinteg-controller-oss/blob/Sundsvall/deploy/README.md); [solreplay](adr-2026-11-02-battery-save-solar-shadow.md). Inga register, systemd-tjänster, EVCC, inverterinställningar eller säkerhetsgrindar har ändrats i samband med utredningen.

**Externa källor:**

- [Solinteg OpenAPI – EMS Mode](https://apidocs-en.solinteg-cloud.com/en/docs/solutions/ems-ems-mode/) och [EMS/ToU overview](https://apidocs-en.solinteg-cloud.com/en/docs/solutions/ems-guide/).
- [Solinteg OpenAPI – ToU Mode](https://apidocs-en.solinteg-cloud.com/en/docs/solutions/ems-tou-mode/): General i tomma tidsluckor, schemapersistens och firmwareberoende.
- [Solinteg MHT-4~20K User Manual (offentlig spegel)](https://www.manualslib.com/manual/2960065/Solinteg-Mht-4-20k-Series.html), där installerad MHT-10K-25 hör till modellfamiljen.
- [Solinteg M2HT-25~50K User Manual](https://www.solinteg.com/wp-content/uploads/download/Solinteg-M2HT-25~50K-UM-EN.pdf), **annan serie**: explicit master/slav-avgränsning för CommsWatchDog är endast tolkningsstöd.
- [Sunwoda SunESS H teknisk specifikation](https://www.sunwodaenergy.com/residential/residential-ess-suness-h-series.html) (exakt batteri-/BMS-revision inte identitetsverifierad).

**Källkritik:** Fast konfiguration, aktuella BMS-annonserade tillstånd, registrerad kontroll-`applied` och **faktisk fysisk effekt** är olika evidensnivåer. Klienta API- eller forumuppgifter kan skilja från vår firmware. Historiska driftvärden får inte behandlas som säkerhetsgarantier. Ingen tillverkarspecifik EMS-timeout eller skyddsgaranti har ännu verifierats.
