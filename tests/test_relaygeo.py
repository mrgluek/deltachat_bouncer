"""relaygeo.py: country flags of relays, cached for a day and refreshed in the background."""
import ipaddress
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

TEST_DIR = tempfile.TemporaryDirectory(prefix="test_relaygeo_")
TEST_DB = os.path.join(TEST_DIR.name, "bouncer.db")
os.environ["DB_PATH"] = TEST_DB

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

import database
import relaygeo


class _SyncThread:
    """Runs the refresh right away instead of on a thread."""

    def __init__(self, target, args=(), **kw):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class Base(unittest.TestCase):
    def setUp(self):
        database.close_db()
        database.DB_PATH = TEST_DB
        self._rm()
        database.init_db()
        relaygeo._cache = None
        relaygeo._busy.clear()
        for name in ("_wl_busy", "_wl_versions"):
            if hasattr(relaygeo, name):
                getattr(relaygeo, name).clear()
        relaygeo._failed_at.clear()
        p = patch.object(relaygeo, "threading", SimpleNamespace(Thread=_SyncThread))
        p.start()
        self.addCleanup(p.stop)
        self.wl_on = False
        self.wl = {}  # ip -> list subnet ("" = not listed)
        for p in (patch.object(relaygeo.whitelist, "enabled", side_effect=lambda kind: self.wl_on),
                  patch.object(relaygeo.whitelist, "ip_status", side_effect=lambda ip: self.wl.get(str(ip)))):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._rm)

    @staticmethod
    def _rm():
        database.close_db()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(TEST_DB + suffix)
            except FileNotFoundError:
                pass


class TestLookup(Base):
    def test_country_from_ipinfo_full_and_lite(self):
        ip = [ipaddress.ip_address("203.0.113.5")]
        with patch.object(relaygeo.iplookup, "resolve", return_value=ip):
            with patch.object(relaygeo.iplookup, "_ipinfo_ip", return_value={"country": "DE", "city": "Falkenstein"}):
                self.assertEqual(relaygeo.lookup_country("a.example"), "DE")
            with patch.object(relaygeo.iplookup, "_ipinfo_ip", return_value={"country": "Germany", "country_code": "de"}):
                self.assertEqual(relaygeo.lookup_country("a.example"), "DE")

    def test_ripe_fallback_and_nothing(self):
        ip = [ipaddress.ip_address("203.0.113.5")]
        with patch.object(relaygeo.iplookup, "resolve", return_value=ip), \
                patch.object(relaygeo.iplookup, "_ipinfo_ip", return_value=None), \
                patch.object(relaygeo.iplookup, "_ripe_country", return_value="NL"):
            self.assertEqual(relaygeo.lookup_country("a.example"), "NL")
        with patch.object(relaygeo.iplookup, "resolve", return_value=[]):
            self.assertIsNone(relaygeo.lookup_country("gone.example"))


class TestFlag(Base):
    def test_first_call_starts_lookup_then_cached(self):
        with patch.object(relaygeo, "lookup", return_value={"cc": "DE", "wl": None, "ips": []}) as look:
            self.assertEqual(relaygeo.flag("A.example"), "")     # unknown yet: nothing to show
            self.assertEqual(relaygeo.flag("a.example"), "🇩🇪")  # the lookup has run since
            self.assertEqual(relaygeo.flag("a.example"), "🇩🇪")
        self.assertEqual(look.call_count, 1)

    def test_survives_restart(self):
        with patch.object(relaygeo, "lookup", return_value={"cc": "FR", "wl": None, "ips": []}):
            relaygeo.flag("a.example")
        relaygeo._cache = None  # a new process reads the database
        with patch.object(relaygeo, "lookup") as look:
            self.assertEqual(relaygeo.flag("a.example"), "🇫🇷")
        look.assert_not_called()

    def test_stale_entry_is_shown_while_refreshed(self):
        relaygeo._cache = {"a.example": {"cc": "DE", "wl": None, "at": time.time() - relaygeo.CACHE_SECONDS - 5}}
        with patch.object(relaygeo, "lookup", return_value={"cc": "NL", "wl": None, "ips": []}) as look:
            self.assertEqual(relaygeo.flag("a.example"), "🇩🇪")  # old value now
            look.assert_called_once()
            self.assertEqual(relaygeo.flag("a.example"), "🇳🇱")  # new one after the refresh

    def test_failure_keeps_old_flag_and_waits_before_retry(self):
        relaygeo._cache = {"a.example": {"cc": "DE", "wl": None, "at": time.time() - relaygeo.CACHE_SECONDS - 5}}
        with patch.object(relaygeo, "lookup", return_value=None) as look:
            self.assertEqual(relaygeo.flag("a.example"), "🇩🇪")
            self.assertEqual(relaygeo.flag("a.example"), "🇩🇪")
        self.assertEqual(look.call_count, 1)  # no hammering after a failure

    def test_unknown_stays_empty_and_never_raises(self):
        with patch.object(relaygeo, "lookup", side_effect=RuntimeError("boom")):
            self.assertEqual(relaygeo.flag("a.example"), "")
            self.assertEqual(relaygeo.flag("a.example"), "")
        self.assertNotIn("a.example", relaygeo._busy)


