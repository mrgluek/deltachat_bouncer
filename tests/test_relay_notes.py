"""2.56.0: /relays lists members with unreachable relays; relay notes for people whose relay is
down or a public mail provider (relay_notes.py)."""
import contextlib
import os
import random
import sys
import tempfile
import time
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

TEST_DIR = tempfile.TemporaryDirectory(prefix="test_relay_notes_")
TEST_DB = os.path.join(TEST_DIR.name, "bouncer.db")
os.environ["DB_PATH"] = TEST_DB

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
    import dc_helpers
    import relay_notes
    import relaygeo
    import state

SELF_FP = "D58B5E6C55482F94F9F5B84B6FB9FE3AD2658082"
PEER_FP = "DEC0E8D11FC4686F2C715C56F8C9AD699BC19AD8"


def grouped(fp):
    groups = [fp[i:i + 4] for i in range(0, len(fp), 4)]
    return "\n".join(" ".join(groups[i:i + 5]) for i in range(0, len(groups), 5))


def encinfo(relays, name="Alice", addr=None, self_addr="bot@bot.example"):
    """get_contact_encryption_info() text as built by core contact.rs get_encrinfo (core 2.66):
    two fingerprint blocks ("<name> (<addr>):", the bot's own one is "Me (<addr>):"), sorted by
    address, then the literal "Relays:" section with the addresses of the contact's key."""
    addr = addr or (relays[0] if relays else "alice@example.org")
    blocks = sorted([(addr, name, PEER_FP), (self_addr, "Me", SELF_FP)])
    text = "Messages are end-to-end encrypted.\nFingerprints:"
    for a, n, fp in blocks:
        text += f"\n\n{n} ({a}):\n{grouped(fp)}"
    if relays:
        text += "\n\nRelays:" + "".join(f"\n{r}" for r in relays)
    return text


class CountingDB:
    """Counts database connections (reader pool acquisitions and write transactions)."""

    def __init__(self):
        self.reads = 0
        self.writes = 0
        self._connect = database._connect
        self._writer = database._writer_transaction

    def __enter__(self):
        real_connect, real_writer = self._connect, self._writer

        def connect(*a, **kw):
            self.reads += 1
            return real_connect(*a, **kw)

        @contextlib.contextmanager
        def writer():
            self.writes += 1
            with real_writer() as conn:
                yield conn

        database._connect = connect
        database._writer_transaction = writer
        return self

    def __exit__(self, *exc):
        database._connect = self._connect
        database._writer_transaction = self._writer

    @property
    def total(self):
        return self.reads + self.writes


class Base(unittest.TestCase):
    def setUp(self):
        database.close_db()
        database.DB_PATH = TEST_DB
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(TEST_DB + suffix):
                os.remove(TEST_DB + suffix)
        database.init_db()
        with state._monitor_lock:
            state._cmping_server_status.clear()
            state._cmping_server_errors.clear()
        state._chat_relays_anti_spam.clear()
        relay_notes._dm_cache.clear()
        relay_notes._chat_last_note.clear()
        relay_notes._suggest_cache.clear()
        self.sent = []
        self._patches = [
            patch.object(dc_helpers, "_send", side_effect=self._capture),
            patch.object(relay_notes, "SEND_DELAY_SECONDS", 0),
            patch.object(dc_helpers, "_get_bot_domains", return_value=[]),
        ]
        for p in self._patches:
            p.start()
        self.now = time.time()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        with state._monitor_lock:
            state._cmping_server_status.clear()
            state._cmping_server_errors.clear()
        database.close_db()

    def _capture(self, bot, accid, chat_id, text, reply_to_id=None, **kw):
        self.sent.append(SimpleNamespace(chat_id=chat_id, text=text, reply_to_id=reply_to_id))
        return None

    def set_status(self, domain, healthy, error=None):
        state._set_cmping_server_status(domain, healthy, error)

    def down_since(self, domain, seconds_ago, error="connection timed out"):
        self.set_status(domain, False, error)
        database.record_cmping_server_down(domain, int(self.now - seconds_ago), error)

    def add_pings(self, domain, count, avg):
        with database._writer_transaction() as conn:
            conn.executemany(
                "INSERT INTO cmping_history (src, dst, avg, checked_at) VALUES (?, ?, ?, ?)",
                [(domain, "other.example", avg, self.now - i) for i in range(count)])

    def healthy_relay(self, domain, avg, count=20):
        self.set_status(domain, True)
        database.add_cmping_monitor(domain)
        self.add_pings(domain, count, avg)


