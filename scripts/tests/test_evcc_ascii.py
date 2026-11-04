"""Offline EVCC2 protocol and manual-write gate tests. No network or hardware."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))
from evcc_ascii import (
    EvccAscii, EvccError, EvccUncertainWrite, RAW_PAUSE,
    PAUSED_PILOT_RAW, observed_evcc_mode, amps_to_raw, frame, parse_reply,
)


def reply_read(regs):
    return frame(bytes([1, 3, len(regs) * 2]) +
                 b"".join(v.to_bytes(2, "big") for v in regs), reply=True)


def config(raw):
    return reply_read([0x0F00, 0, 0, raw, 0])


def live(raw, current=100, state=0xC2, side_current=0):
    return reply_read([0x2E00 | state, 0x8000 | raw, side_current, current, side_current])


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
        answers = [FakeSocket(config(1000)), FakeSocket(live(1000, current=1000, state=0xA1, side_current=1000))]
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
            FakeSocket(config(RAW_PAUSE)), FakeSocket(live(PAUSED_PILOT_RAW, current=1, state=0xB2)),
        ]
        with mock.patch.dict(os.environ, {
            "EV_ENABLED": "1", "EVCC_CONTROL_ARMED": "1", "EVCC_MANUAL_TEST": "1",
        }), mock.patch("evcc_ascii.socket.create_connection", side_effect=responses) as dial:
            after = client.set_limit_raw(RAW_PAUSE, expected_raw=166)
        self.assertEqual(after.configured_raw, RAW_PAUSE)
        self.assertEqual(after.pilot_raw, PAUSED_PILOT_RAW)
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

    def test_scheduled_control_has_separate_owner_gate_from_manual_test(self):
        client = EvccAscii("example.invalid", scheduled_control=True)
        with mock.patch.dict(os.environ, {
            "EV_ENABLED": "1", "EVCC_CONTROL_ARMED": "1",
            "EVCC_MANUAL_TEST": "1", "EVCC_PRICE_CONTROL_ARMED": "0",
        }, clear=True), mock.patch("evcc_ascii.socket.create_connection") as dial:
            with self.assertRaises(PermissionError):
                client.set_limit_raw(RAW_PAUSE, expected_raw=100, scheduled=True)
            with self.assertRaises(PermissionError):
                client.set_limit_raw(RAW_PAUSE, expected_raw=100)
            dial.assert_not_called()

    def test_scheduled_pause_uses_single_cas_fc10_and_readback(self):
        client = EvccAscii("example.invalid", scheduled_control=True)
        responses = [
            FakeSocket(config(100)), FakeSocket(live(100, current=60)),
            FakeSocket(frame(bytes.fromhex("011000140001"), reply=True)),
            FakeSocket(config(RAW_PAUSE)), FakeSocket(live(PAUSED_PILOT_RAW, current=1, state=0xB2)),
        ]
        with mock.patch.dict(os.environ, {
            "EV_ENABLED": "1", "EVCC_CONTROL_ARMED": "1",
            "EVCC_PRICE_CONTROL_ARMED": "1",
        }, clear=True), mock.patch("evcc_ascii.socket.create_connection", side_effect=responses) as dial:
            after = client.set_limit_raw(RAW_PAUSE, expected_raw=100, scheduled=True)
        self.assertEqual(after.configured_raw, RAW_PAUSE)
        self.assertEqual(dial.call_count, 5)

    def test_scheduled_resume_requires_start_approval(self):
        client = EvccAscii("example.invalid", scheduled_control=True)
        with mock.patch.dict(os.environ, {
            "EV_ENABLED": "1", "EVCC_CONTROL_ARMED": "1",
            "EVCC_PRICE_CONTROL_ARMED": "1",
        }, clear=True), mock.patch("evcc_ascii.socket.create_connection") as dial:
            with self.assertRaises(PermissionError):
                client.set_limit_raw(100, expected_raw=RAW_PAUSE, scheduled=True)
            dial.assert_not_called()

    def test_observed_modes_are_strictly_cp_pilot_and_phase_bound(self):
        self.assertEqual(observed_evcc_mode(
            "C2", 100, 100, (0.1, 6.0, 0.1)), "charging_6a")
        self.assertEqual(observed_evcc_mode(
            "B2", RAW_PAUSE, PAUSED_PILOT_RAW, (0.1, 0.1, 0.1)),
            "paused_connected")
        for state, cfg, pilot, currents in (
            ("A1", RAW_PAUSE, PAUSED_PILOT_RAW, (0.1, 0.1, 0.1)),
            ("C2", RAW_PAUSE, PAUSED_PILOT_RAW, (0.1, 0.1, 0.1)),
            ("B2", RAW_PAUSE, RAW_PAUSE, (0.1, 0.1, 0.1)),
            ("B2", RAW_PAUSE, 266, (0.1, 0.1, 0.1)),
            ("B2", RAW_PAUSE, PAUSED_PILOT_RAW, (0.1, 5.5, 0.1)),
            ("B2", RAW_PAUSE, PAUSED_PILOT_RAW, (0.4, 0.1, 0.1)),
            ("B2", RAW_PAUSE, PAUSED_PILOT_RAW, (None, 0.1, 0.1)),
            ("C2", 100, 100, (0.1, 0.0, 0.1)),
            ("C2", 100, RAW_PAUSE, (0.1, 6.0, 0.1)),
            ("B2", 100, 100, (0.1, 0.1, 0.1)),
            ("C2", 100, 100, (0.1, float("nan"), 0.1)),
        ):
            with self.subTest(state=state, config=cfg, pilot=pilot, currents=currents):
                self.assertIsNone(observed_evcc_mode(state, cfg, pilot, currents))

    def test_scheduled_pause_refuses_changed_cp_before_fc10(self):
        client = EvccAscii("example.invalid", scheduled_control=True)
        responses = [FakeSocket(config(100)), FakeSocket(live(100, current=0, state=0xB2))]
        with mock.patch.dict(os.environ, {
            "EV_ENABLED": "1", "EVCC_CONTROL_ARMED": "1",
            "EVCC_PRICE_CONTROL_ARMED": "1",
        }, clear=True), mock.patch(
            "evcc_ascii.socket.create_connection", side_effect=responses
        ) as dial:
            with self.assertRaises(EvccError):
                client.set_limit_raw(RAW_PAUSE, expected_raw=100, scheduled=True)
        self.assertEqual(dial.call_count, 2)

    def test_scheduled_resume_requires_confirmed_b2_1000_267(self):
        client = EvccAscii("example.invalid", scheduled_control=True)
        valid = [
            FakeSocket(config(RAW_PAUSE)), FakeSocket(live(PAUSED_PILOT_RAW, current=1, state=0xB2)),
            FakeSocket(frame(bytes.fromhex("011000140001"), reply=True)),
            FakeSocket(config(100)), FakeSocket(live(100, current=60, state=0xC2)),
        ]
        with mock.patch.dict(os.environ, {
            "EV_ENABLED": "1", "EVCC_CONTROL_ARMED": "1",
            "EVCC_PRICE_CONTROL_ARMED": "1", "EVCC_START_APPROVED": "1",
        }, clear=True), mock.patch(
            "evcc_ascii.socket.create_connection", side_effect=valid
        ) as dial:
            after = client.set_limit_raw(100, expected_raw=RAW_PAUSE, scheduled=True)
        self.assertEqual((after.state, after.configured_raw, after.pilot_raw), ("C2", 100, 100))
        self.assertEqual(dial.call_count, 5)

        # An unplugged CP or a changed pilot can never reach FC10.
        for state, pilot in ((0xA1, PAUSED_PILOT_RAW), (0xB2, 1000), (0xB2, 100)):
            with self.subTest(state=state, pilot=pilot):
                responses = [
                    FakeSocket(config(RAW_PAUSE)),
                    FakeSocket(live(pilot, current=1, state=state)),
                ]
                with mock.patch.dict(os.environ, {
                    "EV_ENABLED": "1", "EVCC_CONTROL_ARMED": "1",
                    "EVCC_PRICE_CONTROL_ARMED": "1", "EVCC_START_APPROVED": "1",
                }, clear=True), mock.patch(
                    "evcc_ascii.socket.create_connection", side_effect=responses
                ) as dial:
                    with self.assertRaises(EvccError):
                        client.set_limit_raw(100, expected_raw=RAW_PAUSE, scheduled=True)
                self.assertEqual(dial.call_count, 2)

    def test_resume_readback_b2_is_pending_not_6a_verified(self):
        self.assertIsNone(observed_evcc_mode(
            "B2", 100, 100, (0.1, 0.1, 0.1)))
        client = EvccAscii("example.invalid", scheduled_control=True)
        responses = [
            FakeSocket(config(RAW_PAUSE)),
            FakeSocket(live(PAUSED_PILOT_RAW, current=1, state=0xB2)),
            FakeSocket(frame(bytes.fromhex("011000140001"), reply=True)),
            FakeSocket(config(100)), FakeSocket(live(100, current=1, state=0xB2)),
        ]
        with mock.patch.dict(os.environ, {
            "EV_ENABLED": "1", "EVCC_CONTROL_ARMED": "1",
            "EVCC_PRICE_CONTROL_ARMED": "1", "EVCC_START_APPROVED": "1",
        }, clear=True), mock.patch(
            "evcc_ascii.socket.create_connection", side_effect=responses
        ) as dial:
            after = client.set_limit_raw(100, expected_raw=RAW_PAUSE, scheduled=True)
        self.assertEqual((after.state, after.configured_raw), ("B2", 100))
        self.assertEqual(dial.call_count, 5)

    def test_unplug_during_scheduled_resume_is_uncertain_no_retry(self):
        client = EvccAscii("example.invalid", scheduled_control=True)
        responses = [
            FakeSocket(config(RAW_PAUSE)),
            FakeSocket(live(PAUSED_PILOT_RAW, current=1, state=0xB2)),
            FakeSocket(frame(bytes.fromhex("011000140001"), reply=True)),
            FakeSocket(config(100)),
            FakeSocket(live(100, current=1000, state=0xA1, side_current=1000)),
        ]
        with mock.patch.dict(os.environ, {
            "EV_ENABLED": "1", "EVCC_CONTROL_ARMED": "1",
            "EVCC_PRICE_CONTROL_ARMED": "1", "EVCC_START_APPROVED": "1",
        }, clear=True), mock.patch(
            "evcc_ascii.socket.create_connection", side_effect=responses
        ) as dial:
            with self.assertRaises(EvccUncertainWrite):
                client.set_limit_raw(100, expected_raw=RAW_PAUSE, scheduled=True)
        self.assertEqual(dial.call_count, 5)

    def test_fc10_pause_waits_for_physical_cp_settling_without_second_write(self):
        client = EvccAscii("example.invalid", scheduled_control=True)
        sockets = [
            FakeSocket(config(100)), FakeSocket(live(100, current=60)),
            FakeSocket(frame(bytes.fromhex("011000140001"), reply=True)),
            # FC10 accepted, but car has not opened its contactor yet.
            FakeSocket(config(RAW_PAUSE)),
            FakeSocket(live(PAUSED_PILOT_RAW, current=60, state=0xC2)),
            FakeSocket(config(RAW_PAUSE)),
            FakeSocket(live(PAUSED_PILOT_RAW, current=1, state=0xB2)),
        ]
        with mock.patch.dict(os.environ, {
            "EV_ENABLED": "1", "EVCC_CONTROL_ARMED": "1",
            "EVCC_PRICE_CONTROL_ARMED": "1",
        }, clear=True), mock.patch(
            "evcc_ascii.socket.create_connection", side_effect=sockets
        ) as dial, mock.patch("evcc_ascii.time.sleep") as delay:
            after = client.set_limit_raw(RAW_PAUSE, expected_raw=100, scheduled=True)
        self.assertEqual((after.state, after.configured_raw, after.pilot_raw),
                         ("B2", RAW_PAUSE, PAUSED_PILOT_RAW))
        self.assertEqual(dial.call_count, 7)
        delay.assert_called_once()
        self.assertEqual(
            sum(b"0110" in sock.sent[0] for sock in sockets), 1)

    def test_fc10_pause_transition_timeout_never_retries_write(self):
        client = EvccAscii("example.invalid", scheduled_control=True)
        sockets = [
            FakeSocket(config(100)), FakeSocket(live(100, current=60)),
            FakeSocket(frame(bytes.fromhex("011000140001"), reply=True)),
            FakeSocket(config(RAW_PAUSE)),
            FakeSocket(live(PAUSED_PILOT_RAW, current=60, state=0xC2)),
        ]
        with mock.patch.dict(os.environ, {
            "EV_ENABLED": "1", "EVCC_CONTROL_ARMED": "1",
            "EVCC_PRICE_CONTROL_ARMED": "1",
        }, clear=True), mock.patch(
            "evcc_ascii.socket.create_connection", side_effect=sockets
        ) as dial, mock.patch("evcc_ascii.time.monotonic", side_effect=[0, 10]):
            with self.assertRaises(EvccUncertainWrite):
                client.set_limit_raw(RAW_PAUSE, expected_raw=100, scheduled=True)
        self.assertEqual(dial.call_count, 5)
        self.assertEqual(sum(b"0110" in sock.sent[0] for sock in sockets), 1)

    def test_pause_bad_pilot_readback_is_uncertain_without_retry(self):
        client = EvccAscii("example.invalid", scheduled_control=True)
        responses = [
            FakeSocket(config(100)), FakeSocket(live(100, current=60)),
            FakeSocket(frame(bytes.fromhex("011000140001"), reply=True)),
            FakeSocket(config(RAW_PAUSE)), FakeSocket(live(RAW_PAUSE, current=1, state=0xB2)),
        ]
        with mock.patch.dict(os.environ, {
            "EV_ENABLED": "1", "EVCC_CONTROL_ARMED": "1",
            "EVCC_PRICE_CONTROL_ARMED": "1",
        }, clear=True), mock.patch(
            "evcc_ascii.socket.create_connection", side_effect=responses
        ) as dial:
            with self.assertRaises(EvccUncertainWrite):
                client.set_limit_raw(RAW_PAUSE, expected_raw=100, scheduled=True)
        self.assertEqual(dial.call_count, 5)

    def test_current_round_down_and_limits(self):
        self.assertEqual(amps_to_raw(6), 100)
        self.assertEqual(amps_to_raw(10), 166)
        self.assertEqual(amps_to_raw(16), 266)
        for invalid in (0, 5.9, 16.1, float("nan")):
            with self.assertRaises(ValueError):
                amps_to_raw(invalid)


if __name__ == "__main__":
    unittest.main()
