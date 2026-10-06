"""Voice meetings: an audio bridge (MCU) for Delta Chat calls.

Delta Chat calls are 1:1 only, so a meeting is a star: every participant is
in a normal call with the bot, and the bot mixes. Every 20 ms the room's
mixer takes one decoded frame per participant, drops silent / muted ones
(noise gate with hangover, plus the app's mutedState), sums the rest and
sends each participant the sum minus their own voice (mix-minus).

Admin-controlled and off by default (/meets on|off) - it costs one Opus
decode + encode per participant. Capacity is a shared budget of
MEET_TOTAL_SLOTS (8) places across at most MEET_MAX_ROOMS (2) rooms: one room
can take all 8, two rooms get 4 each, and a second room is refused while the
first has more than 4 people.

/meet           create a room (12-char unguessable base62 id)
/join_<id>      the bot calls you into the room; while the room is open you
/join <id>      can also simply call the bot and you land in it again
/meets on|off   admin switch (also 1/0); no argument shows the status

Rooms live in memory (a bot restart drops them) and close
MEET_IDLE_MINUTES (60) after the last participant left, at the latest
MEET_MAX_HOURS after creation. Nothing is recorded: audio exists only in
short in-memory queues. The bot does decrypt and mix everyone's audio, so a
meeting is not end-to-end encrypted between participants - every room and
join message says so.
"""
import collections
import re
import secrets
import string
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

try:
    import numpy as np
except ImportError:  # no call support installed
    np = None
from deltachat2 import events

import calls
import config
import database
import dc_helpers
import state

rtc = calls.rtc

ID_ALPHABET = string.ascii_letters + string.digits
MEET_ID_RE = re.compile(r"^[A-Za-z0-9]{12}$")
GATE_RMS = 300          # int16 RMS below which a frame counts as silence
GATE_HOLD_FRAMES = 15   # keep mixing 300 ms after speech so word ends aren't cut
INBOX_MAX_BACKLOG = 4   # frames; drop older ones so a burst can't add latency
JOIN_TONES = ((330.0, 6), (None, 4), (440.0, 6))
LEAVE_TONES = ((440.0, 6), (None, 4), (330.0, 6))

PRIVACY_NOTE = (
    "🔒 The bot does not record or store your voice. It mixes everyone's audio on its server, "
    "so a meeting is not end-to-end encrypted between participants, and other participants "
    "can record what they hear."
)


def new_meet_id() -> str:
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(12))


def meets_enabled() -> bool:
    return database.get_config("meets_enabled") == "1"


def _tones(spec) -> list:
    frames = []
    for freq, n in spec:
        frames.extend(rtc.tone_frames(freq, n, amplitude=0.2))
    return frames


def _fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


# --------------------------------------------------------------------------
# media
# --------------------------------------------------------------------------


class MeetOutTrack(rtc.PacedAudioTrack if rtc else object):
    """What one participant hears: tones first, then their mix, else silence."""

    def __init__(self) -> None:
        super().__init__()
        self.prio: collections.deque = collections.deque()
        self.queue: collections.deque = collections.deque(maxlen=INBOX_MAX_BACKLOG + 2)

    def push(self, pcm: np.ndarray) -> None:
        self.queue.append(pcm)

    def play(self, frames: list) -> None:
        self.prio.extend(frames)

    async def recv(self):
        await self.pace()
        if self.prio:
            frame = self.prio.popleft()
        elif self.queue:
            frame = rtc.frame_from_pcm(self.queue.popleft())
        else:
            frame = rtc.silence_frame()
        return self.stamp(frame)


def mix_frames(frames: dict) -> dict:
    """Mix-minus: {key: int16 frame} of active speakers -> {key: mix without own voice}.

    Keys missing from the result get silence. Pure, unit tested.
    """
    if not frames:
        return {}
    total = np.zeros(rtc.FRAME_SAMPLES, dtype=np.int32)
    for pcm in frames.values():
        total += pcm
    return {key: total - pcm.astype(np.int32) for key, pcm in frames.items()} | {"*": total}