def make_bot(relays_by_contact):
    bot = MagicMock()
    bot.rpc.get_contact_encryption_info.side_effect = lambda accid, cid: encinfo(relays_by_contact.get(cid, []))
    bot.rpc.get_basic_chat_info.return_value = {"chat_type": "Single"}
    return bot


def message_event(from_id, text="/help", chat_id=500, msg_id=900, command=None):
    msg = SimpleNamespace(from_id=from_id, chat_id=chat_id, id=msg_id, text=text, is_info=False)
    if command is None:
        command = text.split()[0] if text.startswith("/") and "@" not in text.split()[0] else ""
    return SimpleNamespace(msg=msg, command=command, payload="")


# ── Parser ───────────────────────────────────────────────────────────────────

class TestParser(Base):
    def test_realistic_sample_reads_only_the_relays_section(self):
        info = encinfo(["alice@relay1.example", "alice@Relay2.Example"], addr="alice@relay1.example")
        self.assertIn("Me (bot@bot.example):", info)  # our own address is in the text
        self.assertEqual(relay_notes.parse_relay_domains(info), ["relay1.example", "relay2.example"])

    def test_contact_named_relays_and_unicode_line_separators(self):
        real = "\n\nRelays:\nalice@mail.ru\n"
        # a contact named "Relays" puts a second header (and hex lines) above the real section
        self.assertEqual(relay_notes.parse_relay_domains("Relays:\nDEC0 E8D1\n" + real), ["mail.ru"])
        # U+2028 in a name is not a line break: it cannot fake a section
        fake = "Alice\u2028Relays:\u2028x@dead.example\u2028"
        self.assertEqual(relay_notes.parse_relay_domains(fake + real), ["mail.ru"])
        self.assertEqual(relay_notes.parse_relay_domains(fake), [])

    def test_new_core_headers_without_address(self):
        info = ("Messages are end-to-end encrypted.\nFingerprints:\n\nAlice:\n" + grouped(PEER_FP) +
                "\n\nMe:\n" + grouped(SELF_FP) + "\n\nRelays:\nalice@nine.testrun.org\n")
        self.assertEqual(relay_notes.parse_relay_domains(info), ["nine.testrun.org"])

    def test_no_relays_section_gives_nothing(self):
        self.assertEqual(relay_notes.parse_relay_domains(encinfo([], addr="alice@mail.ru")), [])
        self.assertEqual(relay_notes.parse_relay_domains("No encryption."), [])
        self.assertEqual(relay_notes.parse_relay_domains(None), [])
        self.assertEqual(relay_notes.parse_relay_domains(MagicMock()), [])

    def test_section_stops_at_non_address(self):
        info = "Relays:\nalice@good.example\nsomething else entirely\nbob@later.example"
        self.assertEqual(relay_notes.parse_relay_domains(info), ["good.example"])

    def test_garbage_in_section_is_ignored(self):
        self.assertEqual(relay_notes.parse_relay_domains("Relays:\n<script>@x\n"), [])

    def test_domain_of(self):
        self.assertEqual(relay_notes.domain_of(" User@Mail.RU "), "mail.ru")
        self.assertEqual(relay_notes.domain_of("no-at-sign"), "")
        self.assertEqual(relay_notes.domain_of(None), "")


# ── Selection ────────────────────────────────────────────────────────────────

