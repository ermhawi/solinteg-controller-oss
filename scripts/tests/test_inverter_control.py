"""Tests for scripts/services/inverter_control.py — the module that actually writes to
the inverter's Modbus registers. Uses fakes.FakeModbusClient (no real hardware/network).

Run: python3 -m unittest scripts.tests.test_inverter_control -v   (from the repo root)
     or: python3 -m unittest discover -s scripts/tests
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))

from fakes import install_pymodbus_stub  # noqa: E402

install_pymodbus_stub()

import inverter_control as ic  # noqa: E402


def make_inverter(soc_pct: float = 50.0) -> ic.Inverter:
    inv = ic.Inverter(host="test-host")
    inv.client.regs[ic.REG_SOC] = int(round(soc_pct * 100))
    return inv


def set_already_in_ems_mode(inv: ic.Inverter, priority: int) -> None:
    """Pre-populate registers so _already_set_for(priority) reads True — simulates the
    inverter already being mid-charge/discharge from a prior tick (the fast-path case)."""
    inv.client.regs[ic.REG_MAX_AC_OUTPUT] = ic.GRID_CAP_RAW
    inv.client.regs[ic.REG_MAX_AC_INPUT] = (-ic.GRID_CAP_RAW) & 0xFFFF
    inv.client.regs[ic.REG_PRIORITY] = priority
    inv.client.regs[ic.REG_WORK_MODE] = ic.WORK_MODE_EMS_BATTCTRL


class InverterControlTestCase(unittest.TestCase):
    def test_target_raw_for_action_matches_charge_sign_convention(self):
        with mock.patch.object(ic, "SIGN", "neg_charge"):
            self.assertEqual(ic.target_raw_for_action("charge", 3000), -300)
            self.assertEqual(ic.target_raw_for_action("discharge", 3000), 300)
        with mock.patch.object(ic, "SIGN", "pos_charge"):
            self.assertEqual(ic.target_raw_for_action("charge", 3000), 300)
            self.assertEqual(ic.target_raw_for_action("discharge", 3000), -300)

    def setUp(self):
        self._armed_patch = mock.patch.object(ic, "ARMED", True)
        self._sign_patch = mock.patch.object(ic, "SIGN", "neg_charge")
        self._sleep_patch = mock.patch.object(ic.time, "sleep")  # skip real rate-limit/settle delays
        self._armed_patch.start()
        self._sign_patch.start()
        self._sleep_patch.start()
        ic._forced_active = False

    def tearDown(self):
        mock.patch.stopall()
        ic._forced_active = False


class ForceChargeTests(InverterControlTestCase):
    def test_full_setup_sequence_when_not_already_set(self):
        inv = make_inverter(soc_pct=50.0)
        ic.force_charge(inv, 3000)

        writes = inv.client.write_calls()
        written_addrs = [addr for addr, _ in writes]
        # caps + priority must land before the power target; power before the mode switch
        self.assertLess(written_addrs.index(ic.REG_MAX_AC_INPUT), written_addrs.index(ic.REG_BATT_POWER_TARGET))
        self.assertLess(written_addrs.index(ic.REG_MAX_AC_OUTPUT), written_addrs.index(ic.REG_BATT_POWER_TARGET))
        self.assertLess(written_addrs.index(ic.REG_PRIORITY), written_addrs.index(ic.REG_BATT_POWER_TARGET))
        self.assertLess(written_addrs.index(ic.REG_BATT_POWER_TARGET), written_addrs.index(ic.REG_WORK_MODE))

        self.assertEqual(dict(writes)[ic.REG_PRIORITY], 0)  # PV priority for charging
        self.assertEqual(dict(writes)[ic.REG_WORK_MODE], ic.WORK_MODE_EMS_BATTCTRL)
        # neg_charge sign: charging encodes as a NEGATIVE raw target
        self.assertEqual(inv.client.regs[ic.REG_BATT_POWER_TARGET], (-300) & 0xFFFF)
        self.assertTrue(ic._forced_active)

    def test_fast_path_skips_setup_writes_when_already_set(self):
        inv = make_inverter(soc_pct=50.0)
        set_already_in_ems_mode(inv, priority=0)

        ic.force_charge(inv, 3000)

        writes = inv.client.write_calls()
        written_addrs = {addr for addr, _ in writes}
        # only the power target should be rewritten — everything else was already correct
        self.assertEqual(written_addrs, {ic.REG_BATT_POWER_TARGET})
        self.assertTrue(ic._forced_active)

    def test_zero_power_returns_to_auto_instead_of_forcing(self):
        inv = make_inverter(soc_pct=50.0)
        ic.force_charge(inv, 0)

        writes = dict(inv.client.write_calls())
        self.assertEqual(writes.get(ic.REG_WORK_MODE), ic.WORK_MODE_GENERAL)
        self.assertEqual(writes.get(ic.REG_BATT_POWER_TARGET), 0)
        self.assertFalse(ic._forced_active)

    def test_at_or_above_soc_ceiling_returns_to_auto_instead_of_charging(self):
        inv = make_inverter(soc_pct=ic.SOC_CEILING_PCT)
        ic.force_charge(inv, 3000)

        writes = dict(inv.client.write_calls())
        self.assertEqual(writes.get(ic.REG_WORK_MODE), ic.WORK_MODE_GENERAL)
        self.assertNotIn(ic.REG_PRIORITY, writes)  # never entered the charge setup path
        self.assertFalse(ic._forced_active)

    def test_disarmed_never_touches_the_client(self):
        ic.ARMED = False
        try:
            inv = make_inverter(soc_pct=50.0)
            ic.force_charge(inv, 3000)
            self.assertEqual(inv.client.write_calls(), [])
        finally:
            ic.ARMED = True


class ForceDischargeTests(InverterControlTestCase):
    def test_full_setup_sequence_when_not_already_set(self):
        inv = make_inverter(soc_pct=50.0)
        ic.force_discharge(inv, 2000)

        writes = dict(inv.client.write_calls())
        self.assertEqual(writes[ic.REG_PRIORITY], 1)  # battery priority for discharging
        self.assertEqual(writes[ic.REG_WORK_MODE], ic.WORK_MODE_EMS_BATTCTRL)
        # neg_charge sign: discharge is the OPPOSITE of charge -> POSITIVE raw target
        self.assertEqual(inv.client.regs[ic.REG_BATT_POWER_TARGET], 200)
        self.assertTrue(ic._forced_active)

    def test_fast_path_skips_setup_writes_when_already_set(self):
        inv = make_inverter(soc_pct=50.0)
        set_already_in_ems_mode(inv, priority=1)

        ic.force_discharge(inv, 2000)

        written_addrs = {addr for addr, _ in inv.client.write_calls()}
        self.assertEqual(written_addrs, {ic.REG_BATT_POWER_TARGET})

    def test_at_or_below_soc_floor_recovers_instead_of_returning_to_auto(self):
        inv = make_inverter(soc_pct=ic.SOC_FLOOR_PCT)
        ic.force_discharge(inv, 2000)

        writes = dict(inv.client.write_calls())
        self.assertEqual(writes.get(ic.REG_WORK_MODE), ic.WORK_MODE_EMS_BATTCTRL)
        self.assertEqual(writes.get(ic.REG_BATT_POWER_TARGET),
                         (-ic.SOC_FLOOR_RECOVERY_W // 10) & 0xFFFF)
        self.assertEqual(writes.get(ic.REG_PRIORITY), 0)  # charging
        self.assertTrue(ic._forced_active)


class ReturnToAutoCapRestoreTests(InverterControlTestCase):
    """Pins restore_ac_limits' contract. It is a no-op against today's behaviour (nothing
    ever writes a restrictive 50208/50209), so these tests are what stop a future PV-only
    charge (REG_MAX_AC_INPUT=0) or battery-freeze (both 0) from leaking a restrictive cap
    into the following run of idle slots. See restore_ac_limits' docstring."""

    def test_leaving_a_forced_state_restores_unrestricted_caps_before_the_mode_write(self):
        inv = make_inverter(soc_pct=50.0)
        ic.force_charge(inv, 3000)          # establishes the forced state
        self.assertTrue(ic._forced_active)
        inv.client.calls.clear()            # isolate return_to_auto's own writes

        ic.return_to_auto(inv)

        writes = inv.client.write_calls()
        addrs = [addr for addr, _ in writes]
        self.assertIn(ic.REG_MAX_AC_OUTPUT, addrs)
        self.assertIn(ic.REG_MAX_AC_INPUT, addrs)
        self.assertEqual(dict(writes)[ic.REG_MAX_AC_OUTPUT], ic.GRID_CAP_RAW)
        self.assertEqual(dict(writes)[ic.REG_MAX_AC_INPUT], (-ic.GRID_CAP_RAW) & 0xFFFF)
        # Both caps must land while 50000 is still EMS BattCtrl — the doc's write-order claim
        # is that EMS limit writes outside EMS BattCtrl can be ignored.
        self.assertLess(addrs.index(ic.REG_MAX_AC_OUTPUT), addrs.index(ic.REG_WORK_MODE))
        self.assertLess(addrs.index(ic.REG_MAX_AC_INPUT), addrs.index(ic.REG_WORK_MODE))
        self.assertFalse(ic._forced_active)

    def test_idle_to_idle_does_not_write_caps(self):
        """The common path: no forced state was ever established, so return_to_auto must keep
        its exact previous cost — mode + target only, no cap writes."""
        inv = make_inverter(soc_pct=50.0)
        self.assertFalse(ic._forced_active)

        ic.return_to_auto(inv)

        addrs = [addr for addr, _ in inv.client.write_calls()]
        self.assertNotIn(ic.REG_MAX_AC_OUTPUT, addrs)
        self.assertNotIn(ic.REG_MAX_AC_INPUT, addrs)
        self.assertEqual(addrs, [ic.REG_WORK_MODE, ic.REG_BATT_POWER_TARGET])

    def test_disarmed_leaving_a_forced_state_writes_nothing(self):
        """Preserves dispatch_loop's "never even open a connection when disarmed" — the cap
        restore must go through write_u16's disarmed short-circuit like every other write."""
        inv = make_inverter(soc_pct=50.0)
        ic._forced_active = True
        ic.ARMED = False
        try:
            ic.return_to_auto(inv)
            self.assertEqual(inv.client.write_calls(), [])
        finally:
            ic.ARMED = True


