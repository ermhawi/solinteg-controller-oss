# Read-only EV rollout on Dell, 2026-10-28

## Scope and state

This is a **live observation deployment**, not a charging controller.
It does not touch /opt/solinteg/app, Solinteg's active dispatch,
the live systemd environment or its web application.

Four independently enabled systemd units:

| Unit | Reads | Writes |
| --- | --- | --- |
| solinteg-ev-ha | Internal HA Energy State API | Its own JSON snapshots |
| solinteg-ev-observer | Waveshare (private LAN address; example `192.168.0.20:4196`), Modbus ASCII **FC03 only** | Its own EVCC JSON snapshot |
| solinteg-ev-shadow | Existing optimizer_runs price rows, live.json, and above snapshots | Its own advisory plan JSON |
| solinteg-ev-history | Local HA, EVCC, plan and inverter JSON snapshots | Private 30-day SQLite history |

All continuously running observation services have **no physical write path**.
Their planned physical charging permission remains *always 0 A*.
One separate, explicitly user-approved **manual** FC10 decrease to 6 A
was performed on 2026-10-28 and verified by register/pilot readback;
it was not issued by any sidecar. The old solinteg-evcc.service,
which contains optional manual writing functionality, was **not installed**.

## Isolated runtime

- Directory: /opt/solinteg/ev-shadow-runtime
- Account: solinteg-evshadow, with group solinteg (necessary to read
  the existing price SQLite DB and live.json).
- Code: root-owned, read-only for the worker. The bundle contains
  only common.py, ev_price_policy.py, ev_solar_policy.py,
  ha_ev_session.py, ev_shadow_planner.py, evcc_observer.py and
  ev_history.py. No dispatch or inverter-write modules are deployed here.
- Files: the worker can write only to its own var/ directory.
  The historian stores synchronized 30-second samples in
  `var/ev-history.sqlite` (mode 0600), with 30-day retention.
  Stale or failed EVCC measurements are recorded as **NULL**, not 0 A.
- Configuration: standalone ev-shadow.env. NEVER source
  /opt/solinteg/solinteg.env in these units; the live house controller
  has SOLINTEG_CONTROL_ARMED=1.
- HA token: var/ha.token, owner shadow user, mode 0400.
  It was copied from the existing private ha-kube/.env without printing
  the value. The *code* makes GET-only calls; a Home Assistant
  long-lived token is not inherently a permission-scoped read-only token.
  It must remain protected and not appear in repo, journal or URL.
- Device protections: NoNewPrivileges, no capabilities, ProtectSystem
  strict, ProtectHome, PrivateDevices, restricted WritePaths, network
  deny by default. The HA reader only permits the ClusterIP and its
  host-networked backend on the Dell LAN; the FC03 observer only
  permits the Waveshare IP; the price planner has no network access.
  Network rules are defense in depth, NOT an independent safety fuse.

The private deployment uses an HA ClusterIP and a separate host-networked
backend address. Both must be explicitly whitelisted for the HA reader;
allowing the ClusterIP alone failed due to DNAT. The public source templates
use **synthetic example addresses** (`10.0.0.20`, `192.168.0.10` and
`192.168.0.20` for the Waveshare). Real endpoints are kept only in the
private deployment configuration. Any endpoint change requires a reviewed,
fail-closed unit update.

## Deliberately uncalibrated scenario

The private runtime enables an **illustrative** economic scenario
with explicitly assumed usable capacity 81.608 kWh and AC efficiency
0.90. These are neither calibrated charging measurements nor a
verified battery capacity specification. Session output contains
energy_estimate_unverified=true and physical_control_eligible=false.
The strict EV_SESSION_PATH is always invalid, even with a fresh API
result. This prevents a shadow API poll being mistaken for a BMS
measurement.

Initial settings are deadline mode, target 80%, next 07:00 Swedish
local departure. Economy mode can be inspected in separate dry-run
scenarios. The public Next.js /ev page and its settings-write API have
NOT been deployed into the active Solinteg web app.

## Observed first run

As of 2026-10-28 approximately 14:14 UTC:

- Volvo API observation: 63% battery, 80% target, connected; OEM
  last-changed metadata not equivalent to a physical sample.
- Waveshare FC03: EVCC B2, configured raw PWM 267, pilot raw 267,
  0.3 A total internal channel measurement. This is essentially idle;
  pilot capacity ~=16 A is not evidence of 16 A charging.
- Shared house purchase price: about 128.92 ore/kWh at the time.
- Deadline mode shadow recommended 0 A now (cheaper hours later).
- Expected charging energy was ~15.415 kWh *only under the above
  explicit hypothetical capacity/loss assumptions*.
- Phase protection verified: false. Charging physical permission: 0 A.
  All hardware write counts: 0.
- Existing web/dispatch/poller MainPIDs remained unchanged through
  the rollout. No Modbus FC10, HA POST or inverter write was sent.

## Manual 6 A cap, bounded FC03 reads, and history