class TestSelection(unittest.TestCase):
    def setUp(self):
        self.rng = random.Random(1)

    def test_excludes_own_unhealthy_and_few_pings(self):
        averages = {"own.example": (10, 50), "down.example": (20, 50), "few.example": (30, 9),
                    "a.example": (100, 10), "b.example": (200, 40)}
        status = {"own.example": True, "down.example": False, "few.example": True,
                  "a.example": True, "b.example": True}
        picked = relay_notes.select_relay_suggestions(averages, status, exclude={"own.example"}, rng=self.rng)
        self.assertEqual(sorted(picked), ["a.example", "b.example"])

    def test_unknown_status_is_not_suggested(self):
        averages = {"a.example": (100, 50)}
        self.assertEqual(relay_notes.select_relay_suggestions(averages, {}, rng=self.rng), [])

    def test_picks_from_the_five_fastest(self):
        averages = {f"r{i}.example": (i * 100 + 50, 50) for i in range(10)}
        status = {d: True for d in averages}
        fastest = {f"r{i}.example" for i in range(5)}
        for seed in range(30):
            picked = relay_notes.select_relay_suggestions(averages, status, rng=random.Random(seed))
            self.assertEqual(len(picked), 2)
            self.assertEqual(len(set(picked)), 2)
            self.assertTrue(set(picked) <= fastest)

    def test_whitelisted_first_for_public_provider_users(self):
        averages = {"fast.example": (50, 50), "fast2.example": (60, 50),
                    "wl1.example": (900, 50), "wl2.example": (950, 50)}
        status = {d: True for d in averages}
        picked = relay_notes.select_relay_suggestions(
            averages, status, whitelisted={"wl1.example", "wl2.example"}, prefer_whitelisted=True, rng=self.rng)
        self.assertEqual(sorted(picked), ["wl1.example", "wl2.example"])
        # One whitelisted relay: the other place goes to the fastest of the rest
        picked = relay_notes.select_relay_suggestions(
            averages, status, whitelisted={"wl1.example"}, prefer_whitelisted=True,
            rng=self.rng, top_n=1)
        self.assertEqual(picked[0], "wl1.example")
        self.assertEqual(picked[1], "fast.example")

    def test_fewer_than_two_candidates(self):
        status = {"a.example": True}
        self.assertEqual(relay_notes.select_relay_suggestions({"a.example": (10, 50)}, status, rng=self.rng),
                         ["a.example"])
        self.assertEqual(relay_notes.select_relay_suggestions({}, status, rng=self.rng), [])


# ── Contact relay cache ──────────────────────────────────────────────────────

class TestCache(Base):
    def test_refresh_at_most_once_a_day(self):
        bot = make_bot({42: ["alice@one.example"]})
        for i in range(5):
            self.assertEqual(relay_notes.contact_domains(bot, 1, 42, now=self.now + i * 3600), ["one.example"])
        self.assertEqual(bot.rpc.get_contact_encryption_info.call_count, 1)
        # A day later the list is read again and the change is picked up
        bot.rpc.get_contact_encryption_info.side_effect = lambda a, c: encinfo(["alice@two.example"])
        self.assertEqual(relay_notes.contact_domains(bot, 1, 42, now=self.now + 25 * 3600), ["two.example"])
        self.assertEqual(bot.rpc.get_contact_encryption_info.call_count, 2)

    def test_unreadable_info_is_retried_after_ten_minutes_not_a_day(self):
        bot = MagicMock()
        bot.rpc.get_contact_encryption_info.side_effect = Exception("no key yet")
        for _ in range(3):
            self.assertEqual(relay_notes.contact_domains(bot, 1, 42, now=self.now), [])
        self.assertEqual(bot.rpc.get_contact_encryption_info.call_count, 1)  # not one RPC per message
        bot.rpc.get_contact_encryption_info.side_effect = lambda accid, cid: encinfo(["alice@yandex.ru"])
        self.assertEqual(relay_notes.contact_domains(bot, 1, 42, now=self.now + 601), ["yandex.ru"])

    def test_transient_rpc_error_keeps_the_known_relays(self):
        bot = make_bot({42: ["alice@one.example", "alice@two.example"]})
        relay_notes.contact_domains(bot, 1, 42, now=self.now)
        bot.rpc.get_contact_encryption_info.side_effect = Exception("timeout")
        got = relay_notes.contact_domains(bot, 1, 42, primary_addr="alice@one.example", now=self.now + 2 * 86400)
        self.assertEqual(got, ["one.example", "two.example"])  # not shrunk to the primary
        # and it is retried soon, not in a day
        bot.rpc.get_contact_encryption_info.side_effect = lambda a, c: encinfo(["alice@three.example"])
        self.assertEqual(relay_notes.contact_domains(bot, 1, 42, now=self.now + 2 * 86400 + 601), ["three.example"])

    def test_only_domains_are_stored(self):
        bot = make_bot({42: ["alice.secret@one.example"]})
        relay_notes.contact_domains(bot, 1, 42, now=self.now)
        with database._reader_connection() as conn:
            rows = conn.execute("SELECT domains FROM contact_relays").fetchall()
        self.assertEqual(rows, [("one.example",)])


