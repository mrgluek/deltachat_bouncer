"""Autokick warnings must reach the core before starting the grace period."""
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

TEST_DIR = tempfile.TemporaryDirectory(prefix="test_autokick_warnings_")
os.environ["DB_PATH"] = os.path.join(TEST_DIR.name, "bouncer.db")

try:
    import deltachat2  # noqa: F401
except ImportError:
    sys.modules["deltachat2"] = MagicMock()
try:
    import deltabot_cli  # noqa: F401
except ImportError:
    sys.modules["deltabot_cli"] = MagicMock()

# Tests must not load local secret configuration.
with patch("os.path.isfile", return_value=False):
    import database
    import dc_helpers
    import moderation


class TestAutokickWarnings(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="autokick_")
        self.addCleanup(self.temp_dir.cleanup)
        database.close_db()
        database.DB_PATH = os.path.join(self.temp_dir.name, "bouncer.db")
        database.init_db()
        self.addCleanup(database.close_db)
        self.now = 2000000000.0
        self.clock = patch("moderation.time.time", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        database.set_chat_monitored_since(100, self.now - 100 * 86400)
        self.bot = MagicMock()
        self.bot.rpc.get_chat_contacts.return_value = [1, 10, 11]
        self.bot.rpc.get_basic_chat_info.return_value = {"name": "Test Group"}
        self.bot.rpc.get_contact.side_effect = lambda accid, cid: SimpleNamespace(
            name=f"Member {cid}", display_name=f"Member {cid}",
            address=f"member{cid}@example.org", last_seen=1900000000.0)
        self.bot.rpc.create_chat_by_contact_id.side_effect = lambda accid, cid: 9000 + cid
        self.bot.rpc.send_msg.return_value = 123
        for name in ("_is_dc_admin", "_is_contact_autokick_ignored"):
            p = patch.object(dc_helpers, name, return_value=False)
            p.start()
            self.addCleanup(p.stop)

    def warn(self):
        return moderation._perform_autokick_warnings_for_chat(self.bot, 1, 100, 90)

    def kick(self):
        return moderation._perform_autokick_for_chat(self.bot, 1, 100, 90)

    def test_success_records_private_warning_once(self):
        self.warn()
        for cid in (10, 11):
            self.assertEqual(database.get_autokick_warning(100, cid), self.now)
        self.assertEqual(database.get_chat_last_autokick_warn_at(100), self.now)
        self.bot.rpc.send_msg.reset_mock()
        self.warn()
        self.bot.rpc.send_msg.assert_not_called()

    def test_send_raising_does_not_record_or_block_other_members(self):
        def send(accid, chat_id, msg):
            if chat_id == 9010:
                raise RuntimeError("private send failed")
            return 123
        self.bot.rpc.send_msg.side_effect = send
        with self.assertLogs("bouncer_bot", level="ERROR"):
            self.warn()
            self.warn()
        self.assertIsNone(database.get_autokick_warning(100, 10))
        self.assertEqual(database.get_autokick_warning(100, 11), self.now)
        private_attempts = [c for c in self.bot.rpc.send_msg.call_args_list if c.args[1] == 9010]
        self.assertEqual(len(private_attempts), 2)  # One per run, even on persistent failure.

    def test_send_none_does_not_record_warning_or_broadcast(self):
        self.bot.rpc.send_msg.return_value = None
        with self.assertLogs("bouncer_bot", level="ERROR"):
            self.warn()
        self.assertIsNone(database.get_autokick_warning(100, 10))
        self.assertIsNone(database.get_autokick_warning(100, 11))
        self.assertEqual(database.get_chat_last_autokick_warn_at(100), 0)

    def test_outbox_exception_does_not_record_or_stop_the_run(self):
        with patch.object(dc_helpers, "_send", side_effect=RuntimeError("outbox failed")) as send:
            with self.assertLogs("bouncer_bot", level="ERROR"):
                self.warn()
        self.assertEqual([c.args[2] for c in send.call_args_list], [9010, 9011, 100])
        self.assertIsNone(database.get_autokick_warning(100, 10))
        self.assertIsNone(database.get_autokick_warning(100, 11))
        self.assertEqual(database.get_chat_last_autokick_warn_at(100), 0)

    def test_failed_send_retries_then_kicks_only_after_grace_period(self):
        self.bot.rpc.send_msg.side_effect = RuntimeError("send failed")
        self.warn()
        self.now += 2 * 86400
        self.assertEqual(self.kick(), [])
        self.bot.rpc.remove_contact_from_chat.assert_not_called()
        self.bot.rpc.send_msg.side_effect = None
        self.warn()
        delivered_at = self.now
        self.assertEqual(database.get_autokick_warning(100, 10), delivered_at)
        self.now += 86400 - 1
        self.assertEqual(self.kick(), [])
        self.now += 1
        self.assertEqual([c["id"] for c in self.kick()], [10, 11])
        self.assertIsNone(database.get_autokick_warning(100, 10))

    def test_kick_never_happens_without_recorded_warning(self):
        self.assertEqual(self.kick(), [])
        self.now += 30 * 86400
        self.assertEqual(self.kick(), [])
        self.bot.rpc.remove_contact_from_chat.assert_not_called()

    def test_group_failure_does_not_invalidate_delivered_private_warning(self):
        def send(accid, chat_id, msg):
            if chat_id == 100:
                raise RuntimeError("group send failed")
            return 123
        self.bot.rpc.send_msg.side_effect = send
        self.warn()
        self.assertEqual(database.get_autokick_warning(100, 10), self.now)
        self.assertEqual(database.get_chat_last_autokick_warn_at(100), 0)

    def test_outbox_future_is_awaited_before_recording_delivery_time(self):
        submitted = threading.Event()
        future = Future()
        failures = []
        database.record_autokick_warning(100, 11, self.now)
        database.set_chat_last_autokick_warn_at(100, self.now)

        def submit(fn, **kwargs):
            submitted.set()
            return future

        def run():
            try:
                self.warn()
            except BaseException as exc:
                failures.append(exc)

        with patch.object(dc_helpers.outbox, "submit", side_effect=submit) as mock_submit:
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(submitted.wait(2))
                self.assertIsNone(database.get_autokick_warning(100, 10))
                self.assertTrue(worker.is_alive())
                self.now += 60
            finally:
                future.set_result(123)
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(database.get_autokick_warning(100, 10), self.now)
            self.assertTrue(mock_submit.call_args.kwargs["wait"])
            self.assertEqual(mock_submit.call_args.kwargs["priority"], dc_helpers.PRIO_LOW)


if __name__ == "__main__":
    unittest.main()
