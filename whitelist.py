"""Russian mobile internet whitelists: the "RU mobile whitelist" lines of /ip and /dns.

When Russian operators "restrict" mobile internet, only whitelisted resources
stay reachable; a chatmail relay on a listed address keeps working for its
users. The lists are community-maintained, collected from reports -
operators, regions and even cell towers differ, so being listed is a good
sign, not a guarantee. Two checks, each with its own list:

- "ip": ru-mobile-whitelist-ip.txt of https://github.com/ancored/ru-mobile-whitelist,
  rebuilt daily from several public lists (hxehex, roscomvpn, d010b, overskye,
  escapingworm ...) minus the subnets reported as dropped from the whitelists;
  IPv4 subnets in CIDR notation (its IPv6 lines are skipped) - /ip for
  addresses, blocks and AS numbers, /dns for the addresses of the domain and
  its MX hosts.
- "domain": whitelist.txt of https://github.com/hxehex/russia-mobile-internet-whitelist,
  exact host names (the SNI the filters let through) - /dns for the domain
  itself and its listed subdomains. The ancored SNI list is not used: it
  folds hosts into parent domains (domain_suffix), which loses the exact names.

Each list is downloaded on first use and refreshed every REFRESH_SECONDS; when
a download fails the old copy is kept and the next try waits RETRY_SECONDS.
The admin turns each check on or off with /ruwhitelist (stored in the
database). Without that, RU_WHITELIST_URL / RU_WHITELIST_DOMAINS_URL in .env
decide: unset means on with the default list, another URL means a list in the
same format, an empty value means off.
"""
import bisect
import ipaddress
import os
import threading
import time
import urllib.request

from deltachat2 import events

import config
import database
import dc_helpers
import state

SOURCE_PAGES = ("https://github.com/ancored/ru-mobile-whitelist (IP subnets)",
                "https://github.com/hxehex/russia-mobile-internet-whitelist (domains)")
_RAW_IP = "https://raw.githubusercontent.com/ancored/ru-mobile-whitelist/release"
_RAW_DOMAINS = "https://raw.githubusercontent.com/hxehex/russia-mobile-internet-whitelist/main"
# kind -> (env variable, default URL, what the admin calls it)
LISTS = {
    "ip": ("RU_WHITELIST_URL", f"{_RAW_IP}/ru-mobile-whitelist-ip.txt", "IP subnets"),
    "domain": ("RU_WHITELIST_DOMAINS_URL", f"{_RAW_DOMAINS}/whitelist.txt", "domains"),
}
REFRESH_SECONDS = 6 * 3600
RETRY_SECONDS = 600
MAX_BYTES = 16 * 1024 * 1024
MAX_SHOWN = 5

LABEL = "📶 RU mobile whitelist"

_fetch_lock = threading.Lock()   # one download at a time; others wait for its result
# kind -> parsed list: "ip" {"nets": {prefixlen: {network int}}, "starts", "ends", "count"},
# "domain" {"names": set, "count"}
_data: dict[str, dict] = {}
_loaded_at: dict[str, float] = {}
_next_try: dict[str, float] = {}


def _env(kind: str) -> str:
    env, default, _ = LISTS[kind]
    return os.environ.get(env, default).strip().strip("'\"")


def _url(kind: str) -> str:
    return _env(kind) or LISTS[kind][1]  # turned on by /ruwhitelist despite an empty .env value


def setting(kind: str) -> str | None:
    """"on" / "off" as set by /ruwhitelist, or None (then .env decides)."""
    try:
        value = database.get_config(f"ru_whitelist_{kind}")
    except Exception as e:
        config.logger.warning(f"RU whitelist: could not read the setting: {e}")
        return None
    return value if value in ("on", "off") else None


def enabled(kind: str) -> bool:
    value = setting(kind)
    if value is not None:
        return value == "on"
    return bool(_env(kind))