class FreezeBatteryTests(InverterControlTestCase):
    """The real battery freeze (50207/50208/50209 = 0 in EMS BattCtrl).

    The write ORDER is the entire subject. Confirmed on the reference deployment 2026-08-07: a
    restrictive cap written BEFORE mode entry is stored but never enforced — `50209 = 0` written
    pre-mode produced a full 6 kW grid-funded charge, identical to the uncapped control, *while the
    register read back as 0*. For a freeze that is the worst possible failure shape: it reads back as
    frozen, is not frozen, and leaves the inverter in the EMS-with-open-caps state MODBUS.md says
    never to enter. See OPTIMIZER-AUDIT.md E6.
    """

    def freezable(self, soc_pct: float = 50.0, battery_w: int = 0) -> ic.Inverter:
        inv = make_inverter(soc_pct=soc_pct)
        # 30258 is an S32 across two registers, so a negative value needs the full 32-bit two's
        # complement — setting only the low word reads back as a large positive number.
        raw = battery_w & 0xFFFFFFFF
        inv.client.regs[ic.REG_BATTERY_POWER] = (raw >> 16) & 0xFFFF
        inv.client.regs[ic.REG_BATTERY_POWER + 1] = raw & 0xFFFF
        return inv

    def setUp(self):
        # The base fixture arms the module, sets the sign convention, and stubs time.sleep (so the
        # confirmation poll and the 3 s settle cost nothing here). Shadowing it without calling it
        # left every write a disarmed no-op — which is how these tests first "failed".
        super().setUp()

    def test_writes_the_mode_before_the_restrictive_caps(self):
        inv = self.freezable()
        ic.freeze_battery(inv)

        addrs = [addr for addr, _ in inv.client.write_calls()]
        self.assertIn(ic.REG_WORK_MODE, addrs)
        for cap in (ic.REG_MAX_AC_OUTPUT, ic.REG_MAX_AC_INPUT):
            with self.subTest(cap=cap):
                self.assertIn(cap, addrs)
                # THE assertion this class exists for.
                self.assertLess(addrs.index(ic.REG_WORK_MODE), addrs.index(cap))
        # …and the neutral target lands after the caps, so the inverter never runs a stale setpoint
        # with the caps already shut.
        self.assertLess(addrs.index(ic.REG_MAX_AC_INPUT), addrs.index(ic.REG_BATT_POWER_TARGET))

    def test_commands_the_documented_values(self):
        inv = self.freezable()
        ic.freeze_battery(inv)
        writes = dict(inv.client.write_calls())
        self.assertEqual(writes[ic.REG_WORK_MODE], ic.WORK_MODE_EMS_BATTCTRL)
        self.assertEqual(writes[ic.REG_MAX_AC_OUTPUT], 0)
        self.assertEqual(writes[ic.REG_MAX_AC_INPUT], 0)
        self.assertEqual(writes[ic.REG_BATT_POWER_TARGET], 0)
        self.assertEqual(writes[ic.REG_PRIORITY], 1)

    def test_raises_and_reverts_when_the_battery_is_still_moving(self):
        # The registers took the writes and read back correctly, and the battery moved anyway: that
        # is "stored but not enforced", and the readback provably cannot detect it. Only the measured
        # power can.
        inv = self.freezable(battery_w=-900)
        with self.assertRaises(ic.FreezeNotEnforced):
            ic.freeze_battery(inv)
        # Reverted, not left in a half-applied state: General mode restored and caps reopened.
        writes = inv.client.write_calls()
        addrs = [addr for addr, _ in writes]
        self.assertIn(ic.REG_WORK_MODE, addrs)
        self.assertEqual(inv.client.regs[ic.REG_WORK_MODE], ic.WORK_MODE_GENERAL)
        self.assertEqual(inv.client.regs[ic.REG_MAX_AC_OUTPUT], ic.GRID_CAP_RAW)

    def test_a_small_idle_draw_still_counts_as_held(self):
        # The pack idles with a BMS draw; the tolerance is not zero.
        inv = self.freezable(battery_w=-20)
        ic.freeze_battery(inv)  # must not raise

    def test_refuses_near_the_soc_floor_and_returns_to_auto(self):
        # Whether the inverter's own on-grid floor (52502/52503) can still grid-charge with AC input
        # blocked is OPEN (MODBUS.md: the 2026-08-07 probe ran at 42-44% SoC and could not answer
        # it). A freeze near the floor could silently defeat the one protection that does not depend
        # on this software, so it must refuse rather than find out in production.
        inv = self.freezable(soc_pct=ic.SOC_FLOOR_PCT + 1.0)
        ic.freeze_battery(inv)
        writes = dict(inv.client.write_calls())
        self.assertEqual(writes.get(ic.REG_WORK_MODE), ic.WORK_MODE_GENERAL)
        self.assertNotIn(ic.REG_MAX_AC_INPUT, [a for a, _ in inv.client.write_calls()
                                               if a == ic.REG_MAX_AC_INPUT and writes[a] == 0])

    def test_aborts_to_auto_when_ems_mode_is_never_confirmed(self):
        inv = self.freezable()
        # Mode write silently does not stick — the pre-mode-cap hazard in its purest form.
        with mock.patch.object(ic.Inverter, "work_mode", return_value=ic.WORK_MODE_GENERAL), \
             mock.patch.object(ic, "FREEZE_MODE_CONFIRM_TIMEOUT_S", 0.05), \
             mock.patch.object(ic, "FREEZE_MODE_POLL_S", 0.01):
            with self.assertRaises(ic.FreezeNotEnforced):
                ic.freeze_battery(inv)
        # Never wrote a restrictive cap, because they would have been stored and not enforced.
        self.assertEqual(
            [addr for addr, val in inv.client.write_calls()
             if addr in (ic.REG_MAX_AC_OUTPUT, ic.REG_MAX_AC_INPUT) and val == 0],
            [],
        )

    def test_writes_nothing_when_disarmed(self):
        inv = self.freezable()
        with mock.patch.object(ic, "ARMED", False):
            ic.freeze_battery(inv)
        self.assertEqual(inv.client.write_calls(), [])


