"""Russian mobile internet whitelists: the "RU mobile whitelist" line of /ip and /dns.

When Russian operators "restrict" mobile internet, only whitelisted resources
stay reachable; a chatmail relay on a listed address keeps working for its
users. Data source: the community list
https://github.com/hxehex/russia-mobile-internet-whitelist (cidrwhitelist.txt,
IPv4 subnets in CIDR notation; its ipwhitelist.txt is a subset of them). It is
collected by hand from reports: operators, regions and even cell towers
differ, so a listed address is a good sign, not a guarantee.

The list (about 0.5 MB) is downloaded on first use and refreshed every
RU_WHITELIST_REFRESH_SECONDS; when a download fails the old copy is kept and
the next try waits RU_WHITELIST_RETRY_SECONDS. RU_WHITELIST_URL in .env points
to another list in the same format; an empty value turns the check off.
"""
import bisect
import ipaddress
import os
import threading
import time
import urllib.request

import config

DEFAULT_URL = "https://raw.githubusercontent.com/hxehex/russia-mobile-internet-whitelist/main/cidrwhitelist.txt"
SOURCE_PAGE = "https://github.com/hxehex/russia-mobile-internet-whitelist"
REFRESH_SECONDS = 6 * 3600
RETRY_SECONDS = 600
MAX_BYTES = 16 * 1024 * 1024
MAX_SHOWN = 5

LABEL = "📶 RU mobile whitelist"

_fetch_lock = threading.Lock()   # one download at a time; others wait for its result
_data: dict | None = None        # {"nets": {prefixlen: {network int}}, "starts": [...], "ends": [...], "count": n}
_loaded_at = 0.0
_next_try = 0.0


def _url() -> str:
    return os.environ.get("RU_WHITELIST_URL", DEFAULT_URL).strip().strip("'\"")


def enabled() -> bool:
    return bool(_url())