def _fetch(url: str) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": f"BouncerBot/{config.VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read(MAX_BYTES).decode("utf-8", errors="replace")
    except Exception as e:
        config.logger.warning(f"RU whitelist: download of {url} failed: {e}")
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


def parse_domains(text: str) -> dict:
    """One host name per line -> {"names": set of lowercase ASCII names, "count"}."""
    names = set()
    for line in text.splitlines():
        name = line.split("#", 1)[0].strip().lower().rstrip(".")
        if not name or " " in name or "." not in name:
            continue
        try:
            name = name.encode("idna").decode("ascii") if not name.isascii() else name
        except UnicodeError:
            continue
        names.add(name)
    return {"names": names, "count": len(names)}


def _get(kind: str) -> dict | None:
    """The current list, downloading it when missing or stale; None if unavailable."""
    now = time.time()
    data = _data.get(kind)
    if data is not None and now - _loaded_at.get(kind, 0) < REFRESH_SECONDS:
        return data
    with _fetch_lock:
        now = time.time()
        data = _data.get(kind)
        if (data is not None and now - _loaded_at.get(kind, 0) < REFRESH_SECONDS) or now < _next_try.get(kind, 0):
            return data  # fresh after waiting on the lock, or a failed try was recent
        text = _fetch(_url(kind))
        new = (parse if kind == "ip" else parse_domains)(text) if text else None
        if new and new["count"]:
            _data[kind], _loaded_at[kind] = new, now
            config.logger.info(f"RU whitelist: loaded {new['count']} {LISTS[kind][2]}")
            return new
        _next_try[kind] = now + RETRY_SECONDS
        return data


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
    data = _get("ip")
    if data is None:
        return None
    return _subnet_of(data, int(ipaddress.ip_address(ip))) or ""


def ip_line(ip) -> str | None:
    """The /ip line for an address; None when the check is turned off."""
    if not enabled("ip"):
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
    if not enabled("ip"):
        return None
    net = ipaddress.ip_network(net, strict=False)
    if net.version != 4:
        return f"{LABEL}: IPv4 only, not checked"
    data = _get("ip")
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
    if not enabled("ip"):
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
    data = _get("ip")
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
    if not enabled("ip"):
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
    if _get("ip") is None:
        return f"{LABEL}: list unavailable, try later"
    parts = []
    for a in v4:
        status = ip_status(a)
        parts.append(f"{a} ✅ (in {status})" if status else f"{a} ❌")
    return f"{LABEL}: {', '.join(parts)}"


def domain_line(domain: str) -> str | None:
    """The /dns line for a name: listed itself, only some subdomains, or not at all.

    The list names exact hosts (the SNI filters let through), so a listed
    subdomain says nothing about the domain itself, and the other way round.
    """
    if not enabled("domain"):
        return None
    data = _get("domain")
    if data is None:
        return f"{LABEL} (domains): list unavailable, try later"
    domain = domain.lower().rstrip(".")
    shown = _display(domain)
    subs = sorted((n for n in data["names"] if n.endswith("." + domain)), key=lambda n: (n.count("."), n))
    more = f" and {len(subs) - MAX_SHOWN} more" if len(subs) > MAX_SHOWN else ""
    listed_subs = ", ".join(map(_display, subs[:MAX_SHOWN])) + more
    if domain in data["names"]:
        return f"{LABEL} (domains): ✅ {shown} is listed" + (f"; also {listed_subs}" if subs else "")
    if subs:
        return f"{LABEL} (domains): ⚠️ {shown} is not listed, only {len(subs)} of its subdomains: {listed_subs}"
    return f"{LABEL} (domains): ❌ {shown} is not listed"


def _display(name: str) -> str:
    try:
        return name.encode("ascii").decode("idna") if "xn--" in name else name
    except UnicodeError:
        return name


# ── /ruwhitelist (admin): turn the checks on or off ──

_KIND_WORDS = {"ip": "ip", "ips": "ip", "subnets": "ip", "domain": "domain", "domains": "domain"}


def _clear_report_caches():
    """Cached /ip and /dns answers carry the old lines; drop them."""
    with state._ip_lookup_lock:
        state._ip_lookup_cache.clear()
    with state._dns_lock:
        state._dns_cache.clear()


def _status_line(kind: str) -> str:
    _, _, label = LISTS[kind]
    value = setting(kind)
    how = "set by /ruwhitelist" if value else f"default, {LISTS[kind][0]} in .env"
    line = f"• {label} ({kind}): {'✅ on' if enabled(kind) else '❌ off'} ({how})"
    data = _data.get(kind)
    if data is not None:
        age = int((time.time() - _loaded_at.get(kind, 0)) // 60)
        line += f", {data['count']} entries loaded {age} min ago"
    return line


def command_reply(payload: str) -> str:
    """Apply ``/ruwhitelist [ip|domains] [on|off]`` and describe the state."""
    words = (payload or "").lower().split()
    usage = "Usage: /ruwhitelist [ip|domains] [on|off]"
    kinds = list(LISTS)
    if words and words[0] in _KIND_WORDS:
        kinds = [_KIND_WORDS[words.pop(0)]]
    if len(words) > 1 or (words and words[0] not in ("on", "off")):
        return usage
    if words:
        for kind in kinds:
            database.set_config(f"ru_whitelist_{kind}", words[0])
        _clear_report_caches()
    lines = ["📶 **RU mobile whitelist checks** (/ip, /dns)"]
    lines += [_status_line(k) for k in LISTS]
    lines += [f"Source: {page}" for page in SOURCE_PAGES]
    lines.append(usage)
    return "\n".join(lines)


@config.dc_cli.on(events.NewMessage(command="/ruwhitelist"))
def ruwhitelist_command(bot, accid, event):
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
        return
    dc_helpers._send(bot, accid, msg.chat_id, command_reply(event.payload))
