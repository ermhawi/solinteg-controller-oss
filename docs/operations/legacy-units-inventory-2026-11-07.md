# A03 · Historiska SoC25-, temporary-, shadow- och broker-readonly-enheter

**Read-only source/runtime inventory 2026-11-07. Ingen kod, unit eller systemd-konfiguration ändrad.**

Målet är att förhindra att återställningsbevis, manuella verktyg eller tidigare migrationsenheter klassas som skräp enbart för att de inte är installerade. Källans `deploy/` har **26** relevanta `.service/.timer`-enheter. `systemctl show` gav **`not-found/inactive` för samtliga 26**. Det är ett installationsbevis för exakt de 26 namnen – inte bevis för att deras shell-/Python-entrypoints saknar operatörsnytta.

| Källgrupp | Enheter (samtliga har `solinteg-`-prefix) | Status/ägar- och bevarandeplan |
| --- | --- | --- |
| Tidig broker-readonly | `broker-readonly.service` | **historical + recovery-reserve; BEHÅLL** tills broker/TCP-migrationsbeslut och rollback verifierats. Aktiv broker är en annan unit. `poller-broker-readonly.conf` är en deploy-drop-in-mall, inte live-avinstallationsbevis |
| EV-shadow | `ev-shadow.service`, `ev-grid-solar-shadow.service`, `ev-grid-solar-shadow.timer` | **historical + shadow/manual; BEHÅLL** tills EVCC-observatör/priscontroller och deras befintliga profiler bevisats oberoende. `ev-shadow.env.example` är inte produktionshemlighet |
| SoC25 reserve | `soc25-reserve-watchdog.service/.timer`, `soc25-reserve-deadline.service/.timer` | **manual/recovery-reserve; FRYS**; planeras att återställas eller avvecklas endast mot verifierade tempfiler, deadline/rollback och senaste utförande |
| SoC25 plan bypass | `soc25-plan-bypass.service/.timer`, `soc25-plan-bypass-deadline.service/.timer` | **manual/temporary; FRYS**; planbypass kan ändra ekonomiska säkerhetsförutsättningar, inte automatiskt återskapas |
| SoC25 recovery retry | `soc25-recovery-retry-watchdog.service/.timer`, `soc25-recovery-retry-deadline.service/.timer` | **manual/recovery-reserve; FRYS**; före borttagning behöver P0-historiken kring SoC-recovery och sista rebootfallet granskas |
| SoC25 third recovery | `soc25-recovery-third-watchdog.service/.timer`, `soc25-recovery-third-deadline.service/.timer` | **manual/recovery-reserve; FRYS**; samma krav, annan retry-generation |
| Temporär SoC25 | `temp-soc25-watchdog.service/.timer`, `temp-soc25-adaptive-watchdog.service/.timer`, `temp-soc25-evpaused-watchdog.service/.timer` | **manual/temporary; FRYS**; kan vara lastbalans-, EV-paus- eller övergångsprober, ej generella livejobb |

**Verifierad inventering:** 1 broker-readonly, 3 EV-shadow, 16 SoC25-varianter, 6 temporära SoC25 = **26**. Inga av dessa 26 har en installerad enhet med samma namn; de kan fortfarande användas manuellt via shell, dokumentation eller för rollback. Utöver dem är `solinteg-broker-control.service` och `solinteg-ev-price-controller.timer` **aktiva** och får aldrig klassificeras som här avvecklade/historiska.

## Återställning, borttagning och dokumentationsgrind

1. **Ingen fysisk databas-, enhets- eller scriptstädning** genom A03. Alla listade filer kvarstår i Git, och installerade tjänster ändras inte.
2. För varje kandidat till A06 krävs faktisk anrops-/dynamik-/logg-/manual- och backupkontroll, inte enbart `not-found`. Skilj klart mellan historisk kod och återhämtningsreserv (A01–A02).
3. Om ett SoC25-/readonlyprov återupptas: separate explicit operatörs-GO, verifierad live-SoC, enda Modbus-ägare, beständig recovery marker och reversibel backup. Inga automatiska `systemctl enable/start` får ske för att verifiera dokumentationen.
4. Fryslistan för manuella prober och source-only `hold_probe` finns i [A04](probe-quarantine-2026-11-07.md). Säkerhetsprinciper och inget blind `Auto` i [fencing-ADR](../architecture/adr-2026-11-07-modbus-fencing.md).

**Status:** `source` plus read-only faktisk installerad unitstatus; inga runtimeändringar, ingen avveckling.