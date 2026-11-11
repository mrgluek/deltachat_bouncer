"""Monitoring readers use copies; mutations share a short-lived state lock."""
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

TEST_DIR = tempfile.TemporaryDirectory(prefix="test_monitor_locks_")
os.environ["DB_PATH"] = os.path.join(TEST_DIR.name, "bouncer.db")
os.environ["DC_DB_DIR"] = TEST_DIR.name

try:
    import deltachat2
except ImportError:
    sys.modules["deltachat2"] = MagicMock()
try:
    import deltabot_cli
except ImportError:
    class MockBotCli:
        def __init__(self, *args, **kwargs):
            pass

        def on(self, *args, **kwargs):
            return lambda func: func

    sys.modules["deltabot_cli"] = MagicMock(BotCli=MockBotCli)

import database
import state
import cmping
import cmping_commands as commands
import cmcall_monitor


class LockedDict(dict):
    """Make missing lock coverage deterministic, even with the GIL."""
    def check(self):
        if not state._monitor_lock._is_owned():
            raise AssertionError("monitor state accessed without its lock")

    def __iter__(self):
        self.check()
        return super().__iter__()

    def items(self):
        self.check()
        return super().items()

    def keys(self):
        self.check()
        return super().keys()

    def get(self, *args):
        self.check()
        return super().get(*args)

    def __setitem__(self, *args):
        self.check()
        return super().__setitem__(*args)

    def pop(self, *args):
        self.check()
        return super().pop(*args)


