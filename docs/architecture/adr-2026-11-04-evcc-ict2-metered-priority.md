# ADR 2026-11-04: EVCC Ict2 som primär mätning för EV/L3-batterisamordning

**Beslut:** Accepterat för **read-only/shadow** och historikinsamling.
**Ingen fysisk EVCC- eller inverterstyrning godkänns av denna ADR.**
Branch: permanent `Sundsvall`, aldrig merge till `main`.

## Varför

Tidigare `l3_battery_policy.py` identifierar bilen med en uppskattad
L3-huslast på minst 2 500 W. I 543 verifierade historiska 6 A-prover nåddes
tröskeln bara i 15 fall. Om den sänks till 1 200 W fångas 542/543
bilprover, men 31/162 **ej laddande** kontrollprover passerar också.
En vanlig L3-apparat kan inte identifieras säkert utifrån nät-CT ensam.

Waveshare bryggar **läsning FC03** från ABL EVCC. Den installerade
`evcc_observer.py` ger `channel_currents_a=[Ict1,Ict2,Ict3]` med
0,1 A upplösning. På den aktuella enfasanslutningen är bilens uppmätta
ström **Ict2**, inte `measured_current_a` som är summan inklusive små
mätvärden på Ict1/Ict3. Historiskt verifierad `C2`/100–100 gav
Ict2 cirka 6,0 A. Strömmen mäts; **aktiv effekt mäts inte** av EVCC:s
FC03-värden. `230 V * Ict2 * PF≈1` är ett nominellt **W-estimat**,
inte en precis effektmätare eller fakturerbar energi.

Separata `ev_ac_capture.py` finns redan för 6 A-/230 V-estimat i en
kostnadsbevisdatabas. Den ändras **inte** av denna ADR. Den större
EV-historiken har däremot saknat separata Ict-kanaler och register 50006.

## Beslutad modell och gränser

**Kravförtydligande 2026-11-04 (ersätter tidigare PV>100 W-spärr i den nya
Ict2-shadowmodellen):** Husets aktuella last försörjs först av tillgänglig
solel. Så länge husbatteriet **inte är verifierat fullt** reserveras
resterande solel för **husbatteriets laddning**, medan **hela bilens
laddningsbehov budgeteras som nätimport**, även i stark sol. Först när
husbatteriet är **oberoende verifierat fullt** får solöverskott efter
hushållets exklusive-bil-last prioriteras till bilen. Ytterligare överskott
exporteras; laddar bilen inte alls exporteras hela sådana överskott.
SoC 99 % eller ett ensamt SoC 100 %-värde är inte tillräckligt bevis för
att PV får styras till bilen.

Detta är **prioritering för ekonomisk fördelning**, inte bevisad
fysisk elektronväg. Nät, PV och batteri blandas i växelriktarens AC-nod;
EVCC:s Ict2 visar ström, inte vilken källa som levererade den. För en
granskbar approximation beräknas önskad nätimport på L3 som
`230 V × Ict2 - tillåten PV-till-bil`, jämförd med signerad L3-CT.
Skillnaden är en **styrmålsavvikelse**, inte uppmätt felmatad batterieffekt.
Inverter-/BMS-reglering och säker solöverskottsladdning måste separat
fysiktestas före fysisk aktivering.

1. Kräv **färska** EVCC+inverter-snapshots, `source=waveshare_evcc_fc03_readonly`,
   `read_ok=true`, `state=C2`, faktisk `charging_detected=true`,
   FC03-källa utan skrivningar, samstämmig PWM-konfiguration/pilot,
   Ict1/Ict3 vardera högst 0,5 A och konsistent Ict2/summaström.
   Hantera PWM 1000 (paus), okänd eller gammal ström som **okänd**, inte 0 A.
   Mätningar från 6, 10, 16 och 18 A hanteras; 16/18 A ger **inte**
   automatiskt rätt att ändra inverterbörvärden.
