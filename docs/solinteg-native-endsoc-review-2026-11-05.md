# Native On-grid EndSOC at 20% – documentary verification (2026-11-05)

**Status: INCOMPLETE / NO-GO for register writes or claiming a physical 20% floor.**
Investigation only. No new device reads/writes, config changes, systemd
changes or broker deployment were performed.

## Verified documentary evidence

1. Solinteg's *Inverter Configurations* OpenAPI lists
   `gridConnectedSocProtection` (0/1) and
   `gridConnectedDischargeDepth` (10.0–90.0%). A 20% setting is within
   the published range. It does **not** say that this constraint overrides
   `EMS Battery dispatch (3#3)` or remains effective during a stale
   externally commanded battery discharge.
   https://apidocs-en.solinteg-cloud.com/en/docs/appendix/inverter-configurations/
2. Solinteg's *EMS Mode* OpenAPI says 3#3 directly commands battery
   charge/discharge power and requires stable connectivity; the
   recommended control interval is 5–10s. It does not specify whether
   native cut-off takes priority over 3#3 setpoints.
   https://apidocs-en.solinteg-cloud.com/en/docs/solutions/ems-ems-mode/
3. The manufacturer *Hybrid Inverter MODBUS RTU Protocol* 00.02
   (2022-12-06), register table section 3, identifies `52502` as
   On-grid SOC Protection, `52503` as On-grid Battery End SOC,
   and `50207` as EMS battery setpoint. Appendix 2 describes special
   monitoring/communication behaviour when BMS/meter are externally
   attached to EMS; it does NOT prove a local TCP EMS watchdog/lease.
   https://www.memodo.pl/mwdownloads/512e63211e205651872bde03f72478fe
4. **Encoding discrepancy:** the older protocol table prints factor
   **1000** next to `52503` (and a 0–100% range), whereas this code,
   `MODBUS.md`, and site-specific historical FC03 interpret raw
   `52503=100` as **10% using 0.1% increments**. The older PDF may
   represent a different scale convention, document variant, firmware
   or table error. DO NOT calculate or write a new raw `52503` value
   until the vendor has confirmed the precise encoding for MHT-10K-25,
   installed ARM/DSP and battery firmware.
5. The newer Solinteg MHT-4–12K-25 family includes MHT-10K-25.
   Public MHT-25–50K manuals and cross-brand integrations are NOT
   proof of exact local firmware behaviour.
   https://www.solinteg.com/integ-m/1986370686400937984

## Installation evidence (historical, not re-read today)

- FC03 read on 2026-11-02: `52502=1`, `52503=100`;
  local reporting interpreted 10% at that time. This is NOT
  a physical test of the cut-off under EMS.
- Current economic planner reserve was documented as 20%, software
  floor 12%, and recovery around 13→16% after the 2026-11-02
  economic-policy revision. These are independent of native EndSOC.
- Reference MHT inverter once retained a charge target for at least
  16 minutes without update; the test did not establish that native
  EndSOC constrains stale forced **discharge** on this MHT-10K-25.
- During broker transaction migration, `52502/52503` remain excluded
  from production FC06 allowlists. No code change to this policy is
  proposed.

## Decision alternatives

| Alternative | What would be gained | Unverified downside |
| --- | --- | --- |
| Native EndSOC 20% plus General | Potential passive hardware-resident minimum and normal PV capture | Might block intended economic reserve use; EMS precedence and restart behaviour unknown |
| EMS open-cap 0 W hold | Potentially avoids 50 W standby drift | Can block PV charging; host failure may latch 0 W; still unqualified on installed hardware |
| Existing 50 W EMS save + economic reserve | Currently deployed, avoids immediate new register risk | Measured battery still discharges substantially; not a 20% physical stop |
| Price-weighted top-up before 20% | Reduces chance of deep SoC | Buys some grid energy, does not create physical hold |

**Recommendation:** do not substitute native EndSOC for a hard EMS
protection until Solinteg confirms the priority/encoding on this
firmware. The best future architecture may be native EndSOC=20%
as independently implemented safeguard + existing economic planner
for actual price decisions, without relying on 0 W hold. But whether
native EndSOC preserves PV charging and forced charge below threshold
must be confirmed. No automatic grid charging is implied by setting
a discharge cut-off; it is not a top-up controller.

## Required questions / acceptance gates

1. Obtain MHT-10K-25 exact ARM, DSP, datalogger and BMS firmware via
   OLED `General Settings → System Info → FM Version` or authenticated
   read-only Solinteg configuration API. Keep identifiers private.
2. Vendor must specify precisely the raw scaling/range of local
   `52503` and whether the published 1000 factor maps to
   0.1% or 0.01% for our firmware. Verify `52502/52503` read-only.
3. Vendor must state whether cut-off is enforced under
   `50000=0x0303`, including positive `50207` held indefinitely
   after Modbus loss. Is inverter autonomous, and how does BMS intervene?
4. Ask if raising to 20% changes permitted grid charging, solar capture,
   forced charging, SOC hysteresis and resume, or backup/EPS behaviour.
5. Only after confirmation, plan a **supervised non-deep-discharge**
   physical verification with protected rollback and observed measured
   battery/grid/solar power. Never intentionally force SoC below the
   inverter's current 10% protection to test a failure.

## Source / deployment reconciliation

See `docs/solinteg-vendor-ems-questions-2026-11-03.md`,
`docs/architecture/adr-2026-11-02-ems-general-failsafe.md`,
`docs/solinteg-broker-hold-probe-2026-11-05.md`, and `MODBUS.md`.
The original documentation and the live deployment can differ.
Source changes are NOT production deployment. The prior broker probe
adaptation is still an uncommitted, uninstalled code candidate.
