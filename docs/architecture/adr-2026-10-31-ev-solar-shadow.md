# ADR 2026-10-31: Solar-aware EV/L3 shadow before physical control

**Decision:** Accepted for read-only analysis and shadow observation. **No
approval for any new EVCC or inverter writes.**

## Context

The earlier L3 guard caps only `action=discharge`. A normal `idle` dispatch
returns the inverter to Auto, which can discharge the house battery while
the car charges. At 2026-10-31 08:52–08:53 UTC, the private historian captured
three verified EV-charge samples with simultaneous house-battery discharge;
one measured 4,181 W. The latest preceding action was `idle`. A separate
short 1,848 W discharge sample occurred after the controlled dispatcher
restart while the 300 W SoC-recovery target was still pending.

Historical replay from 2026-10-28 15:30 UTC through 2026-10-31 09:40 UTC
covered 7,919 samples, of which 327 had fresh verified 6 A charging evidence;
102 overlapped battery discharge above 500 W, grouped into six episodes.
Most discharge episodes coincided with a deliberate discharge plan, not
`idle`. This correlation **does not prove** that a given watt was delivered
to the car.

## Decision

1. Analyse existing `ev-history.sqlite` and `telemetry.db` with SQLite
   `mode=ro`. Treat events as `idle_auto`, `planned_discharge`,
   `charge_pending_response`, or `unknown_applied_action`; never infer
   idle from an expired control record.
2. Keep the installed checksum-locked production EV guard, dispatch and
   15-second root watchdog **unchanged** (`gate.json mode=shadow`).
3. Run a separate `solinteg-ev-grid-solar-shadow` systemd oneshot every
   30 seconds, as `solinteg-evshadow`, with no network access, no hardware
   libraries, and only a private advisory JSON output.
4. Validate timestamp age, actual EVCC Ict2, PWM 6 A, 50006=0, all three
   signed CTs and AC/PV/battery consistency. Estimate non-EV L3 load
   as `-CT3 + inverter_ac_w/3 - 230*Ict2`. Estimate PV on L3
   conservatively as `min(PV, max(inverter_ac,0))/3` and compute an
   **unverified** battery-discharge cap after a 180 W non-EV L3 margin.
5. For `idle` with significant daylight, **do not suggest forcing 50 W
   discharge**: that could block normal battery charging from solar.
   Flag `solar_hold_unverified` instead. Respect existing SoC recovery
   and charge decisions. During low-PV `idle`, label 50 W battery-save
   only as a *historically tested shadow scenario*, not an active command.
6. Set `model_actionable=false`, `physical_control_authorized=false`
   and `device_writes=0` in outputs. The model is an advisory, **not a
   certified fuse/thermal/RMS protection system**.

## Extension: solar-aware idle arbitration, 2026-10-31

**Implemented as shadow-only code and independent observation; NOT an
enabled control feature.** `ev_grid_priority_idle_strategy.py` evaluates
only a *currently observed* `idle`/Auto dispatch decision, after the
existing shadow guard has verified a fresh live 50006=0, actual EVCC
Ict2≈6 A, 6 A pilot, and consistent L1/L2/L3, PV/battery/AC data.

It additionally requires **four contiguous, quality-checked** 30-second
historian samples spanning at least 70 seconds, with gaps no longer than
50 seconds and the newest no older than 65 seconds. All samples must
carry verified EV charging and three-phase CT, and the last dispatch
action must have been unchanged long enough to cover that window plus
30 seconds. Missing samples, CT data, unexpected SOC, stale commands or
communication errors **do not** count as zero consumption.

The read-only strategy separates:

- **`battery_save_candidate`**: PV is at least 300 W *below* estimated
  non-EV household load and the house battery discharges over 500 W
  for the entire evidence window. It records a hypothetical 50 W
  battery-save target, but only after checking predicted L1/L2/L3
  import ≤3,200 W per phase and total import ≤6,500 W, as diagnostic
  headroom; neither check is calibrated RMS fuse protection.
- **`solar_surplus_keep_auto`**: PV exceeds the *whole* house+EV load
  by at least 350 W consistently; prefer leaving Auto alone so the
  house battery can charge from surplus.
- **`pv_first_charge_unverified`**: PV exceeds estimated household
  load *excluding* the car by at least 350 W for a stable window,
  but does not cover house+EV together. This is where the desired
  house-battery-first preference would need an **on-device proof** that
  a PV-priority charge target actually funds the house battery with PV
  while the grid funds EV. It deliberately has **no suggested mode
  or writable charge setpoint**, because register 50209's PV-only
  interpretation has already failed economic tests.
- **`soc_recovery_priority`, `await_*_stability`,
  `solar_boundary_uncertain`, `projected_grid_import_high`,
  `solar_surplus_discharge_anomaly`, `dispatch_mode_mismatch`**:
  retain original hardware controls without any proposed switch.