2. Härled med **verifierat `50006=0`** och tre signerade, konsistenta
   CT-värden: `L3_house_W ≈ -CT3_W + inverter_AC_W/3`;
   `L3_non_EV_W ≈ L3_house_W - 230 * Ict2_A`.
   Den ungefärliga tillåtna totala batteriurladdningen beräknas
   konservativt som
   `3 * max(0, L3_non_EV_W - 180 W) / 0.95`, avrundad nedåt,
   med befintligt 50 W minimum och **aldrig större än planerat mål**.
   Den faktiska nätimporten kan skilja sig genom spänning, PF,
   omriktarförluster, fasfördelning och tidsfördröjning.
3. Kontrollera även inverter-AC, PV, batteri, hushållslast och nät-CT mot
   varandra. **Softgränser**: förutsedd import högst 3 200 W per fas och
   6 500 W totalt i detta preliminära test. Detta är **inte**
   20 A-säkringsskydd eller oberoende RMS-/termiskt skydd.
4. **PV är inte längre en blockerare i sig.** Den rena policyn
   `solar_priority_budget()` redovisar `ev_grid_target_w_est`,
   `ev_solar_allowance_w_est`, solöverskott till hushållsbatteriet
   (högsta möjliga före BMS-/laddgränser) och beräknad export först
   vid full batteriverifiering. Full kräver i nuvarande shadow-grind
   SoC minst 99,5 %, `battery_full_confirmed=true` från en framtida
   oberoende BMS-källa och uppmätt batterieffekt inom ±150 W.
   **Fältet finns ännu inte i den installerade telemetrin**, så
   modellen kommer inte tilldela solel till bilen vid
   enbart rapporterad hög SoC.
5. **Bevara befintlig styrning** vid SoC-reserv, BMS-/temperaturgräns,
   `idle/Auto`, befintlig laddning, återhämtning, frys/hold,
   inkonsekventa faser eller okänd EVCC-status. Om PV efter
   hushållslast sannolikt kan ladda husbatteriet redovisas
   `solar_storage_priority_unverified` (ett explicit **skuggmål**),
   inte ett beordrat 50 W-urladdningsläge som kan blockera solladdning.
   Även den redan dokumenterade EMS-`solar_charge`-vägen saknar
   tillräcklig fysisk verifiering; inget börvärde eller tidsgrind
   har aktiverats.
6. `ev_l3_metered_policy.assess()` är **ren**, läser inga filer eller
   nätverk och ger enbart en `MeteredAdvice` med
   `model_actionable=false`, `physical_control_authorized=false`,
   `device_writes=0`. Den har **ingen automatisk verkställighetsväg**.
   Den gamla 6 A-skyddade fysiska policyn, aktiva dispatchern,
   brokertransaktionerna och befintlig watchdog **ändras inte**.

## Mätkedja och verifierbar historik

- `ev_l3_readonly_audit.py` kör ny Ict2-policy **parallellt** med
  äldre 6 A-shadow i enbart läsande CLI. `metered_policy` visar bland
  annat separat `ev_ict2_a`, `ev_power_w_est` och tillåtet
  *hypotetiskt* batteribörvärde.
- `prepare_ev_l3_shadow_dispatch.py` är fortsatt SHA-bunden till exakt
  installerad dispatcherbaseline, med **opt-in logg-only-hook**
  (`DISPATCH_EV_L3_SHADOW_LOG=1`) till nya rena Ict2-policyn.
  Ingen fysisk kontrolltuple modifieras. **Ej installerad**.
- `ev_history.py` innehåller nullable `evcc_ict1_a`, `evcc_ict2_a`,
  `evcc_ict3_a`, `inverter_ac_w`, `battery_temp_c` och
  `grid_unbalanced_output_raw`. Äldre SQLite-tabeller uppgraderas
  additivt med granskad `ALTER TABLE ADD COLUMN`; tidigare
  rader får `NULL`, **aldrig uppskattad Ict2/50006**.
  Denna tjänst skriver endast till sin privata observations-SQLite.
