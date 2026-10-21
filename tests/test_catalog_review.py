import os
import shutil
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

TEST_DB = "test_catalog_review.db"
os.environ["DB_PATH"] = TEST_DB

# Mock packages if not installed
try:
    import deltachat2
except ImportError:
    mock_deltachat2 = MagicMock()
    class MsgData:
        def __init__(self, text="", file="", override_sender_name=None):
            self.text = text
            self.file = file
            self.override_sender_name = override_sender_name
    mock_deltachat2.MsgData = MsgData
    class SystemMessageType:
        MEMBER_ADDED_TO_GROUP = 1
        MEMBER_REMOVED_FROM_GROUP = 2
        GROUP_NAME_CHANGED = 3
        GROUP_IMAGE_CHANGED = 4
    mock_deltachat2.SystemMessageType = SystemMessageType
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
import bot  # noqa: F401  (imports every module the way the bot does)
import catalog_review
import dc_helpers
import channels
import config
import handlers
import state
import web.templates.landing as tpl_landing

DAY = 24 * 3600
USER = 42
CHAT = 500


class _SyncThread:
    """Runs a background worker inline so the test sees its result."""
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


def _known_for(contact_id, days):
    database.ensure_contact_first_seen(contact_id, time.time() - days * DAY)


def _msg(text="", chat_id=CHAT, from_id=USER):
    return SimpleNamespace(text=text, chat_id=chat_id, from_id=from_id, id=1, is_info=False)


