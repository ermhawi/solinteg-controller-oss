# Smart elbilsladdning i Solinteg — designspecifikation

Status: Designspecifikation med etappindelad implementationsplan (uppdaterad 2026-10-24). Alla etapper är ännu ej påbörjade. Ingen EVCC-styrning aktiveras genom dokumentet.

## 1. Mål, beslut och avgränsning

Solinteg ska automatiskt planera laddningen när Volvo XC40 Recharge Twin Plus ansluts till DEFA eRange Uno / ABL eMH1. Planeringen ska minimera hushållets och bilens **gemensamma** förväntade elkostnad, nå önskat SoC när det är möjligt och respektera installations- och utrustningsgränser.

Användarbeslut (fastställda):
- Standardläge: **Smart**, med automatisk planering/verkställighet när kabeln ansluts, utan godkännande per session.
- Mål: **80 % bil-SoC senast 07:00** nästa relevanta morgon, tidszon Europe/Stockholm. Värdena kan ändras i GUI.
- Övriga lägen: **Solöverskott** och **Snabb** samt en tydlig **Pausad**-funktion.
- **Husbatteriet skyddas normalt** mot ytterligare urladdning för bilens skull; husets kritiska last och effektgränser har företräde.
- **Reservläge A**: Saknas färsk Volvo-SoC ska prisstyrd laddning fortsätta konservativt, under förutsättning att bilens egen laddgräns på 80 % har konfigurerats/bekräftats en gång. GUI och loggar markerar SoC/måluppfyllnad som okänd; Home Assistant får en varning.
- Alla sessioner, uppmätt/uppskattad energi, kostnad och styrbeslut ska kunna granskas i Solinteg.

Utanför första leveransen: extern energimätaranslutning på separat METER-buss; certifierad debitering; optimala effektavgifter utan verifierat tariffunderlag; V2G; ny styrning av bilens egna interna laddfunktioner; generell laddboxplattform.

## 2. Befintlig kod och installationsfakta

- Källa: grenen `Sundsvall` i solinteg-controller-oss, granskad 2026-10-19 och ren vid start. `lib/optimizer.ts` kör DP för husbatteriet med 15-minutersintervall och `consumptionKwh`. `lib/plan.ts` är enda beräkningsvägen, `lib/prices.ts` levererar prisserien; `lib/telemetry/*` och `deploy/schema.sql` använder SQLite WAL.
- `scripts/services/dispatch_loop.py` är befintlig exekvering av batteriplaner. `scripts/services/modbus_poller.py` loggar nätflöde, batteri, PV och meterdata per fas. EVCC ska **inte** skriva till samma inverterregister eller få en konkurrerande batterikontroller.
- EVCC: Waveshare RS485 TO ETH (B), tidigare bekräftad som `192.168.1.200:4196`, transparent TCP -> Modbus ASCII, EVCC 38400/8E1, adress 1. Identitet, firmware, laddstatus och intern strömmätningskapacitet har lästs; **faktisk fasström under laddning samt verklig start/stopp-funktion och fail-safe är ännu inte fullständigt verifierade; ett begränsat skrivprov av strömgräns och återställning rapporterades lyckat 2026-10-21**. IP/port och protokollparametrar ska ligga i driftskonfiguration, inte hårdkodas.
- Laddbox: DEFA/ABL eMH1, 230 V, 32 A på märkskylt, enfas. **32 A är märkdata, inte verifierad tillåten ström för installationen.** Föreslagen initial programgräns är högst 16 A och får bara användas efter verifiering av säkringar, kablage, fasval och tillgängligt fasutrymme.
- Användarens anläggning är ungefär 5,4 kWp PV, Sunwoda-batteri 15 kWh, Solinteg-växelriktare. Projektets standardvärden har också referensinstallationsvärden (bl.a. 25,6 kWh batteri och gamla elavtal). **Läs och validera aktiv driftskonfiguration**, härled inte fysisk kapacitet eller avtal från källkodens fallbacks.
- Uppgifter som måste verifieras före liveoptimering: Tibberpåslag, Sundsvall Elnäts energi-/effektavgifter och nätnytta, aktiv elområde SE2, aktuell laddeffekt/fas, Volvons användbara batterikapacitet och laddförluster.

