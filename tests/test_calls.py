import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

TEST_DB = "test_calls.db"
os.environ["DB_PATH"] = TEST_DB

try:
    import deltachat2  # noqa: F401
except ImportError:
    mock_deltachat2 = MagicMock()

    class MsgData:
        def __init__(self, text="", file="", override_sender_name=None):
            self.text = text
            self.file = file
            self.override_sender_name = override_sender_name

    mock_deltachat2.MsgData = MsgData
    sys.modules["deltachat2"] = mock_deltachat2

try:
    import deltabot_cli  # noqa: F401
except ImportError:
    class MockBotCli:
        def __init__(self, *args, **kwargs):
            pass

        def on(self, *args, **kwargs):
            return lambda func: func

        def on_init(self, func):
            return func

        def on_start(self, func):
            return func

        def start(self):
            pass

    mock_deltabot_cli = MagicMock()
    mock_deltabot_cli.BotCli = MockBotCli
    sys.modules["deltabot_cli"] = mock_deltabot_cli

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import calls  # noqa: E402
import database  # noqa: E402
import state  # noqa: E402


class Ev(dict):
    """Core event as deltachat2 delivers it: snake_case keys, attribute access."""
    __getattr__ = dict.__getitem__


try:
    from cmcall import rtc
except Exception:  # aiortc not installed in this environment
    rtc = None


def _cleanup_db():
    database.close_db()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(TEST_DB + suffix):
            os.remove(TEST_DB + suffix)


CONNECTED_SUMMARY = {
    "connected": True,
    "connect_ms": 230,
    "path": {"kind": "relay", "local_type": "relay", "remote_type": "srflx"},
    "audio_received_s": 41.2,
    "voice_s": 12.5,
    "peak_dbfs": -14.0,
    "codec": "opus/48000/2",
    "rtp": {
        "packets_received": 2050, "packets_lost": 3, "loss_pct": 0.15, "jitter_ms": 4.2,
        "packets_sent": 2060, "remote_packets_lost": 0, "rtcp_rtt_ms": 68.4,
    },
}

CMCALL_OK = {
    "ok": True, "stage": None, "error": None,
    "caller_turn": "turn:1.2.3.4:3478 (same host as relay)",
    "callee_turn": "turn:5.6.7.8:3478 (same host as relay)",
    "signaling": {"ring_ms": 812, "accept_ms": 774, "total_ms": 1600},
    "caller_stats": {
        "connected": True, "connect_ms": 96, "path": {"kind": "relay"},
        "echo": {"sent": 6, "received": 6, "rtt_avg_ms": 240.5, "rtt_min_ms": 238.4, "rtt_max_ms": 244.9},
        "rtp": {"loss_pct": 0.0, "jitter_ms": 1.1, "rtcp_rtt_ms": 58.3},
    },
    "callee_stats": {"rtp": {"loss_pct": 0.2, "jitter_ms": 0.9}},
}


