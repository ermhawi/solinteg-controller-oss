# EV-paused house-battery 25% recovery handoff — shadow-only design (2026-11-01)

**Status: proposed / not deployed.** The normal live dispatcher remains unchanged
and recovers the Sunwoda battery at roughly 300 W only until **21% SoC**.
The user has paused the Volvo via EVCC. This plan does not change the inverter,
charger, active root gate, shared environment, systemd units or watchdogs.

## Problem and proposed decision

During the 2026-11-01 incident, the normal dispatcher completed low-SoC
recovery at 21%, returned to `idle`/Auto and the house battery discharged
back below the recovery band. Raising the exit threshold alone would only
move this problem to 25%.

A **separate, read-only policy** in
`scripts/services/soc25_handoff_shadow.py` proposes a narrow 25–26%
hysteresis for review: where the original plan would return to Auto or
discharge at/below 25%, consider retaining the previously validated
**300 W EMS charge** until 26%, rather than an immediate Auto handoff.
An already scheduled charge of 300 W or more is preserved. This
is a *counterfactual setpoint suggestion*, never an applied inverter target.
The model is neither a verified battery hold nor proof that 25% will be
maintained without cycling.

The proposal explicitly rejects or withholds a charge candidate when:

- The DEFA/ABL EVCC pause is not independently measured
  (`B2`, configured PWM `1000`, measured current no more than 1 A).
  The observed paused EVCC pilot raw `267` is **not** a pause readback
  and is not treated as an authorization to resume.
- A sample or control action is missing/stale, inverter SoC/temperature
  is out of scope, phase CT totals disagree or phase output is not
  verified balanced (unless replay explicitly labels that assumption).
- A conservative projected change from the currently observed battery
  discharge to 300 W charge could exceed **3,200 W on any phase** or
  **6,500 W total import**. These are prior test envelopes, **not**
  certified mains-fuse or instantaneous RMS protection.
- PV may already be charging the house battery from true surplus:
  the model flags the Auto/solar handoff as **unverified**, not as safe
  to replace with an untested 50 W discharge or 0 W hold.

At 26%, the *hypothetical* recovery latch releases. Thus cycling between
25–26% remains possible, and stable retention is **not verified**.
A production solution must also preserve economically useful PV charging,
bound duration and grid costs, and handle any unexpected EV resume.

## Historical replay and verification

`scripts/tools/replay_soc25_handoff.py` opens only the existing
`telemetry.db` and `ev-history.sqlite` using SQLite `mode=ro`; it
does not connect to EVCC, the inverter or Home Assistant. Run on the
private Dell machine with appropriate **read-only** database access:

```bash
python3 -m unittest scripts.tests.test_soc25_handoff_shadow -v
sudo -n -u solinteg-evshadow python3 scripts/tools/replay_soc25_handoff.py --hours 4
```

Historical `ev_samples` lack the inverter's contemporaneous register
`50006`; replay **assumes** balanced output, marks every candidate
non-actuating and never reports fuse safety or physical retention as
verified. It cross-correlates the independent EVCC observations,
actual inverter measurements and *applied* dispatcher decisions. The
observed `idle` → SoC decline is a factual diagnostic, **not** proof
that the proposed action would have prevented it.

On 2026-11-01, all 13 focused synthetic tests passed; read-only
historical replay found the previous idle/SoC decline and showed that
the observed period with the EV paused still only contained existing
300 W recovery, **not** a 25% post-recovery handoff. No real
25–26% handoff could therefore be validated.

## Before physical deployment (not authorized)

1. Verify actual 25%→Auto behavior with fresh EVCC, CT, battery,
   phase-balance, PV and BMS evidence; first ensure SoC is above the
   old pilot's 19% safety floor. Record independent EV pause/resume.
2. Implement an *independent* root-owned watchdog with fail-safe rollback,
   expiry, CT/SoC/temperature monitoring and bounded charge/price budget,
   not a second Modbus/EVCC writer. A supervisory stop must not silently
   return to discharge-heavy Auto during a vehicle load.
3. Test night/PV transitions and reactivation of Volvo at 6 A
   separately. The 50 W discharge emulation is **not** a tested
   PV-preserving hold; 0 W open-cap hold remains unverified.
4. Obtain **new explicit approval** for any ARMED dispatch, inverter,
   watchdog, systemd or EVCC change. This design does not reuse the
   now-aborted one-off 25% pilot approval.

See the [31 October pilot](temporary-soc25-adaptive-pilot-2026-10-31.md),
[EV-grid priority limitations](ev-grid-priority-permanent-2026-10-29.md)
and [ADR](architecture/adr-2026-11-01-soc25-handoff-shadow.md).
