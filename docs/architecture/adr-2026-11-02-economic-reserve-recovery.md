# ADR 2026-11-02: prisstyrd mjuk reserv och separat fysisk SoC-återhämtning

**Status:** Implementerat i `Sundsvall`-källkod och selektivt driftsatt på Dell
2026-11-02 kl. 20:47–20:53 CET. Bara installerad optimerare, befintlig
dispatcher, site-inställningen för säkerhetsåterhämtning och kompilerat
webbbygge ändrades. Den installerade dispatchern har även äldre
platsanpassningar som inte ersatts med hela Git-filen.

## Bakgrund och observerad regression

Den 20 oktober var prisoptimeringen fungerande. A/B-återspelning på 13 historiska
`optimizer_runs` (19–31 oktober) visade att den gamla och nya optimeraren
genererade **identiska hela planer** vid samma 12 %-golv, men skilde sig när
en 20 %-planeringsreserv låg över faktisk SoC. Den 23 oktober (SoC 14 %) och
26 oktober (SoC 15 %) beordrade den nyare algoritmen nätladdning omedelbart
trots lägre kommande elpriser. Samtidigt hade dispatchern kopplat en 20 %
**ekonomisk** reserv till säkerhetsåterhämtningens tröskel (<19,5 % → 21 %).
Historiska loggar 21–27 oktober visar många återhämtningsbeslut om 300 W,
som trängde undan ekonomioptimeraren. Detta är en kombination av policy och
driftsätt, inte ett belagt generellt fel i solcellsladdningen.

## Beslut och implementation

- Behåll `SOLINTEG_PLANNER_RESERVE_PCT=20` för **planerad** urladdning;
  en plan som *startar under* 20 % får stanna där och invänta ett billigare
  laddfönster. Den får inte planera ytterligare urladdning under reserven.
  Inget krav på att köpa nätenergi i första 15-minutersslot.
- Ta bort kopplingen där en ekonomisk reserv automatiskt höjer
  `SOC_RECOVERY_ENTRY_PCT` och `SOC_RECOVERY_EXIT_PCT` i dispatchern.
  Separat säkerhetsåterhämtning följer endast mjukvarugolv, buffert och
  återhämtningsutgång. På Sundsvall avses `12 % + 0,15 kWh` (~13 %)
  till `16 %`, 300 W minimum. Inverter/BMS minst 10 % lämnas orört.
- Behåll `prioritize_soc_recovery` som överordnat skydd **när säkerhetsbandet
  faktiskt nås**. Dess nuvarande kod behåller redan en högre genuint
  planerad laddningsnivå när den är tillåten; L3-CT-guard kan begränsa den.
- När planen säger `idle` vid/under ekonomisk reserv och inget solöverskott
  är verifierat, använd **befintligt** EMS-batterisparläge (50 W börvärde)
  i stället för General som annars fortsätter tömma batteriet med huslast.
  Vid mätt solöverskott >100 W används General för naturlig solupptagning.
  Genuint prisplanerad `charge` och säkerhetsåterhämtning behåller företräde.
  Läget förbrukar fortfarande omkring 150–290 W i observerade historiska
  EMS-perioder, så detta är inte ett fysiskt perfekt 20 %-stopp.
- Gör **ingen** full rollback till 20 oktober. Nödvändiga rättningar av
  PlaneraElens pris-API, DST och L3-begränsningar ska bevaras.
- Behåll de tidigare åtta oincheckade filerna. Den separata
  `solar_charge`-varianten är fortsatt opt-in-avstängd, inte en del av
  denna driftsättning. Ingen EVCC-kontroll aktiveras av detta beslut.

## Viktig begränsning

