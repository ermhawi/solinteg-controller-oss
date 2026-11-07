# S08 · Kernel-flock, processdöd, socketägare och isolerad Modbus-loopback

**2026-11-07 – status `tested/source`.** Detta är en körd fel- och återtagningskvalificering i isolerad miljö, **inte** en installerad supervisor och **inte** ett fysiskt GO. Ingenting i denna iteration har ändrat systemd, `/opt/solinteg`, EVCC, eller verklig Modbus-TCP:502.

## Faktiska prov och observerat resultat

| Provtyp | Verktyg och metod | Verifierad observation |
| --- | --- | --- |
| Verklig processdöd | `test_broker_process_fencing.py`: testet startar en *egen* child som håller ett 0440-kernellås i privat `TemporaryDirectory`, nekar andra innehavare, dödar **bara den egna processen** med SIGKILL och väntar på exit | Lock-inode är stabil, `flock` är upptaget under child-processen och återtagbart efter död. **Ingen** fysisk säkerhet följer av att låset släpper: `coordinated_emergency_auto` utan oberoende ny SoC-/EndSOC-källa ger `independent_evidence_source_missing`, `emergency_attempted=False` och noll skrivningar |
| Unix-socketägare, cgroup-/rollyta | Verklig lokal `BrokerIpcServer` på privat AF_UNIX, säkert role callback, konkurrerande socketstart och inode-ersättningssimulation | Endast en server kan äga ett visst pathname. Gammal broker får **inte** unlink:a en annan ägares ersatta socket; readrollen kan inte få en kontrollbegäran accepterad |
| FC03/FC06 wire, enbart 127.0.0.1 | `modbus_loopback_wire.py` implementerar en minimal test-MBAP/FC03/FC06-emulator med port **0 (OS-ephemeral, aldrig :502)**. Via verklig TCP-loopback och lokal UDS körs källans befintliga worker `transaction_charge`, `transaction_auto` och FC03 | En enda verklig *lokal* TCP-session, verifierad registerordning och syntetisk 0x303→0x101. Readrollen nekas styrning; ingen andra writer. Ingen `pymodbus`-installation behövdes |
| Supervisor-övertag och restartkonflikt | Tidigare `test_modbus_broker_supervisor.py`: 18 isolerade scenarier med samma riktiga tillfälliga `flock`, samt `test_modbus_broker_host_integration.py`: 25 scenarier med mockat `systemctl`, `ss`, root-approval och oberoende readback | Korrekt ordning revoke→bevisad markörfrånvaro→stop→lease→två observationer→färsk SoC-/OEM-grind→**högst ett** syntetiskt Auto. Restart under upptaget lås, extern socket, förlorad ack, okänd SoC/EndSOC, dålig serviceidentitet eller återställd approval => explicit NO-GO, inga blind retries |
| Lease-inode och behörigheter | `test_modbus_broker_lease.py`: 11 scenarier med riktiga kernellås, permissions, symlink-/inode-/owner-kontroll och cross-process contention | Ogiltigt 0440/UID/GID, saknad fil, osäker parent eller parallell innehavare ger deterministiskt avslag. Start kräver redan godkänd markör; en framtida supervisor får inte implicit återaktivera brokern |

**Riktad summa: 57/57 tester** (3 nya + 18 supervisor + 25 host integration + 11 lease); bred Pythonregression **1 758/1 758 PASS**. `scripts/tests/test_broker_process_fencing.py` och `scripts/tests/modbus_loopback_wire.py` är de enda nya testmodulerna. Hela testet är hermetiskt: AF_UNIX och ephemeral loopback, privat säker katalog, child-signalen går enbart till process som just startats av testet. Alla testservrar och processer stängs i `finally`/cleanup. De befintliga testerna med simulerat lyckat Auto är **syntetisk registerreadback**, inte fysiskt observerad säkerhet.

## Avgränsning och GO/NO-GO

- **S06/S07 kodstöd är source-only:** säkerhetsrollen och strikt timeout-latch är uttryckligen default OFF, och en processlokal latch **försvinner vid brokerrestart**. En kernellease som blivit fri är inte ett bevis att EMS har frigjort en kvarstående setpoint.
- **Ingen driftad supervisor-/rootfencing-release finns**, och systemd-service, faktisk processdöd/återstart, extern direkt TCP, nät-/Wi-Fi-fel samt Solintegs EMS/BMS- och EndSOC-beteende är fortfarande **NO-GO för produktion**.
- **En fysiskt installerad och självständig säkerhetsfunktion kräver** komplett privata backups, exakt reproducerbar release/paritet, verifierbar permanent approval revocation, socketnätverkets externa exklusivitet, tillverkarverifierad SoC-policy, namngiven bemannad rollback och separat användar-GO. Detta ligger i V02–V06, Z02 och S10. Vi har aldrig släppt denna kvalificering som godkännande för en riktig registerskrivning.
- Denna S08-checklistpunkt handlar uttryckligen om **syntetisk loopback/felprovning**, så dess kriterier kan vara avbockade med `tested/source` medan fysiska kvalificeringspunkter kvarstår öppna.

Se [fencing-ADR](../architecture/adr-2026-11-07-modbus-fencing.md), [S07-felkedja](broker-strict-fencing-2026-11-07.md) och [V01-fysiska stopvillkor](physical-qualification-plan-2026-11-07.md).
