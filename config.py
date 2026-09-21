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
VERSION = "2.15.0"

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
            f"RPC Client: {rpc_client_ver} | deltabot-cli: {cli_ver} | cmping: {cmping_ver}"
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

DOMAIN_REGEX = re.compile(r'^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)+$')

REGULAR_MAIL_DOMAINS = {
    "yandex.ru", "yandex.com", "ya.ru",
    "mail.ru", "list.ru", "bk.ru", "inbox.ru", "internet.ru",
    "rambler.ru"
}

# Age indicator: each circle/square = 1 week of bot knowing the user
_AGE_CIRCLES = ["🔴", "🟠", "🟡", "🟢", "🔵", "🟣", "🟤", "⚫", "⚪"]
_AGE_SQUARES = ["🟥", "🟧", "🟨", "🟩", "🟦", "🟪", "🟫", "⬛", "⬜"]
