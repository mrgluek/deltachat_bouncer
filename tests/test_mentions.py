import os
import sys
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
USERNAMEBOT = 24


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

    def create_chat_by_contact_id(self, accid, contact_id):
        return 5000 + contact_id

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


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


DOESNM = (
    '{"username": "doesnm", "type": "contact", "display_name": "doesnm", "url": "https://deltachat.id/doesnm",'
    ' "invite_link": "https://i.delta.chat/#1A77AD6C&v=3&i=x&s=y&a=611w4j9vb%40cm.dc09.xyz&n=doesnm"}'
)


class TestUsernameLookup(unittest.TestCase):
    def setUp(self):
        state._mention_anti_spam.clear()
        mentions._username_cache.clear()
        self.messages = {101: msg(101, GLUEK, "hi")}
        self.rpc = FakeRpc(self.messages)
        self.rpc.contacts[USERNAMEBOT] = contact("Username Bot", "usernamebot@chatmail.uk")
        self.bot = SimpleNamespace(rpc=self.rpc)

    def send(self, text, msg_id=200, from_id=ALICE):
        m = msg(msg_id, from_id, text)
        self.messages[msg_id] = m
        return mentions.handle_mention(self.bot, 1, m)

    @patch("mentions.urllib.request.urlopen")
    def test_unknown_member_found_via_username_bot(self, urlopen):
        urlopen.return_value = FakeResponse(DOESNM)
        self.assertTrue(self.send("@doesnm посмотри?"))
        self.assertEqual(urlopen.call_args[0][0].full_url, "https://deltachat.id/api/username/doesnm")
        # Only the author is told, in private; nothing goes to the group
        self.assertEqual(len(self.rpc.sent), 1)
        chat_id, data = self.rpc.sent[0]
        self.assertEqual(chat_id, 5000 + ALICE)
        # The i.delta.chat invite comes first (opens in the app), then the short link
        self.assertIn("\nhttps://i.delta.chat/#1A77AD6C&v=3", data.text)
        self.assertIn("Short link: https://deltachat.id/doesnm", data.text)
        self.assertLess(data.text.index("i.delta.chat"), data.text.index("deltachat.id/doesnm"))
        self.assertIn("not in this chat", data.text)
        self.assertIsNone(data.quoted_message_id)

    @patch("mentions.urllib.request.urlopen")
    def test_short_link_only_without_trusted_invite(self, urlopen):
        # Older Username Bot (no invite_link) or a link to some other site: only the short link
        for extra in ('', ', "invite_link": "https://evil.example/#1A77"'):
            mentions._username_cache.clear()
            state._mention_anti_spam.clear()
            self.rpc.sent.clear()
            urlopen.return_value = FakeResponse(
                '{"username": "doesnm", "type": "contact", "display_name": "doesnm", "url": "https://deltachat.id/doesnm"' + extra + '}'
            )
            self.assertTrue(self.send("@doesnm hi"))
            text = self.rpc.sent[0][1].text
            self.assertIn("\nhttps://deltachat.id/doesnm", text)
            self.assertNotIn("Short link", text)
            self.assertNotIn("evil", text)

    @patch("mentions.urllib.request.urlopen")
    def test_group_username(self, urlopen):
        urlopen.return_value = FakeResponse(
            '{"username": "dcfans", "type": "group", "display_name": "DC Fans", "url": "https://deltachat.id/dcfans"}'
        )
        self.assertTrue(self.send("@dcfans hi"))
        self.assertIn("group **DC Fans** (@dcfans)", self.rpc.sent[0][1].text)

    @patch("mentions.urllib.request.urlopen")
    def test_not_registered_is_silent_and_cached(self, urlopen):
        urlopen.side_effect = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        self.assertFalse(self.send("@nobody hi"))
        state._mention_anti_spam.clear()
        self.assertFalse(self.send("@nobody hi", msg_id=201))
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(self.rpc.sent, [])

    @patch("mentions.urllib.request.urlopen")
    def test_errors_are_not_cached(self, urlopen):
        urlopen.side_effect = urllib.error.HTTPError("u", 429, "Too Many", {}, None)
        self.assertFalse(self.send("@doesnm hi"))
        urlopen.side_effect = None
        urlopen.return_value = FakeResponse(DOESNM)
        state._mention_anti_spam.clear()
        self.assertTrue(self.send("@doesnm hi", msg_id=201))

    @patch("mentions.urllib.request.urlopen")
    def test_needs_username_bot_in_chat(self, urlopen):
        del self.rpc.contacts[USERNAMEBOT]
        # A look-alike display name is not enough
        self.rpc.contacts[USERNAMEBOT] = contact("Username Bot", "fake@relay.example")
        self.assertFalse(self.send("@doesnm hi"))
        urlopen.assert_not_called()

    @patch("mentions.urllib.request.urlopen")
    def test_members_and_bad_nicks_are_not_looked_up(self, urlopen):
        urlopen.return_value = FakeResponse(DOESNM)
        self.assertTrue(self.send("@Gluek hi"))  # a member: normal reply in the group
        self.assertEqual(self.rpc.sent[0][0], CHAT)
        state._mention_anti_spam.clear()
        self.assertFalse(self.send("@all hi", msg_id=201))
        self.assertFalse(self.send("@ab hi", msg_id=202))
        self.assertFalse(self.send("@user@relay.example hi", msg_id=203))
        self.assertFalse(self.send("@Alice hi", msg_id=204))  # yourself
        urlopen.assert_not_called()

    @patch("mentions.urllib.request.urlopen")
    def test_lookup_off_without_url(self, urlopen):
        with patch.object(config, "USERNAME_LOOKUP_URL", ""):
            self.assertFalse(self.send("@doesnm hi"))
        urlopen.assert_not_called()

    @patch("mentions.urllib.request.urlopen")
    def test_lookup_respects_cooldown(self, urlopen):
        urlopen.return_value = FakeResponse(DOESNM)
        self.assertTrue(self.send("@doesnm hi"))
        self.assertFalse(self.send("@doesnm again", msg_id=201))
        self.assertEqual(len(self.rpc.sent), 1)


if __name__ == "__main__":
    unittest.main()
