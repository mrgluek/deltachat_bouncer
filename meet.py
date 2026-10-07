"""Voice meetings: an audio bridge (MCU) for Delta Chat calls.

Delta Chat calls are 1:1 only, so a meeting is a star: every participant is
in a normal call with the bot, and the bot mixes. Every 20 ms the room's
mixer takes one decoded frame per participant, drops silent / muted ones
(noise gate with hangover, plus the app's mutedState), sums the rest and
sends each participant the sum minus their own voice (mix-minus). Joining
and leaving play the participant's tune, 5 notes derived from their key
fingerprint, so the others hear who came in.

Admin-controlled and off by default (/meets on|off) - it costs one Opus
decode + encode per participant. Capacity is a shared budget of
MEET_TOTAL_SLOTS (8) places across at most MEET_MAX_ROOMS (2) rooms: one room
can take all 8, two rooms get 4 each, and a second room is refused while the
first has more than 4 people.

/meet           create the chat's room (12-char unguessable base62 id); in a
                chat that has one, show its link again
/join_<id>      the bot calls you into the room; while the room is open you
/join <id>      can also simply call the bot and you land in it again
/meetclose      close the chat's room (its creator or the admin; admin: any id)
/meetnew        close it and start a new one with a new link
/meets on|off   admin switch (also 1/0); no argument shows the status
/radio [N|off]  list the stations / play station N in the chat's meeting / stop
/play [off]     reply to an audio or video message: play its sound in the meeting
/radioadd, /radiodel   admin: curate the station list

Background audio (meet_media.py) goes to everyone under the voices and drops
to MEET_MUSIC_DUCK while someone talks; a played file pauses the radio, which
resumes when the file ends.

A room belongs to the chat it was started in. A group's room is for that
group's members only (checked on /join, on calls back in and every 30 s
during the meeting); a room started in a private chat with the bot is open
to whoever has its link.

Rooms live in memory (a bot restart drops them) and close
MEET_IDLE_MINUTES (60) after the last participant left, at the latest
MEET_MAX_HOURS after creation. Nothing is recorded: audio exists only in
short in-memory queues. The bot does decrypt and mix everyone's audio, so a
meeting is not end-to-end encrypted between participants - every room and
join message says so.
"""
import collections
import hashlib
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
import meet_media
import state

rtc = calls.rtc

ID_ALPHABET = string.ascii_letters + string.digits
MEET_ID_RE = re.compile(r"^[A-Za-z0-9]{12}$")
GATE_RMS = 300          # int16 RMS below which a frame counts as silence
GATE_HOLD_FRAMES = 15   # keep mixing 300 ms after speech so word ends aren't cut
INBOX_MAX_BACKLOG = 4   # frames; drop older ones so a burst can't add latency
# Join tunes: every participant gets 5 notes of the C major pentatonic
# (any combination sounds fine) picked by a hash of their key fingerprint, so
# the others can tell by ear who joined; leaving plays the tune backwards.
TUNE_SCALE = (
    ("C4", 261.63), ("D4", 293.66), ("E4", 329.63), ("G4", 392.00), ("A4", 440.00),
    ("C5", 523.25), ("D5", 587.33), ("E5", 659.25), ("G5", 783.99), ("A5", 880.00),
)
TUNE_NOTES = 5
TUNE_NOTE_FRAMES = 7      # 140 ms per note
TUNE_LAST_FRAMES = 14     # the last note rings a little longer
TUNE_AMPLITUDE = 0.16

NOT_MEMBER = "❌ This meeting is only for members of the group it was started in."
REPLY_PRIVATELY_HINT = (
    "💡 Tapping the command posts it in the group. To keep the group quiet, long-press this "
    "message → Reply Privately, and send me the /join_… command there."
)

PRIVACY_NOTE = (
    "🔒 The bot does not record or store your voice. It mixes everyone's audio on its server, "
    "so a meeting is not end-to-end encrypted between participants, and other participants "
    "can record what they hear."
)


def new_meet_id() -> str:
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(12))


def meets_enabled() -> bool:
    return database.get_config("meets_enabled") == "1"


def tune_for(seed: str) -> tuple:
    """5 scale indexes from a hash of ``seed`` (a key fingerprint), no note twice in a row."""
    digest = hashlib.sha256(("bouncer-meet-tune:" + seed).encode()).digest()
    notes: list[int] = []
    for byte in digest:
        n = byte % len(TUNE_SCALE)
        if notes and n == notes[-1]:
            continue
        notes.append(n)
        if len(notes) == TUNE_NOTES:
            break
    return tuple(notes)


