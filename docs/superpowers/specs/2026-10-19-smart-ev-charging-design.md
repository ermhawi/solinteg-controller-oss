# Smart elbilsladdning i Solinteg — designspecifikation

Status: **Designspecifikation, revision 4, 2026-10-26**, med komplettering om två laddlägen och prisbaserad EV-skuggsimulering 2026-10-28. Bygger vidare på revision 3 (2026-10-21) och tidigare underlag på `Sundsvall`. Den separata skuggkoden har uppdaterats men **ingen produktionsdrift, driftskonfiguration, aktivering eller fysisk hårdvarustyrning ändras**.

Granskningsunderlag: revision 2:s käll- och driftgranskning vid `eedd4fef791ff46f056eb2a8823b9bf817955d8e`, ABL:s dokumenterade register, **separat godkända Modbus-läs- och skrivprov 2026-10-21** samt etapp-/L3-planen i `91c2802` (2026-10-24). Datum, råtelegram och bevisnivå skiljs åt i avsnitt 13. Fältproven gjordes före dokumentrevisionen; inga nya laddkommandon utförs här.

## 0. Nytt huvudspår: L3-samordning av husbatteriet, utan Waveshare

**Bekräftat användarkrav 2026-10-24:** Volvon styr själv när den laddar och sin amperegräns (för närvarande 18 A). Solinteg-appen ska med växelriktarens befintliga Modbus-data minska **husbatteriets** laddning när L3 blir kraftigt belastad. Om bilen börjar ladda när huset går på husbatteri ska Solinteg minska batteriets urladdning så att **elnätet försörjer bilens tillkommande last** och lagrad batterienergi bevaras. Det kräver inte att husbatteriet laddas från nätet samtidigt som det laddas ur. Ingen ny bil-/EVCC-integration, och ingen säkerhetsgaranti för andra apparaters belastning på L3.

**Avkänning och fysisk modell:** På aktuell enhet gav en read-only läsning av Modbus 50006 (Grid Unbalanced Output) värdet **0 = av** den 24 oktober. Under antagandet att invertereffekten därför fördelas jämnt kan husets L3-last skattas som `-meter_l3_w + inverter_ac_w/3`. Det är viktigare än `meter_l3_w` ensam, eftersom redan pågående batteriurladdning kan maskera bilens effekt i nätmätaren. I historiska prover 2026-10-23 ökade härledd L3-last från cirka 700 till 4250 W när bilen började ladda. Volvo HA-entiteten `sensor.volvo_xc40_charging_status` bytte till `charging` omkring 14:00 UTC, i linje med fassteg omkring 13:59 UTC. **Detta är ett klassificeringsantagande**, inte en exakt separat mätning av bilens ström; en stor annan L3-last kan också matcha. Beräkningen ska aldrig användas som säkringsskydd.

**Styrpolicy, endast batteriet:**
- Vid husbatteriladdning minskas laddningseffekten med hänsyn till en **ekonomisk mjuk L3-budget** (initialt 4000 W beräknad L3-huslast plus cirka en tredjedel av batteriladdningen). Denna budget är **inte** en huvud-/gruppsäkringsgräns. Minsta påtvingade laddmål är 300 W för att inte råka återgå till Auto som kan urladda batteriet till bilen.
- Vid hög, bil-liknande L3-last (initialt minst 2500 W) reduceras planerad batteriurladdning med den uppskattade extra L3-lasten (max 4300 W efter 600 W schablonbaslast). När bara en liten urladdning återstår används ett 50 W batterisparläge, **inte Auto**, eftersom Auto kan använda husbatteriet för bilen och 0 W EMS utan verifierad funktion har kända risker.
- När planen är Idle/Auto och en stor L3-last uppstår kan samma 50 W-läge låta nätet försörja huvuddelen av bilen. **Det är inte ett riktigt 0 W-hålläge**: historiska kontrollrader 2026-10-24 visar att 50 W beordrad urladdning gav cirka 160–260 W fysisk batterieffekt (bl.a. inverterförluster). Det begränsar kraftigt urladdningen men garanterar inte absolut oförändrat SoC. Ett riktigt 0 W-hålläge måste verifieras separat vid högre SoC, innan det kan användas. När bilen slutar ladda återgår ursprunglig plan. PV-överskott får inte onödigt hindras; SoC-recovery, BMS-golv, watchdog och befintliga säkra skydd har alltid företräde.
- Vid saknade/gamla/inkonsekventa L1/L2/L3-värden eller om 50006 inte kan verifieras till 0 i aktivt läge **avstår** den frivilliga ekonomiska L3-justeringen. Det innebär inte garanterat batteriskydd under mätbortfall. Latens cirka 30 s pollning plus 60 s dispatch accepteras för **ekonomisk** samordning, inte för överlastskydd.

**Implementationsstatus:** `scripts/services/l3_battery_policy.py` innehåller testbar ren beslutskod. Befintlig dispatch integreras via `DISPATCH_L3_BATTERY_MODE=0|shadow|1`, **default 0**. `shadow` beräknar/loggar utan att ändra styrkommandon. `1` får endast aktiveras efter replay, oberoende kontroll av 50006=0, och kontrollerad jämförelse av faktisk batteri-/nätförbrukning under samtidigt bilpåslag och högre SoC. Historiken verifierar **endast** att 50 W-batterisparläget minskar faktisk urladdning till cirka 160–260 W; den fullständiga funktionen under bilens belastning är inte verifierad. Ingen EVCC-kommunikation krävs. Den separat framtagna `evcc_ascii.py` är inte en del av detta huvudspår.

---

## 1. Mål, beslut och avgränsning

Solinteg ska automatiskt planera laddningen när Volvo XC40 Recharge Twin Plus ansluts till DEFA eRange Uno / ABL eMH1. Planeringen ska minimera hushållets och bilens **gemensamma** förväntade elkostnad, nå önskat SoC när det är möjligt och respektera installations- och utrustningsgränser.

**Integrationsstatus 2026-10-27:** Två lokala utvecklingsspår konsolideras på `Sundsvall`. Batteripolicyn och laddningsvakten hålls skilda; fysisk aktivering av EVCC och nya batterilägen kräver separat godkännande och verifiering. Texten nedan om EVCC är en möjlig senare etapp, inte en beställning om att aktivera skrivningarna.

---

## 1. Mål, beslut och avgränsning

Solinteg-appen ska automatiskt planera och styra laddningen när användarens Volvo XC40 Recharge Twin Plus ansluts till DEFA eRange Uno / ABL eMH1. Målet är låg gemensam kostnad för hushåll och bil, ett genomförbart laddmål och bevarade installationsgränser. Automatisk planering vid anslutning betyder inte att ström omedelbart måste slås på.

Användarbeslut, oförändrade:
- **Klar till avresa** (`deadline`, tidigare Smart) som standard,
  utan godkännande per laddsession: prioritera **80 % senast en vald
  avresetid**, initialt 07:00 Europe/Stockholm, även när sista
  nödvändiga elen är dyr. Prisoptimering sker inom den fysiskt
  genomförbara tid som återstår.
- **Ladda ekonomiskt** (`economy`) som alternativ: **inget krav på
  avresetid**, endast billiga prisfönster och ekonomiskt rimlig
  solöverskottsladdning, med stopp vid 80 %. Ett valfritt explicit
  högsta *totalpris för nätköp* kan sättas i användarinställningen.
  Relativt billiga fönster är inte automatiskt billiga i absoluta kronor.
- **Pausad**, och eventuella avancerade manuella lägen, får inte
  kringgå laddmålet eller elektriska skydd.
- Husbatteriet ska normalt inte användas för att finansiera bilens laddning. Husets grundlast och elektriska gränser har företräde.
- **Reservläge A:** laddningen ska kunna fortsätta automatiskt när bilens batteriprocent saknas. Bilens egen laddgräns används som lokal begränsning; osäker målstatus visas öppet.
- Laddsessioner, levererad energi, kostnadsunderlag och orsaker till styrbeslut ska loggas.

Prioritetsordning: elektrisk säkerhet och manuell paus; verifierad batteriskyddspolicy; därefter **lägesberoende mål**: `deadline` minimerar underskottet vid avresa före ekonomi, medan `economy` saknar avresetidskrav och hellre väntar på billig el. Därefter prioriteras lägre kostnad och färre omkopplingar. Ingen tidspress får kringgå säkerhet eller husbatteriskydd. Om `deadline` inte ryms ska appen visa det, inte lova 80 % eller tömma husbatteriet i smyg.

Den långsiktiga EV-målbilden stöder en laddpunkt och en konfigurerad bil. **Aktuell leverans är read-only HA/Volvo och batterisidig L3-analys i skuggläge; inte autonom EVCC- eller bilstyrning.** Ingen V2G, ingen generell laddboxplattform, inga nya kontroller av bilens laddfunktioner genom Volvo-API och ingen omskrivning av hela Solinteg-systemet. EM111 är en separat senare mätintegration, inte ett krav för att leverera läsning och tydligt märkta uppskattningar. Första versionens schemaval är en begränsad sökning efter bra genomförbara planer, **inte ett bevis på globalt lägsta möjliga kostnad**.

## 2. Befintlig kod och installationsfakta

### 2.1 Verifierad drift, inte referensinstallationens standardvärden

Vid granskningen var `solinteg-web`, `solinteg-poller` och `solinteg-dispatch` aktiva med arbetskatalog `/opt/solinteg/app`. Följande värden kontrollerades både i `/opt/solinteg/solinteg.env` och i de tre processernas miljö. Endast namngivna, icke-hemliga värden lästes ut.

| Parameter | Aktivt värde | Betydelse i designen |
|---|---:|---|
| `SOLINTEG_HOST`, port, enhets-ID | Konfigureras lokalt, publiceras inte | Avser växelriktaren, **inte** Waveshare/EVCC. |
| `SOLINTEG_BATTERY_KWH` | 15.055 kWh | Nuvarande beräkningskapacitet; inte en ny mätning av användbar batterienergi. |
| `SOLINTEG_BATTERY_MAX_W` | 7500 W | Befintlig programgräns för husbatteriet. |
| `SOLINTEG_GRID_CAP_W` | 10000 W | Planerarens sammanlagda nätbudget. |
| `DISPATCH_IMPORT_LIMIT_W` | 10500 W | Separat konfigurerat importtak; inte ett verifierat strömskydd per fas. |
| `SOLINTEG_SOC_FLOOR_PCT`, tak | 12 %, 100 % | Husbatteriets befintliga gränser. Bilens 80 % är ett annat mål. |
| `SOLINTEG_CONTROL_ARMED` | 1 | Batteriets verkställare är redan aktiv. EV-simulering måste isoleras även från dess planunderlag. |
| `DISPATCH_BATTERY_SAVE_W` | 50 W | Begärt lågurladdningsmål; inte bevis på noll eller 50 W faktisk effekt. |
| `PRICE_ZONE` | SE2 | Återanvänd befintlig priszon. |
| `SKATT_OVERFORING_ORE` | 71.38 öre/kWh | Aktiv avgiftsparameter; ska avstämmas mot avtalet, inte ersättas med kodens 71-standard. |
| `SUPPLIER_SURCHARGE_ORE` | 9.29 öre/kWh | Aktivt påslag före den befintliga momsberäkningen. |
| `EXPORT_BONUS_ORE` | 1.5 öre/kWh | Aktiv exportparameter, inte referensvärdet 5.1. |

`SOLINTEG_HOLD_ENABLED`, `DISPATCH_FREEZE_HOLDS` och `SOLINTEG_REFUND_CREDIT` var inte satta och är därmed av enligt aktuell kod. Källfilerna `lib/plan.ts`, `lib/optimizer.ts`, `scripts/services/dispatch_loop.py` och `scripts/services/modbus_poller.py` matchade sina driftsatta källkopior byte för byte. Det är inte en separat verifiering av varje byggartefakt.

Anläggningsuppgifterna 5,4 kWp solpaneler och Sunwoda cirka 15 kWh kommer från användarens tidigare uppgifter. Exakt växelriktarmodell, batteriets effektkurva och bilens användbara batterikapacitet ska inte härledas ur upstream-dokumentens modellnamn. Referensvärdena 25,6 kWh, 15,36 kW och gamla elavtal i koden gäller inte automatiskt denna installation. Inga befintliga driftvärden ändras som en bieffekt av EV-funktionen.

### 2.2 Laddbox, gateway och protokoll

Identifierad laddbox: `1W72D4 / 710444`, DEFA/ABL eMH1, tillverkad 2019-02-27, enfas 230 V, märkström 32 A. **2026-10-21 verifierades aktiv laddning vid cirka 16 A och lyckad sänkning till 10 A** (2.2.1). Användaren har uppgett **20 A huvudsäkring**. Varken den uppgiften eller laddprovet visar ännu vilken fysisk nätfas laddboxen använder, hur mycket samtidig hushållslast som finns eller att 16 A alltid är säkert. Märkström 32 A är inte ett installationsutrymme; även bil/kabel kan begränsa strömmen.

Verifierad gateway: **Waveshare RS485 TO ETH (B)**, lokalt konfigurerad adress/port (inte publicerad), TCP-server med transparent Modbus ASCII 38400/8E1 och EVCC-adress 1. Matning från RFID +12 V/0 V och två RS485-ledare A/B; ingen direkt ansluten EVCC M-ledare. Tidigare mättes cirka **100 Ω** mellan EVCC M och RFID 0 V samt **5 mV** mellan EVCC M och Waveshare V− i drift. Detta är lugnande i vila men utesluter inte transienter; **ingen bygel till EVCC M, V− eller skyddsjord utan verifierat schema**. `EVCC/RFID` och `METER` är olika bussar, så EM111:s separata mätbuss nås inte genom en extra adress på EVCC-bussen.

