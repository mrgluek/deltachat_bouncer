"""IP / prefix / AS lookup: the /ip command.

Data sources:
- ipinfo.io for a single IP address (city, region, country, organization,
  host name; VPN/proxy/hosting flags only on a paid token). Works without a
  token; IPINFO_TOKEN in .env is tried first when set.
- RIPEstat (stat.ripe.net) for the announced prefix of an IP, for prefixes
  and for AS numbers. Free, no key, and global - it uses BGP data from all
  regions, not only the RIPE region. ipinfo's own ASN and IP-range APIs are
  paid plans only, so they are not used.

All lookups share one global queue with at least IP_LOOKUP_INTERVAL_SECONDS
between them (both services rate-limit per client IP, and the bot's IP is one
client for every chat). Answers are cached for an hour; a cached answer is
sent right away without waiting in the queue.
"""
import ipaddress
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from deltachat2 import events

import config
import dc_helpers
import state

IPINFO_URL = "https://ipinfo.io"
RIPESTAT_URL = "https://stat.ripe.net/data"
MAX_PREFIXES_SHOWN = 10

USAGE = (
    "Usage:\n"
    "• `/ip <address>` — IP address: location, network and AS\n"
    "• `/ip <prefix>` — Network block, e.g. 84.64.0.0/13\n"
    "• `/ip AS<number>` — Autonomous system, e.g. AS5378\n"
    "• Reply to a message with `/ip` to look up the first address, block or AS in it"
)

# Candidates are found with loose patterns and then validated with the
# ipaddress module, so "2.28.0", "12:30:45" or a MAC address are skipped.
# IPv4: the look-arounds stop "1.2.3.4.5" or "v1.2.3.4" from matching a part;
# a trailing sentence dot ("see 1.2.3.4.") is still allowed.
_IPV4_RE = r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?!\.?\d)"
# IPv6: at least two colons; "[2001:db8::1]:443" matches without the brackets.
_IPV6_RE = r"(?<![\w:])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?:/\d{1,3})?(?![\w:])"
# "AS5378" / "as 5378" / "ASN5378"; a bare number is an AS only as /ip's argument.
_ASN_RE = r"(?<![\w])ASN?\s?(\d{1,10})(?![\w])"
_TARGET_RE = re.compile(f"(?P<asn>{_ASN_RE})|(?P<ip>{_IPV4_RE}|{_IPV6_RE})", re.IGNORECASE)


def _parse_candidate(token: str):
    """('ip', IPv4Address/IPv6Address) or ('net', IPv4Network/IPv6Network), or None."""
    try:
        if "/" in token:
            # strict=False: "84.65.49.143/13" means the /13 that contains it
            return "net", ipaddress.ip_network(token, strict=False)
        return "ip", ipaddress.ip_address(token)
    except ValueError:
        return None