def tune_name(notes) -> str:
    return " ".join(TUNE_SCALE[n][0] for n in notes)


def tune_frames(notes, reverse: bool = False) -> list:
    """The tune as 20 ms frames: soft bell-like notes (sine + octave, decaying)."""
    seq = list(reversed(notes)) if reverse else list(notes)
    if not seq:
        return []
    rate, chunks = rtc.SAMPLE_RATE, []
    for i, n in enumerate(seq):
        frames = TUNE_LAST_FRAMES if i == len(seq) - 1 else TUNE_NOTE_FRAMES
        length = frames * rtc.FRAME_SAMPLES
        t = np.arange(length) / rate
        freq = TUNE_SCALE[n][1]
        wave = np.sin(2 * np.pi * freq * t) + 0.25 * np.sin(4 * np.pi * freq * t)
        env = np.exp(-t / (length / rate * 0.45))
        attack = int(0.008 * rate)
        env[:attack] *= np.linspace(0, 1, attack)
        env[-int(0.005 * rate):] *= np.linspace(1, 0, int(0.005 * rate))
        chunks.append(wave * env)
    pcm = np.concatenate(chunks) / 1.25 * TUNE_AMPLITUDE * 32767
    return [rtc.frame_from_pcm(pcm[i:i + rtc.FRAME_SAMPLES])
            for i in range(0, len(pcm), rtc.FRAME_SAMPLES)]


def _fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


# --------------------------------------------------------------------------
# media
# --------------------------------------------------------------------------