Protokollkontrakt från ABL Rev. B [S1], s. 3–12:

| Funktion | Kontrakt |
|---|---|
| Transport | Modbus ASCII, 38400/8E1; begäran `:`, svar `>`, CRLF och LRC. |
| Läsning / skrivning | FC03 / FC10, inte FC06. |
| Identitet | `0x0001`, två register; konfigurationsbit 5: strömmätning, bit 6: upstream-timeout. |
| Mätdata | `0x002E`, fem register: status, faktisk pilotbegränsning, Ict1–3 med 0,1 A upplösning. |
| Ogiltig ström | `0x03E8` i fullformat; `0x64` i kompaktformat. Inte noll eller 100 A. |
| Strömbörvärde | `0x0014`: PWM-duty × 10; för vårt 6–32 A-område `floor(A / 0.06)`. |
| Paus | `0x0014 = 0x03E8` betyder 100 % duty, ingen tillåten laddström. Inte råvärde 0. |
| Konfiguration | `0x002C`, endast i E2, påverkar ID, timeout och återstartsbeteende. |

Identitets-/konfigurationsvärdet `01 41 40 BD` **lästes återigen under laddning 2026-10-21**. `0xBD & 0x20 != 0` anger aktiv strömmätningsförmåga; **`0xBD & 0x40 == 0` innebär att upstream-timeout är avstängd**. Ett nät-/Dell-bortfall är alltså **inte** ett verifierat automatiskt laddstopp. En fungerande enhetslokal spärr eller oberoende fysisk lösning måste fortfarande provas före autonom lastbalansering.

Drivrutinen ska separera råvärde, begärd ström, annonserad ström och uppmätt ström. **Fältverifierat:** `0x010B = 267 = 16,02 A` (ursprungsvärde) och `0x00A6 = 166 = 9,96 A` (begärda 10 A). När installationens **hårda** strömtak är exakt 16 A ska en ny styrberäkning avrunda nedåt till `floor(16/0.06)=266` (15,96 A); återställning av ett sparat, tidigare råbörvärde `0x010B` är en skild teståtgärd. Vid skrivtestet följde **Ict2** med 15,5 → 9,6–9,8 → 15,2–15,5 A, medan Ict1/Ict3 låg kring 0,1 A. Ict2 är verifierad intern mätkanal, **inte** därmed säkert fastställd fysisk nätfas L2. C2 är aktiv laddning, inte uppnått bil-SoC-mål.

Waveshares dokumentation skiljer mellan transparent överföring och TCP/RTU-konvertering. Den vanliga B-modellen saknar den isolering som anges för PoE B [S2]. Detta ska beaktas vid installationskontrollen; det bevisar inte orsaken till tidigare kommunikationsfel. Signalreferens och skyddsjord får inte likställas. Varken nät-, gateway- eller elinstallation ändras av den här designrevisionen.

### 2.2.1 Fältprov 2026-10-21: skrivning, fysisk respons och verifierad återställning

Testväg: lokal testvärd → LAN → Waveshare (privat adress/port utelämnad) → EVCC2 → ansluten Volvo XC40. Efter uttryckligt användargodkännande skrevs **FC10, ett register och två databyte, till adress `0x0014` på enhet 1**. Varje börvärde skrevs två gånger, och FC03-status lästes efteråt. Samtliga mottagna ramar kontrollerades för korrekt form och LRC.

| Steg | PWM/börvärde | Faktiskt Ict2 | Tillstånd och resultat |
|---|---|---|---|
| Baslinje | `0x010B` = 16,02 A | 15,3–15,5 A | C2, verifierad via både `0x000F` och `0x002E`. |
| Skriv befintligt värde två gånger | `0x010B` = 16,02 A | 15,3 A | Två giltiga FC10-kvitton, ingen oavsiktlig ändring. |
| Sänk två gånger till cirka 10 A | `0x00A6` = 9,96 A | 14,0 A efter ~1 s; **9,7 A efter ~6 s** | C2, båda kvittona giltiga. |
| Håll reducerad nivå ~31 s | `0x00A6` = 9,96 A | **9,6–9,8 A** | Stabilt, laddningen fortsätter. |
| Återställ två gånger | `0x010B` = 16,02 A | 9,8 A direkt, 13,2 A efter ~3 s, **15,2 A efter ~6 s** | C2, två giltiga kvitton och läsbackar. |
| Oberoende slutkontroll | `0x010B` = 16,02 A | **15,5 A** | C2, återställningen ytterligare verifierad. |

**Utfall:** skrivning, reducering och återställning lyckades **utan observerade skriv-, kontrollsumme- eller återställningsfel**. Ca sex sekunder är en **observerad bilreaktion i detta prov**, inte en garanterad maximal fördröjning. Detta visar fungerande styrkedja under aktiv laddning; det är inte verifiering av felsäkert stopp efter tappad styrenhet, långtidsprestanda eller faslastbalansering.

### 2.2.2 Korrekt readback och vilobeteende per EVCC-tillstånd

**FC10-kvittot `>011000140001DA` ekar endast registeradress och antal, inte börvärdet.** FC03 direkt mot `0x0014` gav två timeouts och får inte användas som enda efterkontroll. Tillförlitlig läsback i provet var **FC03 från `0x000F` med fem register**, där det aktuella konfigurerade värdet ingår, samt **FC03 från `0x002E` med fem register** (C2, annonserad PWM och Ict1–3). Lyckat styrbeslut kräver rätt kvittens **och** avläst börvärde/status samt vid aktiv laddning observerad strömförändring eller uttryckligt okänd fordonsrespons. Råtelegram: [H2] i avsnitt 13.

**Tillståndsskillnad måste hanteras:** vid A1 (ingen bil) 2026-10-19 försvann den första Modbus-läsningen vanligtvis efter cirka **10,5–12 sekunders tyst RS485-buss**; den andra lyckades i ~14–16 ms. Ping, HTTP och ny TCP-session räckte inte för väckning; en föregående läsförfrågan över RS485 gjorde det. Vid **C2 (pågående laddning) 2026-10-21** svarade däremot första läsningen efter **20, 40 och 60 s** utan Modbus-trafik, på **18,7–19,2 ms**, med fortsatt 16,02 A-börvärde och 15,3–15,5 A ström. Detta ger starkt stöd för **tillståndsberoende vila**, men olika testtillfällen/okänd intern mekanism utesluter inte andra förklaringar. Behåll begränsade, read-only retries i alla tillstånd och testa även övergångarna A1→C2/C2→A1.

### 2.3 Befintlig Volvo-integration

HA-repots `apps/home-assistant/ha-config/dashboards/volvo_xc40.yaml` refererar redan till:

- `sensor.volvo_xc40_battery`
- `sensor.volvo_xc40_target_battery_charge_level`
- `sensor.volvo_xc40_charging_status`, `sensor.volvo_xc40_charging_power`
- `sensor.volvo_xc40_charging_limit`, `sensor.volvo_xc40_battery_capacity`
- `sensor.volvo_xc40_estimated_charging_time`

Återanvänd denna integration i stället för att skapa en konkurrerande Volvo-klient. Referenser i dashboarden bevisar inte att alla värden är tillgängliga, färska eller i rätt enhet just nu. Kapacitetsvärdets brutto-/netto-betydelse måste valideras. `charging_limit` är bilens strömgräns, medan `target_battery_charge_level` är procentmålet; de får inte förväxlas. Volvo Energy API v1 stängdes den 20 februari 2026; en eventuell underliggande API-koppling måste använda stödd version och fordonskapabiliteter [S3].

Home Assistants Volvo-dokumentation anger normalt två minuters uppdatering av energidata och inget direkt start-/stoppstöd för laddning [S6]. Om det separata EVCC-spåret senare godkänns är laddboxen dess ställdon; i det aktuella read-only-spåret styrs varken bilen eller laddboxen. På vissa modeller kan `charging_power_status=fault` betyda att en ansluten laddare är pausad. Den molnstatusen får inte ensam klassas som ett elektriskt EVCC-fel eller trigga reset. Kontrollera källtid och råvärdets betydelse i den installerade integrationen; använd inte molndata som snabbt fas- eller stoppskydd.

## 3. Arkitekturbeslut

### 3.1 Behåll samordnaren, men rätta integrationskontraktet

Alternativ C behålls som **framtida målbild om EVCC-styrd laddning väljs**: en EV-planerare runt befintlig batteri-DP. Den aktuella batterisidiga L3-analysen är ett separat shadow-spår utan laddboxberoende. Fristående, konkurrerande optimerare undviks; en helt ny tvådimensionell EV/batteri-DP behövs inte i första leveransen. **Det räcker däremot inte att lägga bilens kWh till `consumptionKwh` och lämna batteriplanerare/verkställare orörda.**

Konkreta integrationspunkter i den granskade koden:

| Kod | Nuvarande beteende | Nödvändig anpassning |
|---|---|---|
| `lib/optimizer.ts`: `dpCore`, `computeFlows`, `evaluateDispatch` | En gemensam last, hushållsmarginal och möjlighet att batterimata hela lasten. | Separat EV-last, korrekt hushållsreserv och samma skyddsbegränsningar i sökning, flöden och värdering. |
| `lib/plan.ts`: `producePlan` | Hämtar data, beräknar och publicerar från både sidrendering och omplaneringsanrop. | Skilj ren kandidatutvärdering från auktoritativ publicering. |
| `lib/telemetry/dispatch.ts`: `logOptimizerRun` | Best effort, returnerar `void`, deduplicerar endast befintliga planfält. | Transaktionell publicering med explicit resultat/ID, session och inställningsrevision. |
| `scripts/services/dispatch_loop.py` | Väljer nyaste täckande plan; live-reglering kan batterimata en ny EV-last. | Gemensam planversion, verklig EV-last, batteriskydd och handskakning vid effektändringar. |
| `lib/telemetry/readings.ts`: `readTrailingLoadProfile` | Lär från timmedel av total `house_load_w`. | Subtrahera tidsmatchad EV-last före medelvärden och reservkvantiler, med kvalitetskontroll. |
| `scripts/services/watchdog.py` | Återgår till inverter-Auto vid död dispatcher. | Samordna med EV-stopp; Auto är inte ett neutralt tillstånd för husbatteriet. |

### 3.2 Datainsamling och kandidatval

Om en framtida EVCC-styrväg väljs ska en lokal Python-tjänst, `scripts/services/evcc_service.py`, ensam äga EVCC-länken och serialisera mätning och styrning. Home Assistant och webbgränssnittet läser via lagrad status, inte genom egna parallella anslutningar till Waveshare. EV-tjänsten skriver aldrig inverterregister.

Koordinatorn använder en sammanhängande snapshot: tid, session, inställningsrevision, prisversion, prognos, husbatteri och verifierad EV-telemetri. Bygg kandidatplaner med billigaste genomförbara intervall, sammanhängande laddblock och solöverskott; förbättra dem med ett begränsat antal deterministiska omflyttningar. Ha ett konfigurerat beräknings-/kandidatbudgettak. Föregående fortfarande genomförbara plan ska vara kandidat. Tidskritisk hårdvarustyrning får inte vänta på att en stor kandidatsökning blir färdig.

Kandidater ska ha samma antagna EV-energibehov, prisserie, hushållsprognos, säkerhetsvillkor, batterihorisont och terminalvärdering. Behåll batteriplanens horisont efter bilens avresa; kapa inte hela batteriproblemet vid 07:00. Välj genomförbarhet/måluppfyllelse först, därefter lägsta jämförbara kostnad inom sökningen. Spara beräkningsbudget och utvärderad lösningskvalitet; kalla inte en heuristik globalt optimal.

### 3.3 Simulering får inte påverka redan aktiv batteristyrning

`SOLINTEG_CONTROL_ARMED=1` är aktivt. Därför är följande ett absolut kontrakt:

**En hypotetisk EV-plan får inte publiceras till `optimizer_runs` eller annan källa som den levande batteridispatchern verkställer.** `EVCC_CONTROL_ARMED=0` hindrar bara EVCC-skrivningar och är i sig inte tillräcklig isolering.

Återanvänd ren planeringslogik, inte `producePlan()` med dess publiceringsbiverkningar för varje kandidat. Spara simulering i `ev_plans` med `execution_mode=shadow`, aldrig i den levande planpekaren. Under read-only/shadow fortsätter det befintliga systemet hantera **verklig** last som tidigare; kandidatens framtida EV-last får inte läcka in. Energiförbrukning som faktiskt mäts från bilen är däremot verklig driftdata.

### 3.4 Publicering och fysisk samordning

Publicera det valda EV-/batteriplanparet i en SQLite-transaktion. En plan innehåller `generation`, `schema_version`, `session_id`, `settings_revision`, `generated_at_utc`, `valid_from_utc`, `valid_until_utc`, indata-ID och båda utgångarna. Alla krav måste valideras innan en aktiv pekare byts. Kontrollera efter kandidatberäkningen att session och inställningar fortfarande gäller. Återanvänd oförändrad generations identitet i stället för att koppla en ny EV-plan till en okänd tidigare deduplicerad batterirad.

Publicering returnerar `published`, `generation` och felorsak; databasfel får inte kvitteras som aktiverad plan. Läsvyer får fortsatt degradera best effort, men styravsikt/planaktivering får inte tyst försvinna. Den gamla `socIsLive`-spärren för husbatteriet ska bevaras. Reservläge A gäller bil-SoC och får inte användas för att publicera en gissad husbatteri-SoC. Identiska planer får förlängd giltighet endast efter förnyad validering av indata och revision; att samma JSON redan finns i databasen är inte en färskhetskontroll.