En planeringsreserv är **inte** ett fysiskt hårt SoC-stopp.
General kan självmant urladda batteriet medan planen säger `idle`.
Det redan kända 50 W-batterisparläget minskar detta men ger inte
0 W faktisk urladdning. Under mätt PV-överskott väljs General igen;
snabba väder-/laständringar mellan dispatch-tick kan fortfarande
tillfälligt urladda. Samma grundläggande konflikt återstår för
framtida EV-laddning (EVCC-pris-/laststyrning är inte aktiverad).
Den fysiska tröskel som garanteras i vår kod är det
separata säkerhetsbandet, inte 20 %. Din ordinarie övervakning av
webb/SoC och anläggningens hårdvaruskydd fortsätter att gälla.

**Verifiering:** Testa låg SoC och fallande elpriser i optimizer-regression,
kör Python/TS-testsviter, jämför faktiskt installerade filer innan eventuell
driftsättning, gör lokal återställningskopia, kontrollera aktiva systemd-tjänster,
färsk fas- och SoC-telemetri och verkliga `control_actions` efter omstart.
Ingen fysisk laddningsökning eller ändring av BMS-register krävs för detta beslut.

## Driftsverifiering 2026-11-02 (CET)

- Kompilerad webb med installationsprefixet `/solinteg` svarade HTTP 200 för
  dashboard, inverter-API och plan-API. Originalwebbbygge är bevarat i
  lokal root-skyddad rollback-kopia. Inget annat webbkodspaket
  från senare Git-commits eller de oincheckade filerna driftsattes.
- Aktiv dispatcher startade med `SOLINTEG_PLANNER_RESERVE_PCT=20`,
  `SOLINTEG_SOC_FLOOR_PCT=12`,
  `DISPATCH_SOC_FLOOR_GUARD_HEADROOM_KWH=0.15` och
  `DISPATCH_SOC_RECOVERY_EXIT_PCT=16`. Vid 19 % loggades
  `SoC recovery complete ... exit 16.00%` i stället för påtvingad
  återhämtning till 21 %.
- Direkt före idle-skyddet i General observerades cirka **1 439 W**
  batteriurladdning (huslast 1 480 W). Efter byte till samma kända
  EMS-batterisparbörvärde **50 W** vid 0 W solproduktion uppmättes
  cirka **229 W** batteriurladdning, cirka **1 304 W nätimport** och
  `EMS BattCtrl`. Detta är verifierad reducerad urladdning, **inte**
  bevis på 0 W eller på framtida solöverskottsbeteende.
- `solinteg-web`, `solinteg-poller`, `solinteg-dispatch` och ordinarie
  `solinteg-watchdog.timer` var aktiva. EVCC-observatören visade A1
  (bil ej laddande) vid förkontroll; bilens nätprioritet är inte införd.
- Inga nya systemd-enheter, timers eller nya inverter-/EVCC-registertyper
  skrevs. Fysisk solupptagning efter väderomslag behöver följas upp.
- Ursprunglig env, installerad dispatcher, optimerare och kompilerat
  webbutdata sparades före driftsättning under
  `/opt/solinteg/rollback-economic-reserve-20261102/`
  med skyddade filrättigheter. Återställning kräver återföring av
  dessa filer och omstart av webb/dispatcher; backupen bevaras.

## Nattlig standbyförlust under 20 % – uppföljning 2026-11-02

**Bakgrund:** Första driftsättningen minskade faktisk tomgångsurladdning
till omkring 180–260 W genom ett redan känt 50 W EMS-börvärde, men
optimeraren förutsade fortfarande konstant SoC under 20 %. Det kan
skjuta fram billig laddning så länge att fysisk säkerhetsåterhämtning
inträffar före det planerade fönstret.

**Korrigering:** Vid en planstart på högst den ekonomiska reserven och
positiv `batterySaveDriftW` utvidgas optimerarens SoC-gitter ned till
den **fysiska** mjukvarugränsen. För mörka slotar med faktisk
huslast är `idle` med oförändrad SoC inte längre en genomförbar
övergång. I stället vägs verkligt standbyuttag mot priset på att
köpa nätenergi, även när SoC redan är under 20 %. Bara **oundviklig**
standby får minska SoC under reserven; vanliga planerade
`discharge`-övergångar är fortfarande förbjudna där.

