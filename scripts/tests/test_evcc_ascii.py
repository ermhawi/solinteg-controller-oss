"""Offline EVCC2 protocol and manual-write gate tests. No network or hardware."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))
from evcc_ascii import (
    EvccAscii, EvccError, EvccUncertainWrite, RAW_PAUSE,
    amps_to_raw, frame, parse_reply,
)


def reply_read(regs):
    return frame(bytes([1, 3, len(regs) * 2]) +
                 b"".join(v.to_bytes(2, "big") for v in regs), reply=True)


def config(raw):
    return reply_read([0x0F00, 0, 0, raw, 0])


def live(raw, current=100, state=0xC2):
    return reply_read([0x2E00 | state, 0x8000 | raw, 0x03E8, current, 0x03E8])


class FakeSocket:
    def __init__(self, response):
        self.response = response
        self.sent = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def settimeout(self, seconds):
        pass

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, n):
        if isinstance(self.response, Exception):
            raise self.response
        data, self.response = self.response[:3], self.response[3:]
        return data


class DriverTests(unittest.TestCase):
    def test_official_frames_and_lrc(self):
        self.assertEqual(frame(bytes.fromhex("010300010002")), b":010300010002F9\r\n")
        self.assertEqual(frame(bytes.fromhex("0110001400010200A6")),
                         b":0110001400010200A632\r\n")
        self.assertEqual(parse_reply(b">01030401011237AD\r\n", 1, 3),
                         bytes.fromhex("0401011237"))

    def test_reject_corrupt_or_foreign_reply(self):
        for message in (b">01030401011237AE\r\n",
                        b">02030401011237AC\r\n",
                        b":01030401011237AD\r\n"):
            with self.assertRaises(EvccError):
                parse_reply(message, 1, 3)

    def test_read_split_tcp_packets(self):
        client = EvccAscii("example.invalid")
        conn = FakeSocket(config(166))
        with mock.patch("evcc_ascii.socket.create_connection", return_value=conn):
            self.assertEqual(client.read(0x0F, 5)[3], 166)
        self.assertTrue(conn.closed)
        self.assertEqual(conn.sent[0], frame(bytes.fromhex("0103000F0005")))

    def test_sleepy_evcc_read_only_retry(self):
        client = EvccAscii("example.invalid")
        attempts = [FakeSocket(TimeoutError("idle")), FakeSocket(config(166))]
        with mock.patch("evcc_ascii.socket.create_connection", side_effect=attempts) as dial:
            self.assertEqual(client.read(0x0F, 5)[3], 166)
            self.assertEqual(dial.call_count, 2)

    def test_reads_two_blocks_not_unsupported_0014(self):
        client = EvccAscii("example.invalid")
        sockets = [FakeSocket(config(166)), FakeSocket(live(166, current=97))]
        with mock.patch("evcc_ascii.socket.create_connection", side_effect=sockets):
            state = client.status()
        self.assertEqual(state.state, "C2")
        self.assertEqual(state.configured_raw, 166)
        self.assertEqual(state.pilot_raw, 166)
        self.assertAlmostEqual(state.measured_current_a, 9.7)
        self.assertIn(b"000F", sockets[0].sent[0])
        self.assertIn(b"002E", sockets[1].sent[0])

    def test_2026_10_21_recorded_field_telegrams(self):
        client = EvccAscii("example.invalid")
        answers = [
            FakeSocket(b">01030A0F3F0215010B00A600A635\r\n"),
            FakeSocket(b">01030A2EC290A60001006000016A\r\n"),
        ]
        with mock.patch("evcc_ascii.socket.create_connection", side_effect=answers):
            status = client.status()
        self.assertEqual((status.configured_raw, status.pilot_raw), (166, 166))
        self.assertEqual(status.state, "C2")
        self.assertEqual(status.currents_a, (0.1, 9.6, 0.1))

    def test_unknown_current_is_not_zero(self):
        client = EvccAscii("example.invalid")
        answers = [FakeSocket(config(1000)), FakeSocket(live(1000, current=1000, state=0xA1))]
        with mock.patch("evcc_ascii.socket.create_connection", side_effect=answers):
            self.assertIsNone(client.status().measured_current_a)

    def test_writes_are_disabled_without_all_gates(self):
        client = EvccAscii("example.invalid", manual_test=True)
        with mock.patch.dict(os.environ, {"EV_ENABLED": "1", "EVCC_CONTROL_ARMED": "0",
                                         "EVCC_MANUAL_TEST": "1"}), \
             mock.patch("evcc_ascii.socket.create_connection") as dial:
            with self.assertRaises(PermissionError):
                client.set_limit_raw(RAW_PAUSE, expected_raw=166)
            dial.assert_not_called()

    def test_manual_pause_single_write_with_two_readbacks(self):
        client = EvccAscii("example.invalid", manual_test=True)
        responses = [
            FakeSocket(config(166)), FakeSocket(live(166, current=98)),
            FakeSocket(frame(bytes.fromhex("011000140001"), reply=True)),
            FakeSocket(config(RAW_PAUSE)), FakeSocket(live(RAW_PAUSE, current=0, state=0xB2)),
        ]
        with mock.patch.dict(os.environ, {
            "EV_ENABLED": "1", "EVCC_CONTROL_ARMED": "1", "EVCC_MANUAL_TEST": "1",
        }), mock.patch("evcc_ascii.socket.create_connection", side_effect=responses) as dial:
            after = client.set_limit_raw(RAW_PAUSE, expected_raw=166)
        self.assertEqual(after.configured_raw, RAW_PAUSE)
        self.assertEqual(after.pilot_raw, RAW_PAUSE)
        self.assertEqual(dial.call_count, 5)
        self.assertEqual(responses[2].sent[0], frame(bytes.fromhex("0110001400010203E8")))

    def test_resume_requires_extra_explicit_gate(self):
        client = EvccAscii("example.invalid", manual_test=True)
        with mock.patch.dict(os.environ, {"EV_ENABLED": "1", "EVCC_CONTROL_ARMED": "1",
                                         "EVCC_MANUAL_TEST": "1"}, clear=True), \
             mock.patch("evcc_ascii.socket.create_connection") as dial:
            with self.assertRaises(PermissionError):
                client.set_limit_raw(amps_to_raw(6), expected_raw=RAW_PAUSE)
            dial.assert_not_called()

    def test_unknown_write_outcome_is_not_retried(self):
        client = EvccAscii("example.invalid", manual_test=True)
        responses = [FakeSocket(config(166)), FakeSocket(live(166)),
                     FakeSocket(TimeoutError("no write ack"))]
        with mock.patch.dict(os.environ, {"EV_ENABLED": "1", "EVCC_CONTROL_ARMED": "1",
                                         "EVCC_MANUAL_TEST": "1"}), \
             mock.patch("evcc_ascii.socket.create_connection", side_effect=responses) as dial:
            with self.assertRaises(EvccUncertainWrite):
                client.set_limit_raw(RAW_PAUSE, expected_raw=166)
        self.assertEqual(dial.call_count, 3)

    def test_current_round_down_and_limits(self):
        self.assertEqual(amps_to_raw(6), 100)
        self.assertEqual(amps_to_raw(10), 166)
        self.assertEqual(amps_to_raw(16), 266)
        for invalid in (0, 5.9, 16.1, float("nan")):
            with self.assertRaises(ValueError):
                amps_to_raw(invalid)


if __name__ == "__main__":
    unittest.main()
