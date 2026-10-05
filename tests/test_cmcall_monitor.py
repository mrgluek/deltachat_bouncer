import itertools
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

TEST_DB = "test_cmcall_monitor.db"
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

import cmcall_monitor as mon  # noqa: E402
import cmping_commands  # noqa: E402
import database  # noqa: E402
import state  # noqa: E402

REPORT_CHAT = 77


def ok(loss=0.2, rtt=240.0, caller_turn="turn:1.1.1.1:3478", callee_turn="turn:2.2.2.2:3478"):
    return {
        "ok": True, "stage": None, "error": None,
        "caller_turn": caller_turn, "callee_turn": callee_turn,
        "signaling": {"total_ms": 1800},
        "caller_stats": {"echo": {"rtt_avg_ms": rtt}, "rtp": {"loss_pct": loss, "jitter_ms": 2.0}},
        "callee_stats": {"rtp": {"loss_pct": loss / 2, "jitter_ms": 1.0}},
    }


def fail(stage="ice", error="ICE/DTLS did not connect (failed)", **turns):
    res = {"ok": False, "stage": stage, "error": error,
           "caller_turn": "turn:1.1.1.1:3478", "callee_turn": "turn:2.2.2.2:3478"}
    res.update(turns)
    return res


class MonitorTestBase(unittest.TestCase):
    servers = ["a.example", "b.example", "c.example"]

    def setUp(self):
        database.close_db()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(TEST_DB + suffix):
                os.remove(TEST_DB + suffix)
        database.DB_PATH = TEST_DB
        database.init_db()
        database.add_cmping_report_chat(REPORT_CHAT)
        state._cmcall_monitor_index = 0
        state._cmcall_monitor_running = False
        self.bot = MagicMock()
        self.sent = []
        ids = itertools.count(1000)

        def fake_send(bot, accid, chat_id, text, reply_to_id=None):
            msg_id = next(ids)
            self.sent.append((chat_id, msg_id, text))
            return msg_id

        self.patches = [
            patch("cmcall_monitor.dc_helpers._get_bot_domains", side_effect=lambda b, a: [self.servers[0]]),
            patch("cmcall_monitor.dc_helpers._send", side_effect=fake_send),
        ]
        for p in self.patches:
            p.start()
        for s in self.servers[1:]:
            database.add_cmping_monitor(s)
        self.calls = []

    def tearDown(self):
        for p in self.patches:
            p.stop()
        database.close_db()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(TEST_DB + suffix):
                os.remove(TEST_DB + suffix)

    def cycle(self, outcomes):
        """outcomes: {(caller, callee): result} or a callable; default ok()."""
        def run(a, b):
            self.calls.append((a, b))
            res = outcomes(a, b) if callable(outcomes) else outcomes.get((a, b), ok())
            return dict(res, checked_at=1_800_000_000 + len(self.calls))
        mon.cmcall_monitor_cycle(self.bot, 1, run=run)

    def status(self, server):
        return database.get_cmcall_server(server).get("status")

    def edits(self):
        return [c.args for c in self.bot.rpc.send_edit_request.call_args_list]


class TestCycle(MonitorTestBase):
    def test_round_robin_and_alternating_direction(self):
        self.cycle({})
        self.assertEqual(self.calls, [("a.example", "b.example"), ("a.example", "c.example")])
        self.calls.clear()
        self.cycle({})  # source b, odd cycle: targets call the source
        self.assertEqual(self.calls, [("a.example", "b.example"), ("c.example", "b.example")])
        for s in self.servers:
            self.assertEqual(self.status(s), "ok")
        self.assertEqual(self.sent, [])
        rows = database.get_cmcall_results()
        self.assertEqual(len(rows), 3)  # a->b stored once (latest), a->c, c->b
        self.assertEqual(database.get_cmcall_server("a.example")["turn"], "turn:1.1.1.1:3478")

    def test_failing_target_alerts_and_recovers(self):
        self.cycle({("a.example", "b.example"): fail()})
        self.assertEqual(self.status("b.example"), "down")
        self.assertEqual(self.status("a.example"), "ok")
        self.assertEqual(len(self.sent), 1)
        chat_id, msg_id, text = self.sent[0]
        self.assertEqual(chat_id, REPORT_CHAT)
        self.assertIn("Calls failing: b.example", text)
        self.assertIn("ice: ICE/DTLS did not connect", text)

        state._cmcall_monitor_index = 0
        self.cycle(lambda a, b: ok())
        self.assertEqual(self.status("b.example"), "ok")
        self.assertEqual(len(self.sent), 1)  # resolution edits the alert in place
        edit = self.edits()[-1]
        self.assertEqual(edit[1], msg_id)
        self.assertIn("Calls restored: b.example", edit[2])
        self.assertEqual(database.get_open_cmcall_events(), [])

    def test_source_failing_with_all_targets_is_blamed(self):
        self.cycle(lambda a, b: fail(stage="setup", error="timeout (45s) waiting for x to go online"))
        self.assertEqual(self.status("a.example"), "down")
        self.assertIsNone(self.status("b.example"))
        self.assertIsNone(self.status("c.example"))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Calls failing: a.example", self.sent[0][2])

    def test_relay_without_turn_is_na_without_alert(self):
        self.cycle({("a.example", "c.example"): fail(stage="setup", error="no TURN", callee_turn="none")})
        self.assertEqual(self.status("c.example"), "n/a")
        self.assertEqual(self.status("b.example"), "ok")
        self.assertEqual(self.sent, [])
        self.assertEqual(mon.server_badge("c.example"), "📞➖ no TURN")

    def test_degraded_after_two_lossy_checks(self):
        lossy = {("a.example", "b.example"): ok(loss=15.0)}
        self.cycle(lossy)
        self.assertEqual(self.status("b.example"), "ok")
        self.assertEqual(self.sent, [])
        state._cmcall_monitor_index = 0
        database.set_config("cmcall_monitor_cycle", "0")
        self.cycle(lossy)
        self.assertEqual(self.status("b.example"), "degraded")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Calls degraded: b.example", self.sent[0][2])
        self.assertIn("packet loss 15.0% in 2 checks in a row", self.sent[0][2])
        # degraded -> down closes the degraded episode and opens a failing one
        state._cmcall_monitor_index = 0
        database.set_config("cmcall_monitor_cycle", "0")
        self.cycle({("a.example", "b.example"): fail()})
        self.assertEqual(self.status("b.example"), "down")
        self.assertIn("Calls restored: b.example", self.edits()[-1][2])
        self.assertIn("Calls failing: b.example", self.sent[-1][2])

    def test_skip_excludes_server_and_resolves_its_alert(self):
        self.cycle({("a.example", "b.example"): fail()})
        msg_id = self.sent[0][1]
        database.add_cmcall_skip("b.example")
        self.calls.clear()
        state._cmcall_monitor_index = 0
        self.cycle({})
        self.assertEqual(self.calls, [("c.example", "a.example")])
        self.assertEqual(self.edits()[-1][1], msg_id)
        self.assertIn("Calls restored", self.edits()[-1][2])
        self.assertEqual(mon.server_badge("b.example"), "📞⏭")


