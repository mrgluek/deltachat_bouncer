import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

import call_notices
import cmcall_monitor
import config
import database
from outbox import Outbox, PRIO_ALERT


SERVER = "chat.tatars.cc"
FAILING = "📞🚨 chat.tatars.cc: calls failing"
DEGRADED = "📞⚠️ chat.tatars.cc: calls degraded"
RESTORED = "📞✅ chat.tatars.cc: calls restored after 1h 14m"


class TestCallNotices(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        database.close_db()
        self.enterContext(patch.object(database, "DB_PATH", str(directory / "bouncer.db")))
        self.addCleanup(database.close_db)
        database.init_db()
        self.enterContext(patch.dict("os.environ", {"DC_DB_DIR": str(directory)}))
        self.enterContext(patch.object(config, "CMCALL_NOTICES", True))
        self.enterContext(patch.object(config, "CMCALL_ALERTS", True))
        self.clock = self.enterContext(patch.object(call_notices, "time", Mock()))
        self.now = 10 * 86400
        self.clock.time.side_effect = lambda: self.now
        self.outbox = self.enterContext(patch.object(call_notices, "outbox", Outbox()))
        self.bot = Mock()
        self.bot.rpc.send_msg.side_effect = [101, 102, 103]
        self.bot.rpc.get_existing_msg_ids.side_effect = lambda accid, ids: ids
        database.add_cmping_report_chat(77)
        self.directory = directory

    def notices(self):
        updates = self.bot.rpc.send_webxdc_status_update.call_args_list
        self.assertTrue(all(call.args[0] == 1 for call in updates))
        return [(call.args[1], json.loads(call.args[2])) for call in updates]

    def queue_notice(self, chat_id=77):
        call_notices.queue_notice(self.bot, 1, chat_id, {"server": SERVER, "kind": "down"})

    def saved_app(self, chat_id):
        return json.loads(database.get_config(f"cmcall_notice_app:{chat_id}"))

    def test_notice_text(self):
        event = {"server": SERVER, "kind": "down", "started_at": 100, "error": "timed out"}
        self.assertEqual(call_notices.format_notice(event), FAILING)
        self.assertEqual(call_notices.format_notice(dict(event, kind="degraded")), DEGRADED)
        self.assertEqual(call_notices.format_notice(dict(event, ended_at=4540)), RESTORED)
        long_notice = call_notices.format_notice(dict(event, server="a" * 250, ended_at=4540))
        self.assertEqual(len(long_notice), 100)
        self.assertTrue(long_notice.endswith("…: calls restored after 1h 14m"))

    def test_monitor_notices_on_open_kind_change_and_resolution(self):
        with patch.object(cmcall_monitor.dc_helpers, "_edit_or_send") as text_alert:
            for seconds, status, error in [(0, "down", "timeout"), (30, "down", "no response"),
                                           (60, "degraded", "packet loss"), (4500, "ok", None)]:
                with self.subTest(seconds=seconds):
                    database.set_cmcall_server_status(SERVER, status, error)
                    cmcall_monitor.sync_alerts(self.bot, 1, [SERVER], now=self.now + seconds)
                    if seconds == 30:
                        self.assertEqual(len(self.notices()), 1)
                        self.assertEqual(database.get_open_cmcall_events()[0]["error"], "no response")
        self.assertEqual(self.notices(), [
            (101, {"info": line, "payload": line, "href": "#notices"})
            for line in (FAILING, DEGRADED, RESTORED)
        ])
        self.assertEqual(database.get_open_cmcall_events(), [])
        self.assertEqual(len(database.get_cmcall_events()), 2)
        text_alert.assert_not_called()
        self.bot.rpc.send_msg.assert_called_once()

    def test_each_chat_has_its_own_saved_app(self):
        database.add_cmping_report_chat(78)
        database.set_cmcall_server_status(SERVER, "down", "timeout")
        cmcall_monitor.sync_alerts(self.bot, 1, [SERVER], now=self.now)
        self.assertEqual([call.args[1] for call in self.bot.rpc.send_msg.call_args_list], [77, 78])
        database.close_db()
        self.queue_notice(77)
        self.assertEqual([msg_id for msg_id, _ in self.notices()], [101, 102, 101])
        self.assertEqual(self.saved_app(77), [101, self.now, 2])
        self.assertEqual(self.saved_app(78), [102, self.now, 1])

    def test_rotation_age_and_missing_message(self):
        self.queue_notice()
        self.now += call_notices.MAX_APP_AGE
        self.queue_notice()
        self.now += 1
        self.queue_notice()
        self.bot.rpc.get_existing_msg_ids.side_effect = None
        self.bot.rpc.get_existing_msg_ids.return_value = []
        self.queue_notice()
        self.assertEqual([msg_id for msg_id, _ in self.notices()], [101, 101, 102, 103])
        self.assertEqual(self.saved_app(77), [103, self.now, 1])
        self.bot.rpc.set_pinned_message_state.assert_not_called()

    def test_queued_notices_rotate_at_update_limit_and_pace_both_sends(self):
        database.set_config("cmcall_notice_app:77", json.dumps([101, self.now, 899]))
        self.bot.rpc.send_msg.side_effect = [102]
        # Use the real queue and drain it manually to observe sends after submission.
        self.outbox._thread = object()
        self.queue_notice()
        self.queue_notice()
        self.assertEqual(self.outbox.waiting(), {"alert": 4})
        self.assertEqual(self.bot.rpc.mock_calls, [])
        for _ in range(4):
            with self.outbox._cond:
                job = self.outbox._next_job()
            self.assertEqual(job.priority, PRIO_ALERT)
            self.outbox._run(job)
            job.future.result()
        self.assertEqual([msg_id for msg_id, _ in self.notices()], [101, 102])
        self.assertEqual(self.saved_app(77), [102, self.now, 1])

    def test_bundle_is_created_in_data_dir_and_reused(self):
        self.queue_notice()
        path = Path(self.bot.rpc.send_msg.call_args.args[2].file)
        self.assertEqual(path, self.directory / "call-notices.xdc")
        with ZipFile(path) as archive:
            self.assertEqual(set(archive.namelist()), {"index.html", "manifest.toml", "icon.png"})
            self.assertIsNone(archive.testzip())
        self.assertEqual(call_notices._build_app(), str(path))