# ── Notes ────────────────────────────────────────────────────────────────────

class TestNotes(Base):
    def setUp(self):
        super().setUp()
        self.healthy_relay("good1.example", 100)
        self.healthy_relay("good2.example", 200)

    def test_no_note_when_nothing_is_wrong(self):
        self.set_status("one.example", True)
        bot = make_bot({42: ["alice@one.example"]})
        relay_notes.maybe_note_for_message(bot, 1, message_event(42))
        self.assertEqual(self.sent, [])

    def test_unknown_status_gives_no_note(self):
        bot = make_bot({42: ["alice@unknown.example"]})
        relay_notes.maybe_note_for_message(bot, 1, message_event(42))
        self.assertEqual(self.sent, [])

    def test_threshold_4_hours(self):
        self.assertEqual(relay_notes.MIN_DOWN_SECONDS, 14400)
        bot = make_bot({42: ["alice@one.example"]})
        self.down_since("one.example", 3 * 3600 + 3000)
        relay_notes.maybe_note_for_message(bot, 1, message_event(42))
        self.assertEqual(self.sent, [])
        database.record_cmping_server_up("one.example", int(self.now))
        self.down_since("one.example", 4 * 3600 + 60)
        relay_notes.maybe_note_for_message(bot, 1, message_event(42))
        self.assertEqual(len(self.sent), 1)
        note = self.sent[0]
        self.assertIn("`one.example` has been unreachable for 4h", note.text)
        self.assertIn("https://good1.example/new", note.text)
        self.assertIn("https://good2.example/new", note.text)
        self.assertNotIn("/addtransport", note.text)
        self.assertNotIn("dcaccount:", note.text)
        self.assertEqual(note.chat_id, 500)
        self.assertEqual(note.reply_to_id, 900)

    def test_rate_limit_six_hours_per_contact_and_domain(self):
        bot = make_bot({42: ["alice@one.example"]})
        self.down_since("one.example", 5 * 3600)
        relay_notes.maybe_note_for_message(bot, 1, message_event(42))
        relay_notes.maybe_note_for_message(bot, 1, message_event(42, "hello"))
        self.assertEqual(len(self.sent), 1)
        # 6 hours later: again
        with patch.object(relay_notes.time, "time", return_value=self.now + 6 * 3600 + 1):
            relay_notes.maybe_note_for_message(bot, 1, message_event(42))
        self.assertEqual(len(self.sent), 2)

    def test_public_provider_note(self):
        bot = make_bot({42: ["alice@mail.ru"]})
        relay_notes.maybe_note_for_message(bot, 1, message_event(42))
        self.assertEqual(len(self.sent), 1)
        text = self.sent[0].text
        self.assertIn("`mail.ru` is a public mail provider", text)
        self.assertIn("Add a chatmail relay: https://", text)

    def test_public_provider_prefers_whitelisted(self):
        self.healthy_relay("wl.example", 2000)
        bot = make_bot({42: ["alice@mail.ru"]})
        with patch.object(relaygeo, "whitelisted_many", return_value={"wl.example"}):
            relay_notes.maybe_note_for_message(bot, 1, message_event(42))
        self.assertIn("https://wl.example/new", self.sent[0].text)

    def test_note_without_links_when_no_relay_qualifies(self):
        with state._monitor_lock:
            state._cmping_server_status.clear()
        bot = make_bot({42: ["alice@one.example"]})
        self.down_since("one.example", 5 * 3600)
        relay_notes.maybe_note_for_message(bot, 1, message_event(42))
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("https://", self.sent[0].text)

    def test_plain_group_message_is_ignored_without_any_lookup(self):
        bot = make_bot({42: ["alice@mail.ru"]})
        bot.rpc.get_basic_chat_info.return_value = {"chat_type": "Group"}
        for _ in range(3):
            relay_notes.maybe_note_for_message(bot, 1, message_event(42, "just chatting"))
        self.assertEqual(self.sent, [])
        bot.rpc.get_contact_encryption_info.assert_not_called()
        self.assertEqual(bot.rpc.get_basic_chat_info.call_count, 1)  # chat type cached

    def test_group_command_gets_note(self):
        bot = make_bot({42: ["alice@mail.ru"]})
        bot.rpc.get_basic_chat_info.return_value = {"chat_type": "Group"}
        relay_notes.maybe_note_for_message(bot, 1, message_event(42, "/top"))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(bot.rpc.get_basic_chat_info.call_count, 1)  # the chat type, once per chat

    def test_command_for_another_bot_is_ignored(self):
        bot = make_bot({42: ["alice@mail.ru"]})
        bot.rpc.get_basic_chat_info.return_value = {"chat_type": "Group"}
        relay_notes.maybe_note_for_message(bot, 1, message_event(42, "/help@ntfybot", command=""))
        self.assertEqual(self.sent, [])

    def test_group_text_that_is_not_our_command_is_ignored(self):
        # "/start" meant for another bot, or "/etc/hosts": the parser leaves event.command empty
        bot = make_bot({42: ["alice@mail.ru"]})
        bot.rpc.get_basic_chat_info.return_value = {"chat_type": "Group"}
        for text in ("/start", "/etc/hosts", "/help@otherbot"):
            relay_notes.maybe_note_for_message(bot, 1, message_event(42, text, command=""))
        self.assertEqual(self.sent, [])

    def test_bot_sender_gets_no_note(self):
        bot = make_bot({42: ["alice@mail.ru"]})
        bot.rpc.get_contact.return_value = SimpleNamespace(is_bot=True)
        relay_notes.maybe_note_for_message(bot, 1, message_event(42))
        self.assertEqual(self.sent, [])
        self.assertEqual(database.get_relay_notes_sent(42, {"mail.ru"}), {})  # and nothing is marked

    def test_public_provider_note_repeats_weekly_a_dead_relay_every_six_hours(self):
        bot = make_bot({42: ["alice@mail.ru"], 43: ["bob@one.example"]})
        self.down_since("one.example", 5 * 3600)
        relay_notes.maybe_note_for_message(bot, 1, message_event(42))
        relay_notes.maybe_note_for_message(bot, 1, message_event(43))
        self.assertEqual(len(self.sent), 2)
        with patch.object(relay_notes.time, "time", return_value=self.now + 2 * 86400):
            relay_notes.maybe_note_for_message(bot, 1, message_event(42))
            relay_notes.maybe_note_for_message(bot, 1, message_event(43))
        self.assertEqual(len(self.sent), 3)  # the dead relay again, mail.ru not yet
        with patch.object(relay_notes.time, "time", return_value=self.now + 7 * 86400 + 1):
            relay_notes.maybe_note_for_message(bot, 1, message_event(42))
        self.assertEqual(len(self.sent), 4)

    def test_failing_send_is_logged_not_raised(self):
        bot = make_bot({42: ["alice@mail.ru"]})
        with patch.object(relay_notes.dc_helpers, "_send", side_effect=Exception("boom")):
            relay_notes._send_later(bot, 1, 500, "text")  # delay is 0 in tests: sends inline

    def test_concurrent_messages_send_one_note(self):
        bot = make_bot({42: ["alice@mail.ru"]})
        threads = [threading.Thread(target=relay_notes.maybe_note_for_message,
                                    args=(bot, 1, message_event(42))) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.sent), 1)

    def test_group_gets_one_note_per_five_minutes(self):
        bot = make_bot({42: ["a@mail.ru"], 43: ["b@mail.ru"], 44: ["c@mail.ru"]})
        bot.rpc.get_basic_chat_info.return_value = {"chat_type": "Group"}
        relay_notes.maybe_note_for_message(bot, 1, message_event(42, "/top"))
        relay_notes.maybe_note_for_message(bot, 1, message_event(43, "/top"))
        self.assertEqual(len(self.sent), 1)
        # 43 was not marked: after the cooldown the note comes
        with patch.object(relay_notes.time, "time", return_value=self.now + 301):
            relay_notes.maybe_note_for_message(bot, 1, message_event(43, "/top"))
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[1].reply_to_id, 900)

    def test_public_provider_without_a_relay_to_suggest_burns_no_allowance(self):
        with state._monitor_lock:
            state._cmping_server_status.clear()  # monitor not warmed up (after a restart)
        bot = make_bot({42: ["alice@mail.ru"]})
        relay_notes.maybe_note_for_message(bot, 1, message_event(42))
        self.assertEqual(self.sent, [])
        self.healthy_relay("good1.example", 100)
        self.healthy_relay("good2.example", 120)
        relay_notes._suggest_cache.clear()
        relay_notes.maybe_note_for_message(bot, 1, message_event(42, "again"))
        self.assertEqual(len(self.sent), 1)

    def test_suggestion_ranking_is_cached_between_notes(self):
        bot = make_bot({i: [f"u{i}@mail.ru"] for i in range(20, 30)})
        with CountingDB() as db_first:
            relay_notes.maybe_note_for_message(bot, 1, message_event(20))
        with CountingDB() as db_second:
            relay_notes.maybe_note_for_message(bot, 1, message_event(21))
        self.assertEqual(len(self.sent), 2)
        self.assertLess(db_second.total, db_first.total)

    def test_special_and_info_messages_are_ignored(self):
        bot = make_bot({2: ["x@mail.ru"]})
        relay_notes.maybe_note_for_message(bot, 1, message_event(2))
        ev = message_event(42)
        ev.msg.is_info = True
        relay_notes.maybe_note_for_message(bot, 1, ev)
        self.assertEqual(self.sent, [])
        bot.rpc.get_contact_encryption_info.assert_not_called()


