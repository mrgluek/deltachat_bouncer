import asyncio
import base64
import email.utils
import hashlib
import json
import os
import shutil
import sys
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# Setup test environment
TEST_DB = "test_security.db"
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
    import aiohttp
    from aiohttp import web
except ImportError:
    mock_aiohttp = MagicMock()
    mock_web = MagicMock()
    class MockResponse:
        def __init__(self, text="", body=None, status=200, content_type="text/plain", headers=None, charset=None):
            self.text = text
            self.body = body or (text.encode('utf-8') if text else b"")
            self.status = status
            self.content_type = content_type
            self.charset = charset
            self.headers = headers or {}
    def mock_json_response(data, status=200, headers=None, content_type="application/json"):
        text = json.dumps(data)
        return MockResponse(text=text, status=status, headers=headers, content_type=content_type)
    class MockFileResponse:
        def __init__(self, path, headers=None, status=200):
            self.path = path
            self.headers = headers or {}
            self.status = status
    mock_web.Response = MockResponse
    mock_web.json_response = mock_json_response
    mock_web.FileResponse = MockFileResponse
    mock_aiohttp.web = mock_web
    sys.modules['aiohttp'] = mock_aiohttp
    sys.modules['aiohttp.web'] = mock_web

try:
    import qrcode
except ImportError:
    mock_qrcode = MagicMock()
    def fake_save(buf, *args, **kwargs):
        buf.write(b"fake_qr_data")
    mock_img = MagicMock()
    mock_img.save.side_effect = fake_save
    mock_qr_instance = MagicMock()
    mock_qr_instance.make_image.return_value = mock_img
    mock_qrcode.QRCode.return_value = mock_qr_instance
    sys.modules['qrcode'] = mock_qrcode

# Add parent directory to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import activitypub
import bot

if bot.web is None:
    bot.web = sys.modules.get('aiohttp.web', MagicMock())


