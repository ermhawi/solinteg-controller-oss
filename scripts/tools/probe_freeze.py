#!/usr/bin/env python3
"""
One-shot on-device probe: does the documented battery FREEZE actually hold the battery on THIS
unit+firmware, and does the write order matter the way MODBUS.md says?

The freeze is `50207 = 0` + `50208 = 0` + `50209 = 0` in EMS BattCtrl (`50000 = 0x303`) —
"battery untouched, grid supplies the house". It is the deliberate version of a hold, as opposed
to Auto (which discharges at whatever the house draws) and to the ~220 W battery-save emulation
(which still moves ~0.055 kWh/slot out of the battery at the CURRENT price, ~2.6 kWh over a 12 h
overnight hold, when the whole point is to keep it for a dearer one).

WHY THIS PROBE EXISTS. `50208 = 0` has never been written on the reference deployment — the earlier
probe deliberately left the output side open to keep the blast radius small — and the three-register
combination has never been verified as a physical freeze. More importantly, MODBUS.md records a
confirmed result that makes the obvious implementation wrong:

    A restrictive cap written BEFORE mode entry is STORED BUT NEVER ENFORCED. `50209 = 0` written
    pre-mode produced a full 6 kW grid-funded charge, identical to the uncapped control, *while the
    register read back as 0*.

So for a freeze the readback is not evidence, and the failure shape is the worst possible: it reads
back as frozen, is not frozen, and leaves the inverter in the EMS-with-open-caps state MODBUS.md
says never to enter. This probe therefore measures BATTERY POWER (30258) and the SoC trend, and
tests both write orders so the rule is confirmed on your unit rather than assumed.

METHOD:
  Phase 0  baseline    Auto, observe. Records how much the battery is moving on its own — a freeze
                       is only demonstrable if there is something to stop.
  Phase A  pre-mode    caps written BEFORE 0x303 (the WRONG order, the one MODBUS.md predicts is
                       inert). Expect: registers read back 0, battery keeps moving.
  Phase B  in-mode     0x303 first, confirmed, THEN caps (the order inverter_control.freeze_battery
                       uses). Expect: battery power falls to ~0 and SoC stops drifting.
  restore              caps reopened while still in 0x303, then General.

Reading the result:
  B holds, A does not   -> the write-order rule is confirmed here, and freeze_battery is correct.
                           Consider DISPATCH_FREEZE_HOLDS=1.
  Both hold             -> the order does not matter on this firmware. Still keep the in-mode order.
  Neither holds         -> this firmware does not implement the freeze as documented. Leave
                           DISPATCH_FREEZE_HOLDS off; the battery-save emulation is what you have.
  A holds, B does not    -> unexpected; do not ship a freeze on this unit without understanding why.

WHAT THIS PROBE CANNOT ANSWER, and you should not conclude it has:
  * Whether the inverter's own on-grid SoC floor (52502/52503) can still grid-charge with AC input
    blocked. That needs the battery NEAR THE FLOOR, which will not coincide with a convenient probe
    window. Until it is answered, freeze_battery refuses below SOC_FLOOR_PCT + 5.
  * Whether a stale `0` cap is enforced after returning to General. This probe restores the caps
    while still in 0x303, so it never leaves one behind — keep restore_ac_limits regardless.

SAFETY. Requires SOLINTEG_CONTROL_ARMED=1 and SOLINTEG_50207_SIGN. Run it AT NIGHT with no PV:
`50208 = 0` blocks the inverter's AC output, so in daylight this curtails PV instead of
demonstrating a hold. The house draws from the grid throughout, which is the intended behaviour of
a freeze, not a side effect. Restore runs from a finally block, and inverter_control's own
atexit/signal fail-safe reverts the mode if this is killed.

Usage:
  SOLINTEG_CONTROL_ARMED=1 python3 scripts/tools/probe_freeze.py [--settle 30] [--samples 8]
"""
import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))

import inverter_control as ic  # noqa: E402

log = logging.getLogger("probe.freeze")

REG_METER = 11000  # +ve = export, -ve = import (same register the poller reads)
REG_PV = 11028


def sample(inv: ic.Inverter) -> dict:
    """Everything that separates 'enforced' from 'stored': the registers AND the physics."""
    return {
        "battery_w": inv.battery_power_w(),
        "soc_pct": inv.soc_pct(),
        "meter_w": inv.read_s32(REG_METER),
        "pv_w": inv.read_s32(REG_PV),
        "mode": inv.work_mode(),
        "cap_out": inv.read_s16(ic.REG_MAX_AC_OUTPUT),
        "cap_in": inv.read_s16(ic.REG_MAX_AC_INPUT),
        "target": inv.read_s16(ic.REG_BATT_POWER_TARGET),
    }


def observe(inv: ic.Inverter, phase: str, samples: int, gap_s: float) -> list[dict]:
    rows = []
    for i in range(samples):
        row = sample(inv)
        rows.append(row)
        log.info("%-14s [%d/%d] battery %+6d W  SoC %5.2f%%  meter %+6d W  pv %5d W  "
                 "mode=0x%X 50208=%d 50209=%d 50207=%d",
                 phase, i + 1, samples, row["battery_w"], row["soc_pct"], row["meter_w"],
                 row["pv_w"], row["mode"], row["cap_out"], row["cap_in"], row["target"])
        if i < samples - 1:
            time.sleep(gap_s)
    return rows