När samordnad EV-drift är aktiverad ska dashboardens läsrendering läsa den senast publicerade planen i stället för att publicera en egen konkurrerande batteriplan. Låt en enda auktoritativ, revisionskontrollerad väg publicera från tids-/händelsestyrd omplanering. Med EV-funktionen avstängd eller i shadow bevaras nuvarande skarpa batteribeteende; detta är en riktad gränssnittsändring, inte ett krav på att först bygga om hela webbappen.

En gemensam databastransaktion innebär **inte** samtidiga fysiska kommandon. Vid EV-start/höjning ska batteridispatchern först bekräfta planversion, frigjord effektbudget och tillämpligt batteriskydd. EV höjs först därefter. Vid minskning/stopp reduceras EV först, varefter reserverat utrymme frisläpps. Bekräftelser har begränsad giltighet. Planbyte, omstart och fel mitt i sekvensen ska lämna en konservativ effektgräns, inte två gamla/nästa planer i godtycklig kombination.

Om en av verkställarna är inkompatibel eller har tappat sin samordningsstatus stoppas den kopplade EV-styrningen. Ingen återgång till en godtycklig äldre EV-generation. Befintlig batterisäkerhet får inte fördröjas när dess återställning behövs; EV-interlock och enhetens felsäkra beteende måste täcka denna situation.

Föreslagna nya filer behålls: `lib/ev/{types,scheduler,economics,repository,vehicle-soc}.ts`, `scripts/services/evcc_service.py`, `app/components/EvChargingCard.tsx`, EV-API-rutter och `deploy/solinteg-evcc.service`. Kända befintliga ändringspunkter i tabellen ovan ingår uttryckligen i omfattningen; inget nytt konkurrerande inverterstyrsystem införs.

## 4. Konfiguration och state

### 4.1 Användarval kontra driftsäkerhet

Beständiga användarinställningar: `mode=smart`, `target_soc_pct=80`, `departure_local_time=07:00`, `timezone=Europe/Stockholm`, önskad ström högst installerad maxgräns samt `battery_ev_discharge_policy=protect`. Bilens verifierade användbara kapacitet och laddförlustmodell lagras med källa och revision. Ingen godtycklig 75-/79-kWh-konstant får väljas från ett annat modellår.

Aktiveringsflaggan `EVCC_CONTROL_ARMED` och verifierade elektriska maxgränser finns i skyddad lokal driftskonfiguration, inte i skrivbara användarinställningar. Befintliga inverterflaggor ändras inte automatiskt. GUI får sänka önskad ström och välja läge men inte höja installationsgränsen. En väl verifierad aktivering gäller löpande; inget nattligt manuellt godkännande införs.

Den lokala bilgränsen hämtas i första hand från den befintliga HA-sensorn när den är tillförlitlig. En engångsbekräftelse (`vehicle_local_limit_confirmed_pct`, tid och källa) stöder reserv A när läsning saknas. Bekräftelsen är ett antagande om bilens kvarvarande inställning, **inte ett tekniskt lås**. Senare känd avvikelse eller ett ändrat appmål gör matchningen ogiltig. Appen ändrar inte bilens gräns i första versionen och får inte påstå att 80 % säkert upprätthålls om bilen ändrats utan att integrationen ser det.

### 4.2 Tillstånd och användaravsikt

Separera `connection_state`, rå EVCC-status, `desired_mode`, `control_state`, datakvalitet och `stop_reason`. Kommunikation som saknas är `unknown`, inte bevisat `unplugged`. `complete` kräver måluppfyllelse från tillförlitlig bilinformation; ett fordonsstopp utan SoC blir `vehicle_stopped/target_unknown`. Nollström ensam kan även bero på schema, paus, fel, fördröjd fordonsrespons eller klimatisering.

Fysisk anslutning skapar eller återupptar en beständig session. Kort nätbortfall/omstart skapar inte nya sessioner. Tillståndsväxlingar ska filtreras mot kontaktstuds. EVCC identifierar inte vilken bil som är ansluten; installationens antagande om den konfigurerade Volvon dokumenteras. Känd konflikt med bilens anslutningsstatus/geografiska kontext ska markeras och Volvo-SoC ska då inte tillämpas på en annan bil. Ingen ny insamling av position krävs för att leverera första versionen.

Manuell paus är en beständig spärr tills användaren återupptar. Den ska inte raderas av sidrendering, ny prisdata, omstart eller kabelanslutning. Snabb och Solöverskott kan väljas för aktuell session utan att förstöra standardprofilen Smart för kommande sessioner. Bilen behåller sin egen laddgräns i alla lägen. Konkurrerande smarta scheman i bilen, Tibber eller HA ska vara avstängda för denna laddpunkt eller tydligt redovisade som externa begränsningar; bara Solinteg bestämmer det automatiska EVCC-schemat.

### 4.3 Avresetid, SoC och klockor

Lås ett absolut `departure_at_utc` vid sessionsplanering: nästa 07:00 Europe/Stockholm strikt efter anslutningstidpunkten. Omplanering flyttar inte tyst tidsfristen ett dygn. Vid 07:00 avslutas Smart-uppdraget med uppnått mål, okänd status eller dokumenterat underskott. En kvarvarande kabelanslutning ska som standard inte skapa ett nytt fullt reservbehov eller oannonserad dagladdning. Ny anslutning eller ett uttryckligt nytt uppdrag kan använda nästa standardtid; en framtida vald regel för daglig återkommande laddning kräver separata uppdrags-ID:n och får aldrig skriva om föregående utfall. Detta preciserar standarden automatisk laddning vid kabelanslutning utan att införa nya dagliga energibudgetar av misstag. Under väntan är EV pausad; manuellt vald Snabb får fortsätta enligt sin egen sessionsavsikt. Solöverskott följer sitt sol-/procentmål och lovar inte ett tidsmål.

Lagra bilvärdets observationstid, hämtningstid och källa separat. HA:s `last_changed` är inte en mät-tidsstämpel, och en ny hämtning av samma gamla molnuppgift gör den inte färsk. Använd källans uppdateringstid där den finns; annars sänk tillförlitligheten. Ett föreslaget initialt SoC-ålderstak är 30 minuter, konfigurerbart och validerat mot integrationens faktiska uppdateringsmönster. Felaktigt framtida tidsstämplade värden och `unknown/unavailable` avvisas. Den gamla dashboardens `float(0)`-presentation får inte återanvändas som styrdata.

## 5. Optimeringsmål och begränsningar

### 5.1 Separera hus och bil i både prognos och drift

För den nya planversionen uttrycks baslast och EV-last separat. Beräkningen ska motsvara:

`planningLoadKwh = max(baseHouseKwh * loadFactor, baseHouseReserveKwh) + evKwh`

Hushållets osäkerhetsmarginal ska alltså **inte** multiplicera den planerade EV-energin. Spara också den omarginaliserade baslasten för rättvis prognosvärdering. Skyddsvillkoren måste finnas både i DP:s bakåtpass och dess flödes-/ekonomiberäkning; en efterhandskontroll kan inte ensam göra en tidigare olaglig optimal bana genomförbar.

Historiska `readings.house_load_w` är total förbrukning bakom mätningen, inklusive bilen när den finns där. Originaldata ändras inte. En ny, versionsmärkt vy härleder `base_house_w` genom tidsmatchad subtraktion av giltig EV-effekt **innan** timmedel, väderjustering och reservkvantiler beräknas. Vid luckor under laddning utesluts berörda träningsintervall eller används en tydligt märkt konservativ reservmodell. Okänd EV-effekt får inte behandlas som noll och sedan läggas till en gång till i planen. Historiska v1-planer behåller sin gamla betydelse.

### 5.2 Vad batteriskydd betyder

`protect` betyder att bilens anslutning inte ska höja husbatteriets urladdning utöver den effekt som behövs för den icke-EV-last som återstår efter tillgänglig sol. På en gemensam AC-skena kan elektroner inte märkas efter mottagare; detta är en uttrycklig styr-/allokeringspolicy, inte fysisk kretsseparation.

En konservativ operativ begränsning under EV-laddning är:

`batteryDischargeAcW <= max(0, baseHouseAcW - pvAvailableAcW) + verifiedIdleToleranceW`

Toleransen ska representera mätosäkerhet och verifierad tomgångsdrift, inte ett dolt tillstånd att mata bilen med flera kW. Ingen extra batteriexport planeras samtidigt med EV-laddning i första versionens `protect`-läge. Husbatteriet får fortfarande täcka husets verkliga underskott. Att stoppa bilen vid **varje** positiv `battery_w` vore därför också fel.

Befintlig `live_discharge_power_w()` använder total huslast och planerat nätflöde. Den måste kompletteras med EV-medveten begränsning; annars kan en laddstart eller högre faktisk EV-effekt tolkas som en hushållstopp som batteriet ska täcka. En konkret korrektion, med positivt nätvärde för import, är `trackingGridW = plannedGridW + (actualEvW - plannedEvW)`: EV-avvikelsen ska i första hand ändra tillåten nätimport, inte batteriurladdningen. Därefter krävs också det separata taket för batteriets försörjning av baslasten ovan; korrektionen ensam gör inte en felaktig grundplan säker. Tidsjusterade observationer och samma mätgräns är förutsättningar.

Även laddsidans effekthantering, sparläget och övergången till Auto måste ta hänsyn till EV-budgeten. Hårdvarudrivrutinen för växelriktaren behålls, men verkställarens policy kan inte lämnas oförändrad. Ett nytt skydd får inte bara läggas på den normala urladdningsvägen och sedan kringgås av en befintlig återställnings- eller reservväg.

Använd konsekventa AC-/DC-gränser. Solintegs råa nätvärde är positivt vid export, medan planens `gridKwh` är positivt vid import. `battery_w` är positiv urladdning på batterisidan, inte ström i ampere. Definiera en enda normalisering vid gränssnittet. PV-, inverter-AC- och batteridata får inte antas vara förlustfria utbytbara watt; verifiera omräkning och balansekvationer mot installerad mätning. Osäker energikälla ska inte ge optimistiskt solöverskott eller skenbart exakt kostnad.

**Full freeze är inte bara oprövad:** `MODBUS.md:266–306` dokumenterar ett lokalt misslyckat försök: cirka 1,3 kW nätimport och cirka 0,5 kW batteriladdning trots nollade register. Upstream-enhetens senare soft-hold-prov gäller en annan verifiering. Ingen av dessa får automatiskt aktiveras här. `DISPATCH_FREEZE_HOLDS` förblir av. Använd bara ett lokalt verifierat sätt att begränsa urladdningen; annars markeras batteriskydd som ej verkställbart och EV reduceras/pausas. Ett hårdvarubegränsat målunderskott ska vara synligt, inte döljas av ständiga omplaneringar.

### 5.3 Fasgränser är inte summan av tre faser

Vår laddbox är enfasig. En total nätbudget på 10 kW eller Solintegs importtak på 10,5 kW garanterar därför inte att laddfasens säkring klarar EV plus andra laster. Import på laddfasen kan också döljas av export på andra faser. Växelriktaren kan begränsa sitt eget energiutbyte men kan inte alltid kompensera en extern laddlast, exempelvis med tomt batteri.

Automatisk variabel laddning kräver verifierad strömbudget på laddfasen: antingen ändamålsenlig lokal lastbalansering med tillräcklig svarstid eller en installationsbedömd statisk EV-budget som är säker utan att förlita sig på snabb programreglering. `meter_l1_w..meter_l3_w` är aktiv effekt, inte fullständig RMS-strömmätning. Att dividera med 230 V är ingen generell säkringsgaranti när effektfaktor, spänning och övriga laster är okända.

Solinteg pollas normalt var 30:e sekund och dispatch-loopen var 60:e sekund; protokolldokumentet i repot beskriver dessutom flera sekunders gatewayfördröjningar. Den kedjan ska inte användas som enda snabba överlastvakt. Höj inte inverterpollfrekvensen eller öppna extra inverterklienter för att kringgå problemet. Varken 6 A eller 16 A är en automatiskt säker reservström för en okänd belastad fas.

### 5.4 Ekonomiskt mål och realiserbara funktioner

Rangordna först säkerhet/batteripolicy och minsta återstående energiunderskott vid avresa. Bland likvärdigt genomförbara kandidater minimera:

`C = sum(importKwh * buyOre - exportKwh * sellOre + batteryWearOre) - terminalEnergyCreditOre`

**Förtydligande 2026-10-28, delad ekonomisk princip:** prisreglerna ska gälla **båda** batterierna, inte en egen godtycklig billig-el-tröskel för Volvon. EV-planeraren använder exakt samma fullständiga `OptimizerSlot.buyPrice` (spot, leverantörspåslag, moms, skatt/överföring), `sellPrice` (alternativkostnad för solel), prisprognos och riskpremie som husets optimerare. Vid höga köppriser väntar `economy` eller använder enbart ekonomiskt lämpligt överskott efter husets prioriterade lagring; `deadline` får däremot använda även dyr nätel när 80 %-målet annars missas. När nätel är relativt billig får nätet täcka återstående bilenergi i båda lägena. För batterierna gemensamt måste lägsta **systemkostnad** och husets reserverade batteri/överlastskydd bevaras även när elbilens last påverkar husoptimerarens plan.

