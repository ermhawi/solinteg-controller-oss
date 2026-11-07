# D02 · Körbart beteendekontrakt: installerad avsikt och faktisk källsemantik

**2026-11-07 — `tested/source`, ingen ny styrning och ingen fysisk kvittens.** Utgångspunkten är read-only AST av faktiskt installerad `/opt/solinteg/app/scripts/services/dispatch_loop.py` med SHA-256 `274d93f6bdeb` och dess 13 [prioritetssteg](../dispatcher-installed-priority-2026-11-07.md). Den installerade `energy_priority_policy.py` är enligt R01 byteidentisk med källan, men själva `dispatch_loop.py` skiljer sig från Git i 13 definitioner. Kontraktet nedan **fryser avsedd logik** i rena källkomponenter utan att påstå att samtliga steg redan genomförs fysiskt.

| Given | When | Then: körd, deterministisk förväntan | Bevis / driftgräns |
| --- | --- | --- | --- |
| SoC 13 %, recovery redan aktiv, EV okänd | `energy_priority_policy.resolve` körs | `winner=soc_recovery`, tidigare `charge 500W` hålls | `test_recovery_wins_without_implicitly_applying_battery_power`. Den faktiska site-recovery-funktionen ligger **före** denna resolver |
| SoC 16 % under 20 % ekonomisk reserv, verifierad EV ej laddande, två giltiga sol-/nätprover | Ren prioriteringspolicy | `winner=solar_capture, idle/0` | 2 giltiga prover i test. `idle`/General är **inte** ett garanterat fysiskt 0-W-frys-läge |
| Samma undervärde men EV-status `unknown` | Ren prioriteringspolicy | `unverified_ev, discharge/50W`, aldrig klassificerat som EV-off | Ett mjukt EMS-sparbörvärde är **inte** bevis att batteriet inte laddar bilen |
| SoC 55 % över reserv, EV laddar, ekonomi vill `discharge/650W` | Ren prioriteringspolicy | `economic_plan, discharge/650W` bevaras | **Känd risk**: enbart denna källa garanterar *inte* EV grid-first; integrerad `coordinate_ev_grid_live`/L3 krävs |
| 1 380 W EV, 4 000 W PV, 1 000 W övrig huslast, husbatteri ej bevisat fullt | `solar_priority_budget` beräknas | EV grid-target 1 380 W, sol till EV 0 W; uppskattat husbatteri-PV-headroom 3 000 W | Nominal 230 V/PF≈1, **planerad allokering**, inte verifierad fysisk strömväg |
| Samma men oberoende bevisat fullt husbatteri | Ren solbudget | EV får upp till 1 380 W sol före export; EV grid-target 0 W | Fortfarande **endast** rekommendation, ej FC10/FC06 |
| EVCC visar 16 A på L3 och projektets konservativa fasbudget överträds | `ev_l3_metered_policy.assess` | `state=phase_import_bound`, inget fysisk handlingsbart kandidat-W | Separat skyddskedja; `physical_control_authorized=false`, `device_writes=0` |
| Operatör har ändrat till EMS BattCtrl, men historisk `idle` föreslås; alternativt EV-prispaus redan `owned/pending/releasing` | Idle-modell respektive manual-CLI-ägarkontroll | `dispatch_mode_mismatch` utan hypotetisk Auto-åtgärd; manuell FC10-väg **nekas** | En gammal dispatcher-slot får inte överstyra fysisk manuell intervention eller återuppta användarens paus |

**Exekverbart test:** `scripts/tests/test_dispatch_intent_contract.py` **8/8 PASS** med faktiska, rena policyfunktioner, fasta in-memory tidsstämplar och syntetiska L3-/EVCC-prover. Även E04-samlås och D03:s 8 ren-policyfall finns separat. Inga sockets, databasskrivningar, aktiverade systemd-enheter eller fysisk Modbus/EVCC.

**Prioritetsordning i installerad dispatcher (read-only D01):** plan → ekonomisk sparning/solfinansiering → SoC recovery → EV grid-first → energi-arbiter → L3-effektgrind → effective target → `needs_apply` → fysisk writer → readback/heartbeat. D02 är en **testad behavior baseline**, inte ett förslag att ändra ordningen; riskerna ska upptäckas i D03/D05–D08 innan en sådan ändring får kvalificeras.

**Definition of Done för D02:** åtta GWT-scenarier täcker recovery, EV-grid-first-intention, solupptagning, ekonomisk reserv, prisplan, fasskydd och manuell intervention. **Avsiktligt utanför D02:** faktiskt installerad golden-master-replay av alla 13 steg, kvitto på fysiskt EV grid-first, HIL-prov, SQLite publicering/TTL och eventuell beteendeförändring. De är fortfarande **NO-GO** under D03/D05–D09, S07/S08 och V02–V06.