class CatalogReviewTestBase(unittest.TestCase):
    def setUp(self):
        database.close_db()
        database.DB_PATH = TEST_DB
        database.init_db()
        state.CHANNEL_MEDIA_DIR = "test_catalog_media"
        self.bot = MagicMock()
        self.bot.rpc.get_contact.return_value = SimpleNamespace(
            address="user@example.org", display_name="User", name="User")
        self.sent = []
        self.admin_msgs = []
        self.patches = [
            patch.dict(os.environ, {"VIRUSTOTAL_API_KEY": ""}),
            patch("dc_helpers._send", side_effect=lambda b, a, chat_id, text, *x, **k: self.sent.append((chat_id, text))),
            patch("catalog_review._notify_admin", side_effect=lambda b, a, text: self.admin_msgs.append(text)),
            patch("catalog_review.threading.Thread", _SyncThread),
            patch("dc_helpers._is_dc_admin", return_value=False),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        database.close_db()
        for path in (TEST_DB, TEST_DB + "-wal", TEST_DB + "-shm"):
            if os.path.exists(path):
                os.remove(path)
        shutil.rmtree("test_catalog_media", ignore_errors=True)

    def texts(self, chat_id=None):
        return [t for c, t in self.sent if chat_id is None or c == chat_id]


class TestEligibility(CatalogReviewTestBase):
    def test_new_contact_is_refused(self):
        _known_for(USER, 1)
        err = catalog_review.check_eligibility(self.bot, 1, USER, "user@example.org")
        self.assertIn("at least 3 days", err)

    def test_unknown_contact_counts_as_new(self):
        self.assertIsNotNone(catalog_review.check_eligibility(self.bot, 1, 99, "new@example.org"))
        self.assertIsNotNone(database.get_contact_first_seen(99))

    def test_old_contact_is_allowed(self):
        _known_for(USER, 5)
        self.assertIsNone(catalog_review.check_eligibility(self.bot, 1, USER, "user@example.org"))

    def test_banned_contact_is_refused(self):
        _known_for(USER, 100)
        database.ban_catalog_submitter("User@Example.org")
        self.assertIn("cannot submit", catalog_review.check_eligibility(self.bot, 1, USER, "user@example.org"))
        database.unban_catalog_submitter("user@example.org")
        self.assertIsNone(catalog_review.check_eligibility(self.bot, 1, USER, "user@example.org"))

    def test_pending_and_weekly_limits(self):
        _known_for(USER, 10)
        for i in range(config.CATALOG_MAX_PENDING_PER_USER):
            database.log_catalog_submission("chat", i, i, USER, "user@example.org", f"c{i}")
        self.assertIn("waiting for review", catalog_review.check_eligibility(self.bot, 1, USER, "user@example.org"))
        for i in range(config.CATALOG_MAX_PENDING_PER_USER):
            database.decide_catalog_submission("chat", i, "approved")
        self.assertIsNone(catalog_review.check_eligibility(self.bot, 1, USER, "user@example.org"))
        for i in range(config.CATALOG_MAX_PER_WEEK):
            database.log_catalog_submission("chat", 100 + i, 0, USER, "user@example.org", "x", status="approved")
        self.assertIn("per week", catalog_review.check_eligibility(self.bot, 1, USER, "user@example.org"))

    def test_total_queue_limit(self):
        _known_for(USER, 10)
        with patch.object(config, "CATALOG_MAX_PENDING_TOTAL", 1):
            database.log_catalog_submission("chat", 1, 1, 7, "other@example.org", "c")
            self.assertIn("queue is full", catalog_review.check_eligibility(self.bot, 1, USER, "user@example.org"))

    def test_stopwords_and_urls(self):
        self.assertEqual(catalog_review.find_stopwords("Free USDT AirDrop"), ["airdrop", "usdt"])
        self.assertEqual(catalog_review.extract_urls("see https://a.example/x. and https://a.example/x"),
                         ["https://a.example/x"])

    def test_auto_approve_rule(self):
        self.assertTrue(catalog_review.should_auto_approve(31, []))
        self.assertFalse(catalog_review.should_auto_approve(29, []))
        self.assertFalse(catalog_review.should_auto_approve(31, ["🚩 Stop words: usdt"]))
        with patch.object(config, "CATALOG_AUTO_APPROVE_DAYS", 0):
            self.assertFalse(catalog_review.should_auto_approve(1000, []))

    def test_submissions_switch(self):
        database.set_config("catalog_submissions", "off")
        self.assertFalse(catalog_review.submissions_enabled())
        database.set_config("catalog_submissions", "on")
        self.assertTrue(catalog_review.submissions_enabled())


class TestChatSubmissions(CatalogReviewTestBase):
    def _chatadd(self, payload="Friendly group"):
        self.bot.rpc.get_basic_chat_info.return_value = {"chat_type": "Group", "name": "Hikers"}
        self.bot.rpc.get_chat_contacts.return_value = [1, USER, 43]
        event = SimpleNamespace(msg=_msg("/chatadd " + payload), payload=payload)
        channels.chatadd_command(self.bot, 1, event)

    def test_young_submitter_creates_nothing(self):
        _known_for(USER, 1)
        self._chatadd()
        self.assertIsNone(database.get_catalog_chat_by_chat_id(CHAT))
        self.assertIn("at least 3 days", self.texts(CHAT)[-1])

    def test_pending_submission_is_hidden(self):
        _known_for(USER, 5)
        self._chatadd()
        item = database.get_catalog_chat_by_chat_id(CHAT)
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["is_private"], 1)
        self.assertEqual(item["submitted_by"], USER)
        self.assertEqual(database.get_all_catalog_chats(approved_only=True), [])
        self.assertIn("waits for review", self.texts(CHAT)[-1])
        self.assertIn(f"/chatapprove{item['id']}", self.admin_msgs[-1])

        # /chatN does not hand out an invite while pending
        self.sent.clear()
        handlers.handle_all_messages(self.bot, 1, SimpleNamespace(msg=_msg(f"/chat{item['id']}", chat_id=9)))
        self.assertIn("not found", self.texts(9)[-1])
        self.bot.rpc.get_chat_securejoin_qr_code.assert_not_called()

    def test_old_submitter_is_auto_approved(self):
        _known_for(USER, 40)
        self._chatadd()
        item = database.get_catalog_chat_by_chat_id(CHAT)
        self.assertEqual(item["status"], "approved")
        self.assertEqual(len(database.get_all_catalog_chats(approved_only=True)), 1)
        self.assertIn("Auto-approved", self.admin_msgs[-1])
        self.assertEqual(database.get_latest_catalog_submission("chat", item["id"])["status"], "auto")

    def test_stopword_blocks_auto_approval(self):
        _known_for(USER, 40)
        self._chatadd("Daily USDT airdrop")
        item = database.get_catalog_chat_by_chat_id(CHAT)
        self.assertEqual(item["status"], "pending")
        self.assertIn("Stop words", self.admin_msgs[-1])

    def test_admin_approve_and_reject(self):
        _known_for(USER, 5)
        self._chatadd()
        cid = database.get_catalog_chat_by_chat_id(CHAT)["id"]
        with patch("dc_helpers._is_dc_admin", return_value=True):
            handlers.handle_all_messages(self.bot, 1, SimpleNamespace(msg=_msg(f"/chatapprove{cid}", chat_id=7)))
        self.assertEqual(database.get_catalog_chat_by_chat_id(CHAT)["status"], "approved")
        self.assertIn("approved", self.texts(7)[-1])

        with patch("dc_helpers._is_dc_admin", return_value=True):
            handlers.handle_all_messages(self.bot, 1, SimpleNamespace(msg=_msg(f"/chatban{cid} spam", chat_id=7)))
        self.assertIsNone(database.get_catalog_chat_by_chat_id(CHAT))
        self.assertTrue(database.is_catalog_submitter_banned("user@example.org"))
        self.assertIn("Reason: spam", self.texts(CHAT)[-1])

    def test_non_admin_cannot_approve(self):
        _known_for(USER, 5)
        self._chatadd()
        cid = database.get_catalog_chat_by_chat_id(CHAT)["id"]
        handlers.handle_all_messages(self.bot, 1, SimpleNamespace(msg=_msg(f"/chatapprove{cid}", chat_id=7)))
        self.assertIn("Only the bot administrator", self.texts(7)[-1])
        self.assertEqual(database.get_catalog_chat_by_chat_id(CHAT)["status"], "pending")

    def test_submissions_off_keeps_admin_only(self):
        _known_for(USER, 40)
        database.set_config("catalog_submissions", "off")
        self._chatadd()
        self.assertIsNone(database.get_catalog_chat_by_chat_id(CHAT))
        self.assertIn("Only the bot administrator", self.texts(CHAT)[-1])

    def test_member_may_remove_user_submitted_chat_only(self):
        database.add_catalog_chat(CHAT, "Admin chat", "", 3)
        event = SimpleNamespace(msg=_msg("/chatremove"), payload="")
        channels.chatremove_command(self.bot, 1, event)
        self.assertIsNotNone(database.get_catalog_chat_by_chat_id(CHAT))

        database.remove_catalog_chat(CHAT)
        _known_for(USER, 40)
        self._chatadd()
        channels.chatremove_command(self.bot, 1, event)
        self.assertIsNone(database.get_catalog_chat_by_chat_id(CHAT))

    def test_member_may_toggle_private_of_user_submitted_chat(self):
        _known_for(USER, 40)
        self._chatadd()
        channels.private_command(self.bot, 1, SimpleNamespace(msg=_msg("/private off"), payload="off"))
        self.assertEqual(database.get_catalog_chat_by_chat_id(CHAT)["is_private"], 0)