**Kodstatus 2026-10-28, två laddlägen:** `scripts/services/ev_price_policy.py` läser husets redan publicerade fullständiga priser och prognosflöden. `deadline` prioriterar 80 % vid en fast avresetid, väljer billigast genomförbara kvart och kan vid akut tidsbrist föreslå laddning utan prisdata, men aldrig förbi elsäkerhets- eller batteriskyddsgränser. `economy` har ingen avresetid och laddar bara under de relativt billigaste prisfönstren (initialt 25 % av upp till 48 h, strikt under positiv median) samt med ekonomiskt lämpligt solöverskott; en konfigurerbar absolut köpprisgräns får begränsa detta ytterligare. Båda stoppar förslaget vid 80 %. Planeraren läser `optimizer_runs` men varken ompublicerar det eller skickar laddkommandon. Denna approximation **är ännu inte en gemensam optimering av båda batterierna**; samordnad EV-/husplanpublicering, korrekt skydd mot husbatteriurladdning för bilens last, en betrodd producerad HA-session och fysisk säkerhet återstår. Se `docs/evcc-solar-shadow.md`.

Därefter välj färre onödiga start/stopp vid i praktiken likvärdig kostnad. Riskmarginaler och eventuell omkopplingsstraffkostnad är planeringspolicy, inte poster på elräkningen. Behåll befintlig riskhantering och använd samma `loadFactor`, hushållsreserv och terminalvärde vid jämförelse. Dra inte ett nytt fullständigt slitagebelopp för bilbatteriet utan en motiverad modell.

För nuvarande angivna privatkundstariff införs ingen påhittad effektavgift, se 8.3. Ett framtida verifierat effekttariffavtal behöver ett separat tillstånd för faktureringsperiod, medelvärdesfönster och redan inträffade toppar. Det kan inte representeras av ett godtyckligt per-slot-påslag och ingår inte i första sökproblemet.

Befintlig DP antar också att sol kan begränsas när exportpriset är negativt (`curtail` i `lib/optimizer.ts`). I den granskade dispatchern finns ingen explicit motsvarande PV-begränsningsåtgärd. EV-kandidater får därför inte räknas hem med en sådan åtgärd som om den vore verifierat tillgänglig. Den nya ekonomivägen ska värdera den faktiskt tillgängliga funktionen; ange annars modellbegränsningen och undvik optimeringsbeslut som kräver orealiserad begränsning. Gissa inte att en inverterinställning redan löser detta.

### 5.5 Tid, energibehov och lägen

Beräkna känt energibehov som `capacityUsableKwh * max(0, targetSoc - currentSoc) / 100`, och skilj batterienergi från AC-energin som behöver köpas/levereras. Laddförluster och hjälpförbrukning ska ingå en gång, med en osäkerhetsmarginal och uppdatering från verkliga sessioner. Låg laddström är inte synonymt med låg kostnad; kortare laddning i billigare intervall kan vara bättre. Ströminställning är ett tak, inte ett löfte om att bilen tar exakt den strömmen.

Varje kandidat begränsas också uppåt av återstående användbart laddbehov och separat motiverad hjälpförbrukning. Negativa priser får inte ge en fiktiv vinst genom att planera energi som en redan full bil inte kan ta emot. Det räcker inte att bara minimera underskott; övre energi-/SoC-gränsen och faktiskt tillgängligt laddfönster ska ingå i genomförbarhetskontrollen.

Kanoniska tider är absoluta UTC-start/sluttider. Behåll prisleverantörens offset när priser tas in; `lib/prices.ts` tar i dag bort den, `lib/economics.ts` använder lokala nycklar och Python tolkar ett naivt första intervall. Enbart rätt antal 92/96/100 intervall fixar inte den upprepade hösttimmen. Lägg till entydiga tidsfält genom pris, plan, SQLite och båda verkställarna; behåll äldre visningsfält genom en explicit bakåtkompatibel adapter. Okänd gammal dubbel timme får inte fabriceras till exakt historisk kostnad.

Första pågående prisintervallet och ett eventuellt delintervall före deadline får bara tillgodoräkna **återstående verklig tid**. Den nya kopplade planvägen behöver explicit `durationHours` (0,25 som kompatibel standard): energi-/effekttak, SoC-utveckling, värdering och Python-konvertering måste använda samma varaktighet. Ett planeringsanrop 06:59 får inte få 15 minuters laddkapacitet före 07:00. Detta kräver riktade ändringar av de nu hårdkodade `/4`- och `SLOT_HOURS`-beräkningarna där kopplade planer passerar, inte en global orelaterad tidsrefaktorering.

För delintervallen behöver även SoC-kvantiseringen kontrolleras. Nuvarande `dpCore` använder `Math.max(1, floor(...))` för tillåten SoC-förflyttning: att bara skala kvartens kWh med kvarvarande tid kan därför ändå tillåta ett för stort minimisteg. Den nya genomförbarhetskontrollen får aldrig överskrida det verkliga effekt-tids-utrymmet för att nå närmaste SoC-nivå; använd en konservativ representerbar övergång eller redovisa begränsningen. Testa detta uttryckligen vid 06:59.

Klar till avresa (`deadline`) fyller genomförbara billiga fönster men tidigarelägger laddning om tidsmarginalen minskar; när det krävs för 80 %-målet får nätköpspriset bli hur högt som helst. Ladda ekonomiskt (`economy`) har **ingen tidsdeadline**, väljer bara relativa billiga fönster (eventuellt med ett explicit absolut köppristak) och kan vänta på obestämd tid. Solel använder verkligt överskott efter husets batteriprioritet och hysteresis, utan att maskera husbatteriurladdning som sol. Överskottsregulatorn måste inkludera redan pågående EV-effekt och reserverad batteriladdning i sin balans, annars skapas en start-/stopp-loop. Bilen har samma 80 %-mål i båda lägena; skuggkoden stoppar förslag men ingen fysisk laddning; ett avancerat manuellt läge får inte kringgå säkerhetsgränserna. Paus tillåter inga nya laddstarter. Snabba säkerhetsminskningar får alltid bryta minsta laddtid; ekonomiska småändringar får inte orsaka kontinuerliga kontaktoromkopplingar.

## 6. Reservbeteende A — saknad Volvo-SoC

Den gamla formuleringen ”välj de billigaste intervallen” var ofullständig när mängden energi var okänd. En kostnadsminimerare utan energikrav kan välja att inte ladda alls. Följande reservkontrakt ersätter den.

### 6.1 Energibehov med uttrycklig osäkerhet

Om SoC var tillförlitlig vid samma obrutna anslutning används den tillsammans med lokal tillförd energi och konservativa förlust-/osäkerhetsgränser för att beräkna ett intervall för återstående behov. Gamla data från före en möjlig körning får inte användas som om bilen fortfarande hade samma laddnivå.

Saknas användbar ankarepunkt används ett konservativt **behovstak** från tomt batteri till det lokala målet, med verifierad kapacitet och en uttrycklig AC-/förlustmodell. Detta är en planeringsgräns, inte en påhittad observation ”batteri 0 %”. Vald energi blir det som ryms av detta behov i säkra laddfönster fram till låst avresetid. Om behovstaket överstiger tillgänglig kapacitet kan hela återstående tiden behövas, även dyrare intervall. Appen visar då att tidsmålet och lägsta pris inte kan garanteras samtidigt.

Reservbudgeten lagras per ladduppdrag och förbrukas bara av känd leverans med motsvarande mätkvalitet. Den får **inte nollställas eller återfyllas till ett helt batteribehov vid varje omplanering, timeout eller omstart**. När ny SoC återkommer förankras behovet vid dess observationstid; lägg bara till senare energileverans, inte hela sessionens integral en gång till. Okänd hjälpförbrukning och mätluckor bevaras som osäkerhet, inte som säkert tillförd batterienergi.

Saknas även verifierad kapacitet används ett begränsat laddmöjlighetsläge fram till avresetiden: utnyttja säkra tillfällen, låt den lokala bilgränsen begränsa laddningen och redovisa okänt kvarvarande behov. Ingen exakt sluttid eller besparing ska visas. Prisdata får styra val där det faktiskt finns tidsmarginal, men systemet ska inte skjuta upp all laddning till ett otillräckligt kort nattfönster.

### 6.2 Villkor och återhämtning

Reserv A kräver fysisk anslutning, fungerande/verifierad EVCC-styrning, tillämplig säker fasbudget, batteriskydd och en läst eller tidigare bekräftad lokal laddgräns som matchar appmålet. Engångsbekräftelse accepteras vid molnbortfall; inga nya nattliga bekräftelser krävs. En känd målkonflikt stoppar användning av just denna procentbaserade reservplan och förklaras tydligt.

Spara `soc_quality=unavailable/estimated`, `remaining_energy_upper_kwh`, `plan_confidence` och `target_reached=unknown`. Lägre ström väljs bara när installation eller ekonomi motiverar den — inte mekaniskt 6 A så att bilen riskerar att missa avresan. Bilen kan konsumera AC-energi för klimatisering utan motsvarande ökning av SoC; lokal 80 %-gräns innebär inte att all AC-förbrukning upphör.

När färska bilvärden återkommer omplaneras samma session. Redan levererad energi behålls och räknas inte på nytt från anslutningen. SoC-förlust orsakar en notifiering per felperiod, inte en per poll. Om HA själv är nere ligger varningen kvar lokalt och levereras senare eller via befintlig oberoende notifieringsväg; notifieringsfel får inte stoppa säkerhetsåtgärder.

| Bortfall | Beteende |
|---|---|
| Endast Volvo-SoC | Reserv A enligt ovan. |
| Aktuella priser saknas | Spara giltiga priser för exakt samma UTC-intervall; använd prognos tydligt märkt, annars tids-/behovsstyrd reservladdning utan billigast-löfte. Aldrig gårdagens priser som dagens fakta. |
| EV-effektmätning saknas men säkert styr-/fasstöd finns | Laddning kan fortsätta inom godkänd statisk budget; energi/kostnad blir ofullständig. Kräver att batteriskyddet kan upprätthållas konservativt utan mätvärdet. |
| Fasbudget eller batteriskydd kan inte upprätthållas | Reducera/pausa enligt verifierad installationsreserv, annars pausa. Använd inte SoC-reserv A som generell felsäker reserv för alla datakällor. |
| EVCC/nätkontakt förloras | Begärt stopp är inte bekräftat stopp. Enhetslokalt felsäkert beteende måste bära bortfallet. |

## 7. Kontroll, watchdog och felsäkra lägen

### 7.1 Ingen implicit skrivning vid läsning

Drivrutinen byggs från ABL:s officiella protokoll, inte genom att kopiera en godtycklig tredjepartsdrivrutin. Den tidigare refererade filen `evcc-io/evcc/charger/abl.go` har en uttrycklig icke-MIT-licensmarkering [S5]. Dess automatiska återaktivering vid statusläsning och dubbla skrivförsök ska inte okritiskt övertas.

Läsning får inga konfigurations-/återställningsbiverkningar. Endast explicit godkända strömbörvärden får skrivas rutinmässigt. Enhets-ID, setup, RCD-test och generella resetkommandon hör inte till normal laddstyrning. **Det separata, användargodkända 16→10→16 A-skrivprovet 2026-10-21 är genomfört och bekräftat i 2.2.1; denna dokumentrevision skickar inga nya kommandon.**

Protokollavkodning ska validera avsändare, funktion, registeridentifiering, datalängd, kontrollsumma och förväntad begäran. Hantera delade/sammanslagna TCP-paket, buffertgränser, sena svar och återanslutning. Timeout följd av sent svar får inte kvittera en annan skrivning. Efter osäkert skrivresultat: kontrollera verkligt tillstånd innan nya höjningar; upprepa inte setup/reset blint. Behåll mätmarkörer som okända, även om UI annars gärna visar noll.

### 7.1.1 Skrivkontrakt efter fältprovet (inte en implementation)

- **Protokoll:** FC10, startregister `0x0014`, antal `0x0001`, byteantal `0x02`, råbörvärde i två byte, ASCII `:...<LRC>\r\n`. Bevara enhetsadress 1 och Waveshares transparenta läge. ABL svarar `>011000140001DA\r\n` för en kvitterad skrivning till detta register, oavsett om värdet är `0x010B` eller `0x00A6`. Kvitto och LRC är nödvändiga men bekräftar **inte** vilket värde som slutligen gäller.
- **Readback:** FC03 `0x000F`, fem register: aktuell konfigurerad strömbegränsning i rätt statusfält (maskera relevanta bitar); därefter FC03 `0x002E`, fem register: EVCC-tillstånd, annonserad PWM och Ict1–3. **Läs inte `0x0014` med FC03 för verifiering** — direkta försök gav timeout i vår enhet. Läsprotokollet måste dokumentera offset, längd, sentinels och giltig LRC i enhetstester.
- **Säker anropsekvens:** säkerställ ensam RS485-ägare; tag färsk status med A1-väckningsretry vid behov; validera fasbudget, önskat läge, planrevision och hårdgräns **innan höjning**; journalför mål och tidigare värde; skicka börvärdet; läs tillbaka. Vid osäkert kvitto, **läs först tillbaka** eftersom en skrivning kan ha verkställts trots uteblivet svar. För samma idempotenta strömbörvärde kan begränsat identiskt återförsök accepteras efter tillståndskontroll; **upprepa aldrig reset-/setup-/state-kommandon blint**.
- **Fysisk verifiering:** vid C2 ska annonserad ström ändras nära omedelbart, men faktiskt fordonsupptag kan ligga kvar över nya 10 A under de första sekunderna (14,0 A vid ~1 s; 9,7 A vid ~6 s i provet). Använd konservativ säkerhetsmarginal, ett definierat maximalt verifieringsfönster och kvalitetsstatus `applied_pending_vehicle` kontra `observed`. Ett stillastående eller frånkopplat fordon kan inte förväntas uppvisa strömändring och ska inte ge falsk kvittens `physically_verified`.
- **Armering och återgång:** separat `EVCC_CONTROL_ARMED` (default av) och verifierad installationsgräns krävs innan framtida automatiska skrivningar. Kontrollerade tester måste alltid spara **exakt ursprungligt råvärde** och ha en `finally`-återställning med avgränsade omförsök och **två oberoende registerkontroller**; visa `restore_unverified` om de misslyckas. Återställning till tidigare `0x010B` betyder inte att autonom 16,02 A tillåts när hårdgränsen senare sätts till 16,00 A. Driftplaner ska följa säker aktuell budget, **inte automatiskt återställa till ett hårdkodat 16 A**.
- **Logg/observabilitet:** logga tid, session, generation, börvärde i A och raw, kvittensram/LRC, readback av båda registerblocken, Ict-kanal, fördröjning till fysisk ändring, återförsök samt slutlig återställningsstatus. Ett missat ACK räknas inte som missat kommando; ett ACK utan readback är inte fullbordad fysisk styrning.