- `ev_l3_metered_replay.py` läser databasen via SQLite
  `mode=ro`, kräver de faktiskt lagrade oberoende mätfälten och
  visar syntetisk 2 300 W-discharge endast som shadowexempel.
  Saknade kolumner ger `status=missing_schema`, inte en
  påhittad historisk mätning.

### Isolerad driftsättning av läsande historik 2026-11-04 ca 10:36 CET

**Driftsatt på Dell: endast** `solinteg-ev-history.service`. Källfilen i
`/opt/solinteg/ev-shadow-runtime/scripts/services/ev_history.py`
installerades atomiskt efter checksumme-/AST- och driftskontroll, med samma
ägare och `0440` som tidigare. SHA-256 före:
`08a7b1612355f5a12e13b8bf4b1ac4fbc29e952cc6a68b72b233e664fa1950a9`;
efter:
`b56b43b7c3c003743abaf7232a50c81ebac901142d291b32e7222eb42afa577d`.

- Rootägd återställningskopia:
  `/var/lib/solinteg-evshadow-rollback/ev_history.py.pre-ict2-20261104`
  (mode `0600`), checksumma lika med ursprungsfilen. **Bevara** tills
  tjänsten och historikens integritet är slutverifierade.
- Endast historiktjänsten startades om, utan ändringar i dess systemd-unit
  eller miljökonfiguration. Broker, poller, dispatch, EVCC-observatör
  och ordinarie watchdog förblev `active` med noll nya omstarter.
- Befintliga privata `ev-history.sqlite` migrerades med sex **nullable**
  kolumner utan förlust eller bakåtfyllning av historiska prover.
  `PRAGMA quick_check=ok`; radantal ökade från **19 413** till **19 416**
  i första efterkontrollen. Ett nytt `A1`-prov visade
  `evcc_ict2_a=NULL`, `inverter_ac_w=859`,
  `battery_temp_c=15`, `grid_unbalanced_output_raw=0` och
  `physical_writes=0`. Nullvärdet är **korrekt**: ingen verifierad
  billaddning pågick.
- Nytt read-only replay av senaste dygnet fann **2 877 historikrader**,
  men ännu **ingen** `C2`-laddning med de nyligen införda oberoende
  Ict2-/50006-kolumnerna. Resultatet kan alltså ännu inte
  validera kompensering i verklig drift. Ingen äldre Ict2 gissas.
- **Återställningsförfarande, ej utfört:** stoppa **endast**
  `solinteg-ev-history.service`, återinstallera exakt den bevarade
  rootkopian med ursprungligt ägarskap, mode och verifierad SHA och
  starta **endast** historiktjänsten. De sex additiva SQLite-kolumnerna
  kan vara kvar och ignoreras av äldre kod; radera ingen historik.

**Regression efter ändringar (källkod/offline):** **1 404 Python-tester OK**
inklusive ny mätpolicy, replay och gammal SQLite-migration.
Frontend: **705 Vitest OK, 1 överhoppat**; lint och TypeScript typecheck
OK. Detta ersätter inte verifiering av en verklig aktiv EVCC-Ict2-session.

**Ej driftsatt:** `ev_l3_metered_policy.py` som dispatcher-hook,
`DISPATCH_EV_L3_SHADOW_LOG=1`, något root-gate eller någon fysisk
batteri-/EVCC-reglering. Den nya renodlade shadow-kandidatens
installeringsförslag är fortfarande **endast genererat i minnet** med
SHA-256 `20bf7f5cad47157b81ab5b93b892892c83fd6033db3c299aeb73cbd3ab0328a4`.
Den tidigare kandidat-SHA:n i den föregående granskningen beskriver
ett **historiskt** loggförslag och ska inte användas för ny release.

### Korrigerad implementation och verkligt 6 A-prov 2026-11-04

