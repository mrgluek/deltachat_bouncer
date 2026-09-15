import asyncio
import base64
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
TEST_DB = "test_activitypub.db"
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


class TestActivityPub(unittest.TestCase):
    def setUp(self):
        database.close_db()
        database.DB_PATH = TEST_DB
        database.init_db()
        bot.CHANNEL_MEDIA_DIR = "test_media_dir"
        bot.index_page_html_cache = None
        if os.path.exists(bot.CHANNEL_MEDIA_DIR):
            shutil.rmtree(bot.CHANNEL_MEDIA_DIR, ignore_errors=True)

    def tearDown(self):
        database.close_db()
        if os.path.exists(TEST_DB):
            try:
                os.remove(TEST_DB)
            except Exception:
                pass
        for ext in ("-wal", "-shm"):
            if os.path.exists(TEST_DB + ext):
                try:
                    os.remove(TEST_DB + ext)
                except Exception:
                    pass
        if os.path.exists("test_media_dir"):
            shutil.rmtree("test_media_dir", ignore_errors=True)

    # ==========================================================================
    # 1. Key Management Tests
    # ==========================================================================

    def test_generate_actor_keypair(self):
        priv_pem, pub_pem = activitypub.generate_actor_keypair()
        self.assertTrue(priv_pem.startswith("-----BEGIN PRIVATE KEY-----"))
        self.assertTrue(pub_pem.startswith("-----BEGIN PUBLIC KEY-----"))
        self.assertIn("-----END PRIVATE KEY-----", priv_pem)
        self.assertIn("-----END PUBLIC KEY-----", pub_pem)

    def test_get_or_create_actor_keys(self):
        token = "testtoken123"
        # First call generates and saves keys
        priv1, pub1 = activitypub.get_or_create_actor_keys(token)
        self.assertTrue(priv1.startswith("-----BEGIN PRIVATE KEY-----"))
        self.assertTrue(pub1.startswith("-----BEGIN PUBLIC KEY-----"))

        # Second call returns the exact same keys from DB
        priv2, pub2 = activitypub.get_or_create_actor_keys(token)
        self.assertEqual(priv1, priv2)
        self.assertEqual(pub1, pub2)

    def test_database_actor_keys_crud(self):
        token = "tokencrud123"
        self.assertIsNone(database.get_ap_actor_keys(token))

        database.save_ap_actor_keys(token, "fake_priv", "fake_pub")
        keys = database.get_ap_actor_keys(token)
        self.assertIsNotNone(keys)
        self.assertEqual(keys["private_key_pem"], "fake_priv")
        self.assertEqual(keys["public_key_pem"], "fake_pub")

        self.assertTrue(database.delete_ap_actor_keys(token))
        self.assertIsNone(database.get_ap_actor_keys(token))

    # ==========================================================================
    # 2. HTTP Signatures Signing and Verification
    # ==========================================================================

    def test_sign_post_request(self):
        priv_pem, pub_pem = activitypub.generate_actor_keypair()
        url = "https://remote.social/inbox"
        body = b'{"type":"Create"}'
        key_id = "https://local.instance/c/testtoken#main-key"

        headers = activitypub.sign_headers("POST", url, body, priv_pem, key_id)
        self.assertIn("Host", headers)
        self.assertEqual(headers["Host"], "remote.social")
        self.assertIn("Date", headers)
        self.assertIn("Digest", headers)
        self.assertTrue(headers["Digest"].startswith("SHA-256="))
        self.assertIn("Signature", headers)
        self.assertIn(f'keyId="{key_id}"', headers["Signature"])
        self.assertIn('algorithm="rsa-sha256"', headers["Signature"])
        self.assertIn('(request-target) host date digest content-type', headers["Signature"])

    def test_sign_get_request(self):
        priv_pem, pub_pem = activitypub.generate_actor_keypair()
        url = "https://remote.social/users/alice"
        key_id = "https://local.instance/c/testtoken#main-key"

        headers = activitypub.sign_headers("GET", url, None, priv_pem, key_id)
        self.assertIn("Host", headers)
        self.assertIn("Date", headers)
        self.assertIn("Accept", headers)
        self.assertIn("Signature", headers)
        self.assertIn('(request-target) host date accept', headers["Signature"])

    def test_signature_roundtrip_verification(self):
        priv_pem, pub_pem = activitypub.generate_actor_keypair()
        url = "https://remote.social/inbox"
        path = "/inbox"
        body = b'{"type":"Follow","actor":"https://remote.social/users/alice"}'
        key_id = "https://remote.social/users/alice#main-key"

        headers = activitypub.sign_headers("POST", url, body, priv_pem, key_id)

        # Verification
        verified = activitypub.verify_http_signature("POST", path, headers, body, pub_pem)
        self.assertTrue(verified)

    def test_signature_verification_tampered_body(self):
        priv_pem, pub_pem = activitypub.generate_actor_keypair()
        url = "https://remote.social/inbox"
        path = "/inbox"
        body = b'{"type":"Follow"}'
        key_id = "https://remote.social/users/alice#main-key"

        headers = activitypub.sign_headers("POST", url, body, priv_pem, key_id)

        # Tampered body should fail digest check
        tampered_body = b'{"type":"Follow","tampered":true}'
        verified = activitypub.verify_http_signature("POST", path, headers, tampered_body, pub_pem)
        self.assertFalse(verified)

    def test_signature_verification_tampered_header(self):
        priv_pem, pub_pem = activitypub.generate_actor_keypair()
        url = "https://remote.social/inbox"
        path = "/inbox"
        body = b'{"type":"Follow"}'
        key_id = "https://remote.social/users/alice#main-key"

        headers = activitypub.sign_headers("POST", url, body, priv_pem, key_id)
        headers["Host"] = "evil.com"

        verified = activitypub.verify_http_signature("POST", path, headers, body, pub_pem)
        self.assertFalse(verified)

    def test_signature_verification_expired_date(self):
        priv_pem, pub_pem = activitypub.generate_actor_keypair()
        url = "https://remote.social/inbox"
        path = "/inbox"
        body = b'{"type":"Follow"}'
        key_id = "https://remote.social/users/alice#main-key"

        headers = activitypub.sign_headers("POST", url, body, priv_pem, key_id)
        # Set date to 10 minutes ago (> 300 seconds)
        headers["Date"] = "Tue, 15 Sep 2020 12:00:00 GMT"

        verified = activitypub.verify_http_signature("POST", path, headers, body, pub_pem)
        self.assertFalse(verified)

    def test_parse_signature_header(self):
        sig = 'keyId="https://example.com/actor#main-key",algorithm="rsa-sha256",headers="(request-target) host date",signature="abc123xyz=="'
        parsed = activitypub.parse_signature_header(sig)
        self.assertEqual(parsed["keyId"], "https://example.com/actor#main-key")
        self.assertEqual(parsed["algorithm"], "rsa-sha256")
        self.assertEqual(parsed["headers"], "(request-target) host date")
        self.assertEqual(parsed["signature"], "abc123xyz==")

    # ==========================================================================
    # 3. JSON Builders
    # ==========================================================================

    def test_build_actor_json(self):
        channel = {
            "token": "tokentest123",
            "name": "My Channel",
            "description": "A wonderful channel",
        }
        base_url = "https://dc.gluek.info"
        pub_pem = "-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A\n-----END PUBLIC KEY-----"

        actor = activitypub.build_actor_json(channel, base_url, pub_pem)
        self.assertEqual(actor["id"], "https://dc.gluek.info/c/tokentest123")
        self.assertEqual(actor["type"], "Service")
        self.assertEqual(actor["preferredUsername"], "tokentest123")
        self.assertEqual(actor["name"], "My Channel")
        self.assertIn("A wonderful channel", actor["summary"])
        self.assertEqual(actor["inbox"], "https://dc.gluek.info/c/tokentest123/inbox")
        self.assertEqual(actor["outbox"], "https://dc.gluek.info/c/tokentest123/outbox")
        self.assertEqual(actor["followers"], "https://dc.gluek.info/c/tokentest123/followers")
        self.assertEqual(actor["icon"]["url"], "https://dc.gluek.info/c/tokentest123/avatar.png")
        self.assertEqual(actor["publicKey"]["id"], "https://dc.gluek.info/c/tokentest123#main-key")
        self.assertEqual(actor["publicKey"]["publicKeyPem"], pub_pem)

    def test_build_note(self):
        channel = {"token": "tokentest123"}
        post = {
            "msg_id": 42,
            "text": "Hello Fediverse! Check https://example.com",
            "timestamp": 1700000000,
        }
        base_url = "https://dc.gluek.info"

        note = activitypub.build_note(channel, post, base_url)
        self.assertEqual(note["id"], "https://dc.gluek.info/c/tokentest123/posts/42")
        self.assertEqual(note["type"], "Note")
        self.assertEqual(note["attributedTo"], "https://dc.gluek.info/c/tokentest123")
        self.assertIn("https://www.w3.org/ns/activitystreams#Public", note["to"])
        self.assertIn("https://dc.gluek.info/c/tokentest123/followers", note["cc"])
        self.assertIn("<a href=\"https://example.com\"", note["content"])
        self.assertEqual(len(note["attachment"]), 0)

    def test_build_note_with_media(self):
        channel = {"token": "tokentest123"}
        post = {
            "msg_id": 99,
            "text": "Photo post",
            "media_type": "image",
            "media_filename": "photo.webp",
            "timestamp": 1700000000,
        }
        base_url = "https://dc.gluek.info"

        note = activitypub.build_note(channel, post, base_url)
        self.assertEqual(len(note["attachment"]), 1)
        att = note["attachment"][0]
        self.assertEqual(att["type"], "Document")
        self.assertEqual(att["mediaType"], "image/webp")
        self.assertEqual(att["url"], "https://dc.gluek.info/media/tokentest123/99/photo.webp")

    def test_build_create_activity(self):
        actor_url = "https://dc.gluek.info/c/tokentest123"
        note = {
            "id": f"{actor_url}/posts/1",
            "type": "Note",
            "published": "2026-09-15T00:00:00Z",
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
            "cc": [f"{actor_url}/followers"],
        }
        activity = activitypub.build_create_activity(actor_url, note)
        self.assertEqual(activity["type"], "Create")
        self.assertEqual(activity["actor"], actor_url)
        self.assertEqual(activity["object"], note)

    def test_build_accept_follow(self):
        actor_url = "https://dc.gluek.info/c/tokentest123"
        follow = {
            "id": "https://remote.social/follows/1",
            "type": "Follow",
            "actor": "https://remote.social/users/alice",
            "object": actor_url,
        }
        accept = activitypub.build_accept_follow(actor_url, follow)
        self.assertEqual(accept["type"], "Accept")
        self.assertEqual(accept["actor"], actor_url)
        self.assertEqual(accept["object"], follow)

    def test_format_post_html(self):
        text = "Line 1\nLine 2\n\nParagraph 2 with https://deltachat.org link."
        formatted = activitypub.format_post_html(text)
        self.assertTrue(formatted.startswith("<p>Line 1<br>Line 2</p>"))
        self.assertIn("<a href=\"https://deltachat.org\"", formatted)

    # ==========================================================================
    # 4. Database Followers CRUD Tests
    # ==========================================================================

    def test_followers_crud(self):
        token = "chanfollower1"
        self.assertEqual(database.get_ap_followers_count(token), 0)
        self.assertEqual(len(database.get_ap_followers(token)), 0)

        # Add follower 1
        database.add_ap_follower(token, "https://mastodon.social/users/alice", "https://mastodon.social/users/alice/inbox", "https://mastodon.social/inbox")
        self.assertEqual(database.get_ap_followers_count(token), 1)

        # Add follower 2
        database.add_ap_follower(token, "https://pleroma.site/users/bob", "https://pleroma.site/users/bob/inbox")
        self.assertEqual(database.get_ap_followers_count(token), 2)

        # Update follower 1 inbox (ON CONFLICT DO UPDATE)
        database.add_ap_follower(token, "https://mastodon.social/users/alice", "https://mastodon.social/users/alice/new_inbox", "https://mastodon.social/inbox")
        self.assertEqual(database.get_ap_followers_count(token), 2)
        followers = database.get_ap_followers(token)
        alice = [f for f in followers if f["follower_actor_id"] == "https://mastodon.social/users/alice"][0]
        self.assertEqual(alice["follower_inbox"], "https://mastodon.social/users/alice/new_inbox")

        # Inboxes query
        inboxes = database.get_ap_follower_inboxes(token)
        self.assertEqual(len(inboxes), 2)

        # Remove follower 1
        self.assertTrue(database.remove_ap_follower(token, "https://mastodon.social/users/alice"))
        self.assertEqual(database.get_ap_followers_count(token), 1)

        # Remove across all channels by actor id
        database.add_ap_follower("anotherchan", "https://pleroma.site/users/bob", "https://pleroma.site/users/bob/inbox")
        removed_count = database.remove_ap_followers_by_actor("https://pleroma.site/users/bob")
        self.assertEqual(removed_count, 2)
        self.assertEqual(database.get_ap_followers_count(token), 0)

    def test_delete_followers_for_channel(self):
        token = "candeletereq"
        database.add_ap_follower(token, "https://fedi.net/u/1", "https://fedi.net/u/1/inbox")
        database.add_ap_follower(token, "https://fedi.net/u/2", "https://fedi.net/u/2/inbox")
        self.assertEqual(database.get_ap_followers_count(token), 2)

        deleted = database.delete_ap_followers_for_channel(token)
        self.assertEqual(deleted, 2)
        self.assertEqual(database.get_ap_followers_count(token), 0)

    # ==========================================================================
    # 5. Web Endpoints Tests
    # ==========================================================================

    def test_webfinger_endpoint(self):
        token = "webfingertok"
        database.add_catalog_channel(chat_id=101, name="Webfinger Channel", description="Test", member_count=5, invite_link="", token=token)
        database.set_config("base_url", "https://dc.gluek.info")

        # Valid request
        req = MagicMock()
        req.query = {"resource": f"acct:{token}@dc.gluek.info"}
        req.headers = {}
        resp = asyncio.run(bot.handle_webfinger(req))
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.content_type, "application/jrd+json")
        data = json.loads(resp.text)
        self.assertEqual(data["subject"], f"acct:{token}@dc.gluek.info")
        self.assertEqual(data["links"][0]["href"], f"https://dc.gluek.info/c/{token}")
        self.assertEqual(data["links"][0]["rel"], "self")

        # Missing resource query param
        req.query = {}
        resp = asyncio.run(bot.handle_webfinger(req))
        self.assertEqual(resp.status, 400)

        # Unknown user
        req.query = {"resource": "acct:unknownuser@dc.gluek.info"}
        resp = asyncio.run(bot.handle_webfinger(req))
        self.assertEqual(resp.status, 404)

        # Soft-deleted channel
        database.remove_catalog_channel(101)
        req.query = {"resource": f"acct:{token}@dc.gluek.info"}
        resp = asyncio.run(bot.handle_webfinger(req))
        self.assertEqual(resp.status, 404)

    def test_handle_ap_actor(self):
        token = "actortoken12"
        database.add_catalog_channel(chat_id=102, name="Actor Channel", description="Testing Actor", member_count=10, invite_link="", token=token)
        database.set_config("base_url", "https://dc.gluek.info")

        req = MagicMock()
        req.match_info = {"token": token}
        req.headers = {}
        resp = asyncio.run(bot.handle_ap_actor(req))
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.content_type, "application/activity+json")
        actor = json.loads(resp.text)
        self.assertEqual(actor["type"], "Service")
        self.assertEqual(actor["preferredUsername"], token)
        self.assertEqual(actor["name"], "Actor Channel")
        self.assertIn("publicKey", actor)

    def test_content_negotiation_channel_preview(self):
        token = "contentneg12"
        database.add_catalog_channel(chat_id=103, name="Neg Channel", description="Content Neg", member_count=3, invite_link="", token=token)
        database.set_config("base_url", "https://dc.gluek.info")

        # HTML request
        req_html = MagicMock()
        req_html.match_info = {"token": token}
        req_html.headers = {"Accept": "text/html,application/xhtml+xml"}
        resp_html = asyncio.run(bot.handle_channel_preview(req_html))
        self.assertEqual(resp_html.status, 200)
        self.assertEqual(resp_html.content_type, "text/html")

        # ActivityPub request
        req_ap = MagicMock()
        req_ap.match_info = {"token": token}
        req_ap.headers = {"Accept": "application/activity+json, application/ld+json"}
        resp_ap = asyncio.run(bot.handle_channel_preview(req_ap))
        self.assertEqual(resp_ap.status, 200)
        self.assertEqual(resp_ap.content_type, "application/activity+json")
        data = json.loads(resp_ap.text)
        self.assertEqual(data["type"], "Service")

    def test_handle_ap_outbox(self):
        token = "outboxtoken1"
        database.add_catalog_channel(chat_id=104, name="Outbox Chan", description="Testing Outbox", member_count=2, invite_link="", token=token)
        database.save_channel_post(chat_id=104, msg_id=501, text="Post #1", timestamp=time.time())
        database.save_channel_post(chat_id=104, msg_id=502, text="Post #2", timestamp=time.time() + 1)
        database.set_config("base_url", "https://dc.gluek.info")

        req = MagicMock()
        req.match_info = {"token": token}
        req.query = {}
        req.headers = {}
        resp = asyncio.run(bot.handle_ap_outbox(req))
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.content_type, "application/activity+json")
        outbox = json.loads(resp.text)
        self.assertEqual(outbox["type"], "OrderedCollection")
        self.assertEqual(outbox["totalItems"], 2)
        self.assertEqual(outbox["first"], f"https://dc.gluek.info/c/{token}/outbox?page=true")
        self.assertEqual(len(outbox["orderedItems"]), 2)

        # Paginated request (?page=true)
        req_page = MagicMock()
        req_page.match_info = {"token": token}
        req_page.query = {"page": "true"}
        req_page.headers = {}
        resp_page = asyncio.run(bot.handle_ap_outbox(req_page))
        self.assertEqual(resp_page.status, 200)
        page = json.loads(resp_page.text)
        self.assertEqual(page["type"], "OrderedCollectionPage")
        self.assertEqual(page["partOf"], f"https://dc.gluek.info/c/{token}/outbox")
        self.assertEqual(page["totalItems"], 2)
        self.assertEqual(len(page["orderedItems"]), 2)

    def test_handle_ap_followers(self):
        token = "followerstok"
        database.add_catalog_channel(chat_id=105, name="Followers Chan", description="", member_count=1, invite_link="", token=token)
        database.add_ap_follower(token, "https://mastodon.social/users/user1", "https://mastodon.social/inbox")
        database.set_config("base_url", "https://dc.gluek.info")

        req = MagicMock()
        req.match_info = {"token": token}
        req.headers = {}
        resp = asyncio.run(bot.handle_ap_followers(req))
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.text)
        self.assertEqual(data["type"], "OrderedCollection")
        self.assertEqual(data["totalItems"], 1)

    def test_handle_ap_post(self):
        token = "singlepost12"
        database.add_catalog_channel(chat_id=106, name="Post Chan", description="", member_count=1, invite_link="", token=token)
        database.save_channel_post(chat_id=106, msg_id=777, text="Special note", timestamp=time.time())
        database.set_config("base_url", "https://dc.gluek.info")

        # Existing post
        req = MagicMock()
        req.match_info = {"token": token, "msg_id": "777"}
        req.headers = {}
        resp = asyncio.run(bot.handle_ap_post(req))
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.content_type, "application/activity+json")
        note = json.loads(resp.text)
        self.assertEqual(note["type"], "Note")
        self.assertIn("Special note", note["content"])

        # Non-existing post
        req.match_info = {"token": token, "msg_id": "999"}
        resp = asyncio.run(bot.handle_ap_post(req))
        self.assertEqual(resp.status, 404)

    def test_nodeinfo_endpoints(self):
        database.set_config("base_url", "https://dc.gluek.info")
        req = MagicMock()
        req.headers = {}

        # Discovery
        resp_disc = asyncio.run(bot.handle_nodeinfo_discovery(req))
        self.assertEqual(resp_disc.status, 200)
        data = json.loads(resp_disc.text)
        self.assertEqual(data["links"][0]["href"], "https://dc.gluek.info/nodeinfo/2.0")

        # Nodeinfo 2.0
        resp_ni = asyncio.run(bot.handle_nodeinfo(req))
        self.assertEqual(resp_ni.status, 200)
        ni_data = json.loads(resp_ni.text)
        self.assertEqual(ni_data["version"], "2.0")
        self.assertIn("activitypub", ni_data["protocols"])

    def test_handle_ap_following(self):
        token = "followingtok"
        database.add_catalog_channel(chat_id=107, name="Following Chan", description="", member_count=1, invite_link="", token=token)
        database.set_config("base_url", "https://dc.gluek.info")

        req = MagicMock()
        req.match_info = {"token": token}
        req.headers = {}
        resp = asyncio.run(bot.handle_ap_following(req))
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.text)
        self.assertEqual(data["type"], "OrderedCollection")
        self.assertEqual(data["totalItems"], 0)

    def test_resolve_public_key(self):
        # 1. Standalone key object (GoToSocial)
        with patch("activitypub.fetch_remote_actor", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = {
                "id": "https://so.example/users/bob/main-key",
                "publicKeyPem": "-----BEGIN PUBLIC KEY-----\nFAKESTANDALONE\n-----END PUBLIC KEY-----"
            }
            pem, doc = asyncio.run(activitypub.resolve_public_key("https://so.example/users/bob/main-key"))
            self.assertEqual(pem, "-----BEGIN PUBLIC KEY-----\nFAKESTANDALONE\n-----END PUBLIC KEY-----")
            self.assertIsNotNone(doc)

        # 2. Embedded publicKey object (Mastodon)
        with patch("activitypub.fetch_remote_actor", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = {
                "id": "https://mastodon.example/users/alice",
                "publicKey": {
                    "id": "https://mastodon.example/users/alice#main-key",
                    "publicKeyPem": "-----BEGIN PUBLIC KEY-----\nFAKEEMBEDDED\n-----END PUBLIC KEY-----"
                }
            }
            pem, doc = asyncio.run(activitypub.resolve_public_key("https://mastodon.example/users/alice#main-key"))
            self.assertEqual(pem, "-----BEGIN PUBLIC KEY-----\nFAKEEMBEDDED\n-----END PUBLIC KEY-----")
            self.assertIsNotNone(doc)

    def test_handle_ap_inbox_follow_and_shared_inbox(self):
        token = "inboxtok1234"
        database.add_catalog_channel(chat_id=108, name="Inbox Chan", description="", member_count=1, invite_link="", token=token)
        database.set_config("base_url", "https://dc.gluek.info")

        # Generate remote keypair for testing incoming signed requests
        remote_priv, remote_pub = activitypub.generate_actor_keypair()
        remote_actor_id = "https://remote.social/users/carol"
        remote_key_id = "https://remote.social/users/carol/main-key"

        # 1. Direct inbox POST: /c/{token}/inbox
        follow_act = {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": "https://remote.social/activities/1",
            "type": "Follow",
            "actor": remote_actor_id,
            "object": f"https://dc.gluek.info/c/{token}",
        }
        body_bytes = json.dumps(follow_act).encode("utf-8")
        headers = activitypub.sign_headers("POST", f"https://dc.gluek.info/c/{token}/inbox", body_bytes, remote_priv, remote_key_id)

        req_direct = MagicMock()
        req_direct.match_info = {"token": token}
        req_direct.path = f"/c/{token}/inbox"
        req_direct.raw_path = f"/c/{token}/inbox"
        req_direct.headers = headers
        req_direct.method = "POST"
        req_direct.read = AsyncMock(return_value=body_bytes)

        with patch("activitypub.resolve_public_key", new_callable=AsyncMock) as mock_resolve, \
             patch("activitypub.fetch_remote_actor", new_callable=AsyncMock) as mock_fetch, \
             patch("activitypub.deliver_to_inbox", new_callable=AsyncMock) as mock_deliver:

            mock_resolve.return_value = (remote_pub, {"id": remote_actor_id})
            mock_fetch.return_value = {
                "id": remote_actor_id,
                "inbox": "https://remote.social/users/carol/inbox",
                "endpoints": {"sharedInbox": "https://remote.social/inbox"}
            }

            resp = asyncio.run(bot.handle_ap_inbox(req_direct))
            self.assertEqual(resp.status, 202)
            followers = database.get_ap_followers(token)
            self.assertEqual(len(followers), 1)
            self.assertEqual(followers[0]["follower_actor_id"], remote_actor_id)

        # 2. Shared inbox POST: /inbox (no token in match_info, extracted from object)
        database.remove_ap_follower(token, remote_actor_id)
        self.assertEqual(database.get_ap_followers_count(token), 0)

        shared_headers = activitypub.sign_headers("POST", "https://dc.gluek.info/inbox", body_bytes, remote_priv, remote_key_id)
        req_shared = MagicMock()
        req_shared.match_info = {}
        req_shared.path = "/inbox"
        req_shared.raw_path = "/inbox"
        req_shared.headers = shared_headers
        req_shared.method = "POST"
        req_shared.read = AsyncMock(return_value=body_bytes)

        with patch("activitypub.resolve_public_key", new_callable=AsyncMock) as mock_resolve, \
             patch("activitypub.fetch_remote_actor", new_callable=AsyncMock) as mock_fetch, \
             patch("activitypub.deliver_to_inbox", new_callable=AsyncMock) as mock_deliver:

            mock_resolve.return_value = (remote_pub, {"id": remote_actor_id})
            mock_fetch.return_value = {
                "id": remote_actor_id,
                "inbox": "https://remote.social/users/carol/inbox",
                "endpoints": {"sharedInbox": "https://remote.social/inbox"}
            }

            resp = asyncio.run(bot.handle_ap_inbox(req_shared))
            self.assertEqual(resp.status, 202)
            followers = database.get_ap_followers(token)
            self.assertEqual(len(followers), 1)
            self.assertEqual(followers[0]["follower_actor_id"], remote_actor_id)

        # 3. Undo(Follow) via shared /inbox
        undo_act = {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": "https://remote.social/activities/2",
            "type": "Undo",
            "actor": remote_actor_id,
            "object": follow_act
        }
        undo_bytes = json.dumps(undo_act).encode("utf-8")
        undo_headers = activitypub.sign_headers("POST", "https://dc.gluek.info/inbox", undo_bytes, remote_priv, remote_key_id)
        req_undo = MagicMock()
        req_undo.match_info = {}
        req_undo.path = "/inbox"
        req_undo.raw_path = "/inbox"
        req_undo.headers = undo_headers
        req_undo.method = "POST"
        req_undo.read = AsyncMock(return_value=undo_bytes)

        with patch("activitypub.resolve_public_key", new_callable=AsyncMock) as mock_resolve:
            mock_resolve.return_value = (remote_pub, {"id": remote_actor_id})
            resp = asyncio.run(bot.handle_ap_inbox(req_undo))
            self.assertEqual(resp.status, 202)
            self.assertEqual(database.get_ap_followers_count(token), 0)

    def test_robots_txt_gotosocial(self):
        req = MagicMock()
        resp = asyncio.run(bot.handle_robots_txt(req))
        self.assertEqual(resp.status, 200)
        self.assertIn("User-agent: GPTBot", resp.text)
        self.assertIn("User-agent: ClaudeBot", resp.text)
        self.assertIn("Allow: /c/", resp.text)
        self.assertIn("Allow: /media/", resp.text)
        self.assertIn("Disallow: /.well-known/webfinger", resp.text)

    def test_multislash_route_and_base_url_cleanup(self):
        token = "cleanurltok1"
        database.add_catalog_channel(chat_id=109, name="Clean Chan", description="", member_count=1, invite_link="", token=token)
        priv, pub = activitypub.get_or_create_actor_keys(token)

        # 1. build_actor_json strips trailing slash
        actor = activitypub.build_actor_json({"token": token, "name": "Clean Chan"}, "https://dc.gluek.info///", pub)
        self.assertEqual(actor["id"], f"https://dc.gluek.info/c/{token}")
        self.assertEqual(actor["publicKey"]["id"], f"https://dc.gluek.info/c/{token}#main-key")
        self.assertEqual(actor["endpoints"]["sharedInbox"], "https://dc.gluek.info/inbox")

        # 2. build_note strips trailing slash
        note = activitypub.build_note({"token": token}, {"id": 123, "text": "Hi"}, "https://dc.gluek.info/")
        self.assertEqual(note["attributedTo"], f"https://dc.gluek.info/c/{token}")
        self.assertNotIn("//c/", note["attributedTo"])

        # 3. bot._get_base_url strips trailing slashes and whitespace
        database.set_config("base_url", "  https://dc.gluek.info///  ")
        req = MagicMock()
        req.headers = {}
        self.assertEqual(bot._get_base_url(req), "https://dc.gluek.info")


if __name__ == "__main__":
    unittest.main()