### 7.2 Ett verifierat lokalt stopp vid förlorad styrning

En watchdog på samma Dell och samma Ethernetförbindelse kan inte rädda situationen när hela Dell eller nätvägen är borta. Full automatisk drift ska därför använda ett **på den installerade EVCC:n verifierat enhetslokalt timeout-/startspärrbeteende**, alternativt en annan oberoende fysisk lösning som är verifierad för samma fel. Enbart ett larm eller upprepade försök att nå en frånkopplad gateway uppfyller inte kravet.

ABL beskriver konfigurationsstöd för upstream-timeout och uppstartsblockering, men den använda API-delen ger inte en tillräcklig garanti om exakt timeouttid, vilka meddelanden som håller den vid liv, persistens eller återstart på denna firmware. Dessa egenskaper mäts en gång vid kontrollerad driftsättning och omprövas när hårdvara/firmware eller relevant konfiguration ändras. Logga råvärden före/efter och den observerade stopptiden.

Normal telemetripollning får inte av misstag hålla laddningen vid liv när planering/batterikoordinator har dött. Testa vilka trafikslag som återställer enhetens timer. Om alla giltiga läsningar återställer den måste EV-tjänsten först försöka verifierat stopp och därefter upphöra med sådan trafik när styrtillståndet är osäkert. Ingen andra agent, gateway-autopoll eller heartbeatfunktion får oavsiktligt neutralisera timeouten. **Skrivprovet verifierade inte beteendet vid bortfall.** Med 2026-10-21 avläst **upstream-timeout avstängd** kan vi inte anta att `0x00A6` eller `0x010B` automatiskt försvinner när Dell/kabel/gateway dör. Inga automatiska höjningar eller dynamisk lastbalansering får bygga på ett sådant antagande; separat felprov och säker enhets-/installationslösning krävs.

Vid återstart: återta inte gammalt laddbörvärde innan aktuell fysisk status, beständig paus, fasbudget, batteriskydd och en giltig plan stämts av. En förlust av inställnings-/sessionsdatabasen startar inte ett nytt standard-Smart med fri ström. Återhämtning efter rena kommunikationsfel kan vara automatisk efter verifierad sekvens; jordfel, svetsad kontaktor och andra elektriska fel får inte resetas i en obegränsad självläkningsloop.

### 7.3 Tre tidsnivåer och en skribent

Skilj mellan 15-minuters ekonomiska prisintervall, lokal EVCC-kontroll/pollning och husets långsammare energiuppdatering. Kontrollslingans period, timeout, tillåten ålder och driftspärr bestäms så att värsta verifierade svarstid inklusive retries ryms före den enhetslokala spärren. Använd monotona klockor för timeouts och UTC för kalender/ekonomi. Tidskraven ska mätas, inte ärvas från inverterpollerns 30 sekunder. **Fältdata kräver tillståndsmedveten kommunikation:** första läsningen i A1 efter ~10,5 s kan gå förlorad, medan första läsningarna i C2 fungerade efter minst 60 s utan polling. Det är inte skäl att slå på en generell keepalive var femte sekund eller härleda tillförlitlighet enbart från C2; prova A1, laddstartsövergång och frånkoppling med begränsat read-only retry. Normal TCP-aktivitet är ingen RS485-väckningsgaranti.

Ensam ägare av EVCC-länken upprätthålls med lokal processlåsning och en instans av systemd-tjänsten. En eventuell watchdog som själv ska ansluta måste först säkra exklusivt övertagande; den får inte bli ytterligare en samtidig RS485-master. Enhetslokal timeout är det sista stödet vid total värdförlust, medan programwatchdog kan ge snabbare felhantering och larm.

Kontrollhälsa ska betyda att nödvändiga data och giltig styravsikt finns, inte bara att processen lyckas röra en heartbeatfil. Saknad heartbeat på en tidigare aktiv installation är inte samma sak som ”har aldrig startats”. Databaslås, full disk och notifieringsfel får inte hindra sänkning/stopp. Vid övergång live till avstängt läge stoppas eller överlämnas först den fysiska kontrollen enligt verifierad procedur; att enbart sluta skriva är inte ett stopp.

### 7.4 Åtkomst

Verifiera befintligt åtkomstskydd och återanvänd autentisering vid reverse proxy där sådan finns. Anta inte att nya mutationsrutter redan är skyddade; både den publika vägen och direkt åtkomst till backend ska omfattas. GET är läsning; PATCH/POST ändrar endast validerad styravsikt. Kontrollera behörighet, ursprung/CSRF där cookies används och inställningsrevision. Lokal tjänstautentisering ska inte bero på en interaktiv browsers inloggning. Exponera inte gateway, råa Modbus-kommandon eller HA-/prisleverantörstoken för webbläsaren. Ingen ny generell säkerhetsplattform behövs.

## 8. Datamodell, API och ekonomi

### 8.1 Lagring och API

Återanvänd SQLite WAL och repots schema-/migreringsmönster. Båda språkens tabellkontrakt måste förbli överensstämmande. `deploy/schema.sql` är i dag dokumentation och tillämpas inte automatiskt; respektive process skapar sina tabeller. Nya kontrollfält behöver därför en explicit, additiv och versionskontrollerad migration som är klar innan nya läsare/skrivare aktiveras. WAL löser inte detta. Säkerhetskopiering och kompatibel återgång ingår i driftsättningsprovet. Nya tabeller/fält:

| Objekt | Minsta kontrakt |
|---|---|
| `ev_settings` | Användarval, revision och senaste bekräftade lokala bilgräns. Verifierade hårdvarugränser är läsbara från driftskonfiguration, inte fritt skrivbara här. |
| `ev_samples` | UTC-observation/hämtning, session, rå status, annonserad PWM, identifierad intern Ict-kanal (Ict2 observerad 2026-10-21), övriga faser, uppmätt ström med kvalitet, SoC och källtid. Saknade värden är NULL med orsak; hårdvarans fysiska nätfas redovisas separat tills verifierad. |
| `ev_plans` | Planversion och schema, shadow/live, input-snapshot, giltighet, kandidatval, deadline, energibehov/intervall och kopplat batteriunderlag. |
| `ev_sessions` | Stabil session-ID, fysisk anslutning/frånkoppling, laddförsök/deadlines, lägesändringar, målutfall och slutorsak. |
| `ev_energy_intervals` | Entydigt UTC-intervall, levererad energi och mätkälla, täckningsgrad, pris-/tariffversion samt allokeringskvalitet. Kan materialiseras som vy initialt om råproven räcker. |
| `ev_actions` | Begärd, kvitterad, återläst och fysiskt observerad ström/effekt som **olika fält**, råbörvärde, FC10-ACK/LRC, båda FC03-readbacks, generation, armed, retries, uppmätt responsfördröjning, återställningsresultat och stopporsak. |
| `ev_price_snapshots` | Oföränderligt normaliserat pris-/tariffunderlag med absoluta UTC-intervall, giltighet, publiceringstid och hash. Planer och energiposter refererar till dess version; befintlig dags-upsert får inte ersätta ett historiskt sessionsunderlag. |

Aktiv kopplad plan har en transaktionellt uppdaterad pekare/generation; shadow-rader får inte väljas av verkställare. Energiposter dedupliceras med session, källa och tidsintervall så att omstart inte dubbelräknar. Lång historik aggregeras till intervall för UI men bevarar revisionsunderlag för kostnader och provtäckning. Nya tabeller får inte växa med identiska omplaneringar vid varje sidvisning.

API: `GET /api/ev/status`, `/plan`, `/sessions`, `/sessions/{id}`; `PATCH /api/ev/settings`; `POST /api/ev/pause` och `/resume`. Status lämnar både önskat och faktiskt tillstånd, tidsstämplar, orsak, planrevision och datakvalitet. Mutationssvar skiljer ”avsikt sparad” från ”hårdvara bekräftad”. Fel såsom olaglig ström, ändrat mål utan motsvarande lokal gräns, gammal revision eller skrivfel ska ha tydligt svar och inte ge en optimistisk framgångsindikering.

Alla nya rutter och klientanrop måste återanvända `lib/base-path.ts` och installationens `/solinteg`-basväg. En ny fungerande API-rutt på localhost men felaktig rot-URL i den publika klienten räknas inte som levererad integration.

### 8.2 Energi: AC-leverans är inte ökning av bilens SoC

Med EVCC-ström blir momentaneffekten en uppskattning: `P_ac_est = U_assumed * I_measured * PF_assumed`. **Ict2 visade 15,5→9,7→15,5 A under det styrda laddprovet** och är vår för närvarande verifierade aktiva EVCC-mätkanal; fysisk fasidentifiering, spänningsmätning och full effektmätning saknas fortfarande. 230 V och PF 1 är antaganden, inte mätningar; 15,5 A × 230 V ≈ 3,57 kW är därför en **schablon**, inte en bekräftelse på den uppgivna cirka 4 kW-laddeffekten. Mätupplösning 0,1 A är inte angiven total mätnoggrannhet. Bilens molnrapporterade effekt är en rimlighetskontroll, inte en lokal kWh-räknare.

Integrera tidsstämplad effekt över faktisk varaktighet, dela intervall vid prisgränser och behåll värden på båda sidor när linjär approximation används. Klipp också vid bekräftad laddstart, paus och frånkoppling; linjär interpolation mellan prover får inte fabricera förbrukning över en känd avstängd period. Modell-/mätfel och täckningsgrad följer med resultatet. Ingen integration av gamla strömvärden genom en lång lucka; ingen PWM-gräns som mätvärde. Ett intervall kan ha delvis känd energi och okänd resterande del. Separera `measured_current_estimated_energy` från `metered_ac_energy` och från helt okänd energi.

AC-energin omfattar bilens omvandlingsförluster och eventuell klimatisering. Lägg därför inte på samma laddförlust igen när sessionens kostnad beräknas. Förlustmodell används för att översätta önskad batteriökning till väntat AC-behov, inte för att blåsa upp redan uppmätta/uppskattade AC-kWh.

När EM111 senare kan läsas ersätter dess räknardifferens energitotalen efter kontroll av enhet, återställning, rullning och tidsgränser. Räknaren ensam kan inte fördela en lång kommunikationsluckas energi exakt över olika elpriser. Behåll då lägre prisallokeringskvalitet även om totala kWh är kända. Ny mätkälla aktiveras med revisionsgräns, utan att tyst skriva om gammal uppskattning till uppmätt fakta.

### 8.3 Prisunderlag och tariff

Återanvänd `lib/prices.ts` och dess aktuella SE2-priskälla. Tibber-API är inte ett krav för första leveransen; det viktiga är att priset matchar det faktiska avtalet. Nuvarande kod bygger inköpspris som `(spot + påslag) * (1 + moms) + skatt/överföring`, med den sistnämnda parametern redan på konsumentbasis. En alternativ leverantörsprisadapter måste deklarera vad som redan ingår, så moms/skatt inte adderas två gånger.

Tibbers publicerade avtalsvillkor skiljer fast påslag från rörliga inköpskostnader [S7]. Det aktiva värdet 9,29 öre/kWh får därför inte utan avstämning betraktas som ett permanent exakt avtalspris. För historisk kostnad ska slutliga prisdelar hämtas från tillämpligt avtal/fakturaunderlag och avstämningen versionslagras; tills dess märks kostnaden som preliminär. Publika villkor bevisar inte en enskild kunds rabatter eller fakturerade värden.

Sundsvall Elnäts publicerade privatkundstariff för 2026 anger 26,3 öre/kWh överföring och 45,0 öre/kWh energiskatt, båda inklusive moms, alltså 71,30 tillsammans [S4]. Aktiv konfiguration är 71,38. Differensen 0,08 öre/kWh ska avstämmas mot faktisk faktura/precision; inga driftvärden ändras i denna dokumentgranskning. Den publicerade säkringstariffen redovisar fasta säkringsavgifter och energiavgift, inte en separat effektavgift. Verifiera kundens tariff innan ekonomin markeras som avtalsavstämd.

Lagra pris- och tariffversion med giltighetsdatum, inte bara aktuell env-konstant. Senare ändrat påslag eller nätavgift får inte skriva om historiken med dagens villkor. Daterade publicerade priser för samma UTC-intervall behåller sin giltighet vid tillfälligt nätfel; prognoser och priser för andra dagar gör det inte. Fasta abonnemangs-/säkringsavgifter är inte marginalkostnad per laddsession. Det går att visa separat schablonallokering, men den får inte marknadsföras som en besparing automatiken orsakat.

