#!/usr/bin/env python3
"""
One-shot on-device probe: does the site import limit (50007/50009) actually cap what the
UTILITY METER imports on THIS unit+firmware?

Why it matters. During a forced charge the meter sees `battery charge power + house load`.
Nothing in the control path bounds that sum in real time: `clamp_power_w` bounds the battery's
own power, and the optimizer's grid-cap feasibility check bounds the PLANNED sum against a
FORECAST load — a load spike above forecast lands on top of the commanded charge. 50208/50209 are
no help (they cap the inverter's own AC exchange, which excludes house load entirely — MODBUS.md).
So 50007/50009 are the only fuse-level guard available, and they are the one pair of control
registers in this repo taken from a third-party doc rather than measured here.

The failure mode to look for is NOT a refused write. It is "stored but not enforced" — exactly
what 50209=0 turned out to do when written before the mode change (MODBUS.md). So this probe reads
the registers back AND measures real power, because those are different bugs with different fixes.

METHOD — force a grid-funded charge, then squeeze the limit below what that charge needs and watch
the meter:

  Phase 0  baseline    observe, no writes (records the resting house load)
  Phase A  unlimited   charge at PROBE_W with no import limit -> the meter should show roughly
                       PROBE_W + house load - PV
  Phase B  in-mode     write 50009 = LIMIT_W, 50007 = 1 while the charge is still running.
                       If enforced, meter import falls to ~LIMIT_W and battery power drops to
                       make room for the house.
  Phase C  pre-mode    back to auto, write the cap while in General (0x101), THEN force the
                       charge. This is the order dispatch_loop.apply_import_limit() uses at
                       service startup, and it is the order that makes 50208/50209 silently
                       inert (MODBUS.md's write-order rule). If these registers behaved the
                       same way, the startup write would be decorative.
  restore              50007 = 0, 50207 = 0, original work mode

Reading the result:
  B and C both ~= LIMIT_W          -> ENFORCED in both orders. Set DISPATCH_IMPORT_LIMIT_W.
  B enforced, C not                -> the startup write is inert; apply_import_limit() would have
                                      to be moved inside an EMS session to be worth anything.
  B meter import ~= LIMIT_W        -> ENFORCED. Set DISPATCH_IMPORT_LIMIT_W to your fuse rating.
  B looks like A, readback correct -> stored but NOT enforced. Do NOT rely on it; the software
                                      bounds are all you have, so keep SOLINTEG_GRID_CAP_W
                                      conservative instead.
  readback wrong / write error     -> wrong address or scale on this firmware. Stop here.

SAFETY. Requires SOLINTEG_CONTROL_ARMED=1 (otherwise every write is a no-op and the probe can
only report the baseline) and SOLINTEG_50207_SIGN. It commands a real charge, so run it when the
house is quiet and the battery has headroom. LIMIT_W is deliberately chosen ABOVE the resting
house load, so a working limit throttles the charge rather than starving the house. The restore
phase runs from a finally block, and inverter_control's own atexit/signal fail-safe reverts the
work mode if this script is killed.

Usage:
  SOLINTEG_CONTROL_ARMED=1 python3 scripts/tools/probe_import_limit.py [probe_w] [limit_w]
  (defaults: probe 3000 W charge, limit 1500 W)
"""
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))

import inverter_control as ic  # noqa: E402

log = logging.getLogger("probe.import_limit")

# Read registers, same ones modbus_poller.py uses. Named rather than inline so the precondition
# arithmetic below is readable: house load is a DERIVED quantity (inverter AC output minus the
# meter), not a register.
REG_METER = 11000        # S32, +ve = export, -ve = import
REG_INVERTER_AC = 11016  # S32, inverter's own AC output
REG_PV = 11028           # PV production

