# Sundsvall Sunwoda SoC recovery calibration — 2026-10-23

## Temporary 25% recovery test — 2026-10-31

A separate, user-approved, two-hour-bounded physical trial started
11:26 CET. The ordinary 21% recovery exited into Auto at 11:21;
at 11:25 the Sunwoda battery had fallen back to 20% while supplying
roughly 1.7 kW of the house, including coincident 6 A EV charging.
This is a concrete recurrence of the observed recovery/idle cycling,
not evidence of where individual battery watts were delivered.

**Temporary mitigation:** Dispatcher-only systemd process override
sets recovery exit 25%, recovery target 1000 W, and physical forced
battery-target cap 1000 W. Existing root/standard watchdogs, site
import limit, inverter floor, shared env, EVCC 6 A and the EV grid
priority gate `mode=shadow` were unchanged. An independent root
timer performs measured-power/CT/EVCC/SoC safety checks every ~15 s
and automatically restores the normal dispatcher start at 25%,
by 13:26 CET, or on failure. At first verified load the inverter
was actually charging around 1024 W while EVCC still read 6.0 A.

**Observed outcome:** The watchdog automatically **aborted at
11:42:49 CET**, after the recorded L3 grid import spiked from about
1.67 kW to **3.277 kW**, breaching the intentionally conservative
3.2 kW per-phase stop limit. An additional L3 load of roughly 1.6 kW
appeared, but the appliance was not identified. The original dispatcher
was restored at **11:42:56 CET**, the pilot timer disabled, and battery
SoC was **21%**. It **did not reach 25%**. No repeat trial or higher
physical charging limit is authorized by this event.

See [operator runbook](docs/temporary-soc25-pilot-2026-10-31.md)
and [ADR](docs/architecture/adr-2026-10-31-temporary-soc25-recovery.md).
Do not convert this one-off approval into permanent 1000 W recovery.

## Current code status — 2026-10-27 consolidation

The 17% reserve and 13–16% recovery experiment described below is **historical**.
The current Sundsvall site configuration instead uses a 12% physical software
floor, a 20% economic planning reserve, and recovery hysteresis from below
19.5% to at least 21%. The reserve does **not** require the unverified 0 W
open-cap hold; `DISPATCH_RESERVE_HOLD_VERIFIED` remains an independent,
default-off gate for that actuator. The optimizer now defaults to the planning
reserve unless a caller explicitly supplies an alternative floor. None of
these repository changes automatically deploys new code or writes to hardware.

## Evidence (read-only on the live Solinteg inverter)

- Reported installation: three Sunwoda 5 kWh modules (15 kWh nominal; controller config 15.055 kWh). Exact Sunwoda BMS model/firmware has not been confirmed.
- Inverter FC03 register 52502 = 1 (on-grid SoC protection enabled).
- Inverter FC03 register 52503 = 100 (0.1% units, i.e. **10%** on-grid minimum).
- Inverter FC03 register 33000 = 1500 (**15% SoC**).
- Inverter FC03 register 33003 = 150 (**15°C battery temperature**).
- BMS charge current limit 33021 = 179 (**17.9 A**).
- BMS discharge current limit 33023 = 188 (**18.8 A**).
- The 53508-53514 EMS-access-only status/error/warning register block read all zero, but this is **not** proof of absence of battery alarms: this interface may not publish these values without direct BMS EMS access.
- All registers were READ, no inverter/BMS register or firmware setting was written.
- Manufacturer SunESS H datasheet: 90% maximum DoD, equivalent to **10%** remaining. The manufacturer's exact BMS low-SoC *re-enable* threshold is not published/verified for this firmware.

## Root cause / why recovery was wasteful

The live site config had `SOLINTEG_SOC_FLOOR_PCT=12`, battery capacity `15.055` kWh,
and the old **0.5 kWh** guard headroom. Thus recovery forcibly charged at
`12 + 100 * 0.5/15.055 = 15.32%` until **20%**, then the optimizer resumed
discharging toward 12%. On 2026-10-22 the dispatch audit recorded **five**
recovery-on/off cycles (235 of 306 audit decisions were in recovery); solar
production was only about 0.8 kWh. The low production was real, but repeated
grid-powered protection cycles can add conversion losses and electricity cost.