Lägsta tillåtna prognos efter standby styrs av den **separata**
fysiska starttröskeln, `BATTERY_MIN_SOC_KWH +
DISPATCH_SOC_FLOOR_GUARD_HEADROOM_KWH` (~13 % på Sundsvall), och
får aldrig understiga fysisk mjukvarugräns. Om en fortsatt drift
skulle korsa tröskeln väljer optimeraren en tidigare prisvägd
`charge` i stället. Vid prognostiserat nettoöverskott >100 W
kan normal `idle` ladda batteriet fritt från solen. För andra
anläggningar utan separat ekonomisk reserv och för planstarter
över reserven är beteendet oförändrat.

**Verifiering:** Fyra nya regressionstester täcker dyr respektive
nåbar billig timme, naturlig solcellsladdning och frånvaro av
fiktiv förlustfri natt-`idle`. A/B-återspelning mot åtta historiska
låg-SoC-körningar (inklusive senaste produktionsplan) gav inget
planerat underskridande av ~13 %; lägsta prognos efter korrigeringen
var cirka 13,4 %. Detta är **modellutfall, inte en garanti**:
SoC-läsningens kvantisering, prognosfel, tidsfördröjning och
inkopplad last kan fortfarande utlösa fysisk återhämtning.
Optimerarens SoC-gitter representerar cirka 0,07 kWh per slot
och kan överskatta den uppmätta standbyförlusten (~0,055 kWh).
Regelbunden omplanering ska fortsätta kalibrera mot uppmätt SoC.

Ändringen kräver endast ny `lib/optimizer.ts` och `lib/plan.ts`
i webbdelen, med `NEXT_BASE_PATH=/solinteg` vid byggning. Den
anpassade, aktiva dispatchern samt EVCC, watchdog, BMS-register
och icke-driftsatt `solar_charge` lämnas oförändrade.

### Selektiv driftsättning 2026-11-02 kl. 21:15 CET

Dells installerade optimerare innehöll äldre, platsspecifika skillnader
mot Git. Därför patchades **enbart** nattförlustsmodellen i en isolerad
kopia av installerad `lib/optimizer.ts` och den enda nya optimeringsparametern
i installerad `lib/plan.ts`, utan att ersätta övrig installerad
källkod. A/B-kontroll av hela dispatch-planen mot Git-kandidaten gav
**byteidentiska planer i samtliga åtta låg-SoC-scenarier**.

Webbbygget kompilerades med `NEXT_BASE_PATH=/solinteg`, och
`/solinteg`, `/solinteg/api/inverter` och
`/solinteg/api/plan` svarade HTTP 200. POST
`/solinteg/api/replan` returnerade `ok=true`,
`socIsLive=true` och en **publicerad 203-slotsplan** med SoC
cirka 17 % vid planstart, förutsagd lägsta SoC cirka 13,38 %
och planerad stödladdning direkt. Den planens fysiska genomförande
ska bedömas mot `control_actions` och ny pollermätning.

**Rollback:** Befintlig webb, `lib/optimizer.ts`,
`lib/plan.ts`, privat env och service-definitioner är sparade
i den root-skyddade mappen
`/opt/solinteg/rollback-standby-drift-20261102/`.
En eventuell återställning kräver återföring av just dessa filer,
webbbygge och omstart av enbart `solinteg-web`. Den aktiva
dispatcherns kod och tjänst har inte ändrats eller startats om
som del av denna selektiva nattförlustsrelease.

