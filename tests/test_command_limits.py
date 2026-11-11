import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

import tempfile
from types import SimpleNamespace

_test_dir = tempfile.TemporaryDirectory()
TEST_DB = os.path.join(_test_dir.name, "limits.db")
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


import calls
import cmping_commands
import config
import database
import dc_helpers
import dnscheck
import state
import virustotal
import dns.exception
import dns.resolver


def tearDownModule():
    database.close_db()
    _test_dir.cleanup()


class TestCommandLimits(unittest.TestCase):
    def setUp(self):
        for name in ("_command_pending", "_virus_anti_spam", "_virus_cooldown_notified"):
            patcher = patch.object(state, name, {}, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("MAX_QUEUED_VIRUS", "MAX_QUEUED_CMPING", "MAX_QUEUED_CMCALL"):
            patcher = patch.object(config, name, 3, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        state._chat_cmping_anti_spam.clear()
        state._chat_cmcall_anti_spam.clear()
        self.bot = MagicMock()
        self.event = SimpleNamespace(payload="relay.example", msg=SimpleNamespace(
            id=9, chat_id=8, from_id=7, text="/cmpingadd relay.example"))
        for name, kwargs in (("_send", {}), ("_react", {}),
                             ("_is_dc_admin", {"return_value": True}),
                             ("_get_bot_domains", {"return_value": ["bot.example"]})):
            patcher = patch.object(dc_helpers, name, **kwargs)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, {"VIRUSTOTAL_API_KEY": "test"})
        env.start()
        self.addCleanup(env.stop)

    def test_each_command_refuses_full_queue_without_thread_or_reaction(self):
        for name, handler, cap in (("virus", virustotal.virus_command, config.MAX_QUEUED_VIRUS),
                                   ("cmping", cmping_commands.cmping_command, config.MAX_QUEUED_CMPING),
                                   ("cmcall", calls.cmcall_command, config.MAX_QUEUED_CMCALL)):
            with self.subTest(command=name), patch("threading.Thread") as thread:
                state._command_pending[name] = cap
                self.event.payload = "https://relay.example" if name == "virus" else "relay.example"
                self._send.reset_mock()
                self._react.reset_mock()
                handler(self.bot, 1, self.event)
                thread.assert_not_called()
                self._react.assert_not_called()
                self._send.assert_called_once_with(self.bot, 1, 8,
                    "⏳ Too many requests are waiting, please try again in a minute.")

    def test_virus_cooldown_is_per_sender_across_chats_and_admin_exempt(self):
        self._is_dc_admin.return_value = False
        self.event.payload = "https://relay.example"
        with patch("threading.Thread") as thread:
            virustotal.virus_command(self.bot, 1, self.event)
            self.event.msg.chat_id = 88
            virustotal.virus_command(self.bot, 1, self.event)
            self.assertEqual(thread.call_count, 1)
            self.bot.rpc.send_reaction.assert_called_once_with(1, 9, [dc_helpers.reaction_text("⏳", "cooldown")])
            self._send.assert_not_called()
            self.event.msg.from_id = 77
            virustotal.virus_command(self.bot, 1, self.event)
            self.assertEqual(thread.call_count, 2)
            self._is_dc_admin.return_value = True
            virustotal.virus_command(self.bot, 1, self.event)
            self.assertEqual(thread.call_count, 3)

    def test_virus_cooldown_notifies_once_per_sender_per_window(self):
        self._is_dc_admin.return_value = False
        self.event.payload = "https://relay.example"
        with patch("threading.Thread") as thread, \
                patch.object(virustotal.time, "time", return_value=1000) as clock, \
                patch.object(dc_helpers, "_react_or_send") as notify:
            virustotal.virus_command(self.bot, 1, self.event)
            for chat_id in range(10, 15):
                self.event.msg.chat_id = chat_id
                virustotal.virus_command(self.bot, 1, self.event)
            notify.assert_called_once_with(self.bot, 1, 10, 9, "⏳", "cooldown",
                "⏳ /virus is on cooldown, please try again in a moment.")
            self._send.assert_not_called()
            self.assertEqual(thread.call_count, 1)
            clock.return_value += config.VIRUSTOTAL_USER_COOLDOWN_SECONDS
            virustotal.virus_command(self.bot, 1, self.event)
            virustotal.virus_command(self.bot, 1, self.event)
            virustotal.virus_command(self.bot, 1, self.event)
            self.assertEqual(thread.call_count, 2)
            self.assertEqual(notify.call_count, 2)

    def _run_add_monitor(self):
        with patch.object(database, "is_cmping_monitor", return_value=False), \
                patch("threading.Thread") as thread:
            cmping_commands.cmpingadd_command(self.bot, 1, self.event)
            self._react.assert_called_once_with(self.bot, 1, 9, "⏳")
            kwargs = thread.call_args.kwargs
            kwargs["target"](*kwargs["args"])

    def test_cmpingadd_finishes_reaction_on_success(self):
        with patch.object(dc_helpers, "_public_relay", return_value=True), \
                patch.object(database, "add_cmping_monitor") as add:
            self._run_add_monitor()
        add.assert_called_once_with("relay.example")
        self.assertEqual(self._react.call_args.args, (self.bot, 1, 9, "☑️"))
        self.assertEqual(state._command_pending["cmping"], 0)

    def test_cmpingadd_finishes_reaction_on_non_public_host(self):
        with patch.object(dc_helpers, "_public_relay", return_value=False), \
                patch.object(database, "add_cmping_monitor") as add:
            self._run_add_monitor()
        add.assert_not_called()
        self.assertEqual(self._react.call_args.args, (self.bot, 1, 9, "❌"))
        self.assertEqual(state._command_pending["cmping"], 0)

    def test_cmpingadd_finishes_reaction_on_exception(self):
        with patch.object(dc_helpers, "_public_relay", return_value=True), \
                patch.object(database, "add_cmping_monitor", side_effect=RuntimeError("database")):
            with self.assertRaisesRegex(RuntimeError, "database"):
                self._run_add_monitor()
        self.assertEqual(self._react.call_args.args, (self.bot, 1, 9, "❌"))
        self.assertEqual(state._command_pending["cmping"], 0)

    def test_slots_release_on_worker_exception_and_start_failure(self):
        target = MagicMock(side_effect=RuntimeError("worker"))
        with patch("threading.Thread") as thread:
            dc_helpers._start_command_worker(self.bot, 1, self.event.msg, "virus", 1, target, ())
            self.assertEqual(state._command_pending["virus"], 1)
            with self.assertRaisesRegex(RuntimeError, "worker"):
                thread.call_args.kwargs["target"]()
            self.assertEqual(state._command_pending["virus"], 0)
            thread.return_value.start.side_effect = RuntimeError("start")
            with self.assertRaisesRegex(RuntimeError, "start"):
                dc_helpers._start_command_worker(self.bot, 1, self.event.msg, "virus", 1, target, ())
            self.assertEqual(state._command_pending["virus"], 0)

    def test_slot_reservations_are_atomic(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        barrier = threading.Barrier(13)
        def submit(_):
            barrier.wait()
            dc_helpers._start_command_worker(self.bot, 1, self.event.msg, "virus", 3, lambda: None, ())
        with ThreadPoolExecutor(max_workers=12) as pool:
            # Patch after starting the real pool threads, so only admitted workers are mocked.
            ready = [pool.submit(barrier.wait) for _ in range(12)]
            barrier.wait()
            for future in ready:
                future.result()
            barrier = threading.Barrier(12)
            with patch("dc_helpers.threading.Thread") as thread:
                list(pool.map(submit, range(12)))
                self.assertEqual(thread.call_count, 3)
                self.assertEqual(state._command_pending["virus"], 3)

    def test_ip_literals_are_rejected_by_all_handlers(self):
        with patch("threading.Thread") as thread:
            for host in ("127.0.0.1", "10.0.0.5", "8.8.8.8", "[::1]", "[2606:4700:4700::1111]"):
                self.event.payload = host
                self.event.msg.text = "/cmpingadd " + host
                for handler in (cmping_commands.cmping_command, calls.cmcall_command,
                                cmping_commands.cmpingadd_command):
                    handler(self.bot, 1, self.event)
            thread.assert_not_called()

    def test_public_resolution_rejects_private_mixed_multicast_and_timeout(self):
        resolver = MagicMock()
        with patch.object(dnscheck, "make_resolver", return_value=resolver):
            for addresses, allowed in ((["127.0.0.1"], False), (["10.0.0.5"], False),
                                       (["8.8.8.8", "192.168.0.1"], False),
                                       (["224.0.0.1"], False), (["::1"], False),
                                       (["8.8.8.8", "2606:4700:4700::1111"], True), ([], False)):
                resolver.resolve.side_effect = [[SimpleNamespace(to_text=lambda a=a: a)
                                                for a in addresses], []]
                self.assertEqual(dc_helpers._public_relay("relay.example"), allowed, addresses)
                self.assertEqual(resolver.resolve.call_args.kwargs["lifetime"], 3)
            resolver.resolve.side_effect = dns.exception.Timeout
            self.assertFalse(dc_helpers._public_relay("relay.example"))

    def test_workers_and_monitor_add_refuse_non_public_destination(self):
        with patch.object(dc_helpers, "_public_relay", return_value=False, create=True), \
                patch.object(cmping_commands, "_bg_cmping_worker_inner") as ping, \
                patch.object(calls, "run_cmcall") as call, \
                patch.object(database, "add_cmping_monitor") as add:
            cmping_commands.bg_cmping_worker(self.bot, 1, 8, 9, ["bot.example"], ["relay.example"])
            calls.bg_cmcall_worker(self.bot, 1, 8, 9, "relay.example", "relay.example")
            cmping_commands._add_public_monitor(self.bot, 1, 8, 9, "relay.example", ["bot.example"])
            ping.assert_not_called()
            call.assert_not_called()
            add.assert_not_called()
            self.assertEqual(self._send.call_count, 3)
            self.assertEqual(self._send.call_args.args[3], "❌ relay.example does not point to a public address.")
            self.assertFalse(state._cmping_global_lock.locked())
            self.assertFalse(state._cmcall_global_lock.locked())

    def test_public_destinations_still_run_and_monitor_add_resolves_in_worker(self):
        with patch.object(dc_helpers, "_public_relay", return_value=True) as public, \
                patch.object(cmping_commands, "_bg_cmping_worker_inner") as ping, \
                patch.object(calls, "run_cmcall", return_value={"ok": True}) as call, \
                patch.object(calls, "format_cmcall_report", return_value="report"), \
                patch.object(database, "add_cmping_monitor") as add, \
                patch.object(database, "is_cmping_monitor", return_value=False), \
                patch("threading.Thread") as thread:
            cmping_commands.cmpingadd_command(self.bot, 1, self.event)
            public.assert_not_called()
            add.assert_not_called()
            kwargs = thread.call_args.kwargs
            kwargs["target"](*kwargs["args"])
            add.assert_called_once_with("relay.example")
            cmping_commands.bg_cmping_worker(self.bot, 1, 8, 9, ["bot.example"], ["relay.example"])
            calls.bg_cmcall_worker(self.bot, 1, 8, 9, "relay.example", "relay.example")
            ping.assert_called_once()
            call.assert_called_once()
