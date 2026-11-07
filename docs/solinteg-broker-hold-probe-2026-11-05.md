# Broker-owned soft-hold hardware probe (prepared, not deployed)

**Status 2026-11-06: OFFLINE recovery hardened; NOT INSTALLED / NO-GO for physical execution.** The existing
`scripts/tools/probe_hold_freeze.py --broker` now uses broker-owned complete
`transaction_hold` and `transaction_auto`, with all FC03 reads through its
allowlisted local Unix IPC. There is no second Modbus TCP client and no raw
FC06 access in broker transaction mode. The historical direct probe is unchanged.

## Why a separate probe identity?

The running broker authorizes control by systemd cgroup and kernel peer
credentials, not a client-provided role. The dedicated
`solinteg-hold-probe.service` is **only a template**, not installed/enabled.
The broker resolves it as `hold_probe` **only** when all hold:
- broker started with explicit `SOLINTEG_BROKER_HOLD_PROBE_ENABLED=1`;
- `solinteg-dispatch.service` is **running** with its actual process
  environment `SOLINTEG_CONTROL_ARMED=0`;
- request comes from that exact systemd unit under the broker UID.
- production broker is armed and complete transactions are enabled.

The limited role permits `status`, allowlisted `read_registers`,
`transaction_hold` and `transaction_auto` only. It **cannot** request
arbitrary FC06, charge/discharge, import-limit changes, full freeze or
other control actions. Broker `--broker` rejects the optional full-freeze
phase even when EPS unused is claimed. The source must be explicitly
`SOLINTEG_MODBUS_SOURCE=broker_transaction`; otherwise it refuses.

## 2026-11-06: supervised systemd stop recovery (source only)

The one-shot unit now uses `Requisite=` (never auto-start broker/dispatcher),
a private persistent `StateDirectory=solinteg-hold-probe`, and an
`ExecStopPost` recovery command. Immediately before the **first** broker
hold transaction, the probe creates the exclusive mode-0600 marker
`/var/lib/solinteg-hold-probe/active` and fsyncs it. An already-existing
marker blocks any new trial. On clean exit it is removed **only after**
verified General `0x101`, neutral `50207=0`, unrestricted `50208/50209`
and a successful physical telemetry read.

If Python is terminated without `finally` (including SIGKILL or systemd's
start timeout), `ExecStopPost` runs `--broker --recover-if-hold` under the
**same** systemd cgroup and restricted broker role. No marker means **no
Modbus request**. With a marker it first verifies the dispatcher is still
disarmed and the inverter is in General or EMS BattCtrl; it then requests
only the broker-owned `transaction_auto`, re-reads registers and physical
telemetry, and removes the marker only on success. An unverified restore
leaves the marker in place and the dispatcher must **not** be re-armed.

This closes the ordinary *probe process termination* gap but does **not**
guarantee recovery after Dell power loss, broker outage, a host-level
systemd failure, a lost Modbus connection, or a firmware-latched EMS
target. `ExecStopPost` is not an independent inverter watchdog. Vendor
confirmation and a separately supervised trial are still prerequisites
for claiming physical safety; no live activation is authorized by this
source change.

Offline verification on Nov 6: 71 focused Python tests passed, including
role restrictions, complete broker transactions, no direct TCP fallback,
simulated abrupt termination, fail-closed recovery and existing-marker
rejection. `systemd-analyze verify` passed on the unit template. This
is **not** a physical SIGKILL, systemd or inverter verification.

### Installation reconciliation and operator gates (not performed)

- On Nov 6 the running Dell measured SoC **16%**, PV **0 W**, EMS BattCtrl,
  and battery charging roughly 300 W: the required `SoC >20%` and
  General-mode/load preconditions were **not** met. No physical trial.
- The installed broker-control service, IPC module and hold-probe script
  do **not** contain the staged hold-probe role/CLI updates. Their hashes
  differ from the development worktree. The one-shot unit is not installed,
  and `SOLINTEG_BROKER_HOLD_PROBE_ENABLED` is unset. The installed
  dispatcher is site-specific and must never be wholesale replaced.
- Before any supervised window: review/sanitize an exact-hash selective
  overlay for broker core, control service, IPC and probe; offline-test the
  **actual installed baseline** and candidate together (including startup,
  authorization denials, killed worker/restart and restore); prepare a
  root-owned rollback/backup independent of old release artifacts.
