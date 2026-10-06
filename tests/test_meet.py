"""Voice meetings (meet.py): mixer, capacity budget, commands, call routing,
and a real 3-party WebRTC meeting through the bot's audio bridge.

Run: python3 -m unittest tests.test_meet -v
"""
import os
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

TEST_DB = "test_meet.db"
os.environ["DB_PATH"] = TEST_DB

from tests import test_calls  # noqa: E402,F401  (installs the deltachat2/deltabot_cli mocks if needed)

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import calls  # noqa: E402
import config  # noqa: E402
import database  # noqa: E402
import state  # noqa: E402

rtc = calls.rtc
if rtc is not None:
    import numpy as np

    import meet
else:  # aiortc not installed in this environment
    meet = None


class Ev(dict):
    __getattr__ = dict.__getitem__


def _cleanup_db():
    database.close_db()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(TEST_DB + suffix):
            os.remove(TEST_DB + suffix)


def _tone(freq, amp=8000):
    t = np.arange(rtc.FRAME_SAMPLES) / rtc.SAMPLE_RATE
    return (np.sin(2 * np.pi * freq * t) * amp).astype(np.int16)


def _note_of(mono):
    """Nearest scale note of a frame (zero-padded FFT: ~3 Hz resolution), or None."""
    if rtc.rms(mono) < 1000:
        return None
    spec = np.abs(np.fft.rfft(mono * np.hanning(mono.size), n=16384))
    spec[:20] = 0
    freq = np.argmax(spec) * rtc.SAMPLE_RATE / 16384
    name, f = min(meet.TUNE_SCALE, key=lambda nf: abs(nf[1] - freq))
    return name if abs(f - freq) / f < 0.03 else None


def _dedup(names):
    return [n for i, n in enumerate(names) if i == 0 or names[i - 1] != n]


def _notes(monos):
    """Distinct notes in order (a note held over several frames counts once)."""
    out = []
    for mono in monos:
        n = _note_of(mono)
        if n and (not out or out[-1] != n):
            out.append(n)
    return out


class _Base(unittest.TestCase):
    def setUp(self):
        _cleanup_db()
        database.DB_PATH = TEST_DB
        database.init_db()
        state.meet_manager = None
        self.bot = MagicMock()
        self.bot.rpc.ice_servers.return_value = "[]"
        self.managers = []
        fp = patch("meet.dc_helpers._get_contact_fingerprint",
                   side_effect=lambda bot, accid, cid, contact=None: f"{cid:040X}")
        fp.start()
        self.addCleanup(fp.stop)

    def tearDown(self):
        for m in self.managers:
            for room in list(m.rooms.values()):
                room.closed = True
        time.sleep(0.1)  # let the mixer tasks see it
        for m in self.managers:
            m.loop.stop()
        state.meet_manager = None
        _cleanup_db()

    def manager(self):
        m = meet.MeetManager(self.bot)
        self.managers.append(m)
        return m

    def fake_participant(self, m, room, cid, connected=True):
        p = meet.Participant(accid=1, contact_id=cid, chat_id=100 + cid, room=room)
        p.out = meet.MeetOutTrack()
        p.connected = connected
        room.participants[cid] = p
        room.empty_since = None
        return p