## 3. Arkitekturbeslut

Tre möjliga upplägg: (A) fristående EV-schemaläggare = enkelt men risk för dubbelräkning och konkurrens om effekt; (B) helt omskriven gemensam DP för EV+batteri = teoretiskt stark men stor regression/komplexitet; (C) **samordnande EV-planerare runt befintlig batteri-DP** = rekommenderat.

Välj C. `lib/ev/scheduler.ts` genererar genomförbara bilscheman; varje kandidat läggs som en separat, styrbar energilast på ursprungliga huslastprognosen; befintlig `optimizeDispatch()` får bedöma batteriflöde och nätkostnad. En gemensam koordinator väljer lägsta uppnåeliga **totalkostnad**, inte lägsta isolerade EV-kostnad. En första version kan jämföra deterministiskt begränsade schemakandidater och utökas senare, utan att ändra inverterdrivrutinen.

Dataflöde:
1. EVCC-telemetri (laddstatus, uppmätt ström om stödd, satt tillåten ström, fel) via en ensam lokal Python-tjänst; bil-SoC via Home Assistant, med tidsstämpel/proveniens.
2. `lib/ev/scheduler.ts` bygger 15-minuters EV-profil i absoluta UTC-intervall, med 07:00 Europe/Stockholm som avresetid. Använd **elapsed time** och 92/96/100-slot-dygn för sommar-/vintertid; ingen indexering med lokal klocktimme.
3. `lib/plan.ts` lägger EV-profilen till en **separat** `houseConsumptionKwh` för att mata in korrekt kombinerad last i optimeringen. Logga båda komponenterna; EV laddning får aldrig ingå två gånger i framtida hushållsprognoser.
4. Koordinatorn publicerar **ett versionsmärkt par**: EV-plan + batteriplan. Python EV-tjänsten förbrukar bara en färsk plan med giltig generation och beräknad sista giltighetstid; inverter-dispatch använder samma generation. Publicering ska vara atomisk eller ha tydlig giltighetsbarriär vid separata SQLite-rader.
5. `scripts/services/evcc_service.py` är ensam ägare av Waveshare TCP/RS485-länken och serialiserar pollning och styrning. Den läser plan och hanterar EVCC-watchdog; ingen samtidig uppkopplad läsare eller webbskrivare får kapa seriell gateway.
6. Befintliga Python-tjänster fortsätter äga inverterregistret; EV-modulen kan **föreslå** replan men får inte kringgå existerande `SOLINTEG_CONTROL_ARMED` eller dess skydd.

Föreslagna moduler: `lib/ev/types.ts`, `lib/ev/scheduler.ts`, `lib/ev/economics.ts`, `lib/ev/repository.ts`, `lib/ev/vehicle-soc.ts`; `scripts/services/evcc_service.py`; `app/components/EvChargingCard.tsx`; `app/api/ev/status`, `/settings`, `/plan`, `/sessions`; `deploy/solinteg-evcc.service`. Namnen är designmål, inte befintliga filer.

## 4. Konfiguration och state

Beständiga inställningar (`ev_settings`, en aktiv rad):
- `mode`: `smart | solar | fast | paused`, default `smart`.
- `target_soc_pct = 80`, `departure_local_time = 07:00`, `timezone = Europe/Stockholm`.
- `max_current_a`: installationsverifierad konfigurerad gräns, aldrig större än vad laddbox/säkring/fas klarar; minsta praktiska pilotström kontrolleras på enheten (IEC 61851 normalt 6 A).
- `vehicle_capacity_usable_kwh`: verifierad fordonsuppgift; `charge_efficiency`: kalibrerbar uppskattning, inte dold konstant.
- `vehicle_local_limit_confirmed_pct`: engångsbekräftad gräns i bilens egen laddinställning (normalt 80 %); måste matcha aktuellt `target_soc_pct` för reservladdning utan färsk bil-SoC. Om målet ändras måste bekräftelsen uppdateras.
- `battery_ev_discharge_policy`: default `protect`; operativa gränser hämtas från verifierade inverterinställningar.
- Live-styrning armeras **endast** via separat, lokalt skyddad driftskonfiguration `EVCC_CONTROL_ARMED=1` (default `0`), aldrig genom öppet GUI/API. Användarinställningarna styr endast önskat laddläge.

