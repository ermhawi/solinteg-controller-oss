# ADR 2026-10-31: One-off adaptive 1.5 kW house battery recovery

**Status:** Approved and physically active 2026-10-31 12:04 CET,
bounded one-off experiment. Not a permanent addition to the
production inverter dispatcher.

## Decision

After the 1 kW pilot aborted on an L3 appliance-like **+1.6 kW**
transient (grid L3 peaked **3,277 W**), raising only the power limit
would cause repeat stops or unnecessary stress. The owner's new
approval is for up to **1.5 kW** with phase-dependent derating,
existing **3.2 kW/phase and 6.5 kW total STOP** thresholds and an
automatic return to ordinary policy at **25% SoC**.

Reuse the installed dispatcher's actual `guard_l3_charge`,
`force_charge`, recovery hysteresis and inverter grid/battery
hardware limits, not a competing Modbus writer. Add a **temporary,
root-owned launch wrapper** in the dispatch *process* that changes
the L3 charge guard's minimum floor from its 1500 W recovery target
back to its existing 300 W safety floor just during the guard call.
The original recovery computation still requests 1500 W; the
L3 budget, confirmed inverter balance, and CT validation can
reduce it. Explicitly bypass the dispatcher's ordinary 250 W
change deadband for all downward charge moves.

Separate the **3,000 W L3 planning budget** (aiming to derate early)
from the independent **3,200 W phase import STOP**. When live data
are missing, the L3 guard permits only the original 300 W recovery,
not 1500 W. The independent root-owned 15-second watchdog checks
both the wrapper+process identity and the same original root EV
gate, EVCC 6 A, inverter, phase, temperature and SoC state.

The temporary process-specific `SOLINTEG_BATTERY_MAX_W=1500`
also clamps optimizer-driven forced charge; it prevents the
planned economic charge from outranking recovery and exceeding
the approved physical maximum.

## Safety limitations

- CT measurements every ~30 seconds, dispatch decisions every ~30
  seconds, and a watchdog every ~15 seconds do **not** guarantee
  instantaneous phase-current regulation. The installation's
  circuit breaker remains the actual overload protection.
- `50006=0` equal-phase assumption is essential and read back
  from the existing poller; unknown or inconsistent values derate.
- 100 W charge steps and a **300 W** fallback avoid a new untested
  zero-power hold; **no** forced PV priority or EVCC current change
  is authorized.
- The old 1 kW pilot's root state remains as an audit log.
  New root state and drop-in have different names, preventing an
  old aborted trial from being silently reactivated.
- The launch wrapper and supervisory scripts are SHA-pinned by
  the new root watchdog, and the installed original dispatch
  source remains pinned by the independent EV priority root gate.
- Any new physical test beyond this individual approval requires
  separate authorization. At completion, the normal dispatcher's
  economic reserve may allow SoC to decrease again.

See [temporary operations](../temporary-soc25-adaptive-pilot-2026-10-31.md).
