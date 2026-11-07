# T06 · Konsekvent SQLite + styrkritisk JSON: privat backup och återläsningsövning

**2026-11-07 — source/tested; ingen aktiv databaskopia eller live-restore utförd.** `scripts/tools/critical_state_backup.py` skapar en konsekvent SQLite-snapshot med källans URI **`mode=ro`** och `sqlite3.Connection.backup`. Endast uttryckligen valda styrkritiska JSON-posters råbyte (privata, utan loggutskrift) läggs i samma versionsmärkta, hashade paket. **Ingen läsning av datarader till logg**, ingen `/opt`-skrivning och ingen Modbus/EVCC-åtgärd.

## Minsta återställningskontrakt

1. Käll-DB öppnas via `mode=ro`, `query_only=ON`; SQLite tar en konsistent online-backup (inklusive data som låg i WAL vid snapshotögonblicket) till en **ny** privat katalog `0700` med filer `0600`. `PRAGMA quick_check`, `user_version`, `schema_version`, säkra tabellnamn och SHA-256 lagras. Inga privata sökvägar, JSON-värden eller session-ID finns i manifestet.
2. Endast allowlistade logiska tillstånd får bifogas: `live`, `heartbeat`, `evcc_readonly`, `evcc_cable`, `ev_price_pause_owner`, `ev_price_controller_status`. Symlänkar, ogiltig JSON och poster över 2 MiB blockerar snapshoten och tar bort ofullständig privat staging. Saknat **valfritt** tillstånd anges som `absent`, aldrig ersatt av ett påhittat bevis. Bakupfilerna kan innehålla privat data och får **aldrig** checkas in.
3. `verify(snapshot)` kontrollerar SQL-struktur/integritet och exakt SHA/byteantal för samtliga säkerhetskopierade JSON-poster. Fel => ingen återställningsövning.
4. `simulate_restore(snapshot, destination)` skapar en **separat scratchkatalog**, injicerar en ny syntetisk SQLite-tabell och korrupta JSON-poster, återställer sedan **bara scratch** med de verifierade originalbyten och kontrollerar samma schema/hash. Scratch tas bort; originalbackup och källsystem förblir orörda.

## Genomförd isolerad incidentreproduktion

`scripts/tests/test_critical_state_backup.py`: **6/6 PASS**. Ett syntetiskt SQLite/WAL med `control_actions.outcome='error_revert_failed'` och `readings.soc_pct=13` tillsammans med `dispatch-heartbeat.json` i felstatus och EV-pausägare `pending` säkerhetskopierades privat, integritetskontrollerades och återlästes efter avsiktlig **scratchkorruption**. Utfallet matchade den ursprungliga incidentbilden: osäker EMS/dispatcheråterställning och **ej** automatiskt återstartbar EVCC-paus. Testerna verifierade dessutom att DB/JSON-källor var byteidentiska före/efter och att paketet inte läcker en privat token i manifestet. Korruption, osäkra path/symlink, otillåten filkategori och frånvarande poster testades fail-closed.

### Operatörsexempel, inte driftsättningskommando

```bash
umask 077
python3 scripts/tools/critical_state_backup.py \
  --db /sokvag/till/redan-lasbar/testdatabas.sqlite \
  --json heartbeat=/sokvag/till/test-heartbeat.json \
  --json ev_price_pause_owner=/sokvag/till/test-ev-owner.json \
  --output-root ~/.local/state/solinteg-critical-snapshots

python3 scripts/tools/critical_state_backup.py \
  --simulate ~/.local/state/solinteg-critical-snapshots/incident-<id> \
  --output-root ~/.local/state/solinteg-critical-snapshots
```

**Begränsningar/GO:** Inga privata live-JSON-/SQLite-filer har lagts i ett nytt persistent produktionsbackup-paket under denna iteration. Ett syntetiskt restore-test bevisar **inte** att verkliga servicekonton, filägare, live-`/opt`, kryptering, retention, restartsekvens, systemd-unit/drop-ins och fysisk broker-/EVCC-ägarskap går att återställa. Detta är ett **testat backup-/simuleringsverktyg**, aldrig en automatisk release/rollback (R07 och V03–V06 kvarstår). Vid verkligt beredskapsprov krävs separat skyddad backupdestination, verifierad SQLite-WAL-snapshot, skyddade tillståndskopior och operatörens GO/NO-GO för eventuella live-manipulationer.
