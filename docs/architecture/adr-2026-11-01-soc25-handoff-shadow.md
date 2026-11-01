# ADR 2026-11-01: Shadow-only 25–26% recovery/Auto handoff

**Status:** Proposed, not approved for physical activation.

## Context

The existing dispatcher charges about 300 W below its normal recovery
exit at 21%. At the exit, an `idle` command may return the inverter
to Auto and discharge the Sunwoda battery immediately. The earlier
physical 25% trials were aborted; their independent watchdog assumed
EVCC charging at 6 A and SoC at least 19%, unlike the presently
EV-paused situation. Higher charging power can violate the trial's
phase/import envelopes, even though the main fuse still provides the
true overload protection.

## Decision

Develop a standalone, pure and **non-actuating** model to explore
replacing a premature Auto/discharge handoff with the already known
300 W charge setting while SoC is latched between 25% and 26%.
At 26% the hypothetical latch releases. Preserve an already planned
charge; with measured PV surplus, require separate solar-first
verification instead of suppressing it through a 50 W discharge hold.
With unverified EV pause, stale/inconsistent CT, projected import
overload or unverified phase balance, emit no candidate.

The history tool reads existing SQLite databases using `mode=ro`
and labels the missing historical `50006` as an assumption. A
suggestion never changes a dispatch tuple, publishes an optimizer plan
or writes inverter/EVCC registers.

## Consequences and alternatives

This only demonstrates decision behaviour and observed historical
gaps. It **does not** guarantee battery retention, PV-first charge,
safe phase current or that a physical 300 W charge behaves identically
when issued across 25% SoC. A 25–26% SoC cycle may remain.

Rejected pending separate evidence: simply raise the old 21% exit
to 25%; reuse the expired 1.5 kW trial without its original
conditions; activate unverified 0 W open-cap hold; force 50 W
discharge while PV could charge the battery.

Any physical trial needs a separate runbook, fresh measurements,
an independent watchdog, bounded time/import/current/SoC limits
and explicit operator approval.

See [shadow design](../soc25-handoff-shadow-2026-11-01.md).
