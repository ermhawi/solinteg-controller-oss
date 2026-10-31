# ADR 2026-10-31: Bounded 25% physical SoC recovery without live code changes

**Status:** Accepted and physically started with separate explicit user consent
on 2026-10-31. This is a **one-off**, not activation of automatic EV
grid-priority, PV-priority forced charging, reserve hold, or freeze.

## Context

The installed Sunwoda battery has a 20% economic planning reserve and
normal emergency hysteresis exiting at 21%. On 2026-10-31 11:21 CET,
the normal ARMED dispatcher legitimately exited its 300 W SoC recovery
at 21%, switched to General/Auto, and at 11:25 CET the inverter's
existing self-use logic discharged the house battery at approximately
1.7 kW while the Volvo continued charging at a verified 6 A.
The battery had returned to 20%. An uninterrupted, approved
25% SoC recharge was needed to obtain eligible shadow-only Auto/idle
samples. Separate physical EV-to-house battery priority remained OFF.

## Decision and rollback

1. Retain the installed checksum-bound ARMED dispatcher and its original
   root and ordinary watchdogs **unchanged**. The existing root EV
   priority gate stays `mode=shadow`; EVCC stays at verified 6 A.
2. Apply an exact, temporary **dispatcher-service-only** systemd
   `ExecStart` override via `/usr/bin/env`:
   `DISPATCH_SOC_RECOVERY_EXIT_PCT=25`,
   `DISPATCH_SOC_FLOOR_RECOVERY_W=1000`,
   `SOLINTEG_BATTERY_MAX_W=1000`. The third variable is essential:
   the existing dispatcher gives a larger planner charge precedence
   over the recovery minimum; the existing inverter-control clamp
   prevents physical forced setpoints exceeding 1000 W.
   The shared private `solinteg.env` is not modified.
3. Keep the site import cap and other safety policy intact. Independently
   stop the experiment if live inverter+EVCC data are stale/inconsistent,
   PV/CT balanced assumptions fail, EVCC pilot/current differs from 6 A,
   battery temperature or SoC is unsafe, physical charging has an
   excessive/absent response, site imports exceed conservative
   3.2 kW/phase or 6.5 kW total, or a service/root gate drifts.
4. Install independent root-owned, network-disabled
   `solinteg-temp-soc25-watchdog.service/.timer` (~15-second tick).
   Its only authorized control action is **restore the original
   dispatcher start**: remove its own checksum-verified drop-in,
   daemon-reload, and restart the ordinary dispatch service.
5. End at reported **25% SoC** or after no more than **two hours**,
   whichever comes first. The original dispatcher automatically resumes
   its previously configured 21% exit recovery/normal optimization.
   The observer and EVCC do not need to be restarted. On success/abort,
   the one-off timer disables itself.
6. Keep root-only operational status in
   `/var/lib/solinteg-recovery-25-pilot/status.json` (0600). The
   automated rollback is crash-retryable; unfamiliar modifications
   are not silently discarded.

## Observed rollout

- **2026-10-31 11:26 CET:** Installed only the process-scoped override
  and independent watchdog. All original dispatch and safety units
  remained active; root priority gate `shadow` and original checksum
  retained.
- **11:26:22 CET:** New dispatcher logged
  `charge 1000 W`, `SoC recovery 20% -> 25%`,
  initially `accepted_pending_physical_confirmation`.
- **11:26:39 CET:** Real inverter readings confirmed approximately
  **−1024 W battery power** (charging), while Volvo EVCC Ict2 remained
  **6.0 A** with pilot/configuration both 6 A. L3 grid import was
  approximately **1451 W**; the other phase imports were small and
  the independent supervisor remained `running`.
- **11:42:42 CET:** Grid L3 import increased from approximately
  **1.67 kW to 3.277 kW**, a roughly **1.6 kW additional load** on
  that phase. The particular appliance is unknown. The battery still
  charged around 933 W, with SoC reported at **21%**. The recorded
  import exceeded the pilot's conservative **3.2 kW/phase** threshold.
- **11:42:49–11:42:56 CET:** The independent watchdog correctly
  **aborted the trial**, removed its exact drop-in, reloaded systemd,
  restarted the normal dispatcher, and disabled its one-shot timer.
  Root status explicitly reports `state=aborted` and
  `conservative L1/L2/L3 import bound exceeded`. Original dispatch
  source/environment, EVCC 6 A and EV root `mode=shadow` remained
  unchanged; 25% was **not reached**. The later 13:26 time deadline
  therefore did not apply.

## Limitations

- Actual battery power may differ slightly from the 1000 W commanded
  setpoint due to inverter/BMS behavior. A >1250 W measured charging
  response aborts the pilot.
- CT watts and 230 V assumptions are **not** independent calibrated
  per-phase RMS fuse protection. Conservatively low stop thresholds
  and the unchanged site import limit reduce but do not eliminate
  electrical risk.
- The 25% target does not guarantee the normal planner will retain
  25% SoC. On restore it is free to discharge the house battery
  toward its original 20% economic reserve. Observe real `idle`
  only if it naturally occurs with sufficient fresh shadow data.
- This pilot **does not** validate a 0 W hold or a PV-preserving
  EV grid-priority mode and must never enable them implicitly.

See the [operator runbook](../temporary-soc25-pilot-2026-10-31.md).
