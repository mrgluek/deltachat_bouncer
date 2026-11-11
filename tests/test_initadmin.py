"""Private, key-bound admin registration and legacy compatibility."""
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

TEST_DIR = tempfile.TemporaryDirectory(prefix="test_initadmin_")
TEST_DB = os.path.join(TEST_DIR.name, "bouncer.db")
os.environ["DB_PATH"] = TEST_DB
os.environ.setdefault("DC_DB_DIR", TEST_DIR.name)

try:
    import deltachat2  # noqa: F401
except ImportError:
    sys.modules['deltachat2'] = MagicMock()
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
    sys.modules['deltabot_cli'] = MagicMock(BotCli=MockBotCli)

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import dc_helpers
import commands
import set_admin

FP_A = "A" * 40
FP_B = "B" * 64
SELF_FP = "C" * 40


class TestInitAdmin(unittest.TestCase):
    def setUp(self):
        database.close_db()
        database.DB_PATH = TEST_DB
        database.init_db()
        self.addCleanup(self.cleanup_db)
        self.bot = MagicMock()
        self.bot.rpc.get_basic_chat_info.return_value = {"chat_type": "Single"}
        self.bot.rpc.get_contact.return_value = SimpleNamespace(address="alice@example.org")
        self.bot.rpc.get_chat_securejoin_qr_code.return_value = "https://i.delta.chat/#" + SELF_FP + "&token=x"
        self.bot.rpc.get_contact_encryption_info.return_value = (
            "Fingerprints:\n\nMe:\n" + SELF_FP + "\n\nAlice:\n" + FP_A)
        self.event = SimpleNamespace(msg=SimpleNamespace(chat_id=100, from_id=10))

    def cleanup_db(self):
        database.close_db()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(TEST_DB + suffix)
            except FileNotFoundError:
                pass

    def run_command(self):
        with patch.object(dc_helpers, "_send") as send:
            commands.initadmin_command(self.bot, 1, self.event)
        return send.call_args[0][3]

    def assert_unclaimed(self):
        self.assertFalse(database.get_config("admin_dc_email"))
        self.assertFalse(database.get_admin_fingerprint())

    def test_private_chat_claims_email_and_fingerprint(self):
        self.assertIn("You are now the admin", self.run_command())
        self.assertEqual(database.get_config("admin_dc_email"), "alice@example.org")
        self.assertEqual(database.get_admin_fingerprint(), FP_A)

    def test_group_and_other_chat_types_rejected(self):
        for chat_type in ("Group", "Mailinglist", "InBroadcast", "OutBroadcast", None):
            with self.subTest(chat_type=chat_type):
                self.bot.rpc.get_basic_chat_info.return_value = {"chat_type": chat_type}
                self.assertIn("end-to-end encrypted private chat", self.run_command())
                self.assert_unclaimed()

    def test_chat_lookup_failure_rejected(self):
        self.bot.rpc.get_basic_chat_info.side_effect = RuntimeError("unavailable")
        self.assertIn("end-to-end encrypted private chat", self.run_command())
        self.assert_unclaimed()

    def test_missing_or_ambiguous_fingerprint_rejected(self):
        for info in ("No encryption.", "Alice:\n" + FP_A,
                     "Me:\n" + SELF_FP + "\n\nAlice:\n" + FP_A + "\n\nBob:\n" + FP_B):
            with self.subTest(info=info):
                self.bot.rpc.get_contact_encryption_info.return_value = info
                self.assertIn("end-to-end encrypted private chat", self.run_command())
                self.assert_unclaimed()

    def test_fingerprint_rpc_failure_rejected(self):
        self.bot.rpc.get_contact_encryption_info.side_effect = RuntimeError("unavailable")
        self.assertIn("end-to-end encrypted private chat", self.run_command())
        self.assert_unclaimed()

    def test_existing_admin_never_overwritten(self):
        for email, fp in (("owner@example.org", FP_B), ("alice@example.org", FP_B), ("", FP_B)):
            with self.subTest(email=email):
                database.set_config("admin_dc_email", email)
                database.set_admin_fingerprint(fp)
                self.assertIn("already set", self.run_command())
                self.assertEqual(database.get_config("admin_dc_email"), email)
                self.assertEqual(database.get_admin_fingerprint(), fp)

    def test_legacy_admin_untouched_and_authorized_with_warning(self):
        database.set_config("admin_dc_email", "alice@example.org")
        self.assertIn("already set", self.run_command())
        self.assertFalse(database.get_admin_fingerprint())
        with self.assertLogs(dc_helpers.logger, level="WARNING") as logs:
            self.assertTrue(dc_helpers._is_dc_admin(self.bot, 1, 10))
        self.assertIn("fingerprint", " ".join(logs.output))
        self.assertEqual(database.get_config("admin_dc_email"), "alice@example.org")

    def test_key_bound_admin_authorization_unchanged(self):
        database.set_config("admin_dc_email", "alice@example.org")
        database.set_admin_fingerprint(FP_B)
        self.assertFalse(dc_helpers._is_dc_admin(self.bot, 1, 10))
        database.set_admin_fingerprint(FP_A)
        self.bot.rpc.get_contact.return_value.address = "alias@example.org"
        self.assertTrue(dc_helpers._is_dc_admin(self.bot, 1, 10))

    def test_simultaneous_commands_have_only_one_winner(self):
        barrier = threading.Barrier(2)
        def fingerprint(rpc, accid, cid):
            barrier.wait(timeout=5)
            return FP_A if cid == 10 else FP_B
        def register(cid):
            event = SimpleNamespace(msg=SimpleNamespace(chat_id=100 + cid, from_id=cid))
            commands.initadmin_command(self.bot, 1, event)
        self.bot.rpc.get_contact.side_effect = lambda a, cid: SimpleNamespace(address=f"{cid}@example.org")
        with patch.object(commands.dc_fingerprint, "get_contact_fingerprint", side_effect=fingerprint), \
                patch.object(dc_helpers, "_send") as send, ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(register, (10, 11)))
        replies = [call.args[3] for call in send.call_args_list]
        self.assertEqual(sum("You are now the admin" in text for text in replies), 1)
        self.assertEqual(sum("already set" in text for text in replies), 1)
        pair = (database.get_config("admin_dc_email"), database.get_admin_fingerprint())
        self.assertIn(pair, (("10@example.org", FP_A), ("11@example.org", FP_B)))

    def test_database_claim_preserves_existing_and_legacy_admin(self):
        for email, fp in (("owner@example.org", FP_B), ("owner@example.org", ""), ("", FP_B)):
            with self.subTest(email=email, fp=fp):
                database.set_config("admin_dc_email", email)
                database.set_admin_fingerprint(fp)
                self.assertFalse(database.claim_admin("alice@example.org", FP_A))
                self.assertEqual((database.get_config("admin_dc_email"), database.get_admin_fingerprint()),
                                 (email, fp))

    def test_claim_rolls_back_both_values_on_failure(self):
        with database._writer_transaction() as conn:
            conn.execute("CREATE TRIGGER fail_fingerprint BEFORE INSERT ON config "
                         "WHEN NEW.key = 'admin_dc_fingerprint' BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            database.claim_admin("alice@example.org", FP_A)
        self.assert_unclaimed()

    def test_operator_cli_can_set_replace_and_clear_admin(self):
        for values, expected in ((("owner@example.org", "aa:aa " + "AA" * 18), ("owner@example.org", FP_A)),
                                 (("new@example.org", FP_B), ("new@example.org", FP_B)),
                                 (("clear", "clear"), ("", ""))):
            with patch("builtins.input", side_effect=values), patch("builtins.print"):
                set_admin.main()
            self.assertEqual((database.get_config("admin_dc_email"), database.get_admin_fingerprint()), expected)
