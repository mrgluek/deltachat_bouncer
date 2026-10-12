import os
import sys
import unittest
import time
import tempfile
import hashlib
import ipaddress
from unittest.mock import MagicMock, patch

# Setup test environment
TEST_DB = "test_ip_bouncer.db"
os.environ["DB_PATH"] = TEST_DB

# Mock packages if not installed
try:
    import deltachat2
except ImportError:
    mock_deltachat2 = MagicMock()
    class MsgData:
        def __init__(self, text="", file="", override_sender_name=None):
            self.text = text
            self.file = file
            self.override_sender_name = override_sender_name
    mock_deltachat2.MsgData = MsgData
    class SystemMessageType:
        MEMBER_ADDED_TO_GROUP = 1
        MEMBER_REMOVED_FROM_GROUP = 2
        GROUP_NAME_CHANGED = 3
        GROUP_IMAGE_CHANGED = 4
    mock_deltachat2.SystemMessageType = SystemMessageType
    sys.modules['deltachat2'] = mock_deltachat2

try:
    import deltabot_cli
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
    sys.modules['deltabot_cli'] = mock_deltabot_cli

try:
    import qrcode
except ImportError:
    sys.modules['qrcode'] = MagicMock()

# Add parent directory to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import bot
import config
import dc_helpers
import state
import iplookup



# Canned API answers keyed by a URL fragment; the first matching fragment wins.
IPINFO_FULL = {"ip": "84.65.49.143", "city": "Slough", "region": "England", "country": "GB",
               "org": "AS5378 Vodafone Limited", "timezone": "Europe/London"}
RIPE = {
    "network-info": {"status": "ok", "data": {"asns": ["5378"], "prefix": "84.64.0.0/13"}},
    "as-overview": {"status": "ok", "data": {"holder": "Vodafone Limited", "announced": True}},
    "announced-prefixes": {"status": "ok", "data": {"prefixes": [
        {"prefix": "90.255.254.0/24"}, {"prefix": "84.64.0.0/13"}, {"prefix": "2a00:23c0::/29"}]}},
    "prefix-overview": {"status": "ok", "data": {
        "announced": True, "is_less_specific": True, "resource": "84.64.0.0/13",
        "asns": [{"asn": 5378, "holder": "Vodafone Limited"}], "related_prefixes": [],
        "block": {"resource": "84.0.0.0/8", "desc": "RIPE NCC (Status: ALLOCATED)"}}},
    "rir-stats-country": {"status": "ok", "data": {"located_resources": [{"location": "GB"}]}},
}


def fake_get(url, timeout=20):
    if "ipinfo.io" in url:
        return dict(IPINFO_FULL), 200
    for call, answer in RIPE.items():
        if f"/{call}/" in url:
            return answer, 200
    return None, 404


class TestFindTarget(unittest.TestCase):
    def test_ipv4_with_trailing_dot(self):
        self.assertEqual(iplookup.find_target("from 84.65.49.143."), ("ip", ipaddress.ip_address("84.65.49.143")))

    def test_first_public_address_wins(self):
        kind, value = iplookup.find_target("10.0.0.5 -> 1.1.1.1:443 and 8.8.8.8")
        self.assertEqual(str(value), "1.1.1.1")

    def test_ipv6_in_brackets(self):
        self.assertEqual(iplookup.find_target("[2a01:4f8::1]:443")[1], ipaddress.ip_address("2a01:4f8::1"))

    def test_network_is_normalized(self):
        self.assertEqual(iplookup.find_target("84.65.49.143/13"), ("net", ipaddress.ip_network("84.64.0.0/13")))

    def test_asn_forms(self):
        for text in ("AS5378", "as5378", "ASN 5378", "network AS5378, UK"):
            self.assertEqual(iplookup.find_target(text), ("asn", 5378), text)

    def test_order_of_appearance(self):
        self.assertEqual(iplookup.find_target("AS5378 owns 84.65.49.143")[0], "asn")
        self.assertEqual(iplookup.find_target("84.65.49.143 is in AS5378")[0], "ip")

    def test_bare_number_only_as_argument(self):
        self.assertEqual(iplookup.find_target("5378", bare_asn=True), ("asn", 5378))
        self.assertIsNone(iplookup.find_target("5378"))

    def test_not_addresses(self):
        for text in ("v2.28.0", "at 12:30:45", "aa:bb:cc:dd:ee:ff", "1.2.3.4.5", "999.1.1.1", "192.168.1.1", "", "AS0"):
            self.assertIsNone(iplookup.find_target(text), text)


