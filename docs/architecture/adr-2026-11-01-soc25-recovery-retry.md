# ADR 2026-11-01: New one-off 26→28% retry after watchdog transition race

**Status:** Source and offline tests implemented; physical activation and
result must be independently recorded.

## Why a new trial

The first 48-prefixed reserve test aborted at 20:38 CET at the first 26%
readback. It incorrectly required immediate EMS mode before the independently
scheduled dispatcher got its next turn; the 120s grace referred to trial
startup 90min earlier. Its aborted root state and 49 restored optimizer rows
must remain permanently available as evidence. Simply restarting the old
timer or overwriting the old status would destroy that safety boundary.

## Decision

Create the new `soc25-recovery-retry-20261101` trial, separate 49-prefixed
root-owned drop-in, root-only status directory, immutable wrapper copy and
new root watchdog/deadline systemd units. The original, SHA-pinned dispatcher
source remains the sole normal Modbus writer. Reuse reviewed process-local
26.5% entry and 28% exit recovery code, including its 1.5kW grid recovery
request, original L3 CT guard, and solar acceptance. Never set the native
BMS/SoC floor to 25%; do not modify EVCC or install the future Modbus broker
during this physical test.

The first 26% sample with General/Auto is observed as
`reserve_nonresponse` and given the existing 180s maximum from the first
failed observation. Invalid CT/EVCC/BMS/temperature and actual excessive
charging continue to trigger immediate rollback.

Completion requires actual charging measured in **two native telemetry
samples at least 20s apart**, followed by independent readback >=28% SoC.
Then automatically restore the original dispatcher and disable both retry
timers. A 28% reading without two real charge samples is not success. An
independent systemd deadline timer enforces a **90-minute** maximum.

Separate root-only supervisor lock prevents concurrent periodic/deadline
rollback from racing. The previous trial and the old optimizer rows are
never rewritten. Only a separate read-only preflight at 25–26% SoC and an
explicit start may activate the retry.

## Consequences

This is a bounded one-off hardware test, not permanent 25% reserve protection.
The native device firmware and live CTS are safety critical; 30s samples
cannot serve as instantaneous fuse protection. Temporary dispatcher restarts
may briefly switch the device to General/Auto. Follow-up design should
consolidate Modbus TCP ownership into a broker only **after** this trial is
terminal and rollback has been verified.

See [runbook](../soc25-recovery-retry-2026-11-01.md).
