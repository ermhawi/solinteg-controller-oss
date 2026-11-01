# Sunwoda 25% SoC reserve with unplugged Volvo — 2026-11-01

**Status: physically activated 2026-11-01 19:07 CET as an isolated six-hour
trial.** The independent root watchdog accepted the unplugged `A1` state
and both systemd timers are active. The hard stop is 2026-11-02 01:07 CET.
**This does not yet prove recovery at 26.5% or lasting 25% retention:**
the house battery was still at 38% at activation. No permanent hold or
hardware/BMS threshold change is authorized by this implementation.

## Goal and observed EVCC state

The previous EV-paused 1,500W trial charged the house battery to 26% at
11:11 CET but ordinary dispatch immediately resumed a 50W discharge target
(measured 172–197W) and SoC returned to 25%. Normal recovery is configured
to start below 19.5% and exit at 21%; the 20% economic planning reserve is
not an inverter-level stop. A separately approved **25% reserve** should
prevent repetitive deep cycling without blocking PV surplus capture.

On 2026-11-01 18:56 CET the EVCC observer reported **A1** (car physically
disconnected), `channel_currents_a=[null,null,null]`,
`measured_current_a=null`, `configured_pwm_raw=1000`,
`charging_detected=false`. A1 is **not a measured zero-ampere reading**.
The admission policy deliberately accepts either fresh independent
`A1` with all three missing current samples, or `B2` with
verified 0–1A current and PWM pause. On plugging in and starting charging
(`C2`), or any unknown/uncertain state, the root watchdog stops this trial
and restores ordinary dispatch. This feature never resumes/changes EVCC.

## Reserve strategy (not a physical zero-power hold)

- Preserve the installed original ARMED dispatcher (identified by SHA256),
  shared private environment, existing inverter on-grid SoC protection
  and BMS limits. **Only** the original dispatcher sends inverter commands;
  the process-local `soc25_reserve_dispatch.py` alters its recovery
  thresholds and charging decision.
- **Entry below 26.5% SoC**, and maintain forced recovery until
  **at least 28%**. This deliberately provides buffer above 25%
  for 30-second telemetry/dispatcher latency and SoC quantization.
  No new hardware on-grid floor or physical 0W hold is attempted.
- During recovery, raise the former 300W fallback request to up to
  **1,500W grid-assisted**, retaining 300W on stale/untrusted CT via
  the original L3 charge guard (planning budget 2,700W L3).
  With real *PV minus household load* surplus, permit a correspondingly
  larger solar-supported request, capped to 5,500W and reduced by the
  original phase guard as needed. When solar wanes, a lower target
  bypasses the economic reassert deadband.
- Above the entry/exit band, original optimization and PV charging
  are unchanged. This is hysteretic **recharging**, not a permanent
  zero-watt retention mechanism: after reaching 28%, the optimizer
  may again discharge until the next recovery entry. Grid charging
  at an expensive spot price is possible within this protected band.

## Independent protective conditions

Read-only preflight, before and during root installation, requires:
fresh (<=65s) inverter and EVCC snapshots; 5min continuous CT history;
SoC **24–90%**, 5–45°C BMS temperature; 50006 balanced; internally
consistent total/phase/house/PV/battery readings; unchanged dispatcher
hash; ordinary systemd watchdogs active; no 45–48 conflicting overlay or
EV priority root gate. Predict a 1,500W charge from the *current* battery
discharge and refuse if projected import reaches **3,000W/phase or
6,300W site**. Recent measured peaks must remain below those values.
Preflight opens SQLite exclusively in `mode=ro` and never writes a device.

The independent root 15-second timer supervises:
- Observed phase import <3,200W per phase and site import <6,500W,
  fresh internally consistent CT/PV/BMS, SoC >=23%, 5–45°C temperature;
  **these software limits are NOT certified instantaneous fuse protection**.
- Charger remains positively verified disconnected A1 or paused B2;
  unverified or active EVCC C2 forces rollback, no action on the charger.
- Original dispatcher, pinned root-owned wrapper/policy and independent
  safety timers remain unchanged and working. The root supervisor never
  imports a Modbus client, communicates with EVCC or writes an inverter
  register itself.
- Below the recovery band (SoC <=26%), actual inverter EMS mode and
  physical battery charge must be verified after 120 seconds; missing
  physical charge for >180 seconds causes rollback. During recovery,
  excessive grid-funded charge is rejected, but normal *nonrecovery*
  scheduled charging above the band may exceed 1.5kW.
- Every trial expires after **six hours**, or is stopped at the operator's
  request or upon reboot. A **second, independent one-shot deadline timer**
  calls the same owned rollback after six hours even if the regular 15-second
  sampling timer stops. Both timers are disabled after normal rollback.
  There is no recurring automatic reactivation.

