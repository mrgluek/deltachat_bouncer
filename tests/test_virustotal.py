import os
import sys
import unittest
import time
import tempfile
import hashlib
from unittest.mock import MagicMock, patch

# Setup test environment
TEST_DB = "test_vt_bouncer.db"
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


class TestVirusTotalInspection(unittest.TestCase):
    def setUp(self):
        database.close_db()
        if os.path.exists(TEST_DB):
            try:
                os.remove(TEST_DB)
            except OSError:
                pass
        database.init_db()

        self.mock_bot = MagicMock()
        self.mock_bot.rpc = MagicMock()
        self.mock_bot.logger = MagicMock()
        self.accid = 1

        # Reset rate limiting and lock
        bot._vt_last_request_time = 0.0
        if bot._vt_global_lock.locked():
            try:
                bot._vt_global_lock.release()
            except RuntimeError:
                pass

    def tearDown(self):
        database.close_db()
        for suffix in ["", "-wal", "-shm"]:
            path = TEST_DB + suffix
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        if bot._vt_global_lock.locked():
            try:
                bot._vt_global_lock.release()
            except RuntimeError:
                pass

    def test_virus_command_missing_api_key(self):
        """Verify warning when VIRUSTOTAL_API_KEY is not set."""
        with patch.dict(os.environ, {"VIRUSTOTAL_API_KEY": ""}):
            mock_event = MagicMock()
            mock_event.msg.chat_id = 100
            mock_event.msg.id = 200
            mock_event.payload = "https://example.com"

            with patch.object(bot, "_send") as mock_send:
                bot.virus_command(self.mock_bot, self.accid, mock_event)
                mock_send.assert_called_once()
                args = mock_send.call_args[0]
                self.assertIn("VirusTotal API key is not configured", args[3])

    def test_virus_command_usage_no_target(self):
        """Verify usage instruction when no URL or file is provided."""
        with patch.dict(os.environ, {"VIRUSTOTAL_API_KEY": "test_api_key"}):
            mock_event = MagicMock()
            mock_event.msg.chat_id = 100
            mock_event.msg.id = 200
            mock_event.msg.quote = None
            mock_event.msg.file = None
            mock_event.payload = ""

            with patch.object(bot, "_send") as mock_send:
                bot.virus_command(self.mock_bot, self.accid, mock_event)
                mock_send.assert_called_once()
                args = mock_send.call_args[0]
                self.assertIn("Usage:", args[3])
                self.assertIn("/virus <url>", args[3])

    @patch.object(bot, "VIRUSTOTAL_RATE_LIMIT_SECONDS", 0.0)
    @patch.object(bot, "_vt_api_request")
    def test_virus_command_clean_url(self, mock_api_req):
        """Verify clean URL scan reports clean status and reaction ☑️."""
        mock_api_req.return_value = (
            {
                "data": {
                    "attributes": {
                        "last_analysis_stats": {
                            "harmless": 75,
                            "malicious": 0,
                            "suspicious": 0,
                            "undetected": 10,
                        },
                        "last_analysis_results": {},
                        "last_analysis_date": 1700000000,
                    }
                }
            },
            200,
            None,
        )

        with patch.dict(os.environ, {"VIRUSTOTAL_API_KEY": "test_api_key"}):
            mock_event = MagicMock()
            mock_event.msg.chat_id = 100
            mock_event.msg.id = 200
            mock_event.payload = "https://clean-domain.example.com"

            with patch.object(bot, "_send") as mock_send, patch.object(bot, "_react") as mock_react:
                bot.virus_command(self.mock_bot, self.accid, mock_event)
                time.sleep(0.1)

                mock_react.assert_any_call(self.mock_bot, self.accid, 200, "⏳")
                mock_react.assert_any_call(self.mock_bot, self.accid, 200, "☑️")

                send_calls = [c[0][3] for c in mock_send.call_args_list]
                self.assertTrue(any("VirusTotal URL Report — Clean" in text for text in send_calls))
                self.assertTrue(any("0/85 security vendors" in text for text in send_calls))

    @patch.object(bot, "VIRUSTOTAL_RATE_LIMIT_SECONDS", 0.0)
    @patch.object(bot, "_vt_api_request")
    def test_virus_command_malicious_url(self, mock_api_req):
        """Verify malicious URL scan reports malicious status, vendor detections, and reaction 🚨."""
        mock_api_req.return_value = (
            {
                "data": {
                    "attributes": {
                        "last_analysis_stats": {
                            "harmless": 40,
                            "malicious": 3,
                            "suspicious": 1,
                            "undetected": 20,
                        },
                        "last_analysis_results": {
                            "VendorA": {"category": "malicious", "result": "phishing"},
                            "VendorB": {"category": "malicious", "result": "malware"},
                            "VendorC": {"category": "suspicious", "result": "suspicious"},
                        },
                        "last_analysis_date": 1700000000,
                    }
                }
            },
            200,
            None,
        )

        with patch.dict(os.environ, {"VIRUSTOTAL_API_KEY": "test_api_key"}):
            mock_event = MagicMock()
            mock_event.msg.chat_id = 100
            mock_event.msg.id = 201
            mock_event.payload = "https://malicious-test.example.com"

            with patch.object(bot, "_send") as mock_send, patch.object(bot, "_react") as mock_react:
                bot.virus_command(self.mock_bot, self.accid, mock_event)
                time.sleep(0.1)

                mock_react.assert_any_call(self.mock_bot, self.accid, 201, "🚨")
                send_calls = [c[0][3] for c in mock_send.call_args_list]
                report = next(t for t in send_calls if "VirusTotal URL Report" in t)
                self.assertIn("VirusTotal URL Report — Malicious", report)
                self.assertIn("4/64 security vendors", report)
                self.assertIn("VendorA", report)
                self.assertIn("phishing", report)

    @patch.object(bot, "VIRUSTOTAL_RATE_LIMIT_SECONDS", 0.0)
    @patch.object(bot, "_vt_api_request")
    def test_virus_command_url_not_found_then_submit_and_poll(self, mock_api_req):
        """Verify 404 URL is submitted via POST and polled via GET /analyses."""
        mock_api_req.side_effect = [
            (None, 404, "Not found"),  # GET /urls/{id}
            ({"data": {"id": "analysis-123"}}, 200, None),  # POST /urls
            (
                {
                    "data": {
                        "attributes": {
                            "status": "completed",
                            "stats": {
                                "harmless": 60,
                                "malicious": 0,
                                "suspicious": 0,
                                "undetected": 10,
                            },
                            "results": {},
                            "date": 1700000000,
                        }
                    }
                },
                200,
                None,
            ),  # GET /analyses/analysis-123
        ]

        with patch.dict(os.environ, {"VIRUSTOTAL_API_KEY": "test_api_key"}):
            mock_event = MagicMock()
            mock_event.msg.chat_id = 100
            mock_event.msg.id = 202
            mock_event.payload = "https://new-domain.example.com"

            with patch.object(bot, "_send") as mock_send, patch.object(bot, "_react") as mock_react:
                mock_send.return_value = 555
                bot.virus_command(self.mock_bot, self.accid, mock_event)
                time.sleep(0.1)

                mock_react.assert_any_call(self.mock_bot, self.accid, 202, "⏳")
                mock_react.assert_any_call(self.mock_bot, self.accid, 202, "☑️")
                send_calls = [c[0][3] for c in mock_send.call_args_list]
                self.assertTrue(any("VirusTotal URL Scan Submitted" in text for text in send_calls))
                self.mock_bot.rpc.send_edit_request.assert_called_once()
                edit_args = self.mock_bot.rpc.send_edit_request.call_args[0]
                self.assertEqual(edit_args[0], self.accid)
                self.assertEqual(edit_args[1], 555)
                self.assertIn("VirusTotal URL Report — Clean", edit_args[2])

    @patch.object(bot, "VIRUSTOTAL_RATE_LIMIT_SECONDS", 0.0)
    @patch.object(bot, "_vt_api_request")
    def test_virus_command_reply_with_url(self, mock_api_req):
        """Verify replying to a message extracts URL and scans it."""
        mock_api_req.return_value = (
            {
                "data": {
                    "attributes": {
                        "last_analysis_stats": {
                            "harmless": 80,
                            "malicious": 0,
                            "suspicious": 0,
                            "undetected": 5,
                        },
                        "last_analysis_results": {},
                        "last_analysis_date": 1700000000,
                    }
                }
            },
            200,
            None,
        )

        with patch.dict(os.environ, {"VIRUSTOTAL_API_KEY": "test_api_key"}):
            mock_event = MagicMock()
            mock_event.msg.chat_id = 100
            mock_event.msg.id = 203
            mock_event.msg.quote = {"message_id": 999, "text": "Take a look at https://reply-target.org"}
            mock_event.msg.file = None
            mock_event.payload = ""

            quoted_msg = MagicMock()
            quoted_msg.file = None
            quoted_msg.filename = None
            quoted_msg.text = "Take a look at https://reply-target.org"
            self.mock_bot.rpc.get_message.return_value = quoted_msg

            with patch.object(bot, "_send") as mock_send, patch.object(bot, "_react") as mock_react:
                bot.virus_command(self.mock_bot, self.accid, mock_event)
                time.sleep(0.1)

                mock_react.assert_any_call(self.mock_bot, self.accid, 203, "☑️")
                send_calls = [c[0][3] for c in mock_send.call_args_list]
                report = next(t for t in send_calls if "VirusTotal URL Report" in t)
                self.assertIn("https://reply-target.org", report)

    @patch.object(bot, "VIRUSTOTAL_RATE_LIMIT_SECONDS", 0.0)
    @patch.object(bot, "_vt_api_request")
    def test_virus_command_reply_with_file(self, mock_api_req):
        """Verify replying to a message with an attached file hashes and scans the file."""
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(b"Hello VirusTotal File Test")
            temp_path = tf.name

        try:
            expected_sha256 = hashlib.sha256(b"Hello VirusTotal File Test").hexdigest()

            mock_api_req.return_value = (
                {
                    "data": {
                        "attributes": {
                            "last_analysis_stats": {
                                "harmless": 65,
                                "malicious": 0,
                                "suspicious": 0,
                                "undetected": 5,
                            },
                            "last_analysis_results": {},
                            "type_description": "Text Document",
                            "last_analysis_date": 1700000000,
                        }
                    }
                },
                200,
                None,
            )

            with patch.dict(os.environ, {"VIRUSTOTAL_API_KEY": "test_api_key"}):
                mock_event = MagicMock()
                mock_event.msg.chat_id = 100
                mock_event.msg.id = 204
                mock_event.msg.quote = {"message_id": 998}
                mock_event.msg.file = None
                mock_event.payload = ""

                quoted_msg = MagicMock()
                quoted_msg.file = temp_path
                quoted_msg.filename = "test_doc.txt"
                quoted_msg.text = ""
                self.mock_bot.rpc.get_message.return_value = quoted_msg

                with patch.object(bot, "_send") as mock_send, patch.object(bot, "_react") as mock_react:
                    bot.virus_command(self.mock_bot, self.accid, mock_event)
                    time.sleep(0.1)

                    mock_react.assert_any_call(self.mock_bot, self.accid, 204, "☑️")
                    send_calls = [c[0][3] for c in mock_send.call_args_list]
                    report = next(t for t in send_calls if "VirusTotal File Report" in t)
                    self.assertIn("VirusTotal File Report — Clean", report)
                    self.assertIn("test_doc.txt", report)
                    self.assertIn(expected_sha256, report)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    @patch.object(bot, "VIRUSTOTAL_RATE_LIMIT_SECONDS", 0.0)
    @patch.object(bot, "_vt_upload_file")
    @patch.object(bot, "_vt_api_request")
    def test_virus_command_file_not_found_then_upload(self, mock_api_req, mock_upload):
        """Verify file not in VT database is uploaded and analysis polled."""
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(b"Unique file content for upload test")
            temp_path = tf.name

        try:
            mock_api_req.side_effect = [
                (None, 404, "Not found"),  # GET /files/{sha256}
                (
                    {
                        "data": {
                            "attributes": {
                                "status": "completed",
                                "stats": {
                                    "harmless": 50,
                                    "malicious": 0,
                                    "suspicious": 0,
                                    "undetected": 12,
                                },
                                "results": {},
                                "date": 1700000000,
                            }
                        }
                    },
                    200,
                    None,
                ),  # GET /analyses/{id}
            ]
            mock_upload.return_value = ({"data": {"id": "file-analysis-999"}}, 200, None)

            with patch.dict(os.environ, {"VIRUSTOTAL_API_KEY": "test_api_key"}):
                mock_event = MagicMock()
                mock_event.msg.chat_id = 100
                mock_event.msg.id = 205
                mock_event.msg.quote = None
                mock_event.msg.file = temp_path
                mock_event.msg.filename = "unique.bin"
                mock_event.payload = ""

                with patch.object(bot, "_send") as mock_send, patch.object(bot, "_react") as mock_react:
                    mock_send.return_value = 777
                    bot.virus_command(self.mock_bot, self.accid, mock_event)
                    time.sleep(0.1)

                    mock_upload.assert_called_once()
                    mock_react.assert_any_call(self.mock_bot, self.accid, 205, "⏳")
                    mock_react.assert_any_call(self.mock_bot, self.accid, 205, "☑️")
                    send_calls = [c[0][3] for c in mock_send.call_args_list]
                    self.assertTrue(any("VirusTotal File Scan Submitted" in text for text in send_calls))
                    self.mock_bot.rpc.send_edit_request.assert_called_once()
                    edit_args = self.mock_bot.rpc.send_edit_request.call_args[0]
                    self.assertEqual(edit_args[0], self.accid)
                    self.assertEqual(edit_args[1], 777)
                    self.assertIn("VirusTotal File Report — Clean", edit_args[2])
                    self.assertIn("unique.bin", edit_args[2])
                    self.assertIn(hashlib.sha256(b"Unique file content for upload test").hexdigest(), edit_args[2])
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    @patch.object(bot, "VIRUSTOTAL_RATE_LIMIT_SECONDS", 0.0)
    @patch.object(bot, "_vt_api_request")
    def test_virus_command_file_too_large(self, mock_api_req):
        """Verify files larger than 32MB not in VT report size limit reached."""
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(b"Large file stub")
            temp_path = tf.name

        try:
            mock_api_req.return_value = (None, 404, "Not found")

            with patch.dict(os.environ, {"VIRUSTOTAL_API_KEY": "test_api_key"}), \
                 patch("os.path.getsize", return_value=35 * 1024 * 1024):
                mock_event = MagicMock()
                mock_event.msg.chat_id = 100
                mock_event.msg.id = 206
                mock_event.msg.quote = None
                mock_event.msg.file = temp_path
                mock_event.msg.filename = "huge_installer.iso"
                mock_event.payload = ""

                with patch.object(bot, "_send") as mock_send, patch.object(bot, "_react") as mock_react:
                    bot.virus_command(self.mock_bot, self.accid, mock_event)
                    time.sleep(0.1)

                    mock_react.assert_any_call(self.mock_bot, self.accid, 206, "⚠️")
                    send_calls = [c[0][3] for c in mock_send.call_args_list]
                    report = next(t for t in send_calls if "File Not in VirusTotal Database" in t)
                    self.assertIn("exceeds the VirusTotal free tier upload limit (32 MB)", report)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    @patch.object(bot, "VIRUSTOTAL_RATE_LIMIT_SECONDS", 0.0)
    @patch.object(bot, "_vt_api_request")
    def test_virus_command_queue_notification(self, mock_api_req):
        """Verify queue notification is sent when worker lock is already held."""
        mock_api_req.return_value = (
            {
                "data": {
                    "attributes": {
                        "last_analysis_stats": {"harmless": 50, "malicious": 0, "suspicious": 0, "undetected": 0},
                        "last_analysis_results": {},
                    }
                }
            },
            200,
            None,
        )

        with patch.dict(os.environ, {"VIRUSTOTAL_API_KEY": "test_api_key"}):
            # Acquire lock manually to simulate active test
            bot._vt_global_lock.acquire()

            mock_event = MagicMock()
            mock_event.msg.chat_id = 100
            mock_event.msg.id = 207
            mock_event.payload = "https://queued-check.com"

            with patch.object(bot, "_send") as mock_send:
                bot.virus_command(self.mock_bot, self.accid, mock_event)
                time.sleep(0.05)

                send_calls = [c[0][3] for c in mock_send.call_args_list]
                self.assertTrue(any("Another VirusTotal check is in progress, your request is queued" in t for t in send_calls))

            # Release lock so worker thread can proceed and finish
            bot._vt_global_lock.release()
            time.sleep(0.1)

    @patch.object(bot, "VIRUSTOTAL_RATE_LIMIT_SECONDS", 0.0)
    @patch.object(bot, "_vt_api_request")
    def test_virus_api_error_handling(self, mock_api_req):
        """Verify HTTP error reporting (e.g. 401, 429)."""
        mock_api_req.return_value = (None, 401, "Invalid VirusTotal API key. Please check VIRUSTOTAL_API_KEY in .env.")
        with patch.dict(os.environ, {"VIRUSTOTAL_API_KEY": "bad_key"}):
            mock_event = MagicMock()
            mock_event.msg.chat_id = 100
            mock_event.msg.id = 208
            mock_event.payload = "https://test-error.com"

            with patch.object(bot, "_send") as mock_send, patch.object(bot, "_react") as mock_react:
                bot.virus_command(self.mock_bot, self.accid, mock_event)
                time.sleep(0.1)

                mock_react.assert_any_call(self.mock_bot, self.accid, 208, "❌")
                send_calls = [c[0][3] for c in mock_send.call_args_list]
                self.assertTrue(any("Invalid VirusTotal API key" in t for t in send_calls))

    @patch.object(bot, "VIRUSTOTAL_RATE_LIMIT_SECONDS", 0.0)
    @patch.object(bot, "_vt_api_request")
    def test_virus_command_url_multi_poll_then_complete(self, mock_api_req):
        """Verify URL scan completes after multiple polling attempts."""
        mock_api_req.side_effect = [
            (None, 404, "Not found"),  # GET /urls/{id}
            ({"data": {"id": "analysis-multi"}}, 200, None),  # POST /urls
            ({"data": {"attributes": {"status": "queued"}}}, 200, None),  # poll 1
            ({"data": {"attributes": {"status": "in_progress"}}}, 200, None),  # poll 2
            (
                {
                    "data": {
                        "attributes": {
                            "status": "completed",
                            "stats": {"harmless": 70, "malicious": 1, "suspicious": 0, "undetected": 5},
                            "results": {"MalVendor": {"category": "malicious", "result": "malware"}},
                            "date": 1700000000,
                        }
                    }
                },
                200,
                None,
            ),  # poll 3
        ]

        with patch.dict(os.environ, {"VIRUSTOTAL_API_KEY": "test_api_key"}):
            mock_event = MagicMock()
            mock_event.msg.chat_id = 100
            mock_event.msg.id = 209
            mock_event.payload = "https://multipoll-test.org"

            with patch.object(bot, "_send") as mock_send, patch.object(bot, "_react") as mock_react:
                mock_send.return_value = 888
                bot.virus_command(self.mock_bot, self.accid, mock_event)
                time.sleep(0.1)

                mock_react.assert_any_call(self.mock_bot, self.accid, 209, "⏳")
                mock_react.assert_any_call(self.mock_bot, self.accid, 209, "🚨")
                self.mock_bot.rpc.send_edit_request.assert_called_once()
                edit_args = self.mock_bot.rpc.send_edit_request.call_args[0]
                self.assertEqual(edit_args[1], 888)
                self.assertIn("VirusTotal URL Report — Malicious", edit_args[2])
                self.assertIn("MalVendor", edit_args[2])

    @patch.object(bot, "VIRUSTOTAL_RATE_LIMIT_SECONDS", 0.0)
    @patch.object(bot, "_vt_api_request")
    def test_virus_command_polling_timeout(self, mock_api_req):
        """Verify URL scan reaches timeout after max_attempts and updates message."""
        mock_api_req.side_effect = [
            (None, 404, "Not found"),  # GET /urls/{id}
            ({"data": {"id": "analysis-timeout"}}, 200, None),  # POST /urls
        ] + [({"data": {"attributes": {"status": "queued"}}}, 200, None)] * 12

        with patch.dict(os.environ, {"VIRUSTOTAL_API_KEY": "test_api_key"}):
            mock_event = MagicMock()
            mock_event.msg.chat_id = 100
            mock_event.msg.id = 210
            mock_event.payload = "https://timeout-test.org"

            with patch.object(bot, "_send") as mock_send, patch.object(bot, "_react") as mock_react:
                mock_send.return_value = 999
                bot.virus_command(self.mock_bot, self.accid, mock_event)
                time.sleep(0.1)

                mock_react.assert_any_call(self.mock_bot, self.accid, 210, "⏳")
                mock_react.assert_any_call(self.mock_bot, self.accid, 210, "⚪️")
                self.mock_bot.rpc.send_edit_request.assert_called_once()
                edit_args = self.mock_bot.rpc.send_edit_request.call_args[0]
                self.assertEqual(edit_args[1], 999)
                self.assertIn("taking longer than usual", edit_args[2])


if __name__ == "__main__":
    unittest.main()
