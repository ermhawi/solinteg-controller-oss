# ADR 2026-11-04: Prisplan för elbil och separat lager i Elpriser

**Status:** Kod och UI-förhandsvisning färdig. Automatisk EVCC-verkställighet
**inte aktiverad** eftersom strikt verifierad Volvo-laddsession saknas.
**Branch:** `Sundsvall` (aldrig `main`).

## Beslut och ekonomi

- EVCC:s FC03-status och **Ict2** bestämmer om bilen verkligen laddar
  och ungefär hur stor lasten är (`P_EV ≈ 230 V × Ict2`).
  Strömmen är mätt, men 230 V är nominellt och inte separat
  aktiv-effektmätning.
- Husets plan ska avse dess förbrukning **utan bilen**. Bilens
  planerade effekt adderas till önskad nätimport på rätt fas.
  **Ändra aldrig faktisk uppmätt `grid_w`, fas-CT, historik eller
  fysiska säkringsgränser** med en syntetisk nätlast; den är
  redan inkluderad i nätmätningen.
- Låga **totala köppriser** prioriteras för bilen, med samma
  nätavgifter, skatter och prognosrisk som husoptimeraren.
  Högt pris innebär normalt paus. Det äldre fastställda
  `deadline`-läget får avvika för att försöka nå 80 % vid
  avresa, men bara med tillförlitligt energibehov; `economy`
  väntar på billiga timmar utan avresetidskrav.
- Solöverskott kan gå till bilen först när hus och husbatteri är
  prioriterade och lagring är oberoende verifierad fullt.
  Nätimport under ett samtidigt överskott är ett möjligt
  men **inte normalt** fall; ingen huvudalgoritm byggs kring
  export samtidigt som bilen laddar.
- Husbatteriets ordinarie DP, Modbus-broker och reserv-/fasvakter
  fortsätter oberoende av bilens prisschema. Befintlig
  `ev_l3_live_policy` ska inte ersättas med ett ofärdigt
  EVCC-schema: 50 W EMS kan annars faktiskt urladda batteriet.

## Elpriser, nytt lager

`lib/ev-price-windows.ts` bygger `EvPriceWindowPlan` från
`PriceChart`-modellens **redan fullständiga köppris i öre/kWh**
(inklusive `SKATT_OVERFÖRING`) och samma
`FORECAST_RISK_PREMIUM_ORE` som husets DP. Modellen är ren och
gör **inga** hårdvaru- eller nätverksanrop. Den följer det
befintliga `ev_price_policy.py`-ekonomilägets kriterier:

1. Högst 48 timmar med 15-minuterspris; minst 6 timmars
   användbart underlag och verkligt publicerat startpris.
2. Billiga perioder väljs från den lägsta priskvartilen.
   Absolut användarvalt maxpris får ytterligare begränsa
   om det senare är tillgängligt som ett **sparat** val.
   Jämnt dyr priskurva får **ingen** automatisk laddperiod;
   noll/negativa priser kan väljas.
3. Prognoskvartar får riskpremie och visas ljusare.
   Ordningen följer redan disambiguerade
   `chartData.time` inklusive andra DST-timmen `#2`;
   inga tvetydiga lokala datum tolkas om.
4. Markera föreslagna laddkvartar längst ned i prisdiagrammet.
   Separat lager **Bil · prisfönster** går att slå på/av
   på dator och mobil; tooltip visar `Ladda 6 A`/`Vänta`.
   Total hypotetisk energi beräknas med
   `6 A × 230 V × 15 min = 0,345 kWh` per vald kvart.
   Markeras explicit **prisförslag, inte aktiv EVCC-styrning
   eller leveranslöfte om 80 %**.

Har vi inte tillräckligt färsk och fullständig prisserie visas
**ingen** bilplan; användarens bilbehov eller SoC gissas aldrig.

## EVCC: förberedd riktig körväg, inte ytterligare shadow-tjänst

`scripts/services/ev_price_controller.py` använder **den
befintliga** Python-prispolicyn `decide_grid_price()`,
som läser samma `optimizer_runs` som husets DP.
`ev_price_policy.read_ev_session()` kräver en separat
färsk och kalibrerad session från Volvo/HA
(`source=home_assistant`); `home_assistant_shadow`,
obetydlig API-uppdatering och antagen användbar
batterikapacitet får **inte** utlösa EVCC-åtgärder.

Först efter ett giltigt pris-/energiunderlag väljs
ett av tre resultat:

- Lågt köppris och 6 A med utrymme på L3 → **kandidat**
  EVCC PWM raw `100` när bilen tidigare varit pausad.
  Automatisk återstart är fortsatt **blockerad** tills det finns
  ett persistent och verifierbart ägarskap för *priskontrollerns
  egen paus*. En manuellt pausad bil får inte återstartas
  automatiskt vid nästa billiga period.
- Högt pris eller pausad ekonomi → begär raw `1000`
  om fysisk laddning är tillåten; beslutet utgår från
  riktig EVCC-status, **inte** växelriktaren.
- Saknad session, gammal mätning, annat EVCC-PWM-värde,
  fel, ändrat fasläge eller otillräcklig säkringsmarginal
  → **ingen ny skrivning** och dokumenterad orsak.

`EvccAscii.set_limit_raw(...,scheduled=True)` är en
separat **opt-in** som inte ändrar den manuella provvägen.
Den kräver `EV_ENABLED=1`, `EVCC_CONTROL_ARMED=1`,
`EVCC_PRICE_CONTROL_ARMED=1` och för start
`EVCC_START_APPROVED=1`. Endast 6 A eller paus tillåts
i denna release. En process ska äga EVCC-seriallåset
`evcc-reader.lock`, FC10 sker som **ett** CAS-anrop med
för- och efterläsning via FC03, **aldrig blind retry**.
Ingen ny Modbus-inverterägare införs.

