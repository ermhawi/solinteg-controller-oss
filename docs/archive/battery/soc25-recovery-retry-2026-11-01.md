> **Arkiverad 2026-11-07:** Historiskt experiment eller migrationssteg, inte en aktuell driftinstruktion. Kör inte gamla installations-, test- eller rollbackkommandon utan ny verifiering. [Nuvarande drift](../../current-state.md) · [Arkivöversikt](../index.md).

# Supervised 26→28% Sunwoda recovery retry — 2026-11-01

**Status: second physical trial aborted by safety supervisor at
20:57 CET (2026-11-01), before a charging sample.** The fresh
49-prefix trial was started after a successful SoC26 read-only
preflight, but an inconsistent inverter battery/AC snapshot was
observed during the dispatcher restart. The new 49-drop-in was
removed and ordinary dispatch restored; both retry timers are
inactive. The earlier aborted 48-prefix test remains untouched.
A bounded startup-snapshot correction is implemented and tested in
source only; **no third physical attempt has been activated**.

## Goal

The first 25% reserve test aborted at 20:38 CET when the independent
watchdog saw the first 26% poller sample just **before** dispatch could
enter 1,500W recovery. Its old immediate 26%/non-EMS abort preempted
the 30-second dispatcher tick. Both old timers stopped; the original
dispatcher returned to its 21% recovery exit and the temporary
no-save sidecar restored **49/49** optimizer rows.

This fresh retry uses new root paths, a unique identity
`soc25-recovery-retry-20261101`, a new `49-...` dispatch overlay
and separately named watchdog/deadline timers. It never overwrites
the first trial's `/var/lib/solinteg-soc25-reserve/status.json` or
its pinned root files. The current installer requires that prior
status be `aborted`, the earlier bypass be `stopped` with all
49 rows restored, and all four older trial timers be inactive.

## Physical behavior and constraints

- Starts only on fresh five-minute read-only preflight with house
  battery **25–26% SoC**, CT prediction <3,000W per phase and
  <6,300W grid import, EVCC independently reported unplugged `A1`
  (null currents; **not a 0A reading**), all ordinary safety units
  running, unchanged original dispatcher SHA, no conflicting
  root EV gate or 45–48 drop-in.
- Runs the original installed dispatcher source through its verified,
  root-owned process-local `soc25_reserve_dispatch` wrapper, recovery
  entry <26.5%, exit >=28%, minimum requested **1,500W** grid-assisted
  recovery, original independent L3 CT guard and solar capture.
  No other inverter/BMS minimum SoC, charger, private site env,
  original dispatch source, or open-cap 0W hold is changed.
- Watchdog observes sampled phase/site limits 3,200W/6,500W,
  5–45°C battery temperature, EVCC A1/B2 with verified pause,
  source/process hashes, native SoC >=23%, and actual battery current.
  These are software sampled limits, **not fuse protection**.
  At a newly observed 26%, dispatcher is given at most **180 seconds**
  from first nonresponse to enter EMS mode and start actual charging.
  Unsafe CT/temperature/charger/hard SoC remains immediate abort.
- A separate root-only timer checks about every 15 seconds. A
  **separate 90-minute systemd deadline timer** stops the trial even
  if the periodic watchdog is no longer running. The dispatcher
  itself still sends all Modbus writes; the root watchdogs send none.
- **Successful completion requires more than a mode setpoint:** at
  least **two distinct native poller samples separated >=20s**
  showing EMS `0x303` and battery actual charging at >=100W while
  SoC<28%, followed by measured **SoC >=28%**.
  Success and failure both remove only the SHA-pinned 49-drop-in,
  restore the original dispatcher with systemd restart, and disable
  both one-off timers. A spurious 28% without charging evidence is
  **aborted**, not reported as success.

## Commands

Read-only commissioning:

```bash
sudo -n python3 scripts/tools/start_soc25_recovery_retry.py --check
```

One approved physical start after the preflight, at most once:

```bash
sudo -n python3 scripts/tools/start_soc25_recovery_retry.py --start --confirm
```

Read-only status and explicitly requested early stop (root-installed copy):