def verdict_for(rows: list[dict]) -> tuple[float, float]:
    """(mean |battery power| in W, SoC drift in points) over a phase."""
    mean_abs_w = sum(abs(r["battery_w"]) for r in rows) / len(rows)
    return mean_abs_w, rows[-1]["soc_pct"] - rows[0]["soc_pct"]


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--settle", type=float, default=30.0, help="seconds to let each phase settle")
    ap.add_argument("--samples", type=int, default=8)
    ap.add_argument("--gap", type=float, default=5.0, help="seconds between samples")
    args = ap.parse_args(argv[1:])

    if not ic.ARMED:
        log.error("DISARMED — every write below is a no-op, so this probe can only report the "
                  "baseline. Set SOLINTEG_CONTROL_ARMED=1 for a real probe.")
        return 2

    inv = ic.Inverter()
    ic._install_failsafe(inv)
    original_mode = inv.work_mode()
    log.info("probe start: original mode 0x%X, SoC %.1f%%", original_mode, inv.soc_pct())

    first = sample(inv)
    if first["pv_w"] > 100:
        log.error("PV is producing %d W — run this at night. 50208=0 blocks the inverter's AC "
                  "output, so in daylight this curtails PV instead of demonstrating a hold.",
                  first["pv_w"])
        return 2

    try:
        baseline = observe(inv, "0-baseline", args.samples, args.gap)

        # Phase A: the WRONG order on purpose — caps before mode entry.
        log.info("phase A: writing caps BEFORE mode entry (the order MODBUS.md predicts is inert)")
        ic.return_to_auto(inv)
        time.sleep(2)
        inv.write_u16(ic.REG_MAX_AC_OUTPUT, 0)
        inv.write_u16(ic.REG_MAX_AC_INPUT, 0)
        inv.write_u16(ic.REG_BATT_POWER_TARGET, 0, verify=False)
        time.sleep(0.3)
        inv.write_u16(ic.REG_WORK_MODE, ic.WORK_MODE_EMS_BATTCTRL)
        ic._forced_active = True
        time.sleep(args.settle)
        pre_mode = observe(inv, "A-pre-mode", args.samples, args.gap)

        # Phase B: the order freeze_battery uses — mode first, confirmed, then caps.
        log.info("phase B: mode first, confirmed, THEN caps (freeze_battery's own order)")
        ic.restore_ac_limits(inv)
        ic.return_to_auto(inv)
        time.sleep(2)
        ic.freeze_battery(inv, min_soc_pct=0.0)  # floor guard bypassed: the probe is the test
        time.sleep(args.settle)
        in_mode = observe(inv, "B-in-mode", args.samples, args.gap)
    except ic.FreezeNotEnforced as exc:
        log.error("phase B refused to confirm a freeze: %s", exc)
        in_mode = None
    finally:
        log.info("restoring: caps reopened while still in EMS, then mode 0x%X", original_mode)
        try:
            ic.restore_ac_limits(inv)
            ic.return_to_auto(inv)
            if original_mode != ic.WORK_MODE_GENERAL:
                inv.write_u16(ic.REG_WORK_MODE, original_mode)
        except Exception as exc:  # noqa: BLE001
            log.error("restore failed (%s) — CHECK THE INVERTER MANUALLY: a zeroed AC cap left "
                      "behind would cripple self-consumption", exc)

    base_w, base_drift = verdict_for(baseline)
    a_w, a_drift = verdict_for(pre_mode)
    log.info("RESULT  baseline |battery| %.0f W (SoC %+.2f pts) | pre-mode %.0f W (SoC %+.2f pts)",
             base_w, base_drift, a_w, a_drift)
    if in_mode is None:
        log.warning("VERDICT: freeze_battery itself refused to confirm a hold. This firmware does "
                    "not implement the documented freeze — leave DISPATCH_FREEZE_HOLDS off.")
        return 1
    b_w, b_drift = verdict_for(in_mode)
    log.info("        in-mode %.0f W (SoC %+.2f pts)", b_w, b_drift)

    held_a = a_w <= ic.FREEZE_IDLE_TOL_W
    held_b = b_w <= ic.FREEZE_IDLE_TOL_W
    if held_b and not held_a:
        log.info("VERDICT: write-order rule CONFIRMED on this unit — in-mode holds (%.0f W), "
                 "pre-mode does not (%.0f W). freeze_battery's order is right; you may set "
                 "DISPATCH_FREEZE_HOLDS=1. Record this in MODBUS.md.", b_w, a_w)
        return 0
    if held_b and held_a:
        log.info("VERDICT: both orders hold — the ordering does not matter on this firmware. Keep "
                 "the in-mode order anyway (it is correct on the unit that was probed) and you may "
                 "set DISPATCH_FREEZE_HOLDS=1.")
        return 0
    if not held_b:
        log.warning("VERDICT: the freeze does NOT hold the battery on this unit (in-mode %.0f W). "
                    "Leave DISPATCH_FREEZE_HOLDS off; the battery-save emulation is what you have.",
                    b_w)
        return 1
    log.warning("VERDICT: unexpected — pre-mode held and in-mode did not. Do not ship a freeze on "
                "this unit without understanding why.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