Läsbar status skiljs från åtgärdsorsak: fysik (`unplugged | connected | charging | fault`), styrläge (`planned | waiting | running | paused | complete`) och datakvalitet (`measured | estimated | stale | unavailable`). Detta undviker att `soc_missing` uppträder som falskt laddboxfel.

## 5. Optimeringsmål och begränsningar

För varje 15-minutersintervall beräknas EV-effekten 0 eller inom EVCC:s verifierade laddbara strömområde. Optimera:

`sum(grid_import_kwh * buy_ore - grid_export_kwh * sell_ore + battery_wear_ore + verified_peak_tariff_delta_ore + switching_penalty_ore)`

under hårda villkor för fysisk kabelstatus, nät/fas-säkringar, aktuella strömgränser, inverter/batteri, färska styrdata och bilens målnivå där SoC är känd. Tidsfristen 07:00 är en högprioriterad constraint; om målet **inte är fysiskt möjligt**, planeras maximalt genomförbar energi, UI visar underskott och förvarning skickas.

- Hushållets grundlast är inte valbar. EV är valbar och skalbar inom verifierade gränser.
- Inverterbatteriet får normalt inte tvångsurladdas för bilen. **Auto/`idle` är inte fysisk freeze**: `solinteg-freeze-findings.md` dokumenterar att `50207/50208/50209 = 0` ännu inte är tillräckligt verifierat. EV-planering måste därför kunna sänka/pausa laddning om faktisk batteriurladdning skulle finansiera bilen. Ingen otestad freeze-registersekvens aktiveras.
- Prioritera verklig uppmätt fasbelastning och säker lastbalanserare framför ekonomisk optimering. Meter-L1/L2/L3 **aktiv effekt i watt är inte en certifierad mätning av fasström i ampere**; kontrollera fasval, spänning och mätarsäkerhet med installatör före automatisk höjning av EV-ström.
- Schema rekalkyleras på ny anslutning, ny prismängd, föråldrad bil-SoC, uppmätt lastökning, PV-avvikelse, förändrat batteri och inställningsändring. Undvik fladdrande reglering genom hysteresis och kortaste giltiga ladd-/pausperioder, kalibrerade på den verkliga enheten.
- Snabb får öka kostnaden, **aldrig** överskrida ström-/lastgränser. Solöverskott laddar bara när bekräftat verkligt överskott bär lasten och husbatteriet inte får oönskad urladdning.

## 6. Reservbeteende A — saknad Volvo-SoC

Trigger: SoC saknas, har passerat stalenessgränsen eller dess källa rapporterar fel; behåll källans senaste observation och tidsstämpel separat. Saknade värden behandlas aldrig som 0 %, 80 % eller en ny exakt mätning.

1. Om bilen är fysiskt ansluten, EVCC fungerar, säker strömgräns är verifierad **och** en gång bekräftad lokal Volvo-gräns 80 % finns: fortsätt automatisk, konservativ **prisoptimerad reservladdning** före 07:00 inom installationsgränsen; bilen själv stoppar senast vid den lokala laddgränsen.
2. Planera med okänd återstående energi och markera `target_reached = unknown`; använd billigaste tillgängliga intervallen utan att presentera exakt beräknad sluttid eller SoC. Om senast pålitligt SoC finns får det bara användas med synlig osäkerhet, inte som nytt faktum.
3. Skicka en samlad Home Assistant-notis `Bilens batteriprocent saknas – reservladdning aktiv` och visa tydlig varning på dashboarden. Fortsätt lagra faktiskt levererad kWh och pris.
4. Om lokal bilgräns inte har bekräftats, EVCC:s styrkapacitet saknas eller strömsäkerhet inte kan verifieras: gör **ingen ny oövervakad start**, indikera blockerad reservladdning. Detta är ett engångsvillkor för aktivering, inte godkännande varje natt.
5. Vid återkomst av färsk SoC: beräkna om plan utan att starta om sessionen, respektera nya mål och minimera onödiga start/stopp.