### 8.4 Tre ekonomiska mått, inte ”verklig” kontrafaktisk kostnad

1. **Beräknad sessionskostnad:** energin allokeras med en dokumenterad balansmodell mellan nät, egen sol, husbatteri och okänd källa. Nät värderas till avtalsenligt inköp; solens alternativkostnad kan vara utebliven nettoexport; batteriets värde inkluderar vald lager-/förlustmodell. Källfördelning är en uppskattning på gemensam AC-skena. Summerad tilldelning får inte överstiga tillgänglig energi och ska stämma med totalen inom angiven osäkerhet. Okänt förblir okänt.
2. **Modellberäknad merkostnad för hela fastigheten:** jämför likvärdig husdrift med respektive utan EV över samma horisont, startläge och värde på kvarvarande batterienergi. Detta är en kontrafaktisk modell, inte en direkt mätning eller fakturapost. Den tidigare benämningen ”verklig merkostnad” tas bort.
3. **Beräknad besparing mot laddning direkt:** jämför med en vid anslutning definierad genomförbar direktladdningsreferens, samma mål/levererad energi, fasbudget, batteripolicy och avresetid. Spara referensens version. Visa inte en positiv besparing genom att leverera färre kWh, tömma husbatteriet mer eller använda framtida information bara i ena scenariot. Negativ besparing är tillåten och ska visas.

`EV-kWh * inköpspris` kan visas som en enkel **nätprisequivalent**, men är inte faktisk sessionskostnad när egen sol eller husbatteri bidragit. Varken solens uteblivna försäljning eller batterislitage får läggas till en gång till ovanpå en helfastighetsmodell som redan omfattar dem. Visa energitäckning, pristäckning och allokeringskvalitet separat; EM111 gör inte alla tre automatiskt exakta.

## 9. GUI

`EvChargingCard` integreras i befintlig huvuddashboard, med automatisk Smart-profil 80 %/07:00. Visa fysisk status, bil-SoC och dess källtid, valt läge, låst avresetid, annonserad respektive uppmätt ström, uppskattad effekt, sessionens kWh, kostnad och kvalitet. Planen visas tillsammans med pris och husbatteri utan att förväxla planerade och utförda värden.

Förklara varför bilen väntar: billigare intervall, fasbudget, husbatteriskydd, bilens egen paus, nått mål, reserv A eller kommunikationsfel. Ett ogiltigt mätvärde blir ”saknas”, inte 0 A/0 kr/0 %. Visa exempelvis ”SoC saknas – reservladdning, lokal bilgräns antagen 80 %” i stället för falsk exakt sluttid. Om mål inte ryms visas förväntat underskott eller osäkerhet före avresan.

Historik: per session och 24 h/7 d/30 d, med pris-/energifördelning, bekräftade styrhändelser och pausorsaker. UI behöver inte vara öppet för att planering och laddning ska fortsätta. Läsrendering får inte skapa ytterligare styrägare. Nya laddmål som inte matchar bilens lokala gräns ger en tydlig varning i samma flöde, inte ett dolt löfte om att appen redan ändrat bilen.

## 10. Verifiering och leveransgränser

### 10.1 Tester som följer av granskningen

**Fältstatus efter 2026-10-21:** FC03 och FC10 under C2, readback och faktisk 16→10→16 A-respons är **verifierade**. FC03 direkt mot `0x0014` är **inte fungerande på vår enhet** och måste ersättas av `0x000F` + `0x002E`. A1-vilotimeout och C2 utan timeout efter 20/40/60 s är **observerade** men inte isolerade till en komponent. Stoppa/pausa via `0x03E8`, kontrollförlust, återstart och lastbalansering mot fysisk fas är **inte ännu verifierade**.

| Test | Förväntat resultat |
|---|---|
| EV shadow medan `SOLINTEG_CONTROL_ARMED=1` | Inga hypotetiska EV-/batterikandidater når levande dispatch; inga EV-skrivningar. |
| Samtidig render, replan och ändrat laddmål | Bara en aktuell session-/inställningsrevision blir aktiv, med samma batteri-/EV-generation. |
| SQLite låst/full, omstart under planbyte | Inget falskt publiceringskvitto, inga dubbla sessions-kWh, stoppskydd fungerar utan loggskrivning. |
| EV-last 4 kWh och hushållsmarginal 1,15 | EV förblir 4 kWh; marginal och hushållskvantiler appliceras inte på EV. |
| Historisk laddning i `house_load_w` | EV dras av före baslast-/reservinlärning och läggs tillbaka exakt en gång. |
| EV startar över planerad effekt | Batteriets live-reglering får inte börja försörja EV eller överskrida skyddad baslast. |
| Husbatteriet försörjer bara husets underskott | EV stoppas inte bara för att `battery_w` är positivt. |
| En fas överbelastas medan summan är låg | Verifierad fasbegränsning gäller ändå; 10/10,5 kW används inte som enda skydd. |
| Sen anslutning, stor batteribrist | Genomförbarhetskontroll visar underskott, prioriterar säker tillförsel och lovar inte 80 %. |
| SoC saknas helt / försvinner / återkommer | Reserv A har ett definierat energibehovstak eller laddmöjlighetsläge; inga påhittade SoC-värden, ingen energidubbelräkning. |
| Lokal bilgräns känd som 90 % men appmål 80 % | Känd konflikt ignoreras inte av en gammal 80 %-bekräftelse. |
| Start 06:59 och DST-dygn 92/100 intervall | Endast verkligt kvarvarande energiutrymme; höstens båda 02:15 har olika identitet/pris. |
| Negativa priser eller saknade framtidspriser | Fullt inköpspris används; ingen plan kräver en obevisad PV-begränsning. |
| Råström `0x03E8`, kompakt `0x64`, fel LRC, kort svar | Okänt/ogiltigt, aldrig 100 A, uppmätt noll eller kostnadsgrundande PWM. |
| Strömbörvärde 16→10→16 A (verifierat 2026-10-21) | FC10 `0x0014` kvitteras, `0x000F` och `0x002E` överensstämmer, Ict2 följer och ursprungligt råvärde återställs. **Separat paus-/stopptest kvarstår.** |
| Processkrasch, död koordinator, kabelbrott, förlorad Dell, gateway-/EVCC-omstart | Lokalt verifierad stopptid och startspärr; läspollning får inte maskera död styrning. |
| Elektriskt fel jämfört med rent kommunikationsfel | Ingen blind elektrisk reset; endast verifierad kommunikationsåterhämtning får automatiseras. |
| Effektprov över prisgräns och lång datalucka | Rätt tidsfördelning och tydlig täckning, ingen extrapolering till påhittad faktura. |
| Byte av tariff/mätkälla och jämförelse mot direktladdning | Historik versionsbevaras; samma energi/terminalvärde jämförs, även negativ besparing visas. |
| A1 utan bil, >10 s vilande RS485 (tidigare observerat) | Första läsningen kan saknas; en begränsad **identisk read-only retry** ger giltig ny FC03-respons. Ingen dold ändring av laddstatus vid läsning. |
| C2 aktiv laddning efter 20/40/60 s utan förfrågningar (observerat 2026-10-21) | Första förfrågan var giltig i varje fältprov. Testa om beteendet upprepas; avsaknad av timeout är **inte** ett stopp-/watchdogbevis. |
| FC03 `0x0014` ger timeout men FC03 `0x000F` och `0x002E` fungerar | Drivrutinen verifierar börvärde genom de båda bekräftade blocken; okända/långsamma svar leder inte till falsk återställning eller höjning. |
| FC10-ACK saknas men kommandot har tillämpats, sent svar och delat TCP-paket | Först readback av aktuellt värde; korrekt korrelation/LRC, inga oavsiktligt upprepade höjningar eller fel kvittens av annat kommando. |
| Ned-/uppreglering under C2, även bil som reagerar långsamt | Logga `ACK` → `readback` → `Ict2 observed` med tidsstämplar; fältprovets ~6 s används som referens, inte som fast garanti. |
| A1→B1→C2→A1, lossad kabel, fel/paus och ny bil | Ingen felklassning av vilotimeout som frånkoppling; ny session bara vid verklig kontaktförändring; status går före gammalt börvärde. |
| Avbruten klient, bruten Ethernetkabel, förlorad Dell och återstart **under reducerad ström** | Dokumentera om EVCC behåller eller ändrar senaste börvärde, samt faktisk stopptid och återstartsspärr. **Upstream-timeout är nu konstaterat AV**; inget tryggt stopp får antas. |
| 20 A huvudsäkring och enfasig laddning med andra laster | Kartlägg fysisk fas och verifiera tidskrav/reserv med lokal strömmätning eller statiskt dimensionerad budget. Ict2 är inte automatiskt byggnadens L2. |
| Fel mitt i testsekvensen efter lyckad skrivning | `finally`-väg försöker återställa tidigare råvärde, bekräftar med två statusblock och visar uttryckligt `restore_unverified` vid utebliven bekräftelse. |

Automatiserade tester ska täcka TypeScript- och Pythonkontrakt samt tillståndsmaskinen utan nät-/registerskrivning. Vid senare produktändringar körs `npm test`, `npm run test:py`, `npm run build` och relevanta schema-/tidskontraktstester enligt repots instruktioner. Denna revision avser dokumentet; dessa testfall är krav, inte påståenden om redan genomförd hårdvaruverifiering.

### 10.2 Leveranser och tydliga aktiveringsvillkor

1. **Read-only telemetri och sessioner:** lokal EVCC-avläsning, verkliga HA-entiteter, beständig session och diagnos av mätkvalitet. Ingen kontroll, ingen påverkan på exekverbara batteriplaner.
2. **Energi och prislogg:** uppskattade AC-kWh med täckning, avtalsversionsmodell och sessionshistorik. Full kontrafaktisk besparingsanalys kan vänta; enkel nätprisequivalent måste märkas korrekt.
3. **Samordnad simulering:** separerade laster, batteriskydd, UTC/delintervall och jämförbara kandidatplaner; replay på verklig historik. Verifiera särskilt att dagens redan aktiva batteristyrning inte påverkas av simuleringen.
4. **Kontrollerad driftsättning:** tre avgränsade förmågor: (a) **börvärdesskrivning/readback och fysisk respons är nu fältverifierade i C2**, men stopp/paus, kommunikationsförlust, enhetslokal timeout och uppstart på denna firmware återstår; (b) verklig 20 A-fasbudget och samordnat batteriskydd; (c) datakällor/lokal bilgräns och omstarts-/publiceringskontrakt. Tidigare godkända delprov ska inte göras om bara för att dokumentationen utökas; osäkra eller nya driftlägen kräver ett eget prov.
5. **Automatisk drift:** en explicit lokal aktivering efter godkända förmågor; därefter helt automatisk Smart-laddning enligt beslutade standarder. Tydlig paus och verifierad återgång, inte upprepade godkännanden per session.
6. **EM111 vid behov:** bättre AC-energimätning via separat mätbuss utan att ändra laddlägen eller schemaläggningskontrakt.

Read-only och simulering kan levereras utan att alla livevillkor redan är uppfyllda. Varje kontrollkrav ska ha ett dokumenterat prov och utfall, inte skapa en ny kedja av generella gransknings-/återhämtningsärenden. Förlorad historik eller exakta prisvillkor begränsar ekonomiska påståenden; det ska inte i sig hindra övrig säker read-only-funktion.

## 11. Kända risker och rättningar i revision 2–3

| Allvar | Brist i utkastet | Införd rättning |
|---|---|---|
| Kritisk | EVCC-timeout antogs kunna lösas med programwatchdog; äldre konfigurationssvar hade timeout av. | Enhetslokalt felsäkert beteende och uppstartsprov, skild liveness för plan och transport. |
| Kritisk | EV-shadow kunde bli exekverbart underlag för redan armerad batteridispatch. | Compute/publish-separation, shadow-isolering och verifierad planaktivering. |
| Hög | Batteriskydd var ett mål men inte ett realiserat DP-/verkställarkontrakt. | Separat last, skyddsgräns i optimering och live-reglering samt ordnad fysisk övergång. |
| Hög | Full freeze beskrevs som enbart overifierad trots dokumenterat misslyckande. | Lokalt negativt prov skiljs från upstream-soft-hold; ingen implicit aktivering. |
| Hög | Okänd SoC utan definierad laddmängd gav ingen körbar reservoptimering. | Behovsintervall, konservativt behovstak och laddmöjlighetsläge med ärlig osäkerhet. |
| Hög | Total nät-kW och långsam telemetri kunde uppfattas som säkert enfasskydd. | Separat verifierad fasbudget, statisk säker reserv eller lokal lastbalansering. |
| Hög | Naiva tider och helt första 15-minutersintervall kunde ge fel pris eller falsk deadlinekapacitet. | Absoluta UTC-intervall och gemensam varaktighet genom TS/Python. |
| Hög | Skrivfunktion, PWM-kodning, paus och ogiltiga mätmarkörer var ospecificerade. | Explicit ABL-kontrakt och separata begärda/annonserade/uppmätta värden. |
| Medel | Referensinställningar blandades ihop med användarens aktiva anläggning. | Verifierad processkonfiguration och rätt inverter/gateway-adresser. |
| Medel | ”Verklig merkostnad” och ”billigast” överdrev vad modellen/mätningen kan bevisa. | Märkt kontrafaktisk merkostnad, genomförbar jämförelse och tre kvalitetsmått. |
| Medel | HA-kopplingen var abstrakt trots befintliga entiteter, och API-version/färskhet saknades. | Återanvänd konfigurerade entiteter, validera kapabilitet/källtid och undvik noll-default. |
| Medel | Hushållsmarginal och historisk EV-last riskerade att räknas dubbelt. | Baslast före träning och separat marginalfri planerad EV-last. |
| Medel | Framtida effekttariff, licens och antagen PV-begränsning kunde bli obefogade beroenden. | Faktiskt tariffunderlag, självständig drivrutin och verifierade fysiska förmågor. |