**Kvarstående exekveringsavvikelser, verifierade utan fysiska
extrakommandon:** Den nya planen publicerades *efter* dispatcherns
beslut vid 21:15. Det befintliga `needs_apply`-skyddet blockerar
`discharge` → `charge` inom samma 15-minutersslot när fysisk
SoC-återhämtning inte är aktiv; det nya planbeslutet tillåts därför
tidigast vid nästa slotgräns (21:30). En read-only-körning av aktiv
`decide` visade också att `DISPATCH_LIVE_LOAD_TRACKING_CHARGE`
kan begära högre laddningseffekt än en liten planerad stödladdning,
eftersom den försöker hålla hela planens nätimport konstant trots
lägre faktisk huslast. Kontrollera kommande verklig
`control_actions` och effektmätning innan man påstår att exakt
planerad laddningsenergi genomförts. Detta åtgärdas **inte** av
nattförlustsmodellens release; framtida justering kräver en separat
kompatibilitets- och säkerhetsgranskning av Dells anpassade dispatcher.

## Minimal stödladdning medan vi väntar på lägre elpris (2026-11-02)

**Observation:** Den nya optimeraren hade avsiktligt små
`charge`-steg kring 350 W under dyrare kvällstimmar och större
laddsteg under nattens billigare timmar. Men den aktiva dispatcherns
`live_charge_power_w` försökte samtidigt hålla den *totala*
prognostiserade nätimporten konstant. När verklig huslast sjönk
växlade laddningsbörvärdet 200–1 100 W trots att den ekonomiska
planen avsåg omkring 350 W.

**Regel:** När en giltig plan vill ladda med högst 500 W, priserna
innehåller ett alternativ minst 1 öre/kWh billigare inom de nästa
12 timmarna och det inte finns mätt nettoöverskott över 100 W,
begränsas uppåtriktad live-lastkompensering till
`min(planerat börvärde, 500 W)`. Nedåtriktad korrigering behålls
så att hög nätlast fortfarande kan sänka laddningen. Regeln gäller
endast anläggningar med separat ekonomisk planeringsreserv och
förutsätter färsk mätt SoC ovanför fysisk återhämtningsgräns.
Den fortsätter vid SoC över 20 % om prisväntan består.

**Undantag:** Fysisk säkerhetsåterhämtning i
`prioritize_soc_recovery` körs *efter* stödladdningsbegränsningen
och kan därför begära sin egen nödvändiga effekt. Äkta
laddningsplaner med över 500 W och verifierat solöverskott
begränsas inte; efterföljande L3-/nätvakt bibehålls. Inga
EVCC- eller nya Modbus-register åtgärdas. En övre gräns på
börvärdet är ingen garanti för exakt uppmätt DC-batterieffekt.

**Återställning:** `DISPATCH_PRICE_WAIT_SUPPORT_MAX_W=0` avaktiverar
regeln vid nästa dispatcherstart. Historiska planer och webbens
prisoptimerare ändras inte. Detaljloggen märker begränsade
beslut med `support_price_wait`, `support_charge_before_w`,
`support_charge_cap_w` och `support_cheaper_future_ore`.

**Verifiering:** Unit-test med cirka 17 % SoC, plan 350 W och
live-lastkompensering 1 000 W ger 350 W; full laddning,
solöverskott, fysisk nödladdning, saknat prisunderlag och
återställningsknapp testas separat. För skarp validering
måste installerad dispatcher patchas selektivt eftersom
dess lokala kod skiljer sig från Git, och därefter
måste faktiskt `control_actions`-börvärde och uppmätt
`battery_w` observeras.

### Driftsättning och fysisk mätning, kl. 21:51–21:52 CET

Endast den aktiva, platsanpassade `dispatch_loop.py` kompletterades
med prisväntansbegränsningen och dispatchern startades om. Ändringen
kompilerades och verifierades i isolerad driftskopia; AST-jämförelse
visade att enda förändrade funktionen var `decide` och att enda
tillagda funktionen var `price_wait_support_charge_target`. Övrig
driftanpassad kod, EVCC, solöverskottsprov, säkerhetsgränser,
webbbygge och privat env lämnades oförändrade. Befintlig
`DISPATCH_IMPORT_LIMIT_W` återtillämpades av normal uppstart.

- **Före:** Historiska `control_actions` visade 400–1 100 W
  från live-lastkompensering under små planerade stödladdningar.
