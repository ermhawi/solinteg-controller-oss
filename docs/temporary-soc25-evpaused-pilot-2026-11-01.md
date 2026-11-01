# Supervised EV-paused house-battery recovery — 2026-11-01

**Status: physically activated at 10:29 CET on 2026-11-01.** The independent
watchdog was verified `running` at 10:31, with real house battery charging.
This is an **ongoing one-off trial**, not evidence of reaching 25% or 26%.
The independent hard stop is 12:29 CET; all safety stops remain active.

## Why another bounded trial

The DEFA/ABL EVCC pause was physically verified on 2026-11-01
(`B2`, configured PWM `1000`, roughly 0.3 A idle indication).
The Sunwoda house battery was below 19% SoC and its normal 300 W recovery
stops near 21%, after which Auto can discharge it back to 18%.
The old 1.5 kW trial cannot be reused because it requires 6 A EVCC
**charging**, SoC at least 19%, and the now-revoked root EV priority gate.
Its earlier 3.2 kW/phase safety stop must not be relaxed.

## Scope and bounded commands

- Start only if **current SoC 19.0–24.9%**, and **all five minutes** of
  uninterrupted historical observations remain at least 19%.
  Initial phase CT, balanced inverter register 50006, meter totals,
  temperature (5–45°C), current battery charging and EVCC **paused** state
  must be independently verified and fresh.
- Admission models an immediate worst-case **1,500 W** battery charge and
  refuses if predicted phase import reaches 3,000 W or site import 6,300 W.
  Recent five-minute import must also stay below those admission thresholds.
  These are **conservative planning margins**, not certified fuse limits.
- Preserve the installed **single ARMED dispatcher**, its private
  `solinteg.env` and original source checksum. One root-owned
  `47-temp-soc25-evpaused-pilot.conf` drop-in starts the isolated launcher.
  The launcher requests up to **1,500 W**, retaining the existing 300 W
  fallback at high/invalid L3 load, with a 2,700 W L3 *planning* budget.
  It never independently opens Modbus or changes EVCC.
- Process-local dispatcher `DISPATCH_SOC_RECOVERY_EXIT_PCT=27` ensures
  the normal dispatcher does **not** enter Auto during the approximately
  15-second interval before the independent watchdog observes **26%** and
  restores the original service. The extra 1% is only a temporary buffer
  above the requested **25%**, not a permanent retention policy.
- An independent root watchdog checks roughly every 15 s and aborts on
  SoC below 19%, lost EV pause, CT imbalance/staleness, uncontrolled
  discharge, battery temperature, wrong work mode, file/process identity
  drift, missing upstream watchdogs, or an actual import over
  **3,200 W on any phase / 6,500 W site total**. It also aborts after
  a **hard two-hour deadline**, even if 25% has not been reached.
  The requested charge is limited to 1,500 W. If the **ordinary**
  optimizer was already charging at up to 3,000 W before switching,
  a strictly timed 120-second startup window permits that *pre-existing*
  measurement while the new process reduces the setpoint. After that,
  measured charging above 1,850 W aborts; even during startup, charging
  above 3,000 W aborts. This window never relaxes grid/phase STOP limits.
  The temporary dispatcher drop-in also sets `Restart=no` and `RuntimeMaxSec=2h` as an
  independent process-lifetime fallback if the watchdog timer itself fails.
  A change in host boot ID forces rollback rather than resuming a one-off
  experiment after a reboot.
- The stop path deletes **only** its checksum-verified 47-prefixed
  drop-in, reloads systemd, restarts the byte-identical original
  dispatcher, records the result and disables only its timer. A
  checksum mismatch blocks deletion and requires human intervention.
  No prior 45-/46-pilot, EVCC or root-gate state is reset.

## Operator steps

A fresh read-only admission check. **Exit code 2 means blocked.**

```bash
sudo -n python3 scripts/tools/start_soc25_evpaused_pilot.py --check
```

Only after all conditions pass, the pre-approved supervised physical trial
can be initiated with the exact one-off installer:

```bash
sudo -n python3 scripts/tools/start_soc25_evpaused_pilot.py --start --confirm
```

The installer repeats the full admission check before installing files,
then **again** before creating the owned drop-in and systemd reload.
It creates root-owned, checksum-pinned state and copies only its new
service, timer and wrapper. The timer is activated **before** the ARMED
dispatcher restart; on exceptions, the installer invokes the watchdog's
owned rollback. An accepted initial tick is **not proof** of physical
charging. Do not leave the operator-controlled session until fresh
`live.json` proves negative `battery_w` and the EVCC still reports
`B2` / at most 1 A.

