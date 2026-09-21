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
    class MockFileResponse:
        def __init__(self, path, headers=None, status=200):
            self.path = path
            self.headers = headers or {}
            self.content_type = self.headers.get("Content-Type", "")
            self.status = status
    mock_web.Response = MockResponse
    mock_web.FileResponse = MockFileResponse
    mock_aiohttp.web = mock_web
    sys.modules['aiohttp'] = mock_aiohttp
    sys.modules['aiohttp.web'] = mock_web
    web = mock_web

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
import channels
import commands
import formatting
import handlers
import security
import state
import web.ap_routes as pw_ap_routes
import web.routes as pw_routes
import web.templates.channel as pw_tpl_channel
import web.templates.errors as pw_tpl_errors
import web.templates.feed as pw_tpl_feed
import web.templates.landing as pw_tpl_landing

if security.web is None:
    security.web = sys.modules.get('aiohttp.web', MagicMock())
if pw_routes.web is None:
    pw_routes.web = sys.modules.get('aiohttp.web', MagicMock())
if pw_ap_routes.web is None:
    pw_ap_routes.web = sys.modules.get('aiohttp.web', MagicMock())

# Ensure pw_routes.qrcode mock writes bytes when save is called
if hasattr(pw_routes, "qrcode") and isinstance(pw_routes.qrcode, MagicMock):
    def _fake_save(buf, *args, **kwargs):
        buf.write(b"fake_qr_data")
    _mock_img = MagicMock()
    _mock_img.save.side_effect = _fake_save
    _mock_qr_instance = MagicMock()
    _mock_qr_instance.make_image.return_value = _mock_img
    pw_routes.qrcode.QRCode.return_value = _mock_qr_instance