class TestSingleServer(MonitorTestBase):
    servers = ["solo.example"]

    def test_lone_relay_calls_itself(self):
        self.cycle({("solo.example", "solo.example"): fail(stage="media", error="no echo")})
        self.assertEqual(self.calls, [("solo.example", "solo.example")])
        self.assertEqual(self.status("solo.example"), "down")
        self.assertIn("Calls failing: solo.example", self.sent[0][2])


class TestRetry(unittest.TestCase):
    @patch("cmcall_monitor.calls.run_cmcall")
    def test_hard_failure_is_retried_once(self, run):
        run.side_effect = [fail(), ok()]
        sleeps = []
        res = mon.run_monitor_single("a.example", "b.example", sleep=sleeps.append)
        self.assertTrue(res["ok"])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(sleeps, [10])
        self.assertEqual(run.call_args[0][0], ["a.example", "b.example"])
        self.assertIn("checked_at", res)

    @patch("cmcall_monitor.calls.run_cmcall")
    def test_no_turn_is_not_retried_and_self_call_uses_one_relay(self, run):
        run.return_value = fail(stage="setup", caller_turn="none", callee_turn="none")
        mon.run_monitor_single("a.example", "a.example", sleep=lambda s: None)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args[0][0], ["a.example"])


class TestCommands(MonitorTestBase):
    def test_status_and_history_formatting(self):
        self.cycle({("a.example", "b.example"): fail()})
        servers = self.servers
        text = mon.format_status(servers, [], database.get_cmcall_results(), now=1_900_000_000)
        self.assertIn("🚨 b.example: down", text)
        self.assertIn("✅ a.example: ok", text)
        self.assertIn("TURN turn:1.1.1.1:3478", text)
        self.assertIn("❌ a.example → b.example", text)
        self.assertIn("✅ a.example → c.example", text)
        self.assertIn("rtt 240 ms · loss 0.2% · signaling 1.8s", text)
        filtered = mon.format_status(servers, [], database.get_cmcall_results(), server_filter="c.ex")
        self.assertNotIn("b.example: down", filtered)
        hist = mon.format_history(database.get_cmcall_events())
        self.assertIn("🚨 b.example down", hist)
        self.assertIn("ongoing", hist)

    def test_skip_commands_need_admin(self):
        event = MagicMock()
        event.payload = "b.example"
        with patch("cmcall_monitor.dc_helpers._is_dc_admin", return_value=False):
            mon.cmcallskip_command(self.bot, 1, event)
        self.assertEqual(database.get_cmcall_skipped(), [])
        self.assertIn("Only the bot administrator", self.sent[-1][2])
        with patch("cmcall_monitor.dc_helpers._is_dc_admin", return_value=True):
            mon.cmcallskip_command(self.bot, 1, event)
            self.assertEqual(database.get_cmcall_skipped(), ["b.example"])
            mon.cmcallunskip_command(self.bot, 1, event)
        self.assertEqual(database.get_cmcall_skipped(), [])

    @patch("cmping_commands.dc_helpers._send")
    @patch("cmping_commands.dc_helpers._get_bot_domains", return_value=["a.example"])
    def test_cmpinglist_shows_call_badges(self, _domains, send):
        self.cycle({("a.example", "c.example"): fail(stage="setup", callee_turn="none")})
        event = MagicMock()
        cmping_commands.cmpinglist_command(self.bot, 1, event)
        text = send.call_args[0][3]
        self.assertIn("a.example: ⚪️ no data · 📞✅", text)
        self.assertIn("c.example: ⚪️ no data · 📞➖ no TURN", text)
        self.assertIn("📞 Calls: checked every 60 min", text)


if __name__ == "__main__":
    unittest.main()