@unittest.skipIf(rtc is None, "aiortc/cmcall not installed")
class TestMixer(_Base):
    def test_meet_id(self):
        ids = {meet.new_meet_id() for _ in range(50)}
        self.assertEqual(len(ids), 50)
        for i in ids:
            self.assertRegex(i, r"^[A-Za-z0-9]{12}$")

    def test_mix_minus(self):
        a, b, c = (np.full(rtc.FRAME_SAMPLES, v, dtype=np.int16) for v in (100, 20, 3))
        out = meet.mix_frames({"a": a, "b": b, "c": c})
        self.assertEqual(int(out["a"][0]), 23)
        self.assertEqual(int(out["b"][0]), 103)
        self.assertEqual(int(out["c"][0]), 120)
        self.assertEqual(int(out["*"][0]), 123)
        self.assertEqual(meet.mix_frames({}), {})

    def test_clip(self):
        loud = np.full(rtc.FRAME_SAMPLES, 30000, dtype=np.int16)
        out = meet.mix_frames({"a": loud, "b": loud, "c": loud})
        self.assertEqual(int(meet._clip(out["a"])[0]), 32767)

    def test_mix_tick_gate_mute_and_self(self):
        m = self.manager()
        room = meet.Room(id="x" * 12, owner=1, closed=True)  # closed: no mixer task
        a, b, c, d = (self.fake_participant(m, room, cid) for cid in (1, 2, 3, 4))
        a.inbox.append(_tone(500))                          # speaking
        b.inbox.append(_tone(700, amp=50))                  # background hiss: gated
        c.inbox.append(_tone(900))                          # speaking but muted
        c.muted = True
        m.mix_tick(room)
        # only a is active: a hears nothing, everybody else hears a
        self.assertEqual(len(a.out.queue), 0)
        for p in (b, c, d):
            self.assertEqual(len(p.out.queue), 1)
            self.assertTrue(np.array_equal(p.out.queue[0], _tone(500)))

    def test_gate_hold_keeps_word_ends(self):
        m = self.manager()
        room = meet.Room(id="y" * 12, owner=1, closed=True)
        a, b = self.fake_participant(m, room, 1), self.fake_participant(m, room, 2)
        a.inbox.append(_tone(500))
        m.mix_tick(room)
        quiet = _tone(500, amp=50)
        for _ in range(meet.GATE_HOLD_FRAMES):
            a.inbox.append(quiet)
            m.mix_tick(room)
        a.inbox.append(quiet)
        m.mix_tick(room)  # hold used up: gated again
        self.assertEqual(len(b.out.queue), b.out.queue.maxlen)  # capped, oldest dropped
        b.out.queue.clear()
        a.inbox.append(quiet)
        m.mix_tick(room)
        self.assertEqual(len(b.out.queue), 0)

    def test_backlog_is_trimmed(self):
        m = self.manager()
        room = meet.Room(id="z" * 12, owner=1, closed=True)
        a, _b = self.fake_participant(m, room, 1), self.fake_participant(m, room, 2)
        for _ in range(20):
            a.inbox.append(_tone(500))
        m.mix_tick(room)
        self.assertEqual(len(a.inbox), meet.INBOX_MAX_BACKLOG - 1)

    def test_out_track_mixes_tune_over_speech(self):
        import asyncio

        track = meet.MeetOutTrack()
        track.play(meet.tune_frames((0, 9, 2, 5, 4)))
        speech = _tone(1000, amp=3000)

        async def take(n):
            return [await track.recv() for _ in range(n)]

        f1, = asyncio.run(take(1))
        self.assertEqual(_note_of(rtc.frame_mono(f1)), "C4")
        tune_pcm = track.prio[0].copy()
        track.push(speech)
        f2, = asyncio.run(take(1))
        out = f2.to_ndarray().reshape(-1)
        self.assertTrue(np.array_equal(out, meet._clip(tune_pcm.astype(np.int32) + speech)))
        self.assertEqual(f2.pts - f1.pts, rtc.FRAME_SAMPLES)
        track.prio.clear()
        track.push(speech)
        f3, = asyncio.run(take(1))
        self.assertTrue(np.array_equal(f3.to_ndarray().reshape(-1), speech))


@unittest.skipIf(rtc is None, "aiortc/cmcall not installed")
class TestTunes(_Base):
    def test_tune_from_seed(self):
        t = meet.tune_for("fp:ABCD")
        self.assertEqual(t, meet.tune_for("fp:ABCD"))  # stable
        self.assertEqual(len(t), meet.TUNE_NOTES)
        self.assertTrue(all(0 <= n < len(meet.TUNE_SCALE) for n in t))
        self.assertTrue(all(a != b for a, b in zip(t, t[1:])), t)
        tunes = {meet.tune_for(f"fp:{i}") for i in range(200)}
        self.assertGreater(len(tunes), 195)  # 10*9^4 = 65610 possible tunes
        self.assertRegex(meet.tune_name(t), r"^([A-G][45] ){4}[A-G][45]$")

    def test_tune_frames(self):
        tune = (0, 9, 2, 5, 4)
        frames = meet.tune_frames(tune)
        self.assertEqual(len(frames), 4 * meet.TUNE_NOTE_FRAMES + meet.TUNE_LAST_FRAMES)
        heard = _notes([rtc.frame_mono(f) for f in frames])
        self.assertEqual(heard, ["C4", "A5", "E4", "C5", "A4"])
        back = _notes([rtc.frame_mono(f) for f in meet.tune_frames(tune, reverse=True)])
        self.assertEqual(back, ["A4", "C5", "E4", "A5", "C4"])
        peak = max(np.abs(rtc.frame_mono(f)).max() for f in frames)
        self.assertLess(peak, 0.2 * 32767)  # not louder than the echo greeting
        self.assertEqual(meet.tune_frames(()), [])

    def test_tune_comes_from_fingerprint_and_is_cached(self):
        m = self.manager()
        t = m.tune_of(1, 42)
        self.assertEqual(t, meet.tune_for(f"fp:{42:040X}"))
        meet.dc_helpers._get_contact_fingerprint.side_effect = lambda *a, **k: None
        self.bot.rpc.get_contact.return_value = MagicMock(address="Bob@Example.org")
        self.assertEqual(m.tune_of(1, 42), t)  # cached
        self.assertEqual(m.tune_of(1, 43), meet.tune_for("addr:bob@example.org"))
        self.assertIn(meet.tune_name(t), m.tune_line(1, 42))