class TestFormatting(unittest.TestCase):
    @patch.object(iplookup, "_http_get_json", side_effect=fake_get)
    def test_ip_report(self, _):
        text = iplookup.lookup("ip", ipaddress.ip_address("84.65.49.143"))
        self.assertIn("Slough, England, GB", text)
        self.assertIn("AS5378 Vodafone Limited", text)
        self.assertIn("84.64.0.0/13 (announced by AS5378)", text)
        self.assertIn("https://ipinfo.io/AS5378/84.64.0.0/13", text)

    @patch.object(iplookup, "_http_get_json")
    def test_ip_report_lite_api(self, mock_get):
        # Lite API: country is a name, the code is in country_code, AS fields are flat
        mock_get.side_effect = lambda url, timeout=20: (
            ({"ip": "84.65.49.143", "asn": "AS5378", "as_name": "Vodafone Limited",
              "country_code": "GB", "country": "United Kingdom"}, 200) if "ipinfo.io" in url else fake_get(url))
        text = iplookup.lookup("ip", ipaddress.ip_address("84.65.49.143"))
        self.assertIn("United Kingdom (GB)", text)
        self.assertIn("AS5378 Vodafone Limited", text)

    @patch.object(iplookup, "_http_get_json", return_value=(None, 0))
    def test_ip_report_both_down(self, _):
        self.assertTrue(iplookup.lookup("ip", ipaddress.ip_address("84.65.49.143")).startswith("❌"))

    @patch.object(iplookup, "_http_get_json")
    def test_token_then_no_token_then_lite(self, mock_get):
        mock_get.return_value = (None, 403)
        with patch.dict(os.environ, {"IPINFO_TOKEN": "tok"}):
            iplookup._ipinfo_ip("84.65.49.143")
        urls = [c.args[0] for c in mock_get.call_args_list]
        self.assertIn("token=tok", urls[0])
        self.assertEqual(urls[1], "https://ipinfo.io/84.65.49.143/json")
        self.assertIn("api.ipinfo.io/lite/", urls[2])

    @patch.object(iplookup, "_http_get_json", side_effect=fake_get)
    def test_asn_report_largest_prefixes_first(self, _):
        text = iplookup.lookup("asn", 5378)
        self.assertIn("AS5378** — Vodafone Limited", text)
        self.assertIn("Announced prefixes: 3 (IPv4 2, IPv6 1)", text)
        self.assertLess(text.index("84.64.0.0/13"), text.index("90.255.254.0/24"))

    @patch.object(iplookup, "_http_get_json", side_effect=fake_get)
    def test_net_report_less_specific(self, _):
        text = iplookup.lookup("net", ipaddress.ip_network("84.65.0.0/16"))
        self.assertIn("Part of announced prefix 84.64.0.0/13", text)
        self.assertIn("AS5378 Vodafone Limited", text)
        self.assertIn("84.0.0.0/8", text)


class TestInternetDB(unittest.TestCase):
    SHODAN = {"ip": "185.220.101.1", "ports": [9001, 443, 80, 7547, 4444],
              "cpes": ["cpe:/a:f5:nginx:1.18.0"], "hostnames": ["exit.example.org"],
              "tags": ["tor", "self-signed"], "vulns": ["CVE-2019-1", "CVE-2021-2"]}

    def _get(self, shodan):
        def get(url, timeout=20):
            if "internetdb.shodan.io" in url:
                return shodan
            if "ipinfo.io" in url:
                return {"ip": "185.220.101.1", "country": "DE", "org": "AS60729 Example"}, 200
            return fake_get(url)
        return get

    def test_ports_software_tags_and_hints(self):
        with patch.object(iplookup, "_http_get_json", side_effect=self._get((self.SHODAN, 200))):
            text = iplookup.lookup("ip", ipaddress.ip_address("185.220.101.1"))
        self.assertIn("Open ports: 80 HTTP, 443 HTTPS, 4444, 7547 TR-069, 9001 Tor relay", text)
        self.assertIn("Software: nginx 1.18.0", text)
        self.assertIn("Shodan tags: tor, self-signed", text)
        self.assertIn("VPN / proxy / Tor ports: 9001 Tor relay", text)
        self.assertIn("Router management ports: 7547 TR-069", text)
        self.assertIn("CVE-2021-2, CVE-2019-1", text)
        self.assertIn("Host names: exit.example.org", text)  # ipinfo had no hostname
        self.assertIn("https://www.shodan.io/host/185.220.101.1", text)

    def test_nothing_seen(self):
        with patch.object(iplookup, "_http_get_json", side_effect=self._get((None, 404))):
            text = iplookup.lookup("ip", ipaddress.ip_address("185.220.101.1"))
            text6 = iplookup.lookup("ip", ipaddress.ip_address("2a01:4f8::1"))
        self.assertIn("Open ports: none seen by Shodan", text)
        self.assertNotIn("shodan.io/host", text)
        self.assertIn("IPv6 is rarely scanned", text6)

    def test_shodan_down(self):
        with patch.object(iplookup, "_http_get_json", side_effect=self._get((None, 0))):
            text = iplookup.lookup("ip", ipaddress.ip_address("185.220.101.1"))
        self.assertIn("Shodan InternetDB did not answer", text)
        self.assertIn("AS60729 Example", text)  # the rest of the report is still there


