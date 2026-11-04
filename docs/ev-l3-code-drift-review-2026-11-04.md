# EV/L3-lastbalansering: kod, dokumentation och Dell-drift (2026-11-04)

**Senare driftuppdatering 2026-11-04:** Den separat låsta
**EVCC-priscontrollern** är nu aktiverad på Dell (max 6 A eller paus,
användargodkänd empirisk energi); inventeringen nedan är fortsatt
historisk för det tidigare read-only-läget. Ordinarie EV/L3-dispatcher
är oförändrad. Se [aktuell EVCC-aktiverings-ADR](architecture/adr-2026-11-04-ev-price-empirical-live.md).

**Nyare driftstatus 2026-11-04 ca 12:50 CET:** Denna inventering är en historisk baslinje. Ett begränsat **live EV/L3-läge** via befintlig dispatcher och Modbus-broker är aktiverat; Systemstatus visar separat bil, och fem gamla shadow-enheter har arkiverats krypterat och avvecklats. Verifierad fysisk 100 % nätfinansiering saknas fortfarande. Se [ny ADR med nuvarande drift och rollback](architecture/adr-2026-11-04-ev-l3-live-grid-first-and-ui.md).

**Historiskt utgångsläge för denna granskning:** Skrivskyddad inventering och historisk återspelning genomförda; ingen tjänst, env, gate, hårdvaruregister eller laddboxinställning ändrades. Detta är ett aktuellt driftkomplement till [EV-releasehistoriken](ev-grid-priority-permanent-2026-10-29.md), [designspecifikationen](superpowers/specs/2026-10-19-smart-ev-charging-design.md) och [brokerreleasen](modbus-broker-transactions-release-2026-11-04.md). Äldre daterade driftpåståenden gäller endast sina angivna kontrolltidpunkter.

## 1. Dokumentationspreflight: faktiskt nuläge kontra källkod

Inspekterat repository: `ermhawi/solinteg-controller-oss`, permanent `Sundsvall`, HEAD `46f0fa5`. Git rent, tracking branch synkroniserad, en worktree, inga stashes eller omergade grenar vid preflight. Miljön är **Dell**, inte en obekräftad framtida Pi-installation.

| Del | Git `Sundsvall` | Installerat på Dell 2026-11-04 | Bedömning |
| --- | --- | --- | --- |
| Normal Modbus-broker | `modbus_broker*.py`, `inverter_control.py`, transaktionsworker | Installerade broker-/kontrollmoduler **byteidentiska** med Git; broker, poller och dispatcher aktiva med `SOLINTEG_BROKER_TRANSACTIONS_ENABLED=1` / `SOLINTEG_MODBUS_SOURCE=broker_transaction` | **Driftsatt och initialt liveverifierat**; 24–72 h uppföljning kvarstår |
| L3-vakt för **husbatteriets laddning** | `l3_charge_guard.py`, anrop från `dispatch_loop.py` | Funktion och anrop finns; `DISPATCH_L3_CHARGE_GUARD_MODE=1` | **Aktiv ekonomisk begränsning**, inte säkringsskydd |
| L3-samordning för **batteriurladdning mot bilen** | `l3_battery_policy.py` och `coordinate_l3_battery()` i dispatchern | `l3_battery_policy.py` saknas; anrop/import saknas; `DISPATCH_L3_BATTERY_MODE=shadow` i processmiljön | **Inte driftsatt, inte ens som inbyggd shadow-hook**; env-flaggan är verkningslös |
| Äldre EV-grid-priority med rootgate | `ev_grid_priority_guard.py`, separat watchdog och observatör | `gate.json` saknas; terminal rollbackstatus `restored_previous_dispatch` finns; watchdog-timern aktiv; ingen aktiverad dispatcher-hook | **Återkallad sedan 2026-11-01**. Aktiv timer är inte bevis för armering |
| EVCC-/Volvo-observation | HA-, EVCC-, shadow- och historiktjänster | Samtliga fyra observationstjänster aktiva, utan autonom EVCC-skrivning; aktuellt EVCC-sample `read_ok=false`, `state=unknown` | **Read-only**, inte verifierad EV-laddning nu |
| `solar_charge` under EMS-batterisparläge | `capture_battery_save_solar()`, default av | Funktionen saknas i produktionsdispatchern; ingen flagga för fysisk aktivering | **Endast källkod/offline** |
| EV-/prisvyn och automatisk EVCC-kontroll | `app/ev`, `EvChargingPlanner`, `evcc_service.py` | EV-vy/planerare saknas i installerad webbapp; EVCC-styrservicen saknas/inaktiv | **Ej driftsatt** |

