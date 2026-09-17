import os
import sys
import unittest
import tempfile
import shutil
from unittest.mock import MagicMock, patch

# Setup test environment
TEST_DB = "test_sticker.db"
os.environ["DB_PATH"] = TEST_DB

# Mock deltachat2 and deltabot_cli if not installed
try:
    import deltachat2
except ImportError:
    mock_deltachat2 = MagicMock()
    class MsgData:
        def __init__(self, text="", file="", viewtype=None, quoted_message_id=None, override_sender_name=None):
            self.text = text
            self.file = file
            self.viewtype = viewtype
            self.quoted_message_id = quoted_message_id
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

# Ensure parent directory is in sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import bot


def _make_sync_thread(target, args=(), daemon=True):
    t = MagicMock()
    t.start.side_effect = lambda: target(*args)
    return t


class TestStickerCommands(unittest.TestCase):
    def setUp(self):
        database.close_db()
        for suffix in ["", "-wal", "-shm"]:
            p = TEST_DB + suffix
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        database.init_db()

        self.mock_bot = MagicMock()
        self.mock_bot.rpc = MagicMock()
        self.mock_bot.logger = MagicMock()
        self.accid = 1
        bot._chat_sticker_anti_spam.clear()
        bot._chat_stickernobg_anti_spam.clear()

        self.temp_test_dir = tempfile.mkdtemp(prefix="dc_test_sticker_")

    def tearDown(self):
        shutil.rmtree(self.temp_test_dir, ignore_errors=True)
        database.close_db()
        for suffix in ["", "-wal", "-shm"]:
            p = TEST_DB + suffix
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def test_sticker_usage_no_target(self):
        """When /sticker is called without an image attachment and without a quote reply, show usage."""
        event = MagicMock()
        event.msg.id = 100
        event.msg.chat_id = 200
        event.msg.from_id = 10
        event.msg.text = "/sticker"
        event.msg.file = None
        event.msg.filename = None
        event.msg.file_bytes = 0
        event.msg.view_type = "Text"
        event.msg.download_state = None
        event.msg.quote = None
        event.payload = ""

        with patch("bot._send") as mock_send, patch("bot._is_dc_admin", return_value=True):
            bot.sticker_command(self.mock_bot, self.accid, event)
            mock_send.assert_called_once()
            args, kwargs = mock_send.call_args
            self.assertIn("Sticker Generator Usage", args[3])
            self.assertEqual(kwargs.get("reply_to_id"), 100)

    def test_sticker_direct_attachment(self):
        """When an image is sent directly with caption /sticker, convert it and send sticker."""
        dummy_img = os.path.join(self.temp_test_dir, "photo.jpg")
        with open(dummy_img, "wb") as f:
            f.write(b"dummy image bytes")

        event = MagicMock()
        event.msg.id = 101
        event.msg.chat_id = 201
        event.msg.from_id = 10
        event.msg.text = "/sticker"
        event.msg.file = dummy_img
        event.msg.filename = "photo.jpg"
        event.msg.file_bytes = 100
        event.msg.view_type = "Image"
        event.msg.download_state = None
        event.msg.quote = None
        event.payload = ""

        with patch("bot._react") as mock_react, \
             patch("bot.convert_to_sticker_webp", return_value=(True, "")) as mock_convert, \
             patch("bot._send_sticker") as mock_send_sticker, \
             patch("bot._is_dc_admin", return_value=True), \
             patch("threading.Thread", side_effect=_make_sync_thread):

            bot.sticker_command(self.mock_bot, self.accid, event)

            # Check ⏳ reaction was added
            mock_react.assert_any_call(self.mock_bot, self.accid, 101, "⏳")
            # Check conversion was called with remove_bg=False
            mock_convert.assert_called_once()
            self.assertEqual(mock_convert.call_args[0][0], dummy_img)
            self.assertFalse(mock_convert.call_args[1].get("remove_bg", False))
            # Check sticker was sent
            mock_send_sticker.assert_called_once()
            # Check ☑️ reaction was added
            mock_react.assert_any_call(self.mock_bot, self.accid, 101, "☑️")

    def test_sticker_reply_to_image(self):
        """Replying to an image message with /sticker extracts the quoted image and sends sticker."""
        dummy_img = os.path.join(self.temp_test_dir, "quoted_photo.png")
        with open(dummy_img, "wb") as f:
            f.write(b"quoted image content")

        quoted_msg = MagicMock()
        quoted_msg.id = 50
        quoted_msg.file = dummy_img
        quoted_msg.filename = "quoted_photo.png"
        quoted_msg.file_bytes = 200
        quoted_msg.view_type = "Image"
        quoted_msg.download_state = None

        self.mock_bot.rpc.get_message.return_value = quoted_msg

        event = MagicMock()
        event.msg.id = 102
        event.msg.chat_id = 202
        event.msg.from_id = 10
        event.msg.text = "/sticker"
        event.msg.file = None
        event.msg.filename = None
        event.msg.file_bytes = 0
        event.msg.view_type = "Text"
        event.msg.download_state = None
        event.msg.quote = {"message_id": 50}
        event.payload = ""

        with patch("bot._react") as mock_react, \
             patch("bot.convert_to_sticker_webp", return_value=(True, "")) as mock_convert, \
             patch("bot._send_sticker") as mock_send_sticker, \
             patch("bot._is_dc_admin", return_value=True), \
             patch("threading.Thread", side_effect=_make_sync_thread):

            bot.sticker_command(self.mock_bot, self.accid, event)

            self.mock_bot.rpc.get_message.assert_called_with(self.accid, 50)
            mock_convert.assert_called_once()
            self.assertEqual(mock_convert.call_args[0][0], dummy_img)
            mock_send_sticker.assert_called_once()
            mock_react.assert_any_call(self.mock_bot, self.accid, 102, "☑️")

    def test_stickernobg_command(self):
        """Calling /stickernobg invokes background removal."""
        dummy_img = os.path.join(self.temp_test_dir, "portrait.jpg")
        with open(dummy_img, "wb") as f:
            f.write(b"portrait image")

        event = MagicMock()
        event.msg.id = 103
        event.msg.chat_id = 203
        event.msg.from_id = 10
        event.msg.text = "/stickernobg"
        event.msg.file = dummy_img
        event.msg.filename = "portrait.jpg"
        event.msg.file_bytes = 150
        event.msg.view_type = "Image"
        event.msg.download_state = None
        event.msg.quote = None
        event.payload = ""

        with patch("bot._react") as mock_react, \
             patch("bot.convert_to_sticker_webp", return_value=(True, "")) as mock_convert, \
             patch("bot._send_sticker") as mock_send_sticker, \
             patch("bot._is_dc_admin", return_value=True), \
             patch("threading.Thread", side_effect=_make_sync_thread):

            bot.stickernobg_command(self.mock_bot, self.accid, event)

            mock_convert.assert_called_once()
            self.assertTrue(mock_convert.call_args[1].get("remove_bg", False))
            mock_send_sticker.assert_called_once()
            mock_react.assert_any_call(self.mock_bot, self.accid, 103, "☑️")

    def test_sticker_alias_nobg(self):
        """Calling /sticker with 'nobg' or '--nobg' activates background removal."""
        dummy_img = os.path.join(self.temp_test_dir, "item.png")
        with open(dummy_img, "wb") as f:
            f.write(b"item image")

        event = MagicMock()
        event.msg.id = 104
        event.msg.chat_id = 204
        event.msg.from_id = 10
        event.msg.text = "/sticker nobg"
        event.msg.file = dummy_img
        event.msg.filename = "item.png"
        event.msg.file_bytes = 120
        event.msg.view_type = "Image"
        event.msg.download_state = None
        event.msg.quote = None
        event.payload = "nobg"

        with patch("bot._react"), \
             patch("bot.convert_to_sticker_webp", return_value=(True, "")) as mock_convert, \
             patch("bot._send_sticker"), \
             patch("bot._is_dc_admin", return_value=True), \
             patch("threading.Thread", side_effect=_make_sync_thread):

            bot.sticker_command(self.mock_bot, self.accid, event)

            mock_convert.assert_called_once()
            self.assertTrue(mock_convert.call_args[1].get("remove_bg", False))

    def test_sticker_conversion_failure(self):
        """When image conversion fails, error reaction ❌ and message are sent."""
        dummy_img = os.path.join(self.temp_test_dir, "corrupt.jpg")
        with open(dummy_img, "wb") as f:
            f.write(b"not a valid image")

        event = MagicMock()
        event.msg.id = 105
        event.msg.chat_id = 205
        event.msg.from_id = 10
        event.msg.text = "/sticker"
        event.msg.file = dummy_img
        event.msg.filename = "corrupt.jpg"
        event.msg.file_bytes = 50
        event.msg.view_type = "Image"
        event.msg.download_state = None
        event.msg.quote = None
        event.payload = ""

        with patch("bot._react") as mock_react, \
             patch("bot._send") as mock_send, \
             patch("bot.convert_to_sticker_webp", return_value=(False, "Invalid or unsupported image file")), \
             patch("bot._is_dc_admin", return_value=True), \
             patch("threading.Thread", side_effect=_make_sync_thread):

            bot.sticker_command(self.mock_bot, self.accid, event)

            mock_react.assert_any_call(self.mock_bot, self.accid, 105, "❌")
            mock_send.assert_called_once()
            self.assertIn("Invalid or unsupported image file", mock_send.call_args[0][3])

    def test_sticker_anti_spam_cooldown(self):
        """Verify 5-second cooldown triggers delayed execution for non-admins."""
        dummy_img = os.path.join(self.temp_test_dir, "spam.jpg")
        with open(dummy_img, "wb") as f:
            f.write(b"spam image")

        event = MagicMock()
        event.msg.id = 106
        event.msg.chat_id = 206
        event.msg.from_id = 100
        event.msg.text = "/sticker"
        event.msg.file = dummy_img
        event.msg.filename = "spam.jpg"
        event.msg.file_bytes = 80
        event.msg.view_type = "Image"
        event.msg.download_state = None
        event.msg.quote = None
        event.payload = ""

        # First call records timestamp
        with patch("bot._is_dc_admin", return_value=False), \
             patch("bot.convert_to_sticker_webp", return_value=(True, "")), \
             patch("bot._send_sticker"), \
             patch("threading.Thread", side_effect=_make_sync_thread):

            bot.sticker_command(self.mock_bot, self.accid, event)

        self.assertIn(206, bot._chat_sticker_anti_spam)

        # Immediate second call should trigger _queue_delayed_command
        with patch("bot._is_dc_admin", return_value=False), \
             patch("bot._queue_delayed_command") as mock_queue:

            bot.sticker_command(self.mock_bot, self.accid, event)
            mock_queue.assert_called_once()
            self.assertEqual(mock_queue.call_args[0][3], "sticker")

    def test_convert_to_sticker_webp_resizing(self):
        """Test proportional resize logic and WebP output with PIL mock."""
        mock_raw_img = MagicMock()
        mock_raw_img.size = (1000, 500)
        mock_raw_img.mode = "RGB"
        mock_raw_img.is_animated = False

        mock_resized_img = MagicMock()
        mock_resized_img.size = (512, 256)
        mock_resized_img.mode = "RGBA"
        mock_raw_img.resize.return_value = mock_resized_img

        mock_pil = MagicMock()
        mock_pil_image = MagicMock()
        mock_pil_imageops = MagicMock()
        mock_pil_imageops.exif_transpose.side_effect = lambda im: im
        mock_pil.Image = mock_pil_image
        mock_pil.ImageOps = mock_pil_imageops

        mock_pil_image.open.return_value.__enter__.return_value = mock_raw_img

        with patch.dict("sys.modules", {"PIL": mock_pil, "PIL.Image": mock_pil_image, "PIL.ImageOps": mock_pil_imageops}):
            dest_file = os.path.join(self.temp_test_dir, "out.webp")
            success, err = bot.convert_to_sticker_webp("dummy.jpg", dest_file, remove_bg=False, max_dim=512)

            self.assertTrue(success, f"Error was: {err}")
            self.assertEqual(err, "")
            # 1000x500 scaled to max_dim 512 -> 512x256
            mock_raw_img.resize.assert_called_once_with((512, 256), mock_pil_image.Resampling.LANCZOS)
            mock_resized_img.save.assert_called_once()

    def test_convert_to_sticker_webp_missing_rembg(self):
        """When remove_bg=True but rembg is not installed, return clear error message."""
        mock_raw_img = MagicMock()
        mock_raw_img.size = (500, 500)
        mock_raw_img.mode = "RGBA"
        mock_raw_img.is_animated = False

        mock_pil = MagicMock()
        mock_pil_image = MagicMock()
        mock_pil_imageops = MagicMock()
        mock_pil_imageops.exif_transpose.side_effect = lambda im: im
        mock_pil.Image = mock_pil_image
        mock_pil.ImageOps = mock_pil_imageops

        mock_pil_image.open.return_value.__enter__.return_value = mock_raw_img

        # Hide rembg from imports
        with patch.dict("sys.modules", {"PIL": mock_pil, "PIL.Image": mock_pil_image, "PIL.ImageOps": mock_pil_imageops, "rembg": None}):
            dest_file = os.path.join(self.temp_test_dir, "out.webp")
            success, err = bot.convert_to_sticker_webp("dummy.jpg", dest_file, remove_bg=True, max_dim=512)

            self.assertFalse(success)
            self.assertIn("rembg", err)
            self.assertIn("/sticker", err)


    def test_stickernobg_anti_spam_cooldown(self):
        """Verify 15-second cooldown triggers delayed execution for /stickernobg."""
        dummy_img = os.path.join(self.temp_test_dir, "spam_nobg.jpg")
        with open(dummy_img, "wb") as f:
            f.write(b"spam nobg image")

        event = MagicMock()
        event.msg.id = 107
        event.msg.chat_id = 207
        event.msg.from_id = 100
        event.msg.text = "/stickernobg"
        event.msg.file = dummy_img
        event.msg.filename = "spam_nobg.jpg"
        event.msg.file_bytes = 90
        event.msg.view_type = "Image"
        event.msg.download_state = None
        event.msg.quote = None
        event.payload = ""

        # First call records timestamp
        with patch("bot._is_dc_admin", return_value=False), \
             patch("bot.convert_to_sticker_webp", return_value=(True, "")), \
             patch("bot._send_sticker"), \
             patch("threading.Thread", side_effect=_make_sync_thread):

            bot.stickernobg_command(self.mock_bot, self.accid, event)

        self.assertIn(207, bot._chat_stickernobg_anti_spam)
        self.assertNotIn(207, bot._chat_sticker_anti_spam)

        # Immediate second call should trigger _queue_delayed_command with stickernobg key
        with patch("bot._is_dc_admin", return_value=False), \
             patch("bot._queue_delayed_command") as mock_queue:

            bot.stickernobg_command(self.mock_bot, self.accid, event)
            mock_queue.assert_called_once()
            self.assertEqual(mock_queue.call_args[0][3], "stickernobg")

    def test_convert_to_sticker_webp_prescale_before_rembg(self):
        """Verify image is pre-scaled to 512px before calling rembg.remove."""
        mock_raw_img = MagicMock()
        mock_raw_img.size = (4000, 2000)
        mock_raw_img.mode = "RGB"
        mock_raw_img.is_animated = False

        mock_prescaled_img = MagicMock()
        mock_prescaled_img.size = (512, 256)
        mock_prescaled_img.mode = "RGBA"
        mock_prescaled_img.convert.return_value = mock_prescaled_img

        mock_nobg_img = MagicMock()
        mock_nobg_img.size = (512, 256)

        mock_raw_img.resize.return_value = mock_prescaled_img

        mock_pil = MagicMock()
        mock_pil_image = MagicMock()
        mock_pil_imageops = MagicMock()
        mock_pil_imageops.exif_transpose.side_effect = lambda im: im
        mock_pil.Image = mock_pil_image
        mock_pil.ImageOps = mock_pil_imageops
        mock_pil_image.open.return_value.__enter__.return_value = mock_raw_img

        mock_rembg = MagicMock()
        mock_rembg.remove.return_value = mock_nobg_img
        mock_session = MagicMock()

        with patch.dict("sys.modules", {"PIL": mock_pil, "PIL.Image": mock_pil_image, "PIL.ImageOps": mock_pil_imageops, "rembg": mock_rembg}), \
             patch("bot._get_rembg_session", return_value=mock_session):
            dest_file = os.path.join(self.temp_test_dir, "nobg_out.webp")
            success, err = bot.convert_to_sticker_webp("big.jpg", dest_file, remove_bg=True, max_dim=512)

            self.assertTrue(success, f"Error was: {err}")
            # Verify pre-scale resize was called on raw_img BEFORE rembg
            mock_raw_img.resize.assert_called_once_with((512, 256), mock_pil_image.Resampling.LANCZOS)
            # Verify rembg.remove was called with the pre-scaled image and session
            mock_rembg.remove.assert_called_once_with(mock_prescaled_img, session=mock_session)
            mock_nobg_img.save.assert_called_once()

    def test_convert_to_sticker_webp_enable_rembg_disabled(self):
        """When ENABLE_REMBG=false, return error message indicating background removal is disabled."""
        mock_raw_img = MagicMock()
        mock_raw_img.size = (500, 500)
        mock_raw_img.mode = "RGB"
        mock_raw_img.is_animated = False

        mock_pil = MagicMock()
        mock_pil_image = MagicMock()
        mock_pil_imageops = MagicMock()
        mock_pil_imageops.exif_transpose.side_effect = lambda im: im
        mock_pil.Image = mock_pil_image
        mock_pil.ImageOps = mock_pil_imageops
        mock_pil_image.open.return_value.__enter__.return_value = mock_raw_img

        with patch.dict("sys.modules", {"PIL": mock_pil, "PIL.Image": mock_pil_image, "PIL.ImageOps": mock_pil_imageops}), \
             patch.dict(os.environ, {"ENABLE_REMBG": "false"}):
            dest_file = os.path.join(self.temp_test_dir, "out_disabled.webp")
            success, err = bot.convert_to_sticker_webp("photo.jpg", dest_file, remove_bg=True)

            self.assertFalse(success)
            self.assertIn("Background removal is disabled", err)

    def test_get_rembg_session_caching(self):
        """Verify _get_rembg_session lazily initializes and caches the session."""
        bot._rembg_session = None
        mock_rembg = MagicMock()
        mock_session_obj = MagicMock()
        mock_rembg.new_session.return_value = mock_session_obj

        with patch.dict("sys.modules", {"rembg": mock_rembg}), \
             patch.dict(os.environ, {"REMBG_MODEL": "u2netp"}):
            sess1 = bot._get_rembg_session()
            self.assertEqual(sess1, mock_session_obj)
            mock_rembg.new_session.assert_called_once_with("u2netp")

            # Second call should return cached session without calling new_session again
            sess2 = bot._get_rembg_session()
            self.assertEqual(sess2, mock_session_obj)
            mock_rembg.new_session.assert_called_once()

        bot._rembg_session = None


if __name__ == "__main__":
    unittest.main()