def _clip(pcm32: np.ndarray) -> np.ndarray:
    return np.clip(pcm32, -32768, 32767).astype(np.int16)


@dataclass
class Participant:
    accid: int
    contact_id: int
    chat_id: int
    room: "Room"
    out: object = None
    peer: object = None
    msg_id: Optional[int] = None
    outgoing: bool = False
    connected: bool = False
    joined_at: Optional[float] = None
    media_ended_at: Optional[float] = None
    left: bool = False
    muted: bool = False
    gate_hold: int = 0
    answer_sdp: Optional[str] = None
    answered: threading.Event = field(default_factory=threading.Event)
    inbox: collections.deque = field(default_factory=lambda: collections.deque(maxlen=25))
    resampler: object = None

    def receive(self, frame) -> None:
        """Decoded frame from the caller (call loop thread) -> mono 960-sample PCM."""
        for out in self.resampler.resample(frame):
            self.inbox.append(out.to_ndarray().reshape(-1).copy())


@dataclass
class Room:
    id: str
    owner: int
    created_at: float = field(default_factory=time.time)
    participants: dict = field(default_factory=dict)  # contact_id -> Participant
    empty_since: Optional[float] = None
    closed: bool = False

    def __post_init__(self):
        if self.empty_since is None:
            self.empty_since = self.created_at


# --------------------------------------------------------------------------
# manager
# --------------------------------------------------------------------------


