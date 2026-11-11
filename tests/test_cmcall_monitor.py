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

        def fake_send(bot, accid, chat_id, msg_data):
            msg_id = next(ids)
            self.sent.append((chat_id, msg_id, msg_data.text))
            return msg_id

        self.patches = [
            patch("cmcall_monitor.dc_helpers._get_bot_domains", side_effect=lambda b, a: [self.servers[0]]),
            # Alert messages are sent from inside the outbox job (_edit_or_send -> _send_now)
            patch("cmcall_monitor.dc_helpers._send_now", side_effect=fake_send),
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

    @patch("cmping_commands.relaygeo.whitelisted", return_value=False)
    @patch("cmping_commands.relaygeo.flag", return_value="")
    @patch("cmping_commands.dc_helpers._send")
    @patch("cmping_commands.dc_helpers._get_bot_domains", return_value=["a.example"])
    def test_cmpinglist_shows_call_badges(self, _domains, send, _flag, _wl):
        self.cycle({("a.example", "c.example"): fail(stage="setup", callee_turn="none")})
        event = MagicMock()
        event.payload = ""
        cmping_commands.cmpinglist_command(self.bot, 1, event)
        text = send.call_args[0][3]
        self.assertIn("a.example · 📞✅", text)  # no data: no speed and no circle
        self.assertIn("c.example · 📞➖ no TURN", text)
        self.assertNotIn("no data", text)
        self.assertIn("📞 Calls: checked every 60 min", text)
        self.assertIn("❓ not checked yet", text)
        self.assertNotIn("📞⚪️", text)

    def _list(self, domains, averages, flags=None, listed=(), payload=""):
        flags = flags or {}
        with patch("cmping_commands.dc_helpers._send") as send, \
                patch("cmping_commands.dc_helpers._get_bot_domains", return_value=domains), \
                patch("cmping_commands.relaygeo.flag", side_effect=lambda d: flags.get(d, "")), \
                patch("cmping_commands.relaygeo.country", side_effect=lambda d: {"🇷🇺": "RU", "🇩🇪": "DE"}.get(flags.get(d), "")), \
                patch("cmping_commands.relaygeo.whitelisted", side_effect=lambda d: d in listed), \
                patch("cmping_commands.database.get_all_cmping_monitors", return_value=[]), \
                patch("cmping_commands.database.get_average_ping_for_server", side_effect=lambda d, limit: averages[d]):
            cmping_commands.cmpinglist_command(self.bot, 1, MagicMock(payload=payload))
        return send.call_args[0][3]

    def test_cmpinglist_entry_format(self):
        text = self._list(["a.example", "b.example", "c.example"],
                          {"a.example": (1439.7, 100), "b.example": (870.0, 37), "c.example": (None, 0)},
                          flags={"a.example": "🇷🇺", "b.example": "🇩🇪"})
        self.assertIn("1. 🇷🇺 a.example: 🟢 1.4s · ", text)    # flag before the name, seconds, no count at 100
        self.assertNotIn("🏓 100", text)
        self.assertIn("2. 🇩🇪 b.example: 🟢 0.9s (🏓 37) · ", text)  # fewer than 100 pings: count stays
        self.assertIn("3. c.example · ", text)                 # no data: just the name
        self.assertNotIn(" ms", text)

    def test_cmpinglist_colors_relative_to_median(self):
        avgs = {"a.example": (1500.0, 100), "b.example": (1600.0, 100), "c.example": (1700.0, 100),
                "d.example": (2500.0, 100), "e.example": (4000.0, 100)}
        text = self._list(list(avgs), avgs)       # median 1.7 s: 🟢 ≤ 2.1, 🟡 ≤ 3.4, 🟠 ≤ 5.1
        for line, circle in (("a.example", "🟢"), ("c.example", "🟢"), ("d.example", "🟡"), ("e.example", "🟠")):
            self.assertIn(f"{line}: {circle}", text)
        self.assertIn("🟢 ≤ 2.1s · 🟡 ≤ 3.4s · 🟠 ≤ 5.1s · 🔴 slower (relative to the median of all servers)", text)

    def test_cmpinglist_whitelisted_is_white_whatever_the_speed(self):
        avgs = {"a.example": (800.0, 100), "b.example": (900.0, 100), "c.example": (7000.0, 100)}
        text = self._list(list(avgs), avgs, listed={"a.example", "c.example"})
        self.assertIn("a.example: ⚪️ 0.8s", text)
        self.assertIn("c.example: ⚪️ 7.0s", text)
        self.assertIn("b.example: 🟢 0.9s", text)
        self.assertIn("⚪️ in the RU mobile whitelist (/wl)", text)
        self.assertNotIn("in the RU mobile whitelist", self._list(list(avgs), avgs))

    def test_cmpinglist_no_legend_without_data(self):
        text = self._list(["a.example"], {"a.example": (None, 0)})
        self.assertNotIn("🟢", text)

    def test_cmpinglist_country_filter(self):
        avgs = {"a.example": (1700.0, 100), "b.example": (1700.0, 100), "c.example": (1700.0, 100),
                "d.example": (1700.0, 100)}
        flags = {"a.example": "🇷🇺", "b.example": "🇩🇪", "c.example": "🇷🇺"}   # d.example: country not known yet
        for arg in ("🇷🇺", "ru", " RU "):
            text = self._list(list(avgs), avgs, flags=flags, payload=arg)
            self.assertIn("CMPing Monitor Servers 🇷🇺", text)
            self.assertIn("1. 🇷🇺 a.example: 🟢 1.7s", text)
            self.assertIn("3. 🇷🇺 c.example", text)             # numbers stay those of the full list
            self.assertNotIn("b.example", text)
            self.assertNotIn("d.example", text)
            self.assertIn("Showing 2 of 4 servers", text)
            self.assertIn("Country not known yet for 1", text)
        self.assertIn("No servers in FR", self._list(list(avgs), avgs, flags=flags, payload="fr"))
        self.assertIn("Give a country flag", self._list(list(avgs), avgs, flags=flags, payload="russia"))

    def test_filter_keeps_colors_of_the_full_list(self):
        avgs = {"a.example": (3000.0, 100), "b.example": (1500.0, 100), "c.example": (1500.0, 100)}
        text = self._list(list(avgs), avgs, flags={"a.example": "🇷🇺"}, payload="ru")  # median 1.5 s of ALL
        self.assertIn("a.example: 🟡 3.0s", text)


class TestPingScale(unittest.TestCase):
    def test_relative_scale_and_fixed_for_few_servers(self):
        self.assertEqual(cmping_commands.ping_scale([2000.0, 3000.0]), cmping_commands.FIXED_LIMITS_MS)
        self.assertEqual(cmping_commands.ping_scale([]), cmping_commands.FIXED_LIMITS_MS)
        limits = cmping_commands.ping_scale([2000.0, 2000.0, 8000.0])  # median 2 s
        self.assertEqual(limits, (2500.0, 4000.0, 6000.0))

    def test_circles_and_guards(self):
        limits = cmping_commands.ping_scale([2000.0, 2000.0, 2000.0])  # 🟢 ≤ 2.5s, 🟡 ≤ 4s, 🟠 ≤ 6s
        c = cmping_commands.ping_circle
        self.assertEqual([c(x, limits) for x in (2400, 3900, 5900, 6100)], ["🟢", "🟡", "🟠", "🔴"])
        self.assertEqual(c(9000, limits), "🔴")
        slow = cmping_commands.ping_scale([9000.0, 9000.0, 9000.0])
        self.assertEqual(c(9000, slow), "🟠")        # all slow: never green or yellow
        fast = cmping_commands.ping_scale([200.0, 200.0, 200.0])  # 🟢 ≤ 250 ms
        self.assertEqual(c(900, fast), "🟢")         # under a second is always green
        self.assertEqual(c(1100, fast), "🔴")


if __name__ == "__main__":
    unittest.main()
