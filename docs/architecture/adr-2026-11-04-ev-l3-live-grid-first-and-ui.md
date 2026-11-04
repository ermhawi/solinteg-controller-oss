# ADR 2026-11-04: EV/L3 live grid-first, separat bil i Systemstatus och avveckling av shadow

**Status:** I drift på Dell, med återstående mätnoggrannhets- och reservbegränsningar.
**Gren:** endast `Sundsvall` → `origin/Sundsvall`; aldrig `main`.
**Fysisk säkerhet:** Ordinarie Solinteg-dispatcher, ensam Modbus-broker,
SoC-/BMS-återhämtning, L3-laddgräns och ordinarie watchdog kvarstår.

## Beslut och mätmodell

En ansluten, verifierat laddande EVCC på L3 ger `P_EV≈230 V × Ict2`
(`C2`, färsk FC03, PWM-konfiguration och pilot identiska, enfas
Ict2 och konsistent kanalström). Det är en **effektuppskattning**,
inte en kalibrerad aktiv effektmätare eller uppmätt nätandel.

Råvärdet `house_load_w = inverter_ac_w - grid_w` **inkluderar bilen**.
När bilen redovisas separat ska den inte dubbelräknas:

```text
house_non_ev_w ≈ max(0, house_load_w - ev_power_est_w)
house_and_ev_w = house_non_ev_w + ev_power_est_w  (inom uppskattningsfel)
```

Denna **härledning** används i systemstatus och i
`ev_l3_live_policy.py`. Den görs **inte** genom att skriva om
pollerns `house_load_w`, `grid_w`, CT, SQLite-telemetri eller
andra råmätningar: nätimport och fasskydd måste bygga på verkligt
uppmätt totalbelastning.

**Energiprioritet:** Solel till icke-EV-huset och husbatteriet;
bilen ska ha nätprioritet tills husbatteriet oberoende verifierats
fullt. Då får överskott efter huset gå till bilen före export.
Källorna är elektriskt blandade via en balanserad växelriktare.
Separat EVCC-Ict2 och L3-nät-CT kan inte fysiskt bevisa exklusivt
nät-/sol-/batteriflöde.

## Systemstatus: exklusiva indikatorer, inte exklusiva elektroner

`lib/evcc-car-flow.ts` och både desktop- och mobilkomponenten:
`PowerFlowCard.tsx`/`MobileOverview.tsx` visar
**Hus exklusive bil** och en separat EV-effekt från Ict2.
Elnät och total energimätning ändras inte.

Pilarna till bilen är **ömsesidigt uteslutande statusindikatorer**:
- `Nät → Bil` endast om L3-nätimport kan täcka beräknad EV-effekt
  inom 125 W tolerans för nominell spänning/mätfel;
- annars `Hus/sol → Bil` om minst en del av efterfrågad EV-effekt
  inte förklaras av L3-nettoimport;
- vid saknad/gammal/opålitlig EVCC- eller L3-data aktiveras **ingen**
  bilpil. Etikett och hjälptext säger uttryckligen **indikativt**;
  detaljer visar gränserna `nät högst` och `lokalt minst`.
  Detta är en klassificering, inte ett påstående att två fysiska
  energikällor aldrig kan leverera samtidigt.

UI-helpers drar av EV när **bilen faktiskt laddar**, oberoende av
vilken pil som är aktiv; att bara dra av i `grid`-läge skulle fortfarande
dubbelräkna när den lokala indikatorn visas.

## Live-dispatch och begränsningar

`DISPATCH_EV_GRID_LIVE=1` är aktiverat i installerad dispatcher.
`ev_l3_live_dispatch.coordinate()` körs i befintlig enda
dispatcher **efter** fysisk SoC-recovery, med befintliga
batteri-/fasvakter och Modbus-brokern som enda fysiska skrivväg.
Ingen EVCC-autonom skrivfunktion har aktiverats.

- **Historisk första liveversion** använde endast 300–600 W
  husbatteriladdning och växlade tillbaka till 50 W urladdning
  vid en solöverskottströskel. Den prioriterade bilen på L3
  otillräckligt. **Se korrigerad, driftsatt L3-reglering nedan.**