class TestChannelSubmissions(CatalogReviewTestBase):
    URL = "https://i.delta.chat/#ABC&x=chan"
    CHAN = 777

    def _rpc_for_channel(self):
        rpc = self.bot.rpc
        rpc.secure_join.return_value = self.CHAN
        rpc.get_basic_chat_info.return_value = {"name": "Chan", "chat_type": "InBroadcast"}
        rpc.get_full_chat_by_id.return_value = {"self_in_group": True}
        rpc.get_chat_description.return_value = "Weekly notes"
        rpc.get_message_ids.return_value = []

    def _submit(self, owner_contact=USER):
        self._rpc_for_channel()
        qr = {"kind": "askJoinBroadcast", "name": "Chan", "contact_id": owner_contact}
        catalog_review.submit_channel(self.bot, 1, _msg("/dchanneladd", chat_id=9), self.URL, qr, "Chan")
        return database.get_catalog_channel_by_chat_id(self.CHAN)

    def test_owner_old_enough_is_listed_with_web(self):
        _known_for(USER, 40)
        ch = self._submit()
        self.assertEqual(ch["status"], "approved")
        self.assertEqual(ch["is_public"], 1)
        self.assertEqual(ch["web_enabled"], 1)
        self.assertIsNotNone(database.get_web_channel_by_token(ch["token"]))
        self.assertIn("Web preview", self.texts(9)[-1])

    def test_third_party_channel_is_listed_without_web(self):
        _known_for(USER, 5)
        ch = self._submit(owner_contact=1234)
        self.assertEqual(ch["status"], "pending")
        self.assertEqual(ch["is_public"], 0)
        self.assertEqual(ch["web_enabled"], 0)
        self.assertIn("not the owner", self.admin_msgs[-1])
        # The admin reviews through the secret preview link while it is pending
        self.assertIsNotNone(database.get_web_channel_by_token(ch["token"]))

        self.assertIn("approved", catalog_review.approve(self.bot, 1, "channel", ch["id"]))
        ch = database.get_catalog_channel_by_id(ch["id"])
        self.assertEqual(ch["is_public"], 1)
        self.assertIsNone(database.get_web_channel_by_token(ch["token"]))
        listed = [c["id"] for c in database.get_all_catalog_channels(public_only=True, web_only=True)]
        self.assertNotIn(ch["id"], listed)
        self.assertNotIn(ch["token"], tpl_landing.get_landing_page_html())

        with patch("dc_helpers._is_dc_admin", return_value=True):
            handlers.handle_all_messages(self.bot, 1, SimpleNamespace(msg=_msg(f"/dchannelweb{ch['id']}on", chat_id=7)))
        self.assertIsNotNone(database.get_web_channel_by_token(ch["token"]))

    def test_unfinished_join_is_never_auto_approved(self):
        _known_for(USER, 40)
        with patch.object(channels, "JOIN_QUICK_WAIT", 0):
            self._rpc_for_channel()
            self.bot.rpc.get_full_chat_by_id.return_value = {"self_in_group": False}
            qr = {"kind": "askJoinBroadcast", "name": "Chan", "contact_id": USER}
            catalog_review.submit_channel(self.bot, 1, _msg(chat_id=9), self.URL, qr, "Chan")
        self.assertEqual(database.get_catalog_channel_by_chat_id(self.CHAN)["status"], "pending")

    def test_existing_catalog_channel_is_untouched(self):
        token = database.add_catalog_channel(self.CHAN, "Admin chan", "", 0, "https://other/link", is_public=1)
        _known_for(USER, 40)
        self._submit(owner_contact=1234)
        ch = database.get_catalog_channel_by_token(token)
        self.assertEqual(ch["name"], "Admin chan")
        self.assertEqual(ch["status"], "approved")
        self.assertIsNone(ch["submitted_by"])

    def test_one_channel_check_at_a_time(self):
        _known_for(USER, 40)
        catalog_review._inflight_submitters.add(USER)
        try:
            qr = {"kind": "askJoinBroadcast", "contact_id": USER}
            catalog_review.submit_channel(self.bot, 1, _msg(chat_id=9), self.URL, qr, "Chan")
        finally:
            catalog_review._inflight_submitters.discard(USER)
        self.bot.rpc.secure_join.assert_not_called()
        self.assertIn("still being checked", self.texts(9)[-1])
        # A finished check frees the slot
        self._submit()
        self.assertNotIn(USER, catalog_review._inflight_submitters)

    def test_contact_links_are_refused(self):
        _known_for(USER, 40)
        qr = {"kind": "askVerifyContact", "contact_id": 5}
        catalog_review.submit_channel(self.bot, 1, _msg(chat_id=9), self.URL, qr, "Someone")
        self.bot.rpc.secure_join.assert_not_called()

    def test_reject_leaves_and_deletes_cache(self):
        _known_for(USER, 5)
        ch = self._submit(owner_contact=1234)
        media = os.path.join(state.CHANNEL_MEDIA_DIR, ch["token"])
        os.makedirs(media)
        result = catalog_review.reject(self.bot, 1, "channel", ch["id"], "scam")
        self.assertIn("rejected", result)
        self.bot.rpc.leave_group.assert_called_with(1, self.CHAN)
        self.assertIsNone(database.get_catalog_channel_by_token(ch["token"]))
        self.assertFalse(os.path.exists(media))
        self.assertEqual(database.get_latest_catalog_submission("channel", ch["id"])["status"], "rejected")

    def test_admin_entries_are_not_rejected(self):
        database.add_catalog_channel(self.CHAN, "Admin chan", "", 0, self.URL, is_public=1)
        cid = database.get_catalog_channel_by_chat_id(self.CHAN)["id"]
        self.assertIn("added by the admin", catalog_review.reject(self.bot, 1, "channel", cid))
        self.assertIsNotNone(database.get_catalog_channel_by_chat_id(self.CHAN))

    def test_link_owner_by_fingerprint(self):
        with patch("dc_helpers._get_contact_fingerprint", return_value="AAAA,BBBB"):
            self.assertTrue(catalog_review.is_link_owner(self.bot, 1, {"fingerprint": "bb bb"}, USER))
            self.assertFalse(catalog_review.is_link_owner(self.bot, 1, {"fingerprint": "CCCC"}, USER))
        self.assertTrue(catalog_review.is_link_owner(self.bot, 1, {"contact_id": USER}, USER))


