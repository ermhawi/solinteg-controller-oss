# Permanent nätprioritering för XC40 på L3 – releaseförberedelse 2026-10-29

**Status 2026-10-31 10:25 CET:** Den minimala EV-hooken är installerad,
`mode=shadow` är aktiverat och root-watchdoggen kör var 15:e sekund.
**Ingen fysisk EV-nätprioritering är aktiverad** (`mode=active` ej godkänt).
Bygger på det verifierade provet 2026-10-28:
[runbook](ev-grid-priority-trial-2026-10-28.md) och
[Notion](https://app.notion.com/p/400aa897399b81af9d11f18187955e75).

## Avgränsning och befintlig drift

- Den vanliga `solinteg-dispatch.service` är **ARMED**. Under den första
  förberedelsen 2026-10-29 ändrades inga tjänster, inverterregister,
  EVCC/Volvo-inställningar, gate-filer eller watchdoggar. 2026-10-31 installerades
  separata watchdog-units och releasefiler utan att ändra den aktiva dispatchern.
- Vid den tidigare read-only-kontrollen körde Dell den äldre dispatchern
  (SHA256 `a2f3aba4be8d64771ed2b20c71c9ceed5c3ce25ff99cad246bc5529afd65a8b7`)
  utan EV-hook. Sedan 10:25 kör den minimalt patchade shadow-versionen;
  det byteexakta originalet finns kvar i rootägd backup.
- Den äldre trial-timern är `disabled/inactive`; trialens tidigare
  rollback-status är `restored_original_dispatch`/`rollback_ok=true`.
  Den nya permanenta watchdog-timern är aktiv med en separat schema-2-gate i shadow-läge.
- **Historisk blockerare 2026-10-29 18:53 CET:** EVCC-observatören
  rapporterade `read_ok=false, state=unknown`. Läsback fungerar åter enligt
  den skrivskyddade kontrollen 2026-10-31; det undanröjer inte övriga krav.
- Git-branchen `Sundsvall` är betydligt nyare än aktiv dispatcher (F-11).
  **Installera inte hela Git HEAD över ARMED-drift** utan separat
  kompatibilitetsgranskning och riskbedömning. Endast det granskade skyddet
  får överföras som minimal versionerad förändring i releasefönstret.

## Historisk read-only releasekontroll före shadow-drift (2026-10-31 10:08 CET)

- `solinteg-dispatch.service` är aktiv men kör fortfarande original-SHA256
  `a2f3aba4be8d64771ed2b20c71c9ceed5c3ce25ff99cad246bc5529afd65a8b7`
  **utan EV-prioriteringshook**. Den aktiva `l3_charge_guard` begränsar
  batteriets *laddning*, inte dess urladdning till bilens fas.
  `DISPATCH_L3_CHARGE_GUARD_MODE=1` och budgeten är 4 000 W.
  `DISPATCH_L3_BATTERY_MODE=shadow` står i miljöfilen men motsvarande
  kod saknas i den installerade dispatchern; detta är ingen aktiv EV-policy.
- EVCC-observatören är aktiv och läser `read_ok=true`, `state=C2`,
  `Ict2=6,0 A`, `configured_pwm_raw=pilot_pwm_raw=100`, `writes=0`.
  Övriga isolerade HA-/pris-/historiktjänster är aktiva men skrivskyddade.
- Vid denna kontroll klockan 10:08 var den permanenta watchdog-timern
  ännu inte installerad. Trial-timern var inaktiv och ingen schema-2-gate
  fanns. `phase_guard_verified=false`;
  inget oberoende RMS-baserat L3-säkringsskydd är verifierat.
- Vid kontrollen understeg husbatteriets SoC 25 % och PV översteg 100 W.
  Nattpolicyns gränser (SoC 25–95 %, PV högst 100 W) var inte uppfyllda.
  Skrivskyddad simulation gav `shadow_state=unavailable`, **ingen fysisk
  begränsning** och ingen rekommenderad ström att applicera. Detta är
  ett tidsspecifikt säkerhetsstopp, inte ett misslyckat funktionstest.
- Den minimala releasekandidaten genererades endast under `/tmp`:
  SHA256 `7934dd1c31e6ea07780f33492ae57ca33447b3ff7e43259edebd2116ae2db6d4`.
  Den kompilerar och `systemd-analyze verify` accepterade watchdog-units.
  Inga tjänster, gate-filer eller fysiska styrinställningar ändrades.
- Releasekontrollen har sex skrivskyddade regressionstester; totalt
  **817 Python-tester** och **697 Vitest-tester** passerade (ett Vitest-test
  hoppades över). ESLint, TypeScript typecheck och Zensical-dokumentationsbygge
  passerade utan fel.

Skrivskyddad preflight (kontrollerar känd dispatcher-SHA, beräknar endast
skuggförslag från färska live-/EVCC-snapshots och returnerar JSON):

```bash
sudo -n python3 scripts/tools/ev_grid_priority_preflight.py
```

`shadow_eligible_now=true` är **inte** ett aktiveringsbeslut.
`physical_activation_authorized` och
`independent_l3_rms_protection_verified` förblir `false`.
Den godkända shadow-omstarten är nu genomförd och verifierad.
Nattgränserna begränsar endast aktiv fysisk styrning, inte
shadow-installationen. Fysisk `mode=active` kräver fortfarande
ett separat uttryckligt godkännande.

## Driftsatt shadow-läge – 2026-10-31 10:25 CET

Användaren godkände en kontrollerad omstart och installation i
**enbart shadow-läge**. Förberedelserna avbröts först eftersom SoC var
20 % och solproduktionen över 100 W; detta visade sig vara en för strikt
tolkning. Nattgränsen är ett säkerhetsvillkor för *aktiv fysisk*
urladdningsbegränsning, **inte** för att installera eller bevaka shadow.
Denna skillnad verifierades även med ett regressionstest som säkrar
oförändrad dispatch-tuple vid SoC 20 % och PV 550 W.

### Versions- och rollbackspärrar

- Originaldispatcher: SHA256
  `a2f3aba4be8d64771ed2b20c71c9ceed5c3ce25ff99cad246bc5529afd65a8b7`,
  root/0400-backup:
  `/var/lib/solinteg-ev-grid-priority/dispatch-before.py`.
- Ursprunglig trialpolicy: SHA256
  `4ef6f2fc36acb449ee05498081a64e6bccabf25bc0c1a46a0f25ac4ced1857d2`,
  root/0400-backup:
  `/var/lib/solinteg-ev-grid-priority/guard-before.py`.
- Installerad dispatcher med **enbart minimal EV-hook**: SHA256
  `7934dd1c31e6ea07780f33492ae57ca33447b3ff7e43259edebd2116ae2db6d4`.
  Originalets serviceägare, grupp och filrättigheter behölls.
- Installerad policy: SHA256
  `709f260b41163bcd18c45a66816ad7f500dedda2235266cb74e8eb2fab9dab79`.
  Identisk med rootägd kopia under
  `/usr/local/libexec/solinteg-ev-grid-priority/`.
- Rootägd schema-2-`/var/lib/solinteg-ev-grid-priority/gate.json`:
  **`mode=shadow`**, unikt activation-ID och bunden dispatcher-SHA.
  Ingen `mode=active`-gate skapades.
- Separat root/systemd-watchdog med timer var 15:e sekund:
  `shadow_monitor_ok` verifierades direkt efter omstart och vid
  följande timerticks. Ordinarie `solinteg-watchdog.timer` behölls.

### Omstart och verifierad drift

Vid deployment var husbatteriet **20 %**, PV **1 787 W** och
EVCC läste ungefär 5,9 A på Ict2 (`state=C2`).
Den vanliga ARMED-dispatchern stoppades med SIGTERM, gick som avsett
tillbaka till Auto (`General 0x101`) och startades sedan med samma
miljöfil och den minimala EV-hooken. Originalets funktion för
nätimportgräns och återhämtningsladdning fortsatte att fungera:
vid 10:25:28 ställdes ordinarie importgräns in igen, och
vid 10:25:31 skickades den befintliga SoC-recovery-laddningen på 300 W.
Vid 10:25:40 var den ännu
`accepted_pending_physical_confirmation`; det är **inte**
en verifiering av faktiskt laddad effekt. Vid 10:26:59 var
uppmätt husbatterieffekt **-313 W** (laddning, inte urladdning),
och SoC fortsatt 20 %. Detta visar att återhämtningsladdningen
återupptogs efter omstarten.

Efterföljande read-only verifiering klockan 10:25:51 och
återigen 10:26:59 visade `solinteg-dispatch.service=active`,
`solinteg-ev-grid-priority-watchdog.timer=active`,
`shadow_monitor_ok` med korrekt activation-ID och installerad
dispatcher-SHA, samt färsk dispatch-heartbeat.
PV var vid den sista kontrollen 2 565 W; shadow-läget var
fortsatt normalt. EV-watchdoggen rapporterar `device_writes=0`.

**Viktigt:** SIGTERM/omstart av den ordinarie ARMED-dispatchern
påverkar växelriktarens befintliga styrning kortvarigt; det är inte
samma sak som EV-policyns shadow-läge. Den nya EV-hooken
skriver inga register och kan inte begränsa batteriet i shadow-läge.
För `discharge` vid hög PV/låg SoC registreras `unavailable`
med oförändrat beslut; `charge`, `idle` och SoC-recovery lämnas
också oförändrade.

### Varför 100 W fortfarande finns i den aktiva policyn

Beräkningen av tillåten batteriurladdning mot bilens L3-last
bygger på en nattlig approximation: växelriktarens balanserade
AC-utmatning fördelas på tre faser när solproduktionen är försumbar.
Vid större PV kan AC-utmatningen komma både från solceller och
husbatteri. Utan en separat, verifierad fördelning riskerar policyn
att felaktigt tilldela bilens laddning till husbatteriet respektive PV.
Därför får `mode=active` ännu bara påverka urladdning vid
PV högst 100 W och SoC 25–95 %. Detta är **en tillfällig gräns för
bevisläget**, inte ett krav på att minska solproduktionen.
En PV-medveten modell, fasprov och säkringsmarginaler måste
utvecklas och verifieras separat innan dessa aktiva gränser höjs.

### Kvalitet efter shadow-deployment

- **820 Python-tester** godkända, inklusive regressionstest för
  skuggdrift med hög PV och SoC under nattpolicyns gräns.
- **697 Vitest-tester** godkända (ett manuellt test överhoppat),
  ESLint, TypeScript typecheck och Zensical-bygge utan fel.
- Källkodens policy och dispatcher-SHA verifierade mot installerade
  rootägda kopior; ingen `mode=active`-gate finns.

### Kvarstående arbete

- Följ fortsatt upp SoC-recovery, shadow-status under både
  dags- och nattförhållanden samt datakvalitet; den första
  mätningen efter restart visade -313 W batteriladdning.
- Förbättra skuggberäkningen för hög PV utan aktivering av fysisk styrning.
- `mode=active`, dynamiska EVCC-strömändringar och en eventuell
  RMS-baserad L3-fassäkringsreglering ingår **inte** i denna release;
  nytt uttryckligt godkännande och ytterligare fysiska prov krävs.

## Versionerad design (ingen extra fysisk aktivering)

- `scripts/services/ev_grid_priority_guard.py` behåller oförändrat schema-1
  för det tidsbegränsade testet och stöder schema-2 för permanent drift.
  Utan giltig gate returneras ursprungligt dispatchbeslut.
- Den nya gate-filen är `/var/lib/solinteg-ev-grid-priority/gate.json`.
  Hela katalogen **och filen** måste ägas av root och inte vara skrivbar
  för grupp eller övriga. Den bor **inte** i det service-skrivbara
  `/opt/solinteg`. En giltig gate kräver exakt policy-ID, unikt
  `activation_id`, `activated_utc` och SHA256 av installerad dispatcher.
  Den medföljande JSON-mallen innehåller avsiktligt ogiltiga platshållare.
- Gate `mode=shadow` beräknar bara förslag. `mode=active` kräver dessutom
  `status.state=active_monitor_ok`, samma `activation_id`, max 40 sekunders
  statusålder, root-ägd watchdogstatus och oförändrat dispatcher-SHA256.
  Saknas något används **ursprungligt dispatchbeslut**.
- Den **separata root/systemd-watchdoggen**, `ev_grid_priority_watchdog.py`,
  ska köras från root-ägd, ej gruppskrivbar
  `/usr/local/libexec/solinteg-ev-grid-priority/`, med en separat
  root-ägd kopia av `ev_grid_priority_guard.py`. Root-watchdoggen
  verifierar att den installerade dispatch-policyn har exakt samma SHA256
  som denna betrodda kopia, och kontrollerar sedan varje 15 s:
  dispatch uppe, checksumma, root-backup, färsk EVCC
  och inverterdata, C2/Ict2, BMS SoC/temperatur, 50006=0, trefas-CT,
  predikterad fasimport, samt att verklig urladdning när EV laddar har
  sjunkit inom rimlig tid (100 s per ny laddsession).
- Fel i datakvalitet, kommunikation, fasbalans, temperatur eller faktisk
  batterirespons gör att watchdog **först återkallar root-gaten**, sedan
  stoppar dispatcher via ordinarie shutdown/Auto, återställer för-release
  dispatcher från root/0400-backup atomiskt och startar tjänsten. Vid
  oväntat nytt fil-SHA vägrar den skriva över okänd kod; status kräver
  då manuell åtgärd. Ingen direkt registerskrivning i watchdoggen.
- En uttryckligen verifierad parkerad/frånkopplad bil med högst 0,6 A,
  även C2 utan faktisk laddström, betyder **ingen prioritering**, inte
  ett permanent fel. Om PV över 100 W eller SoC utanför 25–95 % ger
  policyn endast **standby**, och bara om övriga observationer är
  konsistenta. Övrig SoC-recovery, soloptimering, laddning och idle påverkas inte.
- Vid aktivt EV-läge och inom verifierade nattgränser begränsas bara
  `action=discharge`. 6 A på Ict2, balanserad inverter 50006=0 och
  estimerad icke-EV-huslast på L3 avgör nytt börvärde.
  Beräknad **import på var och en av L1/L2/L3 ≤ 3 200 W**, total
  import ≤ 6 500 W. Detta är försiktiga programvarugränser
  **inte certifierat RMS-fassäkringsskydd**. Ordinarie 20 A-säkringar,
  inverter-/BMS-begränsningar, SoC-golv och
  `l3_charge_guard` förblir självständiga. EVCC-strömtak 6 A, Volvo-mål
  80 %, bilens API:er och FC10 ändras inte.

**Begränsad täckning:** Nattlig 6 A-drift är provad. Att korrekt allokera
solöverskott, högre EV-ström och samtidiga större L1/L2-laster är
**inte fysiskt verifierat**. Under hög PV väljer den nya funktionen
avsiktligt standby framför att påverka övrig styrning; permanent betyder
alltid tillgänglig bevakning, **inte garanterad nätmatning i varje solscenario**.
Ytterligare fysiska prov kräver särskilt tillstånd.

## Checksummebunden minimal dispatcherpatch (offline förberedd, nu i shadow-drift)

F-11 löses genom `scripts/tools/prepare_ev_grid_priority_dispatch.py`, som
läser den verifierade äldre dispatchern och bygger en AST-validerad
kandidat **utan att skriva till /opt eller påverka tjänsten**:

```bash
sudo python3 scripts/tools/prepare_ev_grid_priority_dispatch.py \
  --source /opt/solinteg/app/scripts/services/dispatch_loop.py
```

Detta läsprov på Dell gav original-SHA256
`a2f3aba4be8d64771ed2b20c71c9ceed5c3ce25ff99cad246bc5529afd65a8b7`
och kandidat-SHA256
`7934dd1c31e6ea07780f33492ae57ca33447b3ff7e43259edebd2116ae2db6d4`,
`written=false`. Scriptet avvisar okänd originalchecksumma, ändrade hookankare
och dubbel injektion. Det lägger enbart till `Path`-import och EV-prioritering
före den gamla charge-guarden. Verktyget utför fortfarande **ingen installation**.
Den separat godkända shadow-releaseprocessen använde den checksummebundna
kandidaten för att uppdatera ARMED-installationen 2026-10-31.

## Releasegrind – shadow-steg 1–5 genomförda, aktiv styrning kräver nytt godkännande

1. Kontrollera drift och Git-version en gång till. Hantera F-11 genom
   minimal och separat granskad patch/hook i *faktiskt installerad* dispatcher,
   inte en okontrollerad kopiering av Git HEAD. Kör offline-tester och
   `python3 -m py_compile` för exakt releasefil; säkerställ att service-ARMED,
   BMS-/SOC- och ordinarie L3-charge-guard-väg förblir oförändrade.
2. Säkerställ EVCC `read_ok=true`, `state=C2`, Ict2 ca 5–6,5 A,
   PWM 100, färska inverter-/fasavläsningar, batteritemperatur,
   fysiska BMS-limiter, huvudsäkringar, 50006=0 och stabil Modbus.
   Stopp om någon förutsättning fallerar.
3. Installera separat rootägd state-katalog
   `/var/lib/solinteg-ev-grid-priority` (0755) och **innan
   kodändringen** en byteexakt root/0400-backup
   `dispatch-before.py` av den exekverare som faktiskt kör.
   Dokumentera båda SHA256; dubbelkontrollera stop/start- och Auto-retur.
4. Installera endast granskade nya policymodulen, watchdog-scriptet
   och den minimala dispatcher-hooken. Watchdog-scriptet och en **identisk
   kopia** av policyn installeras under
   `/usr/local/libexec/solinteg-ev-grid-priority`, root:root och
   utan grupp-/övrig skrivrätt (t.ex. filer 0644, katalog 0755).
   Dispatcherns policymodul ligger kvar i tjänstekatalogen;
   SHA256 mellan båda policymodulerna ska vara byteidentiskt.
   Kör sedan gated **shadow-only**-start,
   med root/0644 `gate.json` schema-2 och `mode=shadow`,
   unik aktivering och checksumma av exakt installerad kod.
   **Även tjänsteomstart i ARMED-installationen måste riskbedömas**
   och utförs först efter godkänt underhållsfönster.
5. Installera
   `deploy/solinteg-ev-grid-priority-watchdog.service` + `.timer`;
   verifiera systemd-konfiguration, rättigheter, JSON-källor och
   att timer kör var 15:e sekund **innan** något aktivt läge.
   I shadow-läge får ingen battery/EVCC-åtgärd ske.
6. Efter *nytt uttryckligt godkännande av fysisk inverteraktivering*:
   byt root-gaten atomiskt till `mode=active` och nytt
   `activation_id`/`activated_utc`, låt watchdog etablera färsk
   `active_monitor_ok` och kontrollera därefter dispatch-protokoll
   (`ev_grid_priority_status`, nytt börvärde), reell batteriurladdning,
   nätimport per fas, BMS- och säkringsmarginal. Börja med 6 A L3
   och jämför mot 28 oktober. Avbryt vid avvikelse.
7. Dokumentera ny driftstatus, tidsserier, återställningsresultat,
   exakt release-SHA256, testresultat och beslut i Notion och Git.
   Frisläpp först efter godkänd mätning över relevant belastningsvariation.

## Rollback och oberoende skydd

- Automatisk watchdog: återkalla gate **före** dispatcher-stop,
  stoppa ARMED-tjänsten på ordinarie sätt så att styrningen återgår
  till Auto, återställ backup atomiskt, starta om och kontrollera aktiv
  tjänst samt SHA. Status
  `restored_previous_dispatch` + `rollback_ok=true` betyder framgång.
- Vid `rollback_failed_*`: root-gate ska vara återkallad; **inget
  blint återförsök att skriva över annan dispatcher**. Stoppa och
  felsök med operatör.
- Manuell återställning följer samma ordning. Watchdog-timern får
  inaktiveras **först efter** kontrollerad rollback.
- Den vanliga `solinteg-watchdog.timer` och BMS-skydd ska vara aktiva
  hela tiden. Återgång till tidigare dispatch kan återintroducera
  husbatteriurladdning under EV-laddning; rollback återställer beprövad
  fysisk säkerhet, **inte nätprioriteringens ekonomi**.

## Solmedveten EV/L3-skugganalys – 2026-10-31

**Genomfört, utan fysisk styrning:** Historiska `ev_samples` och
`control_actions` kan nu återspelas via
`scripts/tools/ev_grid_priority_replay.py` (båda SQLite-databaserna öppnas
endast med `mode=ro`). Ett verkligt replayfönster
2026-10-28 15:30 UTC–2026-10-31 09:40 UTC innehöll **7 919 prover**,
**327** verifierade EV-laddprover, **102** samtidiga batteriurladdningar
över 500 W och **sex** sammanhängande episoder. Replay skiljer
`idle_auto`, `planned_discharge`, `charge_pending_response`
och okända beslut. Dessa är korrelationer, inte kausal elflödesmätning.

En isolerad `solinteg-ev-grid-solar-shadow.service/.timer` är installerad
på Dell, med **30 s periodicitet**, som `solinteg-evshadow`.
Rootägd kod under `/usr/local/libexec/solinteg-ev-grid-solar-shadow/`
läser befintliga `live.json`, EVCC:s skrivskyddade FC03-snapshot,
den senaste faktiskt loggade dispatchåtgärden och root-gatens läge.
Den skriver enbart privat advisory JSON till:

`/opt/solinteg/ev-shadow-runtime/var/ev-grid-priority-solar-shadow.json`

Den nya observationsmodellen beräknar en solmedveten hypotetisk
urladdningsgräns och validerar verklig Ict2, 50006=0, fas-CT och
PV/batteri-/AC-konsistens. För `idle`/Auto vid hög PV lämnas förslaget
avsiktligt **utan ett tillämpbart börvärde**
(`solar_hold_unverified`), eftersom påtvingad 50 W-urladdning kan
hindra normal solladdning av husbatteriet. Vid låg SoC och befintlig
`charge` går den befintliga säkerhets-/återhämtningsstyrningen före.

`physical_control_authorized=false`, `model_actionable=false` och
`device_writes=0`. Modellens 230 V-antagande och balanserade
fas-/solfördelning är **inte** ett RMS-fassäkringsskydd.
Historikdatabasen saknar både enskild Ict2 och fasregister 50006 vid
varje prov; replay etiketterar därför dessa uttryckligen som antaganden.

**Datakvalitet i produktion:** 2026-10-31 10:55 CET rapporterade
pollern ett Modbus-avkodningsfel i det utökade CT-blocket, och
`meter_l1_w`/`meter_l2_w`/`meter_l3_w` blev `null` tills dess att
den befintliga retryfunktionen återfår mätvärden.
Den nya observatören visade då `unavailable`
(`invalid or missing meter_l1_w`), utan att använda gamla CT-värden
eller skriva ett nytt inverterbörvärde. Pollern rapporterade att
CT-avläsningen återhämtat sig automatiskt klockan **10:57:29 CET**
efter ett misslyckat läsförsök. En ny observer-körning klockan
**10:57:56 CET** gav åter `existing_charge`, `device_writes=0`,
utan omstart av den vanliga dispatchern. Den ordinarie ARMED-dispatchern
och dess root-watchdog fortsatte vara aktiva; detta är **ingen**
verifiering av säkringsskydd. Fysisk EV-prioritering kräver stabil CT
och oberoende fasström-/RMS-kontroll.

**Driftsättningsgräns:** Den tidigare checksummebundna ARMED-dispatchern,
dess EV-policy och root-watchdog, 6 A EVCC-tak och `gate.json mode=shadow`
är **oförändrade**. Separat explicit tillstånd krävs för att
låta några av de nya observationerna påverka fysisk styrning.

Se [ADR](architecture/adr-2026-10-31-ev-solar-shadow.md) för
beräkningsantaganden och nästa provsteg.

## Nästa skuggsteg: solmedveten Auto/idle-växling

**Utvecklat 2026-10-31, fortfarande inga nya styrskrivningar.**
`scripts/services/ev_grid_priority_idle_strategy.py` utökar den
isolerade read-only-observatören med fältet `idle_strategy`
(när senaste dispatchbeslutet verkligen är `idle`).
Modellen föreslår enbart hypotetiska lägen och kräver fyra
sammanhängande, kvalitetskontrollerade 30-sekundersprover från
`ev-history.sqlite`, samt en stabil dispatchåtgärd över samma
tidsperiod. Aktuell EVCC-Ict2 och 50006 kräver fortfarande separat
färsk verifiering. `work_mode_raw=0x101` måste dessutom bekräfta
att växelriktaren faktiskt är i Auto – ett tidigare loggat
`idle` får aldrig misstas för ett manuellt ändrat inverterläge. Om mätvärden saknas blir resultatet väntan/
`unavailable`, aldrig en påhittad nollström.

Beslutsträd: håll Auto om solöverskottet stabilt täcker **hela**
husets och bilens effekt; enbart testat 50 W-batterisparläge om
solelen med god marginal **inte ens täcker huset utan bilen** och
husbatteriet faktiskt urladdas i flera prover. Föreslagen växling
kontrollerar även konservativt prognostiserad import per fas och
totalt. Mellanläget, då solelen kunde ha laddat husbatteriet om
elnätet försörjt bilen, redovisas som
`pv_first_charge_unverified` **utan något körbart börvärde**,
eftersom den avsedda effektfördelningen inte är verifierad på enheten.
Om solelen redan täcker **hela** huslasten men batteriet ändå
fortsätter urladdas blir resultatet i stället
`solar_surplus_discharge_anomaly`, inte automatiskt `keep_auto`.

En ny read-only replay genom 2026-10-31 11:00 UTC täckte 7 974
prover och 378 verifierade 6 A-laddningsprover. Alla **tre**
verifierade `idle`-prover hade SoC under 25 %. En fysisk
Auto/idle-växling inom godkänt SoC-fönster har därför **inte
bevisats**. Dessutom är 50 W-batterisparläge inte ett sant
0 W-hålläge och det tidigare in-mode freeze-försöket misslyckades.
Inga styrregister, gate- eller watchdogvillkor förändrades.

**Driftsatt 2026-10-31 11:09 CET:** Endast den separata
`solinteg-ev-grid-solar-shadow`-observatörens rootägda Python-filer
uppdaterades på Dell. Ursprunglig observatör finns i en rootägd
0400-backup, medan dess timer åter är aktiv. Kontroll efteråt:
`mode=shadow`, `shadow_monitor_ok`, ordinarie ARMED-dispatcher
och oberoende watchdog aktiva, `device_writes=0`, output 0600.
Eftersom dispatchern fortfarande laddade husbatteriet med SoC cirka
20 %, innehöll observationen `existing_charge` men inget
`idle_strategy` – inga extra `idle`-beslut har utförts eller simulerats
ur ett faktiskt `idle`-läge i produktion.

Se [ADR](architecture/adr-2026-10-31-ev-solar-shadow.md) för
driftsatta SHA256, rollbackbackup, tröskelvärden, mätkvalitetskrav,
falska positiva och begränsningar.

## Kvalitet och avgränsat leveransbeslut

- Offline: befintliga 16 trialpolicyfall/9 trialwatchdogfall
  kompletterade med produktionsgate, statusålder, root-kontroll,
  SHA-lås, L1/L2/L3-import, standby och byteexakt rollback.
- `systemd-analyze verify` ska godkänna de nya enheterna.
- `python3 -m unittest discover -s scripts/tests -p 'test_*.py'`,
  `npm test`, `npm run lint`, `npm run typecheck` ska passera.
- Den godkända driftsättningen 2026-10-31 omfattar endast
  checksummebunden `mode=shadow` och den separata skrivskyddade
  observatören. Alla tillägg som påverkar fysiska styrkommandon,
  inklusive `mode=active` och nya Auto/idle-åtgärder, kräver
  separata säkerhetsgrindar och uttryckligt godkännande.
  Privat konfiguration eller mätloggar får aldrig publiceras.
