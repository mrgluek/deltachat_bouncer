import os
import shutil
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Setup test environment
TEST_DB = "test_faq.db"
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

import database
import bot  # noqa: F401  (loads every module, like the real bot)
import faq
import state

CHAT = 10
ALICE = 20
BOB = 21
FAQ_BOT = 30


def _contact(name, is_bot=False):
    return SimpleNamespace(display_name=name, name=name, auth_name=name, address=f"{name.lower().replace(' ', '')}@example.org", is_bot=is_bot)


def _msg(msg_id, text, from_id=ALICE, quote_id=None, view_type="Text", file=None, chat_id=CHAT):
    return SimpleNamespace(
        id=msg_id, chat_id=chat_id, from_id=from_id, text=text, view_type=view_type, file=file,
        quote={"message_id": quote_id} if quote_id else None,
    )


def _event(msg):
    parts = (msg.text or "").split(maxsplit=1)
    return SimpleNamespace(msg=msg, command=parts[0] if parts else "", payload=parts[1] if len(parts) > 1 else "")


def _sync_thread(target, args=(), daemon=True):
    t = MagicMock()
    t.start.side_effect = lambda: target(*args)
    return t


class FaqTestBase(unittest.TestCase):
    def setUp(self):
        database.close_db()
        # Other test modules set DB_PATH first when the whole suite runs
        self._orig_db_path = database.DB_PATH
        database.DB_PATH = TEST_DB
        for suffix in ["", "-wal", "-shm"]:
            if os.path.exists(TEST_DB + suffix):
                os.remove(TEST_DB + suffix)
        database.init_db()

        self.media_dir = tempfile.mkdtemp(prefix="dc_test_faq_")
        self._orig_media = state.FAQ_MEDIA_DIR
        state.FAQ_MEDIA_DIR = os.path.join(self.media_dir, "faq")
        state._faq_anti_spam.clear()
        state._faq_bot_cache.clear()

        self.contacts = {ALICE: _contact("Alice"), BOB: _contact("Bob")}
        self.messages = {}
        self.members = [1, ALICE, BOB]

        self.bot = MagicMock()
        self.bot.rpc.get_contact.side_effect = lambda accid, cid: self.contacts[cid]
        self.bot.rpc.get_message.side_effect = lambda accid, mid: self.messages[mid]
        self.bot.rpc.get_chat_contacts.side_effect = lambda accid, chat_id: list(self.members)
        self.bot.rpc.get_config.return_value = None

        self.admin_patch = patch("dc_helpers._is_dc_admin", return_value=False)
        self.is_admin = self.admin_patch.start()

    def tearDown(self):
        self.admin_patch.stop()
        state.FAQ_MEDIA_DIR = self._orig_media
        shutil.rmtree(self.media_dir, ignore_errors=True)
        database.close_db()
        for suffix in ["", "-wal", "-shm"]:
            if os.path.exists(TEST_DB + suffix):
                os.remove(TEST_DB + suffix)
        database.DB_PATH = self._orig_db_path

    def add_faq_bot(self):
        self.contacts[FAQ_BOT] = _contact("FAQ Bot", is_bot=True)
        self.members.append(FAQ_BOT)

    def sent(self):
        """[(text, quoted_message_id, file)] of every message the bot sent."""
        return [(c[0][2].text, c[0][2].quoted_message_id, c[0][2].file)
                for c in self.bot.rpc.send_msg.call_args_list]

    def save(self, tag, answer_text, from_id=ALICE, msg_id=200, answer_id=100, cmd="/save"):
        self.messages[answer_id] = _msg(answer_id, answer_text, from_id=BOB)
        msg = _msg(msg_id, f"{cmd} {tag}", from_id=from_id, quote_id=answer_id)
        faq.save_command(self.bot, 1, _event(msg))
        return msg


