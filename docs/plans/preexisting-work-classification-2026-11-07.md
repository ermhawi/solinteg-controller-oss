# Klassificering av tidigare lokala ändringar (B04)

**Läsande preflight 2026-11-07.** Ägare: befintligt Solinteg-arbete på permanent `Sundsvall`-branch; ändringarna fanns före denna genomförandeplan. De får inte kasseras, skrivas över eller publiceras ihop med orelaterade uppgifter. Driftsättning/avveckling ägs av operatören och kräver separat GO. Kontroll: Git-status/diff, SHA-256 av källfil och installerad fil via *read-only* åtkomst, systemd-status, [energi-ADR](../architecture/adr-2026-11-05-energy-priority-arbiter.md) och [hold-probe-NO-GO](../solinteg-broker-hold-probe-2026-11-05.md).

| Fil | Bevisad klassificering 2026-11-07 | Risk/ägarskap och bevarande |
| --- | --- | --- |
| `scripts/services/dispatch_loop.py` | **Delvis installerad/site-patchad**; repo `c5eccc5fc381`, installerad `274d93f6bdeb`. Aktiv dispatcher; befintlig energi-arbiter är aktiverad enligt tidigare driftbeslut, men exakta filskillnader kvarstår. | P0/P1; behåll arbetsdiff och installerad variant. Kräver semantisk R03-diff före portering. |
| `scripts/services/energy_priority_policy.py` | **Installerad, byteidentisk**; `4689830e4eac` i repo och app. | P1; bevara käll- och driftsversion, inte ändra styrning utan separat prov. |
| `scripts/tests/test_energy_priority_policy.py` | **Endast käll-/offlinetest**, saknas i app. | P1; bevara regressionstester med ovanstående policy. |
| `scripts/tools/replay_energy_priority.py` | **Endast offlineanalys**, saknas i app. | P1; bevara för D08, ingen inverter-I/O tillåten. |
| `scripts/services/modbus_broker.py` | **Pågående source-only hold-probe-behörighet**; repo `e8ef63259382`, app `824f44d92a38`. | P0; aldrig blanda till aktiv broker utan full transaktions-/rollgrind. |
| `scripts/services/modbus_broker_control_service.py` | **Pågående source-only hold-probe-identity/disarm-kontroll**; repo `13ad4e2a134b`, app `a815850882ac`. | P0; bevara, inte installera utan operatörsgodkännande. |
| `scripts/services/modbus_broker_ipc.py` | **Pågående source-only hold-probe-IPC-roll**; repo `08965c03070b`, app `c1bb77bdb731`. | P0; bevara, isolera från aktiv broker. |
| `scripts/tests/test_modbus_broker_control.py` | **Offlinetest för broker-/rolländringar**, saknas i app. | P0; bevara som verifikation; ingen fysisk styrning. |
| `scripts/tests/test_modbus_broker_transactions.py` | **Offlinetest för transaktioner/hold-probe**, saknas i app. | P0; bevara inför S04–S08. |
| `scripts/tests/test_probe_hold_freeze.py` | **Offlinetest, annan installerad äldre version**; repo `5f5ee792cc1b`, app `d1eeff4d108f`. | P0; bevara verifieringskedja och installerad rollback. |
| `scripts/tools/probe_hold_freeze.py` | **Source-only för broker recovery; äldre version i app**; repo `1457958deab7`, app `2f9c0e436165`. | P0; fysiskt hold-prov fortsatt **NO-GO**, bevara recovery-marker-kontrakt. |
| `deploy/solinteg-hold-probe.service` | **Endast mall i källa**, ingen `/opt`-kopia; `systemctl` säger `not-found/inactive`. | P0; installera/starta inte utan särskilt GO. |

**Därtill fyra befintliga plan-/dokumentationsändringar:** `docs/index.md`, `docs/known-issues.md`, `zensical.toml` (modifierade), och `docs/plans/codebase-quality-and-safety-2026-11-07.md` (ospårad). Dessa är samtidiga dokumentationsarbeten och ska versionshanteras separat från live-styrkod efter diff- och innehållsgranskning. De räknas inte till de tolv ovan.

**Git:** 16 icke-incheckade filer, 0 staged, 0 stash, 0 omergade grenar, 1 ordinarie worktree, `origin/Sundsvall` synkad vid `98be341`. `git_hygiene.py --mode preflight` = **BLOCKED** på grund av lokala ändringar (förväntat, ingen förlust av arbete). **Status:** `source` + läsande jämförelse; inte en ny release eller fysisk verifiering. **Åtgärd:** registrera och skydda ändringarna; granskas separat inför kommande commits.