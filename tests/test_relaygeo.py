"""relaygeo.py: country flags of relays, cached for a day and refreshed in the background."""
import ipaddress
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

TEST_DB = "test_relaygeo_bouncer.db"
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
        relaygeo._failed_at.clear()
        p = patch.object(relaygeo.threading, "Thread", _SyncThread)
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
        with patch.object(relaygeo, "lookup_country", return_value="DE") as look:
            self.assertEqual(relaygeo.flag("A.example"), "")     # unknown yet: nothing to show
            self.assertEqual(relaygeo.flag("a.example"), "🇩🇪")  # the lookup has run since
            self.assertEqual(relaygeo.flag("a.example"), "🇩🇪")
        self.assertEqual(look.call_count, 1)

    def test_survives_restart(self):
        with patch.object(relaygeo, "lookup_country", return_value="FR"):
            relaygeo.flag("a.example")
        relaygeo._cache = None  # a new process reads the database
        with patch.object(relaygeo, "lookup_country") as look:
            self.assertEqual(relaygeo.flag("a.example"), "🇫🇷")
        look.assert_not_called()

    def test_stale_entry_is_shown_while_refreshed(self):
        relaygeo._cache = {"a.example": {"cc": "DE", "at": time.time() - relaygeo.CACHE_SECONDS - 5}}
        with patch.object(relaygeo, "lookup_country", return_value="NL") as look:
            self.assertEqual(relaygeo.flag("a.example"), "🇩🇪")  # old value now
            look.assert_called_once()
            self.assertEqual(relaygeo.flag("a.example"), "🇳🇱")  # new one after the refresh

    def test_failure_keeps_old_flag_and_waits_before_retry(self):
        relaygeo._cache = {"a.example": {"cc": "DE", "at": time.time() - relaygeo.CACHE_SECONDS - 5}}
        with patch.object(relaygeo, "lookup_country", return_value=None) as look:
            self.assertEqual(relaygeo.flag("a.example"), "🇩🇪")
            self.assertEqual(relaygeo.flag("a.example"), "🇩🇪")
        self.assertEqual(look.call_count, 1)  # no hammering after a failure

    def test_unknown_stays_empty_and_never_raises(self):
        with patch.object(relaygeo, "lookup_country", side_effect=RuntimeError("boom")):
            self.assertEqual(relaygeo.flag("a.example"), "")
            self.assertEqual(relaygeo.flag("a.example"), "")
        self.assertNotIn("a.example", relaygeo._busy)


class TestParseCountry(unittest.TestCase):
    def test_forms(self):
        for text in ("🇷🇺", "ru", "RU", " Ru "):
            self.assertEqual(relaygeo.parse_country(text), "RU")
        for text in ("", "russia", "r", "🇷", "1a", "🇷🇺🇩🇪", "ру"):
            self.assertIsNone(relaygeo.parse_country(text))

    def test_country_code_without_flag(self):
        relaygeo._cache = {"a.example": {"cc": "DE", "at": time.time()}}
        self.assertEqual(relaygeo.country("A.example"), "DE")


if __name__ == "__main__":
    unittest.main()
