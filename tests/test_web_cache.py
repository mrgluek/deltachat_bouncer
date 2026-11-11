"""Bounded channel caches and untrusted ingress headers."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

TEST_DIR = tempfile.TemporaryDirectory(prefix="test_web_cache_")
os.environ["DB_PATH"] = os.path.join(TEST_DIR.name, "cache.db")
os.environ["DC_DB_DIR"] = TEST_DIR.name

try:
    import deltachat2
except ImportError:
    sys.modules["deltachat2"] = MagicMock()
try:
    import deltabot_cli
except ImportError:
    sys.modules["deltabot_cli"] = MagicMock()

import database
import security
import state
from web import routes


class TestWebCache(unittest.TestCase):
    def setUp(self):
        database.close_db()
        database.DB_PATH = os.path.join(TEST_DIR.name, "cache.db")
        database.init_db()
        routes.invalidate_channel_cache()
        self.token = database.add_catalog_channel(7101, "Cache", "", 2, "")
        self.rate_limit = patch.object(security, "check_rate_limit", return_value=True)
        self.rate_limit.start()
        self.addCleanup(self.rate_limit.stop)

    def tearDown(self):
        routes.invalidate_channel_cache()
        database.close_db()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(database.DB_PATH + suffix)
            except FileNotFoundError:
                pass

    def request(self, ingress="", host="example.org", token=None):
        return MagicMock(headers={"X-Ingress-Path": ingress}, host=host,
                         scheme="https", match_info={"token": token or self.token})

    def test_many_unique_ingress_and_hosts_are_bounded(self):
        async def flood():
            for n in range(300):
                req = self.request(f"/api/hassio_ingress/token{n}", f"host{n}.example")
                self.assertEqual((await routes.handle_channel_preview(req)).status, 200)
                self.assertEqual((await routes.handle_channel_rss(req)).status, 200)
        with patch.dict(os.environ, {"BASE_URL": ""}), patch.object(database, "get_config", return_value=None):
            asyncio.run(flood())
        self.assertLessEqual(len(state._channel_preview_cache), 256)
        self.assertLessEqual(len(state._channel_rss_cache), 256)

    def test_invalid_ingress_is_ignored(self):
        invalid = ["//evil.test", "/a//b", "/a/../b", "/..", "/a?x=1", "/a#x",
                   "/a\nb", "/a\rb", "/a\x00b", "/a%2fb", "/a\\b", "/a/./b", "relative", "/" + "a" * 201]
        with patch.object(routes.tpl_channel, "get_channel_preview_html", return_value="page") as render:
            for path in invalid:
                with self.subTest(path=path):
                    routes.invalidate_channel_cache()
                    asyncio.run(routes.handle_channel_preview(self.request(path)))
                    self.assertEqual(render.call_args.kwargs["ingress_path"], "")
        path = "/api/hassio_ingress/token_123-~./"
        with patch.object(routes.tpl_channel, "get_channel_preview_html", return_value="page") as render:
            asyncio.run(routes.handle_channel_preview(self.request(path)))
            self.assertEqual(render.call_args.kwargs["ingress_path"], path)

    def test_expired_entries_removed_on_access_and_insert(self):
        for cache in (state._channel_preview_cache, state._channel_rss_cache):
            with patch("time.time", return_value=100):
                cache["access:"] = (101, "etag", "page")
                cache["sweep:"] = (101, "etag", "page")
            with patch("time.time", return_value=101):
                self.assertIsNone(cache.get("access:"))
                cache["live:"] = (200, "etag", "page")
                self.assertEqual(list(cache.keys()), ["live:"])

    def test_oldest_expiry_evicted(self):
        cache = state._channel_preview_cache
        for n in range(256):
            cache[f"{n}:"] = (time.time() + 1000 - n, "etag", "page")
        cache["new:"] = (time.time() + 2000, "etag", "page")
        self.assertNotIn("255:", cache)
        self.assertIn("0:", cache)
        self.assertEqual(len(cache), 256)

    def test_invalidation_preserves_other_channels(self):
        for cache in (state._channel_preview_cache, state._channel_rss_cache):
            for key in (f"{self.token}:/a", f"{self.token}:/b", "other:/a"):
                cache[key] = (time.time() + 120, "etag", "page")
        routes.invalidate_channel_cache(chat_id=7101)
        for cache in (state._channel_preview_cache, state._channel_rss_cache):
            self.assertEqual(list(cache.keys()), ["other:/a"])
        routes.invalidate_channel_cache(token="other")
        self.assertEqual(len(state._channel_preview_cache), 0)
        self.assertEqual(len(state._channel_rss_cache), 0)

    def test_missing_and_deleted_channels_not_cached(self):
        for token in ("missing", self.token):
            if token == self.token:
                database.remove_catalog_channel(7101)
            for handler in (routes.handle_channel_preview, routes.handle_channel_rss):
                asyncio.run(handler(self.request(token=token)))
        self.assertEqual(len(state._channel_preview_cache), 0)
        self.assertEqual(len(state._channel_rss_cache), 0)

    def test_etags_and_ttls_unchanged(self):
        for handler, cache, ttl in ((routes.handle_channel_preview, state._channel_preview_cache, 60),
                                    (routes.handle_channel_rss, state._channel_rss_cache, 120)):
            req = self.request()
            now = time.time()
            with patch("time.time", return_value=now):
                response = asyncio.run(handler(req))
                self.assertEqual(next(iter(cache.values()))[0], now + ttl)
                req.headers["If-None-Match"] = response.headers["ETag"]
                self.assertEqual(asyncio.run(handler(req)).status, 304)

    def test_concurrent_access_and_invalidation(self):
        def exercise(worker):
            for n in range(500):
                for cache in (state._channel_preview_cache, state._channel_rss_cache):
                    key = f"{self.token}:{worker}-{n}"
                    cache[key] = (time.time() + 60, "etag", "page")
                    cache.get(key)
                    cache.keys()
                routes.invalidate_channel_cache(token=self.token)
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(exercise, range(6)))
        self.assertLessEqual(len(state._channel_preview_cache), 256)
        self.assertLessEqual(len(state._channel_rss_cache), 256)