class ImportLimitTests(InverterControlTestCase):
    """Site-level (utility-meter) import limit, 50007/50009 — the only cap that counts house load
    together with a forced charge. See set_import_limit's docstring and
    scripts/tools/probe_import_limit.py; the registers are not probe-verified on the reference
    unit, which is why dispatch_loop leaves the feature off unless DISPATCH_IMPORT_LIMIT_W is set."""

    def test_writes_the_value_before_the_switch(self):
        # Order is load-bearing: enabling first would briefly enforce whatever value an earlier
        # session left in 50009, which could be far below the fuse rating.
        inv = make_inverter()
        ic.set_import_limit(inv, 10_000)

        writes = inv.client.write_calls()
        addrs = [addr for addr, _ in writes]
        self.assertEqual(dict(writes)[ic.REG_IMPORT_LIMIT], 100)  # 10 kW at 0.1 kW/LSB
        self.assertEqual(dict(writes)[ic.REG_IMPORT_LIMIT_ENABLE], 1)
        self.assertLess(addrs.index(ic.REG_IMPORT_LIMIT), addrs.index(ic.REG_IMPORT_LIMIT_ENABLE))

    def test_rounds_to_the_registers_tenth_of_a_kilowatt_resolution(self):
        inv = make_inverter()
        ic.set_import_limit(inv, 11_449)
        self.assertEqual(dict(inv.client.write_calls())[ic.REG_IMPORT_LIMIT], 114)

    def test_rejects_values_outside_the_registers_range(self):
        inv = make_inverter()
        for watts in (0, -1, 100_001):
            with self.subTest(watts=watts):
                with self.assertRaises(ValueError):
                    ic.set_import_limit(inv, watts)
        self.assertEqual(inv.client.write_calls(), [])

    def test_rejects_a_value_that_rounds_away_to_no_limit_at_all(self):
        # 40 W rounds to raw 0, and 0 on this register means "no import allowed" or "off"
        # depending on firmware — either way not what the caller asked for.
        inv = make_inverter()
        with self.assertRaises(ValueError):
            ic.set_import_limit(inv, 40)
        self.assertEqual(inv.client.write_calls(), [])

    def test_clear_turns_the_switch_off_and_leaves_the_value_alone(self):
        inv = make_inverter()
        ic.set_import_limit(inv, 10_000)
        inv.client.calls.clear()

        ic.clear_import_limit(inv)

        writes = inv.client.write_calls()
        self.assertEqual([addr for addr, _ in writes], [ic.REG_IMPORT_LIMIT_ENABLE])
        self.assertEqual(dict(writes)[ic.REG_IMPORT_LIMIT_ENABLE], 0)
        self.assertEqual(inv.client.regs[ic.REG_IMPORT_LIMIT], 100)  # value untouched

    def test_writes_nothing_when_disarmed(self):
        inv = make_inverter()
        with mock.patch.object(ic, "ARMED", False):
            ic.set_import_limit(inv, 10_000)
        self.assertEqual(inv.client.write_calls(), [])


