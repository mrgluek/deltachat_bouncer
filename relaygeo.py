"""Country flags of relay servers for /cmpinglist.

flag(domain) never waits for the network: it answers from a cache (a day, kept in the
database so a restart does not empty it) and, when an entry is missing or older than a day,
starts one background lookup - resolve the host, ask ipinfo.io (RIPEstat as a fallback) for
the country of its first address - while the old flag, if any, is still shown. A failed
lookup is retried after RETRY_SECONDS.
"""
import json
import threading
import time

import config
import database
import iplookup

CACHE_SECONDS = 24 * 3600
RETRY_SECONDS = 3600
_CONFIG_KEY = "relay_country_cache"

_lock = threading.Lock()
_cache: dict[str, dict] | None = None   # {domain: {"cc": "DE" | "", "at": checked-at}}
_busy: set[str] = set()                  # lookups running or queued
_failed_at: dict[str, float] = {}


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(database.get_config(_CONFIG_KEY) or "{}")
        except (ValueError, TypeError):
            _cache = {}
    return _cache


def _save() -> None:
    try:
        database.set_config(_CONFIG_KEY, json.dumps(_cache))
    except Exception as e:
        config.logger.warning(f"Could not save the relay country cache: {e}")


def lookup_country(domain: str) -> str | None:
    """Two-letter country code of the host's first address, or None."""
    for ip in iplookup.resolve(domain):
        ip_s = ip.compressed
        info = iplookup._ipinfo_ip(ip_s) or {}
        cc = info.get("country_code") or (info.get("country") if len(info.get("country") or "") == 2 else None)
        cc = cc or iplookup._ripe_country(ip_s)
        if cc and len(cc) == 2 and cc.isalpha():
            return cc.upper()
        return None  # the first address is the one that is asked about
    return None


def _refresh(domain: str) -> None:
    try:
        cc = lookup_country(domain)
        with _lock:
            if cc:
                _load()[domain] = {"cc": cc, "at": time.time()}
                _failed_at.pop(domain, None)
                _save()
            else:
                _failed_at[domain] = time.time()
    except Exception as e:
        config.logger.warning(f"Country lookup for {domain} failed: {e}")
        with _lock:
            _failed_at[domain] = time.time()
    finally:
        with _lock:
            _busy.discard(domain)


def country(domain: str) -> str:
    """Two-letter country code of the relay, "" while unknown; starts a refresh when stale."""
    domain = domain.strip().lower()
    now = time.time()
    with _lock:
        entry = _load().get(domain)
        stale = entry is None or now - entry.get("at", 0) > CACHE_SECONDS
        start = (stale and domain not in _busy and now - _failed_at.get(domain, 0) > RETRY_SECONDS)
        if start:
            _busy.add(domain)
        cc = entry.get("cc") if entry else ""
    if start:
        threading.Thread(target=_refresh, args=(domain,), name="relay-geo", daemon=True).start()
    return cc or ""


def flag(domain: str) -> str:
    """Flag emoji for the relay's country, "" while unknown."""
    return iplookup._flag(country(domain))


def parse_country(text: str) -> str | None:
    """"🇷🇺", "ru" or "RU" -> "RU"; None for anything else."""
    text = (text or "").strip()
    if len(text) == 2 and text.isascii() and text.isalpha():
        return text.upper()
    if len(text) == 2 and all(0x1F1E6 <= ord(c) <= 0x1F1FF for c in text):
        return "".join(chr(ord(c) - 0x1F1E6 + ord("A")) for c in text)
    return None
