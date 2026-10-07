"""Call testing: the echo call service and the /cmcall and /callstats commands.

Echo service
------------
Anyone can call the bot from a Delta Chat app. The bot answers, plays a short
two-tone greeting and then loops the caller's audio straight back (optionally
delayed by CALL_ECHO_DELAY), so the caller hears themselves. After the hangup
the bot sends a "📞 Echo call report" message into that chat: duration, media
path (direct / STUN / TURN relay), audio heard from the caller, packet loss,
jitter and round-trip time.

The WebRTC part is `cmcall.rtc.EchoPeer` (aiortc) from the cmcall package. All
aiortc objects live on one dedicated event loop thread (`rtc.CallLoop`) so bot
work can never starve call media. Core events arrive on the deltabot-cli event
thread; the hooks below only hand work off to worker threads.

/cmcall runs the `cmcall` CLI (relay-to-relay call test) as a subprocess, like
/cmping does with `cmping`, and formats its --json result.
"""
import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from deltachat2 import events

import config
import database
import dc_helpers
import state
import turn_names

try:
    from cmcall import rtc
    _RTC_IMPORT_ERROR = None
except Exception as _e:  # aiortc / cmcall not installed
    rtc = None
    _RTC_IMPORT_ERROR = _e

CALL_EVENT_KINDS = ("IncomingCall", "OutgoingCallAccepted", "CallEnded")
REPORT_PREFIX = "📞"  # cmcall --to waits for a message starting with this

PATH_LABELS = {
    "relay": "TURN relay",
    "relay/p2p": "TURN relay (one side)",
    "stun": "peer-to-peer via NAT (STUN)",
    "direct": "direct",
}