Prisdata saknas: använd endast tydligt uppmärkta senast giltiga priser eller defensivt tidsstyrt reservschema före avresetid. Om säker laddning är möjlig men prisosäkerhet råder måste UI visa att lägsta kostnad **inte** kan garanteras. Inverter-SoC eller nät/fasdata saknas: använd inte antaganden för att tömma husbatteriet eller överskrida säkringsgränsen.

## 7. Kontroll, watchdog och felsäkra lägen

- EVCC-anslutning och skrivkommandon måste testas separat: aktuellt registrerat tillstånd, min/max PWM, faktiskt start/stopp, effektbegränsning, återhämtning efter förlorad TCP/låsning samt vad boxen gör vid servicekrasch. Modbus-läsning är **inte** bevis på fungerande stopp.
- `EVCC_CONTROL_ARMED=0` betyder shadow mode: läs, planera, simulera och logga, **inga** EVCC-skrivningar. Engångsaktivering via skyddad driftskonfiguration får först ske efter dokumenterat lyckade fysiska prover.
- EVCC-service styr seriellt, idempotent, med monoton versionskontroll, kort TCP-timeout, max antal försök, läs-tillbaka + fysisk tillståndskontroll. Vid kommunikationsförlust är verkligt EVCC-tillstånd **okänt** tills oberoende kontroll bekräftar det; logga inte ett lyckat stopp enbart för att det begärts.
- Befintlig oberoende watchdog får byggas ut att övervaka EVCC-service heartbeat och tillämpa en **på den här laddboxen verifierad** säker återställning (eller larma/blockera fortsatt automatisk drift om fjärrstopp inte går att garantera). Nuvarande inverter-watchdog täcker ännu inte EVCC.
- Manual paus, bilens lokala gräns och hårdvaruskydd ska ha företräde framför appens schema; kontrollen ska inte återaktiveras efter ett verkligt säkerhetsfel utan att orsaken har hanterats.
- UI:s ändringsanrop måste skyddas med autentisering och åtkomstkontroll; skrivfunktionen får inte exponeras öppet via publik reverse proxy. Inga HA/Tibber-hemligheter i klient eller telemetry-loggar.

## 8. Datamodell, API och ekonomi

Nya SQLite-tabeller (WAL och migrationspolicy som `deploy/schema.sql`, versionsmärkt):
- `ev_settings`: valda användarinställningar och verifierade hardware-gates.
- `ev_samples`: UTC-tid, EVCC-state, mätt fasström eller ogiltig markör, erbjuden ström, bil-SoC/källtid, laddboxfel och datakvalitet.
- `ev_plans`: generation, beräknat UTC, giltighet, vald kandidat med 15-minutersplan, prisproveniens, koppling till `optimizer_runs.id`, osäkerheter och beslutsskäl.
- `ev_sessions`: session-ID, start/slut, mål, läge, levererade AC-kWh, källa/kvalitet, uppskattad kostnad, måluppfyllelse/okänd, slutorsak.
- `ev_actions`: tid, planversion, tänkt/applicerad ström, läsback, armed, beslut, retry/fel/watchdog, kopplad session.

API i Next.js: `GET /api/ev/status`, `GET /api/ev/plan`, `GET /api/ev/sessions`, `PATCH /api/ev/settings`, `POST /api/ev/pause`, `POST /api/ev/resume`. Läs-API endast status; mutations-API validerar typ/gränser, är autentiserat och sätter **avsikt** i DB. Bara den lokala Python EVCC-tjänsten får göra faktiska styrskrivningar.

Effekt när EM111 ännu inte läses separat: `P_est_kW ≈ U_assumed_V * I_EVCC_A / 1000` vid enfas (inledningsvis 230 V, effektfaktor nära 1). Energi beräknas med tidsintegral på **giltiga** strömprover och samplingsintervall; långa luckor ska ge `unknown`/osäkerhetsmarkering, inte interpolerad faktura. Om EVCC rapporterar kontrollvärde (PWM) men inte verklig ström får det aldrig presenteras som uppmätt energi.

