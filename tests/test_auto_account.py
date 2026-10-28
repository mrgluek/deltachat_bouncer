import os
import sys
import unittest
from unittest.mock import MagicMock

# Setup test environment
TEST_DB = "test_auto_account.db"
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

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot


class TestAutoAccount(unittest.TestCase):
    def _args(self, account=None):
        args = MagicMock(account=account)
        args.cmd = bot.config.dc_cli.__class__  # anything not named _serve_cmd
        return args

    def _serve_args(self, account=None):
        def _serve_cmd(*_):
            pass
        return MagicMock(account=account, cmd=_serve_cmd)

    def test_serve_command_name_matches_deltabot_cli(self):
        try:
            from deltabot_cli import cli
        except ImportError:
            self.skipTest("deltabot_cli not installed")
        self.assertEqual(cli._serve_cmd.__name__, "_serve_cmd")

    def test_creates_account_on_serve_without_configured_one(self):
        b = MagicMock()
        b.rpc.get_all_account_ids.return_value = []
        b.rpc.add_account.return_value = 1
        b.rpc.get_contact.return_value = {"address": "abc@chatmail.example"}  # self contact
        bot.ensure_configured_account(b, self._serve_args())
        b.rpc.set_config.assert_any_call(1, "bot", "1")
        b.rpc.init_transports.assert_called_once_with(1, None)
        self.assertIn("abc@chatmail.example", b.logger.info.call_args.args[0])

    def test_reuses_unconfigured_account(self):
        b = MagicMock()
        b.rpc.get_all_account_ids.return_value = [3]
        b.rpc.is_configured.return_value = False
        bot.ensure_configured_account(b, self._serve_args())
        b.rpc.add_account.assert_not_called()
        b.rpc.init_transports.assert_called_once_with(3, None)

    def test_uses_account_given_with_option(self):
        b = MagicMock()
        b.rpc.is_configured.return_value = False
        bot.ensure_configured_account(b, self._serve_args(account=5))
        b.rpc.get_all_account_ids.assert_not_called()
        b.rpc.init_transports.assert_called_once_with(5, None)

    def test_nothing_when_configured_or_not_serving(self):
        b = MagicMock()
        b.rpc.get_all_account_ids.return_value = [1]
        b.rpc.is_configured.return_value = True
        bot.ensure_configured_account(b, self._serve_args())
        b.rpc.is_configured.return_value = False
        bot.ensure_configured_account(b, self._args())
        b.rpc.init_transports.assert_not_called()
        b.rpc.add_account.assert_not_called()

    def test_failure_is_logged_not_raised(self):
        b = MagicMock()
        b.rpc.get_all_account_ids.return_value = []
        b.rpc.init_transports.side_effect = RuntimeError("no relay answered")
        bot.ensure_configured_account(b, self._serve_args())
        b.logger.error.assert_called_once()


if __name__ == "__main__":
    unittest.main()