At 15% SoC the BMS still advertises **18.8 A discharge allowance**. This
does not prove what it will permit below 15%, but rules out an unconditional
documented need to force-grid-charge the pack from 15% to 20%.

## Site-specific cautious first calibration (controlled rollout)

- Leave `SOLINTEG_SOC_FLOOR_PCT=12` untouched; this is above the inverter
  10% protection and remains the hard operational planning/discharge floor.
- Set `DISPATCH_SOC_FLOOR_GUARD_HEADROOM_KWH=0.15`, making the emergency
  entry approximately **13.0%** at 15.055 kWh.
- Set `DISPATCH_SOC_RECOVERY_EXIT_PCT=16` (3 percentage points hysteresis);
  this is a **local trial**, not a claimed Sunwoda BMS restart percentage.
- Keep `DISPATCH_SOC_FLOOR_RECOVERY_W=300` unchanged, and keep the recovery
  override higher priority than the normal optimizer.
- **Do not change** inverter on-grid SoC protection setting (10%) or BMS
  safety limits; do not increase the optimizer floor to 20% as a substitute.

### Validation limits and safety

A unit test verifies 15% normal operation, recovery entry <13%, hysteresis
to 16%, and emergency charging below the 12% operational floor.

Replay of logged 21–22 Oct readings can show *how often the new trigger would
have activated on the OLD trajectory*, but it cannot predict real economic
savings or future minimum SoC: the old controller actively held SoC at 15%.
Only forward observation can reveal whether a reduced guard is sufficient.

Post-change inspect `control_actions` and `readings` for at least a full
night, especially SoC near 13%, BMS allowed discharge, actual battery power,
BMS/inverter alerts, and unexpected Modbus write failures. **Stop/rollback
on a BMS discharge inhibit, alarm, inverter protection, SoC below 12%, or
unexpected rapid repeated recovery cycles.** Do not force a discharge into a
protected battery just to test it.

### Revert

Restore the saved `/opt/solinteg/solinteg.env` version and restart
`solinteg-dispatch`; original site behavior has no explicit recovery
variables and therefore uses code defaults: 0.5 kWh headroom and exit at
`SOLINTEG_SOC_FLOOR_PCT + 8` = 20%.

The private production env file is **never committed to Git**.

## Follow-up: eliminate normal optimization → emergency recovery cycling

Code change on `Sundsvall` adds an **independent opt-in planner reserve**
`SOLINTEG_PLANNER_RESERVE_PCT=17`, while keeping
`SOLINTEG_SOC_FLOOR_PCT=12` and recovery entry ~13%, exit 16%. The live
TypeScript optimizer takes `minSocKwh` only on the production planning path:
the hindsight oracle and unrelated installs keep the original safety-floor
model. The Python dispatcher, at or below 17%, refuses ordinary discharge
and idle→auto when there is no live PV surplus. It would instead invoke a
0 W EMS open-cap hold; when PV surplus is available it returns to Auto so
free solar can fill the battery. Emergency recovery retains top priority.

**Important: the open-cap 0 W hold is NOT probe-verified on the Sundsvall
inverter.** It was physically verified on an upstream reference unit only
(MODBUS.md). The new dispatcher therefore refuses to start with the economic
reserve enabled unless `DISPATCH_RESERVE_HOLD_VERIFIED=1` is set as a second
explicit opt-in. Never bypass this gate just to get the code running.

To safely unlock:
1. First wait for a natural SoC above **20%**, darkness (<150 W PV),
   house drain >400 W. Verify that the inverter's backup/EPS output
   has no connected load; the existing probe may temporarily restrict
   inverter AC output in its optional full-freeze phase.
2. Disarm the dispatcher and run the documented
   `scripts/tools/probe_hold_freeze.py` with proper backup/restore as
   documented in its own RUNBOOK. Watch the battery's actual 30258 power
   and grid meter; require ~0 W battery and matching grid import for the
   soft-hold phase. Restore and verify original mode and caps.