class TestTopics(unittest.TestCase):
    def test_normalize_and_display(self):
        self.assertEqual(faq.normalize_tag("  Group   RULES "), "group rules")
        self.assertEqual(faq.display_tag("  Group   RULES "), "Group RULES")

    def test_validate(self):
        self.assertIsNone(faq.validate_tag("rules"))
        self.assertIsNotNone(faq.validate_tag("   "))
        self.assertIsNotNone(faq.validate_tag("/rules"))
        self.assertIsNotNone(faq.validate_tag("x" * 101))

    def test_faq_bot_needs_bot_flag(self):
        self.assertTrue(faq._is_faq_bot_contact(_contact("FAQ Bot", is_bot=True)))
        self.assertFalse(faq._is_faq_bot_contact(_contact("FAQ Bot", is_bot=False)))
        self.assertFalse(faq._is_faq_bot_contact(_contact("Bouncer", is_bot=True)))


class TestFaqDatabase(FaqTestBase):
    def test_round_trip(self):
        self.assertIsNone(database.save_faq_entry(CHAT, "rules", "Rules", "Be nice", None, None, ALICE))
        entry = database.get_faq_entry(CHAT, "rules")
        self.assertEqual(entry["text"], "Be nice")
        self.assertEqual(entry["author_id"], ALICE)
        self.assertIsNone(database.get_faq_entry(CHAT + 1, "rules"))

    def test_overwrite_keeps_created_at(self):
        database.save_faq_entry(CHAT, "rules", "Rules", "v1", None, None, ALICE)
        created = database.get_faq_entry(CHAT, "rules")["created_at"]
        time.sleep(0.01)
        old = database.save_faq_entry(CHAT, "rules", "Rules", "v2", None, None, BOB)
        self.assertEqual(old["text"], "v1")
        entry = database.get_faq_entry(CHAT, "rules")
        self.assertEqual((entry["text"], entry["author_id"], entry["created_at"]), ("v2", BOB, created))

    def test_topics_sorted_and_delete(self):
        database.save_faq_entry(CHAT, "links", "Links", "a", None, None, ALICE)
        database.save_faq_entry(CHAT, "about", "About", "b", None, None, ALICE)
        self.assertEqual(database.get_faq_topics(CHAT), ["About", "Links"])
        self.assertEqual(database.delete_faq_entry(CHAT, "links")["text"], "a")
        self.assertIsNone(database.delete_faq_entry(CHAT, "links"))
        self.assertEqual(database.get_faq_topics(CHAT), ["About"])


