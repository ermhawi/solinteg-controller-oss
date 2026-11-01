# ADR 2026-11-01: temporärt utan batterisparläge för att prova 25 %-återhämtning

**Status:** Implemented as separately enabled, one-off plan-only test.
Physical outcome must be confirmed from inverter readings and root status.

## Context

A supervised six-hour SoC25 reserve trial was already active with Volvo
unplugged A1; its recovery starts below 26.5% and exits at 28%. On the
evening of 2026-11-01, the economic optimizer planned `idle` plus
`batterySaveDriftKwh` until 21:45. The installed ARMED dispatcher
interpreted that as an EMS 50W save target. At around 37% SoC it
would take too long to enter the desired recovery band before trial expiry.

## Decision

Temporarily remove **only** the current 15-minute slot's
`batterySaveDriftKwh` from the latest valid
`optimizer_runs.dispatch_json` to let existing dispatcher interpret
that `idle` slot as **Auto/self-use**. Do not manipulate Modbus directly,
restart/change the active root-pinned reserve process, alter the
charger, manipulate permanent optimizer settings, or export battery to grid.

Use a root-owned copy of an audited script and two independent systemd
timers: recurring 15s re-patching of new plan rows from the app and
one-time expiry **2026-11-01 21:45 CET**. Back up the exact original
JSON and patched SHA before each SQLite write; restore only if the
current row still matches the recorded patch. End early at
**SoC <=26.5%**, if the existing reserve watchdog is unhealthy, or if
EVCC no longer verifies unplugged A1 or CT/PV/BMS measurements are unsafe.

The active SoC25 reserve retains independent oversight and will
prioritize recovery when the physical SoC drops to 26% (integer
telemetry). This plan-only bypass never changes the reserve's
26.5→28% thresholds or six-hour root deadline.

## Consequences

Temporary plan forecasts become intentionally inconsistent with real
discharge in the patched slots. The price optimizer refreshes new runs
from actual SoC, and the original rows are restored on test end.
A successful SQL update alone is not evidence of physical mode or
battery flow; verify with Modbus-backed `live.json` and dispatch action
audit before reporting actual behavior. Software phase import limits are
not an instantaneous fuse guarantee. There is no persistent policy
change and no production-grade 25% zero-power hold.

See [runbook](../temporary-soc25-plan-bypass-2026-11-01.md).
