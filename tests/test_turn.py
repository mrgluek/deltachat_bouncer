"""Callers' TURN servers (turn_names.py, echo reports, /callstats) and /test-turn.

Run: python3 -m unittest tests.test_turn -v
"""
import json
import os
import sqlite3
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

from tests import test_calls  # noqa: F401  (installs the deltachat2/deltabot_cli mocks if needed)

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import calls  # noqa: E402
import config  # noqa: E402
import database  # noqa: E402
import state  # noqa: E402
import turn_names  # noqa: E402
from web.templates import turn_test as tpl_turn_test  # noqa: E402

TEST_DB = "test_turn.db"

OFFER = """v=0
o=- 1 2 IN IP4 127.0.0.1
s=-
m=audio 9 UDP/TLS/RTP/SAVPF 111
a=candidate:1 1 udp 2122260223 192.168.1.20 50000 typ host
a=candidate:2 1 udp 1686052607 203.0.113.9 50000 typ srflx raddr 192.168.1.20 rport 50000
a=candidate:3 1 udp 41885439 46.224.39.230 61000 typ relay raddr 203.0.113.9 rport 50000
a=candidate:4 1 udp 41885183 2a01:4f8:c014:9593:0:0:0:1 61001 typ relay raddr 2001:db8::5 rport 50001
a=candidate:5 1 udp 41885439 46.224.39.230 61002 typ relay raddr 203.0.113.9 rport 50002
a=candidate:6 1 tcp 41885439 185.68.247.62 61003 typ relay raddr 203.0.113.9 rport 50003 tcptype passive
"""

NAMES = {
    "46.224.39.230": "turn.delta.chat",
    "2a01:4f8:c014:9593::1": "turn.delta.chat",
    "185.68.247.62": "chatmail.uk",
}


def _cleanup_db():
    database.close_db()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(TEST_DB + suffix):
            os.remove(TEST_DB + suffix)


class TestRelayAddresses(unittest.TestCase):
    def test_relay_candidates_from_sdp(self):
        ips = turn_names.relay_ips_from_sdp(OFFER)
        # unique, normalized; host/srflx candidates and raddr (the caller's own
        # addresses) are never picked up
        self.assertEqual(ips, ["46.224.39.230", "2a01:4f8:c014:9593::1", "185.68.247.62"])
        self.assertEqual(turn_names.relay_ips_from_sdp(None), [])
        self.assertEqual(turn_names.relay_ips_from_sdp("candidate:1 1 udp 1 [::1] 5 typ relay"), ["::1"])

    def test_relay_candidates_from_aioice(self):
        from aioice import Candidate

        cands = [
            Candidate("1", 1, "udp", 1, "192.168.1.20", 5000, "host"),
            Candidate("2", 1, "udp", 1, "46.224.39.230", 6000, "relay",
                      related_address="203.0.113.9", related_port=5000),
            Candidate("3", 1, "udp", 1, "46.224.39.230", 6001, "relay"),
        ]
        self.assertEqual(turn_names.relay_ips_from_candidates(cands), ["46.224.39.230"])

    def test_names(self):
        self.assertEqual(turn_names.name_relays(["46.224.39.230", "2a01:4f8:c014:9593::1", "198.51.100.1",
                                                 "185.68.247.62"], NAMES),
                         ["turn.delta.chat", "other", "chatmail.uk"])
        self.assertEqual(turn_names.caller_turn([]), [])


class TestResolution(unittest.TestCase):
    def setUp(self):
        turn_names._cache.update(at=0.0, hosts=(), names={})

    def test_forward_dns_first_name_wins_and_is_cached(self):
        answers = {"turn.delta.chat": {"46.224.39.230"}, "chatmail.uk": {"185.68.247.62", "46.224.39.230"},
                   "slow.example": set()}
        with patch.object(turn_names, "_resolve", side_effect=lambda h: answers[h]) as res:
            hosts = ["turn.delta.chat", "chatmail.uk", "slow.example"]
            names = turn_names.ip_names(hosts)
            self.assertEqual(names, {"46.224.39.230": "turn.delta.chat", "185.68.247.62": "chatmail.uk"})
            turn_names.ip_names(hosts)
            self.assertEqual(res.call_count, 3)  # second call served from the cache

    def test_slow_dns_does_not_block(self):
        def resolve(h):
            if h == "slow.example":
                time.sleep(3)
            return {"46.224.39.230"} if h == "turn.delta.chat" else set()

        with patch.object(turn_names, "RESOLVE_TIMEOUT_S", 0.5), patch.object(turn_names, "_resolve", side_effect=resolve):
            t0 = time.time()
            names = turn_names.ip_names(["slow.example", "turn.delta.chat"])
            self.assertLess(time.time() - t0, 2)
        self.assertEqual(names, {"46.224.39.230": "turn.delta.chat"})

    def test_known_hosts(self):
        bot = MagicMock()
        bot.rpc.list_transports.return_value = [{"addr": "bot@chatmail.uk"}, {"addr": "bot@chat.gluek.info"}]
        with patch.object(database, "get_all_cmping_monitors", return_value=["nine.testrun.org", "chatmail.uk"]), \
                patch.object(config, "CALL_TURN_HOSTS", ["turn.example.org"]):
            hosts = turn_names.known_hosts(bot, 1)
        self.assertEqual(hosts, ["turn.delta.chat", "turn.example.org", "chatmail.uk", "chat.gluek.info",
                                 "nine.testrun.org"])


