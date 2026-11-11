"""/ban, /unban: the ban list (database.py) and its enforcement (moderation.py)."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

TEST_DB = "test_ban_bouncer.db"
os.environ["DB_PATH"] = TEST_DB

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
import moderation

FP_A = "A" * 40
FP_B = "B" * 40
FP_ADMIN = "C" * 40


class Base(unittest.TestCase):
    def setUp(self):
        database.close_db()
        database.DB_PATH = TEST_DB
        self._cleanup_files()
        database.init_db()
        self.bot = MagicMock()
        self.people = {10: ("Alice", "alice@example.org", FP_A), 11: ("Bob", "bob@example.org", FP_B),
                       12: ("Admin", "admin@example.org", FP_ADMIN)}
        self.chats = {100: [1, 10, 11, 12], 101: [1, 10], 102: [1, 11]}
        self.removed = []
        rpc = self.bot.rpc
        rpc.get_chatlist_entries.return_value = list(self.chats)
        rpc.get_basic_chat_info.return_value = {"chat_type": "Group"}
        rpc.get_chat_contacts.side_effect = lambda accid, chat_id: list(self.chats[chat_id])
        rpc.get_contact.side_effect = lambda accid, cid: SimpleNamespace(
            name=self.people[cid][0], display_name=self.people[cid][0], address=self.people[cid][1], id=cid)
        rpc.get_contact_ids.return_value = []
        rpc.remove_contact_from_chat.side_effect = lambda accid, chat, cid: (
            self.removed.append((chat, cid)), self.chats[chat].remove(cid))
        patches = [
            patch.object(dc_helpers, "_get_contact_fingerprint", side_effect=lambda b, a, cid, contact=None: (
                self.people[cid][2] if cid in self.people else None)),
            patch.object(dc_helpers, "_is_dc_admin", side_effect=lambda b, a, cid, contact=None: cid in (12, 99)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._cleanup_files)

    @staticmethod
    def _cleanup_files():
        database.close_db()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(TEST_DB + suffix)
            except FileNotFoundError:
                pass

    def _event(self, payload, from_id=99, quote=None):
        ev = MagicMock()
        ev.msg.chat_id, ev.msg.from_id, ev.msg.id = 100, from_id, 5
        ev.msg.quote = quote
        ev.payload = payload
        return ev

    def _run(self, fn, payload, **kw):
        with patch.object(dc_helpers, "_send") as send:
            fn(self.bot, 1, self._event(payload, **kw))
        return send.call_args[0][3]


class TestDatabase(Base):
    def test_add_normalizes_and_is_idempotent(self):
        self.assertTrue(database.add_banned_fingerprint("aa:aa " + "AA" * 18, contact_id=10, name="Alice"))
        self.assertTrue(database.is_fingerprint_banned(FP_A.lower()))
        self.assertFalse(database.add_banned_fingerprint(FP_A, contact_id=77))
        self.assertEqual(database.get_all_banned()[0]["contact_id"], 77)  # a later id is remembered
        self.assertEqual(len(database.get_all_banned()), 1)

    def test_empty_and_remove(self):
        self.assertFalse(database.add_banned_fingerprint("  "))
        database.add_banned_fingerprint(FP_B)
        self.assertTrue(database.remove_banned_fingerprint(FP_B))
        self.assertFalse(database.remove_banned_fingerprint(FP_B))
        self.assertFalse(database.is_fingerprint_banned(FP_B))


class TestBanCommand(Base):
    def test_admin_only(self):
        text = self._run(moderation.ban_command, "10", from_id=50)
        self.assertIn("Only the bot administrator", text)
        self.assertEqual(database.get_all_banned(), [])

    def test_ban_by_id_removes_from_every_group(self):
        text = self._run(moderation.ban_command, "10")
        self.assertTrue(database.is_fingerprint_banned(FP_A))
        self.assertEqual(sorted(self.removed), [(100, 10), (101, 10)])
        self.assertIn("Removed from 2 group(s)", text)
        self.assertIn("alice@example.org", text)
        self.assertIn(11, self.chats[102])  # others stay

    def test_ban_by_name_address_and_reply(self):
        self._run(moderation.ban_command, "bob")
        self.assertTrue(database.is_fingerprint_banned(FP_B))
        database.remove_banned_fingerprint(FP_B)
        self.bot.rpc.get_contact_ids.return_value = [11]  # no longer in this chat: found by the core's search
        self._run(moderation.ban_command, "bob@example.org")
        self.assertTrue(database.is_fingerprint_banned(FP_B))
        self.bot.rpc.get_message.return_value = SimpleNamespace(from_id=10)
        self._run(moderation.ban_command, "", quote={"message_id": 7})
        self.assertTrue(database.is_fingerprint_banned(FP_A))

    def test_ban_by_fingerprint_finds_members_in_other_groups(self):
        text = self._run(moderation.ban_command, FP_B.lower())
        self.assertTrue(database.is_fingerprint_banned(FP_B))
        self.assertEqual(sorted(self.removed), [(100, 11), (102, 11)])
        self.assertEqual(database.get_all_banned()[0]["contact_id"], 11)  # id remembered for the hourly sweep
        self.assertIn("Removed from 2", text)

    def test_never_bans_admin_or_self_or_no_key(self):
        text = self._run(moderation.ban_command, "12 1 999")
        self.assertEqual(database.get_all_banned(), [])
        self.assertIn("cannot ban the bot administrator", text)
        self.assertIn("not a person", text)
        self.assertEqual(self.removed, [])

    def test_ambiguous_and_unknown(self):
        self.people[13] = ("Bobby", "bobby@example.org", "D" * 40)
        self.chats[100].append(13)
        text = self._run(moderation.ban_command, "bob nobody")
        self.assertIn("matches 2 contacts", text)
        self.assertIn("nobody found", text)
        self.assertEqual(database.get_all_banned(), [])

    def test_list_shows_bans(self):
        self.assertIn("empty", self._run(moderation.ban_command, ""))
        self._run(moderation.ban_command, "10")
        self.assertIn("Alice", self._run(moderation.ban_command, "list"))


class TestUnban(Base):
    def test_unban_by_address_id_prefix(self):
        for target in ("alice@example.org", "10", FP_A[:8]):
            database.add_banned_fingerprint(FP_A, contact_id=10, name="Alice", addr="alice@example.org")
            text = self._run(moderation.unban_command, target)
            self.assertIn("Unbanned Alice", text)
            self.assertFalse(database.is_fingerprint_banned(FP_A))

    def test_unban_unknown_and_admin_only(self):
        self.assertIn("No ban matches", self._run(moderation.unban_command, "nobody@example.org"))
        database.add_banned_fingerprint(FP_A, name="Alice")
        self.assertIn("Only the bot administrator", self._run(moderation.unban_command, "alice", from_id=50))
        self.assertTrue(database.is_fingerprint_banned(FP_A))


class TestEnforcement(Base):
    def test_join_of_banned_key_is_undone(self):
        database.add_banned_fingerprint(FP_A, name="Alice")  # banned by fingerprint only
        self.assertTrue(moderation.enforce_ban_on_join(self.bot, 1, 100, 10))
        self.assertEqual(self.removed, [(100, 10)])
        self.assertEqual(database.get_all_banned()[0]["contact_id"], 10)

    def test_other_joins_untouched(self):
        self.assertFalse(moderation.enforce_ban_on_join(self.bot, 1, 100, 11))  # empty ban list
        database.add_banned_fingerprint(FP_A)
        self.assertFalse(moderation.enforce_ban_on_join(self.bot, 1, 100, 11))
        self.assertFalse(moderation.enforce_ban_on_join(self.bot, 1, 100, 1))   # the bot itself
        self.assertEqual(self.removed, [])

    def test_admin_is_never_removed(self):
        database.add_banned_fingerprint(FP_ADMIN)
        self.assertFalse(moderation.enforce_ban_on_join(self.bot, 1, 100, 12))
        self.assertEqual(moderation.ban_sweep(self.bot, 1, deep_fps={FP_ADMIN}), 0)

    def test_hourly_sweep_uses_remembered_ids(self):
        database.add_banned_fingerprint(FP_B, contact_id=11)
        self.assertEqual(moderation.ban_sweep(self.bot, 1), 2)
        self.assertEqual(sorted(self.removed), [(100, 11), (102, 11)])
        self.bot.rpc.get_chat_contacts.reset_mock()
        database.remove_banned_fingerprint(FP_B)
        self.assertEqual(moderation.ban_sweep(self.bot, 1), 0)
        self.bot.rpc.get_chat_contacts.assert_not_called()  # nothing banned: no scan


if __name__ == "__main__":
    unittest.main()
