"""Country flags and RU-whitelist status of relay servers for /cmpinglist.

flag(domain) never waits for the network: it answers from a cache (a day, kept in the
database so a restart does not empty it) and, when an entry is missing or older than a day,
starts one background lookup - resolve the host, ask ipinfo.io (RIPEstat as a fallback) for
the country of its first address and check its IPv4 addresses against the RU mobile
whitelist (whitelist.py) - while the old values, if any, are still shown. A failed
lookup is retried after RETRY_SECONDS.
"""
import json
import threading
import time

import config
import database
import iplookup
import whitelist

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


def lookup(domain: str) -> dict | None:
    """{"cc": country code of the first address, "wl": all IPv4 addresses are in the RU mobile
    whitelist (None when that check is off or its list unavailable)}, or None when the host
    does not resolve or no country is found."""
    ips = iplookup.resolve(domain)
    if not ips:
        return None
    ip_s = ips[0].compressed
    info = iplookup._ipinfo_ip(ip_s) or {}
    cc = info.get("country_code") or (info.get("country") if len(info.get("country") or "") == 2 else None)
    cc = cc or iplookup._ripe_country(ip_s)
    if not (cc and len(cc) == 2 and cc.isalpha()):
        return None
    wl = None
    if whitelist.enabled("ip"):
        v4 = [ip for ip in ips if ip.version == 4][:4]
        statuses = [whitelist.ip_status(ip) for ip in v4]  # may wait for the first download: we are in the background
        if not v4:
            wl = False  # the list is IPv4 only
        elif None not in statuses:
            wl = all(statuses)
    return {"cc": cc.upper(), "wl": wl}


def lookup_country(domain: str) -> str | None:
    found = lookup(domain)
    return found["cc"] if found else None


def _refresh(domain: str) -> None:
    try:
        found = lookup(domain)
        with _lock:
            if found:
                _load()[domain] = {"cc": found["cc"], "wl": found["wl"], "at": time.time()}
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


def _entry(domain: str) -> dict:
    """The cached entry ({} while unknown); starts a refresh when missing, stale or from before
    the whitelist check was added. Never waits."""
    domain = domain.strip().lower()
    now = time.time()
    with _lock:
        entry = _load().get(domain) or {}
        stale = not entry or "wl" not in entry or now - entry.get("at", 0) > CACHE_SECONDS
        start = (stale and domain not in _busy and now - _failed_at.get(domain, 0) > RETRY_SECONDS)
        if start:
            _busy.add(domain)
        entry = dict(entry)
    if start:
        threading.Thread(target=_refresh, args=(domain,), name="relay-geo", daemon=True).start()
    return entry


def country(domain: str) -> str:
    """Two-letter country code of the relay, "" while unknown."""
    return _entry(domain).get("cc") or ""


def flag(domain: str) -> str:
    """Flag emoji for the relay's country, "" while unknown."""
    return iplookup._flag(country(domain))


def whitelisted(domain: str) -> bool:
    """True when all the relay's IPv4 addresses are in the RU mobile whitelist (as of its last
    lookup) and that check is turned on."""
    return _entry(domain).get("wl") is True and whitelist.enabled("ip")


def parse_country(text: str) -> str | None:
    """"🇷🇺", "ru" or "RU" -> "RU"; None for anything else."""
    text = (text or "").strip()
    if len(text) == 2 and text.isascii() and text.isalpha():
        return text.upper()
    if len(text) == 2 and all(0x1F1E6 <= ord(c) <= 0x1F1FF for c in text):
        return "".join(chr(ord(c) - 0x1F1E6 + ord("A")) for c in text)
    return None