@unittest.skipIf(rtc is None, "aiortc/cmcall not installed")
class TestCapacity(_Base):
    def test_budget(self):
        m = self.manager()
        self.assertEqual(m.room_capacity(1), 8)
        self.assertEqual(m.room_capacity(2), 4)
        self.assertTrue(m.can_create_room()[0])
        r1 = m.create_room(owner=1)
        self.assertEqual(m.room_capacity(), 8)
        for cid in range(10, 15):  # 5 people: a second room would leave only 4 places
            self.fake_participant(m, r1, cid)
        ok, why = m.can_create_room()
        self.assertFalse(ok)
        self.assertIn("8 places", why)
        r1.participants.pop(14)
        self.assertTrue(m.can_create_room()[0])
        m.create_room(owner=2)
        self.assertEqual(m.room_capacity(), 4)
        ok, why = m.can_create_room()
        self.assertFalse(ok)
        self.assertIn("maximum of 2 meetings", why)

    @patch("meet.threading.Thread")
    def test_join_full_room(self, _thread):
        m = self.manager()
        room = m.create_room(owner=1)
        for cid in range(10, 18):
            self.fake_participant(m, room, cid)
        self.assertIn("full (8 people)", m.join(1, 99, room.id))
        self.assertIn("already in", m.join(1, 10, room.id))
        self.assertIn("No such meeting", m.join(1, 99, "A" * 12))
        room.participants.pop(17)
        reply = m.join(1, 99, room.id)
        self.assertTrue(reply.startswith("📞"))
        self.assertIn("8/8", reply)
        self.assertIn("does not record", reply)
        self.assertEqual(m.contact_room[99], room.id)

    def test_wants_incoming_needs_switch_and_open_room(self):
        m = self.manager()
        room = m.create_room(owner=5)
        self.assertFalse(m.wants_incoming(5))  # meetings are off
        database.set_config("meets_enabled", "1")
        self.assertTrue(m.wants_incoming(5))
        self.assertFalse(m.wants_incoming(6))
        m.close_room(room, "test")
        self.assertFalse(m.wants_incoming(5))

    @patch("meet.dc_helpers._send")
    def test_reaper(self, _send):
        m = self.manager()
        idle = m.create_room(owner=1)
        busy = m.create_room(owner=2)
        p = self.fake_participant(m, busy, 3, connected=False)
        now = time.time()
        m.reap(now + 59 * 60)
        self.assertIn(idle.id, m.rooms)
        m.reap(now + 61 * 60)
        self.assertNotIn(idle.id, m.rooms)
        self.assertIn(busy.id, m.rooms)  # occupied: idle timer does not run
        m.reap(now + config.MEET_MAX_HOURS * 3600 + 60)
        self.assertNotIn(busy.id, m.rooms)
        self.assertTrue(p.left)


