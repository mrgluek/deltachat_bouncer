import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

# Mock deltachat2 if not installed
try:
    import deltachat2  # noqa: F401
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
    sys.modules['deltabot_cli'] = mock_deltabot_cli

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import mentions
import state

CHAT = 10
ALICE = 20   # sender
GLUEK = 21
GLEB = 22
BOB = 23


def contact(name, address, display_name=None):
    return SimpleNamespace(name=name, display_name=display_name or name, address=address)


class FakeRpc:
    def __init__(self, messages, chat_type="Group"):
        self.contacts = {
            ALICE: contact("Alice", "alice@relay.example"),
            GLUEK: contact("Gluek", "mrgluek@relay.example"),
            GLEB: contact("Gleb Ivanov", "gleb@relay.example"),
            BOB: contact("Bob", "bob@relay.example"),
            config.DC_CONTACT_ID_SELF: contact("Bouncer", "bot@relay.example"),
        }
        self.messages = messages  # msg_id -> SimpleNamespace(from_id, is_info)
        self.chat_type = chat_type
        self.sent = []

    def get_chat_contacts(self, accid, chat_id):
        return list(self.contacts)

    def get_contact(self, accid, contact_id):
        return self.contacts[contact_id]

    def get_message_ids(self, accid, chat_id, info_only, add_daymarker):
        return sorted(self.messages)

    def get_message(self, accid, msg_id):
        return self.messages[msg_id]

    def get_basic_chat_info(self, accid, chat_id):
        return {"chat_type": self.chat_type}

    def send_msg(self, accid, chat_id, data):
        self.sent.append((chat_id, data))
        return 999


def msg(msg_id, from_id, text="", quote=None, is_info=False):
    return SimpleNamespace(id=msg_id, chat_id=CHAT, from_id=from_id, text=text, quote=quote, is_info=is_info)


class TestParseMention(unittest.TestCase):
    def test_parses_leading_nick(self):
        self.assertEqual(mentions.parse_mention("@Gluek посмотри?"), "Gluek")
        self.assertEqual(mentions.parse_mention("@Gluek, посмотри"), "Gluek")
        self.assertEqual(mentions.parse_mention("@Gluek: глянь"), "Gluek")
        self.assertEqual(mentions.parse_mention("  @Gluek"), "Gluek")
        self.assertEqual(mentions.parse_mention("@mrgluek@relay.example hi"), "mrgluek@relay.example")

    def test_ignores_other_text(self):
        self.assertIsNone(mentions.parse_mention("hi @Gluek"))
        self.assertIsNone(mentions.parse_mention("@ Gluek"))
        self.assertIsNone(mentions.parse_mention(""))
        self.assertIsNone(mentions.parse_mention(None))


class TestHandleMention(unittest.TestCase):
    def setUp(self):
        state._mention_anti_spam.clear()
        self.messages = {
            100: msg(100, GLUEK, "old"),
            101: msg(101, GLUEK, "latest from Gluek"),
            102: msg(102, BOB, "bob says"),
            103: msg(103, GLEB, "gleb says"),
            104: msg(104, 2, "Member added", is_info=True),
        }
        self.rpc = FakeRpc(self.messages)
        self.bot = SimpleNamespace(rpc=self.rpc)

    def send(self, text, quote=None, from_id=ALICE, msg_id=200):
        m = msg(msg_id, from_id, text, quote=quote)
        self.messages[msg_id] = m
        return mentions.handle_mention(self.bot, 1, m)

    def test_replies_to_last_message_of_mentioned_user(self):
        self.assertTrue(self.send("@Gluek посмотри?"))
        self.assertEqual(len(self.rpc.sent), 1)
        chat_id, data = self.rpc.sent[0]
        self.assertEqual(chat_id, CHAT)
        self.assertEqual(data.text, "@Gluek посмотри?")
        self.assertEqual(data.quoted_message_id, 101)
        self.assertEqual(data.override_sender_name, "Alice")

    def test_case_insensitive_and_address(self):
        self.assertTrue(self.send("@gluek look"))
        state._mention_anti_spam.clear()
        self.assertTrue(self.send("@mrgluek look", msg_id=201))
        self.assertEqual([d.quoted_message_id for _, d in self.rpc.sent], [101, 101])

    def test_unique_prefix_and_first_name(self):
        self.assertTrue(self.send("@Gleb hi"))
        self.assertEqual(self.rpc.sent[0][1].quoted_message_id, 103)

    def test_short_prefix_is_ignored(self):
        # "Gl" fits both Gluek and Gleb, and is shorter than the prefix minimum anyway
        self.assertFalse(self.send("@Gl hi"))
        # "Gle" fits only Gleb
        self.assertTrue(self.send("@Gle hi", msg_id=201))
        self.assertEqual(self.rpc.sent[0][1].quoted_message_id, 103)

    def test_ambiguous_between_two_members(self):
        self.rpc.contacts[GLEB] = contact("Gleb Ivanov", "givanov@relay.example")
        self.rpc.contacts[BOB] = contact("Gleb Petrov", "bob@relay.example")
        self.assertFalse(self.send("@Gleb hi"))
        self.assertEqual(self.rpc.sent, [])

    def test_unknown_nick_and_self_mention(self):
        self.assertFalse(self.send("@Nobody hi"))
        self.assertFalse(self.send("@Alice hi", msg_id=201))
        self.assertFalse(self.send("@Bouncer hi", msg_id=202))
        self.assertEqual(self.rpc.sent, [])

    def test_no_message_from_target(self):
        self.messages.pop(103)
        self.assertFalse(self.send("@Gleb hi"))

    def test_skips_when_already_replying_to_target(self):
        self.assertFalse(self.send("@Gluek look", quote={"message_id": 100}))
        self.assertTrue(self.send("@Gluek look", quote={"message_id": 102}, msg_id=201))

    def test_only_in_groups(self):
        self.rpc.chat_type = "Single"
        self.assertFalse(self.send("@Gluek hi"))
        self.rpc.chat_type = "OutBroadcast"
        self.assertFalse(self.send("@Gluek hi", msg_id=201))

    def test_ignores_bot_and_system_senders(self):
        self.assertFalse(self.send("@Gluek hi", from_id=config.DC_CONTACT_ID_SELF))
        self.assertFalse(self.send("@Gluek hi", from_id=2, msg_id=201))

    def test_cooldown_per_sender(self):
        self.assertTrue(self.send("@Gluek one"))
        self.assertFalse(self.send("@Gluek two", msg_id=201))
        self.assertTrue(self.send("@Gluek from bob", from_id=BOB, msg_id=202))
        self.assertEqual(len(self.rpc.sent), 2)


if __name__ == "__main__":
    unittest.main()