Monitor and terminate early:

```bash
sudo -n python3 /usr/local/libexec/solinteg-recovery-25-pilot/temporary_soc25_evpaused_watchdog.py --status
sudo -n python3 /usr/local/libexec/solinteg-recovery-25-pilot/temporary_soc25_evpaused_watchdog.py --stop
systemctl is-active solinteg-dispatch.service
systemctl is-active solinteg-temp-soc25-evpaused-watchdog.timer
```

Root state is kept mode 0600 at
`/var/lib/solinteg-recovery-25-evpaused/status.json`.
No automatic future activation is scheduled when preflight is blocked.
Do not rerun a completed one-off test without a fresh review and
separate scoped approval.

## Evidence and limitations

- **2026-11-01 10:18 CET:** initial preflight failed at 18% SoC,
  while the normal dispatcher continued to charge at around 300 W.
- **2026-11-01 10:22 CET:** SoC first reported 19%, but the previous
  five minutes still contained 18%, correctly denying activation.
  Phase and site import admission projections were within limits at that
  instant. Later measurements must be checked again before any start.
- Reusing the existing 15–30 s monitoring means instantaneous overload
  cannot be ruled out; installed electrical protections remain essential.
- The real 0 W hold is still unverified; forcing 50 W discharge can
  interfere with PV charging. Once the trial restores the old
  dispatcher, Auto **may discharge** the battery again. Reaching 26%
  is **not** equivalent to a verified lasting 25% reserve.
- The Volvo remains paused throughout this one-off trial. Resuming
  6 A charging needs its own verification and approval.

## Verified live deployment — 2026-11-01

- **10:29:36 CET:** The new preflight succeeded at 20% SoC with the
  independently paused Volvo (`B2`, raw `1000`, 0.3 A), five minutes
  of safe SoC/CT history, fresh energy measurements and measured
  imported power below the admission forecasts. The separately approved
  one-off installer returned `started_supervised`. Root watchdog timer
  was activated before the pilot process restart.
- **10:29:38 CET — startup caveat:** Applying `RuntimeMaxSec=2h` in the
  new systemd drop-in and invoking `daemon-reload` unexpectedly made
  systemd apply the maximum runtime retroactively to the **older**
  dispatcher, which had already run for more than two hours. It exited
  via its ordinary safe shutdown, briefly selecting inverter General/Auto
  before the installer restarted the new pilot process at **10:29:45**.
  Do not reuse this exact installation sequence for a later trial
  without fixing and verifying that transition; the 120-second watchdog
  settle guard covered this one-off change.
- **10:29:53 CET:** The new process requested **1,500 W**; the initial
  control action was `accepted_pending_physical_confirmation`,
  not claimed as physical proof.
- **10:30:23 CET:** Physical readback verified **−1,494 W**
  `battery_w`, actual EMS BattCtrl (`0x303`), SoC **20%**,
  grid import **1,912 W** with L1/L2/L3 imports
  **874/533/504 W**. The independent watchdog recorded
  `state=running`, the same physical battery charge and verified EVCC
  `B2` 0.3 A. At **10:30:33** the ordinary dispatcher logged
  `charge 1500W / applied`.
- **10:31:15 CET:** Root watchdog still `running`;
  measured `battery_w=-1502` W, SoC **20%**, phase imports
  **872/526/573 W**, Volvo still paused.
- **10:31:47 CET (after 120-second settling period):** Independent timer
  still reported `running`, real charging was **1,519 W** within the
  1,850 W safety tolerance, and actual EMS BattCtrl mode was verified.
  Volvo remained `B2` at 0.3 A. These readings verify ongoing
  supervision and physical charge; the test has not reached its SoC target.
- **Hard stop:** **12:29:36 CET**, or earlier on 26% SoC or any watchdog
  abort. Independently verify the state before asserting a completed
  trial. The ordinary dispatcher may resume Auto and draw the house
  battery down after this trial ends.

## Verification

```bash
python3 -m unittest scripts.tests.test_temporary_soc25_evpaused_pilot -v
python3 -m unittest discover -s scripts/tests -p "test_*.py"
systemd-analyze verify deploy/solinteg-temp-soc25-evpaused-watchdog.service deploy/solinteg-temp-soc25-evpaused-watchdog.timer
```

Related: [non-actuating 25–26% shadow](soc25-handoff-shadow-2026-11-01.md),
[prior aborted adaptive pilot](temporary-soc25-adaptive-pilot-2026-10-31.md),
[ADR](architecture/adr-2026-11-01-soc25-evpaused-pilot.md).