class TestReports(unittest.TestCase):
    def test_echo_report_connected(self):
        text = calls.format_echo_report(CONNECTED_SUMMARY, 83, "hangup", None)
        self.assertTrue(text.startswith(calls.REPORT_PREFIX))
        self.assertIn("Duration: 1:23 · connected in 230 ms", text)
        self.assertIn("Path: TURN relay (bot relay ↔ you srflx)", text)
        self.assertIn("voice 12.5 s", text)
        self.assertIn("2050 received, 3 lost (0.1%), jitter 4.2 ms", text)
        self.assertIn("round trip 68 ms", text)
        self.assertNotIn("⚠️", text)

    def test_echo_report_no_voice_and_max_duration(self):
        summary = dict(CONNECTED_SUMMARY, voice_s=0.0)
        text = calls.format_echo_report(summary, 300, "max-duration", None)
        self.assertIn("No voice was detected", text)
        self.assertIn("limited to", text)

    def test_echo_report_hangup_before_connect(self):
        text = calls.format_echo_report({"connected": False, "remote_candidates": {"host": 1}}, 0, "hangup", None)
        self.assertIn("ended before the media connection", text)
        self.assertNotIn("❌", text)

    def test_echo_report_not_connected(self):
        summary = {"connected": False, "remote_candidates": {"host": 2, "srflx": 1},
                   "local_candidates": {"host": 1, "relay": 1}, "trickled_candidates": 3}
        text = calls.format_echo_report(summary, 0, "ice-failed", "no media connection (failed)")
        self.assertIn("❌ Call answered, but no media connection (failed).", text)
        self.assertIn("host 2, srflx 1 (+3 trickled)", text)
        self.assertIn("UDP is blocked", text)

    def test_cmcall_report_ok(self):
        text = calls.format_cmcall_report(CMCALL_OK, "chatmail.uk", "chat.gluek.info")
        self.assertIn("1️⃣ chatmail.uk\n2️⃣ chat.gluek.info", text)
        self.assertIn("Signaling: 1️⃣→2️⃣ 812 ms, 1️⃣←2️⃣ 774 ms", text)
        self.assertIn("Media: TURN relay, connected in 96 ms", text)
        self.assertIn("Echo RTT: avg 240 ms (min 238 / max 245), 6/6 beeps back", text)
        self.assertIn("RTP loss 0.0% / 0.2%", text)
        self.assertNotIn("❌", text)

    def test_cmcall_report_failure_single_relay(self):
        result = {"ok": False, "stage": "signaling", "error": "timeout (60s) waiting for the call"}
        text = calls.format_cmcall_report(result, "chatmail.uk", "chatmail.uk")
        self.assertIn("❌ Failed at signaling: timeout (60s)", text)
        self.assertNotIn("2️⃣", text)


@unittest.skipIf(rtc is None, "cmcall/aiortc not installed")
class TestEchoStun(unittest.TestCase):
    ICE = '[{"urls":["turn:1.2.3.4:3478"],"username":"u","credential":"p"}]'

    def urls(self, mode):
        with patch.object(calls.config, "CALL_ECHO_STUN", mode):
            return rtc.describe_ice_servers(calls.echo_ice_servers(self.ICE))

    def test_auto_uses_turn_server_as_stun(self):
        self.assertEqual(self.urls("auto"), ["stun:1.2.3.4:3478", "turn:1.2.3.4:3478"])

    def test_off_keeps_turn_only(self):
        self.assertEqual(self.urls("off"), ["turn:1.2.3.4:3478"])

    def test_explicit_stun_server(self):
        self.assertEqual(self.urls("stun.example.org:3478")[0], "stun:stun.example.org:3478")


class TestPrivacy(unittest.TestCase):
    def setUp(self):
        _cleanup_db()
        database.DB_PATH = TEST_DB
        database.init_db()

    def tearDown(self):
        _cleanup_db()

    def test_webrtc_libraries_do_not_log_candidates(self):
        import logging

        import config  # noqa: F401 - importing it applies the levels
        for name in ("aioice.ice", "aioice.turn", "aiortc.rtcpeerconnection"):
            self.assertGreaterEqual(logging.getLogger(name).getEffectiveLevel(), logging.WARNING, name)

    def test_prune_call_echo_log(self):
        now = time.time()
        database.add_call_echo_log(10, 42, now - 40 * 86400, 5, True, "relay", 0, 50, 1, 2, "hangup", None)
        database.add_call_echo_log(10, 42, now - 3600, 5, True, "stun", 0, 50, 1, 2, "hangup", None)
        self.assertEqual(database.prune_call_echo_log(now - 30 * 86400), 1)
        rows = database.get_recent_call_echo_logs()
        self.assertEqual([r["path"] for r in rows], ["stun"])

    @patch("calls.dc_helpers._is_dc_admin", return_value=False)
    @patch("calls.dc_helpers._send")
    def test_callstats_states_privacy(self, mock_send, _admin):
        database.add_call_echo_log(10, 42, time.time(), 65, True, "stun", 0.4, 55, 2, 9, "hangup", None)
        event = MagicMock()
        event.msg.from_id = 10
        calls.callstats_command(MagicMock(), 1, event)
        text = mock_send.call_args[0][3]
        self.assertIn("Calls are not recorded; call statistics (no audio, no IP addresses) are kept for 30 days", text)
        self.assertIn("Your last calls:", text)
        with patch.object(calls.config, "CALL_ECHO_LOG_DAYS", 0):
            calls.callstats_command(MagicMock(), 1, event)
        text = mock_send.call_args[0][3]
        self.assertIn("no call history is kept", text)
        self.assertNotIn("Your last calls", text)