def _fetch(url: str) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": f"BouncerBot/{config.VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read(MAX_BYTES).decode("utf-8", errors="replace")
    except Exception as e:
        config.logger.warning(f"RU whitelist: download failed: {e}")
        return None


def parse(text: str) -> dict:
    """CIDR lines (IPv4; bare addresses count as /32) -> lookup tables.

    ``nets`` finds the most specific listed subnet of an address, the merged
    ``starts`` / ``ends`` intervals answer "how much of this block is listed".
    """
    nets: dict[int, set[int]] = {}
    intervals = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            n = ipaddress.ip_network(line, strict=False)
        except ValueError:
            continue
        if n.version != 4:
            continue
        nets.setdefault(n.prefixlen, set()).add(int(n.network_address))
        intervals.append((int(n.network_address), int(n.broadcast_address)))
    starts, ends = [], []
    for s, e in sorted(intervals):
        if ends and s <= ends[-1] + 1:
            ends[-1] = max(ends[-1], e)
        else:
            starts.append(s)
            ends.append(e)
    return {"nets": nets, "starts": starts, "ends": ends, "count": len(intervals)}


def _get() -> dict | None:
    """The current list, downloading it when missing or stale; None if unavailable."""
    global _data, _loaded_at, _next_try
    url = _url()
    if not url:
        return None
    now = time.time()
    if _data is not None and now - _loaded_at < REFRESH_SECONDS:
        return _data
    with _fetch_lock:
        now = time.time()
        if (_data is not None and now - _loaded_at < REFRESH_SECONDS) or now < _next_try:
            return _data  # fresh after waiting on the lock, or a failed try was recent
        text = _fetch(url)
        data = parse(text) if text else None
        if data and data["count"]:
            _data, _loaded_at = data, now
            config.logger.info(f"RU whitelist: loaded {data['count']} subnets")
        else:
            _next_try = now + RETRY_SECONDS
        return _data


def _subnet_of(data: dict, addr: int, max_len: int = 32) -> str | None:
    """Most specific listed subnet containing ``addr`` (no longer than ``max_len``)."""
    for plen in range(max_len, -1, -1):
        bases = data["nets"].get(plen)
        if bases:
            base = addr & ((0xFFFFFFFF << (32 - plen)) & 0xFFFFFFFF)
            if base in bases:
                return f"{ipaddress.IPv4Address(base)}/{plen}"
    return None


def _listed_in(data: dict, first: int, last: int) -> list[tuple[int, int]]:
    """Listed ranges inside [first, last], clipped to it."""
    starts, ends = data["starts"], data["ends"]
    i = max(bisect.bisect_right(starts, first) - 1, 0)
    out = []
    while i < len(starts) and starts[i] <= last:
        if ends[i] >= first:
            out.append((max(starts[i], first), min(ends[i], last)))
        i += 1
    return out


def _ranges_text(ranges: list[tuple[int, int]]) -> str:
    parts = []
    for s, e in ranges:
        nets = ipaddress.summarize_address_range(ipaddress.IPv4Address(s), ipaddress.IPv4Address(e))
        parts += [n.compressed for n in nets]
    more = f" and {len(parts) - MAX_SHOWN} more" if len(parts) > MAX_SHOWN else ""
    return ", ".join(parts[:MAX_SHOWN]) + more


def ip_status(ip) -> str | None:
    """Listed subnet of an IPv4 address, "" if not listed, None if the list is unavailable."""
    data = _get()
    if data is None:
        return None
    return _subnet_of(data, int(ipaddress.ip_address(ip))) or ""


def ip_line(ip) -> str | None:
    """The /ip line for an address; None when the check is turned off."""
    if not enabled():
        return None
    ip = ipaddress.ip_address(ip)
    if ip.version != 4:
        return f"{LABEL}: IPv4 only, not checked"
    status = ip_status(ip)
    if status is None:
        return f"{LABEL}: list unavailable, try later"
    if status:
        return f"{LABEL}: ✅ in {status} — may stay reachable when mobile internet is restricted"
    return f"{LABEL}: ❌ not listed — likely unreachable when mobile internet is restricted"


def net_line(net) -> str | None:
    """The /ip line for a block: listed as a whole, partly or not at all."""
    if not enabled():
        return None
    net = ipaddress.ip_network(net, strict=False)
    if net.version != 4:
        return f"{LABEL}: IPv4 only, not checked"
    data = _get()
    if data is None:
        return f"{LABEL}: list unavailable, try later"
    first, last = int(net.network_address), int(net.broadcast_address)
    ranges = _listed_in(data, first, last)
    listed = sum(e - s + 1 for s, e in ranges)
    if listed == last - first + 1:
        whole = _subnet_of(data, first, net.prefixlen)
        return f"{LABEL}: ✅ whole block listed" + (f" (in {whole})" if whole and whole != net.compressed else "")
    if ranges:
        share = listed * 100 / (last - first + 1)
        share = f"{share:.0f}%" if 0.5 <= share < 99.5 else ("<1%" if share < 0.5 else ">99%")
        return (f"{LABEL}: ⚠️ partly listed, {listed} of {net.num_addresses} addresses "
                f"({share}): {_ranges_text(ranges)}")
    return f"{LABEL}: ❌ not listed"


def prefixes_line(prefixes: list[str]) -> str | None:
    """The /ip line for an AS: how many of its announced IPv4 prefixes are listed."""
    if not enabled():
        return None
    v4 = []
    for p in prefixes:
        try:
            n = ipaddress.ip_network(p, strict=False)
        except ValueError:
            continue
        if n.version == 4:
            v4.append(n)
    if not v4:
        return None
    data = _get()
    if data is None:
        return f"{LABEL}: list unavailable, try later"
    hit = [n for n in v4 if _listed_in(data, int(n.network_address), int(n.broadcast_address))]
    if not hit:
        return f"{LABEL}: ❌ no announced IPv4 prefix listed"
    shown = ", ".join(n.compressed for n in hit[:MAX_SHOWN])
    more = f" and {len(hit) - MAX_SHOWN} more" if len(hit) > MAX_SHOWN else ""
    return f"{LABEL}: ✅ {len(hit)} of {len(v4)} IPv4 prefixes listed in whole or part: {shown}{more}"


def addresses_line(ips: list[str]) -> str | None:
    """One /dns line for several addresses: "1.2.3.4 ✅ (in 1.2.0.0/16), 5.6.7.8 ❌"."""
    if not enabled():
        return None
    v4 = []
    for a in ips:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        if ip.version == 4 and ip.compressed not in v4:
            v4.append(ip.compressed)
    if not v4:
        return None
    if _get() is None:
        return f"{LABEL}: list unavailable, try later"
    parts = []
    for a in v4:
        status = ip_status(a)
        parts.append(f"{a} ✅ (in {status})" if status else f"{a} ❌")
    return f"{LABEL}: {', '.join(parts)}"