Installerad `dispatch_loop.py` har SHA-256 `34d0a927d970a1de343a16dd22d2c13fb6d1eb43d665dc5ec4bce8d740b84e57` (1 848 rader) jämfört med Git-versionens 2 197 rader. AST-jämförelsen visar sex Git-exklusiva funktioner/klasser: `coordinate_l3_battery`, `verify_l3_balanced_output`, `capture_battery_save_solar`, `require_fresh_broker_sample`, `record_broker_inhibit`, `BrokerTelemetryInhibited`. Sju befintliga funktioner har skillnader, bl.a. `main`, `apply_decision`, `prioritize_soc_recovery`. **Ersätt aldrig den installerade dispatchern med hela Git-filen.**

Brokern ändrar **transporten för befintliga styrsekvenser**, inte batteri-/EV-policy. Gammal watchdog och `DISPATCH_BROKER_STALE_GUARD=0` behålls enligt [risk-ADR](architecture/adr-2026-11-04-broker-legacy-failure-scope.md); detta är ett accepterat felvägsgap, inte åtgärdat.

## 2. Verifiering mot verklig laddhistorik (enbart läsning)

- `python3 -B -m unittest discover -s scripts/tests -p 'test_l3_battery_policy.py'`: **27 OK**.
- `test_ev_grid_priority_guard.py`: **20 OK**; `test_ev_grid_priority_preflight.py`: **6 OK**; `test_ev_grid_priority_replay.py`: **6 OK**. **Totalt 59 riktade tester OK.**
- `sudo -n python3 -B scripts/tools/ev_grid_priority_replay.py --since 2026-10-28T00:00:00+00:00 --until 2026-11-05T00:00:00+00:00 --max-examples 3`: **543** historiska verifierade EVCC-6 A-prover; **122** med samtidig uppmätt husbatteriurladdning över 500 W; **24** EV-prover under loggat `idle`, **alla utanför** legacy-policyns SoC-band 25–95 %. Ingen kausal energiöverföring från husbatteri till bil är bevisad.
- Ytterligare read-only genomgång med `l3_battery_policy.observe_l3` och `suggest_battery_target` av samma 543 prover: **15** når den hårdkodade bil-liknande tröskeln `2500 W` i härledd L3-huslast; **0** skulle ändra de *historiskt loggade planerade* åtgärderna. Detta är inte ett fysiskt effektprov: `inverter_ac_w` rekonstruerades från `grid_w + house_load_w` (ingen oberoende AC-readback i historiken), och en planerad åtgärd behöver inte ha verkställts. **2500 W-tröskeln har inte demonstrerad täckning för aktuell 6 A-laddning.** Att sänka den utan separat EV-verifiering riskerar falska positiva vid andra L3-laster.
- **311** historiska 6 A-prover har PV över 100 W; **311** har SoC under 25 %. Historisk `50006` och separat EVCC-Ict2 saknas för flertalet prover. Replay ersätter inte färskt fysiskt kontrollprov.

### Live-preflight 2026-11-04

Vid kontrollen: `soc_pct=20`, `50006=0` i senaste poller-snapshot, aktiv broker/poller/dispatcher utan omstarter, men EVCC `read_ok=false`, `state=unknown`, och root-gate saknas. `ev_grid_priority_preflight.py` avbryter **korrekt med exit 2**: den äldre patchgeneratorn kräver SHA-256 `a2f3aba4…`, medan installerad dispatcher har ändrats. Dess `baseline_verified=false` och `physical_activation_authorized=false` är ett verkligt **NO-GO**, inte ett skäl att kringgå hashlåset.

