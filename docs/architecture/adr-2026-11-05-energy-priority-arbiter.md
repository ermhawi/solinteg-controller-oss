# ADR 2026-11-05: central priority for battery reserve, solar capture and EV

**Status:** Source/tests implemented on `Sundsvall`; **selectively deployed and enabled
on Dell at 2026-11-05 11:28:46 CET** after explicit operator approval.
`DISPATCH_ENERGY_PRIORITY=1` is set in a dispatcher-only systemd drop-in;
the source default remains `0`. The native EndSOC/EMS firmware question is
still unresolved. See the live-release evidence below.

## Incident and observed facts

On 2026-11-05, while house battery SoC was 13–14%, the deployed dispatcher
alternated between General and a requested EMS 50 W battery-save discharge
in response to one instantaneous PV-minus-house-load measurement near
100 W. Under forced EMS, the inverter physically discharged about
280 W while exporting roughly 850–950 W despite sufficient PV.
General did charge the battery. The site's existing physical/poller
telemetry and control_actions reproduce this behavior; the Modbus broker
serialized the applied transactions as intended.

The production dispatcher is a **site-specific 1,861-line file**, not an
identical copy of the ~2,197-line source version. In particular, its own
EV/L3 live hook and later low-SoC adjustments must be retained.
**Do not overwrite the installed dispatcher with the Git version.**

## Decision: constraints first, then one economic resolution

Physical safety requirements constrain every energy optimization proposal:

| Order | Requirement | Executor consequence |
| --- | --- | --- |
| 0 | Inverter/BMS safeguards, valid SoC/telemetry, AC/phase budgets | Reject or cap unsafe operations; no claimed certified fuse protection |
| 1 | Physical SoC recovery (site: approximately 13% entry, 16% exit) | Override idle, battery saving and price waiting; obey phase limits |
| 2 | EV grid-first and its independent charge/pause ownership | Never deliberately use the house battery to power a verified charging EV |
| 3 | Real solar capture below economic reserve (20%) | Prefer General when PV surplus is stable and EV is verified not charging |
| 4 | Price optimizer (after efficiency, sell value and battery wear) | Its genuine planned charge wins; above reserve its discharge plan remains authoritative |
| 5 | Economic EMS battery save (requested 50 W) | Only below reserve when verified solar capture is unavailable |
| 6 | Export | Leftover generation, or optimizer-selected economic export above reserve |

These are **policy priorities**, not promises that electrons are physically
traceable. A request of 50 W in EMS often means 150–290 W actual
discharge; a soft 20% reserve is **not** a firmware/BMS hard floor.
Native register 52503 remains unchanged and its precedence in EMS
is unverified (see the separate firmware/EndSOC investigation).

### Single final energy arbiter

Existing stages remain intact:

1. Optimizer creates a proposal, including batterySaveDriftKwh.
2. SoC recovery overrides the proposal when actually in its emergency band.
3. EV/L3 policies suggest lower discharge/solar charging only with verified data.
4. **New pure energy arbiter** reconciles economic reserve, forced EMS save,
   actual solar capture, recovery and EV state once.
5. Existing hard L3/site-budget guard caps charging.
6. Existing broker is the **only** inverter writer. No new command,
   watchdog, daemon, direct TCP client or EVCC writer is added.

Implementation: scripts/services/energy_priority_policy.py (pure resolver),
scripts/services/dispatch_loop.py (one guarded call after all EV proposals),
scripts/tests/test_energy_priority_policy.py, and read-only
scripts/tools/replay_energy_priority.py.

The arbiter may change a low-SoC candidate to only the existing
idle/General or discharge/EMS 50 W action. It never invents a charge,
raises a charge target, changes EVCC, or skips an existing recovery charge.
A planned charge or explicit solar_charge action has precedence. An
otherwise rejected solar-funded charge must **not** silently route
back to battery-draining General when measured solar is absent.

## Hysteresis and observation quality

Initial, **unqualified production thresholds** (to be tuned by replay):

- Enter solar capture only after two separate, sequential inverter
  readings 15–55 s apart with PV − house >= **300 W** in both,
  freshest <=75 s, and latest showing >=100 W export or >=200 W charging.