Efter **separat uttryckligt godkännande** ändrades EVCC:s pausade PWM
1000 med **ett** manuellt FC10-kommando till PWM 100 (6 A);
två läsningar verifierade 100/100 och sedan verklig `C2`-laddning,
Ict2 cirka 6,0–6,1 A. Det är en verklig EVCC-fältverifiering men
**inte** bevis för att nätet finansierade hela billasten.
Endast EVCC-observatören pausades för den låsta skrivningen och
återstartades; ordinarie Solinteg-inverter/dispatcher/broker/watchdog
ändrades inte. Privat provlogg bevaras utanför Git på Dell.

I senaste separata **read-only**-återspelning fick 32 verifierade C2-prover
nya `ev_grid_target_w_est` trots PV långt över 100 W.
Vid SoC cirka 24 % och PV cirka 2,7 kW var 6 A-målet 1 380 W
nätimport, medan nettomätaren på L3 visade ungefär 551 W;
skillnaden 829 W markeras som **målavvikelse utan kausalitetsbevis**.
Källkodens nya `solar_priority_budget` styr inte EVCC eller inverter;
full-batteri-grenen är fortfarande enbart syntetiskt testad.
`solar_storage_priority_unverified` medför **ingen** automatisk
force-charge-/50 W-/Auto-ändring, eftersom de driftlägena måste
verifieras separat mot BMS och Solintegs EMS.

**Installerad drift:** enbart den tidigare read-only-historiktjänsten
är uppgraderad. `ev_l3_metered_policy.py` körs endast via explicita
read-only-audit/replay-anrop; ingen inkopplad dispatcher-hook.
Den gamla `ev_grid_priority_guard.py` har fortfarande en **historisk
PV ≤100 W-grind**, men dess rootgate är återkallad och den är **inte**
ansluten till aktiv dispatcher. Den får **inte** återaktiveras för
den här prioriteringen utan separat granskning och releasebeslut.
**Validering efter kravkorrigeringen:** 1 411 Python-unittester OK,
inklusive ny live-liknande dagsol-fixture, syntetiskt
full-batteri-/exportfall och skrivskyddad historian-replay;
Zensical-dokumentationsbygge och `git diff --check` godkända.
Det accepterade äldre failure-scenariot i brokern behålls.

### Driftsatt: Ict2-grid-first-shadow i befintlig observationssidecar 2026-11-04

**Fysisk EV-nätprioritering fortsatt NO-GO.** Enbart den redan
installerade `solinteg-ev-grid-solar-shadow.timer` (cirka 30 s) har
uppgraderats med ett ytterligare *read-only* JSON-fält. Ingen ny timer,
watchdog, rootgate, dispatcher-hook, brokerskrivning, EVCC-FC10 eller
inverterkontroll har införts.

Den rootägda befintliga observatörfilen skiljde sig från den nyare
Git-källan. Därför användes en **checksummebunden additiv injektion**,
inte kopiering av hela Git-observatören. Skriptet
`scripts/tools/prepare_ev_l3_metered_shadow.py` kräver exakt
installerad ursprungs-SHA-256
`91b569847b2380304c038913cd0a68f915117edb663f04f01f5caae5596aeb79`
och kompilerar endast en import samt en `metered_policy`-samling
**före** den gamla grindkontrollen. Ny installerad observatörs-SHA-256:
`eac42a0f1ddb2734c93a2d827a9bc12ab41cc82897b7ff23e36f8ae28837b0cc`.

Installerat som rootägda 0644-filer i
`/usr/local/libexec/solinteg-ev-grid-solar-shadow/`:

- `ev_grid_priority_shadow_observer.py`: originalet plus separat
  `metered_policy`-fält (ingen ändrad legacy-`state`);
- `ev_l3_metered_shadow_observer.py`: separat JSON-/SQLite-read-only
  kollektor som misslyckas konservativt vid saknade data;