De valbara, ännu oinstallerade
`deploy/solinteg-ev-price-controller.service` och
`.timer` är **riktig framtida EVCC-verkställighet**,
inte en parallell shadow- eller read-only-planerare.
Tjänsten är default `EV_PRICE_CONTROL_ENABLED=0` och
saknar med avsikt både skarpa LAN-allowlistvärden och
kalibrerad session i Dell-drift. Skapa **ingen**
fysisk timer eller aktivering förrän hela nästa
avsnitts kvalificering är genomförd.

## Kvalificering före fysisk aktivering

- Implementera och verifiera persistent *paused-by-price-owner*-läge,
  inklusive att manuell paus alltid har företräde även vid omstart,
  förlorad kontakt och ny EV-session. Utan detta blockeras
  automatisk återstart från raw `1000`.
- Kalibrera och verifiera bilens användbara kWh/AC-förluster
  och korrekt fysiskt tidsstämplad SoC/anslutning/80 %-mål.
  **Dagens HA-läsare skapar ingen strikt giltig EV-session**.
- Verifiera en enda EVCC-skrivare och FC03-lås, att B2→C2
  efter tidsstyrd start fungerar, och att
  C2→B2 paus faktiskt stoppar ström på Ict2.
  Kontrollera Volvo-appens eget smart-laddschema för
  konkurrerande styrning.
- Separata bevis på huvud-/laddsäkringar, L3-gräns,
  6 A-minimum, BMS-skydd, godkänt
  prisfönster, låg-SoC-förlopp och kontrollerad rollback.
  Fortsätt använda fysisk mätning för gränserna,
  inte synthetic `EV_W` som ett falskt CT-värde.
- Säkerställ att `deadline` faktiskt kan hinna till
  avresetiden; om kvalitet saknas **avstå automatiska
  start/stopp** snarare än att lova 80 %.
- Installerad produktionswebb skiljer sig från Git
  på delar av chart/tooltip. Vid release ska endast
  härledda patchhunkar överföras; **skriv inte över**
  redan driftsatta korrigeringar i chart/tooltip.

**Definition of Done:** egenhet, dokumentation,
små regressionstester, pris- och DST-test, no-write-test,
säkert återställningsunderlag, verklig EVCC-Ict2-
och 15-minuterspriskorrelationslogg samt aktiv driftstatus.



## Driftsättning och verifiering 2026-11-04

**Faktiskt driftsatt på Dell:** Enbart Elpriser-grafens nya
`Bil · prisfönster`-lager. Installerad webb utgår från tidigare
fungerande build `VSy0q51U-ki10RIFbraPw` och kör nu
`-wdTiqvWMysN32fTxf8eF`. De fyra berörda
webbkällfilerna (`app/page.tsx`,
`app/components/PriceChart.tsx`,
`app/components/price-chart/ChartLegend.tsx`,
`app/components/price-chart/ChartTooltip.tsx`) patchades mot
**installerad** kod, inte ersattes med en fullständig Git-kopia.
Produktionsspecifika hover-/tooltip-korrigeringar behölls.
Endast nya `lib/ev-price-windows.ts` installerades därutöver.
Next byggdes med `NEXT_BASE_PATH=/solinteg`, och både
`/solinteg` och `/solinteg/api/inverter` gav HTTP 200
efter uppstart. Aktiva dispatcher, broker, poller, EVCC-/HA-läsare
och ordinarie watchdog förblev i drift.

**Rollback:** rootägd verifierad källfil-/Next-backup och
installationsmanifest finns i
`/var/lib/solinteg-evshadow-rollback/ev-price-chart-preview-20261104/`.
Man kan stoppa endast `solinteg-web.service`, återställa fyra
exakta originalfiler och `.next` från `next-before`, ta bort
endast den nya separat ägda `lib/ev-price-windows.ts` om dess
SHA stämmer, och starta webben igen. Första installationsförsöket
återställdes automatiskt när HTTP-kontrollen kom före Next.js
började lyssna, och en senare verifierad beredskapskontroll
godkände den oförändrade kandidatbuilden utan att ändra andra
tjänster.

**Inte installerat:** `solinteg-ev-price-controller.service`
och `.timer`. Dessa två unitmallar är fortfarande
`LoadState=not-found` i produktion, även om källkoden
har färdiga CAS-/FC03- och prisgrindar. Anledning:
HA-läsaren skapar ingen kalibrerad `EV_SESSION_PATH` och
verifierad energimängd/tidsstämplar till 80 % saknas.

**Tester:** 1 459 Python-unittester OK,
722 Vitest OK (1 överhoppat), ESLint och TypeScript OK,
Next-produktionsbygge med korrekt `/solinteg`-prefix OK,
`systemd-analyze verify` för de inaktiva unitmallarna OK,
och dokumentationsbygge OK. Direkt test av oarmerad
`ev_price_controller.py --once` gav
`state=disabled, writes=0`. Separata manuella EVCC-tester
och den nya pris-kontrollerns frånvarospärr testades utan
fysiska EVCC-telegram.


## Volvo-SoC i Elpriser: historik och illustrativ plan (2026-11-04)

Källa: befintliga produktionshistorikern solinteg-ev-history.service
och den privata SQLite-filen ev-history.sqlite (fortsatt mode 0600).
build_soc_timeline() läser högst 48 timmars HA/Volvo API-observationer
och publicerar endast time, socPct, capturedAtUtc och reportedAtUtc i
gruppläsbara ev-soc-timeline.json (0640, atomisk ersättning) under
samma 0750-runtimekatalog. Inga fullständiga EV-loggar, HA-token
eller privata sensorer görs läsbara för webbapplikationen.
Det sista rapporterade SoC-värdet i varje Stockholm-kvart visas;
saknad kvart blir en lucka, inte 0 % eller påhittad interpolation.
Höstdygnets andra 02:xx får nyckeln #2.