- Vid lägre solelöverskott begränsas ett planerat **positivt
  urladdningsbörvärde över 50 W** med balanserat AC, CT3, PV och
  `non_ev_l3 = -meter_l3 + inverter_ac/3 - ev_power_est`.
  Begränsningen får aldrig öka ursprunglig urladdning eller överskrida
  beräknade importgränser 3 000 W per fas och 6 000 W totalt.
- Charge/recovery/hold bevaras. Stale data, `B2`, PWM-konflikt,
  annan fas, oväntat inverterläge eller felaktiga summeringar
  ger inte en ny fysisk åtgärd.
- Det befintliga **50 W EMS-minimumet kan i verkligheten ge
  cirka 160–260 W urladdning**. När redan planerat 50 W,
  `idle/General`, eller PV växlar snabbt är det **inte** verifierat
  att bilen uteslutande belastar nätet. 20 A säkring skyddas
  inte av denna ekonomiska kontroll; ordinarie skydd behålls.
- Den normala optimizerns **historiska förbrukningsprognos** kan
  fortfarande innehålla äldre billaddning i rå `house_load_w`.
  Den korrigerade EV/L3-liveregleringen ligger efter planen;
  fullständig historisk separering av EV-kWh i plan-/prisprognosen
  återstår, och får inte göras genom att skriva om gamla råmätningar.
  Fullt husbatteri kräver fortsatt oberoende BMS-verifiering.

## Driftsättningsbevis och rollback

**Testat:** 1 437 Python-unittester OK, 714 Vitest OK (ett
överhoppat), ESLint, TypeScript och produktionsbygge godkända.
Båda UI-vyerna och `ev_l3_live_policy` har regressionstester för
dubbelräkning, exklusiva indikatorer, saknade värden, försiktig
discharge-begränsning samt SoC-/fasgrindar.

**Live-moduler:** Endast
`/opt/solinteg/app/scripts/services/ev_l3_live_policy.py` och
`ev_l3_live_dispatch.py` byttes atomiskt, därefter kontrollerad
omstart av `solinteg-dispatch.service`. Installerad dispatcher själv
är fortsatt SHA-256
`979b6e3f51b7d2c6237a528fc00e739376357c0f14d684e2d238fd9a1bf52f10`.
Inga broker-/watchdog-/EVCC-skrivvägar ändrades.
Rootägd återställning finns i
`/var/lib/solinteg-evshadow-rollback/ev-l3-live-house-accounting-20261104/`
med två gamla filers SHA och ursprunglig ägare/mode.

**Webb:** Tre källfiler byttes under `/opt/solinteg/app` och
Next byggdes om som `solinteg`. Installerad server kräver
**`NEXT_BASE_PATH=/solinteg` vid byggtillfället**.
Ett första bygge saknade prefixet och hade fel routning; detta
upptäcktes och rättades under releasefönstret. Ny verifierad build-ID:
`DQ1nR72QYjWkg1psIoU5y`, korrekt
`/solinteg/api/inverter` gav **HTTP 200** med färsk bil.
Exempel: rå total `house_load_w=1900 W`, Ict2=5,9 A
(`ev_power_est=1357 W`), **visad huslast=543 W**;
rå `grid_w` och CT oförändrade. Rootägd rollback med exakt gamla
tre källfiler samt hela förra `.next`-bygget finns under
`/var/lib/solinteg-evshadow-rollback/ev-car-ui-exclusive-20261104/`
(`next-before`). Vid återställning stoppa **endast webben**,
återställ dessa filer och bygget och starta om; nytt Next-bygge
måste få original-`NEXT_BASE_PATH`.

**Retirerade EV-shadow:** Efter verifierad inactive/disabled-status
krypterades 13 käll-/unit-/statusfiler med GPG AES256 till den
kontrollerade USB-monteringen
`/mnt/media-stack/archive/solinteg/2026-11-04-ev-shadow-retirement/`.
SHA-256 av arkivet
`c3be49228b6e777b5c11780cd15ad664b4c6b6fa43f04e16b8194846ad5a4b67`.
Separat **dekryptering och byteexakt återläsning** godkändes **innan**
fem inaktiva systemd EV-shadow-enheter, deras separat rootägd
observatörskod och den oanvända shadow-planeraren togs bort.
`systemctl daemon-reload` utfört, historiktjänstens gamla
`After=... solinteg-ev-shadow.service` uppdaterat utan tjänsteomstart.
Rotägd terminal tombstone:
`/var/lib/solinteg-evshadow-rollback/ev-shadow-retirement-20261104.status.json`.
**Aktiva** `solinteg-ev-observer`, `solinteg-ev-history`,
`solinteg-ev-ha`, broker, poller, vanlig watchdog och dispatcher
är kvar. Äldre rootägd rollback och schemabekräftelser behålls.