class MeetOutTrack(rtc.PacedAudioTrack if rtc else object):
    """What one participant hears: their mix, with join/leave tunes laid on top."""

    def __init__(self) -> None:
        super().__init__()
        self.prio: collections.deque = collections.deque()
        self.queue: collections.deque = collections.deque(maxlen=INBOX_MAX_BACKLOG + 2)

    def push(self, pcm: np.ndarray) -> None:
        self.queue.append(pcm)

    def play(self, frames: list) -> None:
        """Queue tune frames (AudioFrames or int16 arrays); they are mixed over
        the conversation instead of interrupting it."""
        for f in frames:
            self.prio.append(f if isinstance(f, np.ndarray) else f.to_ndarray().reshape(-1))

    async def recv(self):
        await self.pace()
        tune = self.prio.popleft() if self.prio else None
        mix = self.queue.popleft() if self.queue else None
        if tune is not None and mix is not None:
            frame = rtc.frame_from_pcm(_clip(tune.astype(np.int32) + mix))
        elif tune is not None or mix is not None:
            frame = rtc.frame_from_pcm(tune if tune is not None else mix)
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
    tune: tuple = ()
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
    chat_id: int = 0          # the chat it was started in (and belongs to)
    group: bool = False       # group room: members of chat_id only
    accid: int = 1
    created_at: float = field(default_factory=time.time)
    participants: dict = field(default_factory=dict)  # contact_id -> Participant
    empty_since: Optional[float] = None
    closed: bool = False
    background: object = None              # meet_media.BackgroundSource playing in the room
    radio_id: Optional[int] = None         # the station to play / resume after a file
    duck: float = 1.0                      # current music gain factor (ducking)

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
        self.tunes: dict[int, tuple] = {}         # contact_id -> join tune (memory only)
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

    def create_room(self, owner: int, chat_id: int = 0, group: bool = False, accid: int = 1) -> Room:
        with self._lock:
            room = Room(id=new_meet_id(), owner=owner, chat_id=chat_id, group=group, accid=accid)
            self.rooms[room.id] = room
            self.contact_room[owner] = room.id
        self.loop.submit(self._mix(room))
        config.logger.info(f"Meet {room.id[:4]}…: created by contact {owner} "
                           f"in {'group' if group else 'private'} chat {chat_id}")
        return room

    def room_for_chat(self, chat_id: int) -> Optional[Room]:
        with self._lock:
            return next((r for r in self.rooms.values() if r.chat_id == chat_id and not r.closed), None)

    def find_room(self, ref: str) -> Optional[Room]:
        """Room by full id or a unique prefix of at least 4 characters (as /meets shows)."""
        with self._lock:
            if ref in self.rooms:
                return self.rooms[ref]
            hits = [r for rid, r in self.rooms.items() if len(ref) >= 4 and rid.startswith(ref)]
            return hits[0] if len(hits) == 1 else None

    def is_member(self, room: Room, contact_id: int) -> bool:
        """May this contact be in the room? Group rooms: members of their group only."""
        if not room.group:
            return True
        try:
            return contact_id in self.bot.rpc.get_chat_contacts(room.accid, room.chat_id)
        except Exception as e:
            config.logger.warning(f"Meet {room.id[:4]}…: member check failed: {e}")
            return False

    def check_members(self) -> None:
        """Drop participants (and call-back routes) of people who left the room's group."""
        for room in list(self.rooms.values()):
            if not room.group:
                continue
            with self._lock:
                parts = list(room.participants.values())
                routed = [c for c, rid in self.contact_room.items() if rid == room.id]
            if not parts and not routed:
                continue
            try:
                members = set(self.bot.rpc.get_chat_contacts(room.accid, room.chat_id))
            except Exception as e:
                config.logger.warning(f"Meet {room.id[:4]}…: member check failed: {e}")
                continue
            with self._lock:
                for cid in routed:
                    if cid not in members and self.contact_room.get(cid) == room.id:
                        self.contact_room.pop(cid, None)
            for p in parts:
                if p.contact_id not in members:
                    self.leave(p, "not-member")

    def close_room(self, room: Room, reason: str) -> None:
        with self._lock:
            if room.closed:
                return
            room.closed = True
            self.rooms.pop(room.id, None)
            for cid in [c for c, rid in self.contact_room.items() if rid == room.id]:
                self.contact_room.pop(cid, None)
            members = list(room.participants.values())
            background, room.background, room.radio_id = room.background, None, None
        if background is not None:
            background.stop()
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
                self.check_members()
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

    def tune_of(self, accid: int, contact_id: int) -> tuple:
        """The contact's join tune, from their key fingerprint (the address if
        there is none): the same in every meeting and on every bot restart."""
        tune = self.tunes.get(contact_id)
        if tune is None:
            seed = None
            try:
                fp = dc_helpers._get_contact_fingerprint(self.bot, accid, contact_id)
                if isinstance(fp, str) and fp:
                    seed = "fp:" + fp.split(",")[0]
            except Exception:
                pass
            if seed is None:
                try:
                    seed = "addr:" + str(self.bot.rpc.get_contact(accid, contact_id).address).lower()
                except Exception:
                    seed = f"contact:{contact_id}"
            tune = self.tunes[contact_id] = tune_for(seed)
        return tune

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
        if not self.is_member(room, contact_id):
            return NOT_MEMBER
        with self._lock:
            if room.closed:
                return f"❌ No such meeting, or it has closed: {room_id}"
            self.contact_room[contact_id] = room_id
            n = len(room.participants) + (0 if current else 1)
        threading.Thread(target=self._safe, args=(self._call_out, accid, contact_id, room),
                         name="meet-call-out", daemon=True).start()
        return (f"📞 Calling you into the meeting now ({n}/{cap} in the room). "
                f"If you miss it, just call me back while the room is open.\n"
                f"{self.tune_line(accid, contact_id)}\n\n{PRIVACY_NOTE}")

    def tune_line(self, accid: int, contact_id: int) -> str:
        return (f"🎵 Your join tune: {tune_name(self.tune_of(accid, contact_id))} — the others hear it "
                f"when you come in (backwards when you leave). It comes from your key fingerprint, "
                f"so it is the same in every meeting.")

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
        self.tune_of(accid, contact_id)
        with self._lock:
            if room.closed or len(room.participants) >= self.room_capacity():
                return None
            p = Participant(accid=accid, contact_id=contact_id, chat_id=chat_id, room=room, outgoing=outgoing)
            p.tune = self.tunes.get(contact_id, ())
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
        if room is not None and not self.is_member(room, contact_id):
            with self._lock:
                if self.contact_room.get(contact_id) == room.id:
                    self.contact_room.pop(contact_id, None)
            calls._end_call(rpc, accid, msg_id)
            dc_helpers._send(self.bot, accid, chat_id, NOT_MEMBER)
            return
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
        tune = tune_frames(p.tune)
        p.out.play(tune)  # you hear your own tune too: that is how the others hear you join
        for o in others:
            o.out.play(tune)
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
        if p.connected and others:
            tune = tune_frames(p.tune, reverse=True)
            for o in others:
                o.out.play(tune)
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
                if reason in ("closed", "renewed", "turned-off", "max-lifetime", "not-member"):
                    tail = ""
                elif room.closed:
                    tail = "The meeting has closed."
                else:
                    tail = (f"The room stays open for {config.MEET_IDLE_MINUTES} minutes after the last "
                            f"person leaves: call me or tap /join_{room.id} to get back in.")
                why = {"media-lost": " (connection lost)", "turned-off": " (meetings were turned off)",
                       "max-lifetime": " (maximum meeting length reached)",
                       "closed": " (the meeting was closed)",
                       "renewed": " (the meeting was closed and restarted with a new link)",
                       "not-member": " (you are no longer in the group this meeting belongs to)",
                       }.get(reason, "")
                dc_helpers._send(self.bot, accid, p.chat_id,
                                 f"📞 You left the meeting after {took}{why}. {tail}".rstrip())

    # -- mixing ------------------------------------------------------------

    def mix_tick(self, room: Room) -> None:
        """One 20 ms step: pull a frame per participant, gate, mix-minus, add the
        background audio (ducked under voices), push."""
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
        music = self._music_frame(room, bool(active)) if parts else None
        if not active and music is None:
            return
        mixes = mix_frames(active)
        total = mixes.pop("*", None)
        for p in parts:
            voice = mixes.get(p.contact_id, total)
            if p.contact_id in active and len(active) == 1:
                voice = None  # only they are talking: nothing to hear from the others
            if voice is None and music is None:
                continue
            if voice is None:
                out = music
            elif music is None:
                out = voice
            else:
                out = voice + music
            p.out.push(_clip(out))

    def _music_frame(self, room: Room, voices: bool):
        """The background audio's next frame at its volume, ducked while someone talks."""
        bg = room.background
        pcm = bg.read() if bg is not None else None
        if pcm is None:
            return None
        target = config.MEET_MUSIC_DUCK if voices else 1.0
        start = room.duck
        # duck within ~60 ms, come back over ~0.7 s
        rate = 0.35 if target < start else 0.03
        room.duck = start + (target - start) * rate
        gain = np.linspace(start, room.duck, pcm.size, dtype=np.float32) * config.MEET_MUSIC_VOLUME
        return (pcm.astype(np.float32) * gain).astype(np.int32)

    # -- background audio ------------------------------------------------------

    def _listening(self, room: Room):
        def listening() -> bool:
            with self._lock:
                return not room.closed and any(p.connected for p in room.participants.values())
        return listening

    def _set_background(self, room: Room, source) -> None:
        with self._lock:
            if room.closed:
                old = source
            else:
                old, room.background = room.background, source
                room.duck = 1.0
        if old is not None:
            old.stop()

    def start_radio(self, room: Room, station: dict):
        """Play a station in the room (replacing whatever plays). Returns the source."""
        src = meet_media.BackgroundSource(
            station["url"], "radio", station["name"], listening=self._listening(room),
            on_end=lambda s, err: self._background_ended(room, s, err), radio_id=station["id"])
        with self._lock:
            room.radio_id = station["id"]
        self._set_background(room, src.start())
        config.logger.info(f"Meet {room.id[:4]}…: radio #{station['id']} on")
        return src

    def play_file(self, room: Room, path: str, title: str):
        """Play a file in the room; a running station pauses and resumes afterwards."""
        src = meet_media.BackgroundSource(
            path, "file", title, listening=self._listening(room),
            on_end=lambda s, err: self._background_ended(room, s, err))
        self._set_background(room, src.start())
        config.logger.info(f"Meet {room.id[:4]}…: playing a file")
        return src

    def stop_background(self, room: Room, only_file: bool = False) -> Optional[str]:
        """Stop the room's background audio. With ``only_file`` just a playing file
        (the station, if any, resumes). Returns what was stopped: radio/file/None."""
        with self._lock:
            bg = room.background
            kind = bg.kind if bg is not None else None
            if only_file and kind != "file":
                return None
            if not only_file:
                room.radio_id = None
        self._set_background(room, None)
        if only_file:
            self._resume_radio(room)
        return kind

    def _resume_radio(self, room: Room) -> None:
        with self._lock:
            radio_id = room.radio_id if not room.closed and room.background is None else None
        if radio_id is None:
            return
        station = database.get_radio_stream(radio_id)
        if station is None:  # deleted meanwhile
            with self._lock:
                room.radio_id = None
            return
        self.start_radio(room, station)

    def _background_ended(self, room: Room, src, error: Optional[str]) -> None:
        with self._lock:
            if room.background is not src or room.closed:
                return
            room.background = None
            if src.kind == "radio" and error:
                room.radio_id = None
        # errors before the audio started are answered by the command that started it
        if error and src.started.is_set() and src.frames_out:
            what = "📻 The radio stopped" if src.kind == "radio" else f"⚠️ Playing {src.title} stopped"
            dc_helpers._send(self.bot, room.accid, room.chat_id, f"{what}: {error}")
        if src.kind == "file":
            self._resume_radio(room)

    def stations_removed(self, station_id: int) -> None:
        for room in list(self.rooms.values()):
            with self._lock:
                playing = room.background is not None and getattr(room.background, "radio_id", None) == station_id
                if room.radio_id == station_id:
                    room.radio_id = None
            if playing:
                self._set_background(room, None)

    def background_line(self, room: Room) -> str:
        bg = room.background
        if bg is None:
            return ""
        if bg.kind == "radio":
            return f"📻 {bg.radio_id}. {bg.title}"
        return f"▶️ {bg.title}"

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
            kind = "group" if r.group else "private"
            bg = self.background_line(r)
            lines.append(f"• {r.id[:4]}… ({kind}) — {n}/{self.room_capacity()} connected, "
                         f"open {_fmt_duration(now - r.created_at)}{idle}" + (f" — {bg}" if bg else ""))
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