3. Only after recording a successful local measurement set BOTH
   `SOLINTEG_PLANNER_RESERVE_PCT=17` and
   `DISPATCH_RESERVE_HOLD_VERIFIED=1` in
   `/opt/solinteg/solinteg.env`; build the Next.js application
   (site constant is build-time), restart web+dispatch and explicitly
   request a fresh optimizer plan. Confirm live `reserve_hold` decisions,
   readback and **physical** stable SoC.
4. Preserve original configuration and deploy build for rollback;
   if hold proves ineffective, remove the reserve variables, rebuild
   web with old settings and restart dispatch. Never change the 10%
   inverter BMS protection or 12% software safety floor.

Historical replay of saved `optimizer_runs.inputs_json` from Oct 21/22,
using 15.055 kWh, 12% physical floor and a 17% planning reserve, found:
- Oct 21, 229 slots: old minimum 12.00%, new minimum 17.00%;
  simulated grid cash cost difference +133.2 öre for the new plan.
- Oct 22, 229 slots: old minimum 12.00%, new minimum 17.00%;
  simulated cash cost difference +102.4 öre for the new plan.
These are horizon-wide *planned* cash values, not realised single-day losses
or controlled-recovery costs, and DO NOT establish net savings. A closed-loop
real-world trial is needed after the hardware gate clears.

The prior 13%→16% trial remains in production in the interim. This code
change does NOT itself enable 17% reserve or issue a hold command to the
unverified Sundsvall inverter.

## Read-only follow-up, 2026-10-23 15:33 Europe/Stockholm

- **PlaneraElen restored:** commit `4afc98d` on `Sundsvall` replaces the
  HTTP 410 endpoint with `https://planeraelen.se/api/price-data`, accepts
  `forecast_issued`, and preserves both 02:xx occurrences during the Oct 25
  DST fallback. 653 tests passed (1 skipped); isolated staging served the
  complete 100-slot Sunday. A **web-only** deployment at 15:24 used a saved
  rollback and did not restart dispatch/poller. Production price API returned
  192 actual + 158 forecast slots; a fresh optimizer run at 15:28 had
  231 intervals, through 2026-10-25 23:45. The earlier 410 warnings predate
  the successful web restart.
- **Low-power setpoint is not a physical hold:** `control_readbacks` confirms
  50 W discharge writes/readbacks `50207=5` in mode `0x303`, but stable
  `readings.battery_w` averaged **+211 W** over 12 eligible periods
  (153–283 W). Ten same-window register-readback samples had median +211 W.
  This is consistent with a firmware-imposed minimum/low-power behavior,
  but does not prove its mechanism. Do not infer physical battery protection
  from a successful setpoint readback.
- **Recurrence evidence:** recovery completed at 13:59 on 16% SoC; optimizer
  immediately commanded 300–800 W normal discharge (14:00–14:14), followed
  by 50 W battery-save (actual roughly +200 W), then idle/Auto at 14:45
  (measured approximately +600 W). Recovery reactivated 14:55 at 12%.
  During the six hours ending 15:33 there were **two recovery starts and
  two completions**, SoC range 12–16%. At 15:33 SoC was 13% with approximately
  −296 W measured recovery charging.
- **Operational decision:** keep `SOLINTEG_SOC_FLOOR_PCT=12`, ~13%-entry,
  16%-exit, 300 W recovery, and both reserve opt-ins **unset**. Do not
  compensate by guessing another 50207 command, enabling three-cap freeze,
  or auto-testing 0 W hold. Wait for **natural SoC >20%** and the separate
  documented safe local hold-verification prerequisites; then request a
  supervised test, never an automatic one. The prepared
  `probe_hold_freeze.py` now defaults to **soft hold only**, requires
  the running dispatch process to be disarmed and SoC **strictly over 20%**,
  and treats an unverified restore as failure; its full-freeze step requires
  separate explicit EPS confirmation. This is a static preparation,
  **not a Sundsvall inverter test or permission to enable 17% reserve**.
  Continue the existing 48-hour read-only watch for BMS issues, Modbus
  errors and repeated cycles.
