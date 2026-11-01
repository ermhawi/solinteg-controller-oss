"""Offline-only broker fault-injection tests. No sockets to the inverter, ever."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

SERVICES = Path(__file__).resolve().parents[1] / "services"
sys.path.insert(0, str(SERVICES))
from modbus_broker import Broker, BrokerError, Fenced, UnsafeSample, EMS_BATTCTRL, GENERAL
from modbus_broker_ipc import BrokerIpcClient, BrokerIpcServer


def s32_regs(value):
    value &= 0xFFFFFFFF
    return [(value >> 16) & 0xFFFF, value & 0xFFFF]


class Response:
    def __init__(self, registers=(), error=False):
        self.registers = list(registers)
        self._error = error

    def isError(self):
        return self._error


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, dt):
        self.now += dt


class FakeDongle:
    """Synthetic strict one-session FC03/FC06 dongle and controlled faults."""

    def __init__(self, clock=None):
        self.clock = clock or FakeClock()
        self.regs = {}
        self.writes = []
        self.reads = []
        self.active = 0
        self.peak = 0
        self.clients = 0
        self.fail_at_write = None
        self.fail_write_indices = set()
        self.read_error_addr = None
        self.stall_addr = None
        self.stall_seconds = 0
        self.flip_mode_addr = None
        self.flip_mode_to = EMS_BATTCTRL
        self.block_write_addr = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self.load(grid=-800, battery=200, ac=200, pv=0, mode=GENERAL)

    def put_s32(self, addr, value):
        hi, lo = s32_regs(value)
        self.regs[addr] = hi
        self.regs[addr+1] = lo

    def load(self, *, grid, battery, ac, pv, mode=GENERAL, soc=50.0):
        self.put_s32(10994, int(grid / 3))
        self.put_s32(10996, int(grid / 3))
        self.put_s32(10998, grid - 2*int(grid / 3))
        self.put_s32(11000, grid)
        self.put_s32(11016, ac)
        self.put_s32(11028, pv)
        self.put_s32(30258, battery)
        self.regs[33000] = int(soc * 100)
        self.regs[33001] = 9900
        self.regs[33003] = 250
        self.regs[50000] = mode
        self.regs[50006] = 0

    def connection(self):
        self.clients += 1
        return FakeConnection(self)


class FakeConnection:
    def __init__(self, device):
        self.device = device
        self.connected = False

    def connect(self):
        if self.device.active:
            return False
        self.device.active += 1
        self.device.peak = max(self.device.peak, self.device.active)
        self.connected = True
        return True

    def close(self):
        if self.connected:
            self.device.active -= 1
        self.connected = False

    def read_holding_registers(self, addr, count=1, device_id=1):
        assert self.connected and device_id == 255
        d = self.device
        d.reads.append((addr, count))
        if d.stall_addr == addr:
            d.clock.sleep(d.stall_seconds)
        if d.flip_mode_addr == addr:
            d.regs[50000] = d.flip_mode_to
            d.flip_mode_addr = None
        if d.read_error_addr == addr:
            return Response(error=True)
        return Response([d.regs.get(addr+i, 0) for i in range(count)])

    def write_register(self, addr, value, device_id=1):
        assert self.connected and device_id == 255
        d = self.device
        if d.block_write_addr == addr:
            d.entered.set()
            if not d.release.wait(4):
                raise TimeoutError("test write stall")
        d.writes.append((addr, value))
        if d.fail_at_write == len(d.writes) or len(d.writes) in d.fail_write_indices:
            raise OSError("uncertain FC06 response")
        d.regs[addr] = value & 0xFFFF
        return Response()


class BrokerTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.device = FakeDongle(self.clock)
        self.broker = Broker(self.device.connection, clock=self.clock, sleep=self.clock.sleep,
                             min_write_interval_s=0, phase_guard=lambda *_: True,
                             armed=True, max_sample_s=20, charge_sign="neg_charge")

    def tearDown(self):
        self.device.release.set()
        self.broker.close()

    def read(self):
        return self.broker.submit("read_snapshot", role="read")

    def ctrl(self, op, rid="t1", watts=None):
        return self.broker.submit(op, role="control", request_id=rid, watts=watts)

    def test_valid_sample_retains_poller_fields_and_per_block_times(self):
        result = self.read()
        self.assertEqual(result["status"], "valid")
        data = result["sample"]
        self.assertEqual(data["soc_pct"], 50)
        self.assertEqual(data["soc_kwh"], 12.8)
        self.assertEqual(data["work_mode"], "General")
        self.assertEqual(data["battery_w"], 200)
        self.assertEqual(data["grid_w"], -800)
        self.assertEqual(data["house_load_w"], 1000)
        self.assertEqual(sum(data["meter_l%d_w" % i] for i in (1,2,3)), -800)
        self.assertEqual(data["mode_before"], GENERAL)
        self.assertEqual(data["mode_after"], GENERAL)
        self.assertGreaterEqual(len(data["blocks"]), 6)
        self.assertEqual(data["quality"], "valid")
        self.assertEqual(self.broker.status()["last_valid_timestamp"], data["timestamp"])

    def test_exact_third_soc25_bad_energy_snapshot_rejected(self):
        self.assertEqual(self.read()["status"], "valid")
        last = self.broker.status()["last_valid_timestamp"]
        self.device.load(grid=-11, battery=-1692, ac=978, pv=0, mode=EMS_BATTCTRL)
        result = self.read()
        self.assertEqual((result["status"], result["reason"]),
                         ("rejected", "energy_balance_invalid"))
        self.assertIsNone(result["sample"])
        self.assertEqual(self.broker.status()["last_valid_timestamp"], last)
        self.assertEqual(self.broker.status()["rejected_samples"], 1)

    def test_mode_flip_between_register_blocks_rejected(self):
        self.device.flip_mode_addr = 30258
        result = self.read()
        self.assertEqual(result["reason"], "mode_changed")
        self.assertIsNone(result["sample"])

    def test_ct_mismatch_is_never_usable(self):
        self.device.put_s32(10998, -900)
        result = self.read()
        self.assertEqual(result["reason"], "ct_invalid")

    def test_extended_ct_fallback_not_usable_for_control(self):
        self.device.read_error_addr = 10994
        result = self.read()
        self.assertEqual(result["reason"], "ct_invalid")
        with self.assertRaisesRegex(UnsafeSample, "preflight rejected"):
            self.ctrl("charge", watts=1500)
        self.assertEqual(self.device.writes, [])

    def test_four_to_eight_second_cloud_stall_is_measured(self):
        self.device.stall_addr = 30258
        self.device.stall_seconds = 8
        result = self.read()
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["sample"]["duration_s"], 8)
        self.assertTrue(any(b["address"] == 30258 and b["end_s"]-b["start_s"] == 8
                            for b in result["sample"]["blocks"]))

    def test_slow_acquisition_cannot_refresh_valid_timestamp(self):
        self.read()
        last = self.broker.status()["last_valid_timestamp"]
        self.device.stall_addr = 30258
        self.device.stall_seconds = 25
        result = self.read()
        self.assertEqual(result["reason"], "stale")
        self.assertEqual(self.broker.status()["last_valid_timestamp"], last)

    def test_timeout_fail_closed(self):
        self.device.read_error_addr = 30258
        self.assertEqual(self.read()["reason"], "timeout")
        self.assertIsNone(self.broker.status()["last_valid_timestamp"])

    def test_one_lease_single_client_even_for_parallel_requests(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.read())) for _ in range(12)]
        for t in threads: t.start()
        for t in threads: t.join(4)
        self.assertEqual(len(results), 12)
        self.assertTrue(all(x["status"] == "valid" for x in results))
        self.assertEqual((self.device.peak,self.device.clients,self.device.active), (1,1,1))
        second = self.device.connection()
        self.assertFalse(second.connect())
        self.assertEqual(self.device.peak, 1)

    def test_control_preserves_write_order_and_readback(self):
        result = self.ctrl("charge", watts=1500)
        self.assertEqual(result["status"], "registers_verified_only")
        self.assertIs(result["physical_effect_verified"], False)
        self.assertEqual(self.device.writes, [
            (50209, (-1100)&0xFFFF),(50208, 1100),(50210, 0),
            (50207, (-150)&0xFFFF),(50000, EMS_BATTCTRL)])
        self.assertEqual(self.device.regs[50000], EMS_BATTCTRL)
        self.assertEqual(self.device.peak,1)

    def test_pos_charge_polarity_is_explicitly_parameterized(self):
        device=FakeDongle(FakeClock())
        b=Broker(device.connection,armed=True,charge_sign="pos_charge",
                 phase_guard=lambda *_:True,min_write_interval_s=0,
                 clock=device.clock,sleep=device.clock.sleep)
        try:
            b.submit("charge",role="control",request_id="pos",watts=1000)
            self.assertIn((50207,100),device.writes)
        finally:
            b.close()

    def test_unowned_auto_does_not_overwrite_operator_selected_mode(self):
        self.device.regs[50000] = 0x400
        result = self.ctrl("auto")
        self.assertEqual(result["status"],"unowned_noop")
        self.assertEqual(self.device.regs[50000],0x400)
        self.assertEqual(self.device.writes, [])

    def test_clean_shutdown_restores_only_owned_control(self):
        self.ctrl("charge",watts=1000)
        self.assertEqual(self.device.regs[50000],EMS_BATTCTRL)
        self.assertTrue(self.broker.status()["owns_forced_state"])
        self.broker.close()
        self.assertEqual(self.device.regs[50000],GENERAL)
        self.assertEqual(self.device.active,0)

    def test_request_id_idempotent_retry_and_conflict(self):
        first = self.ctrl("discharge", watts=500)
        old_writes = list(self.device.writes)
        second = self.ctrl("discharge", watts=500)
        self.assertEqual(first, second)
        self.assertEqual(self.device.writes, old_writes)
        with self.assertRaises(BrokerError):
            self.ctrl("charge", watts=500)

    def test_unarmed_default_blocks_all_control(self):
        inactive = Broker(self.device.connection)
        try:
            with self.assertRaises(PermissionError):
                inactive.submit("charge",role="control",request_id="new",watts=1000)
            with self.assertRaises(PermissionError):
                inactive.submit("safety_auto",role="safety",request_id="emergency")
        finally:
            inactive.close()
        self.assertEqual(self.device.writes, [])

    def test_role_gates_raw_and_safety_actions(self):
        with self.assertRaises(PermissionError):
            self.broker.submit("charge",role="read",request_id="new",watts=1000)
        with self.assertRaises(PermissionError):
            self.broker.submit("safety_auto",role="control",request_id="new")
        with self.assertRaises(PermissionError):
            self.broker.submit("write_register",role="safety",request_id="new")
        with self.assertRaises(ValueError):
            self.ctrl("charge",rid="bad name",watts=1000)
        with self.assertRaises(ValueError):
            self.ctrl("charge",rid="range",watts=0)
        self.assertEqual(self.device.writes, [])

    def test_charge_refuses_soc_ceiling_and_discharge_floor(self):
        self.device.load(grid=-800,battery=200,ac=200,pv=0,soc=98)
        with self.assertRaisesRegex(UnsafeSample, "ceiling"):
            self.ctrl("charge",watts=1500)
        self.device.load(grid=-800,battery=200,ac=200,pv=0,soc=8)
        with self.assertRaisesRegex(UnsafeSample, "floor"):
            self.ctrl("discharge",rid="other",watts=1500)
        self.assertEqual(self.device.writes, [])

    def test_unknown_working_mode_refuses_control(self):
        self.device.regs[50000] = 0x400
        with self.assertRaisesRegex(UnsafeSample,"unexpected inverter working mode"):
            self.ctrl("discharge",watts=1000)
        self.assertEqual(self.device.writes, [])

    def test_invalid_timeouts_never_queue_writes(self):
        for timeout in ("5", float("nan"), float("inf"), 0, 121, True):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.broker.submit("charge",role="control",request_id="invalid",
                                   watts=1000,timeout_s=timeout)
        self.assertEqual(self.device.writes, [])

    def test_hold_requires_separate_validation(self):
        with self.assertRaisesRegex(UnsafeSample, "unvalidated"):
            self.ctrl("reserve_hold", watts=1000)
        self.assertEqual(self.device.writes, [])

    def test_sign_must_be_explicitly_confirmed(self):
        b=Broker(self.device.connection, armed=True, phase_guard=lambda *_:True,
                 min_write_interval_s=0)
        try:
            with self.assertRaisesRegex(UnsafeSample,"sign convention not confirmed"):
                b.submit("charge",role="control",request_id="polarity",watts=1500)
        finally:
            b.close()
        self.assertEqual(self.device.writes, [])

    def test_missing_phase_guard_blocks_control(self):
        unguarded = Broker(self.device.connection, armed=True,min_write_interval_s=0,
                           charge_sign="neg_charge")
        try:
            with self.assertRaisesRegex(UnsafeSample, "phase guard"):
                unguarded.submit("charge",role="control",request_id="no_guard",watts=1500)
        finally:
            unguarded.close()
        self.assertEqual(self.device.writes, [])

    def test_failing_every_write_position_attempts_auto_rollback(self):
        for step in range(1,6):
            device=FakeDongle(FakeClock())
            device.fail_at_write=step
            broker=Broker(device.connection,armed=True,charge_sign="neg_charge",phase_guard=lambda *_:True,
                          min_write_interval_s=0,clock=device.clock,sleep=device.clock.sleep)
            try:
                with self.assertRaisesRegex(BrokerError, "rollback"):
                    broker.submit("charge",role="control",request_id="crash",watts=1000)
                self.assertEqual(device.regs[50000],GENERAL)
                self.assertTrue(any(addr == 50000 and value == GENERAL for addr,value in device.writes))
            finally:
                broker.close()

    def test_safety_preempts_queued_work_and_latches_control(self):
        self.device.block_write_addr=50210
        errors={}
        def control():
            try:self.ctrl("charge",watts=1000)
            except Exception as exc:errors["charge"]=type(exc).__name__
        t=threading.Thread(target=control)
        t.start()
        self.assertTrue(self.device.entered.wait(3))
        def safety():
            try:errors["safety"]=self.broker.submit(
                "safety_auto",role="safety",request_id="safe")
            except Exception as exc:errors["safety"]=type(exc).__name__
        s=threading.Thread(target=safety)
        s.start()
        deadline=time.monotonic()+2
        while not self.broker.status()["safety_latched"] and time.monotonic()<deadline:
            time.sleep(.005)
        self.assertTrue(self.broker.status()["safety_latched"])
        with self.assertRaises(Fenced):
            self.ctrl("discharge",rid="late",watts=1000)
        self.device.release.set()
        t.join(4);s.join(4)
        self.assertEqual(errors["charge"],"BrokerError")
        self.assertEqual(errors["safety"]["mode"],GENERAL)
        self.assertEqual(self.device.regs[50000],GENERAL)
        self.assertFalse(any(addr==50207 and value==100 for addr,value in self.device.writes))

    def test_safety_idempotency_namespace_cannot_be_blocked_by_dispatcher(self):
        self.ctrl("charge",rid="shared",watts=1000)
        recovered=self.broker.submit("safety_auto",role="safety",request_id="shared")
        self.assertEqual(recovered["mode"],GENERAL)
        self.assertTrue(self.broker.status()["safety_latched"])

    def test_double_safety_auto_only_replays_same_request_once(self):
        first=self.broker.submit("safety_auto",role="safety",request_id="safe")
        writes=list(self.device.writes)
        self.assertEqual(self.broker.submit("safety_auto",role="safety",request_id="safe"), first)
        self.assertEqual(self.device.writes,writes)
        self.assertTrue(self.broker.status()["safety_latched"])
        with self.assertRaises(Fenced):self.ctrl("charge",watts=1000)

    def test_disarmed_unauthorized_call_never_instantiates_client(self):
        b=Broker(self.device.connection)
        try:
            with self.assertRaises(PermissionError):
                b.submit("safety_auto",role="read",request_id="unsafe")
            self.assertEqual(self.device.clients,0)
        finally:b.close()

    def test_source_never_opens_a_physical_modbus_socket(self):
        for name in ("modbus_broker.py", "modbus_broker_ipc.py"):
            source=(SERVICES/name).read_text()
            self.assertNotIn("from pymodbus",source)
            self.assertNotIn("ModbusTcpClient(",source)
            self.assertNotIn('os.environ.get("SOLINTEG_HOST"', source)


    def test_rejected_diagnostics_are_separate_without_raw_power_data(self):
        recorded=[]
        b=Broker(self.device.connection, clock=self.clock, sleep=self.clock.sleep,
                 min_write_interval_s=0, rejection_sink=recorded.append)
        try:
            self.device.load(grid=-11,battery=-1692,ac=978,pv=0,mode=EMS_BATTCTRL)
            rejected=b.submit("read_snapshot")
            self.assertEqual(rejected["reason"],"energy_balance_invalid")
            self.assertEqual(len(recorded),1)
            self.assertEqual(recorded[0]["quality"],"energy_balance_invalid")
            self.assertNotIn("battery_w", recorded[0])
            self.assertNotIn("grid_w", recorded[0])
            self.assertIsNone(b.status()["last_valid_timestamp"])
        finally:
            b.close()

    def test_rate_limit_is_between_all_five_writes(self):
        clock=FakeClock()
        device=FakeDongle(clock)
        b=Broker(device.connection,armed=True,charge_sign="neg_charge",phase_guard=lambda *_:True,
                 clock=clock,sleep=clock.sleep,min_write_interval_s=2)
        try:
            self.assertEqual(b.submit("charge",role="control",request_id="rate",watts=1000)
                             ["status"],"registers_verified_only")
            self.assertGreaterEqual(clock.now-100,8)
            self.assertEqual(len(device.writes),5)
        finally:
            b.close()

    def test_rollback_failure_must_not_claim_auto_restored(self):
        device=FakeDongle(FakeClock())
        device.fail_write_indices={3,6}  # command fails, then mode restore fails
        b=Broker(device.connection,armed=True,charge_sign="neg_charge",phase_guard=lambda *_:True,
                 min_write_interval_s=0,clock=device.clock,sleep=device.clock.sleep)
        try:
            with self.assertRaisesRegex(BrokerError,"rollback FAILED"):
                b.submit("charge",role="control",request_id="fail",watts=1000)
        finally:
            b.close()

    def test_generation_fences_queued_command_submitted_before_safety(self):
        self.device.block_write_addr=50210
        results={}
        def request(op,key):
            try:
                results[key]=self.broker.submit(op,role="control",request_id=key,
                                                 watts=1000)
            except Exception as exc:
                results[key]=type(exc).__name__
        active=threading.Thread(target=request,args=("charge","active"))
        active.start()
        self.assertTrue(self.device.entered.wait(3))
        queued=threading.Thread(target=request,args=("discharge","queued"))
        queued.start()
        deadline=time.monotonic()+2
        while self.broker.status()["queue_depth"]<1 and time.monotonic()<deadline:
            time.sleep(.005)
        self.assertGreaterEqual(self.broker.status()["queue_depth"],1)
        safety=threading.Thread(target=lambda: results.update(
            safety=self.broker.submit("safety_auto",role="safety",request_id="safe")))
        safety.start()
        deadline=time.monotonic()+2
        while not self.broker.status()["safety_latched"] and time.monotonic()<deadline:
            time.sleep(.005)
        self.assertTrue(self.broker.status()["safety_latched"])
        self.device.release.set()
        for t in (active,queued,safety):t.join(4)
        self.assertEqual(results["queued"],"Fenced")
        self.assertEqual(results["active"],"BrokerError")
        self.assertEqual(results["safety"]["mode"],GENERAL)
        self.assertFalse(any(addr==50207 and value==100 for addr,value in self.device.writes))

    def test_disconnect_recreates_the_only_owned_client(self):
        self.assertEqual(self.read()["status"],"valid")
        self.broker._transport.close()  # simulate dongle reset
        self.assertEqual(self.device.active,0)
        self.assertEqual(self.read()["status"],"valid")
        self.assertEqual(self.device.clients,2)
        self.assertEqual(self.device.peak,1)

    def test_unknown_request_result_timeout_fences_future_control(self):
        self.device.block_write_addr=50210
        try:
            with self.assertRaisesRegex(TimeoutError,"result unknown"):
                self.broker.submit("charge",role="control",request_id="slow",
                                   watts=1000,timeout_s=.05)
            self.assertTrue(self.broker.status()["safety_latched"])
            with self.assertRaises(Fenced):
                self.ctrl("discharge",rid="later",watts=1000)
        finally:
            self.device.release.set()
        self.assertTrue(self.broker._worker.is_alive())

class BrokerIpcTest(unittest.TestCase):
    def setUp(self):
        self.tempdir=tempfile.TemporaryDirectory()
        self.root=Path(self.tempdir.name)
        self.clock=FakeClock()
        self.device=FakeDongle(self.clock)
        self.broker=Broker(self.device.connection,clock=self.clock,sleep=self.clock.sleep,
                           min_write_interval_s=0,armed=True,charge_sign="neg_charge",
                           phase_guard=lambda *_:True)
        self.socket_path=self.root/"broker.sock"
        self.server=BrokerIpcServer(self.socket_path,self.broker)
        self.server.start()
        self.client=BrokerIpcClient(self.socket_path)

    def tearDown(self):
        self.server.close()
        self.broker.close()
        self.tempdir.cleanup()

    def test_uds_read_only_peer_role_cannot_be_spoofed(self):
        self.assertEqual(self.client.request("read_snapshot")["status"],"valid")
        self.assertTrue(self.client.request("status")["offline_only"])
        with self.assertRaisesRegex(RuntimeError,"ValueError"):
            self.client.request("status",role="safety")
        with self.assertRaisesRegex(RuntimeError,"ValueError"):
            self.client.request("safety_auto",request_id="fake", role="safety")
        with self.assertRaisesRegex(RuntimeError,"PermissionError"):
            self.client.request("charge",request_id="fake",watts=1000)
        self.assertEqual(self.device.writes, [])
        self.assertEqual(self.socket_path.stat().st_mode & 0o777,0o600)

    def test_peer_credential_roles_not_payload_role(self):
        self.server.close()
        self.server=BrokerIpcServer(self.socket_path,self.broker,role_for_peer=lambda *_:"control")
        self.server.start()
        result=self.client.request("charge",request_id="legit",watts=1000)
        self.assertEqual(result["status"],"registers_verified_only")
        with self.assertRaisesRegex(RuntimeError,"PermissionError"):
            self.client.request("safety_auto",request_id="fake")
        self.assertEqual(self.device.peak,1)

    def test_ipc_rejects_non_json_unknown_keys_oversized_messages(self):
        with self.assertRaisesRegex(RuntimeError,"ValueError"):
            self.client.request("read_snapshot",arbitrary="raw-fc06")
        with self.assertRaises(ValueError):
            self.client.request("read_snapshot",junk="x"*20000)

    def test_socket_does_not_remove_other_owners_file(self):
        self.server.close()
        self.socket_path.write_text("owned by another process")
        self.server.close()
        self.assertEqual(self.socket_path.read_text(),"owned by another process")


if __name__ == "__main__":
    unittest.main()
