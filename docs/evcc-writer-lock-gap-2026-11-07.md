# E04 · EVCC FC10-ägarskap, dubbla lås och kvarvarande manual-race

**Read-only käll- och runtimeinventering 2026-11-07. E04 förblir öppen.** Inga FC03/FC10 har körts mot laddboxen.

**Identifierade möjliga skrivare:**

| Entrypoint | Exakt FC10-väg | Samordning och driftsstatus | Risk / krav |
| --- | --- | --- | --- |
| `ev_price_controller.py` | `EvccAscii.set_limit_raw(..., scheduled=True)` | `EV_PRICE_PAUSE_OWNER_LOCK` med nofollow/0600/ägarvalidering + `LOCK_EX|LOCK_NB` (**ägarlås**) före `EVCC_READER_LOCK` med `LOCK_EX|LOCK_NB` (**transportlås**). Persistent `pending/owned/releasing/revoked` och kabel-/revision-readback. `solinteg-ev-price-controller.timer` aktiv/väntande; oneshot inactive mellan tick | **Enda schemalagda prisstyrda skrivare** enligt kända entrypoints; 1 FC10 högst per beslut, läsback-verifiering innan `owned`. Fysisk kvalificering/ägarövergång återstår |
| `evcc_service.py` | Manuell `--pause` eller `--resume-amps` via `EvccAscii.set_limit_raw(..., manual_test=True)` | Kräver `EV_ENABLED=1`, `--confirm` och `--expected-raw`. **Tar inte `EVCC_READER_LOCK` eller ägarlåset.** Ingen aktiv schemalagd service med detta namn i verifierad EV-kedja | **KÄND RACE** om CLI körs medan priscontroller utför FC03/FC10. Besluta säker gemensam serialization/operatörsmode i E04 innan punkt får bockas av |
| `evcc_observer.py`, `ha_ev_session.py`, `ev_history.py` | Enbart read/observation enligt importer och source-manifest R05 | Observerar status; `evcc_observer.py` får inte ha raw-write API | Separat fysisk transport kan ändå kräva gemensamt `EVCC_READER_LOCK`; source-only-inspektion bevisar inte extern exklusivitet |
| Övriga manuella `scripts/tools/evcc_physical_qualification.py` och operatörsverktyg | Explicit fysisk kvalificering | **FRYS/NO-GO**, inte del av normal GitHub-testkedja | Ingen ny FC10, pause/resume eller kabelinkoppling utan särskilt GO |

**Verifikation:** `scripts/tests/test_ev_writer_lock_contract.py` (2/2) bevisar AST-ordning för de två låsen före priscontrollerns enda `set_limit_raw`, och reproducerar att manuella CLI:ns `set_limit_raw` saknar flock trots sina egna säkerhetsflaggor. `scripts/tests/test_ev_price_pause_owner.py` har sedan tidigare konkurrens-/ägar-/kabeltest med fake transport. EV-runtime source-paritet R05 = **12/12**, men `evcc_service.py` är ett separat manual-entrypoint utanför de 12 och måste kvalificeras separat.

**NO-GO för E04-kvittens:** komplettera ett gemensamt serialiseringskontrakt för manual-/scheduled-/observer-kod, gör riktade parallelltester och isolera extern operatörsåtkomst. Tester får inte kontakta laddboxen; status `tested/source`, **inte** fysisk verifiering. Se [hold-/hardware-frysning](operations/probe-quarantine-2026-11-07.md) och [EVCC-operativ paritet](operations/release-inventory.md).