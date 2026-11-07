# S06 · Opt-in-behörighetsgrind för en framtida broker-supervisor

**2026-11-07 · `tested/source`, INTE installerad eller fysiskt kvalificerad.** Den faktiska `solinteg-broker-control.service` har ännu **ingen** tillåten supervisor-ägare. Den här ändringen korrigerar den tidigare missmatchningen mellan `safety`-roll och `Broker.submit(safety_auto)` endast bakom en separat, avstängd flagga. Den installerade, oberoende Dell-/Wi-Fi-watchdoggens ansvar förändras **inte** och varken ny systemd-unit, nätfencing eller automatisk recovery är driftsatt.

## Avgränsad källa

- `scripts/services/modbus_broker_safety_policy.py` äger konstanten `SUPERVISOR_UNIT=solinteg-broker-safety-supervisor.service`, exakt opt-in `SOLINTEG_BROKER_SAFETY_AUTO_ENABLED=1`, samt en ren, konservativ FC03-baserad preflight. Flaggan är **default OFF**; andra strängar, inklusive `true`/okänd, ger aldrig implicit behörighet.
- `role_for_systemd_peer` i `modbus_broker_control_service.py` hämtar fortsatt **OS-kärnans** `SO_PEERCRED` och `/proc/<pid>/cgroup` och kräver samma effektiva UID som brokerprocessen. Endast den **exakta** framtida unitens cgroup och explicit aktiverad flagga ger rollen `safety`; `solinteg-dispatch.service`, äldre watchdog, poller, HTTP-klient och namn med suffix nekas säkerhetsrollen.
- `build_control_broker` accepterar endast sträng `0` eller `1`; ett aktiverat supervisor-läge kräver **samtidigt** `armed=True`, `runtime_mode=control` och redan godkända complete-transactions. Den särskilda operationen `safety_auto` är avvisad i control-mode utan flaggan, och safety-rollen får **inte** skriva rå FC06 eller köra normal ladd-/urladdningstransaktion.
- `Broker._apply` gör, vid opt-in-säkerhetsoperation, **en färsk serialiserad FC03-snapshot** och kräver `status=valid`, `sample.quality=valid`, SoC **minst 20 %**, ändligt rimligt SoC, samt endast General eller EMS BattCtrl. För General returneras `already_general` utan ny FC06. Vid EMS krävs dessutom brokerintern **verifierbar ägarskapsflagga**, annars stoppas skrivningen. En ytterligare generationskontroll görs före ett möjligt Auto-anrop. `registers_verified_only` är **inte** ett fysiskt effektbevis.
- **Ingen** av dessa signaler kan bevisa fysisk 20 %-EndSOC, permanent nätverks-/TCP-exklusivitet, stängd gammal socket efter processdöd eller korrekt battery-W efter Dell-strömbortfall. Kraven S07/S08/V gäller fortsatt och ingen release-GO följer av att källkoden passerar tester.

## Riskmatris och acceptans

| Källa/kallelse | Förväntat beslut | Faktisk skrivning i offlinefixture |
| --- | --- | --- |
| Flagga unset/`0`, även från supervisorcgroup | `PermissionError` i control runtime | **0**, inte ens TCP-factory öppnas |
| Exakt supervisorcgroup + annan effektiv UID | Ingen safety-roll | **0** |
| Äldre watchdog, dispatcher, poller, efterliknande cgroup | Ingen safety-roll; control-rollen kan aldrig begära `safety_auto` | **0** |
| Flagga `1`, men runtime saknar arm/transaction-mode | Konstruktion nekas | **0** |
| Flagga `1`, giltig syntetisk General/SoC 56 % | `already_general` | **0 FC06** |
| Flagga `1`, SoC 13 %, trasig CT eller oägd EMS | `PermissionError`, latch förhindrar ny normalstyrning tills operatör | **0 FC06** |
| Flagga `1`, syntetiskt känd brokerägd EMS/SoC 55 % | Endast samma workers serialiserade Auto + readback, med caching av request-ID | **En serie i en fake**, utan fysisk garanti |

**Testunderlag:** `scripts/tests/test_modbus_supervisor_access.py` **8/8**, kompletterat med tidigare `test_safety_gap_contracts.py` **5/5** och befintliga broker-/IPC-tester. Alla externa Modbus-/EVCC-anslutningar saknas i fixtures. Kvarstående **NO-GO**: en särskild godkänd framtida supervisor-unit existerar inte i runtime, programmatic safety-auto når inte säkert OEM/BMS-golv utan vendorbevis, och workerns `_owns_control` måste härledas från platsens faktiska transaction-owner innan något fysiskt försök. Inte ens dessa tester får användas för att ändra systemd eller slå på flaggan i produktion.

**Diffhygien:** Två befintligt smutsiga källfiler innehöll äldre, separat ägda ändringar. Endast hunken för den här S06-grinden är kandidat att stage/commit; ingen `hold_probe`-kod eller andra lokala ändringar får oavsiktligt följa med. Se [fencing-ADR](../architecture/adr-2026-11-07-modbus-fencing.md), [S07-felinjektionen](broker-fault-injection-2026-11-07.md) och [V01](physical-qualification-plan-2026-11-07.md).