@dataclass
class EchoSession:
    accid: int
    msg_id: int
    chat_id: int
    contact_id: int
    peer: object
    started_at: float = field(default_factory=time.time)
    accepted_at: Optional[float] = None
    media_ended_at: Optional[float] = None  # when the media connection closed
    finished: bool = False
    end_reason: str = ""
    error: Optional[str] = None
    offer_relay_ips: list = field(default_factory=list)  # TURN servers in the caller's offer
    caller_domain: Optional[str] = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class EchoCallManager:
    """Answers incoming calls with an echo and reports on them."""

    def __init__(self, bot):
        self.bot = bot
        self.loop = rtc.CallLoop("bouncer-calls")
        self.sessions: dict[int, EchoSession] = {}
        self._lock = threading.Lock()

    # -- event entry points (deltabot-cli event thread: never block here) --

    def on_incoming_call(self, accid: int, event) -> None:
        threading.Thread(
            target=self._answer_safe, args=(accid, event), name="echo-call-answer", daemon=True
        ).start()

    def on_call_ended(self, accid: int, event) -> None:
        session = self.sessions.get(int(event.msg_id))
        if session is not None:
            threading.Thread(
                target=self.finish, args=(session, "hangup"), name="echo-call-end", daemon=True
            ).start()

    # -- answering --

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for s in self.sessions.values() if not s.finished)

    def _answer_safe(self, accid: int, event) -> None:
        try:
            self._answer(accid, event)
        except Exception as e:
            config.logger.exception(f"Echo call {event.get('msg_id')}: answering failed: {e}")

    def _answer(self, accid: int, event) -> None:
        msg_id, chat_id = int(event.msg_id), int(event.chat_id)
        rpc = self.bot.rpc
        try:
            contact_id = rpc.get_message(accid, msg_id).from_id
        except Exception:
            contact_id = 0

        if self.active_count() >= config.CALL_ECHO_MAX_CONCURRENT:
            config.logger.info(f"Echo call {msg_id}: declined, {self.active_count()} calls active")
            _end_call(rpc, accid, msg_id)
            dc_helpers._send(self.bot, accid, chat_id,
                             f"{REPORT_PREFIX} The echo line is busy right now, please call again in a minute.")
            return

        ice = echo_ice_servers(rpc.ice_servers(accid))
        peer = rtc.EchoPeer(ice, delay=config.CALL_ECHO_DELAY, greeting=True)
        session = EchoSession(accid, msg_id, chat_id, contact_id, peer)
        session.offer_relay_ips = turn_names.relay_ips_from_sdp(event.place_call_info)
        session.caller_domain = _contact_domain(rpc, accid, contact_id)
        with self._lock:
            self.sessions[msg_id] = session

        config.logger.info(
            f"Echo call {msg_id} from contact {contact_id} in chat {chat_id} "
            f"(video={event.get('has_video')}, ice servers: {rtc.describe_ice_servers(ice)})"
        )
        try:
            answer = self.loop.run(peer.accept(event.place_call_info), timeout=30)
            if session.finished:
                # caller hung up while we were gathering: finish() already ran
                # and closed the (then still empty) peer - close the new pc too
                self.loop.run(peer.close(), timeout=10)
                return
            rpc.accept_incoming_call(accid, msg_id, answer)
        except Exception as e:
            session.error = f"could not answer: {e}"
            _end_call(rpc, accid, msg_id)
            self.finish(session, "error", end_call=False)
            return
        session.accepted_at = time.time()

        # Watchdog: media must come up, and calls are capped in length.
        connected = self.loop.run(peer.wait_connected(config.CALL_ECHO_CONNECT_TIMEOUT),
                                  timeout=config.CALL_ECHO_CONNECT_TIMEOUT + 5)
        if session.finished:
            return
        if not connected:
            session.error = f"no media connection ({peer.failed_state or 'ICE timeout'})"
            self.finish(session, "ice-failed")
            return
        deadline = session.accepted_at + config.CALL_ECHO_MAX_SECONDS
        while not session.finished and time.time() < deadline:
            if peer.pc is None or peer.pc.connectionState in ("failed", "closed"):
                # A hangup closes the caller's media at once (DTLS close,
                # ~0.1 s), while the "call ended" message still has to travel
                # through the relays. Wait for it before calling this a drop.
                session.media_ended_at = time.time()
                grace_end = session.media_ended_at + config.CALL_ECHO_HANGUP_GRACE
                while not session.finished and time.time() < grace_end:
                    time.sleep(0.2)
                if not session.finished:
                    session.error = "media connection lost"
                    self.finish(session, "media-lost")
                return
            time.sleep(1)
        if not session.finished:
            self.finish(session, "max-duration")

    # -- finishing --

    def finish(self, session: EchoSession, reason: str, end_call: bool = True) -> None:
        with session.lock:
            if session.finished:
                return
            session.finished = True
            session.end_reason = reason
        try:
            summary = self.loop.run(session.peer.summary(), timeout=10)
        except Exception as e:
            config.logger.warning(f"Echo call {session.msg_id}: stats failed: {e}")
            summary = {}
        # TURN servers the caller reached: relay candidates from the offer and
        # those trickled in later over the iceTrickling channel
        try:
            relay_ips = self.loop.run(_remote_relay_ips(session.peer), timeout=5)
        except Exception:
            relay_ips = []
        relay_ips += [ip for ip in session.offer_relay_ips if ip not in relay_ips]
        try:
            self.loop.run(session.peer.close(), timeout=10)
        except Exception:
            pass
        if end_call and reason != "hangup":
            _end_call(self.bot.rpc, session.accid, session.msg_id)
        try:
            caller_turn = turn_names.caller_turn(relay_ips, self.bot, session.accid)
        except Exception as e:
            config.logger.warning(f"Echo call {session.msg_id}: TURN names failed: {e}")
            caller_turn = None

        # end of the call = when media stopped, not when the hangup message arrived
        ended = session.media_ended_at or time.time()
        duration = max(0.0, ended - session.accepted_at) if session.accepted_at else 0.0
        rtp = summary.get("rtp", {})
        try:
            if config.CALL_ECHO_LOG_DAYS > 0:
                database.prune_call_echo_log(time.time() - config.CALL_ECHO_LOG_DAYS * 86400)
                database.add_call_echo_log(
                    contact_id=session.contact_id,
                    chat_id=session.chat_id,
                    started_at=session.started_at,
                    duration_s=round(duration, 1),
                    connected=bool(summary.get("connected")),
                    path=(summary.get("path") or {}).get("kind"),
                    loss_pct=rtp.get("loss_pct"),
                    rtt_ms=rtp.get("rtcp_rtt_ms"),
                    jitter_ms=rtp.get("jitter_ms"),
                    voice_s=summary.get("voice_s"),
                    end_reason=reason,
                    error=session.error,
                    caller_turn=None if caller_turn is None else ",".join(caller_turn),
                    caller_domain=session.caller_domain,
                )
        except Exception as e:
            config.logger.warning(f"Echo call {session.msg_id}: could not log call: {e}")

        text = format_echo_report(summary, duration, reason, session.error, caller_turn)
        dc_helpers._send(self.bot, session.accid, session.chat_id, text)
        # The call message carries the caller's SDP offer, i.e. their ICE
        # candidates with local and public IP addresses. Core would keep it
        # for delete_device_after (36 h); the bot has no use for it once the
        # call is over.
        try:
            self.bot.rpc.delete_messages(session.accid, [session.msg_id])
        except Exception as e:
            config.logger.warning(f"Echo call {session.msg_id}: could not delete the call message: {e}")
        config.logger.info(f"Echo call {session.msg_id} finished ({reason}), {duration:.0f}s")
        with self._lock:
            self.sessions.pop(session.msg_id, None)


