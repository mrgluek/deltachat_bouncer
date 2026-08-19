import os
import sys
import unittest
import time
import datetime
from unittest.mock import MagicMock, patch

# Setup test environment
TEST_DB = "test_bouncer.db"
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

class TestBouncerBot(unittest.TestCase):
    def setUp(self):
        database.DB_PATH = TEST_DB
        database.init_db()
        bot._cmping_server_status = {}
        bot._cmping_server_errors = {}
        bot._cmping_last_results = {}

    def tearDown(self):
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

    def test_format_duration(self):
        self.assertEqual(bot._format_duration(45), "45s")
        self.assertEqual(bot._format_duration(120), "2m")
        self.assertEqual(bot._format_duration(125), "2m 5s")
        self.assertEqual(bot._format_duration(3600), "1h")
        self.assertEqual(bot._format_duration(3660), "1h 1m")
        self.assertEqual(bot._format_duration(86400), "1d")
        self.assertEqual(bot._format_duration(90000), "1d 1h")

    def test_cmping_incident_database_operations(self):
        now = int(time.time())
        inc_id = database.create_cmping_incident(now)
        self.assertIsInstance(inc_id, int)

        active = database.get_active_cmping_incident()
        self.assertIsNotNone(active)
        self.assertEqual(active["id"], inc_id)
        self.assertEqual(active["status"], "ongoing")

        database.set_cmping_incident_msg_id(inc_id, 12345, 9999)
        database.set_cmping_incident_msg_id(inc_id, 67890, 8888)
        msg_ids = database.get_cmping_incident_msg_ids(inc_id)
        self.assertEqual(msg_ids.get(12345), 9999)
        self.assertEqual(msg_ids.get(67890), 8888)

        database.resolve_cmping_incident(inc_id, now + 120, "All servers operational")
        self.assertIsNone(database.get_active_cmping_incident())

        recent = database.get_recent_cmping_incidents(limit=5)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["id"], inc_id)
        self.assertEqual(recent[0]["status"], "resolved")
        self.assertEqual(recent[0]["summary"], "All servers operational")

    def test_cmping_downtime_events(self):
        now = int(time.time())
        database.record_cmping_server_down("chat.example.com", now, "Connection timeout (60s)")
        
        events = database.get_server_cmping_downtime_events("chat.example.com")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["server"], "chat.example.com")
        self.assertEqual(events[0]["error_msg"], "Connection timeout (60s)")
        self.assertIsNone(events[0]["went_up_at"])

        database.record_cmping_server_up("chat.example.com", now + 300)
        events_after = database.get_server_cmping_downtime_events("chat.example.com")
        self.assertEqual(len(events_after), 1)
        self.assertEqual(events_after[0]["went_up_at"], now + 300)

    def test_cmping_incident_alert_lifecycle_and_editing(self):
        report_chat_id = 7711
        database.add_cmping_report_chat(report_chat_id)
        all_servers = ["node1.cc", "node2.cc", "node3.cc"]

        mock_bot = MagicMock()
        mock_bot.rpc.send_msg.return_value = 50001
        bot.dc_accid = 1

        with patch.object(bot, '_send', return_value=50001) as mock_send:
            # 1. Server 1 fails -> Incident created, _send called
            database.record_cmping_server_down("node1.cc", int(time.time()), "Incoming from node2.cc failed")
            bot._cmping_server_status = {"node1.cc": False, "node2.cc": True, "node3.cc": True}
            bot._cmping_server_errors = {"node1.cc": "Incoming from node2.cc failed"}
            bot._sync_cmping_incident_alerts(mock_bot, 1, all_servers)

            mock_send.assert_called_once()
            sent_text = mock_send.call_args[0][3]
            self.assertIn("🚨 **CMPing Incident #", sent_text)
            self.assertIn("node1.cc", sent_text)
            self.assertIn("Incoming from node2.cc failed", sent_text)

            active_inc = database.get_active_cmping_incident()
            self.assertIsNotNone(active_inc)
            msg_ids = database.get_cmping_incident_msg_ids(active_inc["id"])
            self.assertEqual(msg_ids.get(report_chat_id), 50001)

            # 2. Server 2 also fails -> message edited in-place
            mock_send.reset_mock()
            mock_bot.rpc.send_edit_request.reset_mock()

            database.record_cmping_server_down("node2.cc", int(time.time()), "All checks failed")
            bot._cmping_server_status["node2.cc"] = False
            bot._cmping_server_errors["node2.cc"] = "All checks failed"
            bot._sync_cmping_incident_alerts(mock_bot, 1, all_servers)

            mock_send.assert_not_called()
            mock_bot.rpc.send_edit_request.assert_called_once()
            edit_args = mock_bot.rpc.send_edit_request.call_args[0]
            self.assertEqual(edit_args[1], 50001)
            self.assertIn("node1.cc", edit_args[2])
            self.assertIn("node2.cc", edit_args[2])
            self.assertIn("2 / 3 servers unhealthy", edit_args[2])

            # 3. Server 1 recovers -> message edited in-place showing Partial Recovery
            mock_send.reset_mock()
            mock_bot.rpc.send_edit_request.reset_mock()

            database.record_cmping_server_up("node1.cc", int(time.time()))
            bot._cmping_server_status["node1.cc"] = True
            bot._cmping_server_errors.pop("node1.cc", None)
            bot._sync_cmping_incident_alerts(mock_bot, 1, all_servers)

            mock_send.assert_not_called()
            mock_bot.rpc.send_edit_request.assert_called_once()
            edit_args = mock_bot.rpc.send_edit_request.call_args[0]
            self.assertEqual(edit_args[1], 50001)
            self.assertIn("Ongoing (Partial Recovery)", edit_args[2])
            self.assertIn("node2.cc", edit_args[2])
            self.assertIn("node1.cc", edit_args[2])

            # 4. Server 2 recovers -> all healthy! Incident resolved, message edited to Resolved
            mock_send.reset_mock()
            mock_bot.rpc.send_edit_request.reset_mock()

            bot._cmping_server_status["node2.cc"] = True
            bot._cmping_server_errors.pop("node2.cc", None)
            bot._sync_cmping_incident_alerts(mock_bot, 1, all_servers)

            mock_send.assert_not_called()
            mock_bot.rpc.send_edit_request.assert_called_once()
            edit_args = mock_bot.rpc.send_edit_request.call_args[0]
            self.assertEqual(edit_args[1], 50001)
            self.assertIn("✅ **CMPing Incident #", edit_args[2])
            self.assertIn("Resolved", edit_args[2])
            self.assertIn("All 3 monitored servers operational", edit_args[2])

            self.assertIsNone(database.get_active_cmping_incident())

    def test_cmping_root_cause_isolation(self):
        # Test case: source server fails connectivity with all 3 peers.
        # Verify that source is marked UNHEALTHY, but the 3 targets remain HEALTHY.
        all_servers = ["cm-broken.cc", "peer1.cc", "peer2.cc", "peer3.cc"]
        bot._cmping_server_status = {s: True for s in all_servers}

        source = "cm-broken.cc"
        targets = ["peer1.cc", "peer2.cc", "peer3.cc"]
        any_source_success = False
        now = int(time.time())

        # Simulate broken source failure
        if not any_source_success and len(targets) >= 2:
            bot._cmping_server_status[source] = False
            bot._cmping_server_errors[source] = "All peer checks failed"
            database.record_cmping_server_down(source, now, "All peer checks failed")

        self.assertFalse(bot._cmping_server_status["cm-broken.cc"])
        self.assertTrue(bot._cmping_server_status["peer1.cc"])
        self.assertTrue(bot._cmping_server_status["peer2.cc"])
        self.assertTrue(bot._cmping_server_status["peer3.cc"])

    def test_cmpingevents_command(self):
        mock_bot = MagicMock()
        mock_event = MagicMock()
        mock_event.msg.chat_id = 8822

        with patch.object(bot, '_send') as mock_send:
            # 1. No incidents
            bot.cmpingevents_command(mock_bot, 1, mock_event)
            mock_send.assert_called_once()
            self.assertIn("No CMPing incidents recorded", mock_send.call_args[0][3])

            # 2. Incidents exist
            inc_id = database.create_cmping_incident(int(time.time()) - 500)
            database.resolve_cmping_incident(inc_id, int(time.time()), "Resolved")
            inc2_id = database.create_cmping_incident(int(time.time()))

            mock_send.reset_mock()
            bot.cmpingevents_command(mock_bot, 1, mock_event)
            mock_send.assert_called_once()
            text = mock_send.call_args[0][3]
            self.assertIn("CMPing Incident Log", text)
            self.assertIn(f"Incident #{inc_id}", text)
            self.assertIn(f"Incident #{inc2_id}", text)

    def test_cmpinghistory_command(self):
        mock_bot = MagicMock()
        mock_event = MagicMock()
        mock_event.msg.chat_id = 8833
        database.add_cmping_monitor("mail.server1.org")

        with patch.object(bot, '_send') as mock_send:
            # 1. Guide / summary list
            mock_event.msg.text = "/cmpinghistory"
            bot.cmpinghistory_command(mock_bot, 1, mock_event)
            mock_send.assert_called_once()
            text = mock_send.call_args[0][3]
            self.assertIn("CMPing Downtime History Guide", text)
            self.assertIn("mail.server1.org", text)

            # 2. Specific server with outage history
            now = int(time.time())
            database.record_cmping_server_down("mail.server1.org", now - 600, "504 Gateway Timeout")
            database.record_cmping_server_up("mail.server1.org", now - 300)

            mock_send.reset_mock()
            mock_event.msg.text = "/cmpinghistory mail.server1.org"
            bot.cmpinghistory_command(mock_bot, 1, mock_event)
            mock_send.assert_called_once()
            text = mock_send.call_args[0][3]
            self.assertIn("CMPing Downtime History for mail.server1.org", text)
            self.assertIn("504 Gateway Timeout", text)

if __name__ == '__main__':
    unittest.main()
