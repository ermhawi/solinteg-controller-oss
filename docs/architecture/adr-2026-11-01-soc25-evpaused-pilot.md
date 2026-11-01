# ADR 2026-11-01: EV-paused supervised 25% recovery pilot

**Status:** One physical trial authorized; admission safety gates
are mandatory. No permanent actuator or battery hold is approved.

## Context

The previous 1,500 W pilot assumed live 6 A EVCC current and the
root EV priority `shadow` gate, neither of which applies after the
successful 2026-11-01 pause and independent rollback. The normal
300 W SoC recovery exits at 21% into an Auto path that has been
observed discharging from 20 to 18% within ten minutes. A blanket
new 25% recovery exit alone cannot guarantee retention.

## Decision

Develop a **new isolated one-off** physical test that starts only with
SoC at least 19% for a continuous five-minute evidence window and a
verified paused EVCC. Use the existing installed ARMED dispatcher as
the only physical inverter writer, a process-only 1.5 kW clamp,
its tested L3 charge guard with 2.7 kW planning budget, and 300 W
untrusted-data fallback. Never change the shared env, old root gate,
EVCC settings or original dispatcher code.

An independent root-owned timer and watchdog validate device
readbacks, phase/site import and battery SoC/temperature every ~15 s;
the former stop limits remain **3,200 W/phase / 6,500 W site**.
The stricter admission forecast must fit within **3,000 W/phase
/ 6,300 W site**. Abort and checksum-restore at any violation or
two-hour expiry, complete and restore at **26% SoC**, before the
process-scoped **27%** recovery exit could return to Auto.

## Consequences

No auto-start is allowed when preflight is blocked. The trial might
reach neither 25 nor 26% if limited to the 300 W fallback or if it
aborts on a transient; its rollback is more important than throughput.
Neither the historical replay nor these guard thresholds constitute
a certified fuse protection, phase metering accuracy guarantee, or
proof that a permanent 25% reserve will persist after rollback.
A PV-compatible permanent hold and later Volvo 6 A resumption remain
separate work.

See [runbook](../temporary-soc25-evpaused-pilot-2026-11-01.md).