def _chat_kind(bot, accid, chat_id) -> str:
    """'private' (1:1 with the bot), 'group', or something meetings don't support."""
    try:
        chat = bot.rpc.get_basic_chat_info(accid, chat_id)
        kind = chat.get("chat_type") if isinstance(chat, dict) else getattr(chat, "chat_type", None)
    except Exception:
        return "unknown"
    return {"Single": "private", "Group": "group"}.get(str(kind), "other")


def _invite_link() -> str:
    link = state.get_bot_invite_link() or ""
    return link if link.startswith("https://") else ""


def _send_room_link(bot, accid, manager, room, from_id, created: bool) -> None:
    cap = manager.room_capacity()
    head = "📞 **Voice meeting created**" if created else "📞 **This chat's voice meeting**"
    lines = [
        head,
        f"Join: /join_{room.id}",
        "Tap it and I will call you. If you hang up, call me any time while the room is open to get back in. "
        f"Up to {cap} people, voice only.",
    ]
    if room.group:
        lines.append("Only members of this group can join.")
        lines.append(REPLY_PRIVATELY_HINT)
    else:
        lines.append("Anyone you give the /join_… command to can join, so share it only privately. "
                     "Forward them the next message.")
    lines.append(f"The room closes {config.MEET_IDLE_MINUTES} minutes after the last person leaves; "
                 f"its creator can close it with /meetclose or swap the link with /meetnew.")
    lines.append("🎵 Background audio from this chat: /radio lists the stations, and replying /play to an "
                 "audio or video message plays its sound in the meeting.")
    if created:
        lines.append(manager.tune_line(accid, from_id))
    lines += ["", PRIVACY_NOTE]
    dc_helpers._send(bot, accid, room.chat_id, "\n".join(lines))
    if not room.group:
        link = _invite_link()
        add = f"add this bot: {link} and " if link else "add the bot I got this from and "
        dc_helpers._send(bot, accid, room.chat_id,
                         f"🎙️ Join my voice meeting: {add}send it this message: /join_{room.id}")


