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

## Känd lucka

Nuvarande historiska förbrukningsprognos för huset
kan fortfarande innehålla EV-kWh eftersom råmätningen
innehåller all last. Detta måste justeras i separat
deriverad historik/forecast-källa, aldrig genom att
skriva om råmätaren. Den separata UI-huslasten
drar redan av bilens Ict2-effekt.