At **2026-10-28T15:25:31Z**, following the user's explicit approval,
we stopped only `solinteg-ev-observer` while performing one manual
`FC10` write to `0x0014`. A preflight FC03 read showed state `A1`,
`configured_raw=267`, `pilot_raw=267` and unknown actual current
(the charger was not reporting an active current). The single manual
write changed the register to **100** (6 A); its ACK and a fresh pair
of FC03 reads verified `configured_raw=100`, `pilot_raw=100`.
The result is saved privately at
`var/evcc-manual-limit-20261028.json`. **Do not automatically restore
267/16 A.** The user's Volvo-side 6 A limit was not changed.

The first continuous observer version sometimes timed out during its
*first* Modbus FC03 read, whereas the existing manual EVCC reader
succeeded on its second bounded read attempt. We gave the standalone
observer the same **maximum of two FC03 read attempts per register
window**, each on a fresh TCP socket. **FC10 is not present in the
observer.** The updated observer was installed separately and reported
state `A1`, configured and pilot PWM **100/100**, read_ok=true, writes=0.

The independent `solinteg-ev-history` service records each 30-second
UTC-aligned observation in SQLite. Captured signals include raw
`meter_l3_w`, `grid_w`, `battery_w`, house SoC, Volvo last-change and
API-fetch times, EVCC status/actual current/PWM, and shadow-plan
recommendations. Solinteg meter signs are **negative = grid import**;
battery_w **positive = battery discharging**. This is useful for
correlation but does not prove causal attribution during other changing
house loads. The history field `evcc_6a_limit_verified=1` requires
successful, *fresh* FC03 readback of both PWM values 100; unknown reads
are `NULL`, not a verified 6 A limit.

For a supervised manual charge test we should record an interval of
at least 10 minutes in the historian after confirming Volvo's own
6 A setting and 80 % target, monitor L3, and report when actual EVCC
current increases. Do not start charging via Waveshare or alter the
inverter without separate approval.

## Observation and recovery

Read the snapshots as the isolated account (no token values printed):

    sudo -n -u solinteg-evshadow python3 -c 'import json;from pathlib import Path;p=Path("/opt/solinteg/ev-shadow-runtime/var");print(json.dumps({n:json.loads((p/n).read_text()) for n in ("status.json","shadow-plan.json","evcc-readonly.json")},indent=2))'

Inspect the most recent historical samples (read-only; does not touch
the live inverter telemetry database):

    sudo -n -u solinteg-evshadow python3 -c 'import sqlite3; db="file:/opt/solinteg/ev-shadow-runtime/var/ev-history.sqlite?mode=ro"; c=sqlite3.connect(db,uri=True); print(*c.execute("SELECT captured_at_utc,l3_grid_w,grid_w,battery_w,evcc_read_ok,evcc_measured_a,evcc_6a_limit_verified,volvo_soc_pct,charge_mode,suggested_current_a FROM ev_samples ORDER BY id DESC LIMIT 10"),sep="\n")'

Only the EVCC's **internal channel current** is captured: the separate
Carlo Gavazzi EM111 energy meter is not yet integrated into this series.
The inverter's L3 column stores **watts**, not independently measured
RMS phase current. We should not claim this alone enforces the 20 A fuse.

**Later 2026-10-28 field observation:** Waveshare connectivity returned
and the user started charging. FC03 then showed `C2` and **Ict2=6,0 A**
with `PWM=100/100` (6 A), while Ict1/Ict3 measured about 0,1 A each.
The observer's `measured_current_a=6,2` is the **three-channel sum**,
not the active phase's 6,0 A. The separate, deployed read-only
`ev_ac_capture.py` now logs Ict2 and integrates only adjacent,
verified `C2` observations no more than 45 s apart, using explicitly
**assumed 230 V / power factor 1.0**. It does **not** depend on EM111,
alter the EVCC's 6 A limit, supply RMS fuse protection, or create
billable cost. Details in `docs/ev-cost-ledger.md`.

Check service liveness without restarting the house services:

    systemctl is-active solinteg-ev-ha solinteg-ev-shadow solinteg-ev-observer solinteg-ev-history
    journalctl -u solinteg-ev-ha -u solinteg-ev-shadow -u solinteg-ev-observer -u solinteg-ev-history --since "10 minutes ago" --no-pager

If observations cause trouble, undo ONLY the new sidecars:

    sudo systemctl disable --now solinteg-ev-history.service solinteg-ev-shadow.service solinteg-ev-ha.service solinteg-ev-observer.service

Do NOT stop solinteg-dispatch, solinteg-poller, solinteg-web or
clear any Solinteg battery recovery state. Stopping readers does not
alter the EVCC pilot or the car's existing charging schedule. The
sidecar files can remain for diagnostics; do not delete the HA token
or any other files automatically.

## Remaining gates before physical charging

1. Verify actual EVCC pause (0x03E8) and resumption to at least 6 A
   under separately approved supervision, physical meter/readback,
   independent stop and rollback.
2. Independent 20 A L3 RMS phase protection, communications-loss
   fail-safe (EVCC upstream timeout was previously disabled), and
   calibrated car/battery/grid-power attribution.
3. Charge-target and energy calibration using EVSE meter plus Volvo
   battery percentage; time from Volvo last-changed is NOT BMS
   measurement freshness.
4. Proper coordinated house-battery+vehicle optimization.
5. Only then review any limited, separately approved physical pilot.