def echo_ice_servers(ice_json):
    """ICE servers for the echo peer, with a STUN server per CALL_ECHO_STUN.

    The bot usually sits behind NAT (Docker bridge). Without STUN its only
    reachable candidate is the TURN relay, so every echo call went through
    TURN. "auto" uses the relay's TURN server as STUN too, as the Delta Chat
    apps (libwebrtc) do, so calls can connect peer-to-peer through the NAT.
    """
    mode = config.CALL_ECHO_STUN
    if mode in ("off", "0", "no", "false", "none"):
        return rtc.parse_ice_servers(ice_json)
    if mode in ("auto", "", "1", "on", "yes", "true"):
        return rtc.parse_ice_servers(ice_json, turn_as_stun=True)
    return rtc.parse_ice_servers(ice_json, stun=mode)


async def _remote_relay_ips(peer) -> list:
    pc = getattr(peer, "pc", None)
    if pc is None:
        return []
    cands = []
    for conn in rtc._ice_connections(pc):
        cands.extend(conn.remote_candidates)
    return turn_names.relay_ips_from_candidates(cands)


def _contact_domain(rpc, accid, contact_id) -> Optional[str]:
    try:
        addr = rpc.get_contact(accid, contact_id).address or ""
    except Exception:
        return None
    domain = addr.rsplit("@", 1)[-1].strip().lower() if "@" in addr else ""
    return domain or None


def _end_call(rpc, accid, msg_id) -> None:
    try:
        rpc.end_call(accid, msg_id)
    except Exception as e:
        config.logger.debug(f"end_call({msg_id}) failed: {e}")


# --------------------------------------------------------------------------
# report formatting (pure functions, unit tested)
# --------------------------------------------------------------------------


def _fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _fmt_cands(cands: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted((cands or {}).items())) or "none"


def turn_test_url() -> Optional[str]:
    """Public URL of the browser TURN test page, if the web server has a base URL."""
    if not config.TURN_TEST_PAGE:
        return None
    base = database.get_config("base_url") or os.getenv("BASE_URL") or ""
    base = base.strip().rstrip("/")
    if not base:
        return None
    if "://" not in base:
        base = "https://" + base
    return base + "/test-turn"


def format_turn_line(caller_turn: Optional[list], connected: bool) -> Optional[str]:
    """What the caller's relay candidates say about their TURN server."""
    if caller_turn is None:
        return None
    if caller_turn:
        names = ", ".join("another TURN server" if n == turn_names.OTHER else n for n in caller_turn)
        return f"Your app's TURN server: {names} ✅"
    url = turn_test_url()
    test = f" Test it in a browser: {url}" if url else ""
    if connected:
        return ("ℹ️ Your app got no address from its TURN server (blocked or unreachable on your network; "
                f"profiles without TURN on their own relay use {turn_names.FALLBACK_TURN_HOST}). This call did "
                f"not need it, but calls through stricter networks will fail.{test}")
    return ("⚠️ Your app got no address from its TURN server: it is blocked or unreachable on your network "
            f"(profiles without TURN on their own relay use {turn_names.FALLBACK_TURN_HOST}).{test}")