Kvalitet: historikradernas Volvo-SoC har kvalitet
api_recent_field_change_unverified, alltså API-rapporterat,
inte verifierade fysiska BMS-prov. Två tider skiljs åt:
capturedAtUtc när historikern läste status och reportedAtUtc
när Volvo senast ÄNDRADE nivån. UI får aldrig påstå att dessa
är samma mätögonblick.

Serverdelen lib/ev-soc-state.ts läser den sanerade SoC-historiken
och den redan grupp-läsbara HA status.json. En färsk API-observation,
ansluten bil, 80 %-mål samt explicit angivna och obekräftade
kapacitets-/verkningsgradsvärden krävs för prognosen. Saknas
eller åldras underlaget visas historisk kurva utan prognos.
Inga UI-kurvor eller scenario-JSON får påverka fysisk EVCC.

lib/ev-soc-plan.ts beräknar per 15-minutersslot:
  Delta EV-SoC i procentenheter =
  0,25 h * 230 V * 6 A * antagen AC-verkningsgrad
  / (1000 * antagen användbar batterikapacitet kWh) * 100

Befintliga laddlägen skiljs:
- Deadline/Klar till avresa: de billigaste kvartarna väljs före
  närmaste avresetid; dyrare kvarter får användas om 80 % kräver
  det. Om ens kontinuerliga 6 A inte räcker visas tydligt att
  80 % inte nås inom horisonten.
- Economy: samma fullständiga köppris och prognosrisk som
  husoptimeraren, billigaste kvartilen och valfritt maxpris,
  ingen avresetidsgaranti. Både beräknad energi och SoC begränsas
  till 80 %.

Historisk Volvo-SoC visas SOLID och framtida SoC STRECKAD, med
egen färg och egen Bil-SoC-lagerknapp, på samma högra 0–100 %-
axel som husbatteriet. Tooltip säger Volvo rapporterat respektive
beräknad bil-SoC. Prisfönstren använder samma hypotetiska
laddkvartar som framtids-SoC när färsk session finns.
Ingen förändring av husoptimerarens köp, sälj eller SoC.

Prognosen är ILLUSTRATIV: 230 V nominellt, 6 A, antagen
kapacitet/verkningsgrad, API-rapporterad start-SoC och
ingen aktiverad EVCC-priskontroller. Dells aktuella historiska
HA-scenario använder 81,608 kWh och 90 % AC-verkningsgrad,
men dessa är INTE kalibrerade. De visas som antaganden.
För aktiv plan krävs riktig kalibrering, korrekta BMS-tider,
EVCC-stopptest och persistent ägarskap för manuell paus.

Rollback: backupa webbens berörda originalfiler och tidigare
.next-bygge; backupa historikerns enda ev_history.py separat.
Installera/restarta endast webb och historian; dispatcher,
broker, Modbus, EVCC-skrivning och befintliga vakter förblir
orörda.



### Verifierad Dell-release 2026-11-04

- Produktionswebben uppgraderad från build-ID
  `-wdTiqvWMysN32fTxf8eF` till
  `aF6E6oCYGEE8dqY_UOu2Z` med
  `NEXT_BASE_PATH=/solinteg`. Vid verifieringen var både
  `/solinteg` och `/solinteg/api/inverter` HTTP 200,
  och sidans serverrenderade HTML innehöll `Bil-SoC`.
- Historikern är enda nya datakällan och publicerade
  **193 sanerade kvartsvärden**, där sista Volvo-rapporterade
  nivån vid release var **56 %**. JSON mode 0640,
  privat SQLite mode 0600. Tjänstens dataåtkomst
  för webbanvändaren `solinteg` verifierad separat.
- Exakt fyra webbkällor och två nya rena TypeScript-moduler
  installerades med SHA-verifiering, plus historikerns
  enda uppdaterade Python-fil. Den tidigare
  produktionsspecifika tooltip-/hover-fixen bevarades.
  Endast `solinteg-web.service` och
  `solinteg-ev-history.service` startades om.
- Rollback och byteexakta original finns under
  `/var/lib/solinteg-evshadow-rollback/ev-soc-chart-20261104/`,
  inklusive gamla `.next` och installationsmanifestet
  `status=deployed_verified`. Två tidigare avbrutna
  Next-byggen återställdes automatiskt. Grundorsaker:
  ett saknat typimportberoende i produktionsinstallationen
  och rootägd `.next/trace`. Slutbygget kördes i en
  ny tom `.next` som ordinarie `solinteg`-användare.
- Live dispatcher, Modbus-broker, poller, EVCC-observatör,
  Volvo-läsare och ordinarie watchdog var fortsatt aktiva.
  EVCC-prisstyrningens service/timer var fortfarande
  **not-found** och inga fysiska EVCC-/inverterskrivningar
  utfördes.
- Verifiering före release: 1 461 Python-unittester OK,
  **734 Vitest godkända (ett överhoppat)** inklusive
  12 nya riktade tester, TypeScript, ESLint, Zensical
  och Next-produktionsbygge utan blockerande fel.

## Känd lucka

**Delvis åtgärdat:** den härledda huslastprognosen använder
nu separat vardags-/helgprofil och drar av verifierad
EVCC-Ict2-effekt från kompletta historiska UTC-timmar.
För tider utan mätbar EVCC-ström ligger rålasten kvar,
vilket är avsiktligt konservativt. Nät-CT, råtelemetri
samt visad huslast och separat bil är oförändrade.
Se [lastprognosens ADR](adr-2026-11-04-weekday-ev-load-profile.md).