### 11.1 Revision 3 — nytt verifierat respektive kvarstående

| Status | Observation eller beslut | Praktisk påverkan på kommande implementation |
|---|---|---|
| **Verifierat** | FC10 `0x0014`, PWM 267→166→267, FC03 readback via `0x000F` och `0x002E` samt fysisk strömrespons. | Implementera den bevisade vägen. Undvik en ny generell Modbus-probe- eller drivrutinsutredning innan funktionerna byggs. |
| **Verifierat negativt** | FC03 direkt mot `0x0014` gav två timeouts. | Förbjud denna läsning som bekräftelse; använd statusblocken. |
| **Observerat, tolkning öppen** | A1 tappar ofta första request efter ~10,5 s; C2 svarade direkt även efter 60 s. | Tillståndsmedveten read-only retry. Ingen obetingad periodisk 5-sekunderspoll som samtidigt riskerar att påverka en framtida watchdog. |
| **Kritiskt kvar** | Upstream-timeout av; ingen förlust-/stopptest under laddning. | Fördröj autonom dynamisk styrning tills enhetslokalt/oberoende stoppskydd bevisats. Read-only/shadow får gå vidare. |
| **Högt kvar** | Endast Ict2 internt verifierad; användaruppgiven 20 A huvudsäkring, okända samtidiga faslaster. | Inför inte automatiskt 16 A som säker standardström och använd inte aggregerade kW som ensam fastighetsgräns. |
| **Verifierat, begränsad mängd** | Fordonets ström ändrades på ~6 s under detta 30-sekundersprov. | Definiera verifieringsfönster, mät mönstret på fler sessioner och skilj ACK/readback/fysisk respons. |

Kvarstående fysiska osäkerheter är **konkreta aktiveringsvillkor** snarare än skäl att skapa en öppen följd av extra utredningsärenden. Ett redan avslutat delprov dokumenteras och accepteras. Elektriska skydd, batteriskydd och stoppbeteende måste fortfarande verifieras innan liveautonomi. **Ingen jordning, säkring, RCD, inverterregister, produktionskod eller gatewayinställning ändras genom denna dokumentgranskning.**

## 12. Etappindelad implementation, acceptanskriterier och progresslogg

**Beslut 2026-10-24, reviderat 2026-10-26:** Åtta isolerade, var för sig driftsättbara etapper. Varje etapp ska kunna byggas, testas, driftsättas och återställas utan att ändra befintlig batteristyrning av misstag. Etappindelningen bygger på den tidigare planen i `91c2802`; revision 4 bevarar dess säkerhetsvillkor men skiljer den närmast aktuella läs-/skuggvägen från framtida fysisk styrning.

**Aktuell genomförandegräns:** Prioritera read-only Home Assistant/Volvo (`charging_status`, `charging_power`, `last_reported`) tillsammans med Solintegs tidsstämplade L3-mätning och `battery_w`. Undersök först hur husbatteriet kan anpassas till bilens last på L3 **utan Waveshare/EVCC som förutsättning**. Bilens befintliga smartladdning kan behållas; L3-anpassning av husbatteriet är **inte ett överlastskydd** för bil, spis eller andra laster. Den ursprungliga EVCC-styrda prisplanen är en **separat möjlig framtida väg**, inte automatiskt vald arkitektur.

**Armeringsgräns:** Dokumentation, telemetry, GUI och shadow/replay ger inte rätt att skicka kommandon till inverter, bil eller laddbox. Alla fysiska skrivprov och varje skarp aktivering kräver uttryckligt separat godkännande och tillämpliga säkerhetsprov. Tidigare godkända enstaka fältprov ger inte generell framtida skrivbehörighet.

### 12.1 Gemensamma leveransregler

- **Branch och avgränsning:** en separat, begränsad commit per etapp på `Sundsvall`; `main` lämnas för upstream. Ingen orelaterad refaktorering och ingen implicit push/deploy.
- **Oberoende spärrar:** `EV_ENABLED=false` som framtida standard för EV-funktioner och `EVCC_CONTROL_ARMED=0` som separat spärr för *alla* EVCC-skrivningar. Läs-/shadow-läge får inte kunna aktivera EVCC- eller inverterkommandon via sidrendering, omplanering eller notifiering. Även eventuell batterisidig L3-aktivering måste ha en egen explicit spärr.
- **Isolering:** hypotetiska EV-planer ska aldrig publiceras till skarpa `optimizer_runs` eller annan källa för den redan aktiva batteridispatchern. En read-only HA-adapter ska inte äga eller styra Volvon. Högst en ägare av den eventuella EVCC-länken.
- **Före deploy:** verifiera feature flags, schema, testsuite, byggbarhet, aktuella batterigränser, säkerhetskopiering och reproducerbar rollback. Kör TS-/Python-/integrationstester relevanta för etappen, med dokumenterat stopp vid saknade säkerhetsprov.
- **Efter deploy:** jämför servicehälsa, loggar, datatider, plan och faktisk batteridispatch före/efter. Verifiera att inga nya fysiska kommandon skickas i read-only/shadow och att UI/API fungerar även vid databortfall.
- **Rollback:** återgå till tidigare fungerande release och verifiera batteridriften. Kodrollback är **inte** bevis för att laddboxens tidigare börvärde har återställts; vid fysisk styrning måste läsback och verkligt säkert hårdvarutillstånd verifieras oberoende.
- **Stop/go och spårbarhet:** alla acceptanskriterier, källor/mätvärden, commit, deploy, testresultat, hinder, rollback och beslut loggas i 12.3. Endast `Verifierad` kan godkänna nästa beroende; en rapporterad observation utan acceptansunderlag är inte ett klart steg.

### 12.2 Åtta driftsättbara etapper

#### Etapp 1 – Isolerad EV-grundstruktur

**Leverans:** EV-typer, revisionshanterad konfiguration, avstängda feature flags och additiva/idempotenta SQLite-migrationer. Ingen ny aktiv styrprocess.

**Acceptanskriterier:**
1. `EV_ENABLED=false` och `EVCC_CONTROL_ARMED=0` medför inga EVCC-skrivningar eller EVCC-uppkopplingar och påverkar inte inverter-dispatch.
2. Nya tabeller, migrering och start-/stoppförlopp fungerar med befintlig telemetridatabas utan historikförlust.
3. Testsuite, build, skarp smoke-test och återgång till tidigare release är verifierade.

**Deploy:** funktionaliteten avstängd; endast schema och kompatibilitet.

#### Etapp 2 – Read-only fas- och EV-telemetri

**Leverans:** läsning av befintliga Solinteg-fasmätvärden med källtid/färskhet. EVCC/Modbus ASCII-lässervice är en **valfri senare komplettering**, inte ett beroende för första read-only-testet med HA/Volvo.

**Acceptanskriterier:**
1. Färska `meter_l1_w`, `meter_l2_w`, `meter_l3_w` och `battery_w` kan observeras och jämföras med kända laststeg; saknade/stale värden är okända, inte noll.
2. Mätkedjans register, fasordning, CT-placering, samplingsintervall och fördröjning dokumenteras. Nuvarande cirka 30 sekunders **netto-W** är bara analysdata, **inte verifierad RMS-ström eller snabbt huvudsäkringsskydd**.
3. Vid önskemål om säker snabb EV-lastbalansering krävs en verifierad, tillräckligt snabb mätning av faktisk fasström och spänning (alternativt separat trefasmätare), plus felbeteende. Avsaknad blockerar EVCC-styrning men inte shadow.
4. Om EVCC-läsning senare läggs till: en ensam länkägare, FC03 med kontrollerat retry, tydlig mätkvalitet, ingen FC10 eller dold återställning.

**Deploy:** read-only, alla skrivspärrar av.

#### Etapp 3 – Volvo/HA-adapter med färskhetskontroll

**Leverans:** read-only `battery/SoC`, `charging_status`, `charging_power`, `last_reported` och önskat/lokalt laddmål genom Home Assistant när respektive källa stöder det. Förbered reserv A utan styrning.

**Acceptanskriterier:**
1. Enhet, källtid, inläsningstid och faktiskt uppdateringsintervall verifieras mot HA; två minuter är dokumenterad normaluppdatering, inte garanti för att varje entitet är färsk.
2. Saknad, oförändrad och återkommen data visas korrekt; inget saknat SoC/effektvärde ersätts med 0 eller behandlas som en ny observation.
3. Tokens hålls lokalt, inga Volvo-/EVCC-/inverterskrivningar utförs och fel i HA kan inte påverka batteriets normala dispatch.

**Deploy:** enbart läsning och skugginsamling.

#### Etapp 4 – Laddningskort och sessionshistorik

**Leverans:** mobilanpassad EV-panel, read-only status-/sessions-API samt historik 24 h/7 d/30 d med källa, kvalitet och uppskattningsmetod.

**Acceptanskriterier:**
1. Panel och API fungerar under rätt basväg, utan omladdningsloop eller regression för befintliga kort.
2. Anslutning, laddstart/-stopp, återanslutning och mätluckor bildar begripliga sessioner; `measured/estimated/stale/unavailable` hålls isär.
3. PWM, molnrapporterad effekt och uppskattad kWh blandas inte ihop med energimätare. Okänd L3-ström redovisas som okänd.
4. Inga GUI- eller API-åtgärder kan styra laddbox eller inverter i etappen.

**Deploy:** read-only dashboard; rollback av panelen påverkar inte styrningen.

#### Etapp 5 – Prisbaserad EV-planering i shadow mode

**Leverans:** separat simulerad laddplan för 80 % före 07:00, priszon SE2, avresetid, laddgränser och reserv A. Ingen EV-plan exekveras.

**Acceptanskriterier:**
1. UTC-intervall, DST (92/96/100 slots), första delintervall och en deadline 06:59 behandlas utan överskattad energi.
2. Full bil, för kort laddfönster, saknad/stale SoC och prisbortfall ger konservativ och begriplig plan; ingen påhittad garanti eller besparing.
3. Simulera L3-laststeg, paus och föråldrad fasdata; inga simulerade gränser används som fysisk säkringsgaranti.
4. Planen loggas enbart som `shadow`. Inga skarpa `optimizer_runs`, inverterskrivningar, bilkommandon eller EVCC-skrivningar.

**Deploy:** simulering; verifierad oförändrad batteridispatch.

#### Etapp 6 – Samordnad EV-/batterioptimering i shadow mode

**Leverans:** jämför EV-kandidater och husbatteriplan på samma indata och prishorisont. Utvärdera samtidigt batteriets **L3-anpassning** utan att ändra bilens eget laddschema.

**Acceptanskriterier:**
1. EV-last subtraheras från historisk baslast och läggs till exakt en gång; ekonomin använder samma riskmarginal och terminalvärdering.
2. Batteriet får i modellen täcka husets icke-EV-last men inte öka urladdningen för att försörja bilen. Simulera säkert beteende vid föråldrat L3-/Volvo-värde.
3. Gemensamma versioner, sessioner och publiceringsspärrar håller vid samtidiga planer, omstart och SQLite-fel; skarp dispatch fortsätter den gamla vägen.
4. Replay och loggar jämför planerat/verkligt `battery_w`, L3 och bilens laddstatus. Ingen otestad inverter-freeze eller bil-/laddboxstyrning införs.

**Deploy:** shadow, ingen fysisk reglering.

#### Etapp 7 – Separat godkända fysiska styrprov (villkorad)

**Leverans:** disarmed styrkod och *endast efter nytt uttryckligt godkännande* avgränsade prov för vald väg: antingen husbatteriets L3-policy eller framtida EVCC-styrning. Dessa är två skilda ställdon, med skilda tester och tillstånd.

**Acceptanskriterier:**
1. Verifiera aktuell BMS-/batterigräns, fasordning/mätkvalitet och faktisk installation; historisk nettoeffekt i W ger inte en säker amperegräns för 20 A-säkringen.
2. För EVCC: spara och läs tillbaka ursprungligt råbörvärde; godkända reduktions-/stopp- och återställningsprov måste ha verklig respons. Det tidigare **16→10→16 A-provet** är endast en verifierad delmängd.
3. Testa nät-/värdbortfall, CT-/mätbortfall, tjänsteomstart och ett **oberoende, verifierat felsäkert beteende**. EVCC:s upstream-timeout var av vid fältprovet; en samtidig mjukvaruwatchdog är inte tillräcklig.
4. Alla prov är tidsbegränsade och återgår till kontrollerat säkert läge med läsback. Ingen bestående armering, inget automatiskt återförsök efter osäkert skrivresultat.

**Deploy:** disarmed. **Blockerad för nya fysiska prov** till separat beslut och säkerhetsunderlag.

#### Etapp 8 – Övervakad pilot av uttryckligen vald styrväg

**Leverans:** tidsbegränsad, separat godkänd pilot. Förstahandsvägen är batterisidig L3-anpassning tillsammans med bilens egen smartladdning; en EVCC-styrd Smart/Solöverskott/Snabb/Pausad-pilot är ett annat, ännu ej valt alternativ.