class TestSave(FaqTestBase):
    def test_save_text(self):
        self.save("Rules", "Be nice, {name}")
        entry = database.get_faq_entry(CHAT, "rules")
        self.assertEqual((entry["display_tag"], entry["text"], entry["author_id"]), ("Rules", "Be nice, {name}", ALICE))
        self.assertIn("Saved FAQ topic **Rules**", self.sent()[0][0])

    def test_save_without_quote_shows_usage(self):
        faq.save_command(self.bot, 1, _event(_msg(200, "/save rules")))
        self.assertIsNone(database.get_faq_entry(CHAT, "rules"))
        self.assertIn("Reply to a message", self.sent()[0][0])

    def test_save_slash_topic_rejected(self):
        self.save("/rules", "x")
        self.assertEqual(database.get_faq_topics(CHAT), [])
        self.assertIn("can't start with", self.sent()[0][0])

    def test_overwrite_only_by_author_or_admin(self):
        self.save("rules", "v1", from_id=ALICE)
        self.save("RULES", "v2", from_id=BOB, msg_id=201, answer_id=101)
        self.assertEqual(database.get_faq_entry(CHAT, "rules")["text"], "v1")
        self.assertIn("saved by someone else", self.sent()[-1][0])

        self.save("rules", "v3", from_id=ALICE, msg_id=202, answer_id=102)
        self.assertEqual(database.get_faq_entry(CHAT, "rules")["text"], "v3")
        self.assertIn("Updated", self.sent()[-1][0])

        self.is_admin.side_effect = lambda b, a, cid, *args, **kw: cid == BOB
        self.save("rules", "v4", from_id=BOB, msg_id=203, answer_id=103)
        self.assertEqual(database.get_faq_entry(CHAT, "rules")["text"], "v4")

    @patch("faq.threading.Thread", side_effect=_sync_thread)
    def test_save_attachment_copies_file(self, _thread):
        src_dir = tempfile.mkdtemp(dir=self.media_dir)
        src = os.path.join(src_dir, "blob123.png")
        with open(src, "wb") as f:
            f.write(b"png")
        self.messages[100] = _msg(100, "the logo", from_id=BOB, view_type="Image", file=None)
        with patch("dc_helpers._get_msg_file_info", return_value={"path": src, "filename": "logo.png"}):
            faq.save_command(self.bot, 1, _event(_msg(200, "/save logo", quote_id=100)))

        entry = database.get_faq_entry(CHAT, "logo")
        self.assertEqual((entry["text"], entry["viewtype"]), ("the logo", "Image"))
        self.assertTrue(entry["file_path"].startswith(state.FAQ_MEDIA_DIR))
        self.assertEqual(os.path.basename(entry["file_path"]), "logo.png")
        self.assertTrue(os.path.exists(entry["file_path"]))
        self.bot.rpc.send_reaction.assert_any_call(1, 200, ["☑️"])

        # Overwriting with plain text deletes the stored file
        self.save("logo", "no logo anymore", msg_id=201, answer_id=101)
        self.assertFalse(os.path.exists(entry["file_path"]))
        self.assertIsNone(database.get_faq_entry(CHAT, "logo")["file_path"])

    @patch("faq.threading.Thread", side_effect=_sync_thread)
    def test_save_attachment_download_failure(self, _thread):
        self.messages[100] = _msg(100, "", from_id=BOB, view_type="Image")
        with patch("dc_helpers._get_msg_file_info", return_value=None):
            faq.save_command(self.bot, 1, _event(_msg(200, "/save logo", quote_id=100)))
        self.assertIsNone(database.get_faq_entry(CHAT, "logo"))
        self.assertIn("Could not download", self.sent()[-1][0])

    def test_delete_file_refuses_foreign_path(self):
        outside = os.path.join(self.media_dir, "a", "b", "keep.txt")
        os.makedirs(os.path.dirname(outside))
        open(outside, "w").close()
        faq._delete_file(outside)
        self.assertTrue(os.path.exists(outside))


