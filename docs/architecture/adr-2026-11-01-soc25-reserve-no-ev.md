# ADR 2026-11-01: keep Sunwoda >=25% with EV disconnected

**Status:** The 2026-11-01 six-hour trial **aborted at 26%** on
20:38 CET; original dispatcher restored. A watchdog scheduling-race
fix is implemented and tested in source only; there is **no active
trial** and physical effectiveness/permanent behavior remain unverified.

## Context

The previous supervised 1,500W EV-paused recovery stopped at 26% SoC
and the old dispatch then applied a 50W discharge command. Actual
battery discharge was ~172–197W and SoC immediately returned to 25%.
The old 20% economic reserve cannot enforce an inverter-level 25% floor.

At 18:56 CET the Volvo was unplugged. EVCC reported CP `A1` with
`measured_current_a=null` and no channel currents, not measured 0A.
The earlier pilot accepted only B2 paused with a real 0–1A reading.

## Decision

Keep the byte-identical installed dispatcher as sole inverter writer;
inject process-local SoC recovery thresholds **entry <26.5%,
exit >=28%**, preserving the old 12% software emergency floor and
native on-grid/BMS settings. Limit *grid-assisted* recovery to 1,500W
with its original independent L3 charge guard, but permit fresh
PV surplus above this (up to a separate 5,500W budget). Do not enable
firmware-unverified zero-power holds or the failed three-register freeze.

Use a separate root-owned, SHA-pinned 48-prefix one-off overlay with
a ~15s watchdog, conservative per-phase/site bounds, physical inverter
charge verification, strict 5min preflight and a hard **six-hour
deadline** enforced by an additional independently enabled one-shot timer. Do not activate the test by committing or checking code.
The watchdog may accept only fresh trustworthy A1 (unplugged, null
currents) or B2 (configured pause, measured <=1A). All other states,
including a reconnected charging C2 car, cause rollback without
writing to EVCC. Reboot also triggers rollback.

## First 26% crossing: abort and corrective decision (20:38 CET)

The 30-second poller measured 27% at 20:37:33 and first measured
26% at **20:38:03**. The independent watchdog ran at 20:38:10,
**before the next dispatcher iteration**, while the inverter was still
in `General/Auto`. The old `elapsed_since_trial_start >=120s`
check incorrectly demanded immediate EMS recovery at 26% and aborted
all six-hour-trial overrides. The separate plan-bypass companion then
restored all 49 optimizer rows. The regular dispatcher resumed its
pre-existing 21% recovery exit and 50W battery-save target.

**Amendment:** a newly observed 26% SoC with missing EMS mode is now
a monitored **`reserve_nonresponse`**, not an immediate abort.
Reuse the existing **180-second first-nonresponse timeout** to allow
poller/dispatch/Modbus synchronization. A later real charge clears it;
otherwise the original root restoration still runs after 180s. All
immediate EVCC/CT/BMS/phase/overcharge/SoC<23 hard stops are retained.

This is a **source/test change only** and does not authorize reuse of
the one-off aborted root state. Physical reactivation requires a new
identity, preflight and separate authorization. The 26→28% recharge
has **not** been verified on hardware.

## Tradeoffs

This is hysteretic grid/PV recharge, **not** a 0W physical hold and
not guaranteed cost optimal. Returning to price optimization at 28%
may discharge the battery to 26.5% again. The 1.5% margin is
intentionally above the requested 25%; it is not a newly configured
hardware floor. A1 is a disconnected CP observation, **not** a
measurement of zero current. All CT limits are software sampling
envelopes, not fuse protection. The real 26.5→28% transition and
solar handoff must be observed before approval for permanent operation.

See [runbook](../soc25-reserve-no-ev-2026-11-01.md).