- Continue General across short PV/house-load dips; do not switch
  back to EMS just because one sample crosses the old 100 W threshold.
- Exit after two observed deficits <= **−200 W**, or two discharges
  >600 W, once at least 90 s have elapsed since entry.
- Immediate exit if actual battery discharge >=1,500 W at the latest
  observation, or accumulated *estimated* discharge exceeds **30 Wh**
  during the latch. This calculation is sample-based, not a physical
  lower-SoC or energy guarantee.
- Once solar capture exits, block its re-entry for **90 s** after
  sustained deficit or **180 s** after excessive measured discharge.
  An exit must never re-enter General in the same decision just because
  the previous two samples still show PV surplus. Preserve this
  cooldown through a brief EVCC/telemetry uncertainty.
- Stale/inconsistent inverter data, unverified EVCC status or verified
  EV charging inhibits **new** General solar transitions below reserve.
  The lower-energy EMS 50 W request is still not a zero-discharge hold.
- Require an independently fresh and non-charging EVCC state. EVCC's
  real A1 records have charging_detected=null, so A1/B2 with no positive
  charge evidence counts as not charging. C2/charging is a strict veto.
- If SoC is missing/untrusted, preserve the existing candidate rather
  than issue a new unverified physical decision.

A future EMS-specific PV-only charge may avoid General's unplanned
discharge, but remains **out of scope** until actual import/phase
behavior is proven. A high solar output does not authorize grid-funded
charging without the optimizer or physical SoC recovery.

## Deterministic acceptance matrix

- Recovery under the site emergency band beats every other action.
- Planned charge is not replaced by battery saving or solar switching.
- Verified charging EV cannot trigger low-SoC General transition.
- Two fresh, trustworthy surplus readings switch EMS save to General.
- A single intermittent house-load spike cannot immediately cancel that state.
- Sustained deficit or measured discharge budget returns General to EMS save.
- Stale/repeated/tampered samples cannot establish solar availability.
- Economic reserve is not promoted into mandatory grid charging.
- Above the economic reserve, the present optimizer still owns economics.
- Production feature flag 0 gives **identical behavior** to the original
  dispatcher; no extra hardware code path is executed.

## Read-only replay, limitations and rollout

On 2026-11-05, 10:20–11:00 Europe/Stockholm, replay of actual site telemetry,
applied action history and matching EVCC evidence (all 40 replay ticks
verified EVCC A1/not charging) yielded:

- Baseline applied-target changes represented as minute-held actions: **6**.
- Proposed resolver action changes in that counterfactual replay: **2**.
- Solar capture was the winning proposal in **23** ticks.
- **2** timestamps explicitly converted a historical EMS 50 W decision
  into General under measured surplus.

The replay is **not a simulated physical inverter**: it reuses historical
physical measurements that would have changed under a different strategy.
It cannot prove that the battery would gain a particular amount of energy,
or that the controller would still see the same subsequent house/grid
signals. The control_actions table records applied actions, not each
intermediate decision, so change counts are indicative, not a causal
before/after benchmark.

**Offline verification:** 30 targeted policy/integration tests and the full
Python regression suite (1,607 tests) passed on 2026-11-05. The read-only
replay queried the live SQLite files with mode=ro; EXPLAIN QUERY PLAN for
the new recent-readings query confirmed an indexed range search via idx_ts.
There were no device writes, service changes or persisted private replay
outputs. Git-hygiene preflight is separately blocked by pre-existing and
new uncommitted work; that is not evidence of a failing policy test.

**Release gate completed 2026-11-05:** Operator subsequently authorized
activation. A selective, exact-hash-pinned patch against the installed
site-specific dispatcher passed AST comparison and staged import tests,
original files were backed up and rollback validated, and live startup,
feature-flag inheritance, broker routing and telemetry were verified.
Existing broker failure and watchdog behavior remain unchanged.
Do not merge `Sundsvall` into `main`.

## Live rollout on Dell, 2026-11-05

**Explicit operator approval:** Activate the new priority function now.
This is a selective, exact-hash-pinned patch to the site-specific dispatcher,
not a synchronization or replacement with the entire Git implementation.