@config.dc_cli.on(events.NewMessage(command="/meet"))
def meet_command(bot, accid, event):
    msg = event.msg
    if not meets_enabled() or rtc is None:
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Voice meetings are disabled on this bot.")
        return
    kind = _chat_kind(bot, accid, msg.chat_id)
    if kind not in ("private", "group"):
        dc_helpers._send(bot, accid, msg.chat_id,
                         "❌ Meetings can be started in a group or in a private chat with me.")
        return
    manager = get_manager(bot)
    room = manager.room_for_chat(msg.chat_id)
    if room is not None:  # a chat has one room: show it again (never another chat's)
        if room.group and not manager.is_member(room, msg.from_id):
            dc_helpers._send(bot, accid, msg.chat_id, NOT_MEMBER)
            return
        _send_room_link(bot, accid, manager, room, msg.from_id, created=False)
        return
    owned = [r for r in list(manager.rooms.values()) if r.owner == msg.from_id]
    if len(owned) >= config.MEET_MAX_ROOMS_PER_USER:
        dc_helpers._send(bot, accid, msg.chat_id, (
            "ℹ️ You already have an open meeting in another chat. Close it first: send /meetclose "
            "in that chat or to me privately."))
        return
    ok, why = manager.can_create_room()
    if not ok:
        dc_helpers._send(bot, accid, msg.chat_id, why)
        return
    room = manager.create_room(msg.from_id, chat_id=msg.chat_id, group=(kind == "group"), accid=accid)
    _send_room_link(bot, accid, manager, room, msg.from_id, created=True)


