# EV/L3-lastbalansering: kod, dokumentation och Dell-drift (2026-11-04)

**Status: produktionsaktivering av EV-nätprioritet = NO-GO.** Skrivskyddad inventering och historisk återspelning genomförda; ingen tjänst, env, gate, hårdvaruregister eller laddboxinställning ändrades. Detta är ett aktuellt driftkomplement till [EV-releasehistoriken](ev-grid-priority-permanent-2026-10-29.md), [designspecifikationen](superpowers/specs/2026-10-19-smart-ev-charging-design.md) och [brokerreleasen](modbus-broker-transactions-release-2026-11-04.md). Äldre daterade driftpåståenden gäller endast sina angivna kontrolltidpunkter.

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

**Resultat för denna granskning:** Dokumentations- och testarbete, ingen produktionsändring. Ingen ny service, timer, städning, arbetskopia eller provsättning har skapats; gammal root-rollback och bevarade backup-/USB-arkiv lämnas orörda. Nästa fysiska steg är uttryckligen **blockerat** av otillräcklig aktuell EVCC-evidens, gammal patchbaseline och otillräcklig validering av 6 A-detektion.
