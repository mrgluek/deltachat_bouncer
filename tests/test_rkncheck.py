import io
import ipaddress
import json
import os
import sys
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

os.environ.setdefault("DB_PATH", "test_rkncheck.db")

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
    mock_deltabot_cli = MagicMock()
    mock_deltabot_cli.BotCli = MockBotCli
    sys.modules['deltabot_cli'] = mock_deltabot_cli

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rkncheck  # noqa: E402
import iplookup  # noqa: E402
import whitelist  # noqa: E402

DAYS = [{"date": f"2026-11-{d:02d}", "count": 0} for d in range(12, 26)]

CLEAN = {"target": "chatmail.uk", "blocked": False, "ips": ["77.68.103.218"], "blocked_subnets": [],
         "cdn_providers": {}, "complaints": DAYS}
RUTRACKER = {"target": "rutracker.org", "blocked": True, "rkn_domain": "rutracker.org",
             "ips": ["104.21.32.39"], "blocked_subnets": [],
             "cdn_providers": {"cloudflare": [{"provider": "cloudflare", "cidr": "104.21.0.0/17"}]},
             "complaints": DAYS[:7] + [dict(d, count=1) for d in DAYS[7:]]}


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class FakeAPI:
    """Answers GET /check?target=... from a dict; counts requests."""

    def __init__(self, answers=None, status=None):
        self.answers = answers or {}
        self.status = status
        self.calls = []

    def __call__(self, req, timeout=None):
        target = req.full_url.split("target=", 1)[1]
        self.calls.append(target)
        if self.status:
            raise urllib.error.HTTPError(req.full_url, self.status, "x", {}, None)
        data = self.answers.get(target)
        if data is None:
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
        return FakeResponse(json.dumps(data).encode())


class Base(unittest.TestCase):
    def setUp(self):
        rkncheck.clear_cache()
        rkncheck._sent.clear()
        rkncheck._paused_until = 0.0
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("CHEBURCHECK_URL", None)

    def tearDown(self):
        self.env.stop()

    def api(self, **kw):
        fake = FakeAPI(**kw)
        p = patch.object(rkncheck, "urlopen", side_effect=fake)
        p.start()
        self.addCleanup(p.stop)
        return fake


class TestLines(Base):
    def test_clean_domain(self):
        self.api(answers={"chatmail.uk": CLEAN})
        self.assertEqual(rkncheck.report_lines("chatmail.uk"),
                         ["🚫 RKN blocklists: ✅ not listed, not on a throttled CDN"])
        self.assertEqual(rkncheck.link_line("chatmail.uk"), "🔎 https://cheburcheck.ru/check?target=chatmail.uk")

    def test_registry_cdn_and_complaints_shown_apart(self):
        self.api(answers={"rutracker.org": RUTRACKER})
        self.assertEqual(rkncheck.report_lines("rutracker.org"), [
            "🚫 RKN blocklists: ⛔ domain is in the registry",
            "🐢 Throttled CDN in Russia (16-20 KB limit): cloudflare",
            "📣 7 \"does not work\" reports on cheburcheck.ru in 14 days (7 in the last 7)",
        ])

    def test_parent_entry_subnets_and_whitelist(self):
        data = dict(CLEAN, target="a.example.org", blocked=True, rkn_domain="*.example.org",
                    blocked_subnets=["203.0.113.0/24"],
                    whitelist={"domain": "a.example.org", "rank": 3, "last_ok": "2026-11-20T10:00:00"})
        self.api(answers={"a.example.org": data})
        self.assertEqual(rkncheck.report_lines("a.example.org"), [
            "🚫 RKN blocklists: ⛔ domain is in the registry (as *.example.org)",
            "🚫 RKN blocklists: ⛔ in blocked subnets: 203.0.113.0/24",
            "✅ CDN exception: this name lifts the 16-20 KB limit (confirmed 2026-11-20)",
        ])

    def test_asn_prefixes(self):
        data = dict(CLEAN, target="AS64500", asn_info={
            "asn": 64500, "prefixes": [f"198.51.{i}.0/24" for i in range(10)],
            "blocked_prefixes": [f"198.51.{i}.0/24" for i in range(7)]})
        self.api(answers={"AS64500": data})
        self.assertEqual(rkncheck.report_lines("AS64500")[0],
                         "🚫 RKN blocklists: ⛔ 7 of 10 prefixes blocked or on a throttled CDN: 198.51.0.0/24, 198.51.1.0/24, "
                         "198.51.2.0/24, 198.51.3.0/24, 198.51.4.0/24 and 2 more")

    def test_not_found_and_errors(self):
        self.api()
        self.assertEqual(rkncheck.report_lines("nope.example"),
                         ["🚫 RKN blocklists: cheburcheck.ru could not resolve it"])
        rkncheck.clear_cache()
        with patch.object(rkncheck, "urlopen", side_effect=OSError("timed out")):
            self.assertEqual(rkncheck.report_lines("chatmail.uk"),
                             ["🚫 RKN blocklists: cheburcheck.ru unavailable, try later"])

    def test_turned_off_by_empty_env(self):
        fake = self.api(answers={"chatmail.uk": CLEAN})
        os.environ["CHEBURCHECK_URL"] = ""
        self.assertEqual(rkncheck.report_lines("chatmail.uk"), [])
        self.assertIsNone(rkncheck.link_line("chatmail.uk"))
        self.assertEqual(fake.calls, [])

    def test_other_api_address(self):
        os.environ["CHEBURCHECK_URL"] = "https://mirror.example/api/v1/"
        seen = []
        with patch.object(rkncheck, "urlopen", side_effect=lambda req, timeout=None: seen.append(req.full_url)
                          or FakeResponse(json.dumps(CLEAN).encode())):
            rkncheck.report_lines("chatmail.uk")
        self.assertEqual(seen, ["https://mirror.example/api/v1/check?target=chatmail.uk"])