class TestRunCmcall(unittest.TestCase):
    @patch("calls.subprocess.run")
    def test_parses_json_after_noise(self, mock_run):
        mock_run.return_value = MagicMock(stdout='warning\n{"ok": true, "stage": null}', stderr="", returncode=0)
        self.assertEqual(calls.run_cmcall(["a.example"]), {"ok": True, "stage": None})
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[1:4], ["--json", "-d", "6"])

    @patch("calls.subprocess.run", side_effect=calls.subprocess.TimeoutExpired("cmcall", 180))
    def test_timeout(self, _mock_run):
        res = calls.run_cmcall(["a.example"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["stage"], "timeout")

    @patch("calls.subprocess.run")
    def test_garbage_output(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", stderr="Traceback...\nBoom", returncode=1)
        res = calls.run_cmcall(["a.example"])
        self.assertFalse(res["ok"])
        self.assertIn("Boom", res["error"])


class TestCommands(unittest.TestCase):
    def setUp(self):
        _cleanup_db()
        database.DB_PATH = TEST_DB
        database.init_db()
        state._chat_cmcall_anti_spam.clear()

    def tearDown(self):
        _cleanup_db()

    def _event(self, payload, chat_id=42, from_id=10):
        event = MagicMock()
        event.payload = payload
        event.msg.chat_id = chat_id
        event.msg.from_id = from_id
        event.msg.id = 99
        return event

    @patch("calls.threading.Thread")
    @patch("calls.dc_helpers._send")
    def test_cmcall_rejects_bad_input(self, mock_send, mock_thread):
        for payload in ("", "a.example b.example c.example", "not_a_domain"):
            calls.cmcall_command(MagicMock(), 1, self._event(payload))
        self.assertEqual(mock_send.call_count, 3)
        self.assertIn("Usage: /cmcall", mock_send.call_args_list[0][0][3])
        self.assertIn("Invalid server domain", mock_send.call_args_list[2][0][3])
        mock_thread.assert_not_called()

    @patch("calls.dc_helpers._is_dc_admin", return_value=False)
    @patch("calls.dc_helpers._react")
    @patch("calls.threading.Thread")
    @patch("calls.dc_helpers._send")
    def test_cmcall_starts_worker_then_cooldown(self, mock_send, mock_thread, _react, _admin):
        calls.cmcall_command(MagicMock(), 1, self._event("chatmail.uk chat.gluek.info"))
        mock_thread.assert_called_once()
        self.assertEqual(mock_thread.call_args[1]["args"][4:], ("chatmail.uk", "chat.gluek.info"))
        calls.cmcall_command(MagicMock(), 1, self._event("chatmail.uk"))
        self.assertEqual(mock_thread.call_count, 1)
        self.assertIn("cooldown", mock_send.call_args[0][3])

    @patch("calls.dc_helpers._is_dc_admin", return_value=False)
    @patch("calls.dc_helpers._send")
    def test_callstats_for_user_shows_own_calls(self, mock_send, _admin):
        database.add_call_echo_log(10, 42, time.time(), 65, True, "stun", 0.4, 55, 2, 9, "hangup", None)
        database.add_call_echo_log(11, 43, time.time(), 5, True, "relay", 0, 40, 1, 2, "hangup", None)
        calls.callstats_command(MagicMock(), 1, self._event("", from_id=10))
        text = mock_send.call_args[0][3]
        self.assertIn("Your last calls:", text)
        self.assertIn("1:05 · peer-to-peer via NAT (STUN) · loss 0.4% · rtt 55 ms", text)
        self.assertNotIn("TURN relay", text)

    @patch("calls.dc_helpers._is_dc_admin", return_value=True)
    @patch("calls.dc_helpers._send")
    def test_callstats_for_admin_aggregates(self, mock_send, _admin):
        database.add_call_echo_log(10, 42, time.time(), 65, True, "relay", 1.0, 60, 2, 9, "hangup", None)
        database.add_call_echo_log(11, 43, time.time(), 0, False, None, None, None, None, None,
                                   "ice-failed", "no media connection (failed)")
        calls.callstats_command(MagicMock(), 1, self._event(""))
        text = mock_send.call_args[0][3]
        self.assertIn("24h: 2 calls, 1 connected, avg loss 1.0%, avg rtt 60 ms (TURN relay 1)", text)
        self.assertIn("#11", text)
        self.assertIn("no media connection", text)


@unittest.skipIf(rtc is None, "cmcall/aiortc not installed")
class TestEchoCallIntegration(unittest.TestCase):
    """Real WebRTC: a cmcall ProbePeer calls the bot's EchoCallManager."""

    def setUp(self):
        _cleanup_db()
        database.DB_PATH = TEST_DB
        database.init_db()
        self.probe_loop = rtc.CallLoop("test-probe")

    def tearDown(self):
        self.probe_loop.stop()
        _cleanup_db()

    def _bot(self):
        bot = MagicMock()
        bot.rpc.ice_servers.return_value = "[]"
        bot.rpc.get_message.return_value = MagicMock(from_id=10)
        self.answers = []
        bot.rpc.accept_incoming_call.side_effect = lambda accid, msg_id, sdp: self.answers.append(sdp)
        return bot

    def _incoming(self, offer, msg_id=500):
        return Ev(kind="IncomingCall", msg_id=msg_id, chat_id=42, place_call_info=offer, has_video=False)

    def test_greeting_is_the_callers_join_tune(self):
        import meet
        bot = self._bot()
        with patch("meet.dc_helpers._get_contact_fingerprint", return_value="AB:CD"):
            frames = calls._caller_tune_frames(bot, 1, 10)
        self.assertEqual(len(frames), len(meet.tune_frames(meet.tune_for("fp:AB:CD"))))
        self.assertIsNone(calls._caller_tune_frames(bot, 1, 0))  # unknown caller -> default greeting

    @patch("calls.dc_helpers._send")
    def test_call_is_echoed_and_reported(self, mock_send):
        bot = self._bot()
        manager = calls.EchoCallManager(bot)
        probe = rtc.ProbePeer([], interval=0.5)
        try:
            offer = self.probe_loop.run(probe.offer(), timeout=20)
            manager.on_incoming_call(1, self._incoming(offer))
            deadline = time.time() + 20
            while not self.answers and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(self.answers, "bot did not answer")
            self.probe_loop.run(probe.accept_answer(self.answers[0]), timeout=10)
            self.assertTrue(self.probe_loop.run(probe.wait_connected(15), timeout=20))
            time.sleep(1.4)  # skip the greeting (the caller's 5-note tune, ~0.85 s)
            self.probe_loop.run(probe.run_probe(2.0), timeout=30)
            echo = self.probe_loop.run(probe.summary(), timeout=10)["echo"]
            self.assertEqual(echo["received"], echo["sent"], echo)

            # caller hangs up -> CallEnded -> report
            self.assertEqual(manager.active_count(), 1)
            manager.on_call_ended(1, Ev(kind="CallEnded", msg_id=500, chat_id=42))
            deadline = time.time() + 15
            while not mock_send.called and time.time() < deadline:
                time.sleep(0.05)
        finally:
            self.probe_loop.run(probe.close(), timeout=10)
            manager.loop.stop()

        text = mock_send.call_args[0][3]
        self.assertEqual(mock_send.call_args[0][2], 42)
        self.assertIn("Echo call report", text)
        self.assertIn("Path: direct", text)
        self.assertIn("Packets from you:", text)
        bot.rpc.end_call.assert_not_called()  # the caller hung up, not the bot
        # the call message (caller's SDP with their IPs) is deleted right away
        bot.rpc.delete_messages.assert_called_once_with(1, [500])
        rows = database.get_recent_call_echo_logs()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["connected"])
        self.assertEqual(rows[0]["end_reason"], "hangup")
        self.assertEqual(manager.active_count(), 0)

    def _connected_call(self, bot, manager, msg_id):
        probe = rtc.ProbePeer([], interval=0.5)
        offer = self.probe_loop.run(probe.offer(), timeout=20)
        manager.on_incoming_call(1, self._incoming(offer, msg_id=msg_id))
        deadline = time.time() + 20
        while not self.answers and time.time() < deadline:
            time.sleep(0.05)
        self.probe_loop.run(probe.accept_answer(self.answers[-1]), timeout=10)
        self.assertTrue(self.probe_loop.run(probe.wait_connected(15), timeout=20))
        time.sleep(1.5)  # let the bot's watchdog reach its media loop
        return probe

    def _wait_report(self, mock_send):
        deadline = time.time() + 20
        while not mock_send.called and time.time() < deadline:
            time.sleep(0.05)
        return mock_send.call_args[0][3]

    @patch("calls.dc_helpers._send")
    def test_hangup_message_after_media_close_is_not_a_drop(self, mock_send):
        bot = self._bot()
        manager = calls.EchoCallManager(bot)
        try:
            probe = self._connected_call(bot, manager, 600)
            hung_up = time.time()
            self.probe_loop.run(probe.close(), timeout=10)   # media goes first...
            time.sleep(2.0)                                   # ...the relay is slower
            manager.on_call_ended(1, Ev(kind="CallEnded", msg_id=600, chat_id=42))
            text = self._wait_report(mock_send)
        finally:
            manager.loop.stop()
        self.assertNotIn("dropped", text)
        row = database.get_recent_call_echo_logs()[0]
        self.assertEqual(row["end_reason"], "hangup")
        # duration ends at the media close, not 2 s later at the hangup message
        accepted_to_hangup = hung_up - row["started_at"]
        self.assertLess(row["duration_s"], accepted_to_hangup + 1.0)

    @patch("calls.dc_helpers._send")
    def test_media_lost_without_hangup_message(self, mock_send):
        bot = self._bot()
        manager = calls.EchoCallManager(bot)
        try:
            with patch.object(calls.config, "CALL_ECHO_HANGUP_GRACE", 1):
                probe = self._connected_call(bot, manager, 601)
                self.probe_loop.run(probe.close(), timeout=10)
                text = self._wait_report(mock_send)
        finally:
            manager.loop.stop()
        self.assertIn("The media connection dropped", text)
        self.assertEqual(database.get_recent_call_echo_logs()[0]["end_reason"], "media-lost")
        bot.rpc.end_call.assert_called_once_with(1, 601)

    @patch("calls.dc_helpers._send")
    def test_no_history_when_log_days_is_zero(self, mock_send):
        bot = self._bot()
        manager = calls.EchoCallManager(bot)
        class FakePeer:
            async def summary(self):
                return dict(CONNECTED_SUMMARY)

            async def close(self):
                pass

        session = calls.EchoSession(1, 501, 42, 10, peer=FakePeer())
        session.accepted_at = time.time() - 30
        try:
            with patch.object(calls.config, "CALL_ECHO_LOG_DAYS", 0):
                manager.finish(session, "hangup")
        finally:
            manager.loop.stop()
        self.assertEqual(database.get_recent_call_echo_logs(), [])
        self.assertIn("Echo call report", mock_send.call_args[0][3])
        bot.rpc.delete_messages.assert_called_once_with(1, [501])

    @patch("calls.dc_helpers._send")
    def test_busy_line_declines(self, mock_send):
        bot = self._bot()
        manager = calls.EchoCallManager(bot)
        try:
            with patch.object(calls.config, "CALL_ECHO_MAX_CONCURRENT", 0):
                manager._answer(1, self._incoming("v=0"))
        finally:
            manager.loop.stop()
        bot.rpc.end_call.assert_called_once_with(1, 500)
        self.assertIn("busy", mock_send.call_args[0][3])
        bot.rpc.accept_incoming_call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