All outputs retain
`model_actionable=false`, `physical_control_authorized=false`,
`device_writes=0`. The independent observer reads the existing
`ev-history.sqlite` in read-only mode and publishes a separate
`idle_strategy` section alongside its original `advice` section
*only during a trusted observed idle decision*. No code was added to
the ARMED dispatcher or to either root watchdog.

**Replay limitation:** The 2026-10-28 15:30 UTC–2026-10-31 11:00 UTC
window contains 7,974 samples, 378 with verified EV 6 A charging,
including three `idle`/Auto samples; all three `idle` samples have
house-battery SoC below the 25% threshold. Consequently **no real
above-25%-SoC idle-to-save transition has been validated yet**.
The 50 W mode was previously physically tested in isolation, not as
an automatic solar-aware transition. The new hysteresis is supported
by synthetic unit tests, not a new physical experiment.

**Dell rollout 2026-10-31 11:09 CET:** Only the independent
`solinteg-ev-grid-solar-shadow` observer was upgraded in its
root-owned `/usr/local/libexec/solinteg-ev-grid-solar-shadow/` directory.
The observer timer alone was temporarily stopped and restarted.
The idle strategy also requires current inverter mode readback
`work_mode_raw=0x101` (General/Auto) to agree with the logged idle
decision: stale idle logs cannot override a manual inverter mode.
Missing/mismatched mode returns `dispatch_mode_mismatch`, no proposal.
Installed observer SHA256
`55a166f41b82ca20887554771bd7e333dce7dde198de0ea84fd7fdadce3597aa`;
installed idle-policy SHA256
`e92792af8095cd4d97d2c5c67069a077afa7ad223edd073d43a3a436b496129e`
(final shadow-only revision at 11:11 CET). The earlier read-only strategy variant
`0d33e2480248237cb1a419a96290f72b19c6308414601655b3edc9d3bce46a0d`
has a 0400 root backup at
`/var/lib/solinteg-ev-grid-priority/solar-idle-model-before-anomaly-20261031.py`.
The intermediate `24ac9cb907b0c3d55d73d8426407fa2ef3cbba974089608f8f903191d9bb700e`
revision is also backed up as a root-only 0400 file
`/var/lib/solinteg-ev-grid-priority/solar-idle-model-before-auto-mode-check-20261031.py`.
This revision marks sustained battery discharge **despite whole-house
PV surplus** as `solar_surplus_discharge_anomaly` with no proposed
control, rather than incorrectly declaring that Auto is behaving normally.
Root-only 0400 observer rollback backup:
`/var/lib/solinteg-ev-grid-priority/solar-shadow-observer-before-idle-20261031.py`.
The old observer's SHA256
`8daef9e8e3a4b539fe2c1a424d84bb73b7c03550f3556ea6c1e701e0d854b59a`
was verified against Git HEAD prior to installation.
The ordinary ARMED dispatcher, EV guard, both root and legacy
watchdogs, EVCC 6 A limit and schema-2 root gate `mode=shadow`
were **not** changed. After installation the one-shot observer
reported `existing_charge`, `device_writes=0`, output 0600,
and all safety timers/services were active. During `charge`,
`idle_strategy` is correctly omitted rather than inventing idle evidence.
The systemd timer continues collecting read-only observations every
~30 seconds without a ChatGPT automation.

## Limitations and follow-up

- The historian stores a three-channel EVCC current sum, not Ict2, and
  lacks a historical record of 50006 at each sample. The replay **always
  labels these as assumptions** and cannot authorize physical operation.
- Balanced inverter AC allocation does not independently prove how PV
  and battery power were mixed on L3; 230 V and 95% conversion are
  approximations. Real-time CT and battery data can validate consistency,
  but not infer causal battery-to-EV energy flow.
- Original active EV policy still knows only the night-tested 6 A
  discharge case and **still does not protect Auto/idle**. No physical
  mode change was made by this ADR.
- At 2026-10-31 10:55 CET the ordinary poller lost the extended
  L1/L2/L3 CT block after a Modbus decode error and marked all three
  phase fields NULL. The observer correctly returned `unavailable`;
  no fallback to stale or invented phase readings is acceptable. The
  existing poller retries independently without changing physical
  control. The poller reported CT recovery at 10:57:29 CET, after one
  failed attempt. A read-only observer run at 10:57:56 CET then
  recovered to `existing_charge` with zero device writes. Verify
  sustained reliability before any active EV test.
- Before any physical trial: prove a solar-preserving alternative to
  Auto, retain SoC recovery, test nighttime and daytime transitions,
  validate phase/RMS current margins independently, and obtain separate
  explicit authorization with rollback.
