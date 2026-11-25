"""Russian blocklists via cheburcheck.ru: the "RKN" lines of /ip and /dns.

Source: GET https://cheburcheck.ru/api/v1/check?target=<domain|IP|CIDR|ASN>
(https://cheburcheck.ru/kb/api, no token needed). Its "blocked" flag is true
for any of three reasons, which the bot shows apart:

- rkn_domain: the domain (or the registry entry covering it) is in the
  Roskomnadzor registry;
- blocked_subnets: an address is in a blocked subnet of the registry
  (for an AS, asn_info.blocked_prefixes mixes these with CDN prefixes);
- cdn_providers: an address belongs to a CDN that Russian operators throttle
  (connections stall after 16-20 KB) - e.g. Cloudflare.

Also shown: a domain in its CDN exception list ("whitelist": the name lifts
the 16-20 KB limit) and how many users reported "does not work" in 14 days.
The network scanners (/probe) need an API token and are not used.

The API limits clients per IP (a burst of about 5 requests). The bot sends at
most PER_MINUTE requests a minute, pauses PAUSE_SECONDS after HTTP 429 and
keeps answers for CACHE_SECONDS, so a repeated /ip or /dns costs nothing.
One request per uncached target, no timer. CHEBURCHECK_URL in .env changes
the API address; an empty value turns the lines off.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import config
from netutil import urlopen

DEFAULT_URL = "https://cheburcheck.ru/api/v1"
WEB_URL = "https://cheburcheck.ru/check"
CACHE_SECONDS = 3600
CACHE_MAX = 500
PER_MINUTE = 4
PAUSE_SECONDS = 60
TIMEOUT = 10
MAX_SHOWN = 5

LABEL = "🚫 RKN blocklists"

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}
_sent: list[float] = []      # request times, last minute
_paused_until = 0.0


def api_url() -> str:
    value = os.environ.get("CHEBURCHECK_URL")
    if value is None:
        return DEFAULT_URL
    return value.strip().strip("'\"").rstrip("/")


def enabled() -> bool:
    return bool(api_url())


def _take() -> bool:
    """Reserve one request within the per-minute budget."""
    now = time.time()
    with _lock:
        if now < _paused_until:
            return False
        _sent[:] = [t for t in _sent if now - t < 60]
        if len(_sent) >= PER_MINUTE:
            return False
        _sent.append(now)
        return True


def _fetch(target: str) -> dict:
    """-> the API answer, {"not_found": True} or {"error": short reason}."""
    global _paused_until
    url = f"{api_url()}/check?{urllib.parse.urlencode({'target': target})}"
    req = urllib.request.Request(url, headers={
        "User-Agent": f"BouncerBot/{config.VERSION}", "Accept": "application/json"})
    try:
        with urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, dict) else {"error": "bad answer"}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"not_found": True}
        if e.code == 429:
            with _lock:
                _paused_until = time.time() + PAUSE_SECONDS
            config.logger.warning(f"cheburcheck: rate limit, pausing {PAUSE_SECONDS} s")
            return {"error": "busy"}
        return {"error": f"HTTP {e.code}"}
    except Exception as e:
        config.logger.warning(f"cheburcheck: request failed: {e}")
        return {"error": "no answer"}


def check(target: str) -> dict | None:
    """The cheburcheck answer for ``target`` (cached), or None when turned off."""
    if not enabled():
        return None
    key = target.lower()
    with _lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    if not _take():
        return {"error": "busy"}
    result = _fetch(target)
    if "error" not in result:  # failures are not kept: the next request tries again
        with _lock:
            _cache[key] = (time.time(), result)
            if len(_cache) > CACHE_MAX:
                oldest = min(_cache, key=lambda k: _cache[k][0])
                _cache.pop(oldest, None)
    return result


def _list(items: list[str]) -> str:
    more = f" and {len(items) - MAX_SHOWN} more" if len(items) > MAX_SHOWN else ""
    return ", ".join(items[:MAX_SHOWN]) + more


def report_lines(target: str) -> list[str]:
    """The RKN lines for a domain, address, CIDR block or "AS<n>"; [] when turned off."""
    data = check(target)
    if data is None:
        return []
    if data.get("error"):
        reason = "busy" if data["error"] == "busy" else "unavailable"
        return [f"{LABEL}: cheburcheck.ru {reason}, try later"]
    if data.get("not_found"):
        return [f"{LABEL}: cheburcheck.ru could not resolve it"]

    lines = []
    rkn_domain = data.get("rkn_domain")
    if rkn_domain:
        shown = data.get("target") or target
        lines.append(f"{LABEL}: ⛔ domain is in the registry"
                     + (f" (as {rkn_domain})" if rkn_domain.lower() != str(shown).lower() else ""))
    asn = data.get("asn_info") or {}
    if asn:
        blocked = asn.get("blocked_prefixes") or []
        if blocked:
            # cheburcheck counts registry subnets and throttled CDN networks together here
            lines.append(f"{LABEL}: ⛔ {len(blocked)} of {len(asn.get('prefixes') or [])} prefixes "
                         f"blocked or on a throttled CDN: {_list(blocked)}")
    elif data.get("blocked_subnets"):
        lines.append(f"{LABEL}: ⛔ in blocked subnets: {_list(data['blocked_subnets'])}")
    cdn = data.get("cdn_providers") or {}
    if cdn:
        lines.append(f"🐢 Throttled CDN in Russia (16-20 KB limit): {_list(sorted(cdn))}")
    wl = data.get("whitelist")
    if isinstance(wl, dict) and wl.get("domain"):
        last_ok = str(wl.get("last_ok") or wl.get("lastOk") or "")[:10]
        lines.append("✅ CDN exception: this name lifts the 16-20 KB limit"
                     + (f" (confirmed {last_ok})" if last_ok else ""))
    if not lines:
        lines.append(f"{LABEL}: ✅ not listed, not on a throttled CDN")

    days = [d.get("count") or 0 for d in data.get("complaints") or [] if isinstance(d, dict)]
    total = sum(days)
    if total:
        recent = sum(days[-7:])
        lines.append(f"📣 {total} \"does not work\" reports on cheburcheck.ru in 14 days ({recent} in the last 7)")
    return lines


def link_line(target: str) -> str | None:
    """The "🔎 cheburcheck.ru" link for the footer of a report; None when turned off."""
    if not enabled():
        return None
    return f"🔎 {WEB_URL}?{urllib.parse.urlencode({'target': target})}"


def clear_cache():
    with _lock:
        _cache.clear()
