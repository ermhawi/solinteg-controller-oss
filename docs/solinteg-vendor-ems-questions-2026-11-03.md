# Solinteg MHT-10K-25: tillverkarfrågor om EMS och autonom nödfallback

**Status 2026-11-03:** Frågebatteri **förberett, inte skickat**.
Ingen firmwareidentifiering eller fjärråtkomst från tillverkaren
är godkänd genom detta dokument.

**Kontakt:** Solinteg uppger `service.eu@solinteg.com` som
serviceadress för Sverige. Källa:
[Solintegs officiella kontaktlista](https://www.solinteg.com/about-solinteg.html).

## Informationsbehov före varje fysisk test

- Exakt hårdvara på plats: **MHT-10K-25**, Sunwoda
  ungefär 15 kWh; ARM-/DSP-/logger-/BMS-versioner **ännu okända**.
- Verifierad FC03 2026-11-02:
  `52502=1` (OnGrid SoC Protection on),
  `52503=100` (10 % EndSOC, 0,1 %-steg).
- Lokal EMS i drift: `50000=0x0303`,
  batteritarget `50207` (+ = discharge, − = charge).
- Applikationens 12 %-arbetsgräns och 20 %-ekonomiska reserv
  är **inte** inverter-/BMS-garantier.
- Installerad direktpoller och dispatcher använder samtidigt
  Modbusdongeln; timeout-/anslutningsfel har även gjort
  `return_to_auto` omöjlig. Kommunikationen skall stabiliseras
  innan automatisk fallback.

## Föreslagna frågor till Solinteg (engelska)

**Subject:** MHT-10K-25 – EMS Battery Control / On-grid EndSOC safety on Modbus loss

1. Does On-grid SoC protection (`52502=1`) with EndSOC
   (`52503=100`, 10 %) limit actual battery discharge
   while `50000=0x0303` is active and the last positive
   `50207` target remains latched following loss of Modbus TCP?
   Please distinguish **General** versus **EMS BattCtrl**.
2. Is there an inverter-side EMS watchdog, TTL, timeout, or
   automatic fallback when no new `50207` is received?
   If so, which model/ARM/DSP firmware versions support it,
   what triggers/refreshes it, and what is the exact physical
   mode/target when it expires? Does `CommsWatchDog`
   specifically apply to local third-party Modbus TCP rather
   than only parallel master/slave communications?
3. What is the **manufacturer-approved safe sequence** for
   neutralizing a previously forced EMS setpoint after
   communication loss or interrupted multi-register FC06
   transaction? Should `50207=0` be applied before
   `50000=0x0101`, and how do `50208/50209/50210`
   and their write order affect the physical result?
   Does zero battery target prevent normal PV charging?
4. Does the built-in BMS independently enforce the actual
   discharge current, SoC and under-voltage limits if a
   `50207` discharge request persists? What read-only
   telemetry or alarm status proves enforcement, and does
   native EndSOC constrain `0x0303`?
5. Is it safe on this hardware/firmware to increase the
   inverter's built-in On-grid EndSOC to the desired
   reserve, and are there grid-charging, warranty or
   BMS side effects? Please specify any prerequisites.
6. The manufacturer *Hybrid Inverter MODBUS RTU Protocol* v00.02
   lists scale **1000** for register `52503`, while our current
   MHT-10K-25 code and historical readback treat `52503=100`
   as 10.0% (0.1% steps). What is the **correct raw encoding,
   scaling and range for exactly our model/ARM/DSP/BMS firmware**?
   We will not write `52503` based on an unverified conversion.
7. If on-grid EndSOC is raised from the current 10% to 20%,
   does this cut off a continuing positive `50207` discharge
   while `50000=0x0303` stays selected, including after
   third-party Modbus commands stop? Does solar charging
   remain possible, and when/how does discharge resume?
8. What is the recommended **read-only** way to obtain
   exact ARM/DSP, datalogger and battery BMS firmware
   versions? We prefer the OLED `FM Version` page
   or the monitoring system's `firmwareVersion` field,
   not undocumented `100xx` Modbus probes that have
   timed out on a related reference inverter.

**Out of scope:** No request for Solinteg to remotely
reconfigure registers, change firmware, restart the
inverter, or take control of the installation.

## Before sending

Add private model/firmware identifiers obtained from OLED
`General Settings → System Info → FM Version` or an
authorized read-only cloud configuration query.
Share serial numbers and BMS details **privately with
the vendor**, never in the public Git repository.
The recipient must explicitly acknowledge model and
firmware-specific behavior; generic information about
an unrelated MHT model is insufficient.

Relevant manufacturer API reference: [Solinteg OpenAPI
device configurations](https://apidocs-en.solinteg-cloud.com/en/docs/appendix/inverter-configurations/)
lists `firmwareVersion` as read-only with ARM/DSP components.
A platform login/token and device serial are required
for the separate read-only API request, but are not
available in this task's supplied credentials.

See [fallback inventory and matrix](solinteg-fallback-readonly-inventory-2026-11-03.md)
and [EMS/General ADR](architecture/adr-2026-11-02-ems-general-failsafe.md).
