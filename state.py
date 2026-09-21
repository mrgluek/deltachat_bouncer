"""Mutable runtime state shared across modules.

Everything here is either a lock, a cache, or a live handle set once the bot
connects (dc_bot_instance/dc_accid). Other modules must always go through
`state.<name>` (never `from state import <name>`) for any value that gets
*rebound* at runtime (dc_bot_instance, dc_accid, the cmping/index caches,
etc.) - a bare import would freeze a stale reference the moment the module
loads. Values that are only ever mutated in place (dict/list/lock contents)
are safe either way, but we use `state.<name>` consistently for clarity.
"""
import os
import threading

import database

dc_bot_instance = None
dc_accid = None

# Anti-spam: {chat_id: timestamp}
_chat_bounce_anti_spam: dict[int, float] = {}
_chat_top_anti_spam: dict[int, float] = {}
_chat_invite_anti_spam: dict[int, float] = {}
_chat_anti_spam = _chat_bounce_anti_spam  # alias for backwards compatibility
_chat_relays_anti_spam: dict[int, float] = {}
_chat_search_anti_spam: dict[int, float] = {}
_chat_cmping_anti_spam: dict[int, float] = {}
_chat_slap_anti_spam: dict[int, float] = {}
_chat_sticker_anti_spam: dict[int, float] = {}
_chat_stickernobg_anti_spam: dict[int, float] = {}
_rembg_global_lock = threading.Lock()
_domain_locks: dict[str, threading.Lock] = {}
_domain_locks_lock = threading.Lock()
_cmping_global_lock = threading.Lock()
resilient_lock = threading.Lock()

# Persistent rembg / u2net models storage
if os.getenv("DC_DB_DIR"):
    _default_u2net_dir = os.path.join(os.getenv("DC_DB_DIR"), "u2net")
    os.environ.setdefault("U2NET_HOME", _default_u2net_dir)
    os.environ.setdefault("REMBG_HOME", _default_u2net_dir)

CHANNEL_MEDIA_DIR = os.path.join(os.getenv("DC_DB_DIR", "data"), "channel_media")
os.makedirs(CHANNEL_MEDIA_DIR, exist_ok=True)
bot_invite_link_cache = {}
index_page_html_cache = None

# VirusTotal global lock and rate-limiting
_vt_global_lock = threading.Lock()
_vt_last_request_time = 0.0

# CMPing monitoring state
_cmping_monitor_idx_db = database.get_config("cmping_monitor_index")
_cmping_monitor_index = int(_cmping_monitor_idx_db) if _cmping_monitor_idx_db is not None else 0
_cmping_monitor_running = False

_cmping_last_results: dict[tuple, dict] = database.get_all_cmping_results()
_cmping_server_status: dict[str, bool] = {}
_cmping_server_errors: dict[str, str] = {}
_cmping_incident_last_edit_state = {}


def _set_cmping_server_status(server: str, is_healthy: bool, error: str | None = None):
    global _cmping_server_status, _cmping_server_errors
    _cmping_server_status[server] = is_healthy
    while len(_cmping_server_status) > 500:
        _cmping_server_status.pop(next(iter(_cmping_server_status)), None)
    if error:
        _cmping_server_errors[server] = error
        while len(_cmping_server_errors) > 500:
            _cmping_server_errors.pop(next(iter(_cmping_server_errors)), None)
    elif error is not None or is_healthy:
        _cmping_server_errors.pop(server, None)


# Delayed-command debouncing (e.g. /bounce retried before its cooldown queue fires)
_pending_delayed_commands: dict[str, tuple[threading.Timer, list[int]]] = {}
_pending_delayed_lock = threading.Lock()

# Failover retry bookkeeping for MSG_FAILED events
_message_failover_attempts = {}

# Channel web-preview caches
_channel_preview_cache: dict[str, tuple[float, str, str]] = {}  # key -> (expires_at, etag, html)
_channel_rss_cache: dict[str, tuple[float, str, str]] = {}  # key -> (expires_at, etag, rss_xml)
_qr_cache: dict[str, tuple[bytes, str]] = {}  # key -> (bytes, content_type)


def get_bot_invite_link() -> str:
    if "link" in bot_invite_link_cache:
        return bot_invite_link_cache["link"]
    cfg_link = database.get_config("bot_invite_link")
    if cfg_link:
        return cfg_link
    if dc_bot_instance and dc_accid:
        try:
            link = dc_bot_instance.rpc.get_chat_securejoin_qr_code(dc_accid, None)
            if link:
                bot_invite_link_cache["link"] = link
                return link
        except Exception as e:
            import config
            config.logger.warning(f"Could not get securejoin link: {e}")
    return ""