Pris per intervall tas från leverantörens inköpspris för köpt el, med korrekt energiskatt, moms och Sundsvall Elnäts verkliga avtal. För solel använd utebliven försäljningsintäkt, för batteri beakta förluster/slitage; effektavgift bara om verifierat tidsbaserat tariffschema finns. Spara prisversion och indata så en session är revisionsbar. Skilj mellan (1) uppskattad allokerad laddkostnad och (2) kontrafaktisk **merkostnad för hela fastigheten med EV jämfört med utan EV**. Den andra beräkningen ska baseras på reoptimerad jämförbar batteriplan, inte enbart spotpris × kWh.

Historisk hushållslastmodell: separera EV-kWh från inverterns uppmätta `house_load_w` innan inlärning/forecast och lägg EV-planen tillbaka **en gång** för driftoptimering. Om EV-proveniens saknas måste tränings-/replayperiod markeras och användas försiktigt, inte tyst behandlas som ren hushållslast.

## 9. GUI

`EvChargingCard` integreras i huvuddashboarden: anslutningsstatus, bil-SoC (med källtid), målet 80 %, avresetid 07:00, Smart/Solöverskott/Snabb/Pausad, beräknad start och klar-senast, planens billigaste intervall, momentan EV-ström/effekt, ackumulerade kWh och kostnad, husbatteriets skyddsläge. Reserv A visar `SoC okänd, bilens lokala 80 %-gräns styr`. Historik med 24h/7d/30d och per-session pris- och energikurvor, med tydlig precision: uppmätt/uppskattad.

## 10. Verifiering och leveransgränser

Tester innan automatiken får armera:
1. EVCC transport: giltiga ASCII-LRC, timeout, retries, parallella anslutningar, start/stopp/readback, att mätt ström verkligen är mätt och hur EVCC beter sig vid avbrott. Kör read-only före kontrollerad skrivprobe.
2. Optimering: 07:00 före nästa morgon, mål 80 %, redan full bil, fysiskt otillräckligt laddfönster, laddström/cap, PV-bonus kontra nattpris, ingen oavsiktlig batteriurladdning, prisbortfall, no-SoC reserv A och återkomst av SoC.
3. Tider: DST med 92/96/100 slot, tidsförskjutning, omstart strax före 07 och fullgiltig UTC-planversion.
4. Telemetri: SQLite-concurrency, migrationskompatibilitet, särskild EV-kWh avräknad från träning, avbrott/luckor markerade, energiintegral och kostnadsallokering/replay.
5. Styrning: failover när nät eller HA försvinner, watchdog, manuell paus, säker spärr efter verkligt fel, återstart utan två aktiva TCP-författare; ingen förbigång av inverter/fasskydd.
6. `npm test`, `npm run test:py` och `npm run build` samt read-only integration och kontrollerad fysisk test med loggad verifiering innan live-enable.

Leveransstegen konkretiseras i avsnitt 12 till åtta separat driftsättbara etapper med acceptanskriterier och progresslogg. EM111 på separat METER-buss kvarstår som en senare möjlig utökning.

## 11. Kända risker och villkor för driftsättning

- EVCC-styrregister är protokolldokumenterade; ett begränsat skrivprov med sänkt och återställd strömgräns rapporterades lyckat 2026-10-21. **Verklig start/stopp, felåterhämtning och säker återställning återstår att verifiera** inför automatisk styrning.
- `EVCC/RFID` och `METER` är separata RS485-bussar; befintlig Waveshare på EVCC ger inte automatisk EM111-mätning.
- `Solinteg Auto/idle` är inte dokumenterad fysisk batterieffektfrysning; EV-laddning måste observera verklig `battery_w` och säkerheten får inte bygga på oprövad `freeze_battery()`.
- Hårdvarans effektgränser, aktiv konfiguration, EV fas, elmätarnas mätnoggrannhet och avtal (Tibber/Sundsvall Elnät) måste vara kontrollerade. Nuvarande kod innehåller också referensinstallationsvärden som inte får ärvas oavsiktligt.
- Bilen måste själv upprätthålla 80 % i reservläge A, annars kan vi inte garantera procenttak utan aktuell bil-SoC.
- Appen är energioptimering, **inte** en certifierad ersättning för elektriskt överströmsskydd eller installatörsbedömd lastbalansering.