## Driftförsiktighet och öppna frågor

Vid releasen låg husbatteriets SoC kring **18–19 %** medan bilens
Ict2 var cirka 5,9–6,0 A. Perioder med 50 W EMS-börvärde och
måttlig verklig urladdning kvarstår; detta är **inte** en garanti om
0 W batteri→bil. Följ `control_actions.detail_json`,
färska EVCC-/CT-värden, SoC och fysisk batteriström.
Använd inte exklusiv visningspil som enda acceptanskriterium.

## Korrigering efter observerad växling 2026-11-04

**Driftsatt Dell: första korrigering 12:27 UTC och efterföljande PV-kontinuitetsfix senare samma dag; fortsatt mätuppföljning.**
Användaren såg att L3-vägen till bilen växlade mellan nätimport
och lokal växelriktar-AC när solenergin varierade.

**Verifierad orsak:**
- Livepolicyn gav högst 600 W PV-laddning av husbatteriet,
  medan 50 W urladdningsbörvärde gav faktisk cirka 160–260 W
  resturladdning; beslutet följde en gräns för
  `pv_w - non_ev_house_w`, inte det som faktiskt krävs
  för att EV-fasens nätimport ska täcka bilen.
- Den installerade dispatcherns `needs_apply()` höll dessutom kvar
  50 W urladdning under samma planperiod vid ett byte
  till vanlig `charge` (utom fysisk SoC-recovery).
- Vid två timmars skrivskyddad återspelning (240 verifierade
  C2-prover) var L3-importen ofta cirka 0,6 kW under bilens
  uppskattade 1,36 kW. För **179 beräkningsmässigt lämpade
  prover** gav den nya fasbaserade modellen en
  hypotetisk medianminskning av nätunderskottet
  från 592 till 27 W. Denna återspelning
  **är inte fysisk leveransverifiering** och innefattar
  inte reglertid eller BMS-dynamik.

**Korrigerad styrning:**
1. Läs fortsatt färsk, enfasig 6 A Ict2, konsistent tre-fas CT,
   balanserad inverter (50006=0), SoC/BMS och PV.
2. Beräkna `nonEV_L3 ≈ -CT3 + inverter_AC/3 - EVCC_Ict2×230`
   för mätkontroll och urladdningsbegränsning. **Låt inte denna
   varierande hushållslast slå av batteriets PV-laddning.**
   Första fasberoende versionen gjorde just detta och är ersatt.
3. Under verifierad EV-laddning absorberar husbatteriet i stället
   PV enligt `min(3500W, floor((PV_W - 100W) × 0,95 /100W)×100W)`,
   minst 300 W för ny solelsstyrd laddning. En tillfällig
   värmare på L3 får alltså inte växla tillbaka till 50 W
   urladdning. Styrförslag kontrolleras fortfarande mot
   befintliga fas-/importgränser (3 000 W/fas och 6 000 W totalt)
   samt SoC/BMS innan någon fysisk skrivning. Vid otillräcklig
   PV blir det fortsatt ingen ny solbaserad laddning.
4. När optimizerplanen redan laddar lika mycket eller mer
   och inte har en solfinansieringsspärr, behåll planens
   laddning; annars kan en **verifierad** EV-prioritetsjustering
   ersätta mindre charge/discharge/idle. Fysisk SoC-recovery,
   freeze/hold och felaktig/stale telemetri lämnas oförändrade.
5. `ev_grid_live_priority_charge=True` används enbart av
   EVCC-verifierad livepolicy för att låta `needs_apply`
   passera den vanliga *mid-slot discharge→charge*-spärren.
   Vanliga ekonomiska planändringar förblir spärrade.
   Minskning av sådan PV-baserad laddning väntar inte
   på det vanliga effektdödbandet vid ett snabbt moln.

