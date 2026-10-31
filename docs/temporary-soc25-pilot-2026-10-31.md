# Temporary Sunwoda 20% → 25% recovery pilot (2026-10-31)

**Scope:** Explicitly approved one-time physical charge trial on the Dell
Sundsvall installation. This is **not** approval to activate EV grid-priority
or automatic EV/solar charge control.

The battery's normal recovery is approximately 300 W until 21% SoC.
The vehicle keeps its verified 6 A EVCC pilot and its own charging schedule.
The test uses an existing, independently watchdogged ARMED dispatcher, not
a second Modbus writer and not the unverified battery freeze/hold mode.

## Temporary dispatcher-only override

The existing `/opt/solinteg/solinteg.env` and the checksum-pinned live
dispatcher code are **unchanged**. A dedicated, root-owned systemd drop-in
is applied temporarily only to `solinteg-dispatch.service`:

`/etc/systemd/system/solinteg-dispatch.service.d/45-temp-soc25-pilot.conf`

Its `ExecStart` uses the pre-existing Python interpreter and source,
preceded by `/usr/bin/env` with:

- `DISPATCH_SOC_RECOVERY_EXIT_PCT=25` — exit recovery at 25% SoC.
- `DISPATCH_SOC_FLOOR_RECOVERY_W=1000` — recovery requests 1000 W.
- `SOLINTEG_BATTERY_MAX_W=1000` — existing inverter_control clamp
  bounds **all forced charge/discharge targets in this process** to 1000 W.
  This is necessary because an optimizer-planned charge can otherwise
  supersede the recovery rate.

No source change or import-limit increase is needed.
The standard site-level import limit remains configured.
The original dispatch systemd unit and its environment become effective
again when the exact temporary drop-in is removed and the unit restarted.

## Independent automated rollback

The separately installed `solinteg-temp-soc25-watchdog.timer` runs the
root-owned, no-network, no-Modbus
`scripts/services/temporary_soc25_watchdog.py` roughly every 15 seconds.

The root-only state is:

`/var/lib/solinteg-recovery-25-pilot/status.json` (0600)

The watchdog restores the original dispatch start (remove its own verified
drop-in, systemd daemon-reload, restart existing dispatch) when **any**
of these occurs:

- Reported house SoC reaches **25.0%**, or hard deadline **2 hours**.
- Inverter/EVCC snapshot stale by more than 65 seconds, malformed/missing
  L1/L2/L3 CT, inverter phase-balance readback unexpected, or inconsistent
  phase sums.
- Grid import on **any** phase exceeds 3,200 W; total grid import exceeds
  6,500 W. These are conservative diagnostic stop limits, **not**
  independent RMS/fuse protection.
- Battery SoC drops below 19%, temperature is outside 5–45°C,
  measured battery charging exceeds 1,250 W or stable charging ceases.
- Charging car is no longer verified at 6 A (EVCC C2, Ict2, pilot),
  dispatcher is inactive, dispatcher process override is missing, or
  the root EV safety gate/source checksum changes.

A 120-second startup settling interval only relaxes process/mode response
checks; timestamp, temperature, phase CT and EV 6 A checks are immediate.
The supervisor has no Modbus or EVCC writes. Both original hardware
watchdogs and the EV priority `mode=shadow` remain enabled.

After a successful rollback the one-off timer disables itself and records
`completed` or `aborted`. A crashed/interrupted restore retries on its
next tick. Do not tamper with the drop-in or state while the pilot runs.
If the root state is missing while an owned drop-in persists, manual
intervention is required rather than deleting unfamiliar configuration.

## Operator controls

Read status (no secrets printed):

```bash
sudo /usr/bin/python3 /usr/local/libexec/solinteg-recovery-25-pilot/temporary_soc25_watchdog.py --status
systemctl is-active solinteg-dispatch.service
systemctl is-active solinteg-temp-soc25-watchdog.timer
```

Abort the trial immediately, with verified restoration of the normal
dispatcher process environment:

```bash
sudo /usr/bin/python3 /usr/local/libexec/solinteg-recovery-25-pilot/temporary_soc25_watchdog.py --stop
```

Inspect the actual battery charging response in existing `live.json`
or `control_actions` (not just the accepted register setpoint). If the
timer or its watchdog service is unhealthy, **abort before attempting
further control**. Do not stop the original dispatch or watchdog timers
to conduct this test.

## Actual one-time field deployment — 2026-10-31

- **11:26 CET:** Scoped dispatcher drop-in applied and verified in
  the live Python process environment; normal `solinteg.env` and
  installed dispatcher checksum remain byte-identical.
- Physical command `charge 1000 W` accepted in the ordinary
  `control_actions` database. Actual inverter observations after
  the mode settled showed **−1024 W** battery power, not merely a
  setpoint readback. The car continued at EVCC **Ict2 6.0 A**,
  pilot raw 100, while L3 imported approximately **1451 W**.
- The independent systemd timer ran successful `running` checks,
  original watchdogs remained active and root EV gate stayed
  `mode=shadow`.
- **Hard deadline:** 13:26 CET. The observed completion/abort state
  must be read from the private status JSON; it was **not yet
  reached** at initial verification.

Automated restoration behavior and the reason for using a
dispatcher-process-scoped override are recorded in
[ADR 2026-10-31](architecture/adr-2026-10-31-temporary-soc25-recovery.md).

No private credentials, private IPs or user-specific tariffs are
included in this public document. An actual deployment outcome is
recorded here separately from the implementation and should never
be inferred merely from a successful dry-run test.
