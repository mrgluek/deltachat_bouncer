"""Russian mobile internet whitelist (whitelist.py): parsing, lookups, report lines, refresh."""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("DB_PATH", "test_whitelist_bouncer.db")

# Mock packages if not installed (whitelist imports config and registers /ruwhitelist).
# Handlers must stay plain functions for the other test modules, as there.
try:
    import deltachat2  # noqa: F401
except ImportError:
    sys.modules['deltachat2'] = MagicMock()
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
    sys.modules['deltabot_cli'] = MagicMock(BotCli=MockBotCli)

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import whitelist

LIST = """# comment
2.63.0.0/17
2.63.128.0/18
5.8.0.0/24
5.8.1.0/24
95.163.32.10
not a network
2a00::/16
"""


DOMAINS = """# comment
chatmail.example
mta-sts.chatmail.example
www.chatmail.example
img.big.example
xn--e1afmkfd.xn--p1ai
"""


class Base(unittest.TestCase):
    def setUp(self):
        for d in (whitelist._data, whitelist._loaded_at, whitelist._next_try):
            d.clear()
        self.db = {}
        self.mock_fetch = MagicMock(side_effect=lambda url: DOMAINS if url.endswith("domains.txt") else LIST)
        patches = [
            patch.object(whitelist, "_fetch", self.mock_fetch),
            patch.dict(os.environ, {"RU_WHITELIST_URL": "https://example.org/list.txt",
                                    "RU_WHITELIST_DOMAINS_URL": "https://example.org/domains.txt"}),
            patch.object(whitelist.database, "get_config", side_effect=self.db.get),
            patch.object(whitelist.database, "set_config", side_effect=self.db.__setitem__),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class TestParse(unittest.TestCase):
    def test_merges_adjacent_and_skips_junk(self):
        data = whitelist.parse(LIST)
        self.assertEqual(data["count"], 5)  # IPv6 and junk lines dropped, bare address = /32
        # 2.63.0.0/17 + 2.63.128.0/18 and 5.8.0.0/24 + 5.8.1.0/24 join into one range each
        self.assertEqual(len(data["starts"]), 3)


class TestLines(Base):
    def test_ip(self):
        self.assertIn("✅ in 2.63.0.0/17", whitelist.ip_line("2.63.1.1"))
        self.assertIn("✅ in 95.163.32.10/32", whitelist.ip_line("95.163.32.10"))
        self.assertIn("❌ not listed", whitelist.ip_line("8.8.8.8"))
        self.assertIn("IPv4 only", whitelist.ip_line("2a00::1"))

    def test_most_specific_subnet_wins(self):
        data = whitelist.parse("10.0.0.0/8\n10.1.0.0/16\n")
        self.assertEqual(whitelist._subnet_of(data, int(whitelist.ipaddress.ip_address("10.1.2.3"))), "10.1.0.0/16")

    def test_net(self):
        self.assertIn("✅ whole block listed (in 2.63.0.0/17)", whitelist.net_line("2.63.4.0/22"))
        self.assertEqual(whitelist.net_line("5.8.0.0/23"), f"{whitelist.LABEL}: ✅ whole block listed")
        partly = whitelist.net_line("2.63.0.0/16")
        self.assertIn("⚠️ partly listed, 49152 of 65536 addresses (75%): 2.63.0.0/17, 2.63.128.0/18", partly)
        self.assertIn("(<1%)", whitelist.net_line("95.163.32.0/24"))
        self.assertIn("❌ not listed", whitelist.net_line("8.8.8.0/24"))

    def test_prefixes(self):
        line = whitelist.prefixes_line(["2.63.0.0/16", "8.8.8.0/24", "5.8.1.0/24", "2a00::/29"])
        self.assertIn("✅ 2 of 3 IPv4 prefixes listed in whole or part: 2.63.0.0/16, 5.8.1.0/24", line)
        self.assertIn("❌ no announced IPv4 prefix listed", whitelist.prefixes_line(["8.8.8.0/24"]))
        self.assertIsNone(whitelist.prefixes_line(["2a00::/29"]))

    def test_addresses(self):
        line = whitelist.addresses_line(["2.63.1.1", "2a00::1", "8.8.8.8", "2.63.1.1"])
        self.assertEqual(line, f"{whitelist.LABEL}: 2.63.1.1 ✅ (in 2.63.0.0/17), 8.8.8.8 ❌")
        self.assertIsNone(whitelist.addresses_line(["2a00::1"]))


class TestDomains(Base):
    def test_listed_with_subdomains(self):
        self.assertEqual(whitelist.domain_line("Chatmail.Example."),
                         f"{whitelist.LABEL} (domains): ✅ chatmail.example is listed; "
                         "also mta-sts.chatmail.example, www.chatmail.example")

    def test_only_subdomains_or_nothing(self):
        self.assertIn("⚠️ big.example is not listed, only 1 of its subdomains: img.big.example",
                      whitelist.domain_line("big.example"))
        self.assertIn("❌ example.net is not listed", whitelist.domain_line("example.net"))
        self.assertIn("❌ other.example is not listed", whitelist.domain_line("other.example"))

    def test_idn(self):
        self.assertIn("✅ пример.рф is listed", whitelist.domain_line("xn--e1afmkfd.xn--p1ai"))
        self.assertIn("xn--e1afmkfd.xn--p1ai", whitelist.parse_domains("пример.рф\n")["names"])


class TestSwitches(Base):
    def test_turned_off_by_empty_url(self):
        with patch.dict(os.environ, {"RU_WHITELIST_URL": "", "RU_WHITELIST_DOMAINS_URL": ""}):
            self.assertIsNone(whitelist.ip_line("2.63.1.1"))
            self.assertIsNone(whitelist.addresses_line(["2.63.1.1"]))
            self.assertIsNone(whitelist.domain_line("chatmail.example"))
        self.mock_fetch.assert_not_called()

    def test_admin_command_turns_each_off_and_on(self):
        reply = whitelist.command_reply("ip off")
        self.assertIn("IP subnets (ip): ❌ off (set by /ruwhitelist)", reply)
        self.assertIn("domains (domain): ✅ on (default, RU_WHITELIST_DOMAINS_URL in .env)", reply)
        self.assertIsNone(whitelist.ip_line("2.63.1.1"))
        self.assertIsNotNone(whitelist.domain_line("chatmail.example"))
        whitelist.command_reply("off")
        self.assertIsNone(whitelist.domain_line("chatmail.example"))
        self.assertEqual(self.db, {"ru_whitelist_ip": "off", "ru_whitelist_domain": "off"})
        whitelist.command_reply("domains on")
        self.assertIn("✅", whitelist.domain_line("chatmail.example"))

    def test_admin_on_wins_over_empty_env(self):
        with patch.dict(os.environ, {"RU_WHITELIST_URL": ""}):
            whitelist.command_reply("ip on")
            self.assertIn("✅ in 2.63.0.0/17", whitelist.ip_line("2.63.1.1"))
        # the default list is used then
        self.assertEqual(self.mock_fetch.call_args.args[0], whitelist.LISTS["ip"][1])

    def test_switch_clears_cached_reports(self):
        whitelist.state._ip_lookup_cache["ip:2.63.1.1"] = (0.0, "old")
        whitelist.state._dns_cache["chatmail.example"] = (0.0, "old")
        whitelist.command_reply("ip")  # only shows the state
        self.assertIn("ip:2.63.1.1", whitelist.state._ip_lookup_cache)
        whitelist.command_reply("on")
        self.assertEqual(whitelist.state._ip_lookup_cache, {})
        self.assertEqual(whitelist.state._dns_cache, {})

    def test_bad_arguments(self):
        for arg in ("maybe", "ip on off", "dns on"):
            self.assertTrue(whitelist.command_reply(arg).startswith("Usage:"), arg)
        self.assertEqual(self.db, {})


class TestLoading(Base):

    def test_downloaded_once_then_refreshed(self):
        whitelist.ip_line("2.63.1.1")
        whitelist.ip_line("8.8.8.8")
        self.assertEqual(self.mock_fetch.call_count, 1)
        whitelist._loaded_at["ip"] -= whitelist.REFRESH_SECONDS + 1
        whitelist.ip_line("8.8.8.8")
        self.assertEqual(self.mock_fetch.call_count, 2)

    def test_failed_download_keeps_old_list_and_waits(self):
        whitelist.ip_line("2.63.1.1")
        whitelist._loaded_at["ip"] -= whitelist.REFRESH_SECONDS + 1
        self.mock_fetch.side_effect = lambda url: None
        self.assertIn("✅", whitelist.ip_line("2.63.1.1"))  # stale copy still answers
        whitelist.ip_line("2.63.1.1")
        self.assertEqual(self.mock_fetch.call_count, 2)  # no new try before RETRY_SECONDS

    def test_unavailable(self):
        self.mock_fetch.side_effect = lambda url: None
        self.assertIn("list unavailable", whitelist.ip_line("2.63.1.1"))
        self.assertIn("list unavailable", whitelist.addresses_line(["2.63.1.1"]))


if __name__ == "__main__":
    unittest.main()