class TestAnswers(FaqTestBase):
    def setUp(self):
        super().setUp()
        database.save_faq_entry(CHAT, "rules", "Rules", "Hi {name}! Topics:\n{faq}", None, None, ALICE)
        database.save_faq_entry(CHAT, "links", "Links", "see site", None, None, ALICE)

    def test_topic_message_is_answered(self):
        faq.faq_topic_handler(self.bot, 1, _event(_msg(300, "  RULES ", from_id=BOB)))
        text, quoted, _ = self.sent()[0]
        self.assertEqual(quoted, 300)
        self.assertEqual(text, "Hi Bob! Topics:\n• Links\n• Rules")

    def test_topic_as_reply_answers_the_quoted_person(self):
        self.messages[299] = _msg(299, "how do I join?", from_id=BOB)
        faq.faq_topic_handler(self.bot, 1, _event(_msg(300, "rules", from_id=ALICE, quote_id=299)))
        text, quoted, _ = self.sent()[0]
        self.assertEqual(quoted, 299)
        self.assertTrue(text.startswith("Hi Bob!"))

    def test_other_messages_ignored(self):
        for i, text in enumerate(["rules please", "/rules", "", "x" * 200]):
            faq.faq_topic_handler(self.bot, 1, _event(_msg(300 + i, text)))
        faq.faq_topic_handler(self.bot, 1, _event(_msg(310, "rules", view_type="Image")))
        faq.faq_topic_handler(self.bot, 1, _event(_msg(311, "rules", chat_id=CHAT + 1)))
        self.bot.rpc.send_msg.assert_not_called()

    def test_same_topic_throttled_except_admin(self):
        faq.faq_topic_handler(self.bot, 1, _event(_msg(300, "rules")))
        faq.faq_topic_handler(self.bot, 1, _event(_msg(301, "rules")))
        faq.faq_topic_handler(self.bot, 1, _event(_msg(302, "links")))
        self.assertEqual(len(self.sent()), 2)
        self.is_admin.return_value = True
        faq.faq_topic_handler(self.bot, 1, _event(_msg(303, "rules")))
        self.assertEqual(len(self.sent()), 3)

    def test_answer_with_file(self):
        stored = os.path.join(state.FAQ_MEDIA_DIR, str(CHAT), "abc", "logo.png")
        os.makedirs(os.path.dirname(stored))
        open(stored, "wb").close()
        database.save_faq_entry(CHAT, "logo", "logo", "", stored, "Image", ALICE)
        faq.faq_topic_handler(self.bot, 1, _event(_msg(300, "logo")))
        msg_data = self.bot.rpc.send_msg.call_args[0][2]
        self.assertEqual((msg_data.file, msg_data.viewtype), (stored, "Image"))

    def test_faq_list_and_single_topic(self):
        faq.faq_command(self.bot, 1, _event(_msg(300, "/faq")))
        self.assertIn("• Links\n• Rules", self.sent()[0][0])
        faq.faq_command(self.bot, 1, _event(_msg(301, "/faq Links")))
        self.assertEqual(self.sent()[1][0], "see site")
        faq.faq_command(self.bot, 1, _event(_msg(302, "/faq nope")))
        self.assertIn("no FAQ topic", self.sent()[2][0])

    def test_remove(self):
        faq.remove_command(self.bot, 1, _event(_msg(300, "/remove links", from_id=BOB)))
        self.assertIsNotNone(database.get_faq_entry(CHAT, "links"))
        self.assertIn("saved by someone else", self.sent()[-1][0])
        faq.remove_command(self.bot, 1, _event(_msg(301, "/remove LINKS", from_id=ALICE)))
        self.assertIsNone(database.get_faq_entry(CHAT, "links"))
        self.assertIn("Removed FAQ topic **Links**", self.sent()[-1][0])


class TestFaqBotPresent(FaqTestBase):
    def setUp(self):
        super().setUp()
        self.add_faq_bot()
        database.save_faq_entry(CHAT, "rules", "Rules", "Be nice", None, None, ALICE)

    def test_silent_on_topics_and_faq(self):
        faq.faq_topic_handler(self.bot, 1, _event(_msg(300, "rules")))
        faq.faq_command(self.bot, 1, _event(_msg(301, "/faq")))
        self.bot.rpc.send_msg.assert_not_called()

    def test_explicit_address_is_answered(self):
        faq.faq_command(self.bot, 1, _event(_msg(300, "/faq@bouncer")))
        self.assertEqual(len(self.sent()), 1)

    def test_shadow_mode_mirrors_save_and_remove(self):
        # Bob may overwrite Alice's topic: we mirror what the FAQ Bot does
        self.save("rules", "Be very nice", from_id=BOB)
        self.assertEqual(database.get_faq_entry(CHAT, "rules")["text"], "Be very nice")
        self.save("new", "fresh", from_id=BOB, msg_id=201, answer_id=101)
        self.assertIsNotNone(database.get_faq_entry(CHAT, "new"))
        faq.remove_command(self.bot, 1, _event(_msg(202, "/remove new", from_id=ALICE)))
        self.assertIsNone(database.get_faq_entry(CHAT, "new"))
        self.bot.rpc.send_msg.assert_not_called()

    def test_takes_over_after_faq_bot_leaves(self):
        faq.faq_topic_handler(self.bot, 1, _event(_msg(300, "rules")))
        self.bot.rpc.send_msg.assert_not_called()
        self.members.remove(FAQ_BOT)
        # Still cached as present until the TTL expires
        faq.faq_topic_handler(self.bot, 1, _event(_msg(301, "rules")))
        self.bot.rpc.send_msg.assert_not_called()
        state._faq_bot_cache[CHAT] = (time.time() - 3600, True)
        faq.faq_topic_handler(self.bot, 1, _event(_msg(302, "rules")))
        self.assertEqual(self.sent()[0][0], "Be nice")


if __name__ == "__main__":
    unittest.main()
