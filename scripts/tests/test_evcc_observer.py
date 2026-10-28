"""Only FC03 network reads are possible through the Waveshare observer."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import evcc_observer as obs

NOW = datetime(2026, 10, 28, 13, 0, tzinfo=timezone.utc)
# Real recorded replies from earlier supervised EVCC FC03 diagnostics.
CONFIG_REPLY = b">01030A0F3F0215010B00A600A635\r\n"
STATUS_REPLY = b">01030A2EC290A60001006000016A\r\n"


class FakeSocket:
    def __init__(self, reply):
        self.reply = reply
        self.sent: list[bytes] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def settimeout(self, *_):
        pass

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, *_):
        result, self.reply = self.reply, b""
        return result


class ObserverTests(unittest.TestCase):
    def test_historical_evcc_telegrams_and_checksum(self):
        self.assertEqual(
            obs._parse_read_reply(CONFIG_REPLY, start=0x000F, count=5),
            (0x0F3F, 0x0215, 0x010B, 0x00A6, 0x00A6))
        self.assertEqual(
            obs._parse_read_reply(STATUS_REPLY, start=0x002E, count=5),
            (0x2EC2, 0x90A6, 0x0001, 0x0060, 0x0001))
        self.assertEqual(obs._read_frame(0x000F, 5), b":0103000F0005E8\r\n")
        self.assertTrue(obs._read_frame(0x002E, 5).startswith(b":0103"))
        for start, count in ((0x0014, 5), (0x000F, 2), (0x002E, 9)):
            with self.subTest(start=start):
                with self.assertRaises(ValueError):
                    obs._read_frame(start, count)

    def test_only_fc03_and_all_readbacks_are_explicitly_advisory(self):
        sockets = [FakeSocket(CONFIG_REPLY), FakeSocket(STATUS_REPLY)]
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            obs.socket, "create_connection", side_effect=lambda *_a, **_kw: sockets.pop(0)
        ):
            value = obs.observe(host="192.168.0.20", lock_file=temp+"/l", now=NOW)
        self.assertTrue(value["read_ok"])
        self.assertEqual(value["configured_pwm_raw"], 0x00A6)
        self.assertEqual(value["pilot_pwm_raw"], 0x00A6)
        self.assertAlmostEqual(value["configured_current_a"], 9.96, places=1)
        self.assertTrue(value["pilot_matches_config"])
        self.assertTrue(value["charging_detected"])
        self.assertAlmostEqual(value["measured_current_a"], 9.8)
        self.assertFalse(value["l3_rms_current_verified"])
        self.assertFalse(value["physical_control_eligible"])
        self.assertFalse(value["metered_ev_energy_available"])
        self.assertEqual(value["modbus_function"], "FC03")
        self.assertEqual(value["writes"], 0)

    def test_bounded_fc03_retry_when_first_tcp_connection_times_out(self):
        # Confirmed live behavior: EVCC may not reply on the first socket,
        # but the second bounded FC03 attempt succeeds within ~2.6s.
        regs = (0x0F3F, 0x0215, 0x010B, 100, 100)
        with mock.patch.object(obs, "_read_window", side_effect=[
            obs.EvccReadError("EVCC network read failed"), regs
        ]) as single:
            result = obs._read_window_retry("192.168.0.20", 0x000F, 5,
                                            port=4196, timeout=2.5)
        self.assertEqual(result[3], 100)
        self.assertEqual(single.call_count, 2)
        self.assertEqual([call.args[1] for call in single.call_args_list], [0x000F, 0x000F])

    def test_fc03_reader_does_not_retry_beyond_two_failed_connections(self):
        with mock.patch.object(obs, "_read_window",
                               side_effect=obs.EvccReadError("timeout")) as single:
            with self.assertRaises(obs.EvccReadError):
                obs._read_window_retry("192.168.0.20", 0x002E, 5,
                                       port=4196, timeout=2.5)
        self.assertEqual(single.call_count, 2)

    def test_status_checksum_corruption_disables_report(self):
        corrupted = STATUS_REPLY[:-4] + b"FF\r\n"
        with self.assertRaises(obs.EvccReadError):
            obs._parse_read_reply(corrupted, start=0x002E, count=5)
        with self.assertRaises(obs.EvccReadError):
            obs._parse_read_reply(CONFIG_REPLY, start=0x002E, count=5)

    def test_disallowed_public_destination_or_port(self):
        for host in ("", "example.org", "8.8.8.8", "172.8.1.1", "::1"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                obs.observe(host=host, now=NOW)
        with self.assertRaises(ValueError):
            obs.observe(host="192.168.0.20", port=0, now=NOW)

    def test_no_network_when_service_gate_off(self):
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"EVCC_READ_ONLY_ENABLED": "0"}), \
             mock.patch.object(obs, "once") as once, \
             mock.patch.object(sys, "argv", ["evcc_observer.py"]), \
             redirect_stdout(output):
            obs.main()
        self.assertEqual(json.loads(output.getvalue())["writes"], 0)
        once.assert_not_called()

    def test_connection_failure_returns_unknown_not_stale_success(self):
        with mock.patch.dict(os.environ, {"EVCC_READ_HOST": "192.168.0.20"}), \
             mock.patch.object(obs, "observe", side_effect=obs.EvccReadError("fault")):
            result = obs.once(now=NOW)
        self.assertFalse(result["read_ok"])
        self.assertEqual(result["state"], "unknown")
        self.assertIsNone(result["charging_detected"])
        self.assertFalse(result["physical_control_eligible"])
        self.assertEqual(result["writes"], 0)

    def test_source_contains_no_modbus_write_path(self):
        source = Path(obs.__file__).read_text()
        self.assertNotIn("set_limit_raw", source)
        self.assertNotIn("RAW_PAUSE", source)
        self.assertNotIn("EVCC_CONTROL_ARMED", source)
        self.assertIn("_READ_FUNCTION = 0x03", source)
        self.assertIn("data = bytes([slave, _READ_FUNCTION])", source)
        self.assertNotIn("import evcc_ascii", source)


if __name__ == "__main__":
    unittest.main()