class TestReports(unittest.TestCase):
    def test_turn_line_in_reports(self):
        connected = dict(test_calls.CONNECTED_SUMMARY)
        text = calls.format_echo_report(connected, 42, "hangup", None, ["turn.delta.chat"])
        self.assertIn("Your app's TURN server: turn.delta.chat ✅", text)
        text = calls.format_echo_report(connected, 42, "hangup", None, ["chatmail.uk", "other"])
        self.assertIn("Your app's TURN server: chatmail.uk, another TURN server ✅", text)
        text = calls.format_echo_report(connected, 42, "hangup", None, None)
        self.assertNotIn("TURN server", text)  # unknown (e.g. older calls): nothing claimed

        with patch.object(calls, "turn_test_url", return_value="https://dc.example.org/test-turn"):
            text = calls.format_echo_report(connected, 42, "hangup", None, [])
            self.assertIn("ℹ️ Your app got no address from its TURN server", text)
            self.assertIn("https://dc.example.org/test-turn", text)
            failed = {"connected": False, "remote_candidates": {"host": 2, "srflx": 1}}
            text = calls.format_echo_report(failed, 0, "ice-failed", "no media connection (failed)", [])
            self.assertIn("⚠️ Your app got no address from its TURN server", text)
            self.assertIn("turn.delta.chat", text)
            self.assertNotIn("Usually this means", text)
            text = calls.format_echo_report({"connected": False}, 0, "hangup", None, [])
            self.assertIn("no address from its TURN server", text)
        text = calls.format_echo_report(failed, 0, "ice-failed", "x", None)
        self.assertIn("Usually this means UDP is blocked", text)

    def test_turn_test_url(self):
        with patch.object(calls.database, "get_config", return_value="dc.example.org/"):
            self.assertEqual(calls.turn_test_url(), "https://dc.example.org/test-turn")
        with patch.object(calls.database, "get_config", return_value=None), patch.dict(os.environ, {"BASE_URL": ""}):
            self.assertIsNone(calls.turn_test_url())
        with patch.object(calls.database, "get_config", return_value="https://dc.example.org"), \
                patch.object(config, "TURN_TEST_PAGE", False):
            self.assertIsNone(calls.turn_test_url())


class TestCallLog(unittest.TestCase):
    def setUp(self):
        _cleanup_db()
        database.DB_PATH = TEST_DB
        database.init_db()

    def tearDown(self):
        _cleanup_db()

    def _log(self, turn, domain, connected=True, ago=60):
        database.add_call_echo_log(10, 42, time.time() - ago, 30, connected, "stun", 0.1, 50, 2, 10,
                                   "hangup", None, caller_turn=turn, caller_domain=domain)

    def test_aggregate_and_admin_line(self):
        self._log("turn.delta.chat", "gmail.com")
        self._log("chatmail.uk", "chatmail.uk")
        self._log("chatmail.uk,turn.delta.chat", "chatmail.uk")
        self._log("", "gmail.com", connected=False)
        self._log("", "gmail.com", connected=False)
        self._log("", "yandex.ru")
        self._log(None, None)                       # logged before 2.25.0
        self._log("", "old.example", ago=8 * 86400)  # outside the window
        agg = database.get_call_echo_turn_aggregate(time.time() - 7 * 86400)
        self.assertEqual(agg["calls"], 6)
        self.assertEqual(agg["turn"], {"chatmail.uk": 2, "turn.delta.chat": 2})
        self.assertEqual(agg["none"], 3)
        self.assertEqual(agg["none_domains"], {"gmail.com": 2, "yandex.ru": 1})
        line = calls.format_turn_aggregate(agg)
        self.assertEqual(line, "Callers' TURN (7d): chatmail.uk 2/6, turn.delta.chat 2/6 · "
                               "no TURN address: 3/6 (gmail.com 2, yandex.ru 1)")
        self.assertIsNone(calls.format_turn_aggregate(database.get_call_echo_turn_aggregate(time.time() + 10)))

        rows = database.get_recent_call_echo_logs(limit=10)
        lines = [calls.format_call_log_line(r) for r in rows]
        self.assertTrue(any("TURN chatmail.uk, turn.delta.chat" in x for x in lines))
        self.assertTrue(any(x.endswith("TURN none") and "❌" in x for x in lines))
        self.assertTrue(any("TURN" not in x for x in lines))  # unknown for old rows

    def test_old_table_is_migrated(self):
        _cleanup_db()
        conn = sqlite3.connect(TEST_DB)
        conn.execute("CREATE TABLE call_echo_log (id INTEGER PRIMARY KEY AUTOINCREMENT, contact_id INTEGER, "
                     "chat_id INTEGER, started_at REAL, duration_s REAL, connected INTEGER, path TEXT, "
                     "loss_pct REAL, rtt_ms REAL, jitter_ms REAL, voice_s REAL, end_reason TEXT, error TEXT)")
        conn.execute("INSERT INTO call_echo_log (contact_id, started_at, connected) VALUES (7, ?, 1)", (time.time(),))
        conn.commit()
        conn.close()
        database.init_db()
        self._log("turn.delta.chat", "gmail.com")
        rows = database.get_recent_call_echo_logs()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["caller_turn"] for r in rows}, {None, "turn.delta.chat"})

    @patch("calls.dc_helpers._send")
    def test_callstats_admin_shows_turn_line(self, mock_send):
        self._log("turn.delta.chat", "gmail.com")
        bot = MagicMock()
        msg = MagicMock(from_id=1, chat_id=5)
        with patch("calls.dc_helpers._is_dc_admin", return_value=True):
            calls.callstats_command(bot, 1, MagicMock(msg=msg))
        self.assertIn("Callers' TURN (7d): turn.delta.chat 1/1", mock_send.call_args[0][3])