class TestIpCommand(unittest.TestCase):
    def setUp(self):
        self.bot = MagicMock()
        state._ip_lookup_cache.clear()
        state._ip_lookup_pending = 0
        state._ip_lookup_last_time = 0.0

    def _event(self, payload="", quote=None):
        ev = MagicMock()
        ev.msg.chat_id, ev.msg.id = 100, 200
        ev.msg.quote = quote
        ev.payload = payload
        return ev

    def test_usage_without_argument_or_reply(self):
        with patch.object(dc_helpers, "_send") as send:
            iplookup.ip_command(self.bot, 1, self._event())
        self.assertIn("Usage:", send.call_args[0][3])

    def test_private_address_rejected(self):
        with patch.object(dc_helpers, "_send") as send, patch.object(iplookup.threading, "Thread") as th:
            iplookup.ip_command(self.bot, 1, self._event("192.168.1.1"))
        self.assertIn("private or reserved", send.call_args[0][3])
        th.assert_not_called()

    def test_reply_uses_first_target_in_quoted_message(self):
        self.bot.rpc.get_message.return_value = MagicMock(text="banned 10.1.1.1 and 84.65.49.143, AS5378")
        with patch.object(dc_helpers, "_react"), patch.object(iplookup.threading, "Thread") as th:
            iplookup.ip_command(self.bot, 1, self._event(quote={"message_id": 55}))
        self.bot.rpc.get_message.assert_called_with(1, 55)
        args = th.call_args.kwargs["args"]
        self.assertEqual(args[4:], ("ip", ipaddress.ip_address("84.65.49.143")))

    def test_reply_without_target(self):
        self.bot.rpc.get_message.return_value = MagicMock(text="hello")
        with patch.object(dc_helpers, "_send") as send:
            iplookup.ip_command(self.bot, 1, self._event(quote={"message_id": 55}))
        self.assertIn("No public IP address", send.call_args[0][3])

    def test_argument_wins_over_reply(self):
        with patch.object(dc_helpers, "_react"), patch.object(iplookup.threading, "Thread") as th:
            iplookup.ip_command(self.bot, 1, self._event("AS24940", quote={"message_id": 55}))
        self.assertEqual(th.call_args.kwargs["args"][4:], ("asn", 24940))
        self.bot.rpc.get_message.assert_not_called()

    def test_cached_answer_skips_queue(self):
        state._ip_lookup_cache["asn:5378"] = (time.time(), "cached report")
        with patch.object(dc_helpers, "_send") as send, patch.object(iplookup.threading, "Thread") as th:
            iplookup.ip_command(self.bot, 1, self._event("AS5378"))
        self.assertEqual(send.call_args[0][3], "cached report")
        th.assert_not_called()

    def test_full_queue_refused(self):
        state._ip_lookup_pending = config.IP_LOOKUP_MAX_QUEUE
        with patch.object(dc_helpers, "_send") as send, patch.object(iplookup.threading, "Thread") as th:
            iplookup.ip_command(self.bot, 1, self._event("AS5378"))
        self.assertIn("Too many lookups", send.call_args[0][3])
        th.assert_not_called()

    @patch.object(config, "IP_LOOKUP_INTERVAL_SECONDS", 0.0)
    @patch.object(iplookup, "lookup", return_value="report")
    def test_worker_sends_caches_and_frees_slot(self, mock_lookup):
        state._ip_lookup_pending = 1
        with patch.object(dc_helpers, "_send") as send, patch.object(dc_helpers, "_react"):
            iplookup.bg_ip_worker(self.bot, 1, 100, 200, "asn", 5378)
            iplookup.bg_ip_worker(self.bot, 1, 100, 201, "asn", 5378)
        self.assertEqual(mock_lookup.call_count, 1)  # second answer came from the cache
        self.assertEqual(send.call_args[0][3], "report")
        self.assertEqual(state._ip_lookup_pending, -1)  # one slot freed per worker

    @patch.object(config, "IP_LOOKUP_INTERVAL_SECONDS", 0.2)
    @patch.object(iplookup, "lookup", side_effect=lambda k, v: f"report {v}")
    def test_worker_waits_between_lookups(self, _):
        with patch.object(dc_helpers, "_send"), patch.object(dc_helpers, "_react"):
            start = time.time()
            iplookup.bg_ip_worker(self.bot, 1, 100, 200, "asn", 1)
            iplookup.bg_ip_worker(self.bot, 1, 100, 201, "asn", 2)
        self.assertGreaterEqual(time.time() - start, 0.2)


if __name__ == "__main__":
    unittest.main()
