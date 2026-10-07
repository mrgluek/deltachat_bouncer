"""Static configuration: imports side-effects, version info, and constant tables.

Anything here is fixed at import time (env loading, logging setup, cooldown
durations, regexes). Mutable runtime state (locks, caches, the live bot
instance) lives in state.py instead - see that module's docstring for the
split rationale.
"""
import logging
import mimetypes
import os
import re
import threading

from deltabot_cli import BotCli

for _ext, _mt in (
    ('.webp', 'image/webp'),
    ('.png', 'image/png'),
    ('.jpg', 'image/jpeg'),
    ('.jpeg', 'image/jpeg'),
    ('.gif', 'image/gif'),
    ('.svg', 'image/svg+xml'),
    ('.ico', 'image/x-icon'),
    ('.mp4', 'video/mp4'),
    ('.webm', 'video/webm'),
    ('.mp3', 'audio/mpeg'),
    ('.ogg', 'audio/ogg'),
    ('.wav', 'audio/wav'),
    ('.aac', 'audio/aac'),
    ('.m4a', 'audio/m4a'),
    ('.pdf', 'application/pdf'),
):
    mimetypes.add_type(_mt, _ext)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("bouncer_bot")

# Privacy: at INFO level aioice logs every ICE candidate pair it checks, i.e.
# the IP addresses of everyone who calls the echo service. Keep the WebRTC
# libraries at WARNING unless CALL_DEBUG_LOG=1 is set for troubleshooting.
if os.environ.get("CALL_DEBUG_LOG", "").strip().lower() not in ("1", "true", "yes", "on"):
    for _webrtc_logger in ("aioice", "aiortc"):
        logging.getLogger(_webrtc_logger).setLevel(logging.WARNING)
VERSION = "2.26.1"

DC_FALLBACK_PATTERN = re.compile(
    r'\s*\[(?:Image|Video|Voice|Audio|Document|File|Sticker|Gif)[ \-–]+[^\]]+\]',
    re.IGNORECASE
)


def log_version_info(bot):
    """Log version information of Bouncer Bot, DeltaChat Core, RPC Client, and key dependencies on startup."""
    try:
        import importlib.metadata

        def get_pkg_ver(pkg_name):
            try:
                return importlib.metadata.version(pkg_name)
            except Exception:
                return None

        rpc_client_ver = get_pkg_ver("deltachat-rpc-client") or get_pkg_ver("deltachat2") or "unknown"
        cli_ver = get_pkg_ver("deltabot-cli") or "unknown"
        cmping_ver = get_pkg_ver("cmping") or "unknown"
        cmcall_ver = get_pkg_ver("cmcall") or "unknown"

        core_ver = "unknown"
        try:
            sys_info = bot.rpc.get_system_info()
            if isinstance(sys_info, dict):
                core_ver = sys_info.get("deltachat_core_version", "unknown")
            else:
                core_ver = getattr(sys_info, "deltachat_core_version", "unknown")
        except Exception as e:
            core_ver = f"error ({e})"

        bot.logger.info(f"=== Bouncer Bot v{VERSION} Startup Version Check ===")
        bot.logger.info(
            f"Bouncer Bot: {VERSION} | DeltaChat Core: {core_ver} | "
            f"RPC Client: {rpc_client_ver} | deltabot-cli: {cli_ver} | cmping: {cmping_ver} | "
            f"cmcall: {cmcall_ver}"
        )
    except Exception as e:
        bot.logger.warning(f"Failed to check versions on startup: {e}")


dc_cli = BotCli("bouncer")

DC_CONTACT_ID_SELF = 1
INACTIVITY_DAYS_THRESHOLD = 21
INACTIVITY_SECONDS_THRESHOLD = INACTIVITY_DAYS_THRESHOLD * 24 * 3600