@unittest.skipIf(rtc is None, "aiortc/cmcall not installed")
class TestCommands(_Base):
    def _event(self, text, payload="", from_id=10, chat_id=42):
        return MagicMock(msg=MagicMock(text=text, from_id=from_id, chat_id=chat_id), payload=payload)

    @patch("meet.dc_helpers._is_dc_admin", return_value=False)
    @patch("meet.dc_helpers._send")
    def test_meets_admin_only_and_off_by_default(self, mock_send, _admin):
        meet.meets_command(self.bot, 1, self._event("/meets on", "on"))
        self.assertIn("Only the bot administrator", mock_send.call_args[0][3])
        self.assertFalse(meet.meets_enabled())
        meet.meet_command(self.bot, 1, self._event("/meet"))
        self.assertIn("disabled", mock_send.call_args[0][3])
        meet.join_command(self.bot, 1, self._event("/join_abcdefABCDEF", "abcdefABCDEF"))
        self.assertIn("disabled", mock_send.call_args[0][3])

    @patch("meet.dc_helpers._is_dc_admin", return_value=True)
    @patch("meet.dc_helpers._send")
    def test_meets_switch(self, mock_send, _admin):
        for arg, expected in (("on", "1"), ("0", "0"), ("1", "1"), ("off", "0")):
            meet.meets_command(self.bot, 1, self._event("/meets " + arg, arg))
            self.assertEqual(database.get_config("meets_enabled"), expected)
        meet.meets_command(self.bot, 1, self._event("/meets"))
        self.assertIn("Voice meetings: ❌ off", mock_send.call_args[0][3])
        meet.meets_command(self.bot, 1, self._event("/meets maybe", "maybe"))
        self.assertIn("Usage", mock_send.call_args[0][3])

    @patch("meet.dc_helpers._is_dc_admin", return_value=True)
    @patch("meet.dc_helpers._send")
    def test_turning_off_closes_rooms(self, mock_send, _admin):
        database.set_config("meets_enabled", "1")
        m = meet.get_manager(self.bot)
        self.managers.append(m)
        room = m.create_room(owner=1)
        meet.meets_command(self.bot, 1, self._event("/meets off", "off"))
        self.assertTrue(room.closed)
        self.assertEqual(m.rooms, {})

    def _chats(self, groups=(42,), members=(10, 11)):
        """Chat 42 is a group with contacts 10 and 11; every other chat is private."""
        self.bot.rpc.get_basic_chat_info.side_effect = (
            lambda accid, cid: {"chat_type": "Group" if cid in groups else "Single"})
        self.bot.rpc.get_chat_contacts.side_effect = lambda accid, cid: [1, *members]
        database.set_config("meets_enabled", "1")

    def _texts(self, mock_send):
        return [(c[0][2], c[0][3]) for c in mock_send.call_args_list]

    @patch("meet._invite_link", return_value="https://i.delta.chat/#BOT")
    @patch("meet.threading.Thread")
    @patch("meet.dc_helpers._send")
    def test_private_room(self, mock_send, _thread, _link):
        self._chats()
        meet.meet_command(self.bot, 1, self._event("/meet", chat_id=7))
        m = state.meet_manager
        self.managers.append(m)
        room = next(iter(m.rooms.values()))
        self.assertEqual((room.chat_id, room.group, room.owner), (7, False, 10))
        (c1, info), (c2, invite) = self._texts(mock_send)
        self.assertEqual((c1, c2), (7, 7))
        self.assertIn(f"/join_{room.id}", info)
        self.assertIn("share it only privately", info)
        self.assertIn("does not record or store your voice", info)
        self.assertIn("not end-to-end encrypted", info)
        self.assertIn("Up to 8 people", info)
        self.assertIn("/meetclose", info)
        # the second message is made for forwarding: bot invite + command
        self.assertEqual(invite, f"🎙️ Join my voice meeting: add this bot: https://i.delta.chat/#BOT and "
                                 f"send it this message: /join_{room.id}")
        # same chat again: the same room
        mock_send.reset_mock()
        meet.meet_command(self.bot, 1, self._event("/meet", chat_id=7))
        self.assertIn("This chat's voice meeting", mock_send.call_args_list[0][0][3])
        self.assertEqual(len(m.rooms), 1)
        # another chat: the link is never shown there
        mock_send.reset_mock()
        meet.meet_command(self.bot, 1, self._event("/meet", chat_id=42))
        text = mock_send.call_args[0][3]
        self.assertIn("already have an open meeting in another chat", text)
        self.assertNotIn(room.id, text)
        self.assertEqual(len(m.rooms), 1)
        # anyone with the link may join a private room
        self.bot.rpc.create_chat_by_contact_id.return_value = 77
        meet.join_command(self.bot, 1, self._event(f"/join_{room.id}", room.id, from_id=99, chat_id=77))
        self.assertEqual(mock_send.call_args[0][2], 77)
        self.assertIn("Calling you", mock_send.call_args[0][3])
        _thread.return_value.start.assert_called()
        meet.join_command(self.bot, 1, self._event("/join nope", "nope"))
        self.assertIn("Usage", mock_send.call_args[0][3])

    @patch("meet.threading.Thread")
    @patch("meet.dc_helpers._send")
    def test_group_room_is_for_members(self, mock_send, _thread):
        self._chats()
        meet.meet_command(self.bot, 1, self._event("/meet", chat_id=42))
        m = state.meet_manager
        self.managers.append(m)
        room = next(iter(m.rooms.values()))
        self.assertTrue(room.group)
        self.assertEqual(len(mock_send.call_args_list), 1)  # no forwardable invite for groups
        info = mock_send.call_args[0][3]
        self.assertIn("Only members of this group", info)
        self.assertIn("Reply Privately", info)
        # another member gets the same room
        meet.meet_command(self.bot, 1, self._event("/meet", from_id=11, chat_id=42))
        self.assertIn(f"/join_{room.id}", mock_send.call_args[0][3])
        self.assertEqual(len(m.rooms), 1)
        # an outsider with the leaked link is refused, and not routed on call-back
        self.bot.rpc.create_chat_by_contact_id.return_value = 77
        meet.join_command(self.bot, 1, self._event(f"/join_{room.id}", room.id, from_id=12, chat_id=77))
        self.assertEqual(mock_send.call_args[0][3], meet.NOT_MEMBER)
        self.assertNotIn(12, m.contact_room)
        calls_out = [c for c in _thread.call_args_list if c.kwargs.get("name") == "meet-call-out"]
        self.assertEqual(calls_out, [])
        # a member is called
        meet.join_command(self.bot, 1, self._event(f"/join_{room.id}", room.id, from_id=11, chat_id=42))
        self.assertIn("Calling you", mock_send.call_args[0][3])
        self.assertEqual(mock_send.call_args[0][2], 77)  # private chat, not the group

    @patch("meet.dc_helpers._send")
    def test_unsupported_chat(self, mock_send):
        database.set_config("meets_enabled", "1")
        self.bot.rpc.get_basic_chat_info.return_value = {"chat_type": "OutBroadcast"}
        meet.meet_command(self.bot, 1, self._event("/meet", chat_id=9))
        self.assertIn("group or in a private chat", mock_send.call_args[0][3])
        self.assertIsNone(state.meet_manager)  # nothing was started

    @patch("meet.dc_helpers._send")
    def test_removed_member_is_dropped(self, mock_send):
        self._chats(members=(10, 11, 12))
        m = self.manager()
        room = m.create_room(10, chat_id=42, group=True)
        p11 = self.fake_participant(m, room, 11)
        p12 = self.fake_participant(m, room, 12)
        p12.joined_at = time.time() - 65
        m.contact_room[13] = room.id
        self._chats(members=(10, 11))  # 12 and 13 left the group
        m.check_members()
        self.assertTrue(p12.left)
        self.assertFalse(p11.left)
        self.assertNotIn(13, m.contact_room)
        self.assertIn("no longer in the group", mock_send.call_args[0][3])
        self.assertNotIn("/join_", mock_send.call_args[0][3])
        # calling back in after leaving the group: declined, not routed
        m.contact_room[12] = room.id
        ev = Ev(kind="IncomingCall", msg_id=900, chat_id=112, place_call_info="v=0")
        m.on_incoming_call(1, ev, 12)
        self.bot.rpc.end_call.assert_called_with(1, 900)
        self.assertEqual(mock_send.call_args[0][3], meet.NOT_MEMBER)
        self.assertNotIn(12, m.contact_room)

    @patch("meet.dc_helpers._is_dc_admin", return_value=False)
    @patch("meet.dc_helpers._send")
    def test_meetclose_by_creator(self, mock_send, _admin):
        self._chats()
        meet.meet_command(self.bot, 1, self._event("/meet", chat_id=42))
        m = state.meet_manager
        self.managers.append(m)
        room = next(iter(m.rooms.values()))
        p = self.fake_participant(m, room, 11)
        meet.meetclose_command(self.bot, 1, self._event("/meetclose", from_id=11, chat_id=42))
        self.assertIn("Only the person who started", mock_send.call_args[0][3])
        self.assertFalse(room.closed)
        meet.meetclose_command(self.bot, 1, self._event("/meetclose", room.id, from_id=11, chat_id=42))
        self.assertIn("No open meeting", mock_send.call_args[0][3])
        # the creator can close it from their private chat with the bot
        meet.meetclose_command(self.bot, 1, self._event("/meetclose", from_id=10, chat_id=7))
        self.assertTrue(room.closed)
        self.assertTrue(p.left)
        self.assertIn("closed, 1 participant(s) disconnected", mock_send.call_args[0][3])
        self.assertNotIn("/join_", mock_send.call_args[0][3])
        meet.meetclose_command(self.bot, 1, self._event("/meetclose", from_id=10, chat_id=42))
        self.assertIn("no open meeting here", mock_send.call_args[0][3])

    @patch("meet.dc_helpers._is_dc_admin", return_value=True)
    @patch("meet.dc_helpers._send")
    def test_admin_closes_any_room_by_prefix(self, mock_send, _admin):
        self._chats()
        m = meet.get_manager(self.bot)
        self.managers.append(m)
        room = m.create_room(10, chat_id=42, group=True)
        meet.meetclose_command(self.bot, 1, self._event("/meetclose", room.id[:3], from_id=99, chat_id=5))
        self.assertIn("No open meeting", mock_send.call_args[0][3])  # prefix too short
        meet.meetclose_command(self.bot, 1, self._event("/meetclose", room.id[:4], from_id=99, chat_id=5))
        self.assertTrue(room.closed)
        sent = self._texts(mock_send)
        self.assertIn((42, "📞 This chat's voice meeting was closed by the bot admin."), sent)

    @patch("meet.dc_helpers._is_dc_admin", return_value=False)
    @patch("meet.dc_helpers._send")
    def test_meetnew_swaps_the_link(self, mock_send, _admin):
        self._chats()
        meet.meet_command(self.bot, 1, self._event("/meet", chat_id=42))
        m = state.meet_manager
        self.managers.append(m)
        old = next(iter(m.rooms.values()))
        p = self.fake_participant(m, old, 11)
        mock_send.reset_mock()
        meet.meetnew_command(self.bot, 1, self._event("/meetnew", chat_id=42))
        self.assertTrue(old.closed)
        new = m.room_for_chat(42)
        self.assertNotEqual(new.id, old.id)
        self.assertEqual((new.owner, new.group), (10, True))
        texts = self._texts(mock_send)
        self.assertIn("restarted with a new link", texts[0][1])          # to the participant
        self.assertNotIn(new.id, texts[0][1])                           # ...without the new link
        self.assertTrue(any(c == 42 and f"/join_{new.id}" in t for c, t in texts))
        self.assertIn("No such meeting", m.join(1, 11, old.id))
        self.assertTrue(p.left)