- **Efter:** `2026-11-02T20:51:51Z` loggade dispatcher
  `charge 300 W`, `applied`, `support_price_wait=true`,
  `support_charge_before_w=600`, `support_charge_cap_w=300`.
  Planens börvärde var 303 W och dygnets billigare pris i nästa
  12-timmarsfönster 84,98 mot aktuella 90,11 öre/kWh.
- **Fysisk mätning:** Pollern rapporterade efteråt
  `2026-11-02T20:52:16Z`, SoC **17 %**,
  `EMS BattCtrl`, `battery_w=-330 W` (verklig laddning),
  nätimport 1 106 W, solproduktion 0 W. Det är en enskild
  verifierad mätning, inte en långtidsmätning av stabil laddning.
- Vid serviceomstartens normala återgång till General fanns en
  kort övergångsmätning med batteriurladdning 1 239 W.
  **Den nya laddningen hade då ännu inte hunnit tillämpas.**
  Därefter återgick växelriktaren till EMS-laddning.
- Samtliga ordinarie tjänster samt EVCC-observatören var aktiva.
  EVCC var i A1, bilen inte ansluten. Inga nya timer-/servicefiler
  eller EVCC-kommandon skapades.

**Lokal rollback och kvitto:**
`/opt/solinteg/rollback-price-wait-support-20261102/` med den
aktiva dispatcherns tidigare `dispatch_loop.py`,
privat konfiguration (endast skyddad kopia), systemd-definition,
filhashar och driftsättningskvitto. Återställning innebär
återföring av backupfilens ägarskap/behörigheter och omstart av
`solinteg-dispatch` utan ändring av webb eller poller.

## Nattgranskning och separat SoC-minimum (2026-11-03)

**Mätt 2–3 november:** Börvärdena 170–300 W höll den övre
prisväntansgränsen, men SoC sjönk ändå från cirka 17 till
**12 %** innan bulk-laddningen inleddes efter klockan 02.
Flera gånger valde `live_charge_power_w` 0 W eftersom husets
aktuella förbrukning var högre än planens totala nätimport.
Trots `charge` i planen demoterades då beslutet till
`idle`; under ekonomisk reserv bytte dispatchern i stället
till **50 W EMS-sparläge**, som fysiskt kan urladda omkring
150–290 W. Separat säkerhetsåterhämtning fick därför ingripa
vid cirka 13 %; det tidigare optimerarresultatet 13,4 % som
lägsta SoC stämde inte med nattens observerade SoC och
kvantiserings-/BMS-avvikelser.

**Korrigering, opt-in:** `DISPATCH_PRICE_WAIT_SUPPORT_MIN_W=300`
(standard **0**) behåller under faktisk SoC <= ekonomisk
reserv en liten **positiv** laddning även när live-lastkompensering
föreslår 0 W. Målet är högst
`max(optimerarens lilla planerade laddning, 300 W)`,
alltid under befintlig `DISPATCH_PRICE_WAIT_SUPPORT_MAX_W=500`.
Samma prisvillkor gäller: framtida el minst 1 öre/kWh
billigare inom 12 timmar, liten planerande laddning, mätt
PV-överskott <= 100 W, SoC över fysisk återhämtningsgräns
och separat ekonomisk reserv.

**Oberoende säkerhetsvillkor:** Ingen höjning av live-börvärdet
utan senaste 45 sekunders konsistenta fas-/CT-data,
`50006=0`, aktiv L3-vakt och ett konfigurerat nätimporttak
med marginal för den *ytterligare* laddningsbegäran. Budgeten
beräknas utan extra Modbusanslutning från senaste pollerdata.
Saknas verifiering lämnas live-börvärdet oförändrat och
den separata fysiska låg-SoC-säkerhetsåterhämtningen
är fortfarande överordnad. `DISPATCH_PRICE_WAIT_SUPPORT_MIN_W=0`
stänger bara miniminivån; `...MAX_W=0` återställer all
prisväntansbegränsning. Detta är inte en säkringsgaranti.