class TestTurnTestPage(unittest.TestCase):
    def setUp(self):
        from web import routes

        self.routes = routes
        routes._turn_relays_cache.update(at=0.0, relays=[])

    def tearDown(self):
        self.routes._turn_relays_cache.update(at=0.0, relays=[])

    def test_relays_from_the_bots_ice_servers(self):
        bot = MagicMock()
        bot.rpc.ice_servers.return_value = json.dumps([
            {"urls": ["turn:[2a05:d014::7]:3478", "turn:185.68.247.62:3478"], "username": "u", "credential": "p"},
            {"urls": ["turn:213.165.89.146:3478"], "username": "u", "credential": "p"},
            {"urls": ["turn:46.224.39.230:3478"], "username": "public", "credential": "p"},
            {"urls": ["turn:[2a01:db8::1]:3478"], "username": "u", "credential": "p"},
        ])
        names = {"185.68.247.62": "chatmail.uk", "2a05:d014::7": "chatmail.uk",
                 "213.165.89.146": "chat.gluek.info", "46.224.39.230": "turn.delta.chat"}
        with patch.object(state, "dc_bot_instance", bot), patch.object(state, "dc_accid", 1), \
                patch.object(turn_names, "ip_names", return_value=names), \
                patch.object(turn_names, "known_hosts", return_value=[]):
            relays = self.routes._turn_test_relays()
            self.routes._turn_test_relays()
        self.assertEqual(relays, [
            {"name": "chatmail.uk", "url": "stun:185.68.247.62:3478"},     # IPv4 preferred
            {"name": "chat.gluek.info", "url": "stun:213.165.89.146:3478"},
            {"name": "2a01:db8::1", "url": "stun:[2a01:db8::1]:3478"},     # unnamed: by address
        ])  # turn.delta.chat is tested with a real allocation instead
        self.assertEqual(bot.rpc.ice_servers.call_count, 1)  # cached

    def test_no_bot_yet(self):
        with patch.object(state, "dc_bot_instance", None):
            self.assertEqual(self.routes._turn_test_relays(), [])

    def test_page(self):
        fallback = {"host": "turn.delta.chat", "port": 3478, "user": "public", "password": "pw"}
        relays = [{"name": "evil</script><script>alert(1)</script>", "url": "stun:185.68.247.62:3478"}]
        page = tpl_turn_test.get_turn_test_html("", fallback, relays, "dc.example.org")
        self.assertIn('"host": "turn.delta.chat"', page)
        self.assertIn('iceTransportPolicy: t.policy', page)
        self.assertNotIn("</script><script>alert", page)  # names are data, never markup
        self.assertIn('<meta name="robots" content="noindex" />', page)
        for lang in ("en", "ru"):
            self.assertIn(f'"{lang}": {{', page)

    def test_route_respects_switch(self):
        import asyncio

        request = MagicMock(headers={})
        with patch.object(config, "TURN_TEST_PAGE", False), \
                patch.object(self.routes.security, "check_rate_limit", return_value=True):
            with self.assertRaises(self.routes.web.HTTPNotFound):
                asyncio.run(self.routes.handle_turn_test(request))
        with patch.object(self.routes, "_turn_test_relays", return_value=[]), \
                patch.object(self.routes.database, "get_config", return_value="https://dc.example.org"), \
                patch.object(self.routes.security, "check_rate_limit", return_value=True):
            resp = asyncio.run(self.routes.handle_turn_test(request))
        self.assertIn("dc.example.org", resp.text)
        self.assertEqual(resp.content_type, "text/html")
        self.assertIn(turn_names.FALLBACK_TURN_PASSWORD, resp.text)
        self.assertEqual(resp.headers["Cache-Control"], "no-store")


if __name__ == "__main__":
    unittest.main()