class TestReports(CatalogReviewTestBase):
    def setUp(self):
        super().setUp()
        database.add_catalog_channel(900, "Listed", "", 0, "https://i.delta.chat/#x", is_public=1)
        self.cid = database.get_catalog_channel_by_chat_id(900)["id"]

    def test_threshold_hides_entry(self):
        for reporter in (101, 102):
            _known_for(reporter, 10)
            self.assertIn("Thank you", catalog_review.report(self.bot, 1, "channel", self.cid, reporter, "scam"))
        self.assertEqual(database.get_catalog_channel_by_id(self.cid)["is_public"], 1)
        _known_for(103, 10)
        catalog_review.report(self.bot, 1, "channel", self.cid, 103, "")
        ch = database.get_catalog_channel_by_id(self.cid)
        self.assertEqual((ch["status"], ch["is_public"]), ("pending", 0))
        self.assertIn("hidden after 3 reports", self.admin_msgs[-1])

        # Keeping it listed clears the reports
        catalog_review.approve(self.bot, 1, "channel", self.cid)
        self.assertEqual(database.get_catalog_channel_by_id(self.cid)["is_public"], 1)
        self.assertEqual(database.get_catalog_reports("channel", self.cid), [])

    def test_young_and_duplicate_reports_do_not_count(self):
        _known_for(101, 1)
        self.assertIn("new contacts", catalog_review.report(self.bot, 1, "channel", self.cid, 101, ""))
        _known_for(102, 10)
        catalog_review.report(self.bot, 1, "channel", self.cid, 102, "")
        self.assertIn("already reported", catalog_review.report(self.bot, 1, "channel", self.cid, 102, ""))
        self.assertEqual(len(database.get_catalog_reports("channel", self.cid)), 1)

    def test_report_via_command(self):
        _known_for(USER, 10)
        handlers.handle_all_messages(self.bot, 1, SimpleNamespace(msg=_msg(f"/dchannelreport{self.cid} phishing", chat_id=9)))
        self.assertIn("Thank you", self.texts(9)[-1])
        self.assertEqual(database.get_catalog_reports("channel", self.cid)[0]["reason"], "phishing")