# ── Welcome ──────────────────────────────────────────────────────────────────

class TestWelcome(Base):
    def _join(self, bot, contact_id):
        import handlers
        msg = SimpleNamespace(chat_id=100, from_id=12, text="Member added",
                              system_message_type=handlers.SystemMessageType.MEMBER_ADDED_TO_GROUP,
                              info_contact_id=contact_id, infoContactId=None)
        with patch.object(dc_helpers, "get_user_chat_count", return_value=1):
            handlers.handle_dc_info_message(bot, 1, SimpleNamespace(msg=msg))

    def _bot(self, relays):
        bot = make_bot({55: relays})
        bot.rpc.get_chat_contacts.return_value = [1, 12, 55]
        bot.rpc.get_contact.return_value = SimpleNamespace(
            name="Alice", display_name="Alice", address=relays[0] if relays else "alice@x.example")
        return bot

    def test_welcome_is_followed_by_note(self):
        database.add_catalog_chat(100, "Test", "", 2)
        database.update_catalog_chat_welcome(100, 1, None)
        self.healthy_relay("good1.example", 100)
        self.down_since("one.example", 5 * 3600)
        self._join(self._bot(["alice@one.example"]), 55)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("welcome to Test group", self.sent[0].text)
        self.assertIn("**Alice**, your relay `one.example` has been unreachable for 5h", self.sent[1].text)
        self.assertIn("https://good1.example/new", self.sent[1].text)

    def test_welcome_alone_when_nothing_is_wrong(self):
        database.add_catalog_chat(100, "Test", "", 2)
        database.update_catalog_chat_welcome(100, 1, None)
        self.set_status("one.example", True)
        self._join(self._bot(["alice@one.example"]), 55)
        self.assertEqual(len(self.sent), 1)

    def test_no_note_when_welcome_is_off(self):
        database.add_catalog_chat(100, "Test", "", 2)
        bot = self._bot(["alice@mail.ru"])
        self._join(bot, 55)
        self.assertEqual(self.sent, [])
        bot.rpc.get_contact_encryption_info.assert_not_called()