# Helper to read .env file into os.environ if not already present
def _load_env_file():
    for candidate in [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        os.path.join(os.getcwd(), ".env"),
    ]:
        if os.path.isfile(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip("'\"")
                            if k and k not in os.environ:
                                os.environ[k] = v
                break
            except Exception:
                pass

_load_env_file()

# VirusTotal rate-limiting
VIRUSTOTAL_RATE_LIMIT_SECONDS = 15.0

# CMPing monitoring
CMPING_MONITOR_INTERVAL = int(os.environ.get("CMPING_MONITOR_INTERVAL", "1800"))  # default 30 min

# Command cooldowns
BOUNCE_COOLDOWN_SECONDS = 60   # 1 minute for general commands (/bounce, /top, /relays)
SEARCH_COOLDOWN_SECONDS = 10   # 10 seconds for /search command
CMPING_COOLDOWN_SECONDS = 15   # 15 seconds for /cmping command
SLAP_COOLDOWN_SECONDS = 15     # 15 seconds for /slap command
STICKER_COOLDOWN_SECONDS = 5   # 5 seconds for /sticker commands
STICKER_NOBG_COOLDOWN_SECONDS = 15 # 15 seconds for /stickernobg commands
CMCALL_COOLDOWN_SECONDS = 60   # 60 seconds for /cmcall (a call test takes ~30-60s)


def _env_flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() not in ("0", "false", "no", "off", "")


# Echo calls: the bot answers Delta Chat calls and echoes the caller's audio
CALL_ECHO_ENABLED = _env_flag("CALL_ECHO", "1")
CALL_ECHO_WHO = os.environ.get("CALL_ECHO_WHO", "everybody").strip().lower()  # everybody | contacts
CALL_ECHO_DELAY = float(os.environ.get("CALL_ECHO_DELAY", "0"))  # seconds of playback delay
CALL_ECHO_MAX_SECONDS = int(os.environ.get("CALL_ECHO_MAX_SECONDS", "300"))
CALL_ECHO_MAX_CONCURRENT = int(os.environ.get("CALL_ECHO_MAX_CONCURRENT", "2"))
CALL_ECHO_CONNECT_TIMEOUT = 20
# Seconds to wait for the "call ended" message after the caller's media closed
# before reporting a dropped connection (it travels through the relays)
CALL_ECHO_HANGUP_GRACE = 15
# STUN for echo calls: auto = the relay's TURN server doubles as STUN (like the
# Delta Chat apps), off = TURN only, or an explicit host:port / stun:host:port
CALL_ECHO_STUN = os.environ.get("CALL_ECHO_STUN", "auto").strip().lower()
# Days to keep the per-call statistics behind /callstats; 0 = keep none
CALL_ECHO_LOG_DAYS = int(os.environ.get("CALL_ECHO_LOG_DAYS", "30"))
# Extra TURN host names to recognize in callers' relay candidates (comma
# separated); turn.delta.chat, the bot's relays and the monitored relays are
# always known
CALL_TURN_HOSTS = [h.strip().lower() for h in os.environ.get("CALL_TURN_HOSTS", "").split(",") if h.strip()]
# The browser TURN test page (/test-turn): on by default
TURN_TEST_PAGE = _env_flag("TURN_TEST_PAGE", "1")

# Voice meetings (/meet): an audio bridge on top of calls. Switched on by the
# admin with /meets on (off by default). Every participant costs an Opus
# decode + encode, so places are a budget shared by all rooms: one room can use
# all MEET_TOTAL_SLOTS, two rooms get half each, and a second room is refused
# while the first one is fuller than that.
MEET_TOTAL_SLOTS = int(os.environ.get("MEET_TOTAL_SLOTS", "8"))
MEET_MAX_ROOMS = int(os.environ.get("MEET_MAX_ROOMS", "2"))
MEET_MAX_PARTICIPANTS = int(os.environ.get("MEET_MAX_PARTICIPANTS", "8"))  # per room
MEET_IDLE_MINUTES = int(os.environ.get("MEET_IDLE_MINUTES", "60"))  # after the last person left
MEET_MAX_HOURS = float(os.environ.get("MEET_MAX_HOURS", "6"))  # hard limit per room
MEET_RING_SECONDS = int(os.environ.get("MEET_RING_SECONDS", "45"))  # /join: how long the bot rings
MEET_MAX_ROOMS_PER_USER = int(os.environ.get("MEET_MAX_ROOMS_PER_USER", "1"))
# Background audio in meetings (/radio, /play): level relative to the voices,
# and how far it drops while someone talks
MEET_MUSIC_VOLUME = float(os.environ.get("MEET_MUSIC_VOLUME", "0.35"))
MEET_MUSIC_DUCK = float(os.environ.get("MEET_MUSIC_DUCK", "0.3"))

# Call monitoring between relays (cmcall), the call-side twin of the cmping monitor
CMCALL_MONITOR_INTERVAL = int(os.environ.get("CMCALL_MONITOR_INTERVAL", "3600"))  # 0 disables
CMCALL_MONITOR_DURATION = int(os.environ.get("CMCALL_MONITOR_DURATION", "5"))  # seconds of beeps per call
CMCALL_DEGRADED_LOSS_PCT = float(os.environ.get("CMCALL_DEGRADED_LOSS_PCT", "10"))

DOMAIN_REGEX = re.compile(r'^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)+$')

REGULAR_MAIL_DOMAINS = {
    "yandex.ru", "yandex.com", "ya.ru",
    "mail.ru", "list.ru", "bk.ru", "inbox.ru", "internet.ru",
    "rambler.ru"
}

# Age indicator: each circle/square = 1 week of bot knowing the user
_AGE_CIRCLES = ["🔴", "🟠", "🟡", "🟢", "🔵", "🟣", "🟤", "⚫", "⚪"]
_AGE_SQUARES = ["🟥", "🟧", "🟨", "🟩", "🟦", "🟪", "🟫", "⬛", "⬜"]