## Offlineutveckling: EV-prissession och persistent pausägarskap 2026-11-04

**Status: implementerat och Python-verifierat i Git `Sundsvall`, inte
installerat, aktiverat eller fysiskt kvalificerat.** Dokumentationspreflight
utgick från ren `3912b94`, en worktree, inga stashes/omergerade grenar och
`origin/Sundsvall` synkroniserad. Aktuell driftskontroll: de befintliga
`solinteg-broker-control`, `solinteg-dispatch`, `solinteg-poller`,
`solinteg-web`, `solinteg-ev-ha`, `solinteg-ev-observer` och
`solinteg-ev-history` var aktiva; EV-priskontrollerns service och timer
var fortfarande `not-found`. EVCC rapporterade färsk `C2`, PWM
konfig/pilot `100/100` och Ict2 cirka 6,1 A vid read-only-kontrollen.
**Inga produktionsfiler, env-gates, tjänster eller enheter ändrades.**

### Energiunderlag och sessionskontrakt

- Den installerade Volvo/HA-läsaren använder
  `oem_timestamp_role=last_changed_not_sampled`, och skriver endast
  `physical_control_eligible=false` till den strikta sessionen.
  Ett nytt HA-/Volvo-API-GET bevisar inte ett nytt BMS-prov.
- Verklig **read-only** 72-timmarskontroll via
  `ev_ac_quality_audit.audit()` gav 8,934851 kWh **delvis täckt,
  nominellt uppskattad** AC-energi över 23 388,74 s och 8 628 synkade
  historikrader. 779 relevanta Volvo-rader innehöll 11 rapporterade
  SoC-nivåer med totalt 10 procentenheters uppgång. Rapporten är
  `diagnostic_not_calibration`: varken kalibrerat AC/DC-förhållande,
  användbar fysisk kWh eller nettolagrad BMS-energi kunde fastställas.
  Dessa observationer får **inte** multipliceras upp till en godkänd
  sessionsenergi eller medge fysisk EVCC-skrivning.
- `read_ev_session()` för **verkställbar** session kräver nu en
  oberoende `verified_physical_sample`, uttryckligen
  `physical_control_eligible=true`,
  `energy_requirement_quality=independent_ac_calibration`, en högst
  20 minuter gammal faktisk mätnivå, en högst 90 dagar gammal
  verifierad kWh-per-SoC-procentenhet-kalibrering, unik
  `session_id` och heltalig `connection_epoch`. Kalkylen till exakt
  80 % kontrolleras mot levererat `remaining_ac_kwh` och avvisar
  motsägelser, avbrott och inaktuella värden. Enbart kapacitetsantagande
  (t.ex. 81,608 kWh/90 %) eller `230 V × Ict2` kvalificerar inte.
  Båda beslutade lägena `economy` och `deadline` bibehålls.
- **Nästa datakvalificering:** instrumentera en *oberoende kalibrerad
  AC-kWh-mätare* med tidsstämplad laddning/icke-laddning, samkör den med
  EVCC C2/Ict2 och verkliga, källtidsstämplade Volvo/BMS-SoC-steg över
  flera obrutna laddintervall. Härled och begränsa robust kWh per
  SoC-procentenhet med mätosäkerhet; avvisa luckor, start/stop från
  andra system, kabelbyten och äldre än angivna prov. En ny
  verifierad källa måste ge `session_id` och `connection_epoch`
  per fysisk inkoppling. Befintlig `ha_ev_session.once()` får
  **inte** märka om API-uppdateringar till verifierad fysik.

### Pausägarskap och manuell överordning

`ev_price_pause_owner.py` inför ett persistent tillståndsprotokoll:
`pending` före FC10 paus, `owned` först efter säker readback,
`releasing` före återstart, `released` efter säker readback och
`revoked` vid förlorad tillit. 0600-JSON skrivs med atomisk rename,
fil-/katalog-fsync. Ett separat icke-blockerande exklusivt owner-lås
skyddar parallella kontrollinstanser; EVCC-observatörens befintliga
`evcc-reader.lock` skyddar FC03/FC10. Osäker skrivning, omstart
i `pending` eller `releasing`, korrupt ägarfil, felaktig PWM,
ändrat läge, ny session, kabelbyte, gammal telemetri eller utebliven
källkontinuitet ger **ingen automatisk start och ingen blind retry**.

**Absolut aktiveringsblockerare:** `read_manual_intent()` kräver en
oberoende, förlustfri, färsk (högst 90 s) händelsekälla med explicit
`manual_paused`, monoton `manual_generation`,
`continuous_since_utc`, `session_id` och `connection_epoch`.
Ett manuellt stopp eller ändrad generation återkallar ägandet
permanent för den sessionen. Denna producent **finns inte ännu**
och kan inte ersättas med EVCC:s PWM, Volvos
`charging_status` eller en HA GET-tidpunkt: särskilt en paus i
Volvo-appen *under en redan prispausad period* kan förbli osynlig.
Den framtida producenten måste dokumentera att **samtliga** manuella
start-/stoppvägar täcks, skydda sitt runtimebevis mot kontrollerns
egna skrivningar (avsedd read-only-källa `/run/solinteg/ev-manual-intent.json`),
samordna händelser med ägarlåset och kunna visa kontinuitet även över
återstarter. Tills detta bevis är etablerat vägras såväl nya
automatiska prispauser som återstart.

### Offline-test, fysisk spärr och framtida GO

- 75 riktade Python-tester och därefter hela Python-sviten med
  **1 480 tester OK**. Falska/stale EVCC- och nätvärden,
  förlorad session, ny inkoppling, återstart, ändrade laddlägen,
  högt/lågt pris, manuell paus, okänd pausägare, extern PWM,
  avbruten/oklar FC10, två kontrollinstanser och trasigt auditbevis
  gav förväntade no-write-/revoke-fall. EVCC i dessa tester var
  *mockad*, inte fysiskt kontaktad.
