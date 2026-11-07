# T05 · Wi-Fi-recovery-watchdog: installerad drift och rollback

**Läsande inventering 2026-11-07.** Enheten är **aktiv** och arbetar i **FULL**, inte i enbart observationsläge. Inga systemd-, mod-, nät-, Modbus- eller rebootåtgärder har utförts i granskningen.

| Kontroll | Verifierat installerat värde / beteende |
| --- | --- |
| Timer | `wifi-recovery-watchdog.timer` **enabled, active, waiting**, `OnUnitActiveSec=1min`, `AccuracySec=10s`, `Persistent=false`. Observerad trigger 2026-11-07 cirka 11:35 CET |
| Service | `wifi-recovery-watchdog.service` **static, oneshot**, `User=root`, normalt `inactive/dead` mellan timerticks, `TimeoutStartSec=120s`, ingen drop-in |
| Exakt script | Installerad `wifi-recovery-watchdog.py`, SHA-256 prefix `16be045db011`. `MODE` koddefault `observe`; **effektivt** `WIFI_WATCHDOG_MODE=full` via privat configfil |
| Eskaleringsflaggor | `WIFI_WATCHDOG_DRIVER_RELOAD=1`, `WIFI_WATCHDOG_ALLOW_REBOOT=1`; service anger separat interface, ej återgivet här. Hela miljön/andra värden har inte publicerats |
| Nåbarhet och timeout | Gateway-kontroll 5 s, huvudloop även 10 s i felväg; reconnect subprocess-steg timeout 16 och 25 s; drivrutinsomladdning har delsteg med 15, 15, 15, 10 och 25 s timeout |
| Feltröskel | Först **3** på varandra följande misstänkta driver-fel med unreachable gateway ⇒ `reconnect`; **6** ⇒ `reload` om flagga; **10** ⇒ möjlig `reboot` om fault kvarstår och reconnect har försökts |
| Reboot-villkor | Kräver `mode=full`, explicit `ALLOW_REBOOT`, fortfarande felindikation, utfört reconnect, inga tidigare `reboot_attempted`, uptime **minst 20 minuter** och **24 timmars** cooldown från förra rebootmarkören |
| Frisk återhämtning | Gateway nårbar ⇒ nollställer fail-count, fault-state och försöksflaggor; enbart otillgänglig gateway utan separat driver-felindikation ⇒ `no-driver-evidence` i stället för återställningsåtgärd |

**Observationsbevis:** Läsande `journalctl` på service för 2026-11-07 gav 2 792 loggrader. Sökning på *förekomst av åtgärdsord* gav bl.a. 691 `healthy`, 4 `monitor`, 3 `reconnect` och 3 `reload`; dessa är **ordförekomster**, inte bevis för att tre åtgärder verkligen verkställdes. Inget rått journalinnehåll, privata adresser eller envfil har kopierats till Git.

## GO/NO-GO och rollback utan att tappa kontroll

- **Ingen systemd-/drivermodifikation i denna granskning.** Existerande driftsätt `full` är ett installerat faktum, inte godkännande för agenten att ändra det. Reboot under EMS BattCtrl kan lämna fysisk setpunkt kvar utan oberoende säkerhetsgaranti.
- **Read-only före varje tänkt ändring:** notera unitstatus, senaste trigger, effektivt mode, installerad script-hash, Wi-Fi-driver/WLAN-enhet, aktiv broker/TCP-ägare, processheartbeat och aktuell SoC. Kontrollera om ändringsfönstret kan lämna oövervakad EMS.
- **Om operatören separat godkänner att gå till observation:** bevara root-ägd privat kopia/hashes av unit, config och script (aldrig publikt), ändra **enbart** `WIFI_WATCHDOG_MODE` till `observe` i befintlig root-ägd config, kontrollera filsyntax, ladda om **endast berörd unit om det krävs** och verifiera att framtida tick loggar `would-reconnect`/`would-reload`, inte faktiska åtgärder. Detta är ett föreslaget förfarande, **inte** utfört här.
- **Rollback under särskilt GO:** återställ den exakta privata configen från signerad/hashverifierad backup och verifiera script/unit-hash, återgå till tidigare systemd-läge utan att stoppa broker eller dispatcher. Vid osäkert TCP-/SoC-tillstånd ⇒ NO-GO, eskalera operatör i stället för att forcera reboot.
- **Stoppsignal:** minskande SoC, nät-/fasfel, uteblivet gatewaykvitto efter recovery, parallell Modbus-förbindelse eller obevisad fysisk EMS-återgång. Reboot får inte vara generellt godkänd Modbus-failsafe.

**Status:** `source` + read-only installerad evidens; T05 dokumenterad. T03/extern safety-supervisor och S09 (EndSOC/BMS) kvarstår.