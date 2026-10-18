"""The paced outbox in Bouncer: priorities, merging of monitoring alerts and reactions while a
backlog waits, wait=True for messages whose id is needed, and no self-deadlock in the worker."""
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

TEST_DB = "test_bouncer_outbox.db"
os.environ["DB_PATH"] = TEST_DB

try:
    import deltachat2  # noqa: F401
except ImportError:
    mock_deltachat2 = MagicMock()

    class MsgData:
        def __init__(self, text="", file="", override_sender_name=None):
            self.text = text
            self.file = file
            self.override_sender_name = override_sender_name

    mock_deltachat2.MsgData = MsgData
    sys.modules["deltachat2"] = mock_deltachat2

try:
    import deltabot_cli  # noqa: F401
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
    sys.modules["deltabot_cli"] = mock_deltabot_cli

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cmcall_monitor  # noqa: E402
import cmping  # noqa: E402
import database  # noqa: E402
import dc_helpers  # noqa: E402
import outbox  # noqa: E402
import state  # noqa: E402
from outbox import Outbox, PRIO_ALERT, PRIO_REPLY, PRIO_LOW  # noqa: E402


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def queued(burst=100):
    """An outbox that queues (as if started) but has no worker: the test drains it by hand."""
    ob = Outbox(rate_per_minute=60, burst=burst, clock=FakeClock())
    ob._thread = object()
    return ob


def drain(ob):
    sent = []
    while True:
        with ob._cond:
            while ob._heap and ob._heap[0][2].done:
                ob._heap.pop(0)
            if not ob._heap:
                return sent
            job = ob._next_job()
        ob._run(job)
        sent.append(job.label)


class TestOutbox(unittest.TestCase):
    def test_order(self):
        ob = queued()
        for prio, label in ((PRIO_LOW, "autokick warning"), (PRIO_REPLY, "reply"), (PRIO_ALERT, "alert")):
            ob.submit(lambda: None, priority=prio, label=label)
        self.assertEqual(drain(ob), ["alert", "reply", "autokick warning"])

    def test_wait_inside_a_job_does_not_deadlock(self):
        ob = Outbox(rate_per_minute=600, burst=5)
        ob.start()
        inner = ob.submit(lambda: ob.submit(lambda: "nested").result(timeout=2))
        self.assertEqual(inner.result(timeout=5), "nested")

    def test_defaults_match_relay_headroom(self):
        self.assertLess(outbox.RATE_PER_MINUTE, 60)
        self.assertLess(outbox.BURST, 10)


class TestHelpersQueued(unittest.TestCase):
    def setUp(self):
        database.close_db()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(TEST_DB + suffix):
                os.remove(TEST_DB + suffix)
        database.DB_PATH = TEST_DB
        database.init_db()
        self.ob = queued()
        patcher = patch.object(dc_helpers, "outbox", self.ob)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bot = MagicMock()
        self.bot.rpc.send_msg.side_effect = iter(range(5000, 6000))

    def test_send_returns_none_while_queued(self):
        self.assertIsNone(dc_helpers._send(self.bot, 1, 10, "hello"))
        self.assertEqual(self.ob.waiting(), {"reply": 1})
        drain(self.ob)
        self.assertEqual(self.bot.rpc.send_msg.call_args.args[2].text, "hello")

    def test_wait_returns_the_id(self):
        ob = Outbox(rate_per_minute=600, burst=5)
        ob.start()
        with patch.object(dc_helpers, "outbox", ob):
            self.assertEqual(dc_helpers._send(self.bot, 1, 10, "status", wait=True), 5000)

    def test_reaction_merged(self):
        dc_helpers._react(self.bot, 1, 77, "⏳")
        dc_helpers._react(self.bot, 1, 77, "✅")
        drain(self.ob)
        self.bot.rpc.send_reaction.assert_called_once_with(1, 77, ["✅"])

    def test_edit_falls_back_to_new_message(self):
        self.bot.rpc.send_edit_request.side_effect = RuntimeError("message deleted")
        stored = []
        dc_helpers._edit_or_send(self.bot, 1, 10, 4242, "report", on_new_id=stored.append)
        drain(self.ob)
        self.assertEqual(stored, [5000])
        self.assertEqual(self.bot.rpc.send_msg.call_args.args[2].text, "report")

    def test_cmping_incident_merged_while_waiting(self):
        chat_id = 7799
        database.add_cmping_report_chat(chat_id)
        servers = ["n1.cc", "n2.cc"]
        state._cmping_incident_last_edit_state.clear()
        state._cmping_server_status = {"n1.cc": False, "n2.cc": True}
        state._cmping_server_errors = {"n1.cc": "Timeout"}
        database.record_cmping_server_down("n1.cc", int(time.time()), "Timeout")
        cmping._sync_cmping_incident_alerts(self.bot, 1, servers)
        # Second server fails before the first message went out
        database.record_cmping_server_down("n2.cc", int(time.time()), "Refused")
        state._cmping_server_status["n2.cc"] = False
        state._cmping_server_errors["n2.cc"] = "Refused"
        cmping._sync_cmping_incident_alerts(self.bot, 1, servers)
        self.assertEqual(self.ob.waiting(), {"alert": 1})

        drain(self.ob)
        self.bot.rpc.send_msg.assert_called_once()  # one message with both servers
        text = self.bot.rpc.send_msg.call_args.args[2].text
        self.assertIn("n1.cc", text)
        self.assertIn("n2.cc", text)
        inc = database.get_active_cmping_incident()
        self.assertEqual(database.get_cmping_incident_msg_ids(inc["id"]).get(chat_id), 5000)

        # Later updates edit that message
        database.record_cmping_server_up("n1.cc", int(time.time()))
        state._cmping_server_status["n1.cc"] = True
        cmping._sync_cmping_incident_alerts(self.bot, 1, servers)
        drain(self.ob)
        self.assertEqual(self.bot.rpc.send_edit_request.call_args.args[1], 5000)

    def test_cmcall_alert_merged_per_chat(self):
        event = {"id": 1}
        with patch.object(cmcall_monitor, "format_event_message", side_effect=["first", "second"]), \
             patch.object(cmcall_monitor.database, "get_cmcall_event_msg_ids", return_value={}), \
             patch.object(cmcall_monitor.database, "set_cmcall_event_msg_id") as store:
            cmcall_monitor._publish(self.bot, 1, event, [10, 11], 0)
            cmcall_monitor._publish(self.bot, 1, event, [10, 11], 0)
            self.assertEqual(self.ob.waiting(), {"alert": 2})  # one per chat, not four
            drain(self.ob)
        self.assertEqual([c.args[2].text for c in self.bot.rpc.send_msg.call_args_list], ["second", "second"])
        self.assertEqual(store.call_count, 2)

    def test_autokick_warning_is_low_priority(self):
        dc_helpers._send(self.bot, 1, 10, "warning", priority=dc_helpers.PRIO_LOW)
        dc_helpers._send(self.bot, 1, 11, "reply")
        self.assertEqual(drain(self.ob), ["message to chat 11", "message to chat 10"])


if __name__ == "__main__":
    unittest.main()