**Regression och dokumentationskontroll 2026-11-04:** Full Python-svit **1 366 tester OK**; frontend **705 Vitest OK, 1 överhoppat**, ESLint och TypeScript typecheck godkända; Zensical dokumentationsbygge godkänt utan problem. Detta är offline-/källkodstest, inte ett fysiskt EV-lastprov.

## 3. Rekommenderad teknisk följd och aktiveringsgrindar

1. **Källa → installerad kod:** jämför de sju ändrade funktionerna och miljöinställningar för att avgränsa en **minimal, ny, hashbunden** patch till aktuell produktionsbaseline. Bevara site-specifik ekonomi-/SoC-recovery, befintlig importgräns, broker-transaktionsläge och watchdog helt oförändrade. Inga automatiska kopior av hela Git-dispatchern.
2. **Kalibrera och testa EV-detektion:** kör separat shadow-replay med verklig, verifierad 6 A/16 A-laddning, icke-EV-laster på L3, Auto/idle och planerad urladdning både med och utan PV. 2500 W-tröskeln måste bedömas och en falsk-positiv-grind etableras. Färsk EVCC-Ict2 och live `50006=0` krävs för fysisk evidens; historikantaganden räcker inte.
3. **Shadow-installation, först efter separat releasepreflight:** testa exakt installerad kandidat mot dess nuvarande källhash, backup/restore i isolerad miljö och watchdogens `gate.json`-hashbindning. Bekräfta att `shadow` lämnar hela styrbeslutet byte-/värdemässigt oförändrat. Kontrollera att broker/poller fortsätter fungera och att ingen EVCC/inverter-writes utlöses av EV-hooken.
4. **Fysisk aktivering kräver nytt, uttryckligt GO:** verifiera SoC (legacy-grind minst 25 %), batteritemperatur, 50006, tre CT-faser, Ict2/PWM/readback, nät-/L3-säkringsmarginal, SOC-recovery och solöverskott. Värdera även planerat hög-PV Auto/idle-fall där befintlig 50 W EMS-discharge kan hindra solelsladdning. Ingen fysisk `mode=active` eller `DISPATCH_L3_BATTERY_MODE=1` på nuvarande bevisnivå.
5. **Rollback/uppföljning:** rotägd verifierad kopia av exakta installerade filer, kontroll av systemd-processer och ensamt broker-TCP-ägarskap, isolerat rollbackprov, sedan övervakad livekontroll av verkligt nät-/batteriflöde och 24–72 h driftsuppföljning. Vid fel lämnas befintlig Solinteg-watchdog/failure-policy oförändrad enligt användarbeslut; de kända riskerna kvarstår.

**Resultat för den första granskningen:** Dokumentations- och testarbete, ingen produktionsändring. Ingen ny service, timer, städning, arbetskopia eller provsättning skapades; gammal root-rollback och bevarade backup-/USB-arkiv lämnades orörda. Den senare 10:02-kontrollen nedan är ett separat uppföljningssteg.

## 4. Waveshare åter ansluten – kontroll 2026-11-04 ca 10:02 CET

**Endast observation och offline-releaseförberedelse; ingen fysisk driftsättning.**
Den befintliga `solinteg-ev-observer` läser EVCC igen. Historikens senaste fem
minuter gav **10/10** färska lyckade read-only EVCC-prover (`read_ok=1`).
Aktuell status var `A1`, alltså **ingen verifierad bil-laddning**;
`configured_pwm_raw=1000` är EVCC-observatörens **paussentinel** medan
`pilot_pwm_raw=267` skiljer sig. Strömvärden saknas och inga EVCC-skrivningar
har utförts. Ändra **inte** 6 A-/PWM-inställning automatiskt på denna evidens.

Historisk tröskelanalys (validerade, färska EVCC- och fas-CT-prover):