Beslutsstatus: övergripande mål, lägen, 80 %/07:00 och reserv A har godkänts av användaren. Den 2026-10-24 beställdes endast en etappindelad implementationsplan med acceptanskriterier och progresslogg; **inga kodändringar, deployments eller live-styrning har godkänts genom denna beställning.**

## 12. Etappindelad implementation, acceptanskriterier och progresslogg

**Beslut 2026-10-24:** Implementera i åtta isolerade, självständigt driftsättbara etapper. Varje etapp ska ge något verifierbart i verklig miljö utan att påverka befintliga Solinteg-funktioner. Denna ändring är **endast dokumentation**. Ingen kod, konfiguration eller deployment ändras.

### 12.1 Gemensamma leveransregler

- **Branch:** endast Sundsvall i ermhawi/solinteg-controller-oss; inga commits till main. Varje etapp ska ha en begränsad, granskbar ändring och egen dokumenterad driftsättning.
- **Oberoende säkerhetsspärrar:** inför EV_ENABLED (default false) för EV-funktionens aktivering och behåll EVCC_CONTROL_ARMED=0 som oberoende, hårt stopp för samtliga EVCC-skrivningar. EV_ENABLED får inte kunna armera styrning. Läsning, GUI och simulering kan användas med EVCC_CONTROL_ARMED=0.
- **Ingen regression:** befintlig batterieoptimering, inverterregister, dispatch_loop.py, telemetri och GUI ska fungera oförändrat. EVCC får inte skapa en konkurrerande styrloop. Använd separat kodväg, bakåtkompatibla och idempotenta DB-migrationer och oförändrade befintliga defaults.
- **Före varje deploy:** spara tidigare commit/image/config och dokumentera återgång; kontrollera isolation och feature flags; kör npm test, npm run test:py, npm run build och relevanta integrations-/migrationsprov (eller dokumentera varför ett prov saknar stöd och blockera om säkerhetskritisk kontroll saknas).
- **Efter varje deploy:** verifiera servicehälsa, loggar, aktuell batteriplan, faktiskt dispatch-beteende och oförändrade inverterkommandon samt etappens nya funktion. Jämför tidsstämplade före/efter-observationer i faktisk drift.
- **Rollback:** stäng EV_ENABLED, återgå till tidigare fungerande release och verifiera normal batteridrift; använd icke-destruktiva migrationer. För fysisk EVCC-styrning krävs separat verifiering av faktisk laddboxstatus: kodrollback innebär inte att hårdvaran säkert har stoppat.
- **Stop/go:** först när varje acceptanskriterium har belägg och regression, deploy och återgångsväg är verifierade får nästa etapp starta. Fysiska skrivprov och automatisk drift kräver separat uttryckligt godkännande, verifierad elsäkerhet och dokumenterade skydd.
- **Spårbarhet:** uppdatera progressloggen vid varje etapp och statusändring. Registrera commit-SHA, image/deploy, körtid, tester, mätvärden/länkar, risker, rollback och slutbeslut. Saknade belägg betyder inte verifierad.

### 12.2 Åtta driftsättbara etapper

#### Etapp 1 – Isolerad EV-grundstruktur

**Leverans:** EV-typer, separat konfiguration, avstängda feature flags och nya, bakåtkompatibla SQLite-tabeller. Inga nya aktiva EV-processer eller styrkommandon.

**Acceptanskriterier:**
1. EV_ENABLED=false och EVCC_CONTROL_ARMED=0 innebär ingen EVCC-uppkoppling eller skrivning och ingen ändring av inverterstyrningen.
2. Migration fungerar på befintlig databas, är idempotent och påverkar inte tidigare tabeller, läsare eller historik.
3. Befintlig testsuite, build och smoke-test i drift är godkända; föregående release kan återställas.

**Deployläge:** ny grundstruktur avstängd. Endast kompatibilitet testas.

#### Etapp 2 – Read-only EVCC-telemetri

**Leverans:** isolerad, ensam EVCC-lässervice för Waveshare/Modbus ASCII med timeout, reconnect, tidsstämplar och datakvalitet. Inga skrivvägar aktiva.

**Acceptanskriterier:**
1. Verklig anslutning, laddning, bortkoppling och fel kan avläsas utan Modbus-skrivningar; värden visas med källa och färskhet.
2. TCP-bortfall, stale status och återanslutning testas; en enda process äger RS485-gatewayen.
3. PWM/satt gräns skiljs tydligt från faktiskt uppmätt ström och energi; inverter och batteri förblir opåverkade.