def _target_room(bot, accid, manager, msg, arg: str, is_admin: bool):
    """(room, error) for /meetclose and /meetnew: an id (admin, or your own room),
    else this chat's room, else - in a private chat with me - the room you started."""
    if arg:
        room = manager.find_room(arg)
        if room is None or not (is_admin or room.owner == msg.from_id):
            return None, f"❌ No open meeting {arg} that you can manage."
        return room, ""
    room = manager.room_for_chat(msg.chat_id)
    if room is None and _chat_kind(bot, accid, msg.chat_id) == "private":
        owned = [r for r in list(manager.rooms.values()) if r.owner == msg.from_id]
        room = owned[0] if owned else None
    if room is None:
        return None, "ℹ️ There is no open meeting here."
    if not (is_admin or room.owner == msg.from_id):
        return None, "❌ Only the person who started this meeting (or the bot admin) can do that."
    return room, ""


@config.dc_cli.on(events.NewMessage(command="/meetclose"))
def meetclose_command(bot, accid, event):
    msg = event.msg
    if state.meet_manager is None or rtc is None:
        dc_helpers._send(bot, accid, msg.chat_id, "ℹ️ There is no open meeting here.")
        return
    manager = state.meet_manager
    is_admin = dc_helpers._is_dc_admin(bot, accid, msg.from_id)
    room, err = _target_room(bot, accid, manager, msg, (event.payload or "").strip(), is_admin)
    if room is None:
        dc_helpers._send(bot, accid, msg.chat_id, err)
        return
    n = len(room.participants)
    by_admin = room.owner != msg.from_id
    manager.close_room(room, "closed")
    config.logger.info(f"Meet {room.id[:4]}…: closed by {'admin' if by_admin else 'its creator'}")
    dc_helpers._send(bot, accid, msg.chat_id,
                     f"✅ Meeting {room.id[:4]}… closed" + (f", {n} participant(s) disconnected." if n else "."))
    if by_admin and room.chat_id and room.chat_id != msg.chat_id:
        dc_helpers._send(bot, accid, room.chat_id, "📞 This chat's voice meeting was closed by the bot admin.")


@config.dc_cli.on(events.NewMessage(command="/meetnew"))
def meetnew_command(bot, accid, event):
    msg = event.msg
    if not meets_enabled() or rtc is None or state.meet_manager is None:
        dc_helpers._send(bot, accid, msg.chat_id, "ℹ️ There is no open meeting here.")
        return
    manager = state.meet_manager
    is_admin = dc_helpers._is_dc_admin(bot, accid, msg.from_id)
    room, err = _target_room(bot, accid, manager, msg, (event.payload or "").strip(), is_admin)
    if room is None:
        dc_helpers._send(bot, accid, msg.chat_id, err)
        return
    manager.close_room(room, "renewed")
    new = manager.create_room(room.owner, chat_id=room.chat_id, group=room.group, accid=room.accid)
    config.logger.info(f"Meet {room.id[:4]}…: renewed as {new.id[:4]}…")
    if room.chat_id != msg.chat_id:
        dc_helpers._send(bot, accid, msg.chat_id, "✅ Meeting restarted; the new link went to the meeting's chat.")
    _send_room_link(bot, accid, manager, new, room.owner, created=True)


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


# --------------------------------------------------------------------------
# background audio: /radio, /play and the admin's station list
# --------------------------------------------------------------------------

NO_ROOM = "ℹ️ There is no open meeting in this chat. Start one with /meet."
RADIO_OFF_WORDS = ("0", "off", "stop")


def _station_line(st: dict) -> str:
    from urllib.parse import urlparse

    host = urlparse(st["url"]).hostname or ""
    return f"{st['id']}. {st['name']}" + (f" ({host})" if host and host not in st["name"] else "")


def _room_for_command(bot, accid, msg) -> tuple[Optional[MeetManager], Optional[Room], str]:
    """The chat's open room for /radio N, /play - or (None, None, why not)."""
    if not meets_enabled() or rtc is None:
        return None, None, "❌ Voice meetings are disabled on this bot."
    manager = get_manager(bot)
    room = manager.room_for_chat(msg.chat_id)
    if room is None:
        return manager, None, NO_ROOM
    if room.group and not manager.is_member(room, msg.from_id):
        return manager, None, NOT_MEMBER
    return manager, room, ""


