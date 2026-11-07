# R03 · Drift-/källavvikelser i dispatcher – 2026-11-07

**Status: läsande semantisk klassificering, inte driftsättning.** Läsande AST-jämförelse av två exakta filer: utvecklingsträdets `scripts/services/dispatch_loop.py` och aktiv `/opt/solinteg/app/scripts/services/dispatch_loop.py`. Den första har **ej incheckade** ändringar och får därför inte användas som reproducerbar releasekandidat. SHA-256 vid inventering: källa `c5eccc5fc381`, drift `274d93f6bdeb` (12 tecken). Full path och full diff sparas inte i Git. 45 top-level-funktioner/klasser i utvecklingskälla mot 39 i drift; **32 samma AST** (utan positionella attribut), **7 ändrade** och **6 endast i källan**. Python AST-likhet är inte ett fysikbevis.

| Funktion/område | Verifierad skillnad | Varför / bevarandeplan | Klass |
| --- | --- | --- | --- |
| `guard_l3_charge` (EV/L3) | Båda finns, AST skiljer (39 kontra 38 rader), samma yttre anropsyta | Prioriterade L3-fasgränser berör 20 A huvudsäkring; **bevara driftversion** tills source/site-begränsningar jämförts i riktade EV-L3-test | **prove**, P0 |
| `verify_l3_balanced_output` | Endast source | Extra observation mot faktisk fasgräns; aldrig anta installerad verifiering | **keep source-only**, P0, prova utan Modbus-write |
| `coordinate_l3_battery` | Endast source | Ytterligare prioritering av bilens fas och batteri; kräver kontinuitetsprov för aktiv L3-lastbalans | **keep source-only**, P0 |
| `capture_battery_save_solar` | Endast source | PV-surplus inför viloläge/batterispar och observationspolicy; inte aktiv i installerad huvudloop | **keep source-only**, P1 |
| `BrokerTelemetryInhibited`, `require_fresh_broker_sample`, `record_broker_inhibit` | Tre endast source | Nya inhibition/telemetrigap-kontrakt; inte ett aktivt brokerfailsafe-bevis. Behåll source och tester för framtida S06–S08 | **keep source-only**, P0 |
| `needs_apply` | AST skiljer, 44 mot 42 rader, samma anropsyta | Potentiell retargeting-/idempotensskillnad; drift får inte ersättas förrän konsekvens på 15-minuters-tick har testats | **prove**, P0 |
| `prioritize_soc_recovery` | AST skiljer, 90 mot 65 rader | Källan introducerar separat `reserve_hold` med `RESERVE_HOLD_VERIFIED`; installerad kod saknar den vägen. Fysiskt hold kräver en **separat** verifierad flagga | **keep drift, prove source**, P0 |
| `apply_target` | AST skiljer, 50 mot 42 rader | Endast source anropar `force_hold`; aktiv version känner inte den exekveringsgrenen. Får ej porteras utan fysisk hold-kvalificering | **keep drift, block port**, P0 |
| `apply_decision` | AST skiljer, 110 mot 94 rader | Source har `require_fresh_broker_sample`, `record_broker_inhibit` och nytt undantag; drift saknar dem. Källans tillståndsövergång är ej kvalificerad | **prove**, P0 |
| `write_heartbeat` | AST skiljer, 16 mot 10 rader | Source exponerar `broker_policy` och `supervisor_required`; aktiv legacy-watchdog läser **bara timestamp**. Ingen automatisk ändring i säkerhetsklassning | **keep source-only diagnostic**, P0 |
| `main` | AST skiljer, 182 mot 114 rader | Source kedjar fas-/sol-observation och brokerinhibition; installerad loop saknar dessa anrop men har aktiv `arbitrate_energy_priority`. Anslutningsordning/undantagsvägar kan ändra fysisk policy | **prove/keep drift**, P0 |

**Viktigt:** ovanstående 13 *funktionsavvikelser* skiljer sig från de 12 ursprungligen smutsiga arbetsfilerna i B04. Siffrorna kommer från aktuell filinventering och är inte samma mängd.

## Kontinuitets- och regressionsgrind

1. **Bevara driftsversionen** oförändrad. Följ repo/diff i [R01-manifestet](operations/release-inventory.md) och [B04-klassificeringen](plans/preexisting-work-classification-2026-11-07.md).
2. **Ingen automatisk keep/port till live** för någon av de 7 ändrade funktionerna eller 6 nyintroducerade; fel i `main/apply_decision/needs_apply` kan ge kvarhängande EMS. `prove` är en **öppen blockerare**, inte en godkänd migration.
3. Riktade regressioner: `test_dispatch_loop.py` för SoC/EV-L3/heartbeat, `test_modbus_broker_control.py`, `test_modbus_broker_transactions.py` och särskilda `test_safety_gap_contracts.py`. Källan har klarat full local unittest 1641/1641; detta bevisar inte att driftpatchen kan ersättas.
4. **Före GO:** skapa en reproducerbar källa för alla lokala ändringar, test av installerad site-funktionsnivå, registrera fulla men **privata** semantiska diffar, verifiera inga bortfall i fas-/SoC-skydd och bevara rollback. Kontrollen är läsande; ingen `systemctl`, TCP:502 eller registerskrivning genomfördes.

**Disposition:** `keep` = bevara för utveckling utan live-ändring; `prove` = jämför och verifiera innan något får portas; `block port` = kräver särskilt GO för fysik/failsafe. Inga funktioner är klassade `remove`, eftersom faktiskt oanvända entrypoints inte är bevisade.