class MiscTests(InverterControlTestCase):
    def test_clamp_power_w_respects_battery_and_grid_caps(self):
        self.assertEqual(ic.clamp_power_w(-500), 0)
        self.assertEqual(ic.clamp_power_w(10**9), min(ic.BATTERY_MAX_W, ic.GRID_CAP_W))
        self.assertEqual(ic.clamp_power_w(1234), 1234)

    def test_charge_sign_requires_confirmed_convention(self):
        ic.SIGN = ""
        self.assertRaises(RuntimeError, ic._charge_sign)



class ForceHoldTests(InverterControlTestCase):
    """force_hold: EMS BattCtrl at 0 W with the caps OPEN (probed on-device). The
    caps must never be written restrictive here: nothing a hold writes may need undoing beyond
    the mode itself."""

    def test_cold_start_writes_open_caps_then_zero_power_then_mode(self):
        inv = make_inverter(soc_pct=60.0)
        self.assertTrue(ic.force_hold(inv, 15.0))
        writes = inv.client.write_calls()
        addrs = [a for a, _ in writes]
        self.assertIn((ic.REG_MAX_AC_OUTPUT, ic.GRID_CAP_RAW), writes)
        self.assertIn((ic.REG_MAX_AC_INPUT, (-ic.GRID_CAP_RAW) & 0xFFFF), writes)
        self.assertNotIn((ic.REG_MAX_AC_OUTPUT, 0), writes)
        self.assertNotIn((ic.REG_MAX_AC_INPUT, 0), writes)
        self.assertLess(addrs.index(ic.REG_BATT_POWER_TARGET), addrs.index(ic.REG_WORK_MODE))
        self.assertEqual(inv.client.regs[ic.REG_BATT_POWER_TARGET], 0)
        self.assertEqual(inv.client.regs[ic.REG_WORK_MODE], ic.WORK_MODE_EMS_BATTCTRL)
        self.assertTrue(ic._forced_active)

    def test_from_a_discharge_only_the_power_register_is_written(self):
        inv = make_inverter(soc_pct=60.0)
        set_already_in_ems_mode(inv, priority=1)
        ic.force_hold(inv, 15.0)
        self.assertEqual(inv.client.write_calls(), [(ic.REG_BATT_POWER_TARGET, 0)])

    def test_below_the_minimum_it_returns_to_auto_instead(self):
        inv = make_inverter(soc_pct=12.0)
        self.assertFalse(ic.force_hold(inv, 15.0))
        self.assertEqual(inv.client.regs[ic.REG_WORK_MODE], ic.WORK_MODE_GENERAL)
        self.assertNotIn(ic.WORK_MODE_EMS_BATTCTRL, [v for a, v in inv.client.write_calls() if a == ic.REG_WORK_MODE])
        self.assertFalse(ic._forced_active)

    def test_return_to_auto_after_a_hold_reasserts_open_caps_before_the_mode(self):
        inv = make_inverter(soc_pct=60.0)
        ic.force_hold(inv, 15.0)
        n = len(inv.client.write_calls())
        ic.return_to_auto(inv)
        after = [a for a, _ in inv.client.write_calls()[n:]]
        self.assertLess(after.index(ic.REG_MAX_AC_INPUT), after.index(ic.REG_WORK_MODE))
        self.assertEqual(inv.client.regs[ic.REG_WORK_MODE], ic.WORK_MODE_GENERAL)


if __name__ == "__main__":
    unittest.main()