class TestShortDescription(unittest.TestCase):
    def test_first_line_only(self):
        self.assertEqual(channels.short_description("Мяу! 🐱\n\n🔗 https://deltachat.id/ru-cats", 100), "Мяу! 🐱")

    def test_skips_leading_blank_lines_and_collapses_spaces(self):
        self.assertEqual(channels.short_description("\n  \n  Hello   world \nmore", 100), "Hello world")

    def test_long_line_is_cut_at_word(self):
        self.assertEqual(channels.short_description("one two three four", 12), "one two…")

    def test_long_word_is_cut_hard(self):
        self.assertEqual(channels.short_description("x" * 20, 5), "xxxxx…")

    def test_empty(self):
        self.assertEqual(channels.short_description(None, 100), "")

    def test_chats_list_uses_first_line(self):
        database.close_db()
        database.DB_PATH = TEST_DB
        database.init_db()
        try:
            database.add_catalog_chat(1, "Delta Cats", "Мяу! 🐱\n\n🔗 https://deltachat.id/ru-cats 👥 7", 3)
            sent = []
            with patch("dc_helpers._send", side_effect=lambda b, a, c, text, *x, **k: sent.append(text)):
                channels.chats_command(MagicMock(), 1, SimpleNamespace(msg=_msg("/chats")))
            self.assertIn("**Delta Cats** Мяу! 🐱 👥 3", sent[-1])
            self.assertNotIn("deltachat.id", sent[-1])
        finally:
            database.close_db()
            for path in (TEST_DB, TEST_DB + "-wal", TEST_DB + "-shm"):
                if os.path.exists(path):
                    os.remove(path)