class TestLoad(Base):
    def test_repeated_lookups_cost_one_request(self):
        fake = self.api(answers={"chatmail.uk": CLEAN})
        for _ in range(50):
            rkncheck.report_lines("chatmail.uk")
            rkncheck.report_lines("ChatMail.UK")
        self.assertEqual(fake.calls, ["chatmail.uk"])

    def test_at_most_per_minute_requests(self):
        fake = self.api(answers={f"h{i}.example": dict(CLEAN, target=f"h{i}.example") for i in range(200)})
        out = [rkncheck.report_lines(f"h{i}.example") for i in range(200)]
        self.assertEqual(len(fake.calls), rkncheck.PER_MINUTE)
        self.assertEqual(out[-1], ["🚫 RKN blocklists: cheburcheck.ru busy, try later"])

    def test_pause_after_429(self):
        fake = self.api(status=429)
        self.assertIn("busy", rkncheck.report_lines("a.example")[0])
        for i in range(20):
            rkncheck.report_lines(f"b{i}.example")
        self.assertEqual(fake.calls, ["a.example"])

    def test_failures_are_not_cached(self):
        with patch.object(rkncheck, "urlopen", side_effect=OSError("down")):
            rkncheck.report_lines("chatmail.uk")
        fake = self.api(answers={"chatmail.uk": CLEAN})
        self.assertIn("✅", rkncheck.report_lines("chatmail.uk")[0])
        self.assertEqual(fake.calls, ["chatmail.uk"])

    def test_cache_is_bounded(self):
        fake = self.api(answers={f"h{i}.example": CLEAN for i in range(rkncheck.CACHE_MAX + 20)})
        with patch.object(rkncheck, "PER_MINUTE", 10_000):
            for i in range(rkncheck.CACHE_MAX + 20):
                rkncheck.check(f"h{i}.example")
        self.assertEqual(len(rkncheck._cache), rkncheck.CACHE_MAX)
        self.assertEqual(len(fake.calls), rkncheck.CACHE_MAX + 20)


IPINFO = {"ip": "104.21.32.39", "country": "US", "org": "AS13335 Cloudflare, Inc."}


def fake_get(url, timeout=20):
    if "ipinfo.io" in url:
        return dict(IPINFO), 200
    if "network-info" in url:
        return {"status": "ok", "data": {"asns": ["13335"], "prefix": "104.21.32.0/20"}}, 200
    return None, 404


class TestIpReport(Base):
    def setUp(self):
        super().setUp()
        p = patch.object(whitelist, "enabled", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    @patch.object(iplookup, "_http_get_json", side_effect=fake_get)
    def test_host_is_checked_by_name(self, _):
        fake = self.api(answers={"rutracker.org": RUTRACKER})
        with patch.object(iplookup.socket, "getaddrinfo", return_value=[(0, 0, 0, "", ("104.21.32.39", 0))]):
            text = iplookup.lookup("host", "rutracker.org")
        self.assertEqual(fake.calls, ["rutracker.org"])  # the name, not its address
        self.assertIn("🚫 RKN blocklists: ⛔ domain is in the registry", text)
        self.assertIn("🔎 https://cheburcheck.ru/check?target=rutracker.org", text)

    @patch.object(iplookup, "_http_get_json", side_effect=fake_get)
    def test_ip(self, _):
        fake = self.api(answers={"104.21.32.39": dict(CLEAN, target="104.21.32.39")})
        text = iplookup.lookup("ip", ipaddress.ip_address("104.21.32.39"))
        self.assertEqual(fake.calls, ["104.21.32.39"])
        self.assertIn("🚫 RKN blocklists: ✅ not listed", text)
        self.assertLess(text.index("RKN blocklists"), text.index("💡 `/whois"))


if __name__ == "__main__":
    unittest.main()