class TestSecurity(unittest.TestCase):
    def setUp(self):
        database.close_db()
        database.DB_PATH = TEST_DB
        database.init_db()
        bot.CHANNEL_MEDIA_DIR = "test_media_dir_sec"
        bot.index_page_html_cache = None
        if os.path.exists(bot.CHANNEL_MEDIA_DIR):
            shutil.rmtree(bot.CHANNEL_MEDIA_DIR, ignore_errors=True)
        with bot._rate_limit_lock:
            bot._rate_limits.clear()

    def tearDown(self):
        database.close_db()
        for ext in ["", "-wal", "-shm"]:
            f = TEST_DB + ext
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass
        if os.path.exists(bot.CHANNEL_MEDIA_DIR):
            shutil.rmtree(bot.CHANNEL_MEDIA_DIR, ignore_errors=True)
        with bot._rate_limit_lock:
            bot._rate_limits.clear()

    # ==========================================================================
    # 1. SSRF Protection (is_safe_url)
    # ==========================================================================

    def test_is_safe_url_blocks_private_and_loopback_ips(self):
        unsafe_ips = [
            "http://127.0.0.1/actor",
            "http://127.0.0.2:8080/actor",
            "http://10.0.0.1/key",
            "http://10.254.0.1/key",
            "http://172.16.0.1/key",
            "http://172.31.255.255/key",
            "http://192.168.1.1/key",
            "http://192.168.0.254/key",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/key",
            "http://[::ffff:127.0.0.1]/key",
            "http://[::ffff:10.0.0.1]/key",
        ]
        for url in unsafe_ips:
            safe, reason = activitypub.is_safe_url(url, allow_private=False)
            self.assertFalse(safe, f"Expected {url} to be rejected as unsafe, but got safe. Reason: {reason}")

    def test_is_safe_url_blocks_local_and_internal_hostnames(self):
        unsafe_hosts = [
            "http://localhost/inbox",
            "http://localhost.localdomain/actor",
            "http://service.local/key",
            "http://metadata.google.internal/computeMetadata/v1/",
            "http://router.lan/api",
            "http://gateway.home.arpa/status",
        ]
        for url in unsafe_hosts:
            safe, reason = activitypub.is_safe_url(url, allow_private=False)
            self.assertFalse(safe, f"Expected {url} to be rejected as unsafe, but got safe. Reason: {reason}")

    def test_is_safe_url_blocks_non_http_schemes_and_invalid_inputs(self):
        invalid_urls = [
            "file:///etc/passwd",
            "ftp://example.com/resource",
            "gopher://example.com/",
            "data:text/plain;base64,SGVsbG8=",
            "javascript:alert(1)",
            "",
            None,
            123,
            "not a url",
        ]
        for url in invalid_urls:
            safe, reason = activitypub.is_safe_url(url)
            self.assertFalse(safe, f"Expected {url} to be rejected, but got safe.")

    def test_is_safe_url_allows_testing_domains_and_override(self):
        safe_urls = [
            "https://example.com/users/alice",
            "https://test.example.com/actor",
            "https://actor.test/users/bob",
            "https://remote.social/users/carol",
        ]
        for url in safe_urls:
            safe, reason = activitypub.is_safe_url(url)
            self.assertTrue(safe, f"Expected {url} to be allowed, but rejected: {reason}")

        # Override with allow_private=True
        safe_private, _ = activitypub.is_safe_url("http://127.0.0.1:8080/actor", allow_private=True)
        self.assertTrue(safe_private)

    def test_fetch_remote_actor_and_resolve_key_block_unsafe_urls(self):
        # fetch_remote_actor should abort before making any HTTP request
        res_actor = asyncio.run(activitypub.fetch_remote_actor("http://169.254.169.254/actor"))
        self.assertIsNone(res_actor)

        # resolve_public_key should abort before making any HTTP request
        key, doc = asyncio.run(activitypub.resolve_public_key("http://127.0.0.1:8000/key#main-key"))
        self.assertIsNone(key)
        self.assertIsNone(doc)

    # ==========================================================================
    # 2. Cheap Signature Verification Rejection (Date, Digest, KeyId)
    # ==========================================================================

    def test_validate_signature_cheap_missing_or_malformed(self):
        # Missing signature header
        valid, reason = activitypub.validate_signature_cheap("POST", {}, b"")
        self.assertFalse(valid)
        self.assertIn("Missing Signature header", reason)

        # Missing keyId
        headers = {"Signature": 'algorithm="rsa-sha256",headers="date",signature="abc"'}
        valid, reason = activitypub.validate_signature_cheap("POST", headers, b"")
        self.assertFalse(valid)
        self.assertIn("Missing keyId", reason)

        # Missing signature
        headers = {"Signature": 'keyId="https://example.com/key",algorithm="rsa-sha256",headers="date"'}
        valid, reason = activitypub.validate_signature_cheap("POST", headers, b"")
        self.assertFalse(valid)
        self.assertIn("Missing signature data", reason)

    def test_validate_signature_cheap_unsafe_key_id(self):
        headers = {
            "Signature": 'keyId="http://169.254.169.254/key",algorithm="rsa-sha256",headers="(request-target) host date",signature="abc"',
            "Date": email.utils.formatdate(usegmt=True),
        }
        valid, reason = activitypub.validate_signature_cheap("POST", headers, b"")
        self.assertFalse(valid)
        self.assertIn("Unsafe keyId URL", reason)

    def test_validate_signature_cheap_date_checks(self):
        # Missing Date
        headers = {
            "Signature": 'keyId="https://example.com/key",algorithm="rsa-sha256",headers="(request-target) host date",signature="abc"',
        }
        valid, reason = activitypub.validate_signature_cheap("POST", headers, b"")
        self.assertFalse(valid)
        self.assertIn("Missing Date header", reason)

        # Malformed Date
        headers["Date"] = "not-a-valid-date"
        valid, reason = activitypub.validate_signature_cheap("POST", headers, b"")
        self.assertFalse(valid)
        self.assertIn("Malformed Date header", reason)

        # Expired Date (> 300s)
        headers["Date"] = "Tue, 15 Sep 2020 12:00:00 GMT"
        valid, reason = activitypub.validate_signature_cheap("POST", headers, b"")
        self.assertFalse(valid)
        self.assertIn("Date header outside allowed 300s window", reason)

    def test_validate_signature_cheap_digest_checks(self):
        body = b'{"type":"Follow"}'
        good_digest = "SHA-256=" + base64.b64encode(hashlib.sha256(body).digest()).decode('ascii')
        bad_digest = "SHA-256=invalidhashvalue"

        headers = {
            "Signature": 'keyId="https://example.com/key",algorithm="rsa-sha256",headers="(request-target) host date digest",signature="abc"',
            "Date": email.utils.formatdate(usegmt=True),
            "Digest": bad_digest,
        }
        # Bad digest rejected
        valid, reason = activitypub.validate_signature_cheap("POST", headers, body)
        self.assertFalse(valid)
        self.assertIn("Digest header does not match body SHA-256", reason)

        # Good digest accepted
        headers["Digest"] = good_digest
        valid, reason = activitypub.validate_signature_cheap("POST", headers, body)
        self.assertTrue(valid, f"Expected valid, got: {reason}")

    # ==========================================================================
    # 3. Request Body Size Limiting (413 Payload Too Large)
    # ==========================================================================

    def test_handle_ap_inbox_payload_too_large(self):
        req = MagicMock()
        req.method = "POST"
        req.match_info = {"token": "testtok12345"}
        req.remote = "192.0.2.1"
        req.headers = {}
        # Payload > 64 KB (e.g. 65,537 bytes)
        req.read = AsyncMock(return_value=b"x" * 65537)

        resp = asyncio.run(bot.handle_ap_inbox(req))
        self.assertEqual(resp.status, 413)
        self.assertIn("Payload Too Large", resp.text)

    # ==========================================================================
    # 4. Rate Limiting (check_rate_limit & 429 Responses)
    # ==========================================================================

    def test_check_rate_limit_sliding_window(self):
        req = MagicMock()
        req.remote = "203.0.113.10"
        req.headers = {}

        # Allow 3 requests per 10 seconds
        for _ in range(3):
            self.assertTrue(bot.check_rate_limit(req, "test_window", max_requests=3, window_seconds=10))

        # 4th request must be rate limited
        self.assertFalse(bot.check_rate_limit(req, "test_window", max_requests=3, window_seconds=10))

        # Another IP should still be allowed
        req_other = MagicMock()
        req_other.remote = "203.0.113.20"
        req_other.headers = {}
        self.assertTrue(bot.check_rate_limit(req_other, "test_window", max_requests=3, window_seconds=10))

    def test_check_rate_limit_x_forwarded_for(self):
        req = MagicMock()
        req.remote = "127.0.0.1"
        req.headers = {"X-Forwarded-For": "198.51.100.5, 10.0.0.1"}

        for _ in range(2):
            self.assertTrue(bot.check_rate_limit(req, "test_xff", max_requests=2, window_seconds=10))

        # 3rd request blocked based on client IP from XFF
        self.assertFalse(bot.check_rate_limit(req, "test_xff", max_requests=2, window_seconds=10))

    def test_handle_ap_inbox_rate_limiting(self):
        token = "inboxrate123"
        database.add_catalog_channel(chat_id=201, name="Rate Chan", description="", member_count=1, invite_link="", token=token)
        database.set_config("base_url", "https://dc.gluek.info")

        req = MagicMock()
        req.method = "POST"
        req.match_info = {"token": token}
        req.remote = "198.51.100.77"
        req.headers = {}
        req.read = AsyncMock(return_value=b"")

        # Exhaust 60 requests in the inbox bucket
        for _ in range(60):
            self.assertTrue(bot.check_rate_limit(req, "inbox", max_requests=60, window_seconds=60))

        # 61st request directly to handler should return 429
        resp = asyncio.run(bot.handle_ap_inbox(req))
        self.assertEqual(resp.status, 429)
        self.assertIn("Too Many Requests", resp.text)
        self.assertEqual(resp.headers.get("Retry-After"), "60")

    def test_handle_channel_preview_rate_limiting(self):
        token = "previewrate1"
        database.add_catalog_channel(chat_id=202, name="Preview Rate", description="", member_count=1, invite_link="", token=token)

        req = MagicMock()
        req.method = "GET"
        req.match_info = {"token": token}
        req.remote = "198.51.100.88"
        req.headers = {}

        # Exhaust 120 requests
        for _ in range(120):
            self.assertTrue(bot.check_rate_limit(req, "channel_preview", max_requests=120, window_seconds=60))

        resp = asyncio.run(bot.handle_channel_preview(req))
        self.assertEqual(resp.status, 429)
        self.assertIn("Too Many Requests", resp.text)
        self.assertEqual(resp.headers.get("Retry-After"), "60")

    def test_handle_media_file_rate_limiting(self):
        req = MagicMock()
        req.method = "GET"
        req.match_info = {"token": "media123", "msg_id": "1", "filename": "test.jpg"}
        req.remote = "198.51.100.99"
        req.headers = {}

        # Exhaust 120 requests
        for _ in range(120):
            self.assertTrue(bot.check_rate_limit(req, "media", max_requests=120, window_seconds=60))

        resp = asyncio.run(bot.handle_media_file(req))
        self.assertEqual(resp.status, 429)
        self.assertIn("Too Many Requests", resp.text)
        self.assertEqual(resp.headers.get("Retry-After"), "60")

    def test_validate_signature_cheap_anti_replay(self):
        activitypub.reset_signature_replay_cache()
        body = b'{"type":"Follow"}'
        good_digest = "SHA-256=" + base64.b64encode(hashlib.sha256(body).digest()).decode('ascii')
        headers = {
            "Signature": 'keyId="https://remote.social/users/alice#main-key",algorithm="rsa-sha256",headers="(request-target) host date digest",signature="unique-sig-12345678"',
            "Date": email.utils.formatdate(usegmt=True),
            "Digest": good_digest,
        }
        # First verification succeeds
        valid, reason = activitypub.validate_signature_cheap("POST", headers, body)
        self.assertTrue(valid, f"First verify should succeed, got: {reason}")

        # Immediate replay of the exact same signature must fail anti-replay check
        valid2, reason2 = activitypub.validate_signature_cheap("POST", headers, body)
        self.assertFalse(valid2)
        self.assertIn("Replay detected", reason2)

    def test_get_base_url_host_validation(self):
        database.set_config("base_url", "")
        # 1. Valid host and scheme
        req = MagicMock()
        req.headers = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "chat.example.com"}
        self.assertEqual(bot._get_base_url(req), "https://chat.example.com")

        # 2. Host with port
        req.headers = {"Host": "dc.local:8080"}
        self.assertEqual(bot._get_base_url(req), "http://dc.local:8080")

        # 3. Host with illegal characters / header injection fallback to localhost
        req.headers = {"X-Forwarded-Host": "evil.com\r\nInjected-Header: bad"}
        req.host = "evil.com\r\n"
        self.assertEqual(bot._get_base_url(req), "http://localhost")

        # 4. Configured BASE_URL overrides headers
        database.set_config("base_url", "https://canonical.example.com")
        self.assertEqual(bot._get_base_url(req), "https://canonical.example.com")
        database.set_config("base_url", "")

    def test_handle_ap_inbox_key_id_actor_mismatch(self):
        token = "inboxmismatch"
        database.add_catalog_channel(chat_id=301, name="Mismatch Chan", description="", member_count=1, invite_link="", token=token)
        database.set_config("base_url", "https://dc.gluek.info")

        body = json.dumps({
            "type": "Follow",
            "actor": "https://remote.social/users/alice",
            "object": f"https://dc.gluek.info/c/{token}"
        }).encode("utf-8")
        good_digest = "SHA-256=" + base64.b64encode(hashlib.sha256(body).digest()).decode('ascii')

        req = MagicMock()
        req.method = "POST"
        req.match_info = {"token": token}
        req.remote = "198.51.100.44"
        # keyId is on attacker.social, but actor is on remote.social -> origin mismatch!
        req.headers = {
            "Signature": 'keyId="https://attacker.social/keys/1#main-key",algorithm="rsa-sha256",headers="(request-target) host date digest",signature="forged-sig-999"',
            "Date": email.utils.formatdate(usegmt=True),
            "Digest": good_digest,
            "Host": "dc.gluek.info",
        }
        req.read = AsyncMock(return_value=body)

        with patch('activitypub.is_safe_url', return_value=(True, "")):
            resp = asyncio.run(bot.handle_ap_inbox(req))
            self.assertEqual(resp.status, 401)
            self.assertIn("KeyId and actor origin mismatch", resp.text)


if __name__ == "__main__":
    unittest.main()