- `ev_l3_metered_policy.py`: rent matematiskt Ict2-/PV-/SoC-beslut
  med `physical_control_authorized=false` och `device_writes=0`.

Den **gamla rootgrinden saknas fortfarande**. Därför är det medvetet
förväntat att JSON-filens äldre toppnivå-`state="disabled"` samtidigt
som **`metered_policy.state` innehåller aktivt beräknade resultat**.
Inget försök gjordes att kringgå den gamla grindens skydd för dess
egna äldre beslut.

Första produktionskörningen verifierade `C2` Ict2=6,1 A, uppskattat
EV-nätbehov **1 403 W**, solreserv till husbatteriet cirka **340 W**
och `metered_policy.state="soc_or_bms_guard"`. Reservstatusen
påverkar inte den rapporterade icke-aktuerande mätbudgeten. Enbart
observationen tillåts vid låg SoC, aldrig fysisk reglering.

**Tillbakagång:** ursprungsfilen sparades rootägd i
`/var/lib/solinteg-evshadow-rollback/ev_grid_solar_shadow_observer.pre-ict2-20261104.py`
(SHA-256 enligt ovan). För rollback: stoppa **endast**
`solinteg-ev-grid-solar-shadow.timer`, verifiera att dess oneshot har
avslutats, återställ observatörens exakt hashade tidigare innehåll
atomiskt med ursprungligt läsbart 0644-mode, ta bort endast de två
nya ägda modulerna efter jämförelse med förväntade hashvärden och
starta **endast** sidecar-timern. Låt rootgrind, dispatcher,
broker, poller och EVCC-observatör vara orörda. Bevara rollbackfilen
och privat EVCC-provlogg tills arkivkraven har uppfyllts.

**Verifieringsnivå:** 1 418 Python-unittester OK före installering,
Zensical-dokumentationsbygge OK. Installerad version kompilerade och
gav verifierat JSON-resultat vid manuell oneshot-start. Tre
efterföljande, distinkta snapshot-tider verifierades:
10:33:31, 10:34:01 och 10:35:01 UTC, med Ict2 cirka 6,1 A,
nätbehov cirka 1 403 W och `device_writes=0`. Alla observerade
systemd-tjänster/timern var aktiva. SoC låg då kring **22 %** och
modellen redovisade korrekt skyddsstatus utan att stoppa den
fortsatta fysiska urladdningen. Långsiktiga ekonomiska effekter
och fysisk EV-nätprioritet är fortfarande **inte** verifierade.

### Förutsättningar för fysisk driftsättning

1. Kontrollera under övervakad **verklig billaddning** att `C2`,
   Ict2 och PWM följer samma fas vid 6 A; verifiera 10–18 A separat
   endast med godkänd laddström och korrekt säkringsmarginal.
   Aldrig automatiskt upphäva PWM-paus `0x03E8`.
2. Verifiera samtidiga faser/SoC/temperatur/50006 och separera
   belastningar som inte kommer från bilen. Jämför verklig nätimport
   före/efter *utan* att missta korrelation för energiflödesbevis.
3. Testa sol/Auto, SoC-återhämtning, saknade mätningar, synkronisering,
   timeout, prioriteringar, separat releaseplan och exakt
   fungerande rollback/fallback enligt accepterad risk-ADR.
4. Inga nya inverter-/EVCC-skrivningar eller dispatcheromstarter ingår i
   kod- och replayfasen. Ny **uttrycklig fysisk GO** krävs efter dessa steg.

**Kända brister:** EVCC Ict2 har 0,1 A **upplösning**, inte en
kalibrerad mätfelsgaranti. Spänning och effektfaktor är inte uppmätta
här. Modellen ger bara en ekonomisk proxy för att begränsa
batteriets bidrag, inte fysisk separation av nät-/solelflöden eller
säkringars överlastskydd. 30 s observationscykel och dispatchens
tidsfördröjning kan missa transienter.