| L3-tröskel | Verifierad laddning 6 A | Verifierad ej laddande |
| --- | --- | --- |
| 1 200 W | 542/543 | 31/162 |
| 1 500 W | 414/543 | 29/162 |
| 2 500 W (befintlig) | 15/543 | 13/162 |

En statisk 1 200 W-tröskel ger god känslighet men **falska positiva** från
övriga L3-laster. Därför ändras inte källkodens produktionspolicy eller
tröskel. Om senare EV-batterireglering införs måste färsk, verifierad EVCC
Ict2, konsekventa fas-CT och modellens säkerhetsgrindar stödja varje beslut.

Nytt **skrivskyddat** verktyg:
`sudo -n python3 -B scripts/tools/ev_l3_readonly_audit.py`. Det återanvänder
den befintliga EV-grid-priority-policyfunktionen med ett **syntetiskt
`mode=shadow`-manifest i minnet**. Det skapar inte rootgate,
aktiverings-ID på disk eller styrbefogenhet. Livekörning gav
`evcc_read_ok=true`, `evcc_state=A1`, `evcc_paused_by_pwm=true`,
`evcc_pwm_matches=false`, giltiga fasdata och `50006=0`, men
`preview_state=unavailable`, `hypothetical_discharge_w=null` och
`device_writes=0`.

Ny **offline och SHA-låst** kandidatgenerator:
`sudo -n python3 -B scripts/tools/prepare_ev_l3_shadow_dispatch.py`.
Den accepterar endast nuvarande installerad dispatcher-SHA-256
`34d0a927d970a1de343a16dd22d2c13fb6d1eb43d665dc5ec4bce8d740b84e57`,
injicerar endast **opt-in-loggning** före befintlig L3-laddningsvakt och
bevarar hela fysiska styrtuple oförändrad. Loggning är av som standard
(`DISPATCH_EV_L3_SHADOW_LOG=0`). Kandidaten verifierades mot verklig
produktionsbaseline **i minnet** (SHA-256
`14d97403fbf4e0af473ce3fb9277ee388df1ce1bde0b0ba8d3816e844ebd8d57`)
och i isolerade unit-tester. **Denna kandidat-SHA är historisk** och har
ersatts av den senare mätbaserade Ict2-kandidaten i avsnitt 5.
**Ingen fil kopierades till
`/opt/solinteg/app`; ingen dispatcheromstart, drop-in, rootgate eller
kodaktivering gjordes.** Den gamla watchdoggen och brokertransaktionerna
lämnades orörda.

**Verifiering:** Totalt **1 376 Python-tester OK** (inklusive 10 nya tester),
**705 frontendtester OK** och ett överhoppat, ESLint och TypeScript typecheck
OK. Zensical-dokumentationsbygge och `git diff --check` godkända.
Detta är offline-/källkodsverifiering, inte ett fysiskt EV- eller
återställningsprov.

**Kvarstående GO-grind:** faktisk, stabil EVCC-`C2`/Ict2-laddning och
samstämmig pilot/readback, samt SoC, temperatur, PV, fas-/säkringsmarginal
och en särskilt verifierad återställningsväg. Ett nytt releasebeslut krävs
innan även logg-hooken installeras i ARMED-dispatchern (omstart kan påverka
växelriktaren kortvarigt), och ytterligare separat GO krävs innan något
fysiskt EV-prioritetsbörvärde tillåts. Pågående 24–72 h brokeruppföljning
ersätts inte av dessa tester.

## 5. EVCC Ict2 som primär lastsignal – 4 november, separat uppföljning

**[ADR för den nya mätbaserade policyn](architecture/adr-2026-11-04-evcc-ict2-metered-priority.md)** är källan för beslut, ekvationer, gränser,
driftsättning och rollback. Den äldre L3-huströskeln på 2 500 W ersätts
**bara i en ny read-only/shadow-kandidat**, inte i den aktiva dispatchern.

