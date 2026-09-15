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
    import qrcode
except ImportError:
    sys.modules['qrcode'] = MagicMock()

# Add parent directory to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import bot


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
        preview_html = bot.get_channel_preview_html(channel, posts, "https://channels.example.com")
        self.assertIn("Design", preview_html)
        self.assertIn("UI/UX talks", preview_html)
        self.assertIn("Hello world!", preview_html)
        self.assertIn("https://delta.chat", preview_html)
        self.assertIn("photo.jpg", preview_html)
        self.assertIn(f"/c/{token}/rss.xml", preview_html)

        # Tombstone HTML
        tombstone_html = bot.get_tombstone_html(channel["name"])
        self.assertIn("Channel Removed", tombstone_html)
        self.assertIn("Design", tombstone_html)
        self.assertIn("has been removed from the public catalog", tombstone_html)

        # 404 HTML
        not_found_html = bot.get_404_html()
        self.assertIn("Channel Not Found", not_found_html)

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


if __name__ == "__main__":
    unittest.main()