**Acceptanskriterier:**
1. Vid batterisidig pilot ändras bara tillåten batterieffekt efter färsk L3- och BMS-data; detta **ersätter inte** laddboxens eget elektriska överlastskydd.
2. En eventuell EVCC-pilot kräver verifierad RMS-A/marginal, oberoende stopp vid tappad styrning och snabb nedreglering före prisoptimering. Netto-W vid 30 s och ett skickat stoppkommando räcker inte.
3. Fel i HA, Volvo, pris, fasdata, gateway eller DB, liksom manuell paus och omstart, har dokumenterad säker utgång utan oavsiktlig laddhöjning eller batteriurladdning.
4. Aktuell planrevision, begärd och observerad effekt, SoC/energi, beslut, larm och rollback dokumenteras; piloten avarmeras och utvärderas innan eventuell permanent drift.

**Deploy:** disarmed först; därefter endast uttryckligt godkänd fysisk pilot. **Ingen pilot aktiveras av denna revision.**

### 12.3 Progresslogg

Statusvärden: **Planerad**, **Pågår**, **Deployad – ej verifierad**, **Verifierad**, **Blockerad**, **Återställd**. Extern aktivitet eller fältunderlag är inte automatiskt en genomförd etapp; kräver acceptansbevis i detta spår.

| Etapp | Status 2026-10-26 | Kända belägg / hinder | Nästa verifiering |
|---|---|---|---|
| 1. Grundstruktur | Planerad | Inga godkända etapp-1-migrationer/flags redovisade | Schema, flaggor, regression, rollback |
| 2. Fas/EV-telemetri | Pågår – ej verifierad | Tidigare Solinteg-fasprover och EVCC-fältprov; mätfärskhet/RMS ännu inte kvalificerad | Färsk L3 + `battery_w`, CT-bortfall, källa/fas |
| 3. Volvo/HA | Pågår – ej verifierad | Read-only HA/Volvo-spår rapporterat separat; ej accepterat här | `charging_status/power/last_reported`, färskhet och bortfall |
| 4. Panel/historik | Planerad | Read-only panel och sessionskontrakt ej accepterade | Mobil, proveniens och inga mutationer |
| 5. EV-prisskugga | Planerad | Simulerad prisplan ej accepterad | DST, 07:00, reserv A och isolering |
| 6. Gemensam skugga | Planerad | L3-batteriidé och separat shadow-arbete är inte verifiering av denna etapp | Batteriskydd och isolerad kandidat-replay |
| 7. Fysiska prov | Blockerad | Delprov 2026-10-21 utfört; stoppskydd och ny skrivbehörighet saknas | Separat godkännande och oberoende failsafe |
| 8. Övervakad pilot | Blockerad | Etapp 7 och aktiveringsbeslut saknas | Säkerhetsgates, övervakad pilot, verifierad rollback |

**Händelser (append-only):**
- **2026-10-21:** avgränsat, separat godkänt EVCC-prov 16→10→16 A med verifierad respons och återställning. **Inte** godkännande av automatisk strömstyrning eller etapp 7.
- **2026-10-23:** faskarta från tidsbegränsade hushållstester indikerar att bil och största spisplattan belastar L3. Detta är inte oberoende verifierad fasström eller skyddsfunktion.
- **2026-10-24:** åttaetappsplan, acceptans/deploy/rollback och möjlighet till snabb Modbus-lastbalansering dokumenterades i `91c2802`; den senare kräver verifierad mätkedja och oberoende failsafe.
- **2026-10-24–26:** senare inriktning prioriterar husbatteriets L3-anpassning och read-only Volvo/HA utan Waveshare/EVCC som första beroende. Shadowarbete har rapporterats separat; fullständiga acceptansprotokoll har **inte** verifierats i detta dokument.
- **2026-10-26:** revision 4 återför etapperna, förtydligar det aktuella scope-beslutet och tar bort privat nätadress-/värdidentifiering. Enbart dokumentändring; inga kommandon, driftsättningar eller aktiveringar.
- **2026-10-28:** isolerad EVCC2-drivrutin, gated manuell paus/återstart och ren solar-first-skuggpolicy implementerade på `Sundsvall`. 23 riktade offlinetester passerade, inklusive råtelegram från tidigare fältprov. Ingen produktionsdeploy eller FC10. Dell når inte Waveshare (No route to host); fasström, lokalt stoppskydd och fysisk 0x03E8-paus är ännu ej verifierade. **Etapp 7/8 förblir blockerade.** Se `docs/evcc-solar-shadow.md`.

**Mall vid etappbeslut:** datum/tid (Europe/Stockholm), etapp, status, commit, miljö/deploy, tester med resultat, mät- och larmbevis, kvarstående hinder, rollbackutfall, stop/go-beslut och ansvarig verifierare.

## 13. Källor och spårbarhet

### 13.1 Kod och driftkontroll

Kodhänvisningarna avser `eedd4fe` och dess oförändrade produktkod, inte framtida implementation:
- [K1] `lib/plan.ts:93–101,162–188,191–266`: beräkning/publicering, partiellt första intervall, terminalvärde och hold-spärr.
- [K2] `lib/optimizer.ts:175–258,551–565,672–711,927–966`: gemensam last, flödesmodell, terminalvärdering och marginaler. DP:s begränsningar finns även i bakåtpasset.
- [K3] `lib/telemetry/dispatch.ts:227–324`: publiceringsspärr, best-effort-logger, deduplicering och senaste plan.
- [K4] `scripts/services/dispatch_loop.py:284–288,358–368,580–676,724–766,800–856,1110–1125,1190–1260,1475–1495`: tidsmodell, planval, batterisparning, live-reglering och Auto-återställning.
- [K5] `scripts/services/modbus_poller.py:1–41,105–135`, `deploy/schema.sql:19–42`: råa mätvärden, fas-watt, total huslast och tecken.
- [K6] `lib/prices.ts:60–74,102–115,393–432`, `lib/economics.ts:93–110`, `lib/telemetry/readings.ts:237–255`: förlorad UTC-offset, naivt nycklad prismerge redan före kostnadsberäkningen, prisnycklar och lastinlärning. `lib/optimizer.ts:527–539` visar också SoC-gridens minsta steg som måste granskas för korta delintervall.
- [K7] `scripts/services/watchdog.py:15–39,82–105,118–133`: separat process, saknad heartbeat och inverter-Auto.
- [K8] `MODBUS.md:13–68,71–99,150–185,265–330`: protokoll-/fördröjningsförutsättningar, importtak och lokalt misslyckat freeze-prov kontra annan upstream-enhet.
- [K9] HA-repots relativa fil `apps/home-assistant/ha-config/dashboards/volvo_xc40.yaml:12,165–202`: refererade Volvo-entiteter. Ingen live-läsning av bilvärden utfördes i revision 3:s granskning.
- [R1] 2026-10-19: `systemctl show` för tre Solinteg-tjänster, tillåtelselista i env-fil och `/proc/<pid>/environ`, SHA-256-jämförelse av fyra ovan angivna produktkällfiler mot `/opt/solinteg/app`. Resultatet sammanfattas i 2.1. Inga hemligheter kopierades till dokumentet.
- [H1] 2026-10-19: installation/identitetsavläsning och A1-vilotester över Dell–Waveshare–EVCC2. Den tidiga observationen 11/12 och senare 338 läsningar gäller kommunikationen, inte en garanti för skrivningar eller driftstopp. Se [H3] för tillståndsjämförelsen.
- [H2] 2026-10-21: användargodkänt skrivtest med lokal, ej versionshanterad temporär testlogg och testskript. 16→10→16 A med verifierad återställning; nyckelramar och tidsförlopp i 13.3. Temporära loggar är inte en beständig källa — tabellerna och telegrammen i denna spec är sammanfattningen.
- [H3] 2026-10-19 och 2026-10-21: A1-vilotester respektive C2-test efter 20/40/60 sekunders tystnad, med temporärt lokalt testskript och körningslogg. Tillståndsskillnaden är observerad, mekanismen inte fysiskt slutbevisad.

### 13.2 Externa primärkällor, kontrollerade 2026-10-19

- [S1] ABL, **EVCC2/3 API subset for external applications**, 05 0015 20 Rev. B, ändrad 2020-03-12. Sidor 3–6, 8–12; tabellerna har granskats visuellt. `https://www.ablmobility.de/global/downloads/anleitungen/emh1/Schnittstellenbeschreibung_Modbus-ASCII.pdf?m=1715754858`
- [S2] Waveshare, **RS485 TO ETH (B)** och **RS485 TO POE ETH (B)**, transparent överföring, gatewayfunktioner och isoleringsskillnader. `https://www.waveshare.com/wiki/RS485_TO_ETH_%28B%29` och `https://www.waveshare.com/wiki/RS485_TO_POE_ETH_%28B%29`
- [S3] Volvo Cars Developer Portal, **Energy API v2 Release Notes** samt **Introducing the next version of Energy API**. V1-avveckling och kapabilitetsbaserad läsning. `https://developer.volvocars.com/apis/energy/v2/release-notes/` och `https://developer.volvocars.com/news/energy-api-updates/`
- [S4] Sundsvall Elnät, **2026 Säkringsabonnemang Privat**, publicerade inklusive-moms-avgifter. Faktisk kundfaktura har inte granskats. `https://sundsvallelnat.se/min-anslutning/priser-och-avtalsvillkor/2026-sakringsabonnemang-privat`
- [S5] evcc-projektets egen källfil **charger/abl.go**, endast jämförelse av beteende/licensmarkering, inte kod som ska kopieras. Rörlig main/master-källa, läst vid granskningsdatum. `https://raw.githubusercontent.com/evcc-io/evcc/master/charger/abl.go`
- [S6] Home Assistant, **Volvo**, uppdateringsintervall, funktionsstöd och tvetydigt `charging_power_status=fault` när laddaren är pausad. `https://www.home-assistant.io/integrations/volvo/`
- [S7] Tibber, **Sammanfattning elavtalets villkor**, fast påslag kontra rörliga inköpskostnader. Individuell kundfaktura/rabatt har inte verifierats. `https://tibber.com/se/sammanfattning-avtalsvillkor`

**Historiskt anslutningsfel 2026-10-19:** Ett avgränsat tidigare TCP-försök till gatewayen fick timeout innan Modbus sändes. Det var ett tillfälligt fynd och är **överspelat av den fullständigt fungerande TCP-, Modbus- och belastningsstyrningen 2026-10-21**. Det äldre felet förblir spårbart men ska inte längre beskrivas som ett blockerande aktuellt anslutningshinder.

### 13.3 Verkliga råtelegram och reproducerbara fältresultat 2026-10-21 [H2–H3]

Nedan är de centrala telegrammen kopierade från testets framgångsrika meddelanden, inte konstruerade för dokumentet. Begäran `:` och svar `>` avslutas med `\r\n`; LRC i varje bekräftat svar kontrollerades i testskriptet.

| Funktion | Sänd begäran eller mottaget svar | Kommentar |
|---|---|---|
| FC10 ursprungligt börvärde | `:01100014000102010BCC` | `0x010B`, 267 PWM-tiondelar, 16,02 A. |
| FC10 reducerat börvärde | `:0110001400010200A632` | `0x00A6`, 166 PWM-tiondelar, 9,96 A. |
| FC10-svar för båda värdena | `>011000140001DA` | Kvittens om ett skrivet register på `0x0014` — inget värde i svaret. |
| FC03 `0x000F`, reducerad konfiguration | `>01030A0F3F0215010B00A600A635` | Sista två aktuella börvärdesfält visar `0x00A6`. |
| FC03 `0x002E` under reducering | `>01030A2EC290A60001006000016A` | C2; 9,96 A annonserat; Ict2 **9,6 A**. |
| FC03 `0x000F`, återställd konfiguration | `>01030A0F3F0215010B010B010B69` | Aktuella börvärdesfält `0x010B`. |
| FC03 `0x002E` efter återställning | `>01030A2EC2910B000100980001CC` | C2; 16,02 A annonserat; Ict2 **15,2 A**. |

Tidsstämplar med `Europe/Stockholm`: reduceringskommando **12:00:39**, uppmätt **9,7 A 12:00:45**, därefter 9,6–9,8 A till **12:01:10**. Återställning **12:01:10**, **13,2 A 12:01:13**, **15,2 A 12:01:16**, och **15,5 A vid separat slutkontroll**. Ingen återställningsretry behövdes; **ingen rapporterad återställningsavvikelse**. Status var C2 hela provet. Inget testades på `0x03E8`-paus eller `0x0005`-state/”WakeUp”-kommandon.

Vid nya, rent läsande prov **12:06–12:08** svarade FC03 `0x002E` på första försök efter 20, 40 och 60 sekunders inaktivitet i C2; 18,7/19,2/19,1 ms, PWM 267 och Ict2 15,3–15,5 A. Jämför A1-data [H1] där första förfrågan ofta saknades efter ~10,5 s. Avvikelserna **ska modelleras som uppmätt tillståndsbeteende med osäkerhet**, inte som bevis för tillverkarens exakta sleep-timer.

**Beslutsstatus efter revision 4:** Smart/80 %/07:00 och reserv A kvarstår som möjlig framtida EV-målbild, medan nästa leveransspår är read-only HA/Volvo och batterisidig L3-skugganalys enligt etapperna i avsnitt 12. **FC10-strömstyrning och återställning är fältverifierade, men stoppskydd och ny skrivbehörighet saknas.** Inga fysiska skrivningar, automatiska planer eller produktionsaktiveringar följer av dokumentrevisionen.