class TestWhitelist(Base):
    IPS = [ipaddress.ip_address("203.0.113.5"), ipaddress.ip_address("203.0.113.6"), ipaddress.ip_address("2001:db8::1")]

    def _lookup(self, ips):
        with patch.object(relaygeo.iplookup, "resolve", return_value=ips), \
                patch.object(relaygeo.iplookup, "_ipinfo_ip", return_value={"country": "RU"}):
            return relaygeo.lookup("a.example")

    def test_all_ipv4_listed(self):
        self.wl_on = True
        self.wl = {"203.0.113.5": "203.0.113.0/24", "203.0.113.6": "203.0.113.0/24"}
        self.assertEqual(self._lookup(self.IPS)["wl"], True)

    def test_one_address_not_listed(self):
        self.wl_on = True
        self.wl = {"203.0.113.5": "203.0.113.0/24", "203.0.113.6": ""}
        self.assertEqual(self._lookup(self.IPS)["wl"], False)

    def test_unknown_when_off_or_list_unavailable(self):
        self.assertIsNone(self._lookup(self.IPS)["wl"])        # check turned off
        self.wl_on = True                                       # no statuses: list unavailable
        self.assertIsNone(self._lookup(self.IPS)["wl"])

    def test_ipv6_only_is_not_listed(self):
        self.wl_on = True
        self.assertEqual(self._lookup([self.IPS[2]])["wl"], False)

    def test_whitelisted_reads_cache_and_respects_switch(self):
        self.wl_on = True
        relaygeo._cache = {"a.example": {"cc": "RU", "wl": True, "at": time.time(), "ips": []},
                           "b.example": {"cc": "DE", "wl": False, "at": time.time(), "ips": []},
                           "c.example": {"cc": "DE", "wl": None, "at": time.time(), "ips": []}}
        relaygeo._cache["a.example"]["ips"] = ["203.0.113.5"]
        self.wl = {"203.0.113.5": "203.0.113.0/24"}
        self.assertTrue(relaygeo.whitelisted("a.example"))
        self.assertFalse(relaygeo.whitelisted("b.example"))
        self.assertFalse(relaygeo.whitelisted("c.example"))
        self.wl_on = False
        self.assertFalse(relaygeo.whitelisted("a.example"))     # the admin turned the check off

    def test_entry_from_before_the_check_is_refreshed(self):
        self.wl_on = True
        self.wl = {"203.0.113.5": "203.0.113.0/24"}
        relaygeo._cache = {"a.example": {"cc": "RU", "at": time.time()}}   # old format: no "wl"
        with patch.object(relaygeo.iplookup, "resolve", return_value=self.IPS[:1]), \
                patch.object(relaygeo.iplookup, "_ipinfo_ip", return_value={"country": "RU"}):
            self.assertEqual(relaygeo.country("a.example"), "RU")  # still shown meanwhile
            self.assertTrue(relaygeo.whitelisted("a.example"))     # and refreshed in the background


class TestWhitelistRefresh(Base):
    IPS = TestWhitelist.IPS
    _lookup = TestWhitelist._lookup

    def test_fifth_address_is_checked(self):
        self.wl_on = True
        ips = [ipaddress.ip_address(f"203.0.113.{i}") for i in range(1, 6)]
        self.wl = {str(ip): "203.0.113.0/24" for ip in ips[:4]}
        self.wl[str(ips[4])] = ""
        self.assertFalse(self._lookup(ips)["wl"])

    def test_capped_addresses_never_report_positive(self):
        self.wl_on = True
        ips = [ipaddress.ip_address(f"203.0.113.{i}") for i in range(1, 18)]
        self.wl = {str(ip): "203.0.113.0/24" for ip in ips}
        self.assertIsNot(self._lookup(ips)["wl"], True)

    def test_switch_and_list_generation_recheck_without_network(self):
        ips = self.IPS[:1]
        self.wl = {str(ips[0]): "203.0.113.0/24"}
        with patch.object(relaygeo.iplookup, "resolve", return_value=ips) as dns, \
                patch.object(relaygeo.iplookup, "_ipinfo_ip", return_value={"country": "RU"}) as country, \
                patch.object(relaygeo.whitelist, "generation", return_value=1, create=True) as generation:
            relaygeo.country("a.example")  # country cached while whitelist is off
            self.assertFalse(relaygeo.whitelisted("a.example"))
            self.wl_on = True
            self.assertTrue(relaygeo.whitelisted("a.example"))
            self.wl[str(ips[0])] = ""
            generation.return_value = 2
            self.assertFalse(relaygeo.whitelisted("a.example"))
            self.wl[str(ips[0])] = "203.0.113.0/24"
            generation.return_value = 3
            self.assertTrue(relaygeo.whitelisted("a.example"))
            self.assertEqual(dns.call_count, 1)
            self.assertEqual(country.call_count, 1)

    def test_unavailable_list_retried_soon_without_country_lookup(self):
        self.wl_on = True
        with patch.object(relaygeo.iplookup, "resolve", return_value=self.IPS[:1]) as dns, \
                patch.object(relaygeo.iplookup, "_ipinfo_ip", return_value={"country": "RU"}) as country:
            relaygeo.country("a.example")
            self.assertFalse(relaygeo.whitelisted("a.example"))
            self.wl = {str(self.IPS[0]): "203.0.113.0/24"}
            later = time.time() + relaygeo.RETRY_SECONDS + 1
            with patch.object(relaygeo.time, "time", return_value=later):
                self.assertTrue(relaygeo.whitelisted("a.example"))
            self.assertEqual(dns.call_count, 1)
            self.assertEqual(country.call_count, 1)


class TestParseCountry(unittest.TestCase):
    def test_forms(self):
        for text in ("🇷🇺", "ru", "RU", " Ru "):
            self.assertEqual(relaygeo.parse_country(text), "RU")
        for text in ("", "russia", "r", "🇷", "1a", "🇷🇺🇩🇪", "ру"):
            self.assertIsNone(relaygeo.parse_country(text))

    def test_country_code_without_flag(self):
        relaygeo._cache = {"a.example": {"cc": "DE", "wl": None, "at": time.time(), "ips": []}}
        self.assertEqual(relaygeo.country("A.example"), "DE")


if __name__ == "__main__":
    unittest.main()