**Minimal produktionpatch:** installerad dispatcher SHA-256
`979b6e3f51b7d2c6237a528fc00e739376357c0f14d684e2d238fd9a1bf52f10`
fick endast två funktionsändringar via den SHA-pinnade
`scripts/tools/prepare_ev_l3_grid_first_dispatch.py`.
Installerad SHA blev
`4a87facac31ec745765f80e6c2baaee6ad206fd87d9eb1bd2dff0b52155e2f19`.
EV-moduler installerades separat med checksummer.
**Rollback:** root `0700`,
`/var/lib/solinteg-evshadow-rollback/ev-grid-first-l3-feedback-20261104/`,
med byteexakta tre originalfiler och manifest, inget
privat env-innehåll. Endast `solinteg-dispatch.service`
startades om; broker, poller, EVCC och ordinarie watchdog
bevarades. Inga manuella EVCC- eller Modbus-skrivningar
utöver dispatcherns vanliga brokerstyrning.

**Verifierad första tillämpning:** vid 12:27:17 UTC accepterades
ett EV-verifierat `charge 1300 W` (från tidigare
`discharge 50 W`). Under sju uppföljningsavläsningar
12:27:50–12:29:02 UTC växlade faktiskt batteri till
**laddning −1 120 till −1 264 W**. Bilen drog ungefär
1 357–1 403 W (EVCC Ict2×230) och L3 importerade
**1 350–1 430 W**. Det beräknade kvarvarande L3-underskottet
var 0–53 W, alltså inom indikatorns 125 W tolerans.
PV låg omkring 1 421–1 440 W och dispatchern justerade
verkligt börvärde **1 300→1 100 W**. Alla berörda systemd-tjänster
var aktiva utan omstartsfel. Detta är ett kort konkret fysikprov,
**inte garanti för 24 h eller snabb molnväxling**.

**Efterföljande fel och slutlig PV-kontinuitetsfix:**
Under 12:29:29 UTC bytte den första korrigeringen tillbaka
till `discharge 50 W` trots solproduktion 1 290 W.
Mätningarna visade att tillfällig annan L3-huslast ökade
`nonEV_L3` och gjorde den fasbaserade PV-budgeten 0.
När lasten försvann importerade bilen åter cirka 350–360 W
mindre än sitt behov (Ict2×230). Vid 12:30:38 återgick
dispatchern till `charge 900 W`: flappning kvarstod.

**Nu installerad korrigering:** Med verifierad C2/6 A, balanserade
CT/BMS och PV tillåts `grid_first_pv_capture` vara aktiv även
när en extern L3-förbrukare slår till. Batteriets PV-laddningsmål
beräknas från PV och en 100 W responsmarginal, inte från
faslasten eller statuspilens växling. `nonEV_L3` används fortsatt
för evidens och när PV inte räcker för en ny charge-åtgärd.
Denna fix gäller endast den enda aktiva EV-policymodulen.
Installerad SHA-256 efter kontrollerad omstart:
`910e312a9499f17c73834cb2d9d6a1f86a9c7247825bf59f1458174fad9330bc`.
Byteexakt policyoriginal (version efter 12:27-fixen)
och manifest finns i den separata rootägda återställningen
`/var/lib/solinteg-evshadow-rollback/ev-grid-first-pv-continuity-20261104/`.
Dispatcherns SHA `4a87facac31ec745765f80e6c2baaee6ad206fd87d9eb1bd2dff0b52155e2f19`
och adapter förblev oförändrade; bara `solinteg-dispatch`
startades om. Brokern och ordinära skydd lämnades orörda.

**Regression:** Simulerad +750 W L3-värmare gav fortfarande
`grid_first_pv_capture 1800 W` vid PV 2 026 W och 900 W
vid PV 1 093 W, i stället för att återfalla på EMS-discharge.
Skrivskyddad historisk återspelning av 240 riktiga prover
gav 239 möjliga PV-capture-beslut; ett prov avvisades p.g.a.
inkonsistent inverter/CT. Detta är fortfarande en modell,
inte bevisad exekvering vid alla batteri-/nättransienter.

