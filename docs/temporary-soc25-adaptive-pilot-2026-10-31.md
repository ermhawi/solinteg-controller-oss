# Controlled 1.5 kW adaptive Sunwoda 25% SoC recovery — 2026-10-31

**Scope:** The owner separately authorized **one** physical trial up to
**1,500 W** of forced house-battery charging, reduced dynamically against
measured L3 load, with the Volvo EVCC **unchanged at 6 A**, reported
SoC goal **25%**, and automatic safety rollback. There is **no**
authorization for EVCC writes, new inverter mode register sequences,
changing root EV priority from `shadow`, increasing import safety limits,
or permanently changing normal recovery.

## Why a new trial is needed

The previous 1,000 W pilot proved a stable physical 0.9–1.1 kW battery
charge, but failed its **3,200 W/phase emergency stop** when a distinct
~1.6 kW L3 load started at 11:42:42 CET (L3 import peaked 3,277 W).
It correctly rolled back at 11:42:56 and reached just 21% SoC.
Simply raising the fixed target would recreate this condition.

## Runtime architecture

The installed, ARMED
`/opt/solinteg/app/scripts/services/dispatch_loop.py`, its existing
`inverter_control.py`, the private shared `solinteg.env`, the
EVCC settings and the independent EV grid-priority gate are **not
modified**.

A new root-owned *temporary* drop-in for
`solinteg-dispatch.service`, named
`46-temp-soc25-adaptive-pilot.conf`, instead starts a dedicated
launcher. The launcher imports the **same installed dispatch module**
and adjusts only its process-local L3 charge fallback and the
charge-target reduction deadband. All physical Modbus writes still
flow through the **original dispatcher**; the launcher contains no
independent Modbus client.

The process-scoped environment, verified by the launcher and
independent root supervisor, is:

```text
SOLINTEG_TEMP_SOC25_ADAPTIVE=1
DISPATCH_SOC_RECOVERY_EXIT_PCT=25
DISPATCH_SOC_FLOOR_RECOVERY_W=1500
SOLINTEG_BATTERY_MAX_W=1500
DISPATCH_L3_CHARGE_GUARD_MODE=1
DISPATCH_L3_CHARGE_BUDGET_W=3000
DISPATCH_LOOP_INTERVAL_S=30
```

The normal controller first requests at most **1,500 W** while SoC is
below 25%. At each 30-second dispatch tick, the existing active
`guard_l3_charge()` must verify a fresh, internally consistent
L1/L2/L3 CT measurement and `50006=0`. Its existing calculation
estimates L3 *house* demand as
`-meter_l3_w + inverter_ac_w/3`, allowing 1,500 W only when
the equal-third projected charge load fits under a **3,000 W L3
planning budget**. The final charge target may be **300–1,500 W**
in 100 W steps; if phase data are untrusted, it falls back to
**300 W, never Auto**. Downward charge changes of even less than
250 W bypass the old economic deadband.

The safety *stop* is separate from this planning budget. The
independent `solinteg-temp-soc25-adaptive-watchdog.timer` runs every
~15 seconds, using fresh existing 30-second CT/EVCC observations.
It preserves the same **3,200 W import on any phase** and
**6,500 W total import** emergency STOP thresholds as the first
trial, and verifies SoC ≥19% and battery temperature 5–45°C.
It additionally verifies the exact temporary process environment,
launcher cmdline/hash, original dispatch/root-gate hashes, EVCC
6 A charge/pilot, actual inverter EMS mode, real battery charging
response (up to 1,850 W measured) and valid CT consistency.
These meter-based checks are **not certified instantaneous RMS fuse
protection**; a fast L3 appliance spike can still stop the test.

Either a reported **25% SoC**, a safety stop, or a **hard two-hour
deadline** triggers the supervisor to remove only its checksum-verified
`46-` drop-in, `systemctl daemon-reload`, restart the *original*
dispatcher, and disable only the one-shot pilot timer. The prior
aborted `45-` pilot and its root status are left untouched.

## Operations

Root-only status:

```bash
sudo python3 /usr/local/libexec/solinteg-recovery-25-pilot/temporary_soc25_adaptive_watchdog.py --status
systemctl is-active solinteg-temp-soc25-adaptive-watchdog.timer
```

Early abort restoring the original dispatch unit:

```bash
sudo python3 /usr/local/libexec/solinteg-recovery-25-pilot/temporary_soc25_adaptive_watchdog.py --stop
```

Root state is separately stored mode 0600 in
`/var/lib/solinteg-recovery-25-adaptive-pilot/status.json`.
No private IPs, credentials or utility invoices may be published.

## Initial deployment and verified physical response

- **12:03 CET:** First activation was immediately rolled back when an
  installer readiness check sampled the dispatcher during a systemd
  process transition, before confirming the new wrapper's environment.
  The first attempt's status was preserved in a root-only
  `status-attempt1-aborted-20261031.json`. The ordinary dispatcher
  and the old independent watchdogs were verified active afterward.
  No completed 1,500 W charge was claimed for that attempt.
- **12:04 CET:** Second activation added bounded polling for the
  `MainPID`, exact process environment and wrapper command line.
  The independent root timer was enabled before restarting the
  unchanged ARMED dispatcher. The root gate stayed `mode=shadow`
  and the original private `solinteg.env` remained byte-identical.
- **12:04:45 CET:** The normal dispatcher recorded
  `charge 1500 W / applied`, with active L3 guard:
  `L3 estimated load 1555 W, budget 3000 W,
  battery request 1500 -> 1500 W`.
  **12:04:47 CET:** The inverter's real `battery_w=-1523`
  confirmed physical charging, not just Modbus acceptance; EVCC
  Ict2 was **5.9 A** and its 6 A pilot remained raw 100.
  Measured L3 import **1,437 W**; L1/L2 imports **475/110 W**.
- **12:04:58 CET:** Independent watchdog again reported
  `state=running`, `adaptive_wrapper_verified=true`,
  physically measured charge **1,523 W** and valid L1/L2/L3;
  the original root and dispatch watchdogs were still active.
  SoC was still **20%**. This is a verified **ongoing trial**,
  not evidence of reaching 25%.
- **Hard deadline:** **14:04 CET on 2026-10-31**, unless 25% SoC
  or an earlier safety condition ends the trial. Read the live
  root status rather than assuming it completed after a time estimate.