@unittest.skipIf(rtc is None, "aiortc/cmcall not installed")
class TestRouting(_Base):
    def test_incoming_call_routing(self):
        echo, mm = MagicMock(), MagicMock()
        state.echo_call_manager, state.meet_manager = echo, mm
        self.bot.rpc.get_message.return_value = MagicMock(from_id=10)
        try:
            ev = Ev(kind="IncomingCall", msg_id=5, chat_id=42, place_call_info="v=0")
            mm.wants_incoming.return_value = True
            with patch("calls.threading.Thread") as th:
                calls.on_call_event(self.bot, 1, ev)
                target, args = th.call_args[1]["target"], th.call_args[1]["args"]
            target(*args)
            mm._safe.assert_called_once_with(mm.on_incoming_call, 1, ev, 10)
            echo._answer_safe.assert_not_called()
            mm.wants_incoming.return_value = False
            target(*args)
            echo._answer_safe.assert_called_once_with(1, ev)

            calls.on_call_event(self.bot, 1, Ev(kind="OutgoingCallAccepted", msg_id=6, accept_call_info="x"))
            mm.on_outgoing_accepted.assert_called_once()
            calls.on_call_event(self.bot, 1, Ev(kind="CallEnded", msg_id=6))
            mm.on_call_ended.assert_called_once()
            echo.on_call_ended.assert_called_once()
            self.assertIn("OutgoingCallAccepted", calls.CALL_EVENT_KINDS)
        finally:
            state.echo_call_manager = state.meet_manager = None

    def test_without_meetings_echo_answers_directly(self):
        echo = MagicMock()
        state.echo_call_manager = echo
        try:
            calls.on_call_event(self.bot, 1, Ev(kind="IncomingCall", msg_id=5, chat_id=42))
            echo.on_incoming_call.assert_called_once()
        finally:
            state.echo_call_manager = None