**Ekonomisk och fysisk begränsning:** Med inverter
balanserad på tre faser är det normalt omöjligt att
skicka solel via AC till L1/L2-hushållet utan att
samtidigt bidra på bilens L3. För att prioritera nät
till bilen kan systemet därför behöva ladda husbatteriet
med mer solel och köpa mer nätel till övriga
hushållsfaser. Under solbrist eller när 3,5 kW
batteriladdning inte räcker kan nätprioritet inte
garanteras. Exakt källseparering saknar separat
EV-effektmätare och verifierat fasasymmetrisk AC-styrning;
20 A fysiska säkringen har egna skydd, och
SoC-/BMS-grindar går före ekonomisk reglering.

**Regressionskontroll:** 1 447 Python-tester godkända,
715 frontendtester godkända (ett överhoppat);
minimalt installerat AST-diff kontrollerat, övriga
Python-funktioner oförändrade. Inga nya parallella
shadow-tjänster, timers eller ändrat broker-failure-scenario.

**Systemstatus kvalitetsfilter vid PV-kontinuitetsfixen:**
Under efterföljande kort liveprov 12:36:35–12:38:35 UTC
laddades husbatteriet i **9/9** avläsningar vid PV
476–567 W och ingen ny `discharge 50 W` aktiverades.
L3-importen täckte bilens uppskattade effekt i **7/9**.
De två avvikande avläsningarna motsvarade **ett enda**
30 s inverterprov kl. 12:36:34 UTC: inverter-AC
1 957 W, PV 493 W och batteri −537 W, vilket
ger ett orimligt AC/PV/batteri-underskott på
cirka 2 kW. Provet var inte energimässigt
självkonsistent och får inte bekräfta en källa.

Därför har `carFlowTelemetryConsistent()` lagts till i
`lib/evcc-car-flow.ts` och anropas från både desktop-
och mobilpanelen. Car-source-indikatorn är **okänd**
om L1+L2+L3 avviker från nätets summa, inverter-AC
inte stämmer med PV + batteri eller
`inverter_ac_w - grid_w` inte stämmer med huslast.
Precis som policyn kräver denna kontroll bekräftad
balanserad inverter (50006=0). Uppskattad EV-effekt
kan fortsatt visas och huslast är EV-exkluderad;
endast den osäkra källpilen undertrycks.
Regressionsprov använder exakt de två mätserierna
och visar filtrering av det felaktiga provet.

**Webbdriftsättning för kvalitetsfiltret:**
Följande fyra sourcefiler uppdaterades atomiskt med
byteexakta originalsäkra rootkopior:
`lib/evcc-car-flow.ts`, `lib/inverter.ts`,
`app/components/PowerFlowCard.tsx`,
`app/components/MobileOverview.tsx`.
Next byggdes om i produktionsinstallationen av
`solinteg` med **`NEXT_BASE_PATH=/solinteg`**,
bara `solinteg-web.service` stoppades och startades.
Ny build-ID `VSy0q51U-ki10RIFbraPw`; `/solinteg` och
`/solinteg/api/inverter` verifierades **HTTP 200** med
aktuell EVCC C2 och live telemetri. Fullständigt
tidigare `.next`-bygge och de fyra gamla sourcefilerna
finns i root:
`/var/lib/solinteg-evshadow-rollback/ev-car-flow-quality-20261104/`.
Ändringen lägger inte till EVCC-/Modbus-skrivningar
och påverkar inga fysiska styrtjänster.

**Testat på nytt:** 1 447 Python-tester OK,
715 frontend-Vitest OK och ett överhoppat; ESLint,
TypeScript, Next.js produktionsbygge och Zensical
utan fel. I live vid cirka 12:43 UTC var EVCC C2
6,1 A (cirka 1 403 W), L3 importerade 1 460 W,
batteriet laddades med cirka 242 W och PV var cirka 460 W.
Vanlig dispatcher och broker, poller, EVCC-observatör,
historik, Volvo/HA och ordinarie watchdog var aktiva.

**Verifieringsnivå:** kontinuitet och verklig batteriladdning
verifierade under kort solavtagande förlopp, men
aldrig garanterad exklusiv energikälla vid
solbrist, asynkrona mätvärden eller blockering från BMS.
Permanent nätprioritet kräver ytterligare fysisk
och ekonomisk uppföljning.

Nästa acceptans: serieprov sol/mörker, idle/EMS, 6 A och andra
godkända strömmar, oberoende full-batteri-signal, jämförelse
mot ekonomisk plan, samt kontroll av rollback utan att byta
befintligt broker-failure-scenario.