**Bedömning av 100 % och solprognos:** En aktuell plan visar
ungefär 4,59 kWh prognostiserad *brutto*-solenergi fram
till 14:00, men samtidigt ungefär 10,88 kWh
husförbrukning. Därmed finns **inget prognostiserat
nettoöverskott** att lämna ledigt batteriutrymme för.
Inställningen `SOLINTEG_SOC_CEILING_PCT=100` är en
platsspecifik avvikelse från exempelvärdet 98.
Inget nytt laddtak införs utan separat beslutad
batterihälso-/livslängdspolicy och verifierad modellering
av elpris, slitage och framtida solöverskott.

**Modbus-felen** är separat dokumenterade i
[broker-readonly- och driftgranskningen](../modbus-broker-readonly-stage3-2026-11-02.md).
Den första nätstörningen var ett verkligt Wi-Fi-avbrott på
Dell, medan den andra saknar fastställd orsak.
Produktionens färska poller, L3-guard, EVCC-read-only,
fysisk SoC-återhämtning och watchdog får inte ersättas
av ett förhastat broker-byte.

### Selektiv driftsättning på Dell 2026-11-03 kl. 06:22 CET

**Status: installerad och processverifierad.** Endast
den aktiva, platsanpassade `dispatch_loop.py` uppdaterades
till en testad variant samt en enda privat env-inställning,
`DISPATCH_PRICE_WAIT_SUPPORT_MIN_W=300`. Källkodens
**standard är 0** och andra installationer ändrar inte
beteende utan explicit site-opt-in. Webb, planoptimerare,
poller, befintlig watchdog, EVCC och nätverks-/Modbusbroker
lämnades oförändrade.

- AST-jämförelse mot den **installerade** dispatchern:
  befintliga funktioner `decide` och
  `price_wait_support_charge_target` modifierade, endast
  `price_wait_support_headroom_w` tillagd samt `math`
  importerad. Ingen gammal intern site-drift eller
  ej driftsatt `solar_charge` kopierades över.
  `prioritize_soc_recovery`, L3-skydd, beslutsaktivering
  och felsäker återställning lämnades bitidentiska.
- Regressionsprov: **1 276 Python-tester**, **703 Vitest**,
  1 överhoppat, TypeScript-typkontroll, lint och diffcheck
  godkända. Isolerad dry-run med historiska planpriser,
  simulerad låg SoC och faktiskt verifierad live-fasbudget:
  föregående `charge=0 W` blev `charge=300 W`;
  utan säker marginal och vid hög SoC ändras ingenting.
- Första deploy-försöket avbröts konservativt då en
  verifiering läste systemd-processens miljö **före
  exec**. Rollback återställde kod/privat env och
  ordinarie drift. Miljöfilens tolkning verifierades
  därefter med ett kortlivat, icke-hårdvaruanslutet
  systemd-test som städade sig självt.
- Andra försöket: ny dispatcherprocess körde med
  `DISPATCH_PRICE_WAIT_SUPPORT_MIN_W=300` i sin
  faktiska miljö. Första ordinarie action kl. 06:22
  var `charge 1300 W`, `applied`, vid cirka 96 %
  SoC; den nya låg-SoC-regeln påverkar **inte** den
  ordinarie laddningen vid denna höga SoC. Fysisk
  uppföljning visade `EMS BattCtrl`, uppmätt
  batteriladdning cirka 1,3 kW och färsk poller.
- **Kvarstår att verifiera fysiskt:** en kommande
  naturlig prisväntan vid låg SoC, utan att medvetet
  urladda batteriet eller utlösa säkerhetsåterhämtning.

**Rollback:** Rot-skyddad mapp
`/opt/solinteg/rollback-support-floor-20261103/`
bevarar tidigare fungerande dispatcher, privat env,
effektiva systemd-definitioner, hashmanifests och
driftsättningskvitto. Återställ *både* kod och env
och starta enbart om `solinteg-dispatch`. Äldre
fristående rollback-punkter bevaras.