SETTLE_S = 25  # a full modbus_poller cycle plus margin, so the inverter has really reacted
SAMPLES = 6
SAMPLE_GAP_S = 5
# The probe runs three observation phases, so it needs the battery to keep accepting charge for
# several minutes. At 99% SoC a 3 kW charge fills in about three minutes and the inverter stops —
# every phase then reads ~0 import and the verdict below would be drawn from nothing.
MIN_HEADROOM_KWH = 2.0
# Phase A must produce real grid import, or there is nothing for a cap to cap. If PV happens to fund
# the whole charge, or the battery filled, import is ~0 in every phase and "import stayed under the
# limit" is true for reasons that have nothing to do with the limit.
MIN_PHASE_A_IMPORT_FACTOR = 2.0


def read_sample(inv: ic.Inverter) -> dict:
    """One observation of everything that separates 'enforced' from 'stored but ignored'."""
    return {
        "meter_w": inv.read_s32(REG_METER),
        "inverter_ac_w": inv.read_s32(REG_INVERTER_AC),
        "pv_w": inv.read_s32(REG_PV),
        "battery_w": inv.battery_power_w(),
        "soc_pct": inv.soc_pct(),
        "limit_raw": inv.read_u16(ic.REG_IMPORT_LIMIT),
        "limit_on": inv.read_u16(ic.REG_IMPORT_LIMIT_ENABLE),
        "mode": inv.work_mode(),
    }


def observe(inv: ic.Inverter, phase: str) -> list[dict]:
    rows = []
    for i in range(SAMPLES):
        row = read_sample(inv)
        rows.append(row)
        log.info(
            "%s [%d/%d] meter %+6d W  inverter_ac %+6d W  battery %+6d W  SoC %5.2f%%  "
            "50009=%d 50007=%d mode=0x%X",
            phase, i + 1, SAMPLES, row["meter_w"], row["inverter_ac_w"], row["battery_w"],
            row["soc_pct"], row["limit_raw"], row["limit_on"], row["mode"],
        )
        if i < SAMPLES - 1:
            time.sleep(SAMPLE_GAP_S)
    return rows


