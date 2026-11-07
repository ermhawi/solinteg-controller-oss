# R06 / R08 · Reproducerbar, icke-driftsättande release och rollback

**Status 2026-11-07:** `tested` för privat staging, källbackup och scratch-rollback. **Ingen** produktionsinstallation, DB-write, systemd-ändring, Modbus eller EVCC. Release-flaggan är alltid `false`; detta är **inte** GO/NO-GO-godkänd driftsättning.

## Exakt stagingmanifests och beroendebevis (R06)

`scripts/tools/stage_release_candidate.py` läser **endast `git archive HEAD`**, inte arbetskatalogens tidigare lokala ändringar. Det skapar unika privata kataloger, 0700 för kataloger och 0600 för icke-körbara filer, aldrig under `/opt`. Manifestet innehåller Git-SHA, alla källfiler och SHA-256, stabilt `content_sha256`, beroendehärkomst, schema-tabellnamn, samtliga deploy-unitmallars hash, exakt modulmanifest och valfri read-only jämförelse mot installerade unitfiler. Operativ verifieringsmatris finns här:

| Bevisnivå | Lokal kontroll | Status 2026-11-07 |
| --- | --- | --- |
| Ren källa | Git-archive, filhashar och källa-only EV-importvalidering | **602 committade filer**, manifest **PASS**, privata `candidate-9f02a2ee9d7b-*` |
| Dependency/repro | `package.json`, `package-lock.json`, `scripts/requirements.txt`, `requirements-docs.txt`, `deploy/schema.sql` | **5/5 finns och är hashade**. **Begränsning:** `pymodbus>=3.13,<4` är en versionsrange, inte ett låst wheel; Node-locken innehåller exakta npm-versioner, men inga binära byggartefakter ingår |
| SQLite/schema | `deploy/schema.sql`, read-only introspektion via SQLite på riktig databas med `mode=ro` | **8 tabeller** i källschema, **8** i live SQLite; kolumnnamn, deklarerade typer, nullbarhet och PK har **0 avvikelser**; `PRAGMA user_version=0`, SQLite `schema_version=15`. Detta är **schemakompatibilitet**, inte en bekräftad databasbackup |
| Systemd | `--inspect-systemd` använder enbart `systemctl show` (LoadState, ActiveState, FragmentPath) och jämför SHA för unitmallen | **11 units**, 7 `equal`, 3 `different` (EV HA, EV observer, EV price controller), 1 `unverified` (Wi-Fi-timer). Drop-ins och effektiva flaggor måste fortfarande granskas separat via R01/R02 |
| Offline-regression | `scripts/tests/test_release_candidate_stage.py` och `scripts/tests/test_rollback_snapshot.py` | **5+4/9** godkända; test av källa kontra dirty worktree, path traversal, symlink, privata filrättigheter, source-only-guard, korruption och tre restorelägen |
| Drift före/efter | `scripts/tools/release_inventory.py --compare-json` | **R07 NO-GO** vid tidigare observerade 7 käll-/driftdivergenser och skyddad smutsig arbetskatalog. Ingen live-release har jämförts efteråt |

**Körning i operatörens privata miljö:**

```bash
umask 077
python3 scripts/tools/stage_release_candidate.py --inspect-systemd
python3 scripts/tools/rollback_snapshot.py --sudo-read
python3 scripts/tools/rollback_snapshot.py --simulate \
  ~/.local/state/solinteg-release-staging/rollback-srp1ragx
```

Det sista kommandoexemplet hänvisar till **existerande läsande snapshot på Dell 2026-11-07**, inte till allmänt återanvändbart snapshot-ID. `--sudo-read` kör inte kontrollern med root; för otillgängliga, *hårdkodade och tillåtna modulfilvägar* gör den enbart `sudo -n /usr/bin/cat`. Inga privata env-filer, lösenord, telemetrirader, faktiska inverter-register eller EVCC-operationer är del av snapshoten. Verifiera filägare och bevara backupen separat på krypterad plats innan verklig release. Loggar/exakta paths/hashes hålls i privata 0700/0600-kataloger och läggs inte i Git.

## Selektiv rollback och gränser (R08)

`scripts/tools/rollback_snapshot.py` kopierade **24 whitelisted installerade källmoduler** read-only till privat backup: 8 inverter, 4 optimizer/webb och 12 EVCC. Varje fil verifieras med SHA-256 och storlek före simulation; korrupt backup ger fel och **ingen** restore. Testade steg i `--simulate`:

| Scenarie | Testhandling enbart i privat scratch | Resultat |
| --- | --- | --- |
| **before** – ingen ny fil har lagts på | Samtliga 24 källfiler kopieras/självverifieras | 24/24 verified |
| **partial** – en del filer simulerar ny kod | Varannan fil får ett syntetiskt ändrat innehåll; återställ **enbart** ändrade filer från backup | 24/24 verified |
| **after** – alla filer bytta | Samtliga scratch-filer byts syntetiskt; återställs selektivt | 24/24 verified |

`production_writes=0` och `physical_operations=0` i alla lägen. Alla scratch-filer städades efter verifiering, **snapshoten och originalinstallationen är oförändrade**.

**Kvarstående operativt STOPP:**

1. Source-rollback täcker **inte** DB-innehåll, WAL, beroendewheels, Next-`.next`-bundle, systemd-unit/drop-ins, env eller binärfiler. SQLite-strukturen matchar idag men en framtida migrering måste ha separat bakåtkompatibilitets-/PITR-bevis; före faktisk release krävs krypterad, konsekvent DB-backup med `sqlite3.backup` från `mode=ro`-anslutning eller motsvarande.
2. **Under partiellt byte** ska dispatcher och broker behandlas som `unknown`, ingen blind `return_to_auto`. Kräv explicit operator-GO, verifierad exklusiv broker-TCP-ägare och stabilt SoC innan något återställningskommando. Bevara varje privat installerad fil + SHA, identifiera och stoppa exakt ägd process enligt godkänd GO/NO-GO-plan, byt endast verifierade filer atomärt och kontrollera hash/readback; ingen automatisk `systemctl` eller fysisk skrivning i dessa verktyg.
3. **Efter rollback:** kontrollera manifestets Git-SHA och alla installerade filhashar, aktiva systemd drop-ins/effektiva featureflaggor, broker-ägarskap, separat watchdoglarm, färsk heartbeat med besluts-/applystatus, SQLite schema och oberörd EV pause-ownership. Om fysisk state eller broker/SoC fortfarande okänd är fortsatt drift **NO-GO**.
4. Vid avbrutet återställningsförsök: inga parallella skrivare eller blind retry av FC06/FC10. Håll aktuella privata snapshots oförändrade, larma operatör och använd [fencing-ADR](../architecture/adr-2026-11-07-modbus-fencing.md).

**Acceptans:** R06/R08 är genomförda som **säker source-only preparation och scratchkvalificering** med verifierad schema- och unitparitet. De **ger inte tillstånd att driftsätta** eller påstå att återställning fungerar fysiskt; detta kräver särskilt godkännande V01–V06 samt åtgärdade blockerare i S06–S09 och R07.