def format_echo_report(summary: dict, duration: float, reason: str, error: Optional[str],
                       caller_turn: Optional[list] = None) -> str:
    lines = [f"{REPORT_PREFIX} **Echo call report**"]
    turn_line = format_turn_line(caller_turn, bool(summary.get("connected")))
    if not summary.get("connected") and reason == "hangup":
        lines.append("The call ended before the media connection was established.")
        lines.append(f"Your app offered ICE candidates: {_fmt_cands(summary.get('remote_candidates'))}")
        if turn_line:
            lines.append(turn_line)
        return "\n".join(lines)
    if not summary.get("connected"):
        lines.append(f"❌ Call answered, but {error or 'no media connection could be established'}.")
        lines.append(f"Your app offered ICE candidates: {_fmt_cands(summary.get('remote_candidates'))}"
                     + (f" (+{summary['trickled_candidates']} trickled)" if summary.get("trickled_candidates") else ""))
        lines.append(f"Bot candidates: {_fmt_cands(summary.get('local_candidates'))}")
        if turn_line:
            lines.append(turn_line)
        else:
            lines.append("Usually this means UDP is blocked on your network or the TURN server is unreachable.")
        return "\n".join(lines)

    connect = f" · connected in {summary['connect_ms']} ms" if summary.get("connect_ms") is not None else ""
    lines.append(f"Duration: {_fmt_duration(duration)}{connect}")
    path = summary.get("path") or {}
    if path:
        label = PATH_LABELS.get(path.get("kind"), path.get("kind", "?"))
        lines.append(
            f"Path: {label} (bot {path.get('local_type')} ↔ you {path.get('remote_type')})"
        )
    if turn_line:
        lines.append(turn_line)
    voice, received = summary.get("voice_s"), summary.get("audio_received_s")
    if received is not None:
        peak = summary.get("peak_dbfs")
        peak_txt = f", peak {peak} dBFS" if peak is not None else ""
        lines.append(f"Audio from you: {received} s received, voice {voice} s{peak_txt}")
    rtp = summary.get("rtp") or {}
    if "packets_received" in rtp:
        lines.append(
            f"Packets from you: {rtp['packets_received']} received, {rtp.get('packets_lost', 0)} lost "
            f"({rtp.get('loss_pct', 0):.1f}%), jitter {rtp.get('jitter_ms', 0)} ms"
        )
    if "packets_sent" in rtp:
        to_you = f"Packets to you: {rtp['packets_sent']} sent"
        if "remote_packets_lost" in rtp:
            to_you += f", your app reported {rtp['remote_packets_lost']} lost"
        if "rtcp_rtt_ms" in rtp:
            to_you += f", round trip {rtp['rtcp_rtt_ms']:.0f} ms"
        lines.append(to_you)
    if summary.get("codec"):
        lines.append(f"Codec: {summary['codec']}")
    if received and not voice:
        lines.append("⚠️ No voice was detected - check the microphone permission of your app.")
    elif rtp.get("loss_pct", 0) >= 5:
        lines.append("⚠️ High packet loss - expect choppy audio on this network.")
    if reason == "max-duration":
        lines.append(f"⏱️ Echo calls are limited to {_fmt_duration(config.CALL_ECHO_MAX_SECONDS)}, the bot hung up.")
    elif reason == "media-lost":
        lines.append("⚠️ The media connection dropped during the call.")
    return "\n".join(lines)


def _emoji(idx: int) -> str:
    return ["1️⃣", "2️⃣"][idx - 1]