- `ev_l3_metered_policy.py` använder **verkligt uppmätt Ict2** under
  färsk `C2` och samstämmigt PWM-readback för 6–18 A. `230 V × Ict2`
  är en uppskattning av EV-effekt (PF=1 antas); ingen verklig
  effekt- eller energimätare, och ingen säkringsgaranti. Den
  budgeterar **enbart minskning av planerad batteriurladdning** efter
  verifierad `50006=0` och konsistenta L1/L2/L3, AC, PV, SoC,
  batteritemperatur och importprognos.
- Vid `idle/Auto`, sol över 100 W, SoC utanför 25–95 %, okända
  mätvärden eller förutsedd fasimport över mjuk gräns returneras
  **inget verkställbart eller hypotetiskt batteribörvärde**. Testade
  exempel: 6 A ≈ 1 380 W, 10 A ≈ 2 300 W; 16–18 A effekt
  identifieras men de testade fasgränsscenarierna **blockeras**.
- `ev_l3_readonly_audit.py` visar nu den nya kandidatens
  `metered_policy` parallellt med tidigare nattliga 6 A-scenario.
  `prepare_ev_l3_shadow_dispatch.py` är fortsatt endast en
  **SHA-låst, ej installerad** logg-hook (ny kandidat-SHA-256
  `20bf7f5cad47157b81ab5b93b892892c83fd6033db3c299aeb73cbd3ab0328a4`).
- **Endast isolerad skrivskyddad historiktjänst** uppdaterades på Dell:
  `solinteg-ev-history.service` kör ny
  `ev_history.py` med SHA-256
  `b56b43b7c3c003743abaf7232a50c81ebac901142d291b32e7222eb42afa577d`.
  Sex additiva nullable SQLite-kolumner för Ict1/Ict2/Ict3,
  inverter-AC, batteritemperatur och 50006 migrerades utan att
  ändra gamla prover. `quick_check=ok`, nya rader skrivs och
  `physical_writes=0`; avgränsad rootägd återställningskopia finns
  enligt ADR. **Ingen produktionsdispatcher, Modbus-broker,
  EVCC-styrning eller watchdog ändrades.**
- Nytt replay `ev_l3_metered_replay.py` läser endast **verifierad**
  separat historisk Ict2 och 50006. Direkt efter migrationen
  fanns **2 877** lästa prover under senaste dygnet men ännu inget
  nytt `C2`-prov med komplett bevisning; det kan därför inte ge ett
  fysiskt GO. EVCC rapporterade fortsatt `A1`, paus/mismatch
  `1000/267`, ingen faktisk bilström.
- **Slutregression:** 1 404 Python-tester OK, 705 frontendtester OK
  (ett överhoppat), ESLint och TypeScript typecheck godkända.

**Status:** enbart Ict2-observation och säker offline-reglerkandidat.
Fysisk nätprioritet och integrerad dispatcher-shadow är **inte
driftsatta**. Nästa krav är en stabil laddsession med verifierad Ict2
på L3 och samstämmig pilot samt separat sol-/Auto-prov med
återställningsbar releaseplan; inga automatiska PWM-ändringar.

## 6. Korrigerad prioritet efter övervakat 6 A-laddprov 2026-11-04

Under ett separat godkänt engångsprov ändrades endast EVCC:s PWM från paus
(1000) till 100 (6 A), med manuell FC10, låsning och dubbel FC03-läsback.
Bilen gick därefter till `C2` och Ict2 visade 6,0–6,1 A. Broker,
dispatcher, inverter och ordinarie watchdog lämnades orörda.
Observer-/historiktjänsterna fungerar; privat provlogg finns på Dell.
Detta verifierar **EVCC-strömmen, inte nätprioriteten**.

**Kravförtydligande:** vid SoC 24–25 % och solproduktion upp till cirka
2,7 kW ska bilen fortfarande budgeteras för **full nätimport**.
En felsökningsavläsning med Ict2=6 A, 2 740 W PV och uppskattad
icke-EV-huslast på 688 W gav modellen solöverskott till lagring
på högst cirka 2 052 W, **ingen** solel till bil, och ett mål om
1 380 W L3-nätimport för bilen. Samtidigt var faktisk L3-import
cirka 551 W. Differensen 829 W är en **proxy för målavvikelse**,
inte fysikaliskt bevisad batteri-till-bil-överföring.

