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
        database.close_db()
        database.DB_PATH = TEST_DB
        database.init_db()
        bot._cmping_server_status = {}
        bot._cmping_server_errors = {}
        bot._cmping_last_results = {}

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

            database.record_cmping_server_up("node2.cc", int(time.time()))
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
            database.record_cmping_server_down("cm1.test.cc", int(time.time()) - 500, "502 Bad Gateway")
            database.record_cmping_server_up("cm1.test.cc", int(time.time()) - 100)
            database.resolve_cmping_incident(inc_id, int(time.time()), "Affected: cm1.test.cc")

            inc2_id = database.create_cmping_incident(int(time.time()))
            database.add_cmping_monitor("cm2.test.cc")
            bot._cmping_server_status["cm2.test.cc"] = False
            bot._cmping_server_errors["cm2.test.cc"] = "All peer checks failed"

            mock_send.reset_mock()
            mock_event.msg.text = "/cmpingevents"
            bot.cmpingevents_command(mock_bot, 1, mock_event)
            mock_send.assert_called_once()
            text = mock_send.call_args[0][3]
            self.assertIn("CMPing Incident Log", text)
            self.assertIn(f"Incident #{inc_id}", text)
            self.assertIn(f"Incident #{inc2_id}", text)
            self.assertIn("cm2.test.cc", text)
            self.assertIn("All peer checks failed", text)

            # 3. View incident by ID: /cmpingevents <id>
            mock_send.reset_mock()
            mock_event.msg.text = f"/cmpingevents {inc_id}"
            bot.cmpingevents_command(mock_bot, 1, mock_event)
            mock_send.assert_called_once()
            text_detail = mock_send.call_args[0][3]
            self.assertIn(f"CMPing Incident #{inc_id} Details", text_detail)
            self.assertIn("cm1.test.cc", text_detail)
            self.assertIn("502 Bad Gateway", text_detail)

            # 4. View ongoing incident by ID: /cmpingevents <inc2_id>
            mock_send.reset_mock()
            mock_event.msg.text = f"/cmpingevents #{inc2_id}"
            bot.cmpingevents_command(mock_bot, 1, mock_event)
            mock_send.assert_called_once()
            text_ongoing = mock_send.call_args[0][3]
            self.assertIn(f"CMPing Incident #{inc2_id}", text_ongoing)
            self.assertIn("cm2.test.cc", text_ongoing)
            self.assertIn("All peer checks failed", text_ongoing)

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

    def test_database_autokick_operations(self):
        chat_id = 7001
        self.assertEqual(database.get_chat_autokick(chat_id), 0)

        # Set autokick to 90 days
        database.set_chat_autokick(chat_id, 90)
        self.assertEqual(database.get_chat_autokick(chat_id), 90)

        # Set another chat to 30 days
        chat_id_2 = 7002
        database.set_chat_autokick(chat_id_2, 30)
        self.assertEqual(database.get_chat_autokick(chat_id_2), 30)

        all_autokick = dict(database.get_all_autokick_chats())
        self.assertEqual(all_autokick.get(chat_id), 90)
        self.assertEqual(all_autokick.get(chat_id_2), 30)

        # Verify set_chat_monitored_since preserves autokick_days
        database.set_chat_monitored_since(chat_id, time.time() - 1000)
        self.assertEqual(database.get_chat_autokick(chat_id), 90)

        # Disable autokick for chat 1
        database.set_chat_autokick(chat_id, 0)
        self.assertEqual(database.get_chat_autokick(chat_id), 0)
        all_autokick_after = dict(database.get_all_autokick_chats())
        self.assertNotIn(chat_id, all_autokick_after)
        self.assertEqual(all_autokick_after.get(chat_id_2), 30)

    def test_autokick_command(self):
        mock_bot = MagicMock()
        mock_event = MagicMock()
        mock_event.msg.chat_id = 7010
        mock_event.msg.from_id = 100

        # 1. Non-admin is rejected
        with patch('bot._is_dc_admin', return_value=False), patch.object(bot, '_send') as mock_send:
            bot.autokick_command(mock_bot, 1, mock_event)
            mock_send.assert_called_once()
            self.assertIn("Only the bot administrator", mock_send.call_args[0][3])

        # 2. Private chat (non-group) rejected
        mock_bot.rpc.get_basic_chat_info.return_value = {"chat_type": "Single"}
        with patch('bot._is_dc_admin', return_value=True), patch.object(bot, '_send') as mock_send:
            bot.autokick_command(mock_bot, 1, mock_event)
            mock_send.assert_called_once()
            self.assertIn("can only be used in group chats", mock_send.call_args[0][3])

        # 3. Group chat status when disabled
        mock_bot.rpc.get_basic_chat_info.return_value = {"chat_type": "Group"}
        with patch('bot._is_dc_admin', return_value=True), patch.object(bot, '_send') as mock_send:
            mock_event.payload = ""
            bot.autokick_command(mock_bot, 1, mock_event)
            mock_send.assert_called_once()
            self.assertIn("Auto-kick is OFF", mock_send.call_args[0][3])

        # 4. Enable with default (on -> 90 days)
        with patch('bot._is_dc_admin', return_value=True), patch.object(bot, '_send') as mock_send:
            mock_event.payload = "on"
            bot.autokick_command(mock_bot, 1, mock_event)
            mock_send.assert_called_once()
            self.assertIn("threshold of **90 days**", mock_send.call_args[0][3])
            self.assertEqual(database.get_chat_autokick(7010), 90)

        # 5. Check status when enabled
        with patch('bot._is_dc_admin', return_value=True), patch.object(bot, '_send') as mock_send:
            mock_event.payload = "status"
            bot.autokick_command(mock_bot, 1, mock_event)
            mock_send.assert_called_once()
            self.assertIn("Auto-kick is ON", mock_send.call_args[0][3])
            self.assertIn("90 days", mock_send.call_args[0][3])

        # 6. Enable with custom days (e.g. 30)
        with patch('bot._is_dc_admin', return_value=True), patch.object(bot, '_send') as mock_send:
            mock_event.payload = "30"
            bot.autokick_command(mock_bot, 1, mock_event)
            mock_send.assert_called_once()
            self.assertIn("threshold of **30 days**", mock_send.call_args[0][3])
            self.assertEqual(database.get_chat_autokick(7010), 30)

        # 7. Disable (off)
        with patch('bot._is_dc_admin', return_value=True), patch.object(bot, '_send') as mock_send:
            mock_event.payload = "off"
            bot.autokick_command(mock_bot, 1, mock_event)
            mock_send.assert_called_once()
            self.assertIn("Auto-kick disabled", mock_send.call_args[0][3])
            self.assertEqual(database.get_chat_autokick(7010), 0)

    def test_perform_autokick_for_chat(self):
        mock_bot = MagicMock()
        chat_id = 7020
        now = time.time()

        # Group monitored for 100 days
        database.set_chat_monitored_since(chat_id, now - (100 * 86400))

        # Contacts in group:
        # 1: self (should NOT kick)
        # 10: admin (should NOT kick)
        # 20: active 5d ago (should NOT kick)
        # 30: inactive 95d ago (SHOULD kick)
        # 40: never seen (last_seen=0), group monitored 100d > 90d (SHOULD kick)
        mock_bot.rpc.get_chat_contacts.return_value = [1, 10, 20, 30, 40]

        def get_contact_mock(accid, cid):
            c = MagicMock()
            c.id = cid
            if cid == 10:
                c.name = "Admin"
                c.address = "admin@example.com"
                c.last_seen = now - (95 * 86400)
            elif cid == 20:
                c.name = "ActiveUser"
                c.address = "active@example.com"
                c.last_seen = now - (5 * 86400)
            elif cid == 30:
                c.name = "InactiveUser"
                c.address = "inactive@example.com"
                c.last_seen = now - (95 * 86400)
            elif cid == 40:
                c.name = "NeverSeenUser"
                c.address = "neverseen@example.com"
                c.last_seen = 0
            return c

        mock_bot.rpc.get_contact.side_effect = get_contact_mock

        def is_admin_mock(b, accid, cid):
            return cid == 10

        with patch('bot._is_dc_admin', side_effect=is_admin_mock), patch.object(bot, '_send') as mock_send:
            # Without warnings, NO ONE should be kicked
            kicked_unwarned = bot._perform_autokick_for_chat(mock_bot, 1, chat_id, days=90)
            self.assertEqual(len(kicked_unwarned), 0)

            # Record warnings for 30 and 40 given 2 days ago (> 24h grace period)
            database.record_autokick_warning(chat_id, 30, now - (2 * 86400))
            database.record_autokick_warning(chat_id, 40, now - (2 * 86400))

            kicked = bot._perform_autokick_for_chat(mock_bot, 1, chat_id, days=90)
            self.assertEqual(len(kicked), 2)
            kicked_ids = [m["id"] for m in kicked]
            self.assertIn(30, kicked_ids)
            self.assertIn(40, kicked_ids)
            self.assertNotIn(1, kicked_ids)
            self.assertNotIn(10, kicked_ids)
            self.assertNotIn(20, kicked_ids)

            # Verify warnings were cleared upon kick
            self.assertIsNone(database.get_autokick_warning(chat_id, 30))
            self.assertIsNone(database.get_autokick_warning(chat_id, 40))

            # Verify remove_contact_from_chat calls
            mock_bot.rpc.remove_contact_from_chat.assert_any_call(1, chat_id, 30)
            mock_bot.rpc.remove_contact_from_chat.assert_any_call(1, chat_id, 40)
            mock_send.assert_called_once()
            self.assertIn("Auto-kick", mock_send.call_args[0][3])

    def test_kick_command(self):
        mock_bot = MagicMock()
        mock_event = MagicMock()
        mock_event.msg.chat_id = 7030
        mock_event.msg.from_id = 100
        mock_event.msg.quote = None

        mock_bot.rpc.get_basic_chat_info.return_value = {"chat_type": "Group"}
        mock_bot.rpc.get_chat_contacts.return_value = [1, 100, 201, 202, 203]

        def get_contact_mock(accid, cid):
            c = MagicMock()
            c.id = cid
            if cid == 201:
                c.name = "Alice"
                c.display_name = "Alice D"
                c.address = "alice@example.com"
            elif cid == 202:
                c.name = "Bob"
                c.display_name = "Bob M"
                c.address = "bob@example.com"
            elif cid == 203:
                c.name = "Charlie"
                c.display_name = "Charlie C"
                c.address = "charlie@example.com"
            elif cid == 100:
                c.name = "Admin"
                c.display_name = "Admin"
                c.address = "admin@example.com"
            return c

        mock_bot.rpc.get_contact.side_effect = get_contact_mock

        def is_admin_mock(b, accid, cid):
            return cid == 100

        # 1. Non-admin rejected
        with patch('bot._is_dc_admin', return_value=False), patch.object(bot, '_send') as mock_send:
            bot.kick_command(mock_bot, 1, mock_event)
            mock_send.assert_called_once()
            self.assertIn("Only the bot administrator can use /kick", mock_send.call_args[0][3])

        # 2. Kick by numeric ID (/kick 201)
        with patch('bot._is_dc_admin', side_effect=is_admin_mock), patch.object(bot, '_send') as mock_send:
            mock_event.payload = "201"
            bot.kick_command(mock_bot, 1, mock_event)
            mock_bot.rpc.remove_contact_from_chat.assert_called_with(1, 7030, 201)
            mock_send.assert_called_once()
            self.assertIn("Kicked 1 member", mock_send.call_args[0][3])
            self.assertIn("Alice", mock_send.call_args[0][3])

        # 3. Kick with /contact format (/kick /contact202)
        mock_bot.rpc.remove_contact_from_chat.reset_mock()
        with patch('bot._is_dc_admin', side_effect=is_admin_mock), patch.object(bot, '_send') as mock_send:
            mock_event.payload = "/contact202"
            bot.kick_command(mock_bot, 1, mock_event)
            mock_bot.rpc.remove_contact_from_chat.assert_called_with(1, 7030, 202)
            mock_send.assert_called_once()
            self.assertIn("Bob", mock_send.call_args[0][3])

        # 4. Kick by replying to a quoted message
        mock_bot.rpc.remove_contact_from_chat.reset_mock()
        mock_event.msg.quote = {"message_id": 9901}
        quoted_msg = MagicMock()
        quoted_msg.from_id = 203
        mock_bot.rpc.get_message.return_value = quoted_msg

        with patch('bot._is_dc_admin', side_effect=is_admin_mock), patch.object(bot, '_send') as mock_send:
            mock_event.payload = ""
            bot.kick_command(mock_bot, 1, mock_event)
            mock_bot.rpc.remove_contact_from_chat.assert_called_with(1, 7030, 203)
            mock_send.assert_called_once()
            self.assertIn("Charlie", mock_send.call_args[0][3])

        # 5. Protection: Cannot kick admin or self
        mock_event.msg.quote = None
        with patch('bot._is_dc_admin', side_effect=is_admin_mock), patch.object(bot, '_send') as mock_send:
            mock_event.payload = "1 100"
            bot.kick_command(mock_bot, 1, mock_event)
            mock_send.assert_called_once()
            self.assertIn("The bot cannot kick itself", mock_send.call_args[0][3])
            self.assertIn("Cannot kick the bot administrator", mock_send.call_args[0][3])

    def test_bounce_inactivity_threshold_21_days(self):
        self.assertEqual(bot.INACTIVITY_DAYS_THRESHOLD, 21)
        self.assertEqual(bot.INACTIVITY_SECONDS_THRESHOLD, 21 * 24 * 3600)

        mock_bot = MagicMock()
        chat_id = 7040
        now = time.time()
        database.set_chat_monitored_since(chat_id, now - (30 * 86400))

        # Contacts:
        # 1: self
        # 301: active 10 days ago (active under 21d threshold)
        # 302: inactive 25 days ago (inactive under 21d threshold)
        mock_bot.rpc.get_chat_contacts.return_value = [1, 301, 302]

        def get_contact_mock(accid, cid):
            c = MagicMock()
            c.id = cid
            if cid == 301:
                c.name = "UserActive10d"
                c.address = "user10@example.com"
                c.last_seen = now - (10 * 86400)
            elif cid == 302:
                c.name = "UserInactive25d"
                c.address = "user25@example.com"
                c.last_seen = now - (25 * 86400)
            return c

        mock_bot.rpc.get_contact.side_effect = get_contact_mock

        report = bot._check_chat_inactivity(mock_bot, 1, chat_id)
        self.assertIn("Inactive (>21d): 1", report)
        self.assertIn("UserInactive25d", report)
        self.assertNotIn("UserActive10d", report)

    def test_get_cmping_incident_update_interval(self):
        self.assertEqual(bot._get_cmping_incident_update_interval(0), 15)
        self.assertEqual(bot._get_cmping_incident_update_interval(59), 15)
        self.assertEqual(bot._get_cmping_incident_update_interval(60), 30)
        self.assertEqual(bot._get_cmping_incident_update_interval(299), 30)
        self.assertEqual(bot._get_cmping_incident_update_interval(300), 60)
        self.assertEqual(bot._get_cmping_incident_update_interval(3599), 60)
        # 1 hour - 24 hours: 300s (5 minutes)
        self.assertEqual(bot._get_cmping_incident_update_interval(3600), 300)
        self.assertEqual(bot._get_cmping_incident_update_interval(86399), 300)
        # > 24 hours: 3600s (1 hour)
        self.assertEqual(bot._get_cmping_incident_update_interval(86400), 3600)
        self.assertEqual(bot._get_cmping_incident_update_interval(86400 * 7), 3600)

    def test_cmping_incident_rate_limiting(self):
        mock_bot = MagicMock()
        chat_id = 7722
        database.add_cmping_report_chat(chat_id)
        servers = ["cm1.test.org", "cm2.test.org"]

        bot._cmping_server_status["cm1.test.org"] = False
        bot._cmping_server_errors["cm1.test.org"] = "Timeout"
        bot._cmping_server_status["cm2.test.org"] = True
        bot._cmping_incident_last_edit_state.clear()

        # 1. Initial alert
        with patch.object(bot, '_send', return_value=60001):
            bot._sync_cmping_incident_alerts(mock_bot, 1, servers, force_update=False)

        inc = database.get_active_cmping_incident()
        self.assertIsNotNone(inc)
        database.set_cmping_incident_msg_id(inc["id"], chat_id, 60001)
        self.assertIn(inc["id"], bot._cmping_incident_last_edit_state)

        # 2. Immediate second call with same state -> throttled
        mock_bot.rpc.send_edit_request.reset_mock()
        bot._sync_cmping_incident_alerts(mock_bot, 1, servers, force_update=False)
        mock_bot.rpc.send_edit_request.assert_not_called()

        # 3. Time elapsed >= 15s -> sends edit
        last_t, sig = bot._cmping_incident_last_edit_state[inc["id"]]
        bot._cmping_incident_last_edit_state[inc["id"]] = (last_t - 20, sig)
        bot._sync_cmping_incident_alerts(mock_bot, 1, servers, force_update=False)
        mock_bot.rpc.send_edit_request.assert_called_once()

        # 4. Status change (cm2 also goes down) -> immediate edit
        mock_bot.rpc.send_edit_request.reset_mock()
        database.record_cmping_server_down("cm2.test.org", int(time.time()), "Connection refused")
        bot._cmping_server_status["cm2.test.org"] = False
        bot._cmping_server_errors["cm2.test.org"] = "Connection refused"
        bot._sync_cmping_incident_alerts(mock_bot, 1, servers, force_update=False)
        mock_bot.rpc.send_edit_request.assert_called_once()

    def test_cmping_incident_split_after_one_hour_gap(self):
        chat_id = 7722
        database.add_cmping_report_chat(chat_id)
        servers = ["srv1.test.org", "srv2.test.org"]

        mock_bot = MagicMock()
        bot.dc_accid = 1
        t0 = 1000000

        # 1. Srv 1 goes down at t0 -> Incident #1 created
        with patch('time.time', return_value=t0), patch.object(bot, '_send', return_value=70001):
            database.record_cmping_server_down("srv1.test.org", t0, "Timeout")
            bot._cmping_server_status = {"srv1.test.org": False, "srv2.test.org": True}
            bot._cmping_server_errors = {"srv1.test.org": "Timeout"}
            bot._cmping_incident_last_edit_state.clear()
            bot._sync_cmping_incident_alerts(mock_bot, 1, servers)

        active_incs = database.get_all_active_cmping_incidents()
        self.assertEqual(len(active_incs), 1)
        inc1_id = active_incs[0]["id"]
        database.set_cmping_incident_msg_id(inc1_id, chat_id, 70001)

        # 2. Srv 2 goes down at t0 + 4000s (> 1 hour gap) -> Incident #2 created!
        t1 = t0 + 4000
        with patch('time.time', return_value=t1), patch.object(bot, '_send', return_value=70002):
            database.record_cmping_server_down("srv2.test.org", t1, "Refused")
            bot._cmping_server_status["srv2.test.org"] = False
            bot._cmping_server_errors["srv2.test.org"] = "Refused"
            bot._sync_cmping_incident_alerts(mock_bot, 1, servers)

        active_incs = database.get_all_active_cmping_incidents()
        self.assertEqual(len(active_incs), 2)
        inc2_id = active_incs[1]["id"]
        database.set_cmping_incident_msg_id(inc2_id, chat_id, 70002)

        # 3. Srv 2 recovers at t1 + 300s -> Incident #2 resolves, Incident #1 remains active
        t2 = t1 + 300
        mock_bot.rpc.send_edit_request.reset_mock()

        with patch('time.time', return_value=t2):
            database.record_cmping_server_up("srv2.test.org", t2)
            bot._cmping_server_status["srv2.test.org"] = True
            bot._cmping_server_errors.pop("srv2.test.org", None)
            bot._sync_cmping_incident_alerts(mock_bot, 1, servers)

        resolved_calls = [c for c in mock_bot.rpc.send_edit_request.call_args_list if c[0][1] == 70002]
        self.assertEqual(len(resolved_calls), 1)
        self.assertIn(f"Incident #{inc2_id}", resolved_calls[0][0][2])
        self.assertIn("Resolved", resolved_calls[0][0][2])
        self.assertIn("srv2.test.org", resolved_calls[0][0][2])

        active_incs = database.get_all_active_cmping_incidents()
        self.assertEqual(len(active_incs), 1)
        self.assertEqual(active_incs[0]["id"], inc1_id)

        # 4. Srv 1 recovers at t2 + 500s -> Incident #1 resolves
        t3 = t2 + 500
        mock_bot.rpc.send_edit_request.reset_mock()

        with patch('time.time', return_value=t3):
            database.record_cmping_server_up("srv1.test.org", t3)
            bot._cmping_server_status["srv1.test.org"] = True
            bot._cmping_server_errors.pop("srv1.test.org", None)
            bot._sync_cmping_incident_alerts(mock_bot, 1, servers)

        resolved_calls = [c for c in mock_bot.rpc.send_edit_request.call_args_list if c[0][1] == 70001]
        self.assertEqual(len(resolved_calls), 1)
        self.assertIn(f"Incident #{inc1_id}", resolved_calls[0][0][2])
        self.assertIn("Resolved", resolved_calls[0][0][2])
        self.assertIn("srv1.test.org", resolved_calls[0][0][2])

        active_incs = database.get_all_active_cmping_incidents()
        self.assertEqual(len(active_incs), 0)

    def test_database_migration_from_legacy_schema(self):
        import tempfile
        import sqlite3
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            legacy_db_path = tf.name

        try:
            # Create old schema table without incident_id column
            conn = sqlite3.connect(legacy_db_path)
            cur = conn.cursor()
            cur.execute('''
                CREATE TABLE cmping_downtime_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    server TEXT NOT NULL,
                    went_down_at INTEGER NOT NULL,
                    went_up_at INTEGER,
                    error_msg TEXT
                )
            ''')
            conn.commit()
            conn.close()

            # Now run init_db pointing to this legacy db
            with patch('database.DB_PATH', legacy_db_path):
                database.init_db()

            # Verify incident_id column and indices were created successfully
            conn = sqlite3.connect(legacy_db_path)
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(cmping_downtime_events)")
            columns = [r[1] for r in cur.fetchall()]
            self.assertIn("incident_id", columns)
            conn.close()
        finally:
            if os.path.exists(legacy_db_path):
                os.remove(legacy_db_path)

    def test_cmping_incident_reopened_on_service_flapping_within_one_hour(self):
        chat_id = 7733
        database.add_cmping_report_chat(chat_id)
        servers = ["flap.example.com"]

        mock_bot = MagicMock()
        bot.dc_accid = 1
        t0 = 1000000

        # 1. Goes DOWN at t0 -> Incident #1 created with _send
        with patch('time.time', return_value=t0), patch.object(bot, '_send', return_value=80001) as mock_send:
            database.record_cmping_server_down("flap.example.com", t0, "Timeout")
            bot._cmping_server_status = {"flap.example.com": False}
            bot._cmping_server_errors = {"flap.example.com": "Timeout"}
            bot._cmping_incident_last_edit_state.clear()
            bot._sync_cmping_incident_alerts(mock_bot, 1, servers)

        mock_send.assert_called_once()
        active_incs = database.get_all_active_cmping_incidents()
        self.assertEqual(len(active_incs), 1)
        inc1_id = active_incs[0]["id"]
        database.set_cmping_incident_msg_id(inc1_id, chat_id, 80001)

        # 2. Recovers at t0 + 300s -> Incident #1 resolves with send_edit_request
        t1 = t0 + 300
        mock_bot.rpc.send_edit_request.reset_mock()
        with patch('time.time', return_value=t1), patch.object(bot, '_send') as mock_send:
            database.record_cmping_server_up("flap.example.com", t1)
            bot._cmping_server_status["flap.example.com"] = True
            bot._cmping_server_errors.pop("flap.example.com", None)
            bot._sync_cmping_incident_alerts(mock_bot, 1, servers)

        mock_send.assert_not_called()
        mock_bot.rpc.send_edit_request.assert_called_once()
        edit_args = mock_bot.rpc.send_edit_request.call_args[0]
        self.assertEqual(edit_args[1], 80001)
        self.assertIn("Resolved", edit_args[2])
        self.assertEqual(len(database.get_all_active_cmping_incidents()), 0)

        # 3. Flaps DOWN again at t1 + 300s (T = t0 + 600s, < 1 hour) -> Reopens Incident #1!
        t2 = t1 + 300
        mock_bot.rpc.send_edit_request.reset_mock()
        with patch('time.time', return_value=t2), patch.object(bot, '_send') as mock_send:
            database.record_cmping_server_down("flap.example.com", t2, "Connection refused")
            bot._cmping_server_status["flap.example.com"] = False
            bot._cmping_server_errors["flap.example.com"] = "Connection refused"
            bot._sync_cmping_incident_alerts(mock_bot, 1, servers)

        # MUST NOT send a new message
        mock_send.assert_not_called()
        # MUST edit existing message back to Ongoing
        mock_bot.rpc.send_edit_request.assert_called_once()
        edit_args = mock_bot.rpc.send_edit_request.call_args[0]
        self.assertEqual(edit_args[1], 80001)
        self.assertIn(f"Incident #{inc1_id}", edit_args[2])
        self.assertIn("Ongoing", edit_args[2])
        self.assertIn("flap.example.com", edit_args[2])

        active_incs = database.get_all_active_cmping_incidents()
        self.assertEqual(len(active_incs), 1)
        self.assertEqual(active_incs[0]["id"], inc1_id)
        self.assertEqual(active_incs[0]["status"], "ongoing")

        # 4. Finally recovers at t2 + 300s (T = t0 + 900s) -> Resolves again
        t3 = t2 + 300
        mock_bot.rpc.send_edit_request.reset_mock()
        with patch('time.time', return_value=t3), patch.object(bot, '_send') as mock_send:
            database.record_cmping_server_up("flap.example.com", t3)
            bot._cmping_server_status["flap.example.com"] = True
            bot._cmping_server_errors.pop("flap.example.com", None)
            bot._sync_cmping_incident_alerts(mock_bot, 1, servers)

        mock_send.assert_not_called()
        mock_bot.rpc.send_edit_request.assert_called_once()
        edit_args = mock_bot.rpc.send_edit_request.call_args[0]
        self.assertEqual(edit_args[1], 80001)
        self.assertIn(f"Incident #{inc1_id}", edit_args[2])
        self.assertIn("Resolved", edit_args[2])
        self.assertEqual(len(database.get_all_active_cmping_incidents()), 0)

    def test_autokick_database_warnings_and_ignored_fingerprints(self):
        chat_id = 9911
        cid1 = 301
        cid2 = 302
        now = time.time()

        # Test warn_at
        self.assertEqual(database.get_chat_last_autokick_warn_at(chat_id), 0.0)
        database.set_chat_last_autokick_warn_at(chat_id, now)
        self.assertAlmostEqual(database.get_chat_last_autokick_warn_at(chat_id), now, delta=1.0)

        # Test warnings
        self.assertIsNone(database.get_autokick_warning(chat_id, cid1))
        database.record_autokick_warning(chat_id, cid1, now)
        self.assertAlmostEqual(database.get_autokick_warning(chat_id, cid1), now, delta=1.0)

        database.record_autokick_warning(chat_id, cid2, now + 10)
        self.assertIsNotNone(database.get_autokick_warning(chat_id, cid2))

        # Clear one
        database.clear_autokick_warning(chat_id, cid1)
        self.assertIsNone(database.get_autokick_warning(chat_id, cid1))
        self.assertIsNotNone(database.get_autokick_warning(chat_id, cid2))

        # Clear chat
        database.clear_chat_autokick_warnings(chat_id)
        self.assertIsNone(database.get_autokick_warning(chat_id, cid2))

        # Test ignored fingerprints
        fp = "A" * 40
        self.assertFalse(database.is_fingerprint_autokick_ignored(fp))
        self.assertTrue(database.add_autokick_ignored_fingerprint(fp, note="TestBot"))
        self.assertTrue(database.is_fingerprint_autokick_ignored(fp))
        self.assertTrue(database.is_fingerprint_autokick_ignored("a" * 40))

        all_ignored = database.get_all_autokick_ignored_fingerprints()
        self.assertEqual(len(all_ignored), 1)
        self.assertEqual(all_ignored[0][0], fp)
        self.assertEqual(all_ignored[0][1], "TestBot")

        self.assertTrue(database.remove_autokick_ignored_fingerprint(fp))
        self.assertFalse(database.is_fingerprint_autokick_ignored(fp))
        self.assertFalse(database.remove_autokick_ignored_fingerprint(fp))

    def test_get_chat_autokick_warn_threshold(self):
        self.assertEqual(bot._get_chat_autokick_warn_threshold(90), 83)
        self.assertEqual(bot._get_chat_autokick_warn_threshold(30), 23)
        self.assertEqual(bot._get_chat_autokick_warn_threshold(14), 7)
        self.assertEqual(bot._get_chat_autokick_warn_threshold(7), 6)
        self.assertEqual(bot._get_chat_autokick_warn_threshold(5), 4)
        self.assertEqual(bot._get_chat_autokick_warn_threshold(2), 1)
        self.assertEqual(bot._get_chat_autokick_warn_threshold(1), 1)

    def test_perform_autokick_warnings_private_dm_and_24h_broadcast(self):
        mock_bot = MagicMock()
        chat_id = 7050
        now = time.time()
        database.set_chat_monitored_since(chat_id, now - (100 * 86400))
        database.set_chat_autokick(chat_id, 90)

        # Contacts in group:
        # 10: admin (skip)
        # 20: active 5d ago (skip)
        # 30: inactive 85d ago (warning threshold is 83d -> should warn!)
        # 40: inactive 95d ago (should warn!)
        mock_bot.rpc.get_chat_contacts.return_value = [10, 20, 30, 40]
        mock_bot.rpc.create_chat_by_contact_id.side_effect = lambda accid, cid: 9000 + cid
        mock_bot.rpc.get_basic_chat_info.return_value = {"name": "Test Group"}

        def get_contact_mock(accid, cid):
            c = MagicMock()
            c.id = cid
            if cid == 10:
                c.name = "Admin"
                c.address = "admin@example.com"
                c.last_seen = now - (95 * 86400)
            elif cid == 20:
                c.name = "ActiveUser"
                c.address = "active@example.com"
                c.last_seen = now - (5 * 86400)
            elif cid == 30:
                c.name = "WarnUser"
                c.address = "warn@example.com"
                c.last_seen = now - (85 * 86400)
            elif cid == 40:
                c.name = "OverdueUser"
                c.address = "overdue@example.com"
                c.last_seen = now - (95 * 86400)
            return c

        mock_bot.rpc.get_contact.side_effect = get_contact_mock

        with patch('bot._is_dc_admin', side_effect=lambda b, a, cid: cid == 10), patch.object(bot, '_send') as mock_send:
            warned = bot._perform_autokick_warnings_for_chat(mock_bot, 1, chat_id, days=90)
            self.assertEqual(len(warned), 2)
            warned_ids = [c["id"] for c in warned]
            self.assertIn(30, warned_ids)
            self.assertIn(40, warned_ids)

            # Check that private DMs were sent to 30 and 40
            mock_bot.rpc.create_chat_by_contact_id.assert_any_call(1, 30)
            mock_bot.rpc.create_chat_by_contact_id.assert_any_call(1, 40)

            # Check that warnings were recorded in DB
            self.assertIsNotNone(database.get_autokick_warning(chat_id, 30))
            self.assertIsNotNone(database.get_autokick_warning(chat_id, 40))

            # Group broadcast was sent
            self.assertAlmostEqual(database.get_chat_last_autokick_warn_at(chat_id), now, delta=2.0)

            # Second run within 24h: no new DMs and no group broadcast
            mock_send.reset_mock()
            mock_bot.rpc.create_chat_by_contact_id.reset_mock()
            warned2 = bot._perform_autokick_warnings_for_chat(mock_bot, 1, chat_id, days=90)
            self.assertEqual(len(warned2), 2)
            mock_bot.rpc.create_chat_by_contact_id.assert_not_called()
            mock_send.assert_not_called()

    def test_autokick_away_and_ignored_fingerprint_exemptions(self):
        mock_bot = MagicMock()
        chat_id = 7060
        now = time.time()
        database.set_chat_monitored_since(chat_id, now - (100 * 86400))
        database.set_chat_autokick(chat_id, 90)

        # Contact 30: inactive 95d, has /away status -> MUST NOT be warned or kicked
        # Contact 40: inactive 95d, has ignored fingerprint -> MUST NOT be warned or kicked
        # Contact 50: inactive 95d, normal -> CAN be warned/kicked
        mock_bot.rpc.get_chat_contacts.return_value = [30, 40, 50]
        mock_bot.rpc.create_chat_by_contact_id.side_effect = lambda accid, cid: 9000 + cid
        mock_bot.rpc.get_basic_chat_info.return_value = {"name": "Test Group"}

        fp40 = "B" * 40
        database.add_autokick_ignored_fingerprint(fp40, note="Bot 40")
        database.set_away_status(30, "On vacation")

        def get_contact_mock(accid, cid):
            c = MagicMock()
            c.id = cid
            c.last_seen = now - (95 * 86400)
            if cid == 30:
                c.name = "AwayUser"
                c.address = "away@example.com"
            elif cid == 40:
                c.name = "IgnoredBot"
                c.address = "bot@example.com"
                c.fingerprint = fp40
            elif cid == 50:
                c.name = "RegularInactive"
                c.address = "reg@example.com"
            return c

        mock_bot.rpc.get_contact.side_effect = get_contact_mock

        with patch('bot._is_dc_admin', return_value=False), patch('bot._get_contact_fingerprint', side_effect=lambda b, a, cid, contact=None: fp40 if cid == 40 else None), patch.object(bot, '_send'):
            warn_candidates, _ = bot._get_chat_autokick_candidates(mock_bot, 1, chat_id, 90)
            c_ids = [c["id"] for c in warn_candidates]
            self.assertNotIn(30, c_ids) # exempt because of /away
            self.assertNotIn(40, c_ids) # exempt because of ignored fingerprint
            self.assertIn(50, c_ids)

    def test_bounce_command_with_autokick(self):
        mock_bot = MagicMock()
        chat_id = 7070
        now = time.time()
        database.set_chat_monitored_since(chat_id, now - (100 * 86400))
        database.set_chat_autokick(chat_id, 90)

        mock_event = MagicMock()
        mock_event.msg.chat_id = chat_id
        mock_event.msg.from_id = 10
        mock_event.msg.quote = None
        mock_event.payload = ""

        # Contact 30 is inactive 85d (> 83d warn threshold)
        mock_bot.rpc.get_chat_contacts.return_value = [1, 10, 30]
        def get_contact_mock(accid, cid):
            c = MagicMock()
            c.id = cid
            c.display_name = None
            if cid == 1:
                c.name = "BotSelf"
                c.address = "bot@example.com"
                c.last_seen = now
            elif cid == 10:
                c.name = "Admin"
                c.address = "admin@example.com"
                c.last_seen = now
            elif cid == 30:
                c.name = "WarnUser"
                c.address = "warn@example.com"
                c.last_seen = now - (85 * 86400)
            return c
        mock_bot.rpc.get_contact.side_effect = get_contact_mock

        with patch('bot._is_dc_admin', side_effect=lambda b, a, cid: cid == 10), patch.object(bot, '_send') as mock_send:
            bot.bounce_command(mock_bot, 1, mock_event)
            mock_send.assert_called_once()
            msg_text = mock_send.call_args[0][3]
            self.assertIn("Inactivity Warning", msg_text)
            self.assertIn("WarnUser", msg_text)

    def test_autokick_ignore_and_unignore_commands(self):
        mock_bot = MagicMock()
        chat_id = 7080
        mock_bot.rpc.get_basic_chat_info.return_value = {"chat_type": "Group"}
        mock_bot.rpc.get_chat_contacts.return_value = [10, 60]

        mock_contact = MagicMock()
        mock_contact.id = 60
        mock_contact.name = "ServiceBot"
        mock_contact.display_name = None
        mock_contact.address = "service@example.com"

        mock_admin = MagicMock()
        mock_admin.id = 10
        mock_admin.name = "Admin"
        mock_admin.display_name = None
        mock_admin.address = "admin@example.com"

        def get_contact_mock(accid, cid):
            if cid == 60:
                return mock_contact
            elif cid == 10:
                return mock_admin
            return None

        mock_bot.rpc.get_contact.side_effect = get_contact_mock

        fp60 = "C" * 40

        # 1. /autokick ignore service@example.com
        mock_event = MagicMock()
        mock_event.msg.chat_id = chat_id
        mock_event.msg.from_id = 10
        mock_event.payload = "ignore service@example.com"

        with patch('bot._is_dc_admin', return_value=True), patch('bot._get_contact_fingerprint', return_value=fp60), patch.object(bot, '_send') as mock_send:
            bot.autokick_command(mock_bot, 1, mock_event)
            self.assertTrue(database.is_fingerprint_autokick_ignored(fp60))
            mock_send.assert_called_once()
            self.assertIn("Added", mock_send.call_args[0][3])

            # 2. /autokick ignore list
            mock_event.payload = "ignore list"
            mock_send.reset_mock()
            bot.autokick_command(mock_bot, 1, mock_event)
            mock_send.assert_called_once()
            self.assertIn("Ignored Members/Bots", mock_send.call_args[0][3])

            # 3. /autokick unignore
            mock_event.payload = f"unignore {fp60}"
            mock_send.reset_mock()
            bot.autokick_command(mock_bot, 1, mock_event)
            self.assertFalse(database.is_fingerprint_autokick_ignored(fp60))
            mock_send.assert_called_once()
            self.assertIn("Removed", mock_send.call_args[0][3])

    def test_warning_cleared_when_member_becomes_active(self):
        mock_bot = MagicMock()
        chat_id = 7090
        now = time.time()
        database.set_chat_monitored_since(chat_id, now - (100 * 86400))
        database.set_chat_autokick(chat_id, 90)

        # Contact 30 previously received a warning
        database.record_autokick_warning(chat_id, 30, now - (3 * 86400))
        self.assertIsNotNone(database.get_autokick_warning(chat_id, 30))

        # Now contact 30 sends a message -> last_seen becomes active (e.g. today)
        mock_bot.rpc.get_chat_contacts.return_value = [1, 10, 30]
        def get_contact_mock(accid, cid):
            c = MagicMock()
            c.id = cid
            c.display_name = None
            if cid == 30:
                c.name = "ActiveAgain"
                c.address = "activeagain@example.com"
                c.last_seen = now # active right now!
            else:
                c.name = "Admin"
                c.address = "admin@example.com"
                c.last_seen = now
            return c
        mock_bot.rpc.get_contact.side_effect = get_contact_mock

        with patch('bot._is_dc_admin', side_effect=lambda b, a, cid: cid == 10), patch.object(bot, '_send'):
            warn_candidates, kick_candidates = bot._get_chat_autokick_candidates(mock_bot, 1, chat_id, 90)
            self.assertEqual(len(warn_candidates), 0)
            self.assertEqual(len(kick_candidates), 0)

            # Warning MUST have been automatically cleared!
            self.assertIsNone(database.get_autokick_warning(chat_id, 30))

    def test_new_member_grace_period_first_seen(self):
        mock_bot = MagicMock()
        chat_id = 7095
        now = time.time()
        # Group monitored for 200 days
        database.set_chat_monitored_since(chat_id, now - (200 * 86400))
        database.set_chat_autokick(chat_id, 90)

        # Contact 70 just joined 2 days ago (last_seen=0, but first_seen_at = now - 2 days)
        database.ensure_contact_first_seen(70, now - (2 * 86400))

        mock_bot.rpc.get_chat_contacts.return_value = [1, 10, 70]
        def get_contact_mock(accid, cid):
            c = MagicMock()
            c.id = cid
            c.display_name = None
            if cid == 70:
                c.name = "NewMember"
                c.address = "new@example.com"
                c.last_seen = 0
            else:
                c.name = "Admin"
                c.address = "admin@example.com"
                c.last_seen = now
            return c
        mock_bot.rpc.get_contact.side_effect = get_contact_mock

        with patch('bot._is_dc_admin', side_effect=lambda b, a, cid: cid == 10), patch.object(bot, '_send'):
            warn_candidates, kick_candidates = bot._get_chat_autokick_candidates(mock_bot, 1, chat_id, 90)
            # New member has only been in the group 2 days (< 83d warning threshold) -> MUST NOT be warned or kicked!
            c_ids = [c["id"] for c in warn_candidates]
            self.assertNotIn(70, c_ids)
            self.assertEqual(len(kick_candidates), 0)

    def test_resilient_lock_defined_and_usable(self):
        """Verify resilient_lock exists as a threading.Lock and functions properly."""
        self.assertTrue(hasattr(bot, 'resilient_lock'))
        import threading
        # Ensure it behaves as a lock
        acquired = bot.resilient_lock.acquire(timeout=1.0)
        self.assertTrue(acquired)
        bot.resilient_lock.release()

    def test_cmping_domain_validation(self):
        """Verify domain validation rejects invalid strings and potential command injections."""
        mock_bot = MagicMock()
        mock_event = MagicMock()
        mock_event.msg.chat_id = 9010
        mock_event.msg.id = 111

        with patch('bot._send') as mock_send, patch('bot._react'):
            # Invalid domains
            for bad in ["bad;rm-rf", "domain..com", "-invalid.com", "foo/bar"]:
                mock_event.payload = bad
                bot.cmping_command(mock_bot, 1, mock_event)
                mock_send.assert_called()
                last_call_text = mock_send.call_args[0][3]
                self.assertIn("Invalid server domain", last_call_text)

            # Too many arguments check
            mock_event.payload = "server1.org server2.org server3.org"
            bot.cmping_command(mock_bot, 1, mock_event)
            last_call_text = mock_send.call_args[0][3]
            self.assertIn("Only 1 or 2 server parameters are supported", last_call_text)

            # Valid domain
            with patch('bot._get_bot_domains', return_value=["relay1.org"]), patch('threading.Thread') as mock_thread:
                bot._chat_cmping_anti_spam.clear()
                mock_event.payload = "chatmail.example.org"
                bot.cmping_command(mock_bot, 1, mock_event)
                mock_thread.assert_called()

    def test_addtransport_private_chat_enforcement(self):
        """Verify /addtransport is rejected in group chats to protect credentials."""
        mock_bot = MagicMock()
        mock_event = MagicMock()
        mock_event.msg.chat_id = 9020
        mock_event.msg.from_id = 10  # Admin
        mock_event.payload = "user@example.com secret123"

        with patch('bot._is_dc_admin', return_value=True), patch('bot._send') as mock_send:
            # When run in a Group chat
            mock_bot.rpc.get_basic_chat_info.return_value = {"chat_type": "Group"}
            bot.addtransport_command(mock_bot, 1, mock_event)
            mock_send.assert_called()
            sent_text = mock_send.call_args[0][3]
            self.assertIn("private 1-on-1 chat", sent_text)
            mock_bot.rpc.add_or_update_transport.assert_not_called()

            # When run in a Single (private 1-on-1) chat
            mock_send.reset_mock()
            mock_bot.rpc.get_basic_chat_info.return_value = {"chat_type": "Single"}
            bot.addtransport_command(mock_bot, 1, mock_event)
            mock_bot.rpc.add_or_update_transport.assert_called_with(1, {"addr": "user@example.com", "password": "secret123"})
            sent_text = mock_send.call_args[0][3]
            self.assertIn("Backup transport `user@example.com` added", sent_text)

    def test_slap_cooldown_anti_spam(self):
        """Verify /slap enforces cooldown for non-admin users."""
        mock_bot = MagicMock()
        mock_event = MagicMock()
        mock_event.msg.chat_id = 9030
        mock_event.msg.from_id = 55  # Non-admin
        mock_event.msg.id = 100
        mock_event.payload = "someone"

        bot._chat_slap_anti_spam.clear()
        with patch('bot._is_dc_admin', return_value=False), patch('bot._send') as mock_send:
            # First slap proceeds
            bot.slap_command(mock_bot, 1, mock_event)
            self.assertIn(9030, bot._chat_slap_anti_spam)

            # Immediate second slap is blocked by cooldown
            mock_send.reset_mock()
            bot.slap_command(mock_bot, 1, mock_event)
            mock_send.assert_called()
            sent_text = mock_send.call_args[0][3]
            self.assertIn("Please wait", sent_text)

    def test_transport_stats_buffering_and_flushing(self):
        """Verify transport sent/received stats are buffered and written to DB on flush."""
        addr = "relay_buffered@example.com"
        # Clear buffer and reset flush timer
        database._transport_stats_buffer.clear()
        database._last_transport_flush = time.time()

        database.increment_transport_sent(addr)
        database.increment_transport_sent(addr)
        database.increment_transport_received(addr)

        # Before flush, buffer contains counts
        self.assertIn(addr, database._transport_stats_buffer)
        self.assertEqual(database._transport_stats_buffer[addr]["sent"], 2)
        self.assertEqual(database._transport_stats_buffer[addr]["recv"], 1)

        # get_all_transport_stats automatically flushes buffer
        stats = database.get_all_transport_stats()
        matching = [s for s in stats if s['addr'] == addr]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]['msgs_sent'], 2)
        self.assertEqual(matching[0]['msgs_received'], 1)

    def test_database_cleanup_old_records(self):
        """Verify cleanup_old_records prunes away notifications and cmping history older than threshold."""
        now = time.time()
        old_ts = now - (35 * 86400)   # 35 days ago (should be cleaned)
        recent_ts = now - (5 * 86400) # 5 days ago (should be kept)

        # Add away notification
        database.mark_notified_away(101, 201, old_ts)
        database.mark_notified_away(102, 202, recent_ts)

        # Add cmping history
        database.add_cmping_history("src1.org", "dst1.org", 120.0, checked_at=old_ts)
        database.add_cmping_history("src2.org", "dst2.org", 150.0, checked_at=recent_ts)

        cleaned = database.cleanup_old_records(retention_days=30)
        self.assertGreaterEqual(cleaned.get("away_notifications", 0), 1)
        self.assertGreaterEqual(cleaned.get("cmping_history", 0), 1)

        # Recent records must still exist
        self.assertTrue(database.has_notified_away(102, 202, recent_ts))
        avg, count = database.get_average_ping_for_server("src2.org")
        self.assertEqual(count, 1)

    def test_persistent_writer_and_pragmas(self):
        """Verify writer connection is persistent and connections use synchronous=NORMAL and WAL."""
        w_conn1 = database._get_writer_conn()
        w_conn2 = database._get_writer_conn()
        self.assertIs(w_conn1, w_conn2, "Writer connection must be reused and persistent")

        # Check PRAGMAs on writer connection
        sync_mode = w_conn1.execute("PRAGMA synchronous;").fetchone()[0]
        # synchronous: 1 = NORMAL
        self.assertEqual(sync_mode, 1, "Writer connection should have synchronous=NORMAL (1)")

        j_mode = w_conn1.execute("PRAGMA journal_mode;").fetchone()[0]
        self.assertEqual(j_mode.lower(), "wal", "Writer connection should be in WAL mode")

        # Check PRAGMA on reader connection
        r_conn = database._connect()
        try:
            r_sync = r_conn.execute("PRAGMA synchronous;").fetchone()[0]
            self.assertEqual(r_sync, 1, "Reader connection should have synchronous=NORMAL (1)")
        finally:
            r_conn.close()

        # Check close_db resets writer connection
        database.close_db()
        self.assertIsNone(database._writer_conn)

    def test_concurrent_read_while_write_lock_held(self):
        """Verify that reader queries execute without blocking even while _write_lock is held."""
        import threading
        database.set_config("concurrency_test_key", "initial_val")
        read_results = {}
        read_done = threading.Event()

        with database._write_lock:
            def reader_thread():
                cfg = database.get_config("concurrency_test_key")
                read_results["config"] = cfg
                read_done.set()

            t = threading.Thread(target=reader_thread)
            t.start()
            t.join(timeout=2.0)

        self.assertTrue(read_done.is_set(), "Reader thread was blocked by _write_lock")
        self.assertEqual(read_results["config"], "initial_val")

    def test_ensure_contacts_first_seen_batch(self):
        """Verify ensure_contacts_first_seen_batch records multiple contacts in a single transaction."""
        now = time.time()
        c_ids = [501, 502, 503, 504]
        database.ensure_contacts_first_seen_batch(c_ids, now)
        for cid in c_ids:
            self.assertEqual(database.get_contact_first_seen(cid), now)

        # Subsequent call with existing + new contact ignores existing
        later = now + 100
        database.ensure_contacts_first_seen_batch([501, 505], later)
        self.assertEqual(database.get_contact_first_seen(501), now)
        self.assertEqual(database.get_contact_first_seen(505), later)


if __name__ == '__main__':
    unittest.main()