class MonitorLocksTests(unittest.TestCase):
    def setUp(self):
        database.close_db()
        test_dir = tempfile.TemporaryDirectory(prefix="test_monitor_locks_db_")
        self.addCleanup(test_dir.cleanup)
        database.DB_PATH = os.path.join(test_dir.name, "bouncer.db")
        database.init_db()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(database.close_db)
        self.bot = MagicMock()
        self.servers = ["a.example", "b.example"]
        self.event = SimpleNamespace(msg=SimpleNamespace(text="", chat_id=1, from_id=2), payload="")
        for name, value in (
            ("_cmping_last_results", LockedDict({("a.example", "b.example"): {"success": False, "error": "failed", "checked_at": 1}})),
            ("_cmping_server_status", LockedDict({"a.example": False, "b.example": True})),
            ("_cmping_server_errors", LockedDict({"a.example": "failed"})),
            ("_cmping_incident_last_edit_state", LockedDict()),
            ("_cmping_monitor_index", 0), ("_cmping_monitor_running", False),
            ("_cmcall_monitor_index", 0), ("_cmcall_monitor_running", False),
        ):
            self.stack.enter_context(patch.object(state, name, value))
        for target, value in (
            ("dc_helpers._get_bot_domains", self.servers),
            ("dc_helpers._is_dc_admin", True),
            ("database.get_all_cmping_monitors", []),
            ("database.get_all_cmping_report_chats", [1]),
            ("relaygeo.flag", ""), ("relaygeo.whitelisted", False),
        ):
            self.stack.enter_context(patch(target, return_value=value))
        self.stack.enter_context(patch("dc_helpers._send", side_effect=self.outside_lock))
        self.stack.enter_context(patch("cmping._queue_cmping_incident_message", side_effect=self.outside_lock))
        self.stack.enter_context(patch("cmping._format_cmping_incident_message", return_value="incident"))
        for name in ("set_config", "save_cmping_result", "delete_cmping_result",
                     "delete_cmping_results_for_domain", "delete_cmping_history_for_domain",
                     "record_cmping_server_down", "record_cmping_server_up", "save_cmcall_result"):
            original = getattr(database, name)

            def checked(*args, _original=original, **kwargs):
                self.outside_lock()
                return _original(*args, **kwargs)

            self.stack.enter_context(patch.object(database, name, side_effect=checked))
        self.incident = {"id": 1, "started_at": int(time.time()) - 30, "status": "ongoing", "resolved_at": None}
        for target, value in (
            ("database.get_all_active_cmping_incidents", [self.incident]),
            ("database.get_recent_cmping_incidents", [self.incident]),
            ("database.get_cmping_incident_by_id", self.incident),
            ("database.get_cmping_incident_downtime_events", [{"server": "a.example", "went_up_at": None}]),
        ):
            self.stack.enter_context(patch(target, return_value=value))

    def outside_lock(self, *args, **kwargs):
        self.assertFalse(state._monitor_lock._is_owned(), "RPC/network work holds monitor lock")

    def stress(self, reader, iterations=60):
        stop = threading.Event()
        started = threading.Event()
        errors = []

        def writer():
            try:
                i = 0
                while not stop.is_set():
                    with state._monitor_lock:
                        server = f"relay{i % 32}.example"
                        state._set_cmping_server_status(server, False, "failed")
                        state._cmping_last_results[(server, "a.example")] = {"success": False, "error": "failed", "checked_at": 1}
                        state._cmping_last_results.pop((f"relay{(i + 1) % 32}.example", "a.example"), None)
                        state._cmping_monitor_index = i
                    started.set()
                    i += 1
                    time.sleep(0)
            except BaseException as exc:
                errors.append(exc)
                started.set()

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            self.assertTrue(started.wait(2))
            for _ in range(iterations):
                reader()
                time.sleep(0)
        finally:
            stop.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        if errors:
            raise errors[0]

    def test_snapshot_is_an_independent_consistent_copy(self):
        snapshot = state._snapshot_cmping_state()
        with state._monitor_lock:
            state._cmping_last_results[("a.example", "b.example")]["error"] = "changed"
            state._set_cmping_server_status("a.example", True)
            state._cmping_monitor_index = 2
            state._cmping_incident_last_edit_state[1] = (3, ())
        self.assertEqual(snapshot["results"][("a.example", "b.example")]["error"], "failed")
        self.assertFalse(snapshot["status"]["a.example"])
        self.assertEqual(snapshot["errors"]["a.example"], "failed")
        self.assertEqual(snapshot["index"], 0)
        self.assertEqual(snapshot["incident_edits"], {})
        snapshot["results"].clear()
        with state._monitor_lock:
            self.assertTrue(state._cmping_last_results)

    def test_snapshot_stress(self):
        def read():
            snapshot = state._snapshot_cmping_state()
            for server, healthy in snapshot["status"].items():
                if not healthy:
                    self.assertIn(server, snapshot["errors"])
            list(snapshot["results"].items())
        self.stress(read)

    def test_status_setter_stress(self):
        self.stress(lambda: state._set_cmping_server_status("a.example", False, "failed"))

    def test_commands_snapshot_stress(self):
        for func, text in (
            (commands.cmpingstatus_command, "/cmpingstatus"),
            (commands.cmpinglist_command, "/cmpinglist"),
            (commands._cmpingfail_impl, "/cmpingfail"),
            (commands.cmpingevents_command, "/cmpingevents"),
            (commands.cmpingevents_command, "/cmpingevents 1"),
            (commands.cmpinghistory_command, "/cmpinghistory"),
            (commands.cmpinghistory_command, "/cmpinghistory a.example"),
            (commands.cmreport_command, "/cmreport"),
        ):
            with self.subTest(command=text):
                self.event.msg.text = text
                self.stress(lambda: func(self.bot, 1, self.event))

    def test_delete_stress(self):
        self.event.msg.text = "/cmpingdel a.example"
        with patch("database.remove_cmping_monitor", return_value=True):
            self.stress(lambda: commands.cmpingdel_command(self.bot, 1, self.event))

    def test_clear_errors_stress(self):
        self.stress(lambda: cmping._clear_all_errors_for_server("a.example"))

    def test_alert_sync_stress(self):
        self.stress(lambda: cmping._sync_cmping_incident_alerts(self.bot, 1, self.servers))
        with patch("database.get_cmping_incident_downtime_events", return_value=[]):
            self.stress(lambda: cmping._sync_cmping_incident_alerts(self.bot, 1, self.servers))

    def test_cmping_cycle_stress(self):
        def pair(*args):
            self.outside_lock()
            return ({"success": True, "checked_at": 1}, {"success": True, "checked_at": 1})

        def cycle():
            with state._monitor_lock:
                state._cmping_monitor_running = False
                state._cmping_server_status.clear()
            cmping._cmping_monitor_cycle(self.bot, 1, "cmping")

        with patch("cmping._run_monitor_pair", side_effect=pair):
            self.stress(cycle)

    def test_cmcall_cycle_stress(self):
        def run(*args):
            self.outside_lock()
            return {"ok": True, "checked_at": 1}
        with patch("cmcall_monitor.sync_alerts", side_effect=self.outside_lock):
            self.stress(lambda: cmcall_monitor.cmcall_monitor_cycle(self.bot, 1, run=run))
        self.assertFalse(state._cmcall_monitor_running)

    def test_monitor_loop_releases_lock_before_sleep(self):
        class StopLoop(Exception):
            pass

        sleeps = 0

        def sleep(_seconds):
            nonlocal sleeps
            self.outside_lock()
            with state._monitor_lock:
                self.assertFalse(state._cmping_monitor_running)
            sleeps += 1
            if sleeps == 31:
                raise StopLoop()

        def cycle(*args):
            self.outside_lock()
            with state._monitor_lock:
                state._cmping_monitor_running = True

        with patch("cmping.config.CMPING_MONITOR_INTERVAL", 60), \
                patch("cmping.time.sleep", side_effect=sleep), \
                patch("cmping._cmping_monitor_cycle", side_effect=cycle):
            with self.assertRaises(StopLoop):
                cmping._cmping_monitor_loop(self.bot, 1)

    def test_cycle_reservation_uses_shared_lock(self):
        # A second thread must not inspect/claim the running flag before this lock is released.
        for cycle in (
            lambda: cmping._cmping_monitor_cycle(self.bot, 1, "cmping"),
            lambda: cmcall_monitor.cmcall_monitor_cycle(self.bot, 1),
        ):
            with self.subTest(cycle=cycle):
                entered = threading.Event()
                with patch("dc_helpers._get_bot_domains", side_effect=lambda *args: entered.set() or []):
                    with state._monitor_lock:
                        thread = threading.Thread(target=cycle)
                        thread.start()
                        entered_while_locked = entered.wait(0.05)
                    thread.join(3)
                    self.assertFalse(entered_while_locked)
                    self.assertFalse(thread.is_alive())
                    self.assertTrue(entered.is_set())


if __name__ == "__main__":
    unittest.main()