Den nya `ev_l3_metered_policy.py` har därför fått en separat,
ren `solar_priority_budget()`:

- **Inte fullt:** `ev_grid_target_w_est = 230 × Ict2`,
  `ev_solar_allowance_w_est = 0`, överskott efter icke-EV-huslast
  reserveras för **husbatteriets laddning**; ingen säker exportbudget
  utan BMS-laddgräns och verifierad möjlighet att lagra.
- **Oberoende verifierat fullt:** fördela PV efter icke-EV-huslast
  **först till bilen**, sedan till export; övrig EV-effekt budgeteras
  som nätimport. Denna fullverifiering finns ännu inte i den
  installerade `live.json`; det **syntetiska fullfallet är enbart
  unittest**.
- **Ingen EV-laddning:** återstående PV efter huslast vid verifierat
  fullt batteri går till export enligt samma rena prioriteringsregel.
- **PV i sig är aldrig en blockerare.** Vid solöverskott som skulle
  kunna ladda husbatteriet får modellen status
  `solar_storage_priority_unverified`, med uttryckligt önskat
  ladd-/nätmål men **inget inverterkommando**. Den bevarar befintliga
  `charge`-/återhämtningsåtgärder och SoC-/BMS-grindar. `idle/Auto`
  och `solar_charge` kräver fysisk BMS-/EMS-verifiering; 50 W
  påtvingad urladdning är **inte** godkänd lösning under solöverskott.

**Driftstatus:** endast källkod/offline-shadow. Befintlig fysisk styrning
är oförändrad. Solintegs balanserade inverter kan inte märka PV- och
nätwatt efter källa: önskad L3-import måste verifieras tillsammans
med PV→batteri, nät-CT och faktisk batteriladdning. Ingen fysisk
EV-grid-priority-aktivering förrän gränser, laddläge, stabilitet,
återställning och failure-scenario är separat verifierade.

## 7. Driftsatt aktiv read-only-shadow i befintlig sidecar (2026-11-04)

Under det senare, separat godkända shadow-driftsättningssteget aktiverades
**den nya beräkningen, inte den fysiska styrningen**. Den befintliga
`solinteg-ev-grid-solar-shadow.timer` kör var cirka 30:e sekund.
Dess befintliga 0600-JSON har nu ett fristående
`metered_policy`-objekt med EVCC-Ict2, bedömt nätimportbehov för bilen,
icke-EV-huslast, PV-laddningsbudget för husbatteriet, L3-importavvikelse
och `model_actionable=false`. Den äldre rootgate-spärrade
toppnivåstatusen kan fortsatt säga `disabled`, men detta blockerar
**inte** den nya observationen och ger inga nya skrivrättigheter.

**Faktisk första körning:** Ict2 6,1 A, EV-mål 1 403 W
från nätet, möjligt 340 W solöverskott reserverat till
husbatteriet. Låg SoC gav `metered_policy.state=soc_or_bms_guard`.
Källkodens beslut har inga utgångar till styrning. Ursprunglig
installed observer SHA-256 var
`91b569847b2380304c038913cd0a68f915117edb663f04f01f5caae5596aeb79`,
nya SHA-256
`eac42a0f1ddb2734c93a2d827a9bc12ab41cc82897b7ff23e36f8ae28837b0cc`.
Rootägda rollbackfiler och säker isolering dokumenteras i
[mät-/release-ADR](architecture/adr-2026-11-04-evcc-ict2-metered-priority.md).

**Fortfarande saknas:** bevisad fysisk allokering av nätel till EV,
tillräckligt BMS-styrd solelsladdning, oberoende bekräftelse av fullt
hushållsbatteri, test av verifierad kontrollavvikelse och fysisk
fuse-/fallback-verifiering. Ingen fysisk EV-grid-priority-aktivering.