if __name__ == "__main__":
    unittest.main()


class AdminKeyContactTest(unittest.TestCase):
    """Admin notices must go to the admin's key contact: the address-only contact cannot be written to."""
    FP = "DFF2CAB1FEB7182F997C0A01466AA64DE33D8A39"

    def setUp(self):
        database.close_db()
        database.DB_PATH = TEST_DB
        database.init_db()
        database.set_config("admin_dc_email", "admin@example.org")
        database.set_admin_fingerprint(self.FP)
        self.bot = MagicMock()
        contacts = {
            10: {"address": "admin@example.org", "is_key_contact": False, "last_seen": 0},
            11: {"address": "admin@example.org", "is_key_contact": True, "last_seen": 900},   # other device
            12: {"address": "admin@example.org", "is_key_contact": True, "last_seen": 100},   # the admin's key
        }
        self.bot.rpc.get_contact_ids.return_value = [10, 11, 12]
        self.bot.rpc.get_contact.side_effect = lambda acc, cid: contacts[cid]
        self.bot.rpc.lookup_contact_id_by_addr.return_value = 10
        fps = {11: "AAAA" * 10, 12: self.FP}
        self.fp_patch = patch("dc_helpers._get_contact_fingerprint", side_effect=lambda b, a, cid, c=None: fps.get(cid))
        self.fp_patch.start()

    def tearDown(self):
        self.fp_patch.stop()
        database.close_db()
        for f in (TEST_DB, TEST_DB + "-wal", TEST_DB + "-shm"):
            if os.path.exists(f):
                os.remove(f)

    def test_prefers_contact_with_admin_fingerprint(self):
        self.assertEqual(dc_helpers.admin_key_contact_id(self.bot, 1), 12)

    def test_unknown_fingerprint_uses_latest_key_contact(self):
        database.set_admin_fingerprint("")
        self.assertEqual(dc_helpers.admin_key_contact_id(self.bot, 1), 11)

    def test_no_key_contacts_falls_back_to_lookup(self):
        self.bot.rpc.get_contact_ids.return_value = []
        self.assertEqual(dc_helpers.admin_key_contact_id(self.bot, 1), 10)

    def test_admin_chat_uses_key_contact(self):
        self.bot.rpc.create_chat_by_contact_id.side_effect = lambda acc, cid: 1000 + cid
        self.assertEqual(catalog_review._admin_chat_id(self.bot, 1), 1012)