# ── /relays ──────────────────────────────────────────────────────────────────

def relays_bot(members):
    """members: {contact_id: (name, primary address, relay addresses)}"""
    bot = MagicMock()
    bot.rpc.get_chat_contacts.return_value = [1] + list(members)
    bot.rpc.get_contact.side_effect = lambda accid, cid: SimpleNamespace(
        name=members[cid][0], display_name=members[cid][0], address=members[cid][1], last_seen=0)
    bot.rpc.get_contact_encryption_info.side_effect = lambda accid, cid: encinfo(
        members[cid][2], name=members[cid][0], addr=members[cid][1])
    return bot


def run_relays(bot):
    event = SimpleNamespace(msg=SimpleNamespace(from_id=12, chat_id=300, id=1, text="/relays"), payload="")
    with patch.object(dc_helpers, "_is_dc_admin", return_value=True):
        commands.relays_command(bot, 1, event)


class TestRelaysCommand(Base):
    def test_two_blocks_and_unknown_status_skipped(self):
        self.down_since("dead.example", 5 * 3600, "IMAP login failed: connection refused " + "x" * 200)
        self.set_status("fine.example", True)
        bot = relays_bot({
            20: ("Dora", "dora@fine.example", ["dora@fine.example", "dora@dead.example"]),
            21: ("Ivan", "ivan@mail.ru", ["ivan@mail.ru"]),
            22: ("Una", "una@unknown.example", ["una@unknown.example"]),
            23: ("Finn", "finn@fine.example", ["finn@fine.example"]),
        })
        run_relays(bot)
        self.assertEqual(len(self.sent), 1)
        text = self.sent[0].text
        self.assertIn("🔴 **Unreachable relays (1):**", text)
        self.assertIn("• `dead.example`, down 5h — IMAP login failed", text)
        self.assertIn("/contact20 **Dora**", text)
        self.assertNotIn("x" * 100, text)  # reason trimmed
        self.assertNotIn("Una", text)
        self.assertNotIn("Finn", text)
        self.assertIn("Members with public Russian mail providers (1)", text)
        self.assertIn("/contact21 **Ivan**", text)
        self.assertLess(text.index("Unreachable relays"), text.index("public Russian mail providers"))
        # The fetched relays are kept for the relay notes, without a second RPC
        self.assertEqual(database.get_contact_relays(20)[0], ["dead.example", "fine.example"])

    def test_flaky_or_blamed_servers_are_not_unreachable(self):
        # Marked unhealthy by one pair with lost pings (down 44 min), or by a dead partner (no downtime
        # event at all): neither is "unreachable"; only the relay down for hours is
        self.down_since("flaky.example", 44 * 60, "incoming from x failed: 2 of 5 pings lost")
        self.set_status("blamed.example", False)
        self.down_since("dead.example", 3 * 86400 + 20 * 3600, "both directions failed: 5 of 5 pings lost")
        bot = relays_bot({
            20: ("Dora", "dora@dead.example", ["dora@dead.example", "dora@flaky.example"]),
            21: ("Bo", "bo@blamed.example", ["bo@blamed.example", "bo@dead.example"]),
        })
        run_relays(bot)
        text = self.sent[0].text
        self.assertIn("Unreachable relays (1)", text)
        self.assertIn("`dead.example`, down 3d 20h", text)
        self.assertIn("/contact20 **Dora**, /contact21 **Bo**", text)
        self.assertNotIn("flaky.example", text)
        self.assertNotIn("blamed.example", text)

    def test_many_dead_relays_are_capped(self):
        relays = [f"dead{i}.example" for i in range(relay_notes.LIST_RELAYS + 3)]
        for r in relays:
            self.down_since(r, 5 * 3600)
        bot = relays_bot({i: (f"P{i}", f"p{i}@{r}", [f"p{i}@{r}"]) for i, r in enumerate(relays, 20)})
        run_relays(bot)
        text = self.sent[0].text
        self.assertIn(f"Unreachable relays ({len(relays)})", text)
        self.assertIn("…and 3 more relays", text)

    def test_only_russian_block(self):
        bot = relays_bot({21: ("Ivan", "ivan@yandex.ru", ["ivan@yandex.ru"])})
        run_relays(bot)
        text = self.sent[0].text
        self.assertNotIn("Unreachable", text)
        self.assertTrue(text.startswith("⚠️ **Members with public Russian mail providers (1):**"))

    def test_nothing_found(self):
        self.set_status("fine.example", True)
        bot = relays_bot({23: ("Finn", "finn@fine.example", ["finn@fine.example"])})
        run_relays(bot)
        self.assertTrue(self.sent[0].text.startswith("✅"))

    def test_cooldown_is_kept(self):
        bot = relays_bot({23: ("Finn", "finn@fine.example", ["finn@fine.example"])})
        event = SimpleNamespace(msg=SimpleNamespace(from_id=12, chat_id=300, id=1, text="/relays"), payload="")
        with patch.object(dc_helpers, "_is_dc_admin", return_value=False), \
                patch.object(dc_helpers, "_queue_delayed_command") as delayed:
            commands.relays_command(bot, 1, event)
            commands.relays_command(bot, 1, event)
        self.assertEqual(len(self.sent), 1)
        delayed.assert_called_once()


