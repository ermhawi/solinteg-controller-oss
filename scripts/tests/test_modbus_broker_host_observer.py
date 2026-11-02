"""Read-only host observer tests: all OS process/TCP output is injected."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
from modbus_broker_host_observer import observe_host_release

UNIT = "solinteg-broker-readonly.service"
HOST = "192.0.2.25"  # RFC 5737 documentation-only endpoint
STOPPED = "\n".join((
    "LoadState=loaded", "ActiveState=inactive", "MainPID=0", "ControlPID=0",
    "KillMode=control-group", "PrivateNetwork=no", "NetworkNamespacePath=",
))
TCP_CLOSED = (
    "LISTEN 0 128 0.0.0.0:80 0.0.0.0:*\n"
    "ESTAB 0 0 192.0.2.1:45812 198.51.100.15:443\n"
)


class StubRun:
    def __init__(self, service=STOPPED, tcp=TCP_CLOSED):
        self.service, self.tcp = service, tcp
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        output = self.service if args[0] == "systemctl" else self.tcp
        if isinstance(output, Exception):
            raise output
        if isinstance(output, tuple):
            return SimpleNamespace(stdout=output[0], returncode=output[1])
        return SimpleNamespace(stdout=output, returncode=0)


class HostObserverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.socket = Path(self.tmp.name) / "broker.sock"

    def tearDown(self):
        self.tmp.cleanup()

    def observe(self, runner=None, **kwargs):
        run = runner or StubRun()
        value = observe_host_release(unit=UNIT, inverter_ip=HOST,
                                     socket_path=self.socket, run=run,
                                     geteuid=lambda: 0, monotonic=lambda: 99, **kwargs)
        return value, run

    def test_stopped_unit_no_endpoint_no_ipc_is_only_safe_case(self):
        obs, run = self.observe()
        self.assertTrue(obs.exclusive_release_verified())
        self.assertEqual(obs.checked_at, 99)
        self.assertEqual([c[0][0] for c in run.calls], ["systemctl", "ss"])
        for args, opts in run.calls:
            self.assertFalse(opts.get("shell", False))
            self.assertTrue(opts["capture_output"])
            self.assertLessEqual(opts["timeout"], 5)

    def test_matching_peer_even_timewait_or_syn_sent_blocks(self):
        for state in ("ESTAB", "TIME-WAIT", "SYN-SENT"):
            with self.subTest(state=state):
                runner = StubRun(tcp=TCP_CLOSED +
                                 f"{state} 0 0 192.0.2.1:43000 {HOST}:502\n")
                obs, _ = self.observe(runner)
                self.assertEqual(obs.other_tcp_sessions, 1)
                self.assertFalse(obs.exclusive_release_verified())

    def test_other_endpoint_and_port_not_misclassified(self):
        runner = StubRun(tcp=TCP_CLOSED +
                         f"ESTAB 0 0 192.0.2.1:43333 192.0.2.26:502\n"
                         f"ESTAB 0 0 192.0.2.1:43334 {HOST}:503\n")
        obs, _ = self.observe(runner)
        self.assertTrue(obs.exclusive_release_verified())

    def test_ipv4_mapped_ipv6_equivalent_remote_is_detected(self):
        runner = StubRun(tcp=f"ESTAB 0 0 [::1]:49222 [::ffff:{HOST}]:502\n")
        obs, _ = self.observe(runner)
        self.assertEqual(obs.other_tcp_sessions, 1)

    def test_still_active_or_deactivating_cannot_be_safe(self):
        for state in ("active", "deactivating"):
            with self.subTest(state=state):
                run = StubRun(service=STOPPED.replace("inactive", state))
                obs, run = self.observe(run)
                self.assertTrue(obs.broker_alive)
                self.assertFalse(obs.exclusive_release_verified())
                self.assertEqual(len(run.calls), 1)

    def test_unit_missing_or_untrusted_configuration_fails_closed(self):
        cases = (
            STOPPED.replace("LoadState=loaded", "LoadState=not-found"),
            STOPPED.replace("MainPID=0", "MainPID=38"),
            STOPPED.replace("ControlPID=0", "ControlPID=39"),
            STOPPED.replace("KillMode=control-group", "KillMode=process"),
            STOPPED.replace("PrivateNetwork=no", "PrivateNetwork=yes"),
            STOPPED.replace("NetworkNamespacePath=", "NetworkNamespacePath=/run/netns/x"),
            STOPPED.replace("MainPID=0", "MainPID=abc"),
            STOPPED.replace("LoadState=loaded\n", ""),
            STOPPED + "\nActiveState=inactive",
        )
        for config in cases:
            with self.subTest(config=config):
                obs, run = self.observe(StubRun(service=config))
                self.assertFalse(obs.exclusive_release_verified())
                self.assertEqual(len(run.calls), 1)

    def test_incomplete_ss_output_or_permission_denied_is_unknown(self):
        invalid = (
            ("", 1),
            ("ESTAB 0 0\n", 0),
            (f"ESTAB 0 0 192.0.2.1:40000 {HOST}:https\n", 0),
            (f"ESTAB 0 0 192.0.2.1:40000 bad-host:502\n", 0),
        )
        for tcp in invalid:
            with self.subTest(tcp=tcp):
                obs, _ = self.observe(StubRun(tcp=tcp))
                self.assertIsNone(obs.broker_alive)
                self.assertFalse(obs.exclusive_release_verified())

    def test_systemctl_error_and_timeout_are_unknown(self):
        for service in (("", 1), TimeoutError("unit busy")):
            with self.subTest(service=str(service)):
                obs, _ = self.observe(StubRun(service=service))
                self.assertIsNone(obs.broker_alive)
                self.assertFalse(obs.exclusive_release_verified())

    def test_socket_left_behind_blocks_release(self):
        self.socket.write_text("not a trusted socket")
        obs, _ = self.observe()
        self.assertTrue(obs.broker_socket_listening)
        self.assertFalse(obs.exclusive_release_verified())

    def test_non_root_cannot_assert_exclusive_release(self):
        run = StubRun()
        obs = observe_host_release(unit=UNIT, inverter_ip=HOST,
                                   socket_path=self.socket, run=run,
                                   geteuid=lambda: 1000)
        self.assertIsNone(obs.broker_alive)
        self.assertEqual(run.calls, [])
        self.assertFalse(obs.exclusive_release_verified())

    def test_invalid_input_does_not_run_commands(self):
        run = StubRun()
        for unit in ("bad;systemctl stop x", "plain", "../solinteg.service", "--evil.service"):
            with self.subTest(unit=unit), self.assertRaises(ValueError):
                observe_host_release(unit=unit, inverter_ip=HOST,
                                     socket_path=self.socket, run=run,
                                     geteuid=lambda: 0)
        with self.assertRaises(ValueError):
            observe_host_release(unit=UNIT, inverter_ip="inverter.local",
                                 socket_path=self.socket, run=run,
                                 geteuid=lambda: 0)
        self.assertEqual(run.calls, [])


if __name__ == "__main__":
    unittest.main()
