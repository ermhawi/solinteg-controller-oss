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

- Vid verifierad 6 A-laddning och tillräcklig PV-budget kan
  `ev_l3_live_policy` omvandla idle/urladdningsbeslut till
  konservativ 300–600 W batteriladdning. PV efter **icke-EV-huslast**
  reserveras först för husbatteriet; fas- och totalimport kontrolleras.
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

Nästa acceptans: serieprov sol/mörker, idle/EMS, 6 A och andra
godkända strömmar, oberoende full-batteri-signal, jämförelse
mot ekonomisk plan, samt kontroll av rollback utan att byta
befintligt broker-failure-scenario.