**Deployläge:** read-only, EVCC_CONTROL_ARMED=0.

#### Etapp 3 – Volvo-SoC genom Home Assistant

**Leverans:** read-only SoC-adapter som lagrar värde, tidsstämpel, datakälla och färskhet. Förbered reservläge A, utan styrning.

**Acceptanskriterier:**
1. SoC stämmer med Home Assistant och visas med tid/kvalitet.
2. Bortfall, föråldrat värde och återkomst fungerar; okänt SoC antas aldrig vara 0 % eller 80 %.
3. Hemligheter skyddas och inga Volvo-, EVCC- eller inverterkommandon skickas.

**Deployläge:** dataläsning, ingen styrning.

#### Etapp 4 – Laddningskort och sessionshistorik

**Leverans:** EvChargingCard, read-only status-/sessions-API, sessionslogg och historik 24h/7d/30d. Status, energi och kostnad presenteras med tydlig proveniens.

**Acceptanskriterier:**
1. Kortet fungerar på mobil och befintlig dashboard utan förskjutningar, prestandaregression eller påverkan på andra kort.
2. Anslutning, start/slut, telemetrigap och omstart ger begripliga sessioner, inklusive explicit okända perioder.
3. Measured/estimated/stale/unavailable särskiljs; PWM presenteras inte som uppmätt energi och skattade kostnader märks.
4. Inga användarknappar eller API-anrop kan styra laddboxen i detta steg.

**Deployläge:** GUI och read-only-telemetri.

#### Etapp 5 – Prisbaserad EV-planering i shadow mode

**Leverans:** fristående schemaläggare som simulerar 15-minutersladdning mot elpris, användarens 80 %-mål, avresa 07:00 och verifierade strömgränser. Ingen plan exekveras.

**Acceptanskriterier:**
1. UTC-intervall och 07:00 Europe/Stockholm fungerar för vanliga dygn och sommar-/vintertid (92/96/100 slot).
2. Tester täcker redan full bil, otillräckligt laddfönster, prisbortfall, okänd/stale SoC och reserv A; osäkerhet/underskott visas korrekt.
3. Simulerad plan och priskällor loggas och kan jämföras med verkliga observationer; batteriplan och EVCC förblir orörda.

**Deployläge:** simulering, EVCC_CONTROL_ARMED=0.

#### Etapp 6 – Samordnad EV- och batterioptimering i shadow mode

**Leverans:** EV-kandidater jämförs mot hushållets totalkostnad via befintlig batteri-DP på kopierat/snapshotat indata. Planpar får gemensam generation och tydlig giltighetstid. Befintlig dispatch använder fortfarande sin gamla exekveringsväg.

**Acceptanskriterier:**
1. EV-last tillförs exakt en gång och separeras från historiska hushållslastprognoser; ingen dubbelräkning i replay.
2. Kostnadsreplay jämför med/utan EV på lika indata och förklarar valt alternativ; osäker tariff-/mätdata markeras.
3. Versionspar och giltighetsbarriär är testade vid samtidighet, omstart och delvis DB-fel.
4. Verklig batteri-dispatch och inverterkommandon är oförändrade; misstänkt extra batteriurladdning stoppar nästa steg.

**Deployläge:** gemensam simulering utan styrning.

#### Etapp 7 – Kontrollerad fysisk EVCC-probe

**Leverans:** skrivväg kan driftsättas disarmed, men får endast aktiveras för separat godkänt, kort och övervakat fysiskt prov. Läs först befintlig strömgräns, sänk säkert, verifiera faktisk laddrespons och återställ. Testa därefter start/stopp, förbindelsefel och watchdog innan automatisk körning övervägs.