### Release evidence

- Root-only release directory: `/var/lib/solinteg-evshadow-rollback/energy-priority-live-20261105/`,
  containing a baseline backup, staged candidate, policy module, checksummed
  manifest, executable rollback script and status evidence.
- Original installed dispatcher SHA-256:
  `4a87facac31ec745765f80e6c2baaee6ad206fd87d9eb1bd2dff0b52155e2f19`.
- Activated dispatcher SHA-256:
  `274d93f6bdeb7dfdcc2a8f63e82312128e11035e3a0ba1b1a75183888b045cf7`.
  AST comparison verified that `main()` was the only changed existing
  function; three new energy-arbitration helpers were added.
- New policy module SHA-256:
  `4689830e4eac75e4605e24b072787f118cf8333f849cfb1f5ddbe2f53a0372a2`.
- New and only systemd drop-in:
  `/etc/systemd/system/solinteg-dispatch.service.d/40-energy-priority.conf`
  sets `DISPATCH_ENERGY_PRIORITY=1`. No secret environment, broker, watchdog,
  EVCC, BMS register, or unrelated service was modified.
- At 11:28:46 CET, the dispatcher restarted successfully; live process
  confirmed the feature flag and continued the existing
  `SOLINTEG_MODBUS_SOURCE=broker_transaction` transport.
  Poller, broker, watchdog and EV-price controller were all active,
  with zero dispatcher restarts at the initial post-deployment check.
- Preflight: SoC 15%, battery 14 C, EVCC A1/not charging, balanced output,
  fresh poller and EVCC observations, and PV above house consumption.
- First applied control at 11:28:58 CET was `charge 750 W` with
  `energy_priority_winner=soc_recovery`. The existing conservative
  SoC-recovery latch runs after restart until 16%: the new policy correctly
  preserved this higher-priority safety decision.
  Poller at 11:29:18 CET measured battery -734 W, PV 1,620 W,
  house 1,547 W, and grid import 706 W. Grid-funded recovery at 15%
  is existing recovery behavior, not a new solar policy command.

### Rollback and remaining verification

Root-only rollback command:
`/var/lib/solinteg-evshadow-rollback/energy-priority-live-20261105/rollback.sh`.
It checks original/current hashes, restores only the prior dispatcher,
removes the release-specific drop-in, reloads systemd and restarts dispatch.
The unused policy module and private artifacts are preserved for inspection.
Never blindly restore if the current installed code hash has changed.

**Outstanding:** Confirm actual General/EMS hysteresis after the existing
SoC recovery finishes at 16%, including transient house-load spikes,
EVCC charging and grid/battery measurements. The 24-hour operating result
is not yet verified. Native EndSOC/EMS timeout uncertainty remains.

## Follow-up / known limitations

1. The current high-SoC economic optimization still lives in its existing
   stages; this version centralizes **the low-SoC solar-versus-save
   conflict**, not every future energy objective.
2. Unknown EVCC state conservatively prevents a new low-SoC General
   solar entry. This sacrifices some possible PV yield to avoid powering
   an unverified EV from the house battery.
3. General may still discharge into the house before the next 60 s
   decision; the ~30 Wh budget and temperature/SoC safeguards are not a
   hardware cut-off. Native 20% floor remains an independent inquiry.
4. Hysteresis and physical response must be validated by a supervised
   release and at least 24h observation before considering the work
   production-complete.

## Källuppföljning 2026-11-07 (D06, ej installerad)

Den senast committade käll-dispatchern skiljer på `SolarState` som policyförslag och fysisk återkoppling. [D06:s post-ACK-feedback och resetmatris](../operations/dispatch-solar-feedback-2026-11-07.md) kräver två färska, konsekventa inverter-/batterimätningar **efter** en accepterad General-begäran innan intern `capturing=True` får kvarstå; `observed_general` vid tillfälligt underskott är inte `observed_charging`. Inhiberad, skippad, misslyckad eller enbart pending-skrivning ger `unknown`. Det här ändrar **inte** live-installationens 2026-11-05-konfiguration eller tidigare godkännande och ersätter inte dess separata rollback och fysiska utvärdering.