# --------------------------------------------------------------------------
# real WebRTC
# --------------------------------------------------------------------------


class Listener:
    """A client's ears: classifies every decoded frame by its dominant tone."""

    def __init__(self):
        self.freqs = []
        self.monos = []

    def notes(self):
        return _notes(self.monos)

    def clear(self):
        self.freqs.clear()
        self.monos.clear()

    def __call__(self, frame):
        mono = rtc.frame_mono(frame)
        self.monos.append(mono)
        if rtc.rms(mono) > 1200:
            self.freqs.append(round(rtc.dominant_freq(mono, rtc.SAMPLE_RATE)))
        else:
            self.freqs.append(0)

    def heard(self, freq, tol=40):
        return sum(1 for f in self.freqs if f and abs(f - freq) <= tol)


@unittest.skipIf(rtc is None, "aiortc/cmcall not installed")
class TestMeetingIntegration(_Base):
    """Three clients in one room: A calls in (beeps), B is called by the bot
    after /join, C calls back into the room. B and C hear A, A does not hear
    itself, joins and leaves are announced with the participant's tune."""

    def setUp(self):
        super().setUp()
        database.set_config("meets_enabled", "1")
        self.client_loop = rtc.CallLoop("test-clients")
        self.answers = {}
        self.placed = {}
        self.bot.rpc.accept_incoming_call.side_effect = lambda accid, msg_id, sdp: self.answers.__setitem__(msg_id, sdp)
        self.bot.rpc.create_chat_by_contact_id.side_effect = lambda accid, cid: 100 + cid
        self._next_msg = iter(range(800, 900))

        def place(accid, chat_id, sdp, has_video):
            msg_id = next(self._next_msg)
            self.placed[chat_id] = (msg_id, sdp)
            return msg_id

        self.bot.rpc.place_outgoing_call.side_effect = place

    def tearDown(self):
        self.client_loop.stop()
        super().tearDown()

    def _wait(self, cond, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def _call_in(self, m, peer, contact_id, msg_id):
        offer = self.client_loop.run(peer.offer(), timeout=20)
        ev = Ev(kind="IncomingCall", msg_id=msg_id, chat_id=100 + contact_id, place_call_info=offer)
        threading.Thread(target=m.on_incoming_call, args=(1, ev, contact_id), daemon=True).start()
        self.assertTrue(self._wait(lambda: msg_id in self.answers), "bot did not answer")
        self.client_loop.run(peer.accept_answer(self.answers[msg_id]), timeout=10)
        self.assertTrue(self.client_loop.run(peer.wait_connected(15), timeout=20))

    def _connected(self, room, cid):
        p = room.participants.get(cid)
        return p is not None and p.connected

    @patch("meet.dc_helpers._send")
    def test_three_party_meeting(self, mock_send):
        m = self.manager()
        room = m.create_room(owner=11)
        a = rtc.ProbePeer([], interval=0.5)
        b_ears, c_ears = Listener(), Listener()
        b = rtc.EchoPeer([], greeting=False, on_audio=b_ears)
        c = rtc.EchoPeer([], greeting=False, on_audio=c_ears)
        try:
            # A (owner) calls in
            self._call_in(m, a, 11, 701)
            self.assertTrue(self._wait(lambda: self._connected(room, 11)))

            # B taps /join: the bot calls B, B answers
            reply = m.join(1, 12, room.id)
            self.assertIn("Calling you", reply)
            self.assertTrue(self._wait(lambda: 112 in self.placed), "bot did not call B")
            msg_b, offer = self.placed[112]
            answer = self.client_loop.run(b.accept(offer), timeout=20)
            m.on_outgoing_accepted(Ev(kind="OutgoingCallAccepted", msg_id=msg_b, accept_call_info=answer))
            self.assertTrue(self.client_loop.run(b.wait_connected(15), timeout=20))
            self.assertTrue(self._wait(lambda: self._connected(room, 12)))

            # C joined earlier and calls back into the room
            m.contact_room[13] = room.id
            self.assertTrue(m.wants_incoming(13))
            self._call_in(m, c, 13, 702)
            self.assertTrue(self._wait(lambda: self._connected(room, 13)))
            self.assertEqual(len(room.participants), 3)

            # join tunes queue up (B's may still play when C's starts): wait them out
            self.assertTrue(self._wait(lambda: all(not p.out.prio for p in room.participants.values())))
            time.sleep(0.5)  # jitter buffers
            # B heard its own tune when it came in, then C's
            expected = _dedup([meet.TUNE_SCALE[n][0] for n in m.tune_of(1, 12) + m.tune_of(1, 13)])
            self.assertEqual(b_ears.notes(), expected)
            b_ears.clear()
            c_ears.clear()

            # A beeps; B and C hear it, A does not hear itself
            self.client_loop.run(a.run_probe(3.0), timeout=30)
            time.sleep(0.5)
            a_echo = self.client_loop.run(a.summary(), timeout=10)["echo"]
            self.assertGreaterEqual(a_echo["sent"], 5)
            self.assertEqual(a_echo["received"], 0, a_echo)
            beeps_b = sum(b_ears.heard(f) for f in rtc.PROBE_FREQS)
            beeps_c = sum(c_ears.heard(f) for f in rtc.PROBE_FREQS)
            self.assertGreaterEqual(beeps_b, a_echo["sent"] * 3, b_ears.freqs)
            self.assertGreaterEqual(beeps_c, a_echo["sent"] * 3, c_ears.freqs)

            # A hangs up: B and C hear the leave tones, the room stays open
            b_ears.clear()
            self.client_loop.run(a.close(), timeout=10)
            m.on_call_ended(Ev(kind="CallEnded", msg_id=701))
            self.assertTrue(self._wait(lambda: 11 not in room.participants))
            time.sleep(1.5)
            # ...A's tune backwards
            self.assertEqual(b_ears.notes(), [meet.TUNE_SCALE[n][0] for n in reversed(m.tune_of(1, 11))])
            self.assertFalse(room.closed)
            self.assertEqual(m.contact_room.get(11), room.id)  # A can call back in
            left_msg = [c_[0][3] for c_ in mock_send.call_args_list if c_[0][2] == 111]
            self.assertTrue(left_msg and "You left the meeting" in left_msg[-1], left_msg)
            self.assertIn(f"/join_{room.id}", left_msg[-1])

            # B and C leave -> the room is empty and closes after the idle time
            m.on_call_ended(Ev(kind="CallEnded", msg_id=msg_b))
            m.on_call_ended(Ev(kind="CallEnded", msg_id=702))
            self.assertTrue(self._wait(lambda: not room.participants))
            self.assertIsNotNone(room.empty_since)
            m.reap(time.time() + config.MEET_IDLE_MINUTES * 60 + 1)
            self.assertTrue(room.closed)
        finally:
            for peer in (a, b, c):
                self.client_loop.run(peer.close(), timeout=10)

        # the bot never ended the calls itself (everyone hung up) and deleted
        # every call message (they carry the participants' IP addresses)
        self.bot.rpc.end_call.assert_not_called()
        self._wait(lambda: self.bot.rpc.delete_messages.call_count >= 3, timeout=10)
        deleted = sorted(c_[0][1][0] for c_ in self.bot.rpc.delete_messages.call_args_list)
        self.assertEqual(deleted, sorted([701, 702, msg_b]))

    @patch("meet.dc_helpers._send")
    def test_unanswered_join_frees_the_place(self, mock_send):
        m = self.manager()
        room = m.create_room(owner=11)
        with patch.object(config, "MEET_RING_SECONDS", 1):
            m.join(1, 12, room.id)
            self.assertTrue(self._wait(lambda: 112 in self.placed))
            self.assertTrue(self._wait(lambda: self.bot.rpc.end_call.called, timeout=10))
        msg_id = self.placed[112][0]
        self.bot.rpc.end_call.assert_called_with(1, msg_id)
        self.assertNotIn(12, room.participants)
        self.assertEqual(m.contact_room.get(12), room.id)  # can still call back in
        mock_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
