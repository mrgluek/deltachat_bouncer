import os
import sys
import unittest
import time
import shutil
from unittest.mock import MagicMock, patch

# Setup test environment
TEST_DB = "test_webpreview.db"
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
        def __init__(self, text="", body=None, status=200, content_type="text/plain", headers=None):
            self.text = text
            self.body = body or (text.encode('utf-8') if text else b"")
            self.status = status
            self.content_type = content_type
            self.headers = headers or {}
    mock_web.Response = MockResponse
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
import bot

if bot.web is None:
    bot.web = sys.modules.get('aiohttp.web', MagicMock())

# Ensure bot.qrcode mock writes bytes when save is called
if hasattr(bot, "qrcode") and isinstance(bot.qrcode, MagicMock):
    def _fake_save(buf, *args, **kwargs):
        buf.write(b"fake_qr_data")
    _mock_img = MagicMock()
    _mock_img.save.side_effect = _fake_save
    _mock_qr_instance = MagicMock()
    _mock_qr_instance.make_image.return_value = _mock_img
    bot.qrcode.QRCode.return_value = _mock_qr_instance


class TestWebPreview(unittest.TestCase):
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

    def test_generate_channel_token(self):
        token1 = database.generate_channel_token()
        token2 = database.generate_channel_token()
        self.assertEqual(len(token1), 12)
        self.assertEqual(len(token2), 12)
        self.assertNotEqual(token1, token2)
        valid_chars = set("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
        self.assertTrue(all(c in valid_chars for c in token1))

    def test_add_and_get_catalog_channel(self):
        token = database.add_catalog_channel(
            chat_id=1001,
            name="Tech News",
            description="All about technology",
            member_count=42,
            invite_link="https://i.delta.chat/#tech"
        )
        self.assertIsNotNone(token)
        self.assertEqual(len(token), 12)

        # Retrieve by token
        ch = database.get_catalog_channel_by_token(token)
        self.assertIsNotNone(ch)
        self.assertEqual(ch["name"], "Tech News")
        self.assertEqual(ch["description"], "All about technology")
        self.assertEqual(ch["token"], token)
        self.assertEqual(ch["is_deleted"], 0)

        # Retrieve by chat_id
        ch_by_chat = database.get_catalog_channel_by_chat_id(1001)
        self.assertIsNotNone(ch_by_chat)
        self.assertEqual(ch_by_chat["id"], ch["id"])

        # Retrieve all
        channels = database.get_all_catalog_channels()
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0]["token"], token)

    def test_soft_remove_catalog_channel(self):
        token = database.add_catalog_channel(
            chat_id=1002,
            name="Crypto",
            description="Crypto alerts",
            member_count=10,
            invite_link="https://i.delta.chat/#crypto"
        )
        ch = database.get_catalog_channel_by_token(token)

        # Soft remove using chat_id
        success = database.remove_catalog_channel(ch["chat_id"])
        self.assertTrue(success)

        # Should not appear in active channels
        active = database.get_all_catalog_channels(include_deleted=False)
        self.assertEqual(len(active), 0)

        # Should appear in include_deleted=True
        all_channels = database.get_all_catalog_channels(include_deleted=True)
        self.assertEqual(len(all_channels), 1)
        self.assertEqual(all_channels[0]["is_deleted"], 1)
        self.assertIsNotNone(all_channels[0]["deleted_at"])

        # Lookup by token still finds it (for tombstone page)
        ch_deleted = database.get_catalog_channel_by_token(token)
        self.assertIsNotNone(ch_deleted)
        self.assertEqual(ch_deleted["is_deleted"], 1)

    def test_channel_posts_caching_and_pruning(self):
        token = database.add_catalog_channel(
            chat_id=2001,
            name="Delta Chat Updates",
            description="DC news",
            member_count=100,
            invite_link="https://i.delta.chat/#dc"
        )
        
        # Add 105 posts
        for i in range(1, 106):
            database.save_channel_post(
                chat_id=2001,
                msg_id=i,
                from_name=f"Admin {i}",
                text=f"Message {i}",
                timestamp=1700000000.0 + i
            )

        # Prune to max 100
        pruned = database.prune_channel_posts(2001, keep_count=100)
        self.assertEqual(pruned, 5)

        # Retrieve posts (most recent first)
        posts = database.get_channel_posts(2001, limit=100)
        self.assertEqual(len(posts), 100)
        self.assertEqual(posts[0]["msg_id"], 105)
        self.assertEqual(posts[-1]["msg_id"], 6)

    def test_html_generation(self):
        token = database.add_catalog_channel(
            chat_id=3001,
            name="Design",
            description="UI/UX talks",
            member_count=15,
            invite_link="https://i.delta.chat/#design"
        )
        channel = database.get_catalog_channel_by_token(token)

        # Landing page HTML
        landing_html = bot.get_landing_page_html()
        self.assertIn("Delta Chat Bouncer", landing_html)

        # Channel preview HTML
        posts = [
            {
                "msg_id": 1,
                "text": "Hello world! Check https://delta.chat",
                "from_name": "Alice",
                "timestamp": 1700000000.0,
                "media_type": None,
                "media_filename": None,
                "media_path": None
            },
            {
                "msg_id": 2,
                "text": "Image caption",
                "from_name": "Bob",
                "timestamp": 1700000100.0,
                "media_type": "image",
                "media_filename": "photo.jpg",
                "media_path": "test_media_dir/token/photo.jpg"
            }
        ]
        # Channel preview without ingress
        preview_html = bot.get_channel_preview_html(channel, posts, "https://channels.example.com")
        self.assertIn("Design", preview_html)
        self.assertIn("UI/UX talks", preview_html)
        self.assertIn("Hello world!", preview_html)
        self.assertIn("https://delta.chat", preview_html)
        self.assertIn("photo.jpg", preview_html)
        self.assertIn(f"/c/{token}/rss.xml", preview_html)
        self.assertIn("15 subscribers", preview_html)
        self.assertIn('href="/"', preview_html)

        # Channel preview with 1 member (shows Channel badge)
        channel_single = dict(channel)
        channel_single["member_count"] = 1
        single_html = bot.get_channel_preview_html(channel_single, [], "")
        self.assertIn("📢 Channel", single_html)

        # Channel preview with ingress
        ingress = "/api/hassio_ingress/token123"
        preview_ingress = bot.get_channel_preview_html(channel, posts, "", ingress_path=ingress)
        self.assertIn(f"{ingress}/c/{token}/avatar.png", preview_ingress)
        self.assertIn(f"{ingress}/c/{token}/qr.png", preview_ingress)
        self.assertIn(f"{ingress}/media/{token}/2/photo.jpg", preview_ingress)
        self.assertIn(f'href="{ingress}/"', preview_ingress)

        # Landing page with ingress
        landing_ingress = bot.get_landing_page_html(ingress_path=ingress)
        self.assertIn(f"{ingress}/icon.png", landing_ingress)
        self.assertIn(f"{ingress}/qr.png", landing_ingress)

        # Tombstone HTML
        tombstone_html = bot.get_tombstone_html(channel["name"], ingress_path=ingress)
        self.assertIn("Channel Removed", tombstone_html)
        self.assertIn("Design", tombstone_html)
        self.assertIn("has been removed from the public catalog", tombstone_html)
        self.assertIn(f'href="{ingress}/"', tombstone_html)
        self.assertIn(f'{ingress}/icon.png', tombstone_html)

        # 404 HTML
        not_found_html = bot.get_404_html(ingress_path=ingress)
        self.assertIn("Channel Not Found", not_found_html)
        self.assertIn(f'href="{ingress}/"', not_found_html)
        self.assertIn(f'{ingress}/icon.png', not_found_html)

    def test_rss_xml_generation(self):
        token = database.add_catalog_channel(
            chat_id=4001,
            name="Open Source",
            description="FOSS updates",
            member_count=55,
            invite_link="https://i.delta.chat/#foss"
        )
        channel = database.get_catalog_channel_by_token(token)
        posts = [
            {
                "msg_id": 10,
                "text": "Latest release v1.0 is here!",
                "from_name": "Maintainer",
                "timestamp": 1700000000.0,
                "media_type": None,
                "media_filename": None,
                "media_path": None
            }
        ]
        rss_xml = bot.get_channel_rss_xml(channel, posts, "https://channels.example.com")
        self.assertIn("<?xml version=\"1.0\" encoding=\"UTF-8\"?>", rss_xml)
        self.assertIn("<title><![CDATA[Open Source]]></title>", rss_xml)
        self.assertIn("Latest release v1.0 is here!", rss_xml)
        self.assertIn(f"https://channels.example.com/c/{token}", rss_xml)

    @patch('bot._is_dc_admin')
    def test_url_command(self, mock_is_admin):
        mock_bot = MagicMock()
        mock_bot.rpc.send_msg = MagicMock()
        mock_event = MagicMock()

        # Non-admin
        mock_is_admin.return_value = False
        bot.url_command(mock_bot, 123, mock_event)
        self.assertIn("bot administrator", mock_bot.rpc.send_msg.call_args[0][2].text)

        # Admin view default
        mock_is_admin.return_value = True
        mock_event.payload = ""
        bot.url_command(mock_bot, 123, mock_event)
        last_msg = mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("Current base web URL", last_msg)

        # Admin set invalid URL
        mock_event.payload = "ftp://invalid.com"
        bot.url_command(mock_bot, 123, mock_event)
        last_msg = mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("Invalid URL", last_msg)

        # Admin set valid URL
        mock_event.payload = "https://channels.mybot.org/"
        bot.url_command(mock_bot, 123, mock_event)
        last_msg = mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("Base web URL has been set to", last_msg)
        self.assertIn("https://channels.mybot.org", last_msg)
        self.assertEqual(database.get_config("base_url"), "https://channels.mybot.org")

    def test_dchannels_output_contains_preview_url(self):
        token = database.add_catalog_channel(
            chat_id=5001,
            name="News Channel",
            description="Daily headlines",
            member_count=77,
            invite_link="https://i.delta.chat/#news"
        )
        database.set_config("base_url", "https://bouncer.example.org")

        mock_bot = MagicMock()
        mock_bot.rpc.send_msg = MagicMock()
        mock_event = MagicMock()

        bot.dchannels_command(mock_bot, 123, mock_event)
        sent_text = mock_bot.rpc.send_msg.call_args[0][2].text

        self.assertIn("News Channel", sent_text)
        self.assertIn(f"🌐 Preview: https://bouncer.example.org/c/{token}", sent_text)

    def test_format_markdown_html(self):
        # 1. Bold
        html_bold1 = bot.format_markdown_html("This is **bold** text")
        self.assertIn("<strong>bold</strong>", html_bold1)
        html_bold2 = bot.format_markdown_html("This is __bold__ text")
        self.assertIn("<strong>bold</strong>", html_bold2)

        # 2. Italic
        html_italic1 = bot.format_markdown_html("This is *italic* text")
        self.assertIn("<em>italic</em>", html_italic1)
        html_italic2 = bot.format_markdown_html("This is _italic_ text")
        self.assertIn("<em>italic</em>", html_italic2)

        # 3. Strikethrough
        html_strike = bot.format_markdown_html("This is ~~strike~~ text")
        self.assertIn("<del>strike</del>", html_strike)

        # 4. Inline code
        html_code = bot.format_markdown_html("Use `print('hello')` function")
        self.assertIn("<code>print(&#x27;hello&#x27;)</code>", html_code)

        # 5. Code block
        block_input = "```python\ndef greet():\n    return 'hi' < 'hello'\n```"
        html_block = bot.format_markdown_html(block_input)
        self.assertIn('<pre><code class="language-python">def greet():\n    return &#x27;hi&#x27; &lt; &#x27;hello&#x27;</code></pre>', html_block)

        # 6. Spoilers
        html_spoiler = bot.format_markdown_html("The killer is ||John Doe||!")
        self.assertIn('<span class="spoiler"', html_spoiler)
        self.assertIn("John Doe</span>", html_spoiler)

        # 7. Blockquotes
        html_quote = bot.format_markdown_html("> First quote line\n> Second quote line")
        self.assertIn("<blockquote>First quote line<br>Second quote line</blockquote>", html_quote)

        # 8. Markdown links
        html_link = bot.format_markdown_html("Visit [Delta Chat](https://delta.chat) today")
        self.assertIn('<a href="https://delta.chat" target="_blank" rel="noopener noreferrer">Delta Chat</a>', html_link)

        # 9. Autolinking raw URLs
        html_autolink = bot.format_markdown_html("Check out https://gluek.info/blog for updates")
        self.assertIn('<a href="https://gluek.info/blog" target="_blank" rel="noopener noreferrer">https://gluek.info/blog</a>', html_autolink)

        # 10. Security / XSS Immunity
        xss_input = "<script>alert('pwned')</script> and <img src=x onerror=alert(1)> and [xss](javascript:alert(1))"
        html_safe = bot.format_markdown_html(xss_input)
        self.assertNotIn("<script>", html_safe)
        self.assertNotIn("<img", html_safe)
        self.assertNotIn('href="javascript:', html_safe)
        self.assertIn("&lt;script&gt;", html_safe)

    def test_cache_and_invalidation(self):
        token = database.add_catalog_channel(
            chat_id=6001,
            name="Cache Channel",
            description="Testing cache",
            member_count=5,
            invite_link="https://i.delta.chat/#cache"
        )
        # Populate caches manually
        bot._channel_preview_cache[f"{token}:"] = (time.time() + 60, "etag1", "<html>test</html>")
        bot._channel_rss_cache[f"{token}:https://example.com"] = (time.time() + 120, "etag2", "<xml>test</xml>")

        self.assertIn(f"{token}:", bot._channel_preview_cache)
        self.assertIn(f"{token}:https://example.com", bot._channel_rss_cache)

        # Invalidate specific channel by token
        bot.invalidate_channel_cache(token=token)
        self.assertNotIn(f"{token}:", bot._channel_preview_cache)
        self.assertNotIn(f"{token}:https://example.com", bot._channel_rss_cache)

        # Populate again and invalidate by chat_id
        bot._channel_preview_cache[f"{token}:"] = (time.time() + 60, "etag1", "<html>test</html>")
        bot.invalidate_channel_cache(chat_id=6001)
        self.assertNotIn(f"{token}:", bot._channel_preview_cache)

        # Global invalidate
        bot._channel_preview_cache["other:"] = (time.time() + 60, "etag1", "<html>test</html>")
        bot.invalidate_channel_cache()
        self.assertEqual(len(bot._channel_preview_cache), 0)

    def test_qr_code_caching(self):
        bot._qr_cache.clear()
        link = "https://i.delta.chat/#testqr"
        b1, mime1 = bot._generate_qr_bytes(link, fmt="png", box_size=6)
        self.assertEqual(mime1, "image/png")
        self.assertTrue(len(b1) > 0)
        self.assertIn(f"{link}:png:6", bot._qr_cache)

        # Second call returns from cache
        b2, mime2 = bot._generate_qr_bytes(link, fmt="png", box_size=6)
        self.assertEqual(b1, b2)

    def test_robots_txt_disallow_all(self):
        import asyncio
        req = MagicMock()
        resp = asyncio.run(bot.handle_robots_txt(req))
        self.assertEqual(resp.status, 200)
        self.assertIn("User-agent: *", resp.text)
        self.assertIn("Disallow: /", resp.text)
        self.assertNotIn("Allow: /", resp.text)


if __name__ == "__main__":
    unittest.main()