```bash
sudo -n python3 /usr/local/libexec/solinteg-soc25-recovery-retry/soc25_recovery_retry_watchdog.py --status
sudo -n python3 /usr/local/libexec/solinteg-soc25-recovery-retry/soc25_recovery_retry_watchdog.py --stop
systemctl list-timers --all | grep 'solinteg-soc25-recovery-retry'
```

Watch the original physical sample in `/opt/solinteg/live.json`
(`soc_pct`, `battery_w`, `work_mode_raw`, `grid_w` and L1/L2/L3).
In this installation **negative `battery_w` means charging**.
Verify `/opt/solinteg/telemetry.db` `control_actions` actually
reports a `charge` operation, not just a planned slot.
Never claim the physical result from the wrapper's startup status
or the watchdog alone.

## Actual retry outcome — 2026-11-01 20:57 CET

Read-only evidence from the root trial state, dispatcher journal and
SQLite `readings`:

- **20:57:10 CET:** New identity `soc25-recovery-retry-20261101`
  passed the 25–26% read-only preflight with EVCC `A1`; its
  90-minute deadline timer was enabled. Physical start was explicitly
  approved; no root code from the first attempt was overwritten.
- **20:57:13–20:57:19:** Original dispatcher stopped, switched back
  toward General/Auto and restarted with the fresh process-local
  26.5→28% recovery thresholds.
- **20:57:18:** Poller measured SoC **26%**, battery **+1,050W
  discharge**, AC inverter output **156W**, house **1,036W**, grid
  **-880W import**, PV **0W**, phase CT **-542/-311/-26W** and
  General/Auto `0x101`. The independent site/phase/house balance
  remained correct (156 - (-880) - 1036 = 0W), but
  `abs(156 - 1050) = 894W` exceeded the original battery/AC
  coherency tolerance (~605W) during the shutdown/mode transition.
- **20:57:20–20:57:21:** Initial root watchdog rejected the
  inconsistent physical snapshot immediately, returned
  `aborted` (`CT/house/PV/battery energy balance invalid`),
  deleted its exact 49-prefix drop-in, restarted the original
  dispatch and disabled the independent retry deadline timer.
  The recurring timer was never enabled because startup failed.
  **The test made no verified recovery charge.**
- **20:57:48:** The next poller reading had returned to internally
  consistent values: SoC **26%**, battery **+226W**, AC **217W**,
  house **1,094W**, grid **-877W**, same disconnected EVCC state.
  Both the first (48) and second (49) trials remain terminal.
  Ordinary original dispatcher, poller and watchdog remained active.

**Root cause:** Like the earlier 26% transition race, the poller
performs sequential Modbus reads while the inverter is switching
modes; the battery register and AC output may describe different
instants. This is *not proof* that physical AC output was unsafe.
The original 49-root guard did the correct fail-closed thing with
its available input.

**Source-only amendment for any later separately authorized test:**
The base watchdog now distinguishes verified **site/phase/house CT**
inconsistency (always immediate abort) from a **battery-versus-AC**
coherency mismatch *only* in the first 120 seconds, with inverter
in General/Auto, SoC 24–28% and actual observed battery discharge
0–1,500W. That single case becomes supervised
`reserve_nonresponse` with the same **180-second fail-stop**.
EVCC, CT/site absolute import, temperature, overcharging, inverter
phase balance and other safety failures remain immediate stops.
The exact real sample and unsafe variants are regression-tested.

The installed second root code and status are **not modified** after
this failure. **Do not restart or reuse** either terminal identity,
or claim that charging to 28% was proven.

## Tests

```bash
python3 -m unittest scripts.tests.test_soc25_recovery_retry scripts.tests.test_soc25_reserve -v
python3 -m unittest discover -s scripts/tests -p 'test_*.py' -q
systemd-analyze verify deploy/solinteg-soc25-recovery-retry-*.service deploy/solinteg-soc25-recovery-retry-*.timer
```

The abandoned first trial remains permanently recorded in
[the 20:38 incident report](soc25-reserve-no-ev-2026-11-01.md).
This is a **one-off recovery verification**, not persistent production
protection and not part of the planned future single-writer Modbus
broker rollout.