class TestWebPreview(unittest.TestCase):
    def setUp(self):
        database.close_db()
        database.DB_PATH = TEST_DB
        database.init_db()
        state.CHANNEL_MEDIA_DIR = "test_media_dir"
        state.index_page_html_cache = None
        if os.path.exists(state.CHANNEL_MEDIA_DIR):
            shutil.rmtree(state.CHANNEL_MEDIA_DIR, ignore_errors=True)

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
        landing_html = pw_tpl_landing.get_landing_page_html()
        self.assertIn("Delta Chat Bouncer", landing_html)
        self.assertIn("🛡️ Delta Chat Bouncer Bot", landing_html)
        self.assertIn("top-qr-btn", landing_html)
        self.assertIn("📱 QR Code", landing_html)
        self.assertIn("--color-primary: #415e6b;", landing_html)
        self.assertIn("background-light.jpg", landing_html)
        self.assertIn("theme-switcher", landing_html)
        self.assertIn("light-theme-button", landing_html)
        self.assertIn("dark-theme-button", landing_html)
        self.assertIn("system-theme-button", landing_html)
        self.assertIn("data-theme", landing_html)
        self.assertIn(".feature-text h3", landing_html)
        self.assertIn("color: var(--text-main);", landing_html)

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
        preview_html = pw_tpl_channel.get_channel_preview_html(channel, posts, "https://channels.example.com")
        self.assertIn("Design", preview_html)
        self.assertIn("UI/UX talks", preview_html)
        self.assertIn("Hello world!", preview_html)
        self.assertIn("https://delta.chat", preview_html)
        self.assertIn("photo.jpg", preview_html)
        self.assertIn(f"/c/{token}/rss.xml", preview_html)
        self.assertIn(f"@{token}@channels.example.com", preview_html)
        self.assertIn("fedi-tag-btn", preview_html)
        self.assertIn("15 subscribers", preview_html)
        self.assertIn('href="/"', preview_html)
        self.assertIn("https://git.gluek.info/gluek/deltachat_bouncer", preview_html)
        self.assertIn("--color-primary: #415e6b;", preview_html)
        self.assertIn("background-light.jpg", preview_html)
        self.assertIn("<span>🗨️</span>", preview_html)
        self.assertIn("channel-default.svg", preview_html)
        self.assertIn("onerror=\"this.src='/channel-default.svg'\"", preview_html)
        self.assertIn("theme-switcher", preview_html)
        self.assertIn("light-theme-button", preview_html)
        self.assertIn("rgba(17, 27, 33, 0.55)", preview_html)

        # Channel preview with 1 member (shows Channel badge)
        channel_single = dict(channel)
        channel_single["member_count"] = 1
        single_html = pw_tpl_channel.get_channel_preview_html(channel_single, [], "")
        self.assertIn("📢 Channel", single_html)

        # Channel preview with ingress
        ingress = "/api/hassio_ingress/token123"
        preview_ingress = pw_tpl_channel.get_channel_preview_html(channel, posts, "", ingress_path=ingress)
        self.assertIn(f"{ingress}/c/{token}/avatar.png", preview_ingress)
        self.assertIn(f"{ingress}/c/{token}/qr.png", preview_ingress)
        self.assertIn(f"{ingress}/media/{token}/2/photo.jpg", preview_ingress)
        self.assertIn(f'href="{ingress}/"', preview_ingress)
        self.assertIn(f"onerror=\"this.src='{ingress}/channel-default.svg'\"", preview_ingress)

        # Landing page with ingress (no invite link configured)
        landing_ingress_no_link = pw_tpl_landing.get_landing_page_html(ingress_path=ingress)
        self.assertIn(f"{ingress}/icon.png", landing_ingress_no_link)
        self.assertIn("Bot Link Unavailable", landing_ingress_no_link)
        self.assertIn("Bot invite link is not configured yet", landing_ingress_no_link)

        # Landing page with ingress (with bot invite link)
        database.set_config("bot_invite_link", "https://i.delta.chat/#botinvite")
        landing_ingress = pw_tpl_landing.get_landing_page_html(ingress_path=ingress)
        self.assertIn(f"{ingress}/icon.png", landing_ingress)
        self.assertIn(f"{ingress}/qr.png", landing_ingress)
        self.assertIn("Add Bouncer Bot", landing_ingress)
        self.assertIn("Copy Link", landing_ingress)
        self.assertIn('href="https://i.delta.chat/#botinvite"', landing_ingress)
        self.assertIn("<span>🗨️</span> Add Bot to Delta Chat", landing_ingress)
        self.assertIn("rgba(17, 27, 33, 0.55)", landing_ingress)

        # Tombstone HTML
        tombstone_html = pw_tpl_errors.get_tombstone_html(channel["name"], ingress_path=ingress)
        self.assertIn("Channel Removed", tombstone_html)
        self.assertIn("Design", tombstone_html)
        self.assertIn("has been removed from the public catalog", tombstone_html)
        self.assertIn(f'href="{ingress}/"', tombstone_html)
        self.assertIn(f'{ingress}/icon.png', tombstone_html)
        self.assertIn("--color-primary: #415e6b;", tombstone_html)
        self.assertIn(f"{ingress}/background-light.jpg", tombstone_html)
        self.assertIn("theme-switcher", tombstone_html)

        # 404 HTML
        not_found_html = pw_tpl_errors.get_404_html(ingress_path=ingress)
        self.assertIn("Channel Not Found", not_found_html)
        self.assertIn(f'href="{ingress}/"', not_found_html)
        self.assertIn(f'{ingress}/icon.png', not_found_html)
        self.assertIn("--color-primary: #415e6b;", not_found_html)
        self.assertIn(f"{ingress}/background-light.jpg", not_found_html)
        self.assertIn("theme-switcher", not_found_html)

    def test_channel_preview_without_invite_link(self):
        """Verify channel preview disables action button and omits QR modal when invite link is empty."""
        channel_no_invite = {
            "token": "noinvite123",
            "name": "Private Stream",
            "description": "No invite link configured",
            "member_count": 5,
            "invite_link": "",
        }
        posts = [{"msg_id": 1, "text": "Post 1", "from_name": "Admin", "timestamp": time.time()}]
        html_out = pw_tpl_channel.get_channel_preview_html(channel_no_invite, posts, "https://channels.example.com")
        self.assertIn("Invite Link Unavailable", html_out)
        self.assertIn('disabled style="opacity: 0.55; cursor: not-allowed;"', html_out)
        self.assertNotIn("Show QR Code", html_out)
        self.assertNotIn('id="qr-modal"', html_out)
        # RSS link should still be available
        self.assertIn("/c/noinvite123/rss.xml", html_out)

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
        rss_xml = pw_tpl_feed.get_channel_rss_xml(channel, posts, "https://channels.example.com")
        self.assertIn("<?xml version=\"1.0\" encoding=\"UTF-8\"?>", rss_xml)
        self.assertIn("<title><![CDATA[Open Source]]></title>", rss_xml)
        self.assertIn("Latest release v1.0 is here!", rss_xml)
        self.assertIn(f"https://channels.example.com/c/{token}", rss_xml)

    @patch('dc_helpers._is_dc_admin')
    def test_url_command(self, mock_is_admin):
        mock_bot = MagicMock()
        mock_bot.rpc.send_msg = MagicMock()
        mock_event = MagicMock()

        # Non-admin
        mock_is_admin.return_value = False
        commands.url_command(mock_bot, 123, mock_event)
        self.assertIn("bot administrator", mock_bot.rpc.send_msg.call_args[0][2].text)

        # Admin view default
        mock_is_admin.return_value = True
        mock_event.payload = ""
        commands.url_command(mock_bot, 123, mock_event)
        last_msg = mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("Current base web URL", last_msg)

        # Admin set invalid URL
        mock_event.payload = "ftp://invalid.com"
        commands.url_command(mock_bot, 123, mock_event)
        last_msg = mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("Invalid URL", last_msg)

        # Admin set valid URL
        mock_event.payload = "https://channels.mybot.org/"
        commands.url_command(mock_bot, 123, mock_event)
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

        channels.dchannels_command(mock_bot, 123, mock_event)
        sent_text = mock_bot.rpc.send_msg.call_args[0][2].text

        self.assertIn("News Channel", sent_text)
        self.assertIn(f"🌐 Preview: https://bouncer.example.org/c/{token}", sent_text)
        ch_id = database.get_catalog_channel_by_token(token)['id']
        expected_block = f"/dchannel{ch_id} **News Channel**\nDaily headlines\n🌐 Preview: https://bouncer.example.org/c/{token}"
        self.assertIn(expected_block, sent_text)

    def test_format_markdown_html(self):
        # 1. Bold
        html_bold1 = formatting.format_markdown_html("This is **bold** text")
        self.assertIn("<strong>bold</strong>", html_bold1)
        html_bold2 = formatting.format_markdown_html("This is __bold__ text")
        self.assertIn("<strong>bold</strong>", html_bold2)

        # 2. Italic
        html_italic1 = formatting.format_markdown_html("This is *italic* text")
        self.assertIn("<em>italic</em>", html_italic1)
        html_italic2 = formatting.format_markdown_html("This is _italic_ text")
        self.assertIn("<em>italic</em>", html_italic2)

        # 3. Strikethrough
        html_strike = formatting.format_markdown_html("This is ~~strike~~ text")
        self.assertIn("<del>strike</del>", html_strike)

        # 4. Inline code
        html_code = formatting.format_markdown_html("Use `print('hello')` function")
        self.assertIn("<code>print(&#x27;hello&#x27;)</code>", html_code)

        # 5. Code block
        block_input = "```python\ndef greet():\n    return 'hi' < 'hello'\n```"
        html_block = formatting.format_markdown_html(block_input)
        self.assertIn('<pre><code class="language-python">def greet():\n    return &#x27;hi&#x27; &lt; &#x27;hello&#x27;</code></pre>', html_block)

        # 6. Spoilers
        html_spoiler = formatting.format_markdown_html("The killer is ||John Doe||!")
        self.assertIn('<span class="spoiler"', html_spoiler)
        self.assertIn("John Doe</span>", html_spoiler)

        # 7. Blockquotes
        html_quote = formatting.format_markdown_html("> First quote line\n> Second quote line")
        self.assertIn("<blockquote>First quote line<br>Second quote line</blockquote>", html_quote)

        # 8. Markdown links
        html_link = formatting.format_markdown_html("Visit [Delta Chat](https://delta.chat) today")
        self.assertIn('<a href="https://delta.chat" target="_blank" rel="noopener noreferrer">Delta Chat</a>', html_link)

        # 9. Autolinking raw URLs
        html_autolink = formatting.format_markdown_html("Check out https://gluek.info/blog for updates")
        self.assertIn('<a href="https://gluek.info/blog" target="_blank" rel="noopener noreferrer">https://gluek.info/blog</a>', html_autolink)

        # 10. Security / XSS Immunity
        xss_input = "<script>alert('pwned')</script> and <img src=x onerror=alert(1)> and [xss](javascript:alert(1))"
        html_safe = formatting.format_markdown_html(xss_input)
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
        state._channel_preview_cache[f"{token}:"] = (time.time() + 60, "etag1", "<html>test</html>")
        state._channel_rss_cache[f"{token}:https://example.com"] = (time.time() + 120, "etag2", "<xml>test</xml>")

        self.assertIn(f"{token}:", state._channel_preview_cache)
        self.assertIn(f"{token}:https://example.com", state._channel_rss_cache)

        # Invalidate specific channel by token
        pw_routes.invalidate_channel_cache(token=token)
        self.assertNotIn(f"{token}:", state._channel_preview_cache)
        self.assertNotIn(f"{token}:https://example.com", state._channel_rss_cache)

        # Populate again and invalidate by chat_id
        state._channel_preview_cache[f"{token}:"] = (time.time() + 60, "etag1", "<html>test</html>")
        pw_routes.invalidate_channel_cache(chat_id=6001)
        self.assertNotIn(f"{token}:", state._channel_preview_cache)

        # Global invalidate
        state._channel_preview_cache["other:"] = (time.time() + 60, "etag1", "<html>test</html>")
        pw_routes.invalidate_channel_cache()
        self.assertEqual(len(state._channel_preview_cache), 0)

    def test_qr_code_caching(self):
        state._qr_cache.clear()
        link = "https://i.delta.chat/#testqr"
        b1, mime1 = pw_routes._generate_qr_bytes(link, fmt="png", box_size=6)
        self.assertEqual(mime1, "image/png")
        self.assertTrue(len(b1) > 0)
        self.assertIn(f"{link}:png:6", state._qr_cache)

        # Second call returns from cache
        b2, mime2 = pw_routes._generate_qr_bytes(link, fmt="png", box_size=6)
        self.assertEqual(b1, b2)

    def test_robots_txt_disallow_all(self):
        import asyncio
        req = MagicMock()
        resp = asyncio.run(pw_routes.handle_robots_txt(req))
        self.assertEqual(resp.status, 200)
        self.assertIn("User-agent: *", resp.text)
        self.assertIn("Disallow: /", resp.text)
        self.assertIn("User-agent: GPTBot", resp.text)
        self.assertIn("Allow: /c/", resp.text)
        self.assertIn("Allow: /media/", resp.text)

    def test_handle_background(self):
        import asyncio
        req = MagicMock()
        req.path = "/background.jpg"
        resp = asyncio.run(pw_routes.handle_background(req))
        self.assertEqual(resp.status, 200)
        self.assertIn("immutable", resp.headers.get("Cache-Control", ""))
        self.assertIsInstance(resp, web.FileResponse)

        req_light = MagicMock()
        req_light.path = "/background-light.jpg"
        resp_light = asyncio.run(pw_routes.handle_background(req_light))
        self.assertEqual(resp_light.status, 200)
        self.assertIn("immutable", resp_light.headers.get("Cache-Control", ""))
        self.assertIsInstance(resp_light, web.FileResponse)

    def test_dc_fallback_stripping_and_text_preservation(self):
        # 1. Stripping DC attachment fallback strings while keeping author text
        raw_with_img = "what if you just fuck off???\n\n>> 🧑‍💼 Debuging Memes Channel << [Image – 304.26 KiB]"
        res = formatting.format_markdown_html(raw_with_img)
        self.assertNotIn("[Image – 304.26 KiB]", res)
        self.assertNotIn("[Image", res)
        self.assertIn("what if you just fuck off???", res)
        self.assertIn("&gt;&gt; 🧑‍💼 Debuging Memes Channel &lt;&lt;", res)

        # 2. Only fallback tag -> empty string
        only_fallback = "[Image – 500 KiB]"
        self.assertEqual(formatting.format_markdown_html(only_fallback), "")

        only_doc = "  [Document - report.pdf]  "
        self.assertEqual(formatting.format_markdown_html(only_doc), "")

    def test_image_optimization_and_webp_fallback(self):
        import asyncio
        token = database.add_catalog_channel(
            chat_id=8001,
            name="Media Channel",
            description="Images & WebP",
            member_count=12,
            invite_link="https://i.delta.chat/#media"
        )
        ch_dir = os.path.join(state.CHANNEL_MEDIA_DIR, token)
        os.makedirs(ch_dir, exist_ok=True)

        # 1. Existing .webp file served when .webp requested
        webp_path = os.path.join(ch_dir, "100_photo.webp")
        with open(webp_path, "wb") as f:
            f.write(b"RIFFdummyWEBP")

        req_webp = MagicMock()
        req_webp.match_info = {"token": token, "msg_id": "100", "filename": "photo.webp"}
        resp = asyncio.run(pw_routes.handle_media_file(req_webp))
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("Content-Type"), "image/webp")

        # 2. When .jpg requested, falls back to .webp if .webp exists
        req_jpg = MagicMock()
        req_jpg.match_info = {"token": token, "msg_id": "100", "filename": "photo.jpg"}
        resp_fallback = asyncio.run(pw_routes.handle_media_file(req_jpg))
        self.assertEqual(resp_fallback.status, 200)
        self.assertEqual(resp_fallback.headers.get("Content-Type"), "image/webp")

        # 3. Original file served when no .webp exists
        png_path = os.path.join(ch_dir, "101_graphic.png")
        with open(png_path, "wb") as f:
            f.write(b"\x89PNGdummy")

        req_png = MagicMock()
        req_png.match_info = {"token": token, "msg_id": "101", "filename": "graphic.png"}
        resp_png = asyncio.run(pw_routes.handle_media_file(req_png))
        self.assertEqual(resp_png.status, 200)
        self.assertEqual(resp_png.headers.get("Content-Type"), "image/png")

    def test_handle_dc_info_message_removes_channel_when_bot_removed(self):
        token = database.add_catalog_channel(
            chat_id=9001,
            name="News Channel",
            description="Breaking News",
            member_count=100,
            invite_link="https://i.delta.chat/#news"
        )
        self.assertIsNotNone(database.get_catalog_channel_by_chat_id(9001))

        mock_bot = MagicMock()
        mock_event = MagicMock()
        mock_msg = MagicMock()
        mock_msg.chat_id = 9001
        from deltachat2 import SystemMessageType
        mock_msg.system_message_type = SystemMessageType.MEMBER_REMOVED_FROM_GROUP
        mock_msg.info_contact_id = 1
        mock_msg.text = "You were removed from the group."
        mock_event.msg = mock_msg

        handlers.handle_dc_info_message(mock_bot, 1, mock_event)

        # Verify channel was soft-removed
        self.assertIsNone(database.get_catalog_channel_by_chat_id(9001))
        ch_del = database.get_catalog_channel_by_chat_id(9001, include_deleted=True)
        self.assertIsNotNone(ch_del)
        self.assertEqual(ch_del["is_deleted"], 1)

    def test_handle_dc_info_message_channel_text_fallback_removal(self):
        token = database.add_catalog_channel(
            chat_id=9002,
            name="Tech Channel",
            description="Tech discussions",
            member_count=50,
            invite_link="https://i.delta.chat/#tech"
        )
        self.assertIsNotNone(database.get_catalog_channel_by_chat_id(9002))

        mock_bot = MagicMock()
        mock_event = MagicMock()
        mock_msg = MagicMock()
        mock_msg.chat_id = 9002
        mock_msg.system_message_type = None
        mock_msg.info_contact_id = None
        mock_msg.text = "You were removed by the channel owner."
        mock_event.msg = mock_msg

        handlers.handle_dc_info_message(mock_bot, 1, mock_event)

        # Verify channel was soft-removed
        self.assertIsNone(database.get_catalog_channel_by_chat_id(9002))
        ch_del = database.get_catalog_channel_by_chat_id(9002, include_deleted=True)
        self.assertIsNotNone(ch_del)
        self.assertEqual(ch_del["is_deleted"], 1)

    def test_handle_dc_info_message_channel_other_member_removed(self):
        token = database.add_catalog_channel(
            chat_id=9003,
            name="Community Channel",
            description="Community chat",
            member_count=50,
            invite_link="https://i.delta.chat/#comm"
        )
        self.assertIsNotNone(database.get_catalog_channel_by_chat_id(9003))

        mock_bot = MagicMock()
        mock_bot.rpc.get_chat_contacts.return_value = [1, 20, 30]
        mock_event = MagicMock()
        mock_msg = MagicMock()
        mock_msg.chat_id = 9003
        from deltachat2 import SystemMessageType
        mock_msg.system_message_type = SystemMessageType.MEMBER_REMOVED_FROM_GROUP
        mock_msg.info_contact_id = 42
        mock_msg.text = "Member 42 removed by admin."
        mock_event.msg = mock_msg

        handlers.handle_dc_info_message(mock_bot, 1, mock_event)

        # Verify channel is NOT removed
        ch = database.get_catalog_channel_by_chat_id(9003)
        self.assertIsNotNone(ch)
        self.assertEqual(ch["is_deleted"], 0)
        # Member count updated (contacts excluding 1 -> 2 contacts)
        self.assertEqual(ch["member_count"], 2)

    def test_unlisted_channel_and_public_toggle(self):
        # 1. Added as unlisted
        token = database.add_catalog_channel(
            chat_id=9501,
            name="Unlisted Channel",
            description="Hidden chat",
            member_count=5,
            invite_link="https://i.delta.chat/#unlisted",
            is_public=0
        )
        ch = database.get_catalog_channel_by_token(token)
        self.assertIsNotNone(ch)
        self.assertEqual(ch["is_public"], 0)

        # 2. Filtering by public_only
        public_channels = database.get_all_catalog_channels(public_only=True)
        self.assertEqual(len(public_channels), 0)
        all_channels = database.get_all_catalog_channels(public_only=False)
        self.assertEqual(len(all_channels), 1)

        # 3. Toggle to public
        success = database.set_catalog_channel_public(ch["id"], 1)
        self.assertTrue(success)
        ch_pub = database.get_catalog_channel_by_id(ch["id"])
        self.assertEqual(ch_pub["is_public"], 1)
        public_channels = database.get_all_catalog_channels(public_only=True)
        self.assertEqual(len(public_channels), 1)

        # 4. Toggle back to unlisted by chat_id
        success2 = database.set_catalog_channel_public_by_chat_id(ch["chat_id"], 0)
        self.assertTrue(success2)
        ch_unlisted = database.get_catalog_channel_by_id(ch["id"])
        self.assertEqual(ch_unlisted["is_public"], 0)
        public_channels = database.get_all_catalog_channels(public_only=True)
        self.assertEqual(len(public_channels), 0)

    @patch('dc_helpers._is_dc_admin')
    def test_dchannels_admin_dm_vs_non_admin(self, mock_is_admin):
        # Setup 1 public and 1 unlisted channel
        tok_pub = database.add_catalog_channel(
            chat_id=9601,
            name="Public DC",
            description="Public updates",
            member_count=20,
            invite_link="https://i.delta.chat/#pub",
            is_public=1
        )
        tok_unlisted = database.add_catalog_channel(
            chat_id=9602,
            name="Secret DC",
            description="Internal tests",
            member_count=3,
            invite_link="https://i.delta.chat/#sec",
            is_public=0
        )
        ch_unlisted = database.get_catalog_channel_by_token(tok_unlisted)

        mock_bot = MagicMock()
        mock_event = MagicMock()
        mock_msg = MagicMock()
        mock_msg.chat_id = 123
        mock_msg.from_id = 456
        mock_event.msg = mock_msg

        # 1. Non-admin calls /dchannels
        mock_is_admin.return_value = False
        mock_bot.rpc.get_basic_chat_info.return_value = {"chat_type": "Single"}
        channels.dchannels_command(mock_bot, 1, mock_event)
        non_admin_reply = mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("Public DC", non_admin_reply)
        self.assertNotIn("Secret DC", non_admin_reply)
        self.assertNotIn("Publish: /dchannelpub", non_admin_reply)

        # 2. Admin calls /dchannels in a Group chat -> only public channels shown
        mock_is_admin.return_value = True
        mock_bot.rpc.get_basic_chat_info.return_value = {"chat_type": "Group"}
        channels.dchannels_command(mock_bot, 1, mock_event)
        group_admin_reply = mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("Public DC", group_admin_reply)
        self.assertNotIn("Secret DC", group_admin_reply)

        # 3. Admin calls /dchannels in private DM (Single) -> all channels shown with unlisted badge & publish hint
        mock_bot.rpc.get_basic_chat_info.return_value = {"chat_type": "Single"}
        channels.dchannels_command(mock_bot, 1, mock_event)
        admin_dm_reply = mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("Public DC", admin_dm_reply)
        self.assertIn("Secret DC", admin_dm_reply)
        self.assertIn("🔒 **Secret DC** [Unlisted]", admin_dm_reply)
        self.assertIn(f"Publish: /dchannelpub{ch_unlisted['id']}on", admin_dm_reply)

    @patch('dc_helpers._is_dc_admin')
    def test_dchannelpub_command_and_channel_id_access(self, mock_is_admin):
        tok = database.add_catalog_channel(
            chat_id=9701,
            name="Alpha Release",
            description="Alpha testing",
            member_count=8,
            invite_link="https://i.delta.chat/#alpha",
            is_public=0
        )
        ch = database.get_catalog_channel_by_token(tok)
        cid = ch["id"]

        mock_bot = MagicMock()
        mock_event = MagicMock()
        mock_msg = MagicMock()
        mock_msg.chat_id = 999
        mock_msg.from_id = 111
        mock_event.msg = mock_msg

        # 1. Non-admin queries /dchannel<ID> for unlisted channel -> not found
        mock_is_admin.return_value = False
        mock_msg.text = f"/dchannel{cid}"
        handlers.handle_all_messages(mock_bot, 1, mock_event)
        reply1 = mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("not found", reply1)

        # 2. Non-admin attempts /dchannelpub<ID>on -> unauthorized
        mock_msg.text = f"/dchannelpub{cid}on"
        handlers.handle_all_messages(mock_bot, 1, mock_event)
        reply2 = mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("administrator", reply2)

        # 3. Admin calls /dchannelpub<ID>on -> channel becomes public
        mock_is_admin.return_value = True
        mock_msg.text = f"/dchannelpub{cid}on"
        handlers.handle_all_messages(mock_bot, 1, mock_event)
        reply3 = mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("is now **public**", reply3)
        self.assertEqual(database.get_catalog_channel_by_id(cid)["is_public"], 1)

        # 4. Now non-admin queries /dchannel<ID> -> accessible
        mock_is_admin.return_value = False
        mock_msg.text = f"/dchannel{cid}"
        handlers.handle_all_messages(mock_bot, 1, mock_event)
        reply4 = mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("Alpha Release", reply4)
        self.assertNotIn("[Unlisted]", reply4)

        # 5. Admin calls /dchannelpub<ID>off -> channel becomes unlisted
        mock_is_admin.return_value = True
        mock_msg.text = f"/dchannelpub{cid}off"
        handlers.handle_all_messages(mock_bot, 1, mock_event)
        reply5 = mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("is now **unlisted**", reply5)
        self.assertEqual(database.get_catalog_channel_by_id(cid)["is_public"], 0)

        # 6. Admin queries /dchannel<ID> for unlisted channel -> can view with [Unlisted] badge and toggle hint
        mock_msg.text = f"/dchannel{cid}"
        handlers.handle_all_messages(mock_bot, 1, mock_event)
        reply6 = mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("Alpha Release", reply6)
        self.assertIn("[Unlisted]", reply6)
        self.assertIn(f"Publish: /dchannelpub{cid}on", reply6)

    def test_landing_page_unlisted_channel_excluded(self):
        state.index_page_html_cache = None
        database.add_catalog_channel(
            chat_id=9801,
            name="Public Showcase",
            description="Everyone sees this",
            member_count=50,
            invite_link="https://i.delta.chat/#public",
            is_public=1
        )
        tok_unlisted = database.add_catalog_channel(
            chat_id=9802,
            name="Hidden Unlisted",
            description="Nobody sees this on landing page",
            member_count=5,
            invite_link="https://i.delta.chat/#hidden",
            is_public=0
        )
        landing_html = pw_tpl_landing.get_landing_page_html()
        self.assertIn("Public Showcase", landing_html)
        self.assertNotIn("Hidden Unlisted", landing_html)

        # Web preview and RSS for unlisted channel still function normally via direct token
        ch_unlisted = database.get_catalog_channel_by_token(tok_unlisted)
        self.assertIsNotNone(ch_unlisted)
        preview_html = pw_tpl_channel.get_channel_preview_html(ch_unlisted, [], "https://example.com")
        self.assertIn("Hidden Unlisted", preview_html)

    def test_guess_media_content_type(self):
        import tempfile
        # Test extension mappings
        self.assertEqual(security._guess_media_content_type("photo.webp"), "image/webp")
        self.assertEqual(security._guess_media_content_type("photo.jpg"), "image/jpeg")
        self.assertEqual(security._guess_media_content_type("photo.jpeg"), "image/jpeg")
        self.assertEqual(security._guess_media_content_type("photo.png"), "image/png")
        self.assertEqual(security._guess_media_content_type("clip.mp4"), "video/mp4")
        self.assertEqual(security._guess_media_content_type("audio.ogg"), "audio/ogg")
        self.assertEqual(security._guess_media_content_type("icon.svg"), "image/svg+xml")

        # Test magic bytes inspection for extensionless or unknown files
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(b"RIFF\x00\x00\x00\x00WEBPVP8 ")
            tf_webp = tf.name
        try:
            self.assertEqual(security._guess_media_content_type(tf_webp), "image/webp")
        finally:
            os.remove(tf_webp)

        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(b"\x89PNG\r\n\x1a\n")
            tf_png = tf.name
        try:
            self.assertEqual(security._guess_media_content_type(tf_png), "image/png")
        finally:
            os.remove(tf_png)

    def test_channel_avatar_default_svg_when_no_avatar(self):
        import asyncio
        token = database.add_catalog_channel(
            chat_id=9901,
            name="No Avatar Channel",
            description="A channel without an avatar",
            member_count=7,
            invite_link="https://i.delta.chat/#noavatar"
        )
        # Ensure cached avatar does not exist
        cached_avatar = os.path.join(state.CHANNEL_MEDIA_DIR, token, "avatar.png")
        if os.path.exists(cached_avatar):
            os.remove(cached_avatar)

        mock_bot = MagicMock()
        mock_bot.rpc.get_basic_chat_info.return_value = {"id": 9901, "name": "No Avatar Channel"}
        mock_bot.rpc.get_full_chat_by_id.return_value = {"id": 9901, "name": "No Avatar Channel"}
        mock_bot.rpc.get_chat_contacts.return_value = []

        with patch.object(state, "dc_bot_instance", mock_bot), patch.object(state, "dc_accid", 1):
            req = MagicMock()
            req.match_info = {"token": token}
            resp = asyncio.run(pw_routes.handle_channel_avatar(req))
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.content_type, "image/svg+xml")
            self.assertIn(b"<svg", resp.body)
            # Must NOT return bot's icon.png
            self.assertFalse(isinstance(resp, web.FileResponse))

    def test_channel_avatar_fetches_profile_image_camelcase_and_snake_case(self):
        import asyncio
        import tempfile

        # 1. camelCase profileImage in get_basic_chat_info
        token_camel = database.add_catalog_channel(
            chat_id=9902,
            name="CamelCase Avatar",
            description="Uses profileImage",
            member_count=3,
            invite_link="https://i.delta.chat/#camel"
        )
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
            tf.write(b"\xff\xd8\xff\xe0dummyjpg")
            camel_avatar_file = tf.name

        try:
            mock_bot = MagicMock()
            mock_bot.rpc.get_basic_chat_info.return_value = {
                "id": 9902,
                "profileImage": camel_avatar_file
            }
            with patch.object(state, "dc_bot_instance", mock_bot), patch.object(state, "dc_accid", 1):
                req = MagicMock()
                req.match_info = {"token": token_camel}
                resp = asyncio.run(pw_routes.handle_channel_avatar(req))
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers.get("Content-Type"), "image/jpeg")
        finally:
            if os.path.exists(camel_avatar_file):
                os.remove(camel_avatar_file)

        # 2. snake_case profile_image in get_full_chat_by_id fallback
        token_snake = database.add_catalog_channel(
            chat_id=9903,
            name="SnakeCase Avatar",
            description="Uses profile_image in full chat",
            member_count=4,
            invite_link="https://i.delta.chat/#snake"
        )
        with tempfile.NamedTemporaryFile(suffix=".webp", delete=False) as tf:
            tf.write(b"RIFF\x00\x00\x00\x00WEBPVP8 ")
            snake_avatar_file = tf.name

        try:
            mock_bot = MagicMock()
            mock_bot.rpc.get_basic_chat_info.return_value = {"id": 9903}  # No profile image here
            mock_bot.rpc.get_full_chat_by_id.return_value = {
                "id": 9903,
                "profile_image": snake_avatar_file
            }
            with patch.object(state, "dc_bot_instance", mock_bot), patch.object(state, "dc_accid", 1):
                req = MagicMock()
                req.match_info = {"token": token_snake}
                resp = asyncio.run(pw_routes.handle_channel_avatar(req))
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers.get("Content-Type"), "image/webp")
        finally:
            if os.path.exists(snake_avatar_file):
                os.remove(snake_avatar_file)

    def test_handle_icon_and_avatar_path_customization(self):
        import asyncio
        import tempfile

        # Create temporary custom avatar image
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
            tf.write(b"\xff\xd8\xff\xe0customavatar")
            custom_avatar = tf.name

        old_avatar_env = os.environ.get("AVATAR_PATH")
        try:
            os.environ["AVATAR_PATH"] = custom_avatar
            resolved = pw_routes.get_bot_avatar_file_path()
            self.assertEqual(resolved, custom_avatar)

            req = MagicMock()
            req.path = "/icon.png"
            resp = asyncio.run(pw_routes.handle_icon(req))
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.path, custom_avatar)
            self.assertEqual(resp.headers.get("Content-Type"), "image/jpeg")

            # Route by custom filename directly
            req_custom = MagicMock()
            req_custom.path = f"/{os.path.basename(custom_avatar)}"
            resp_custom = asyncio.run(pw_routes.handle_icon(req_custom))
            self.assertEqual(resp_custom.status, 200)
            self.assertEqual(resp_custom.path, custom_avatar)
            self.assertEqual(resp_custom.headers.get("Content-Type"), "image/jpeg")
        finally:
            if old_avatar_env is not None:
                os.environ["AVATAR_PATH"] = old_avatar_env
            else:
                os.environ.pop("AVATAR_PATH", None)
            if os.path.exists(custom_avatar):
                os.remove(custom_avatar)


if __name__ == "__main__":
    unittest.main()