def radio_list_text(chat_room: Optional[Room], manager: Optional[MeetManager], is_admin: bool) -> str:
    stations = database.get_radio_streams()
    lines = ["📻 **Meeting radio**"]
    if stations:
        lines += [_station_line(st) for st in stations]
    else:
        lines.append("No stations yet.")
    if chat_room is not None and manager is not None:
        now = manager.background_line(chat_room)
        lines.append("")
        lines.append(f"Now in this chat's meeting: {now}" if now else "Nothing is playing in this chat's meeting.")
    lines.append("")
    lines.append("/radio <number> plays a station in this chat's meeting (quieter while someone talks), "
                 "/radio off stops it. Reply /play to an audio or video message to play its sound instead.")
    if is_admin:
        lines.append("Admin: /radioadd <url> [name] adds a station, /radiodel <number> removes one. "
                     "Only add streams you may rebroadcast.")
    return "\n".join(lines)


@config.dc_cli.on(events.NewMessage(command="/radio"))
def radio_command(bot, accid, event):
    msg = event.msg
    arg = (event.payload or "").strip().lower().lstrip("#")
    if not arg:
        manager = state.meet_manager if rtc is not None else None
        room = manager.room_for_chat(msg.chat_id) if manager is not None else None
        is_admin = dc_helpers._is_dc_admin(bot, accid, msg.from_id)
        dc_helpers._send(bot, accid, msg.chat_id, radio_list_text(room, manager, is_admin))
        return
    if arg not in RADIO_OFF_WORDS and not arg.isdigit():
        dc_helpers._send(bot, accid, msg.chat_id, "Usage: /radio (list), /radio <number>, /radio off")
        return
    manager, room, why = _room_for_command(bot, accid, msg)
    if room is None:
        dc_helpers._send(bot, accid, msg.chat_id, why)
        return
    if arg in RADIO_OFF_WORDS:
        stopped = manager.stop_background(room)
        dc_helpers._send(bot, accid, msg.chat_id,
                         "📻 Background audio off." if stopped else "ℹ️ Nothing is playing in this chat's meeting.")
        return
    station = database.get_radio_stream(int(arg))
    if station is None:
        dc_helpers._send(bot, accid, msg.chat_id, f"❌ There is no station {arg}. /radio lists them.")
        return
    dc_helpers._react(bot, accid, msg.id, "⏳")
    threading.Thread(target=_start_radio_and_report, args=(bot, accid, msg, manager, room, station),
                     name="meet-radio-start", daemon=True).start()


def _start_radio_and_report(bot, accid, msg, manager, room, station) -> None:
    src = manager.start_radio(room, station)
    if src.wait_started():
        dc_helpers._react(bot, accid, msg.id, "📻")
        dc_helpers._send(bot, accid, msg.chat_id, (
            f"📻 Now playing {_station_line(station)} in this chat's meeting, quieter while someone talks. "
            f"/radio off stops it."))
        return
    with manager._lock:
        if room.background is src:
            room.background = None
        if room.radio_id == station["id"]:
            room.radio_id = None
    src.stop()
    dc_helpers._react(bot, accid, msg.id, "❌")
    dc_helpers._send(bot, accid, msg.chat_id,
                     f"❌ Station {station['id']} is not playing: {src.error or 'no audio within 15 s'}")


@config.dc_cli.on(events.NewMessage(command="/radioadd"))
def radioadd_command(bot, accid, event):
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can add stations.")
        return
    parts = (event.payload or "").strip().split(maxsplit=1)
    url = parts[0] if parts else ""
    name = parts[1].strip() if len(parts) > 1 else ""
    if not meet_media.valid_stream_url(url):
        dc_helpers._send(bot, accid, msg.chat_id, "Usage: /radioadd <http(s) stream URL> [name]")
        return
    existing = database.get_radio_stream_by_url(url)
    if existing:
        dc_helpers._send(bot, accid, msg.chat_id, f"ℹ️ Already in the list: {_station_line(existing)}")
        return
    dc_helpers._react(bot, accid, msg.id, "⏳")
    threading.Thread(target=_probe_and_add, args=(bot, accid, msg, url, name),
                     name="meet-radio-probe", daemon=True).start()


