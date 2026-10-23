"""Russian mobile internet whitelist (whitelist.py): parsing, lookups, report lines, refresh."""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Mock packages if not installed (whitelist imports config, which needs deltabot_cli)
try:
    import deltabot_cli  # noqa: F401
except ImportError:
    sys.modules['deltabot_cli'] = MagicMock()

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


class Base(unittest.TestCase):
    def setUp(self):
        whitelist._data, whitelist._loaded_at, whitelist._next_try = None, 0.0, 0.0
        self.fetch = patch.object(whitelist, "_fetch", return_value=LIST)
        self.mock_fetch = self.fetch.start()
        self.addCleanup(self.fetch.stop)
        env = patch.dict(os.environ, {"RU_WHITELIST_URL": "https://example.org/list.txt"})
        env.start()
        self.addCleanup(env.stop)


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


class TestLoading(Base):
    def test_turned_off_by_empty_url(self):
        with patch.dict(os.environ, {"RU_WHITELIST_URL": ""}):
            self.assertIsNone(whitelist.ip_line("2.63.1.1"))
            self.assertIsNone(whitelist.addresses_line(["2.63.1.1"]))
        self.mock_fetch.assert_not_called()

    def test_downloaded_once_then_refreshed(self):
        whitelist.ip_line("2.63.1.1")
        whitelist.ip_line("8.8.8.8")
        self.assertEqual(self.mock_fetch.call_count, 1)
        whitelist._loaded_at -= whitelist.REFRESH_SECONDS + 1
        whitelist.ip_line("8.8.8.8")
        self.assertEqual(self.mock_fetch.call_count, 2)

    def test_failed_download_keeps_old_list_and_waits(self):
        whitelist.ip_line("2.63.1.1")
        whitelist._loaded_at -= whitelist.REFRESH_SECONDS + 1
        self.mock_fetch.return_value = None
        self.assertIn("✅", whitelist.ip_line("2.63.1.1"))  # stale copy still answers
        whitelist.ip_line("2.63.1.1")
        self.assertEqual(self.mock_fetch.call_count, 2)  # no new try before RETRY_SECONDS

    def test_unavailable(self):
        self.mock_fetch.return_value = None
        self.assertIn("list unavailable", whitelist.ip_line("2.63.1.1"))
        self.assertIn("list unavailable", whitelist.addresses_line(["2.63.1.1"]))


if __name__ == "__main__":
    unittest.main()