On any stop, the root watchdog removes ONLY its checksum-verified
`48-soc25-reserve.conf`, reloads systemd, restarts the byte-identical
original ARMED dispatcher, records the outcome and disables its own
timer. Existing EVCC state and older completed 45–47 experiments
are unchanged.

## Commands

Read-only admission:

```bash
sudo -n python3 scripts/tools/start_soc25_reserve.py --check
```

Explicit physical one-off activation (only after successful preflight and
separately scoped approval):

```bash
sudo -n python3 scripts/tools/start_soc25_reserve.py --start --confirm
```

Monitor/stop, using the root-installed immutable copy:

```bash
sudo -n python3 /usr/local/libexec/solinteg-soc25-reserve/soc25_reserve_watchdog.py --status
sudo -n python3 /usr/local/libexec/solinteg-soc25-reserve/soc25_reserve_watchdog.py --stop
systemctl is-active solinteg-dispatch.service solinteg-soc25-reserve-watchdog.timer solinteg-soc25-reserve-deadline.timer
```

Note that the last EV-paused trial had a transient switch to Auto because
`RuntimeMaxSec=2h` was retroactively applied to a long-running dispatcher
on `daemon-reload`. The **new** root-owned overlay does not set
`RuntimeMaxSec`; a second independent systemd deadline timer enforces
its maximum runtime without changing the running dispatcher's age.
A normal dispatcher restart can still briefly hand control back to Auto.

## Physical activation evidence — 2026-11-01

- **18:56 CET:** Read-only live observation SoC **39%**, battery
  **+975W discharging**, Volvo CP **A1** without current measurements,
  PV **0W**, no 48-prefixed overlay. The completed earlier 47-prefixed
  test was not restarted.
- **19:07 CET:** Strict five-minute read-only preflight **passed**:
  SoC **38%**, EVCC mode `disconnected`, predicted maximum import
  approximately **1,084W/phase** and **2,446W total** for an extra
  1,500W reserve charge. Two root-owned watchdog/deadline timers were
  enabled *before* the ARMED dispatcher restart; installer returned
  `started_supervised` at **19:07:21 CET**.
- **19:07:47 CET:** Real `/proc` inspection confirms the process runs
  `soc25_reserve_dispatch.py`, both isolated environment keys are
  present, original installed `dispatch_loop.py` remains SHA-matched,
  root status is `running` and both watchdog timers are active.
  Native work mode temporarily observed as General after the scheduled
  dispatcher restart; this was before the new target was independently
  read back. See subsequent live measurements rather than assuming
  persistent hold from this early sample.
- **19:08:46 CET:** Native EMS mode `0x303` is again physically
  observed, battery discharge **238W** with SoC **38%** and the
  normal optimizer active above the reserve band. EVCC remains
  **A1**, watchdog state `running` after fresh independent checks,
  and both timers are active.
- **19:09:32 CET (after the 120-second settle period):** Watchdog
  still `running`, SoC **38%**, actual battery discharge approximately
  **164W**, inverter in EMS mode `0x303`, EVCC still disconnected
  `A1`, regular and independent deadline timers active.
  This verifies normal operation above the reserve band, *not yet*
  an actual 26.5→28% recovery transition.
- The independent deadline timer's actual systemd `NEXT` was
  **2026-11-02 01:07:24 CET**. This is the *stop* deadline,
  not a guarantee that battery SoC will cross 26.5% before then.

## Tests and limitations

```bash
python3 -m unittest scripts.tests.test_soc25_reserve -v
python3 -m unittest discover -s scripts/tests -p 'test_*.py' -q
systemd-analyze verify deploy/solinteg-soc25-reserve-*
```

- Historical 25% SoC behavior is observed but **not proof** of a
  successful 26.5→28% physical recovery under this new policy. Verify
  inverter response on a future crossing of 26.5% before claiming success.
- The controller reads the same 30-second telemetry that is unsuitable
  as instantaneous main-fuse or RMS phase-current protection.
- Changing BMS/on-grid minimum SoC to 25% or using `50207=0`
  to hold the battery was *not* done. This firmware's three-register
  freeze has already failed a physical probe. The open-cap 0W hold
  on **this** Sundsvall inverter remains unverified.
- Because `A1` has no current measurement, it is trusted **only**
  as a fresh independently reported disconnected CP state, not
  as a verified current. Any ambiguity fails closed.

See [shadow handoff](soc25-handoff-shadow-2026-11-01.md),
[previous approved trial](temporary-soc25-evpaused-pilot-2026-11-01.md),
and [ADR](architecture/adr-2026-11-01-soc25-reserve-no-ev.md).
