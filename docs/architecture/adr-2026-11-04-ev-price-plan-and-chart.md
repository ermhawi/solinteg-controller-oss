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
