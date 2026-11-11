"""Failures must release the dedup claim so the other core event can retry."""
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from message_dedup import MessageDedup


class TestMessageDedup(unittest.TestCase):
    def test_fetch_failure_then_other_event_succeeds_once(self):
        cache = MessageDedup()
        rpc = Mock()
        rpc.get_message.side_effect = [RuntimeError("RPC unavailable"), "command"]
        handle = Mock()

        def event():
            with cache.processing(1, 42) as claimed:
                if claimed:
                    handle(rpc.get_message(1, 42))

        with self.assertRaises(RuntimeError):
            event()
        event()
        event()
        self.assertEqual(rpc.get_message.call_count, 2)
        handle.assert_called_once_with("command")

    def test_handler_failure_releases_claim(self):
        cache = MessageDedup()
        handle = Mock(side_effect=[RuntimeError("failed handler"), None])
        for _ in range(2):
            try:
                with cache.processing(1, 42) as claimed:
                    self.assertTrue(claimed)
                    handle()
            except RuntimeError:
                pass
        self.assertEqual(handle.call_count, 2)

    def test_accounts_have_independent_message_ids(self):
        cache = MessageDedup()
        for accid in (1, 2):
            with cache.processing(accid, 42) as claimed:
                self.assertTrue(claimed)

    def test_inflight_is_not_evicted_or_processed_twice(self):
        cache = MessageDedup(limit=1)
        with cache.processing(1, 42) as claimed:
            self.assertTrue(claimed)
            for msgid in (43, 44):
                with cache.processing(1, msgid):
                    pass
            with cache.processing(1, 42) as duplicate:
                self.assertFalse(duplicate)
        with cache.processing(1, 42) as duplicate:
            self.assertFalse(duplicate)
        with cache.processing(1, 43) as evicted:
            self.assertTrue(evicted)


class TestBotDedup(unittest.TestCase):
    def setUp(self):
        # Reuse the full bot import stubs used by the existing integration tests.
        from tests import test_bouncer
        self.module = test_bouncer.bot
        self.bot = Mock()
        self.bot.rpc.get_all_account_ids.return_value = []
        self.msg = SimpleNamespace(from_id=10, is_info=False, text="/command")

        class RpcError(Exception):
            pass

        self.RpcError = RpcError
        transport = ModuleType("deltachat2.transport")
        transport.JsonRpcError = RpcError
        with patch.dict(sys.modules, {"deltachat2.transport": transport}), \
                patch.object(sys.modules["deltachat2"], "SpecialContactId", SimpleNamespace(SELF=1, LAST_SPECIAL=9)), \
                patch.object(self.module, "setup_custom_command_parser"), \
                patch.object(self.module.dc_helpers.outbox, "start"):
            self.module.on_start(self.bot, None)

    def test_single_event_rpc_failure_then_batch_retry(self):
        self.bot.rpc.get_message.side_effect = [self.RpcError("temporary"), self.msg]
        self.bot.rpc.get_next_msgs.return_value = [42]
        self.bot._process_message(1, 42)
        self.bot._process_messages(1)
        self.bot._process_message(1, 42)
        self.bot._on_new_msg.assert_called_once_with(1, self.msg)
        self.assertEqual(self.bot.rpc.get_message.call_count, 2)

    def test_batch_fetch_failure_retries_then_deduplicates_single_event(self):
        self.bot.rpc.get_message.side_effect = [self.RpcError("temporary"), self.msg]
        self.bot.rpc.get_next_msgs.return_value = [42]
        self.bot._process_messages(1)
        self.bot._process_message(1, 42)
        self.bot._on_new_msg.assert_called_once_with(1, self.msg)
        self.assertEqual(self.bot.rpc.get_message.call_count, 2)

    def test_cursor_failure_does_not_repeat_successful_handler(self):
        self.bot.rpc.get_message.return_value = self.msg
        self.bot.rpc.get_next_msgs.return_value = [42]
        self.bot.rpc.set_config.side_effect = self.RpcError("cursor update")
        self.bot._process_messages(1)
        self.bot._process_message(1, 42)
        self.bot._on_new_msg.assert_called_once_with(1, self.msg)

    def test_handler_failure_then_retry_and_independent_account(self):
        self.bot.rpc.get_message.return_value = self.msg
        self.bot._on_new_msg.side_effect = [RuntimeError("handler"), None, None]
        with self.assertRaises(RuntimeError):
            self.bot._process_message(1, 42)
        self.bot._process_message(1, 42)
        self.bot._process_message(1, 42)
        self.bot._process_message(2, 42)
        self.assertEqual(self.bot._on_new_msg.call_count, 3)


if __name__ == "__main__":
    unittest.main()