- Prepare the one-shot service without enabling it on boot/timer. Only
  during the staffed window, opt the broker into
  `SOLINTEG_BROKER_HOLD_PROBE_ENABLED=1` and restart with verified socket
  ownership. Temporarily disarm the **running** dispatch process, retaining
  its existing priority/EV/site-specific changes. Do not toggle
  `DISPATCH_RESERVE_HOLD_VERIFIED`.
- Check stable broker/poller/watchdog, fresh BMS/phase telemetry and
  hardware-specific risk acceptance. Start from General with SoC strictly
  above 20%, PV at most 150 W and battery carrying at least 400 W of
  verified house load. Perform **only** the open-cap 0 W phase.
- After stop (successful or otherwise), verify the private result,
  `systemctl`/journal status, absence of an active recovery marker,
  General/target/caps, BMS alarms, physical power/phase measurements and
  the return of normal telemetry before rearming. A failed restoration
  requires manual recovery while dispatcher remains disarmed; do not
  bypass an unresolved marker. Restore broker config, remove only newly
  installed **inactive, owned** trial artifacts after the retention/archive
  procedure, and preserve rollback evidence.

## Test sequence (not yet authorized for production)

Existing probe prerequisites are retained: start in General, SoC strictly
>20% and <=95%, PV <=150 W, battery covering >=400 W of house load,
stable unmodified control registers, disarmed dispatcher still running,
and safe observed grid/phase measurements.

1. Verify broker/poller/dispatch/watchdog stable; record current firmware,
   SoC and physical load, original register values, and rollback.
2. Have an operator prepare the dedicated one-shot service and a separate
   broker configuration opt-in; DO NOT deploy these automatically.
   Do not change the live `DISPATCH_RESERVE_HOLD_VERIFIED` switch.
3. Perform only the default soft-hold phase at night. The broker executes
   the entire 0 W hold sequence and the return to General as two separate
   serialized transactions. Sample physical battery, grid, house, and
   phase powers between them; registered setpoints alone do not prove hold.
4. Verify final General (0x101), target 50207=0, nonrestrictive AC caps
   and resumed normal telemetry/dispatch **before** rearming.
   Evidence is written to the service user's private home directory.
5. Stop and rollback immediately on alarms, interrupted test, stale
   measurements, unexpected PV/house dynamics or failed verification.
   A successful 0 W test is not proof of solar handoff at changing weather.

**Critical unresolved issue:** a killed probe between transactions may leave
the inverter at EMS zero target until existing watchdog/operator restoration
takes effect. The installed watchdog/failure path has not been independently
qualified as an absolute fail-safe for this maintenance test. Therefore the
physical trial remains NO-GO pending controlled test supervision, explicit
stop/rollback procedure and firmware-specific confirmation. Existing native
OnGrid EndSOC at 10% is not a physical 20% guarantee under EMS.

## Sources and experience

- Manufacturer's *Hybrid Inverter MODBUS RTU Protocol* v00.02 (2022-12-06),
  Appendix 1 Battery Charge/Discharge Control: 50000/50207–50210 are EMS
  setpoints; it does **not** promise perfectly zero physical battery W.
  https://www.memodo.pl/mwdownloads/512e63211e205651872bde03f72478fe
- Solinteg EMS Mode: real-time control, communication stability requirement,
  recommended command interval 5–10s; excessive frequency accelerates wear.
  https://apidocs-en.solinteg-cloud.com/en/docs/solutions/ems-ems-mode/
- Solinteg inverter configurations: on-grid SOC protection and 10–90% cut-off.
  https://apidocs-en.solinteg-cloud.com/en/docs/appendix/inverter-configurations/
- `MODBUS.md` records on-device successful open-cap hold on a **different**
  reference inverter, and the AC cap ordering mismatch. Neither proves
  MHT-10K-25/installed firmware.
- `docs/architecture/adr-2026-11-02-ems-general-failsafe.md` describes
  latched EMS targets, independent floor uncertainty, PV blockage and
  watchdog limitations.
- `docs/solinteg-vendor-ems-questions-2026-11-03.md` records questions
  for Solinteg; still requires exact installed firmware and vendor reply.

## Remaining gates

- Offline test of kernel/cgroup probe role, including negative authorization,
  disarmed dispatcher and broker opt-in, and repeated interrupted-transaction
  recovery. The current unit tests cover the script's use of transactions.
- Verify deployed code against Git, establish safe maintenance/rollback
  ownership, BMS warnings, all three phases and watchdog behaviour.
- Get manufacturer confirmation for 0 W in EMS, PV blocking, native EndSOC
  and stale target on this exact firmware. Physically test under supervision
  only after the unresolved stop/rollback risk is accepted.

No systemd service, production env, Modbus register or real device modified
by this source preparation.