def _is_public(obj) -> bool:
    if isinstance(obj, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
        return obj.network_address.is_global and obj.broadcast_address.is_global
    if getattr(obj, "ipv4_mapped", None):  # ::ffff:1.2.3.4
        return obj.ipv4_mapped.is_global
    return obj.is_global


def find_target(text: str, bare_asn: bool = False):
    """First IP address, network or AS number in ``text``.

    Returns (kind, value) with kind 'ip' | 'net' | 'asn' (value is an int for
    'asn'), or None. Private and reserved addresses are skipped, so a log line
    such as "10.0.0.5 -> 84.65.49.143" yields the public address.
    With ``bare_asn`` a lone number ("5378") is an AS - used for /ip's argument.
    """
    if not text:
        return None
    if bare_asn and text.strip().isdigit():
        n = int(text.strip())
        return ("asn", n) if 0 < n < 2**32 else None
    for m in _TARGET_RE.finditer(text):
        if m.group("asn"):
            n = int(re.sub(r"\D", "", m.group("asn")))
            if 0 < n < 2**32:
                return "asn", n
            continue
        parsed = _parse_candidate(m.group("ip"))
        if parsed and _is_public(parsed[1]):
            return parsed
    return None


def _http_get_json(url: str, timeout: int = 20) -> tuple[dict | None, int]:
    """GET a JSON document. Returns (data, http_status); data is None on failure."""
    req = urllib.request.Request(url, headers={
        "User-Agent": f"BouncerBot/{config.VERSION}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), resp.status
    except urllib.error.HTTPError as e:
        return None, e.code
    except Exception as e:
        config.logger.warning(f"/ip: request to {url.split('?')[0]} failed: {e}")
        return None, 0


def _ripestat(call: str, resource: str) -> dict | None:
    # sourceapp identifies the caller, as RIPEstat asks of API users
    q = urllib.parse.urlencode({"resource": resource, "sourceapp": "bouncerbot"})
    data, _ = _http_get_json(f"{RIPESTAT_URL}/{call}/data.json?{q}", timeout=30)
    if not data or data.get("status") != "ok":
        return None
    return data.get("data") or None


def _ipinfo_token() -> str:
    return os.environ.get("IPINFO_TOKEN", "").strip().strip("'\"")


def _ipinfo_ip(ip: str) -> dict | None:
    """ipinfo.io data for an IP, trying the configured token, then no token,
    then the free Lite API (country + AS only, unlimited with a free token).

    A token from ipinfo's current free plan only works on the Lite API, an old
    free or a paid token also on the full endpoint - hence the fallbacks.
    """
    token = _ipinfo_token()
    attempts = []
    if token:
        attempts.append(f"{IPINFO_URL}/{ip}/json?token={urllib.parse.quote(token)}")
    attempts.append(f"{IPINFO_URL}/{ip}/json")
    if token:
        attempts.append(f"https://api.ipinfo.io/lite/{ip}?token={urllib.parse.quote(token)}")
    for url in attempts:
        data, status = _http_get_json(url)
        if data and not data.get("error") and not data.get("bogon"):
            return data
        if status:
            config.logger.info(f"/ip: ipinfo answered HTTP {status}, trying the next way")
    return None


def _flag(cc: str | None) -> str:
    """Country code -> flag emoji ("GB" -> 🇬🇧)."""
    if not cc or len(cc) != 2 or not cc.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in cc.upper())


def _country_line(cc: str | None, name: str | None = None) -> str | None:
    if not cc and not name:
        return None
    label = f"{name} ({cc.upper()})" if name and cc else (name or cc.upper())
    return f"🌍 Country: {label} {_flag(cc)}".rstrip()


def _ripe_country(resource: str) -> str | None:
    """Registration country of a prefix or AS (from the RIR statistics)."""
    data = _ripestat("rir-stats-country", resource)
    for r in (data or {}).get("located_resources") or []:
        if r.get("location"):
            return r["location"]
    return None


def _format_ip(ip) -> str:
    ip_s = ip.compressed
    info = _ipinfo_ip(ip_s) or {}
    net = _ripestat("network-info", ip_s) or {}
    prefix = net.get("prefix") or ""
    asns = net.get("asns") or []

    lines = [f"🌐 **{ip_s}**"]
    if info.get("hostname"):
        lines.append(f"🖥 Host: {info['hostname']}")

    # Full endpoint: country is a code; Lite API: country is a name, code in country_code
    cc = info.get("country_code") or (info.get("country") if len(info.get("country") or "") == 2 else None)
    place = ", ".join(x for x in (info.get("city"), info.get("region")) if x)
    if place:
        lines.append(f"📍 {place}, {cc or ''} {_flag(cc)}".rstrip(", "))
    else:
        # Lite API: "country" is the name (with country_code); full endpoint: it is the code
        c = _country_line(cc, info.get("country") if info.get("country_code") else None)
        if c:
            lines.append(c)
    if info.get("timezone"):
        lines.append(f"🕐 Timezone: {info['timezone']}")

    # AS: "org" on the full endpoint, asn/as_name on Lite, an "asn" object on paid plans
    org = info.get("org")
    if not org and isinstance(info.get("asn"), dict):
        org = f"{info['asn'].get('asn', '')} {info['asn'].get('name', '')}".strip()
    if not org and info.get("asn"):
        org = f"{info['asn']} {info.get('as_name', '')}".strip()
    if org:
        lines.append(f"🏢 {org}")
    elif asns:
        lines.append(f"🏢 AS{asns[0]}")
    if isinstance(info.get("company"), dict) and info["company"].get("name"):
        lines.append(f"🏬 Company: {info['company']['name']}")

    if prefix:
        origin = ", ".join(f"AS{a}" for a in asns)
        lines.append(f"🧱 Prefix: {prefix}" + (f" (announced by {origin})" if origin else ""))
    else:
        lines.append("🧱 Prefix: not announced in BGP")

    # Paid plans only: privacy / connection type
    privacy = info.get("privacy")
    if isinstance(privacy, dict):
        flags = [k.upper() if k in ("vpn", "tor") else k for k in ("vpn", "proxy", "tor", "relay", "hosting") if privacy.get(k)]
        if privacy.get("service"):
            flags.append(privacy["service"])
        lines.append(f"🕵️ Privacy: {', '.join(flags)}" if flags else "🕵️ Privacy: no VPN / proxy / Tor / hosting")

    if not info and not net:
        return f"❌ Could not look up {ip_s}: ipinfo.io and RIPEstat did not answer. Try again later."
    lines.append("")
    lines.append(f"🔎 {IPINFO_URL}/{ip_s}")
    if prefix and asns:
        lines.append(f"🔎 {IPINFO_URL}/AS{asns[0]}/{prefix}")
    return "\n".join(lines)


def _format_net(net) -> str:
    net_s = net.compressed
    data = _ripestat("prefix-overview", net_s)
    if data is None:
        return f"❌ Could not look up {net_s}: RIPEstat did not answer. Try again later."

    lines = [f"🧱 **{net_s}**"]
    asns = data.get("asns") or []
    announced_as = data.get("resource") or net_s
    if data.get("announced") and data.get("is_less_specific"):
        # The block itself is not in BGP; a bigger prefix containing it is
        lines.append(f"📡 Part of announced prefix {announced_as}")
    elif data.get("announced"):
        lines.append("📡 Announced in BGP")
    else:
        lines.append("📡 Not announced in BGP")
    for a in asns:
        lines.append(f"🏢 AS{a.get('asn')} {a.get('holder') or ''}".rstrip())

    related = data.get("related_prefixes") or []
    if related:
        shown = ", ".join(related[:MAX_PREFIXES_SHOWN])
        more = f" and {len(related) - MAX_PREFIXES_SHOWN} more" if len(related) > MAX_PREFIXES_SHOWN else ""
        lines.append(f"🔀 More specific announcements: {shown}{more}")

    c = _country_line(_ripe_country(net_s))
    if c:
        lines.append(c)
    block = data.get("block") or {}
    if block.get("resource"):
        lines.append(f"🗂 Registry block: {block['resource']} — {block.get('desc') or ''}".rstrip(" —"))

    lines.append("")
    if asns:
        lines.append(f"🔎 {IPINFO_URL}/AS{asns[0].get('asn')}/{announced_as}")
    lines.append(f"🔎 https://stat.ripe.net/{net_s}")
    return "\n".join(lines)


def _prefix_sort_key(p: str):
    n = ipaddress.ip_network(p, strict=False)
    return (n.version, n.prefixlen, int(n.network_address))


def _format_asn(asn: int) -> str:
    overview = _ripestat("as-overview", f"AS{asn}")
    if overview is None:
        return f"❌ Could not look up AS{asn}: RIPEstat did not answer. Try again later."

    holder = overview.get("holder")
    lines = [f"🏢 **AS{asn}**" + (f" — {holder}" if holder else "")]
    if not holder:
        lines.append("❔ Not registered or no holder data")

    c = _country_line(_ripe_country(f"AS{asn}"))
    if c:
        lines.append(c)

    pfx = _ripestat("announced-prefixes", f"AS{asn}") or {}
    prefixes = sorted({p["prefix"] for p in pfx.get("prefixes") or [] if p.get("prefix")}, key=_prefix_sort_key)
    if prefixes:
        v4 = [p for p in prefixes if ":" not in p]
        v6 = [p for p in prefixes if ":" in p]
        lines.append(f"📡 Announced prefixes: {len(prefixes)} (IPv4 {len(v4)}, IPv6 {len(v6)})")
        # Largest blocks first: they say the most about the network
        for p in prefixes[:MAX_PREFIXES_SHOWN]:
            lines.append(f"  • {p}")
        if len(prefixes) > MAX_PREFIXES_SHOWN:
            lines.append(f"  … and {len(prefixes) - MAX_PREFIXES_SHOWN} more")
    elif overview.get("announced") is False or pfx:
        lines.append("📡 Announces no prefixes")

    lines.append("")
    lines.append(f"🔎 {IPINFO_URL}/AS{asn}")
    lines.append(f"🔎 https://stat.ripe.net/AS{asn}")
    return "\n".join(lines)


def lookup(kind: str, value) -> str:
    if kind == "asn":
        return _format_asn(value)
    if kind == "net":
        return _format_net(value)
    return _format_ip(value)


def _cache_key(kind: str, value) -> str:
    return f"{kind}:{value}"


def _cached(key: str) -> str | None:
    with state._ip_lookup_lock:
        hit = state._ip_lookup_cache.get(key)
        if hit and time.time() - hit[0] < config.IP_LOOKUP_CACHE_SECONDS:
            return hit[1]
    return None


def _store(key: str, text: str):
    with state._ip_lookup_lock:
        state._ip_lookup_cache[key] = (time.time(), text)
        if len(state._ip_lookup_cache) > 500:  # drop the oldest
            oldest = min(state._ip_lookup_cache, key=lambda k: state._ip_lookup_cache[k][0])
            state._ip_lookup_cache.pop(oldest, None)


def bg_ip_worker(bot, accid, chat_id, msg_id, kind, value):
    """Run one lookup in turn: one at a time, IP_LOOKUP_INTERVAL_SECONDS apart."""
    key = _cache_key(kind, value)
    try:
        with state._ip_lookup_queue_lock:  # FIFO-ish: waiting threads queue on the lock
            wait = config.IP_LOOKUP_INTERVAL_SECONDS - (time.time() - state._ip_lookup_last_time)
            if wait > 0:
                time.sleep(wait)
            text = _cached(key)  # an identical request ahead in the queue may have filled it
            if text is None:
                text = lookup(kind, value)
                state._ip_lookup_last_time = time.time()
                if not text.startswith("❌"):
                    _store(key, text)
        dc_helpers._react(bot, accid, msg_id, "❌" if text.startswith("❌") else "☑️")
        dc_helpers._send(bot, accid, chat_id, text, reply_to_id=msg_id)
    except Exception as e:
        config.logger.error(f"Error in bg_ip_worker: {e}", exc_info=True)
        dc_helpers._react(bot, accid, msg_id, "❌")
        dc_helpers._send(bot, accid, chat_id, f"❌ Lookup failed: {e}", reply_to_id=msg_id)
    finally:
        with state._ip_lookup_lock:
            state._ip_lookup_pending -= 1


def _quoted_text(bot, accid, msg) -> str:
    quote = getattr(msg, "quote", None)
    if not isinstance(quote, dict):
        return ""
    qid = quote.get("message_id") or quote.get("messageId")
    if qid:
        try:
            q = bot.rpc.get_message(accid, qid)
            text = getattr(q, "text", "") if not isinstance(q, dict) else q.get("text", "")
            if isinstance(text, str) and text:
                return text
        except Exception as e:
            config.logger.warning(f"Failed to fetch quoted message for /ip: {e}")
    text = quote.get("text", "")  # the quote excerpt, if the message itself is gone
    return text if isinstance(text, str) else ""


@config.dc_cli.on(events.NewMessage(command="/ip"))
def ip_command(bot, accid, event):
    msg = event.msg
    payload = (event.payload or "").strip()

    if payload:
        target = find_target(payload, bare_asn=True)
        if not target:
            parsed = _parse_candidate(payload.split()[0])
            reason = ("is a private or reserved address" if parsed and not _is_public(parsed[1])
                      else "is not an IP address, network or AS number")
            dc_helpers._send(bot, accid, msg.chat_id, f"❌ `{payload[:60]}` {reason}.\n\n{USAGE}", reply_to_id=msg.id)
            return
    else:
        quoted = _quoted_text(bot, accid, msg)
        if not quoted:
            dc_helpers._send(bot, accid, msg.chat_id, USAGE, reply_to_id=msg.id)
            return
        target = find_target(quoted)
        if not target:
            dc_helpers._send(bot, accid, msg.chat_id,
                             "❌ No public IP address, network or AS number found in that message.",
                             reply_to_id=msg.id)
            return

    kind, value = target
    cached = _cached(_cache_key(kind, value))
    if cached is not None:
        dc_helpers._send(bot, accid, msg.chat_id, cached, reply_to_id=msg.id)
        return

    with state._ip_lookup_lock:
        if state._ip_lookup_pending >= config.IP_LOOKUP_MAX_QUEUE:
            busy = True
        else:
            busy = False
            state._ip_lookup_pending += 1
            ahead = state._ip_lookup_pending - 1
    if busy:
        dc_helpers._send(bot, accid, msg.chat_id, "⏳ Too many lookups queued, please try again in a minute.", reply_to_id=msg.id)
        return

    dc_helpers._react(bot, accid, msg.id, "⏳")
    if ahead:
        config.logger.info(f"/ip: queued behind {ahead} lookup(s)")
    threading.Thread(target=bg_ip_worker, args=(bot, accid, msg.chat_id, msg.id, kind, value), daemon=True).start()