**Acceptanskriterier:**
1. Installatörsbedömd EV-fas, säkring, kabel och tillgängligt fasutrymme är verifierade. Husets 20 A huvudsäkring räcker inte som bevis för säker EV-ström.
2. EVCC_CONTROL_ARMED=0 blockerar varje skrivning även om GUI/planerare är aktiva. Endast EVCC-tjänsten får skriva och varje åtgärd kräver läsback plus fysisk observation.
3. Sänkt/återställd ström, start/stopp där stödd, nätbortfall, tjänsteomstart och watchdog har loggade utfall. Fysiskt stopp eller säkert blockerat läge är verifierat; ett skickat kommando räknas inte som bekräftad stoppåtgärd.
4. Testet avslutas med tidigare verifierad säker inställning och EVCC_CONTROL_ARMED=0. Vid fel avbryt och verifiera hårdvarans faktiska tillstånd före fortsatt arbete.

**Deployläge:** skrivkod disarmed; separat uttryckligt godkännande för övervakat prov. Det tidigare begränsade skrivprovet är inte fullständig start/stopp-verifiering.

#### Etapp 8 – Automatisk smartladdning i övervakad pilot

**Leverans:** exekvera gemensam plan efter alla tidigare gates, med Smart/Solöverskott/Snabb/Pausad, replanering, husbatteriskydd, reserv A och larm. Piloten är tidsbegränsad och armeras separat.

**Acceptanskriterier:**
1. En övervakad pilot följer rätt planversion, respekterar bekräftade effektgränser och försöker nå 80 % till 07:00; om det är omöjligt visas underskott, inte en falsk garanti.
2. Verklig batteriurladdning och fasbelastning övervakas; EV reduceras/pausas säkert utan oprövade inverter-freeze-register eller antaganden om fasampere från endast watt.
3. Testfall för saknad Volvo-SoC, okänd lokal 80 %-gräns, pris-/HA-/nätbortfall, omstart, manuell paus och fel har dokumenterat säkert utfall. Reserv A får bara starta automatiskt om bilens lokala 80 %-gräns är verifierad.
4. Styrbeslut, läsback, fysisk respons, kWh, kostnad, osäkerheter och larm kan följas i loggar och GUI.
5. Disarm/rollback och säker laddboxstatus verifieras. Ingen permanent automatisk aktivering utan separat beslut efter godkänd pilot.

**Deployläge:** först disarmed, sedan en explicit godkänd övervakad pilot. Ingen automatisk aktivering genom dokumentet.

### 12.3 Progresslogg

Status: **Ej påbörjad**, **Pågår**, **Deployad – ej verifierad**, **Verifierad**, **Blockerad**, **Återställd**. Bara **Verifierad** uppfyller kriterierna för att gå vidare.

| Etapp | Status per 2026-10-24 | Commit/deploy | Nästa verifiering |
|---|---|---|---|
| 1. Grundstruktur | Ej påbörjad | – | Flags, schema, regression och rollback |
| 2. EVCC read-only | Ej påbörjad | – | Live-telemetri, gateway-exklusivitet och fel |
| 3. Volvo-SoC | Ej påbörjad | – | HA-värden, datakvalitet och bortfall |
| 4. GUI/historik | Ej påbörjad | – | Mobil, sessioner, mätprecision och read-only |
| 5. Prisplan i shadow | Ej påbörjad | – | DST, prissimulering och reserv A |
| 6. Gemensam shadow | Ej påbörjad | – | Kostnadsreplay, dubbelräkning och isolering |
| 7. Skrivprobe | Ej påbörjad | – | Separat tillstånd och fysiskt fail-safe |
| 8. Automatikpilot | Ej påbörjad | – | Separat tillstånd, säker pilot och rollback |

**Händelselogg (append-only):**
- **2026-10-24 – Planering:** åtta leveransetapper, acceptanskriterier, test/deploy/rollback-gates och progresslogg dokumenterade. Inga funktioner har implementerats eller deployats. Nästa steg: separat beslut om att påbörja etapp 1.

**Mall för nästa loggrad:**
- Datum/tid (Europe/Stockholm), etapp, syfte och status:
- Commit-SHA, deploymentversion och miljö:
- Testresultat (inklusive befintlig regression):
- Driftsverifiering före/efter med tidsstämplade värden, loggar och länkar:
- Uppfyllda/ej uppfyllda acceptanskriterier och blockerare:
- Rollback-förutsättningar samt eventuell faktisk återställning:
- Stop/go-beslut, beslutsgrund och nästa tillåtna steg:
