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
4. **Bevara befintlig styrning** vid SoC under 25 % eller över 95 %,
   batteritemperatur utanför 10–45 °C, PV över 100 W, `idle/Auto`,
   laddning, återhämtning, frys/hold, inkonsekventa faser eller
   okänd EVCC-status. Vid `idle` krävs ytterligare sammanhängande
   mätserie och stabilitet innan något 50 W-hypotetiskt läge kan
   aktiveras. Vid solproduktion måste befintlig möjlighet till
   **solelsladdning av husbatteriet** bevaras; vår rena nattmodell
   ger **inga** dagaktiva styrförslag.
5. `ev_l3_metered_policy.assess()` är **ren**, läser inga filer eller
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
