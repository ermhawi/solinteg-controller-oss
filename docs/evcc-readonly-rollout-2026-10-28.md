# Read-only EV rollout on Dell, 2026-10-28

## Scope and state

This is a **live observation deployment**, not a charging controller.
It does not touch /opt/solinteg/app, Solinteg's active dispatch,
the live systemd environment or its web application.

Three independently enabled systemd units:

| Unit | Reads | Writes |
| --- | --- | --- |
| solinteg-ev-ha | Internal HA Energy State API | Its own JSON snapshots |
| solinteg-ev-observer | Waveshare 192.168.0.20:4196, Modbus ASCII **FC03 only** | Its own EVCC JSON snapshot |
| solinteg-ev-shadow | Existing optimizer_runs price rows, live.json, and above snapshots | Its own advisory plan JSON |

All devices and inverter registers are **read-only**. The physical
charging permission in the plan is *always 0 A*. The old
solinteg-evcc.service, which has gated manual writing functionality,
was **not installed**.

## Isolated runtime

- Directory: /opt/solinteg/ev-shadow-runtime
- Account: solinteg-evshadow, with group solinteg (necessary to read
  the existing price SQLite DB and live.json).
- Code: root-owned, read-only for the worker. The bundle contains
  only common.py, ev_price_policy.py, ev_solar_policy.py,
  ha_ev_session.py, ev_shadow_planner.py and evcc_observer.py.
  No dispatch or inverter-write modules are deployed here.
- Files: the worker can write only to its own var/ directory.
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

The private HA service currently resolves to 10.0.0.20, but its
Kubernetes host-networked endpoint is 192.168.0.10. Both addresses
are whitelisted for the HA reader; with only the ClusterIP whitelisted,
systemd's egress restriction caused a URLError due to DNAT. If either
endpoint changes, the reader fails closed and requires a reviewed unit
update. Waveshare is currently at 192.168.0.20:4196.

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

## Observation and recovery

Read the snapshots as the isolated account (no token values printed):

    sudo -n -u solinteg-evshadow python3 -c 'import json;from pathlib import Path;p=Path("/opt/solinteg/ev-shadow-runtime/var");print(json.dumps({n:json.loads((p/n).read_text()) for n in ("status.json","shadow-plan.json","evcc-readonly.json")},indent=2))'

Check service liveness without restarting the house services:

    systemctl is-active solinteg-ev-ha solinteg-ev-shadow solinteg-ev-observer
    journalctl -u solinteg-ev-ha -u solinteg-ev-shadow -u solinteg-ev-observer --since "10 minutes ago" --no-pager

If observations cause trouble, undo ONLY the new sidecars:

    sudo systemctl disable --now solinteg-ev-shadow.service solinteg-ev-ha.service solinteg-ev-observer.service

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