class MeetManager:
    def __init__(self, bot):
        import av

        self._av = av
        self.bot = bot
        self.loop = rtc.CallLoop("bouncer-meets")
        self.rooms: dict[str, Room] = {}
        self.contact_room: dict[int, str] = {}   # who gets routed into which room
        self.by_msg: dict[int, Participant] = {}
        self._lock = threading.RLock()
        self._reaper = threading.Thread(target=self._reap_loop, name="meet-reaper", daemon=True)
        self._reaper.start()

    # -- capacity --------------------------------------------------------

    def room_capacity(self, n_rooms: Optional[int] = None) -> int:
        n = len(self.rooms) if n_rooms is None else n_rooms
        return min(config.MEET_MAX_PARTICIPANTS, config.MEET_TOTAL_SLOTS // max(1, n))

    def can_create_room(self) -> tuple[bool, str]:
        with self._lock:
            if len(self.rooms) >= config.MEET_MAX_ROOMS:
                return False, (f"❌ The maximum of {config.MEET_MAX_ROOMS} meetings at once is reached. "
                               f"Rooms free up {config.MEET_IDLE_MINUTES} minutes after their last participant leaves.")
            future_cap = self.room_capacity(len(self.rooms) + 1)
            busy = [r for r in self.rooms.values() if len(r.participants) > future_cap]
            if busy:
                return False, (f"❌ Not enough free places for another meeting right now: a meeting with "
                               f"{len(busy[0].participants)} people uses the bot's capacity "
                               f"({config.MEET_TOTAL_SLOTS} places in total).")
            return True, ""

    # -- rooms -------------------------------------------------------------

    def create_room(self, owner: int) -> Room:
        with self._lock:
            room = Room(id=new_meet_id(), owner=owner)
            self.rooms[room.id] = room
            self.contact_room[owner] = room.id
        self.loop.submit(self._mix(room))
        config.logger.info(f"Meet {room.id[:4]}…: created by contact {owner}")
        return room

    def close_room(self, room: Room, reason: str) -> None:
        with self._lock:
            if room.closed:
                return
            room.closed = True
            self.rooms.pop(room.id, None)
            for cid in [c for c, rid in self.contact_room.items() if rid == room.id]:
                self.contact_room.pop(cid, None)
            members = list(room.participants.values())
        for p in members:
            self.leave(p, reason)
        config.logger.info(f"Meet {room.id[:4]}…: closed ({reason})")

    def close_all(self, reason: str) -> None:
        for room in list(self.rooms.values()):
            self.close_room(room, reason)

    def _reap_loop(self) -> None:
        while True:
            time.sleep(30)
            try:
                self.reap()
            except Exception as e:
                config.logger.warning(f"Meet reaper: {e}")

    def reap(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        for room in list(self.rooms.values()):
            if room.participants:
                if now - room.created_at > config.MEET_MAX_HOURS * 3600:
                    self.close_room(room, "max-lifetime")
                continue
            if now - (room.empty_since or room.created_at) > config.MEET_IDLE_MINUTES * 60:
                self.close_room(room, "idle")

    # -- joining -----------------------------------------------------------

    def join(self, accid: int, contact_id: int, room_id: str) -> str:
        """/join: book a place, remember the room for call-backs, call the user."""
        with self._lock:
            room = self.rooms.get(room_id)
            if room is None or room.closed:
                return f"❌ No such meeting, or it has closed: {room_id}"
            current = room.participants.get(contact_id)
            if current is not None and current.connected:
                return "ℹ️ You are already in this meeting."
            cap = self.room_capacity()
            if current is None and len(room.participants) >= cap:
                return f"❌ This meeting is full ({cap} people)."
            self.contact_room[contact_id] = room_id
            n = len(room.participants) + (0 if current else 1)
        threading.Thread(target=self._safe, args=(self._call_out, accid, contact_id, room),
                         name="meet-call-out", daemon=True).start()
        return (f"📞 Calling you into the meeting now ({n}/{cap} in the room). "
                f"If you miss it, just call me back while the room is open.\n\n{PRIVACY_NOTE}")

    def wants_incoming(self, contact_id: int) -> bool:
        """Should an incoming call of this contact go into a meeting (not the echo)?"""
        if not meets_enabled():
            return False
        with self._lock:
            room = self.rooms.get(self.contact_room.get(contact_id, ""))
            return room is not None and not room.closed

    def _safe(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as e:
            config.logger.exception(f"Meet: {fn.__name__} failed: {e}")

    def _new_participant(self, accid: int, room: Room, contact_id: int, chat_id: int,
                         outgoing: bool) -> Optional[Participant]:
        """Reserve a place; replaces a previous attempt of the same contact."""
        with self._lock:
            old = room.participants.get(contact_id)
        if old is not None:
            self.leave(old, "replaced", notify=False)
        with self._lock:
            if room.closed or len(room.participants) >= self.room_capacity():
                return None
            p = Participant(accid=accid, contact_id=contact_id, chat_id=chat_id, room=room, outgoing=outgoing)
            p.out = MeetOutTrack()
            p.resampler = self._av.AudioResampler(format="s16", layout="mono",
                                                  rate=rtc.SAMPLE_RATE, frame_size=rtc.FRAME_SAMPLES)
            room.participants[contact_id] = p
            room.empty_since = None
            return p

    def _make_peer(self, p: Participant):
        ice = calls.echo_ice_servers(self.bot.rpc.ice_servers(p.accid))

        def on_muted(enabled):
            p.muted = not enabled

        p.peer = rtc.EchoPeer(ice, greeting=False, out_track=p.out,
                              on_audio=p.receive, on_muted=on_muted)
        return p.peer

    def _call_out(self, accid: int, contact_id: int, room: Room) -> None:
        rpc = self.bot.rpc
        chat_id = rpc.create_chat_by_contact_id(accid, contact_id)
        p = self._new_participant(accid, room, contact_id, chat_id, outgoing=True)
        if p is None:
            dc_helpers._send(self.bot, accid, chat_id, "❌ The meeting is full or has closed.")
            return
        try:
            peer = self._make_peer(p)
            offer = self.loop.run(peer.offer(), timeout=30)
            if p.left:  # replaced / room closed while gathering: leave() may have missed the new pc
                self.loop.run(peer.close(), timeout=10)
                return
            p.msg_id = int(rpc.place_outgoing_call(accid, chat_id, offer, False))
            with self._lock:
                self.by_msg[p.msg_id] = p
            if not p.answered.wait(config.MEET_RING_SECONDS) or p.left or not p.answer_sdp:
                self.leave(p, "not-answered", notify=False)
                return
            self.loop.run(peer.accept_answer(p.answer_sdp), timeout=10)
        except Exception as e:
            config.logger.warning(f"Meet {room.id[:4]}…: calling contact {contact_id} failed: {e}")
            self.leave(p, "error", notify=False)
            return
        self._run_participant(p)

    def on_incoming_call(self, accid: int, event, contact_id: int) -> None:
        """Called (in a worker thread) when wants_incoming(contact_id) was true."""
        rpc = self.bot.rpc
        msg_id, chat_id = int(event.msg_id), int(event.chat_id)
        with self._lock:
            room = self.rooms.get(self.contact_room.get(contact_id, ""))
        p = self._new_participant(accid, room, contact_id, chat_id, outgoing=False) if room else None
        if p is None:
            calls._end_call(rpc, accid, msg_id)
            dc_helpers._send(self.bot, accid, chat_id, "❌ The meeting is full or has closed.")
            return
        p.msg_id = msg_id
        with self._lock:
            self.by_msg[msg_id] = p
        try:
            peer = self._make_peer(p)
            answer = self.loop.run(peer.accept(event.place_call_info), timeout=30)
            if p.left:  # hung up while we were gathering: leave() may have missed the new pc
                self.loop.run(peer.close(), timeout=10)
                return
            rpc.accept_incoming_call(accid, msg_id, answer)
        except Exception as e:
            config.logger.warning(f"Meet {room.id[:4]}…: answering contact {contact_id} failed: {e}")
            self.leave(p, "error", notify=False)
            return
        self._run_participant(p)

    def on_outgoing_accepted(self, event) -> None:
        p = self.by_msg.get(int(event.msg_id))
        if p is not None:
            p.answer_sdp = event.get("accept_call_info")
            p.answered.set()

    def on_call_ended(self, event) -> None:
        p = self.by_msg.get(int(event.msg_id))
        if p is not None:
            p.answered.set()  # wakes a still-ringing outgoing call
            threading.Thread(target=self.leave, args=(p, "hangup"), daemon=True).start()

    def _run_participant(self, p: Participant) -> None:
        """Wait for media, announce the participant, then watch the connection."""
        connected = self.loop.run(p.peer.wait_connected(config.CALL_ECHO_CONNECT_TIMEOUT),
                                  timeout=config.CALL_ECHO_CONNECT_TIMEOUT + 5)
        if p.left:
            return
        if not connected:
            self.leave(p, "ice-failed")
            return
        with self._lock:
            p.connected = True
            p.joined_at = time.time()
            others = [o for o in p.room.participants.values() if o is not p and o.connected]
        p.out.play(_tones(JOIN_TONES))
        for o in others:
            o.out.play(_tones(JOIN_TONES))
        config.logger.info(f"Meet {p.room.id[:4]}…: contact {p.contact_id} joined "
                           f"({len(others) + 1} connected)")
        while not p.left:
            pc = p.peer.pc
            if pc is None or pc.connectionState in ("failed", "closed"):
                p.media_ended_at = time.time()
                grace_end = p.media_ended_at + config.CALL_ECHO_HANGUP_GRACE
                while not p.left and time.time() < grace_end:
                    time.sleep(0.2)
                if not p.left:
                    self.leave(p, "media-lost")
                return
            time.sleep(1)

    def leave(self, p: Participant, reason: str, notify: bool = True) -> None:
        with self._lock:
            if p.left:
                return
            p.left = True
            room = p.room
            if room.participants.get(p.contact_id) is p:
                room.participants.pop(p.contact_id)
                if not room.participants:
                    room.empty_since = time.time()
            if p.msg_id is not None:
                self.by_msg.pop(p.msg_id, None)
            others = [o for o in room.participants.values() if o.connected]
        p.answered.set()
        if p.connected:
            for o in others:
                o.out.play(_tones(LEAVE_TONES))
        if p.peer is not None:
            try:
                self.loop.run(p.peer.close(), timeout=10)
            except Exception:
                pass
        rpc = self.bot.rpc
        accid = p.accid
        if p.msg_id is not None:
            if reason != "hangup":
                calls._end_call(rpc, accid, p.msg_id)
            try:  # the call message carries the caller's SDP (their IP addresses)
                rpc.delete_messages(accid, [p.msg_id])
            except Exception:
                pass
        if p.connected:
            ended = p.media_ended_at or time.time()
            took = _fmt_duration(ended - (p.joined_at or ended))
            config.logger.info(f"Meet {room.id[:4]}…: contact {p.contact_id} left ({reason}) after {took}")
            if notify:
                if room.closed:
                    tail = "The meeting has closed."
                else:
                    tail = (f"The room stays open for {config.MEET_IDLE_MINUTES} minutes after the last "
                            f"person leaves: call me or tap /join_{room.id} to get back in.")
                why = {"media-lost": " (connection lost)", "turned-off": " (meetings were turned off)",
                       "max-lifetime": " (maximum meeting length reached)"}.get(reason, "")
                dc_helpers._send(self.bot, accid, p.chat_id,
                                 f"📞 You left the meeting after {took}{why}. {tail}")

    # -- mixing ------------------------------------------------------------

    def mix_tick(self, room: Room) -> None:
        """One 20 ms step: pull a frame per participant, gate, mix-minus, push."""
        with self._lock:
            parts = [p for p in room.participants.values() if p.connected]
        active = {}
        for p in parts:
            while len(p.inbox) > INBOX_MAX_BACKLOG:
                p.inbox.popleft()
            pcm = p.inbox.popleft() if p.inbox else None
            if pcm is None or p.muted:
                p.gate_hold = 0
                continue
            if rtc.rms(pcm.astype(np.float32)) >= GATE_RMS:
                p.gate_hold = GATE_HOLD_FRAMES
            elif p.gate_hold > 0:
                p.gate_hold -= 1
            else:
                continue
            active[p.contact_id] = pcm
        if not active:
            return
        mixes = mix_frames(active)
        total = mixes.pop("*")
        for p in parts:
            mix = mixes.get(p.contact_id, total)
            if p.contact_id in active and len(active) == 1:
                continue  # only they are talking: nothing to hear
            p.out.push(_clip(mix))

    async def _mix(self, room: Room) -> None:
        import asyncio

        next_t = time.monotonic()
        while not room.closed:
            next_t += rtc.FRAME_TIME
            delay = next_t - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -0.2:  # stalled: don't burst to catch up
                next_t = time.monotonic()
            try:
                self.mix_tick(room)
            except Exception as e:
                config.logger.warning(f"Meet {room.id[:4]}…: mixer error: {e}")

    def status_lines(self) -> list[str]:
        with self._lock:
            rooms = list(self.rooms.values())
        if not rooms:
            return ["No meetings right now."]
        lines = []
        now = time.time()
        for r in rooms:
            n = sum(1 for p in r.participants.values() if p.connected)
            idle = "" if r.participants else f", empty for {_fmt_duration(now - (r.empty_since or now))}"
            lines.append(f"• {r.id[:4]}… — {n}/{self.room_capacity()} connected, "
                         f"open {_fmt_duration(now - r.created_at)}{idle}")
        return lines


# --------------------------------------------------------------------------
# setup + commands
# --------------------------------------------------------------------------


def get_manager(bot) -> Optional[MeetManager]:
    if rtc is None:
        return None
    if state.meet_manager is None:
        state.meet_manager = MeetManager(bot)
    return state.meet_manager


@config.dc_cli.on(events.NewMessage(command="/meet"))
def meet_command(bot, accid, event):
    msg = event.msg
    if not meets_enabled() or rtc is None:
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Voice meetings are disabled on this bot.")
        return
    manager = get_manager(bot)
    owned = [r for r in list(manager.rooms.values()) if r.owner == msg.from_id]
    if len(owned) >= config.MEET_MAX_ROOMS_PER_USER:
        dc_helpers._send(bot, accid, msg.chat_id,
                         f"ℹ️ You already have an open meeting: /join_{owned[0].id}")
        return
    ok, why = manager.can_create_room()
    if not ok:
        dc_helpers._send(bot, accid, msg.chat_id, why)
        return
    room = manager.create_room(msg.from_id)
    cap = manager.room_capacity()
    dc_helpers._send(bot, accid, msg.chat_id, (
        f"📞 **Voice meeting created**\n"
        f"Join: /join_{room.id}\n"
        f"Tap it and I will call you. If you hang up, call me any time while the room is open to get back in. "
        f"Share the command with the others - up to {cap} people, voice only.\n"
        f"The room closes {config.MEET_IDLE_MINUTES} minutes after the last person leaves.\n\n"
        f"{PRIVACY_NOTE}"
    ))


@config.dc_cli.on(events.NewMessage(command="/join"))  # also /join_<id>: deltachat2 splits at "_"
def join_command(bot, accid, event):
    msg = event.msg
    if not meets_enabled() or rtc is None:
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Voice meetings are disabled on this bot.")
        return
    room_id = (event.payload or "").strip().split(" ")[0]
    if not MEET_ID_RE.match(room_id):
        dc_helpers._send(bot, accid, msg.chat_id, "Usage: /join <meeting id> (tap the /join_… link of a meeting)")
        return
    manager = get_manager(bot)
    reply = manager.join(accid, msg.from_id, room_id)
    if not reply.startswith("📞"):
        dc_helpers._send(bot, accid, msg.chat_id, reply)
        return
    # the call (and the notice) go to the private chat, also when /join was sent in a group
    private_chat = bot.rpc.create_chat_by_contact_id(accid, msg.from_id)
    dc_helpers._send(bot, accid, private_chat, reply)


@config.dc_cli.on(events.NewMessage(command="/meets"))
def meets_command(bot, accid, event):
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /meets.")
        return
    arg = (event.payload or "").strip().lower()
    if arg in ("on", "1"):
        if rtc is None:
            dc_helpers._send(bot, accid, msg.chat_id, "❌ cmcall/aiortc is not installed, meetings cannot run.")
            return
        database.set_config("meets_enabled", "1")
        if state.echo_call_manager is None:  # echo off: still let people call back into rooms
            who = {"everybody": "0", "contacts": "1"}.get(config.CALL_ECHO_WHO, "0")
            try:
                bot.rpc.set_config(accid, "who_can_call_me", who)
            except Exception as e:
                config.logger.warning(f"Could not set who_can_call_me: {e}")
    elif arg in ("off", "0"):
        database.set_config("meets_enabled", "0")
        if state.meet_manager is not None:
            state.meet_manager.close_all("turned-off")
    elif arg:
        dc_helpers._send(bot, accid, msg.chat_id, "Usage: /meets [on|off]")
        return
    lines = [f"📞 Voice meetings: {'✅ on' if meets_enabled() else '❌ off'}",
             f"Capacity: {config.MEET_TOTAL_SLOTS} places in up to {config.MEET_MAX_ROOMS} rooms "
             f"(max {config.MEET_MAX_PARTICIPANTS} per room); rooms close "
             f"{config.MEET_IDLE_MINUTES} min after the last person leaves."]
    if state.meet_manager is not None:
        lines += state.meet_manager.status_lines()
    dc_helpers._send(bot, accid, msg.chat_id, "\n".join(lines))
