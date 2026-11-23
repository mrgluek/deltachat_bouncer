"""2.55.3: the own key fingerprint is read from the core once per account, not per lookup.

Every get_chat_securejoin_qr_code call mints an invite token in the core that is never pruned
(1.2 million tokens, +51,000 a day on production)."""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

TEST_DIR = tempfile.TemporaryDirectory(prefix="test_release_2553_")
os.environ["DB_PATH"] = os.path.join(TEST_DIR.name, "bouncer.db")

try:
    import deltachat2  # noqa: F401
except ImportError:
    sys.modules["deltachat2"] = MagicMock()
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
    sys.modules["deltabot_cli"] = MagicMock(BotCli=MockBotCli)

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

with patch("os.path.isfile", return_value=False):
    import commands
    import database
    import dc_fingerprint
    import dc_helpers
    import moderation

SELF_FP = "D58B5E6C55482F94F9F5B84B6FB9FE3AD2658082"
ADMIN_FP = "9E1B0E5B8CB9CAE6A9D4F0D558C6D85CE9BD3041"
INVITE = f"https://i.delta.chat/#{SELF_FP}&v=3&i=abc&s=def&a=bot%40example.org&n=Bot"


def peer_fp(cid: int) -> str:
    return f"{cid:040X}"


def make_rpc(members=0):
    rpc = MagicMock()
    rpc.get_chat_securejoin_qr_code.return_value = INVITE
    rpc.get_contact_encryption_info.side_effect = lambda accid, cid: (
        f"Messages are end-to-end encrypted.\nFingerprints:\n\nMe:\n{SELF_FP}\n\n"
        f"Peer {cid}:\n{peer_fp(cid)}\n\nRelays:\npeer@example.org")
    rpc.get_chat_contacts.return_value = [1] + list(range(20, 20 + members))
    rpc.get_contact.side_effect = lambda accid, cid: SimpleNamespace(
        name=f"Member {cid}", display_name=f"Member {cid}", address=f"m{cid}@example.org",
        last_seen=1900000000.0, id=cid)
    return rpc


class Base(unittest.TestCase):
    def setUp(self):
        dc_fingerprint.forget_self_fingerprint()
        self.temp_dir = tempfile.TemporaryDirectory(prefix="r2553_")
        self.addCleanup(self.temp_dir.cleanup)
        database.close_db()
        database.DB_PATH = os.path.join(self.temp_dir.name, "bouncer.db")
        database.init_db()
        self.addCleanup(database.close_db)
        self.addCleanup(dc_fingerprint.forget_self_fingerprint)


class TestOwnFingerprintCache(Base):
    def test_one_qr_call_for_1000_lookups(self):
        rpc = make_rpc()
        for i in range(1000):
            self.assertEqual(dc_fingerprint.get_contact_fingerprint(rpc, 1, 20 + i % 50), peer_fp(20 + i % 50))
        self.assertEqual(rpc.get_chat_securejoin_qr_code.call_count, 1)

    def test_one_qr_call_for_1000_admin_checks(self):
        database.set_config("admin_dc_email", "admin@example.org")
        database.set_admin_fingerprint(peer_fp(30))
        bot = SimpleNamespace(rpc=make_rpc())
        for i in range(1000):
            self.assertEqual(dc_helpers._is_dc_admin(bot, 1, 30 + (i % 2)), i % 2 == 0)
        self.assertLessEqual(bot.rpc.get_chat_securejoin_qr_code.call_count, 1)

    def test_failure_is_not_cached_and_accounts_are_separate(self):
        rpc = MagicMock()
        rpc.get_chat_securejoin_qr_code.side_effect = [Exception("down"), INVITE]
        self.assertIsNone(dc_fingerprint.get_self_fingerprint(rpc, 1))
        self.assertEqual(dc_fingerprint.get_self_fingerprint(rpc, 1), SELF_FP)
        self.assertEqual(rpc.get_chat_securejoin_qr_code.call_count, 2)
        rpc.get_chat_securejoin_qr_code.side_effect = None
        rpc.get_chat_securejoin_qr_code.return_value = INVITE
        self.assertEqual(dc_fingerprint.get_self_fingerprint(rpc, 2), SELF_FP)
        self.assertEqual(rpc.get_chat_securejoin_qr_code.call_count, 3)
        dc_fingerprint.forget_self_fingerprint(rpc, 1)
        dc_fingerprint.get_self_fingerprint(rpc, 1)
        self.assertEqual(rpc.get_chat_securejoin_qr_code.call_count, 4)

    def test_rpc_that_cannot_be_weakly_referenced(self):
        class Slotted:
            __slots__ = ("n",)

            def __init__(self):
                self.n = 0

            def get_chat_securejoin_qr_code(self, accid, chat):
                self.n += 1
                return INVITE
        rpc = Slotted()
        for _ in range(10):
            self.assertEqual(dc_fingerprint.get_self_fingerprint(rpc, 1), SELF_FP)
        self.assertEqual(rpc.n, 1)

    def test_parse_fix_for_unicode_line_separators_is_kept(self):
        info = f"Fingerprints:\n\nMe:\n{SELF_FP}\n\nA\u2028B:\n{peer_fp(5)}"
        self.assertEqual(dc_fingerprint.parse_key_fingerprints(info), [SELF_FP, peer_fp(5)])


class TestAutokickPassScale(Base):
    def test_200_members_3_passes_cost_at_most_one_qr_call(self):
        database.set_admin_fingerprint(ADMIN_FP)
        bot = SimpleNamespace(rpc=make_rpc(200))
        database.set_chat_monitored_since(100, 1.0)
        for _ in range(3):
            res = moderation._get_chat_autokick_overview(bot, 1, 100, 90)
            self.assertEqual(res["total_members"], 200)
        self.assertLessEqual(bot.rpc.get_chat_securejoin_qr_code.call_count, 1)
        self.assertEqual(bot.rpc.get_contact_encryption_info.call_count, 200 * 3 * 2)

    def test_help_command_costs_at_most_one_qr_call(self):
        database.set_admin_fingerprint(ADMIN_FP)
        bot = MagicMock()
        bot.rpc = make_rpc()
        event = SimpleNamespace(msg=SimpleNamespace(from_id=30, chat_id=7, id=1, text="/help"))
        for _ in range(5):
            commands.help_command(bot, 1, event)
        self.assertLessEqual(bot.rpc.get_chat_securejoin_qr_code.call_count, 1)


if __name__ == "__main__":
    unittest.main()
