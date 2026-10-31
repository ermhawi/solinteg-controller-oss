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