def format_cmcall_report(result: dict, s1: str, s2: str) -> str:
    one_relay = s1 == s2
    lines = [f"{REPORT_PREFIX} **CMCall Report:**"]
    lines.append(f"1️⃣ {s1}" if one_relay else f"1️⃣ {s1}\n2️⃣ {s2}")
    lines.append("")
    sig = result.get("signaling") or {}
    arrow_fw = "1️⃣→1️⃣" if one_relay else "1️⃣→2️⃣"
    arrow_bw = "1️⃣←1️⃣" if one_relay else "1️⃣←2️⃣"
    if "ring_ms" in sig:
        back = f", {arrow_bw} {sig['accept_ms']} ms" if "accept_ms" in sig else ""
        lines.append(f"Signaling: {arrow_fw} {sig['ring_ms']} ms{back}")
    stats = result.get("caller_stats") or {}
    if stats.get("connected"):
        path = stats.get("path") or {}
        lines.append(
            f"Media: {PATH_LABELS.get(path.get('kind'), path.get('kind', '?'))}, "
            f"connected in {stats.get('connect_ms')} ms"
        )
        echo = stats.get("echo") or {}
        if echo.get("received"):
            lines.append(
                f"Echo RTT: avg {echo['rtt_avg_ms']:.0f} ms (min {echo['rtt_min_ms']:.0f} / "
                f"max {echo['rtt_max_ms']:.0f}), {echo['received']}/{echo['sent']} beeps back"
            )
        else:
            lines.append(f"Echo: 0/{echo.get('sent', 0)} beeps came back")
        rtp1 = stats.get("rtp") or {}
        rtp2 = (result.get("callee_stats") or {}).get("rtp") or {}
        if rtp1 or rtp2:
            lines.append(
                f"RTP loss {rtp1.get('loss_pct', 0):.1f}% / {rtp2.get('loss_pct', 0):.1f}%, "
                f"jitter {rtp1.get('jitter_ms', '?')} / {rtp2.get('jitter_ms', '?')} ms"
                + (f", RTCP RTT {rtp1['rtcp_rtt_ms']:.0f} ms" if "rtcp_rtt_ms" in rtp1 else "")
            )
    turn1, turn2 = result.get("caller_turn"), result.get("callee_turn")
    if turn1:
        lines.append(f"TURN: 1️⃣ {turn1}" + ("" if one_relay or not turn2 else f", 2️⃣ {turn2}"))
    if not result.get("ok"):
        lines.append(f"❌ Failed at {result.get('stage') or '?'}: {result.get('error') or 'unknown error'}")
    gmt_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M GMT")
    bot_name = os.environ.get("DISPLAY_NAME", "Bouncer Bot")
    lines.append("")
    lines.append(f"Generated {gmt_time} by {bot_name}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# hooks
# --------------------------------------------------------------------------


def _is_call_event(event) -> bool:
    return event.get("kind") in CALL_EVENT_KINDS


@config.dc_cli.on(events.RawEvent(func=_is_call_event))
def on_call_event(bot, accid, event):
    echo, meet = state.echo_call_manager, state.meet_manager
    if event.kind == "OutgoingCallAccepted":  # only meetings place calls
        if meet is not None:
            meet.on_outgoing_accepted(event)
        return
    if event.kind == "CallEnded":
        if meet is not None:
            meet.on_call_ended(event)
        if echo is not None:
            echo.on_call_ended(accid, event)
        return
    # IncomingCall: into a meeting the caller joined, else the echo
    if meet is not None:
        threading.Thread(target=_route_incoming_call, args=(bot, accid, event),
                         name="call-route", daemon=True).start()
    elif echo is not None:
        echo.on_incoming_call(accid, event)
    elif config.CALL_ECHO_ENABLED:
        config.logger.warning(f"Incoming call ignored: call support unavailable ({_RTC_IMPORT_ERROR})")


def _route_incoming_call(bot, accid, event) -> None:
    meet, echo = state.meet_manager, state.echo_call_manager
    try:
        contact_id = bot.rpc.get_message(accid, int(event.msg_id)).from_id
    except Exception:
        contact_id = 0
    if contact_id and meet.wants_incoming(contact_id):
        meet._safe(meet.on_incoming_call, accid, event, contact_id)
    elif echo is not None:
        echo._answer_safe(accid, event)


def setup_echo_calls(bot, accid) -> None:
    """Called from on_start: create the manager and open the line for callers."""
    if not config.CALL_ECHO_ENABLED:
        config.logger.info("Echo calls disabled (CALL_ECHO=0).")
        return
    if rtc is None:
        config.logger.warning(f"Echo calls unavailable, cmcall/aiortc not importable: {_RTC_IMPORT_ERROR}")
        return
    who = {"everybody": "0", "contacts": "1"}.get(config.CALL_ECHO_WHO, "0")
    try:
        bot.rpc.set_config(accid, "who_can_call_me", who)
    except Exception as e:
        config.logger.warning(f"Could not set who_can_call_me: {e}")
    if config.CALL_ECHO_LOG_DAYS > 0:
        database.prune_call_echo_log(time.time() - config.CALL_ECHO_LOG_DAYS * 86400)
    else:
        database.prune_call_echo_log(time.time() + 1)  # history disabled: drop what is left
    state.echo_call_manager = EchoCallManager(bot)
    config.logger.info(
        f"Echo calls enabled (who={config.CALL_ECHO_WHO}, delay={config.CALL_ECHO_DELAY}s, "
        f"max {config.CALL_ECHO_MAX_SECONDS}s, {config.CALL_ECHO_MAX_CONCURRENT} concurrent)."
    )


# --------------------------------------------------------------------------
# /cmcall
# --------------------------------------------------------------------------


def run_cmcall(servers: list[str], duration: int = 6, timeout: int = 240) -> dict:
    """Run `cmcall --json` and return its result dict (never raises).

    cmcall's own --timeout bounds each wait (online, ringing, accepted), so a
    dead relay is reported by cmcall with its failing stage well before the
    subprocess timeout kills it.
    """
    cmcall_path = shutil.which("cmcall") or "cmcall"
    cmd = [cmcall_path, "--json", "-d", str(duration), "--timeout", "45", *servers]
    config.logger.info(f"Running: {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "stage": "timeout", "error": f"cmcall did not finish within {timeout}s"}
    except FileNotFoundError:
        return {"ok": False, "stage": "setup", "error": "cmcall is not installed"}
    try:
        start = proc.stdout.index("{")
        return json.loads(proc.stdout[start:])
    except (ValueError, json.JSONDecodeError):
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["no output"]
        return {"ok": False, "stage": "setup", "error": f"cmcall exit {proc.returncode}: {tail[0]}"}


def bg_cmcall_worker(bot, accid, chat_id, msg_id, s1, s2):
    if not state._cmcall_global_lock.acquire(blocking=False):
        dc_helpers._send(bot, accid, chat_id, "⏳ Another CMCall test is running, your request is queued...")
        state._cmcall_global_lock.acquire()
    try:
        servers = [s1] if s1 == s2 else [s1, s2]
        result = run_cmcall(servers)
        dc_helpers._react(bot, accid, msg_id, "☑️" if result.get("ok") else "❌")
        dc_helpers._send(bot, accid, chat_id, format_cmcall_report(result, s1, s2))
    finally:
        state._cmcall_global_lock.release()


@config.dc_cli.on(events.NewMessage(command="/cmcall"))
def cmcall_command(bot, accid, event):
    msg = event.msg
    servers = [s.strip().lower() for s in (event.payload or "").split() if s.strip()]
    usage = "Usage: /cmcall <server> OR /cmcall <server1> <server2>"
    if not servers or len(servers) > 2:
        dc_helpers._send(bot, accid, msg.chat_id, usage)
        return
    for s in servers:
        if not config.DOMAIN_REGEX.match(s):
            dc_helpers._send(bot, accid, msg.chat_id, f"❌ Invalid server domain: `{s}`. {usage}")
            return

    now = time.time()
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        last = state._chat_cmcall_anti_spam.get(msg.chat_id, 0)
        if now - last < config.CMCALL_COOLDOWN_SECONDS:
            wait = max(1, int(config.CMCALL_COOLDOWN_SECONDS - (now - last)))
            dc_helpers._send(bot, accid, msg.chat_id, f"⏳ /cmcall is on cooldown, try again in {wait}s.")
            return
    state._chat_cmcall_anti_spam[msg.chat_id] = now

    dc_helpers._react(bot, accid, msg.id, "⏳")
    s1 = servers[0]
    s2 = servers[1] if len(servers) == 2 else servers[0]
    threading.Thread(
        target=bg_cmcall_worker, args=(bot, accid, msg.chat_id, msg.id, s1, s2), daemon=True
    ).start()


# --------------------------------------------------------------------------
# /callstats
# --------------------------------------------------------------------------


def format_call_log_line(row: dict) -> str:
    when = datetime.fromtimestamp(row["started_at"], timezone.utc).strftime("%m-%d %H:%M")
    turn = row.get("caller_turn")
    turn_txt = None if turn is None else f"TURN {turn.replace(',', ', ') or 'none'}"
    if not row["connected"]:
        return " · ".join([f"{when} ❌ {row.get('error') or 'not connected'}"] + ([turn_txt] if turn_txt else []))
    parts = [f"{when} ✅ {_fmt_duration(row['duration_s'] or 0)}", PATH_LABELS.get(row["path"], row["path"] or "?")]
    if row.get("loss_pct") is not None:
        parts.append(f"loss {row['loss_pct']:.1f}%")
    if row.get("rtt_ms") is not None:
        parts.append(f"rtt {row['rtt_ms']:.0f} ms")
    if turn_txt:
        parts.append(turn_txt)
    return " · ".join(parts)


def format_turn_aggregate(agg: dict, label: str = "7d") -> Optional[str]:
    """Admin line: which TURN servers callers reached, and who reached none."""
    if not agg.get("calls"):
        return None
    parts = [f"{n} {c}/{agg['calls']}" for n, c in agg["turn"].items()]
    line = f"Callers' TURN ({label}): " + (", ".join(parts) if parts else "no relay addresses")
    if agg.get("none"):
        domains = ", ".join(f"{d} {c}" for d, c in list(agg["none_domains"].items())[:5])
        line += f" · no TURN address: {agg['none']}/{agg['calls']} ({domains})"
    return line


def privacy_note() -> str:
    if config.CALL_ECHO_LOG_DAYS > 0:
        kept = f"call statistics (no audio, no IP addresses) are kept for {config.CALL_ECHO_LOG_DAYS} days"
    else:
        kept = "no call history is kept"
    return f"🔒 Calls are not recorded; {kept}."


@config.dc_cli.on(events.NewMessage(command="/callstats"))
def callstats_command(bot, accid, event):
    msg = event.msg
    is_admin = dc_helpers._is_dc_admin(bot, accid, msg.from_id)
    lines = [f"{REPORT_PREFIX} **Echo call statistics**"]
    if not config.CALL_ECHO_ENABLED or rtc is None:
        lines.append("Echo calls are disabled on this bot.")
    else:
        lines.append("Call this bot to hear yourself back; you get a report after hanging up.")
    lines.append(privacy_note())
    if config.CALL_ECHO_LOG_DAYS <= 0:
        if is_admin and state.echo_call_manager is not None:
            lines.append(f"Active now: {state.echo_call_manager.active_count()}")
        dc_helpers._send(bot, accid, msg.chat_id, "\n".join(lines))
        return
    database.prune_call_echo_log(time.time() - config.CALL_ECHO_LOG_DAYS * 86400)
    if is_admin:
        for label, seconds in (("24h", 86400), ("7d", 7 * 86400)):
            agg = database.get_call_echo_aggregate(time.time() - seconds)
            if not agg["calls"]:
                lines.append(f"{label}: no calls")
                continue
            line = f"{label}: {agg['calls']} calls, {agg['connected']} connected"
            if agg["avg_loss_pct"] is not None:
                line += f", avg loss {agg['avg_loss_pct']:.1f}%"
            if agg["avg_rtt_ms"] is not None:
                line += f", avg rtt {agg['avg_rtt_ms']:.0f} ms"
            if agg["paths"]:
                line += " (" + ", ".join(f"{PATH_LABELS.get(p, p)} {n}" for p, n in agg["paths"].items()) + ")"
            lines.append(line)
        turn_line = format_turn_aggregate(database.get_call_echo_turn_aggregate(time.time() - 7 * 86400))
        if turn_line:
            lines.append(turn_line)
        rows = database.get_recent_call_echo_logs(limit=10)
        if rows:
            lines.append("")
            lines.append("Last calls:")
            lines.extend(f"#{r['contact_id']} {format_call_log_line(r)}" for r in rows)
        if state.echo_call_manager is not None:
            lines.append(f"Active now: {state.echo_call_manager.active_count()}")
    else:
        rows = database.get_recent_call_echo_logs(limit=5, contact_id=msg.from_id)
        lines.append("")
        if rows:
            lines.append("Your last calls:")
            lines.extend(format_call_log_line(r) for r in rows)
        else:
            lines.append("You have not called this bot yet.")
    dc_helpers._send(bot, accid, msg.chat_id, "\n".join(lines))