- Vid separat framtida och **uttryckligen godkänd** aktivering får
  en enda EVCC-skrivare via **FC10 register 0x0014** endast utföra
  `raw=1000` (prispaus) eller `raw=100` (6 A återstart);
  FC03 jämför före/efter. Det är **inte** nya kommandon till
  Solintegs inverter, Modbus-broker eller husbatteri. Vid frånkoppling,
  B2 utan belagt ägande, C2 utan tillräcklig faktisk ström,
  mätfel och pris-/säkringsspärr blir åtgärden ingen skrivning.
- Produktions-GO kräver separat verifiering av en äkta laddsession,
  kWh-kalibrering, fullständig manuell interventionstäckning,
  EVCC B2↔C2 och Ict2-fall/stigning, strömbudget/säkringsmarginal,
  Volvo-appens egna smartladdschema, rollback, och sanningsenlig
  journal över utförd FC10. **Ingen fysisk aktivering är godkänd**
  av detta offlinearbete. Den aktiva Modbus-brokern, lastbalansering,
  50 W EMS-begränsning, SoC-/fasvakter och ordinarie watchdog
  är oförändrade. Kända risker i befintligt failure-scenario kvarstår.

### Volvo Smart Charging – on/off-grind, endast offline 2026-11-04

Den tidigare separat verifierade, privata skrivskyddade Android/Chronos-
klienten `smart_schedule_read_only.py` använder mobilappens
`ChargeLocationService/GetChargeLocations`. Enligt dekompilerad
appkod är `hasIsOptimisedChargingEnabled()` närvaromarkören och
`getIsOptimisedChargingEnabled()` värdet; explicit protobuf #7
`false` får inte förväxlas med saknat fält. `availableOptimisedCharging`
(#9) beskriver kapacitet, **inte** på/av.

`ev_volvo_smart_charging.read_smart_charging()` kontrollerar den
**sanerade** PoC-filen (ingen VIN, token eller plats-ID), inklusive
`target_vin_sha256` mot ett separat privat konfigurerat
`EV_VOLVO_TARGET_VIN_SHA256`. PoC:n skickar VIN-bundet läsanrop
och exporterar endast SHA-256 av begärt VIN. Endast en laddplats,
ingen väntande plats-/timer-/ampereändring, inaktiv global timer
samt inga aktiva lokala timer-/avresescheman. Bara explicit
booleskt `optimized_charging_enabled=false` och ett `captured_utc` högst **10 minuter** gammalt får ge `off`.
`true`, för gammalt/saknat/felaktigt svar, VIN-missmatch, aktiv
timer eller oklarhet ger `on` eller `unknown`.

Priscontrollern läser från
`EV_VOLVO_SMART_CHARGING_SNAPSHOT_PATH`
(default `/opt/solinteg/ev-shadow-runtime/var/volvo-smart-charging.json`).
Alla andra utfall än `off` ger **0 EVCC-skrivningar**; eventuellt
`owned` pausägarskap återkallas permanent. Grinden kompletterar,
men ersätter **inte**, oberoende mätt EV-session, manuell-intent-källa
och fysiska EVCC-/säkringstester.

**Ny liveverifiering 2026-11-04 kl. 17:49 UTC:** Efter att användaren
stängt av Volvos smartladdning gav ett nytt godkänt mobilapp-OAuth-flöde
`GetChargeLocations` med en laddplats och explicit protobuf #7 `false`.
Laddplatsens avgångstid 07:00 är nu inaktiv, lokala timers saknas,
global timer/pending global timer saknas, laddplats-pending=0,
`GetAmpLimit` gav 8 A utan pending. `set_amp_limit_calls=0`;
inga fordons-, EVCC- eller inverterkommandon. Mobilappautentiseringens
OTP, åtkomsttoken, callback och VIN exponerades inte i loggar.

Den privata PoC:n skriver nu till en separat 0600-fil
`smart-schedule-current-readonly.json` så daterad historik inte
fortsätter skrivas över. En ny explicit enstegs-export
`scripts/tools/publish_ev_volvo_smart_charging.py` filtrerar
originalet till exakt de nödvändiga interlock-fälten. Den tar bort
plats-ID, alias och övrig metadata; validerar ursprung, VIN-hash,
friskhet, lokala/global timers och pending; avvisar osäkra källfiler,
symlänkar och gammal data. Publikationen sker med `mkstemp`,
0600, runtime-katalogens ägare/grupp, fsync och atomisk rename.

### Aktiverad, enbart skrivskyddad Volvo-statusinsamling (2026-11-04)

I Volvo Cars Android 7.5.0 bekräftades att
`defpackage/vdd.java` använder en DPoP-bunden token-API med
`grant_type=refresh_token`, `refresh_token` och valfri `scope`.
`AuthenticationData.java` håller refresh-token, medan
`AuthenticationRepositoryImpl` hanterar lyckat refresh-svar.
En opt-in OAuth-inloggning gav `REFRESH_ENROLL_SAVED=True`.
DPoP-nyckel och refresh-token lagras endast i en privat
0600-vault `~/.local/share/volvo-smartcharging-oauth/.smart-schedule-refresh-vault.json`
med 0700 föräldrakatalog; inga hemligheter, rått VIN eller access-token
sparas i publika Git-filer eller loggar.

**Live verifierat:** `smart_schedule_refresh.py` har förnyat
access-token med HTTP 200 utan OTP och gjort ett nytt, läsande
`GetChargeLocations` med `optimized_charging_enabled=false`.
Volvos svar innehåller **ingen ersättande** `refresh_token`.
I enlighet med OAuth 2.0 §6 behålls den tidigare giltiga
refresh-token när ingen ny returneras; vid faktiskt tokenbyte görs
0600, atomisk fsync/rename innan efterföljande läsningar.
DPoP `cnf.jkt` ska matcha den skyddade privata nyckeln; avvikelse,
saknat/ogiltigt svar och förlorad kontinuitet stoppar publikationen.

En separat `solinteg-volvo-smart-status.service` och
`solinteg-volvo-smart-status.timer` installerades på Dell.
Timer är **enabled/active**, `OnUnitActiveSec=5min`.
Den privata OAuth-processen körs som `User=hakan` med
`NoNewPrivileges=true`. Root-körd `ExecStartPost=!`
använder en rootägd, installerad kopia av publiceraren
och dess granskade verifieringsmodul; den läser en 0600-källfil
och skriver enbart sanerad JSON atomiskt till
`/opt/solinteg/ev-shadow-runtime/var/volvo-smart-charging.json`.
Filen har mode 0600, ägare `solinteg-evshadow:solinteg`,
och kan läsas av det avsedda servicekontot.
Första manuella systemd-körningen och nästa timerutlösta körning
slutfördes med `Result=success` och `device_writes=0`.
Ingen EVCC-priscontroller-service/timer är installerad
(`LoadState=not-found`).

**Failure- och rollbackpolicy:** En misslyckad refresh-/Chronos-
körning lämnar föregående status oförändrad; efter max 10 minuter
ger kontrollerns färskhetsgrind `unknown` och **ingen EVCC-skrivning**.
Token-återkallelse och förändrat app-API kan därför kräva ny
engångsinloggning för att återansluta. Ingen omstartsautomatik
återstartar bilen. För att stoppa insamlingen:
`sudo systemctl disable --now solinteg-volvo-smart-status.timer`.
Den nya statusinsamlingen är **inte** den förlustfria
händelsekälla som behövs för manuella stopp via Volvo-appen.

**Kvarstående EVCC-GO-blockerare:** en oberoende kalibrerad fysisk
laddsession med korrekt kWh-behov, förlustfri manuell stopp-/start-
händelsekälla, och fysiska EVCC-/fasbudgetstester. Före godkänt GO
är EVCC-prisstyrningen fortsatt oinstallerad och oarmerad.

**Verifiering:** 1 496 Python-tester för Solinteg, 18 privata
OAuth-/DPoP- och Chronos-tester, `systemd-analyze verify`,
och två live lyckade schemaläsningar efter tokenförnyelse.
Ingen fysisk EVCC-, bil- eller inverterstyrning utfördes.

### Fortsatt EVCC-priskvalificering – verklig drift, 2026-11-04

Efter explicit mål att starta automatisk EVCC-prisstyrning utfördes en ny
**uteslutande läsande** förkontroll på Dell. Den nya
`scripts/tools/ev_price_activation_preflight.py` har ingen hårdvaruskrivare,
ingen ARM-kod och tar inte emot någon flagga som kan slå på regleringen.
Den bedömer aktuell verklig Volvo-/EVCC-/L3-telemetri med samma strikta
session-/manual-/pris-/Smart Charging-validerare som verkställaren.
Det sista kvalificeringssteget, fysiska paus-/återstart-/feltester, är
**avsiktligt hårt blockerat** i rapporten tills det har genomförts och
verifierats separat. Detta skript får **aldrig** användas som en
produktionsaktiveringsgenväg.

**Kontrollerad Dell-avläsning:** `solinteg-volvo-smart-status.timer` är
fortsatt `enabled/active`; `GetChargeLocations` ger explicit `off`
inom 10 min. EVCC-FC03 visar färsk `C2`,
`configured_pwm_raw=pilot_pwm_raw=100` och Ict2 omkring 6 A; färska
L3-/nät-CT-uppgifter accepteras i preflighten. Den privata, i APK
7.5.0 dekompilerade `BatteryService/GetLatestBattery` lästes **en
gång skrivskyddat** med redan auktoriserad DPoP-refresh. Det
VIN-matchade svaret gav 62 % SoC, 229 V, 6 A, 1 374 W,
`ChargingStatusV2=CHARGING`, `ChargerPowerStatus=PROVIDING_POWER`
och noll fordonskommandon. Jämförelsen med schablonen
`230 V × 6 A = 1 380 W` stöder den nominella effektmodellen
vid **ett ögonblick**, inte en oberoende mätning av ackumulerad
energi, verifierad batterikapacitet eller en källa till manuella stopp.

**Energikvalitet:** Befintlig, oförändrad `ev_ac_quality_audit.audit()`
gav under senaste 24/72 h `12.391522` kWh *uppskattad partiell*
AC-täckning i `32 409,9` sekunder; 15 rapporterade Volvo-SoC-nivåer
motsvarar +14 procentenheter. Den är korrekt märkt
`diagnostic_not_calibration`: EVCC:s Ict2 beräknas med
**antagna** 230 V och PF=1, intervall saknas och SoC är
avrundat/molnrapporterat. `verified_net_battery_dc_kwh`,
`calibrated_ac_dc_efficiency` och
`calibrated_usable_capacity_kwh` förblir NULL.
`strict-session.json` är aktivt **spärrad** med
`physical_control_eligible=false` och
`OEM last-change timestamp is not a verified BMS sample`;
`/run/solinteg/ev-manual-intent.json` saknas helt.
Ingenting av detta har förfalskats/promoverats till fysisk
styrsignal.

**Laddmålets genomförbarhet:** 62→80 % enligt HA:s okalibrerade
scenariodata kräver ca 16,322 kWh AC, vilket vid 6 A/230 V motsvarar
~11,83 timmars kontinuerlig laddning. Vid provernas tid återstod
~10,89 h till avresa 07:00; `deadline` kan därför **inte lova 80 %**
med denna ström, och en ytterligare prispaus försämrar marginalen.
Bilen lämnades kvar laddande på befintliga 6 A.

**Förlustfri manuell stoppsignal saknas fortfarande.** APK:s
`Battery.ChargingStatus[V2]` visar `CHARGING`, `IDLE`,
`SMART_CHARGING_PAUSED` m.fl. men ingen verifierad avsändare
eller beständig händelse för att användaren tryckt *Stoppa* i
Volvo-appen **medan EVCC:s PWM redan är `1000`**. Den
periodiska laddstatusen kan vara identisk före/efter ett sådant
stopp. Att sätta `lossless=true` utifrån FC03 eller periodisk Volvo
status vore fel och riskerar otillåten automatisk återstart.

**Besked:** `NO-GO` för automatisk prispaus/**återstart**.
Nästa kvalificeringssteg är (1) oberoende kWh-referens och en
sammanhängande laddsession med verifierade mättider/SoC,
(2) faktisk förlustfri auditering av manuellt app-stopp även
under redan prispausad EVCC eller en bevisat säker alternativ
ägarskapsmodell som inte återstartar app-stopp,
(3) kontrollerad fysisk `1000`-paus och `100`-återstart
vid 6 A plus nätfel/omstart/fasvakt/rollback, med separat
verifiering av hela kedjan. Ingen EVCC-priscontroller har
installerats eller armerats och inga FC10-skrivningar utfördes.

### Kabel bort/åter i EVCC – ytterligare sessionsgrind 2026-11-04

Den befintliga FC03-observatören kan nu, **utan extra tjänst, Modbus-
anrop eller styrkommando**, följa `A1` (inte ansluten) och
`B2/C2` (ansluten) med tvåprovskvittens, beständig
`connection_epoch` och spårbar observerad ur-/inkoppling.
Priskontrollerns opt-in `EVCC_CABLE_TRACK_PATH` kontrollerar
färsk, stabil kabelanslutning och knyter en prispaus till just denna
kabel-epoch. En ny epoch eller ett kabelavbrott **återkallar**
gammalt pausägarskap; gammal paus får inte ge automatisk
återstart efter ny anslutning.

Historisk FC03-replay verifierade `A1`→`B2` den
4 november med `configured_pwm_raw=1000` men
`pilot_pwm_raw=267`. Detta räcker för **fysisk
anslutningsindikation**, inte för att tillåta
`1000`→`100`-kommandot. Vid `raw=100` kan bilen starta
utan nytt kommando när kabeln återansluts och bilen accepterar
laddning; vid `raw=1000` måste en framtida controller göra
en *ny* verifierad prövning innan återstart.
Periodisk FC03 kan missa en kort ur-/inkoppling och säger
inget om manuella stopp i Volvo-appen. Den separata förlustfria
manuell-intent-grinden är därför **oförändrat obligatorisk**.
[Separat detalj- och rollbackrunbook](../evcc-cable-tracking-2026-11-04.md).

### Volvo-appens manuella stopp och BMS-prov (2026-11-04, nytt delsteg)

Den dekompilerade Volvo Cars 7.5.0-klienten och en ny **enbart
skrivskyddad** livekontroll av `ChargeNowService/
GetEnabledOverrideChargeTimerAction` gav
`status=1`, `enabled_action=3 (NOT_APPLICABLE)` och
`override_timer_state=1 (NOT_ACTIVE)`. Det betyder att
ingen *Charge now*-överstyrning är aktiv för den aktuella bilen,
**inte** att ett manuellt stopp har detekterats eller att man
säkert kan återstarta EVCC. Dekompilerade `StopCharging`-anrop
återfanns i den offentliga laddningstjänsten/Volvo Wallbox-koden,
inte som en verifierad lokal XC40-stopp-händelseström.
Publik Volvo-support beskriver också fjärrstopp separat för
Wallbox och publika laddare. Vi kan **inte** dra slutsatsen att
alla möjliga bilstopp är omöjliga eller att appens faktiska
beteende är täckt.

En ytterligare read-only mobilapp-DPoP-förfrågan till
`BatteryService/GetLatestBattery` gav 64 % SoC,
`CHARGING`, 6 A, 231 V och 1 386 W med separat rapporterad
`Battery.timestamp` **52 sekunder gammal**. En senare
observation av samma serverbaserade BMS-stämpel var **462 sekunder
gammal** trots fortsatt status `CHARGING`. Vi inför därför
ingen falsk kontinuitetsgaranti. `volvo_battery_read_only.py`
(private, ej Git) kräver VIN-match, ändamålsenlig protobuf,
rimlig SoC och **max 300 s gammal** batteritidsstämpel.
Valfri `VOLVO_READONLY_BATTERY_ENABLED=1` utnyttjar den
**redan existerande** femminuters OAuth/Chronos-tjänsten; ytterligare
Battery/ChargeNow RPC är båda enbart läsningar. Om tillägget
misslyckas fortsätter tidigare verifierad Smart Charging-status
att uppdateras, men det äldre BMS-provet publiceras inte.

Den publika root-ägda statusexportören `publish_ev_volvo_smart_charging.py`
har nu en extra **valfri fältallowlist** som kan lägga
`battery_readonly` i samma privata status-JSON (mode 0600).
Verifieringen kräver source, VIN-SHA256, observerad tid,
batteriprovtid, giltig SoC och `device_writes=0`; giltiga
statusfält saneras och återskapas, extra metadata/tokens eller
rått VIN släpps inte igenom. `manual_stop_lossless=false`
och `metered_ev_energy_available=false` skrivs explicit.
Stale eller ogiltigt BMS-prov **utelämnas** utan att
Smart Charging-statusen skrivs om till påhittat värde.
`ev_price_activation_preflight.py` redovisar dessa villkor
separat som `fresh_chronos_battery_sample_observed`;
godkänt test är **inte** en ersättning för kalibrerad kWh eller
förlustfri app-/manuell-intent-källa.

**NO-GO oförändrat för fysisk EVCC-prisstyrning:** en äkta
AC-kWh-referens, en verifierad session/tidsstämplad SoC-serie,
säker detektion eller särskilt godkänt sessionellt
samtycke vid manuellt Volvo-stopp och kontrollerade
FC10-pauser/återstarter kvarstår. Enbart Volvo-statusläsningen
får aktiveras. 1 526 Python-tester och 25 privata tester OK,
alla utan EVCC/Volvo/inverter-skrivkommando.

**Driftkontroll:** Efter en byteexakt SHA-kontroll av tre installerade
filer stoppades endast Volvo-status-timern tillfälligt. De tre
filerna uppdaterades atomiskt och originalen med manifest sparades i
`/var/lib/solinteg-evshadow-rollback/volvo-bms-readonly-20261104/`
(rootägt, katalog `0700`). `systemd-analyze verify` godkände
servicen; timern återstartades och är `enabled/active`.
Den första manuella servicekörningen gav `Result=success`,
`refreshed=true`, Smart Charging `off`, `device_writes=0`.
Det kompletterande BMS-anropet avvisades som `ValueError` när
provtiden var över 300 s gammal; `battery_readonly` saknas därför
i den filtrerade runtimefilen vid denna kontroll. Detta är ett
**förväntat fail-closed-utfall**, inte ett lyckat färskt BMS-prov.
`solinteg-ev-price-controller.service` är fortsatt `not-found`.
Ingen EVCC-, bil- eller inverterstyrning ändrades.

### EVCC2:s pilot vid prispaus – offlinekorrigering 2026-11-04

Det tidigare antagandet att `configured_pwm_raw == pilot_pwm_raw`
alltid gäller vid paus var **fel** för installationens EVCC2-firmware.
Den befintliga, read-only EVCC-historikern visade följande
`FC03`-kombinationer (ackumulerad replay vid granskningen):

| CP | Konfigurerad PWM | Observerad pilot | Prover | Tolkning |
| --- | ---: | ---: | ---: | --- |
| `C2` | 100 | 100 | 1 779 | Laddkandidat, ca 6 A |
| `B2` | 1000 | 267 | 178 | Ansluten paus, ca 0,1 A när Ict2 finns |
| `A1` | 1000 | 267 | 4 259 | Frånkopplad; **ej återstartbar** |
| `B2` | 100 | 100 | 1 | Kort tvetydig övergång; **avvisa** |
| `C2` | 1000 | 267 | 1 | Kort tvetydig övergång; **avvisa** |

I totalt 6 442 historikposter validerade den nya **gemensamma
rena** `observed_evcc_mode()` 1 229 som `charging_6a` och
16 som `paused_connected`; 5 197 avvisades. Bland de
avvisade fanns 550 äldre `C2`- och 162 äldre `B2`-poster
utan alla tre Ict-fasvärden. Det är **täckningsbrist i
historikerfält**, inte verifierade felaktiga aktuella
FC03-mätningar. Endast kompletta färska mätningar används.

`scripts/services/evcc_ascii.py` definierar nu
`PAUSED_PILOT_RAW=267` som en **observerad pilotindikering,
aldrig skrivbörvärde**, och `observed_evcc_mode()` kräver
exakt `C2 / 100 / 100 / Ict2 5,5–6,5 A` eller
`B2 / 1000 / 267 / Ict2 0–0,5 A` med
Ict1/Ict3 vardera 0–0,3 A. Alla andra lägen,
frånkoppling `A1`, saknade/ogiltiga fasvärden och
felklassning är **NO-GO**. Samma kontroll används av
`ev_price_controller.choose_action()` och
`ev_price_activation_preflight.py`.

I den framtida **schemalagda** FC10-drivern görs nu en
ny fysisk läsning **under EVCC:s befintliga seriallås**:
paus får endast skickas om laddning just då är
verifierad `C2/6 A`, och återstart får endast skickas
om ansluten paus just då är verifierad `B2/1000/267`.
Schemalagt skrivbörvärde begränsas dessutom till endast
`100` (6 A) eller `1000` (paus).
Efter en paus krävs `B2/1000/267` och högst
0,5 A vid readback; osäkert ACK, läsfel, fortsatt ström
eller oväntad pilot ger **osäkert utfall utan retry**.
Efter `100` krävs även att bilen fortfarande ses ansluten.
`B2/100/100` med låg ström får kvittera en **ännu inte
fysiskt startad** laddare direkt efter kommandot; det används
aldrig som förkontroll för en ny åtgärd. Om en urkoppling
`A1` ses före eller efter FC10 markeras resultatet
**osäkert och får inte upprepas automatiskt**. En redan
skickad FC10 kan dock inte göras ogjord genom läsning:
just detta race återstår att kvalificera fysiskt.
Efter `100` bekräftar föraren fortfarande börvärdet,
inte att bilen faktiskt har börjat ladda.

**Driftbeslut:** Det nya priscontroller-/FC10-spåret
är fortfarande **endast källkod och offlineverifierat**.
Inget EVCC-kommando skickades i denna ändring. Detta
löser den tidigare falska pilotspärren vid
normala pausprover, **inte** de kvarstående hindren:
förlustfri Volvo-app-manualstoppkälla, verifierbar
kWh-/SoC-session, fysiskt skyddat paus-/återstartprov,
eller driftens väggbox-/fassäkringsverifiering.
Befintlig EVCC-observatör, Volvo-statuspoller, inverter-
och husbatteridispatch ändras inte.