# ── Scale: bounds on queries and RPC calls ───────────────────────────────────

class TestScale(Base):
    CONTACTS = 1000
    SERVERS = 150

    def setUp(self):
        super().setUp()
        self.servers = [f"relay{i}.example" for i in range(self.SERVERS)]
        with database._writer_transaction() as conn:
            conn.executemany("INSERT INTO cmping_monitors (domain, added_at) VALUES (?, ?)",
                             [(d, self.now) for d in self.servers])
            conn.executemany(
                "INSERT INTO cmping_history (src, dst, avg, checked_at) VALUES (?, ?, ?, ?)",
                [(d, self.servers[(i + 1) % self.SERVERS], 100 + i, self.now - k)
                 for i, d in enumerate(self.servers) for k in range(12)])
        for i, d in enumerate(self.servers):
            # every tenth server is down for five hours, the rest are healthy
            if i % 10 == 0:
                self.down_since(d, 5 * 3600)
            else:
                self.set_status(d, True)
        self.relays = {20 + n: [f"user{n}@{self.servers[n % self.SERVERS]}"] for n in range(self.CONTACTS)}

    def test_one_message_costs_a_bounded_number_of_queries(self):
        bot = make_bot(self.relays)
        # A contact whose relay is fine: one cache read, then (first time) one RPC and one write
        with CountingDB() as db:
            relay_notes.maybe_note_for_message(bot, 1, message_event(21))
        self.assertLessEqual(db.total, 2)
        self.assertEqual(bot.rpc.get_contact_encryption_info.call_count, 1)
        with CountingDB() as db:
            relay_notes.maybe_note_for_message(bot, 1, message_event(21))
        self.assertEqual(db.total, 1)
        self.assertEqual(bot.rpc.get_contact_encryption_info.call_count, 1)

    def test_note_with_150_servers_is_bounded(self):
        bot = make_bot(self.relays)
        with CountingDB() as db:
            relay_notes.maybe_note_for_message(bot, 1, message_event(20))  # relay0 is down
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0].text.count("/new"), 2)
        # cache read + write, downtime, rate limit read + write, monitor list, batched pings
        self.assertLessEqual(db.total, 8)
        self.assertEqual(bot.rpc.get_contact_encryption_info.call_count, 1)

    def test_suggestions_for_public_provider_with_150_servers_is_bounded(self):
        bot = make_bot({20: ["ivan@mail.ru"]})
        with CountingDB() as db:
            relay_notes.maybe_note_for_message(bot, 1, message_event(20))
        self.assertEqual(len(self.sent), 1)
        # + one whitelist switch read and at most one relay country cache load
        self.assertLessEqual(db.total, 9)

    def test_1000_messages_from_1000_contacts(self):
        bot = make_bot(self.relays)
        with CountingDB() as db:
            for cid in self.relays:
                relay_notes.maybe_note_for_message(bot, 1, message_event(cid))
        self.assertEqual(bot.rpc.get_contact_encryption_info.call_count, self.CONTACTS)
        notes = len(self.sent)
        self.assertEqual(notes, self.CONTACTS // 10)
        # 2 per quiet contact; at most 8 for a contact that gets a note
        self.assertLessEqual(db.total, 2 * self.CONTACTS + 6 * notes)
        # The same messages again: all from the cache, no RPC, notes rate limited
        bot.rpc.reset_mock()
        with CountingDB() as db:
            for cid in self.relays:
                relay_notes.maybe_note_for_message(bot, 1, message_event(cid))
        bot.rpc.get_contact_encryption_info.assert_not_called()
        self.assertEqual(len(self.sent), notes)
        self.assertLessEqual(db.total, self.CONTACTS + 2 * notes)

    def test_relays_command_over_1000_members(self):
        members = {cid: (f"User {cid}", addrs[0], addrs) for cid, addrs in self.relays.items()}
        bot = relays_bot(members)
        with CountingDB() as db:
            run_relays(bot)
        text = self.sent[0].text
        self.assertIn(f"Unreachable relays ({self.SERVERS // 10})", text)  # one entry per relay, not per member
        self.assertLessEqual(text.count("/contact"), relay_notes.LIST_RELAYS * relay_notes.LIST_NAMES)
        # Constant, not per member: one batched cache write, one downtime query (+ snapshot-free state)
        self.assertLessEqual(db.total, 3)
        # One encryption info RPC per member, as before 2.56.0: nothing extra
        self.assertEqual(bot.rpc.get_contact_encryption_info.call_count, self.CONTACTS)
        self.assertEqual(bot.rpc.get_contact.call_count, self.CONTACTS)


if __name__ == "__main__":
    unittest.main()
