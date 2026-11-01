# Supervised 26→28% Sunwoda recovery retry — 2026-11-01

**Status:** Implemented for a new, explicitly authorized, root-owned
90-minute physical trial. **Never reuse the aborted 48-prefix test.**
Physical start/result must be recorded separately after readback;
source installation by itself does not prove successful charging.

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