def mean_import_w(rows: list[dict]) -> float:
    """Mean METER import in watts (positive = importing). Export samples count as 0 import."""
    return sum(max(0.0, -r["meter_w"]) for r in rows) / len(rows)


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    probe_w = int(argv[1]) if len(argv) > 1 else 3000
    limit_w = int(argv[2]) if len(argv) > 2 else 1500

    if not ic.ARMED:
        log.warning("DISARMED — every write below is a no-op. Baseline only; set "
                    "SOLINTEG_CONTROL_ARMED=1 for a real probe.")

    inv = ic.Inverter()
    ic._install_failsafe(inv)
    original_mode = inv.work_mode()
    log.info("probe start: charge %d W, limit %d W, original mode 0x%X", probe_w, limit_w, original_mode)

    # ── Preconditions. Each one, unmet, makes the verdict meaningless rather than merely noisy. ──
    pre = read_sample(inv)
    battery_kwh = float(os.environ.get("SOLINTEG_BATTERY_KWH", "25.6"))
    headroom = (100.0 - pre["soc_pct"]) / 100.0 * battery_kwh
    if headroom < MIN_HEADROOM_KWH:
        log.error("SoC %.1f%% leaves only %.2f kWh of headroom (need %.1f). A forced charge would fill "
                  "the battery mid-probe and every phase would read ~0 import. Run this when the "
                  "battery has been drawn down — after the evening peak, or before sunrise.",
                  pre["soc_pct"], headroom, MIN_HEADROOM_KWH)
        return 2
    house_w = pre["inverter_ac_w"] - pre["meter_w"]  # poller's own derivation
    surplus = max(0, pre["pv_w"] - house_w)
    if probe_w <= surplus:
        log.error("live PV surplus is ~%d W and the probe charge is %d W, so the charge would be "
                  "solar-funded and draw nothing from the grid. Raise the charge above the surplus, "
                  "or run this after sunset.", surplus, probe_w)
        return 2
    log.info("preconditions OK: SoC %.1f%% (%.2f kWh headroom), pv %d W, house %d W, surplus ~%d W",
             pre["soc_pct"], headroom, pre["pv_w"], house_w, surplus)

    try:
        baseline = observe(inv, "phase0-baseline")

        ic.force_charge(inv, probe_w)
        time.sleep(SETTLE_S)
        unlimited = observe(inv, "phaseA-unlimited")

        ic.set_import_limit(inv, limit_w)
        time.sleep(SETTLE_S)
        limited = observe(inv, "phaseB-limited")

        # Phase C: the startup order. Clear everything, write the cap in General, THEN charge.
        log.info("phase C: cap written PRE-MODE (in General), the order apply_import_limit() uses")
        ic.clear_import_limit(inv)
        ic.return_to_auto(inv)
        time.sleep(5)
        ic.set_import_limit(inv, limit_w)
        ic.force_charge(inv, probe_w)
        time.sleep(SETTLE_S)
        pre_mode = observe(inv, "phaseC-pre-mode")
    finally:
        log.info("restoring: import limit off, target 0, mode 0x%X", original_mode)
        try:
            ic.clear_import_limit(inv)
            ic.return_to_auto(inv)
            if original_mode != ic.WORK_MODE_GENERAL:
                inv.write_u16(ic.REG_WORK_MODE, original_mode)
        except Exception as exc:  # noqa: BLE001
            log.error("restore failed (%s) — check the inverter manually", exc)

    a, b = mean_import_w(unlimited), mean_import_w(limited)
    c = mean_import_w(pre_mode)
    log.info("RESULT  baseline %.0f W | unlimited %.0f W | in-mode %.0f W | pre-mode %.0f W "
             "(cap asked: %d W)", mean_import_w(baseline), a, b, c, limit_w)
    # Phase A is the control. If it did not import meaningfully more than the cap, then "import
    # stayed under the cap in phase B" is not evidence about the cap — the battery filled, the sun
    # covered it, or the house stopped drawing.
    if a < limit_w * MIN_PHASE_A_IMPORT_FACTOR:
        log.warning("VERDICT: INCONCLUSIVE — the uncapped control phase only imported ~%.0f W against "
                    "a %d W cap, so phase B proves nothing about enforcement. Most likely the battery "
                    "filled or PV funded the charge. Re-run with more headroom, a higher --probe_w, or "
                    "after sunset.", a, limit_w)
        return 3
    readback_ok = limited[-1]["limit_raw"] == int(round(limit_w / 100)) and limited[-1]["limit_on"] == 1
    if not readback_ok:
        log.error("VERDICT: registers did not read back as written (50009=%d, 50007=%d) — wrong "
                  "address or scale on this firmware. Do NOT set DISPATCH_IMPORT_LIMIT_W.",
                  limited[-1]["limit_raw"], limited[-1]["limit_on"])
        return 1
    if b <= limit_w * 1.25 and c <= limit_w * 1.25:
        log.info("VERDICT: ENFORCED in both write orders — in-mode %.0f W, pre-mode %.0f W against a "
                 "%d W cap. These are site-level registers, not EMS-scoped, so apply_import_limit()'s "
                 "startup write is sound. Set DISPATCH_IMPORT_LIMIT_W to your main-fuse rating and "
                 "record it in MODBUS.md.", b, c, limit_w)
        return 0
    if b <= limit_w * 1.25:
        log.warning("VERDICT: enforced IN-MODE only (%.0f W), not pre-mode (%.0f W). The cap works, "
                    "but dispatch_loop.apply_import_limit() writes it at startup in General, which on "
                    "this unit is stored and ignored — same shape as the 50208/50209 write-order rule. "
                    "Do NOT set DISPATCH_IMPORT_LIMIT_W until that write is moved inside an EMS "
                    "session.", b, c)
        return 1
    log.warning("VERDICT: stored but NOT enforced — import stayed at ~%.0f W against a %d W cap. "
                "Leave DISPATCH_IMPORT_LIMIT_W unset and keep SOLINTEG_GRID_CAP_W conservative; "
                "the planner's own grid-cap check is then the only guard.", b, limit_w)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