def _probe_and_add(bot, accid, msg, url: str, name: str) -> None:
    try:
        info = meet_media.probe(url, "radio")
    except Exception as e:
        dc_helpers._react(bot, accid, msg.id, "❌")
        dc_helpers._send(bot, accid, msg.chat_id, f"❌ Not added, the stream does not play: {meet_media._short(e)}")
        return
    from urllib.parse import urlparse

    title = (name or info.get("name") or urlparse(url).hostname or url)[:80]
    station_id = database.add_radio_stream(url, title)
    ch = {1: "mono", 2: "stereo"}.get(info.get("channels"), f"{info.get('channels')} ch")
    dc_helpers._react(bot, accid, msg.id, "✅")
    dc_helpers._send(bot, accid, msg.chat_id, (
        f"✅ Added {station_id}. {title} ({info.get('codec')}, {info.get('rate', 0) / 1000:g} kHz {ch}). "
        f"Play it in a meeting's chat with /radio {station_id}."))


@config.dc_cli.on(events.NewMessage(command="/radiodel"))
def radiodel_command(bot, accid, event):
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can remove stations.")
        return
    arg = (event.payload or "").strip().lstrip("#")
    if not arg.isdigit():
        dc_helpers._send(bot, accid, msg.chat_id, "Usage: /radiodel <number> (see /radio)")
        return
    station = database.get_radio_stream(int(arg))
    if station is None or not database.delete_radio_stream(int(arg)):
        dc_helpers._send(bot, accid, msg.chat_id, f"❌ There is no station {arg}.")
        return
    if state.meet_manager is not None:
        state.meet_manager.stations_removed(station["id"])
    dc_helpers._send(bot, accid, msg.chat_id, f"🗑️ Removed {_station_line(station)}.")


@config.dc_cli.on(events.NewMessage(command="/play"))
def play_command(bot, accid, event):
    msg = event.msg
    arg = (event.payload or "").strip().lower()
    manager, room, why = _room_for_command(bot, accid, msg)
    if room is None:
        dc_helpers._send(bot, accid, msg.chat_id, why)
        return
    if arg in RADIO_OFF_WORDS:
        stopped = manager.stop_background(room, only_file=True)
        resumed = " The radio continues." if room.radio_id is not None else ""
        dc_helpers._send(bot, accid, msg.chat_id,
                         f"⏹ Stopped.{resumed}" if stopped else "ℹ️ No file is playing in this chat's meeting.")
        return
    target = msg if _has_attachment(msg) else None
    quote = getattr(msg, "quote", None)
    if target is None and isinstance(quote, dict):
        quoted_id = quote.get("message_id") or quote.get("messageId")
        if quoted_id:
            try:
                target = bot.rpc.get_message(accid, quoted_id)
            except Exception as e:
                config.logger.warning(f"/play: could not load the quoted message: {e}")
    if target is None:
        dc_helpers._send(bot, accid, msg.chat_id, (
            "Usage: reply /play to an audio or video message (or send one with /play as its caption) "
            "to play its sound in this chat's meeting. /play off stops it."))
        return
    dc_helpers._react(bot, accid, msg.id, "⏳")
    threading.Thread(target=_play_and_report, args=(bot, accid, msg, manager, room, target),
                     name="meet-play", daemon=True).start()


def _has_attachment(msg) -> bool:
    return bool(getattr(msg, "file", None) or getattr(msg, "file_bytes", 0))


def _play_and_report(bot, accid, msg, manager, room, target) -> None:
    def fail(text):
        dc_helpers._react(bot, accid, msg.id, "❌")
        dc_helpers._send(bot, accid, msg.chat_id, text)

    info = dc_helpers._get_msg_file_info(bot, accid, target)  # downloads it if needed
    if not info:
        fail("❌ That message has no file I could download.")
        return
    try:
        details = meet_media.probe(info["path"], "file")
    except Exception:
        fail("❌ That file has no sound I can play (send an audio or video file).")
        return
    title = (info.get("filename") or "the file")[:80]
    if room.closed:
        fail("❌ The meeting has closed.")
        return
    src = manager.play_file(room, info["path"], title)
    if not src.wait_started():
        manager.stop_background(room, only_file=True)
        fail(f"❌ Could not play {title}: {src.error or 'no sound'}")
        return
    length = meet_media.format_duration(details.get("duration"))
    after = " The radio continues afterwards." if room.radio_id is not None else ""
    dc_helpers._react(bot, accid, msg.id, "▶️")
    dc_helpers._send(bot, accid, msg.chat_id, (
        f"▶️ Playing {title}" + (f" ({length})" if length else "") +
        f" in this chat's meeting. /play off stops it.{after}"))
