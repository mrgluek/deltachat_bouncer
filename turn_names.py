"""Which TURN server did a caller's app use?

A Delta Chat app gets its TURN servers from its relays (IMAP METADATA); when
none of its relays announces one, Delta Chat core falls back to
turn.delta.chat (``create_fallback_ice_servers`` in core's calls.rs). The app
then offers a *relay* ICE candidate whose address is the TURN server's relayed
address - for chatmail relays and turn.delta.chat simply the server's IP. So
the relay candidates tell which TURN servers the caller could reach; no relay
candidate at all means its TURN server did not answer (blocked, down, or too
slow).

Names come from forward DNS: the bot resolves the TURN host names it knows
(turn.delta.chat, its own relays, the relays it monitors, CALL_TURN_HOSTS) and
matches IP addresses. Reverse DNS would not work: the PTR record of a TURN
server is usually the hosting provider's name.

Only server addresses are looked at; the candidates' ``raddr`` (the caller's
own address) is never read.
"""
import ipaddress
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Iterable, Optional

import config

FALLBACK_TURN_HOST = "turn.delta.chat"
# Public credentials of the fallback server, as in Delta Chat core
FALLBACK_TURN_PORT = 3478
FALLBACK_TURN_USER = "public"
FALLBACK_TURN_PASSWORD = "o4tR7yG4rG2slhXqRUf9zgmHz"
CACHE_TTL_S = 3600
RESOLVE_TIMEOUT_S = 5.0
OTHER = "other"

# a=candidate:<foundation> <component> <transport> <priority> <address> <port> typ relay ...
_RELAY_RE = re.compile(r"^(?:a=)?candidate:\S+ \d+ \S+ \d+ (\S+) \d+ typ relay\b", re.MULTILINE)

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "hosts": (), "names": {}}


def _norm(ip: str) -> Optional[str]:
    try:
        return ipaddress.ip_address(ip.strip("[]")).compressed
    except ValueError:
        return None


def relay_ips_from_sdp(sdp: Optional[str]) -> list[str]:
    """Relay candidate addresses (= TURN servers) in an SDP, in order, unique."""
    out = []
    for addr in _RELAY_RE.findall(sdp or ""):
        ip = _norm(addr)
        if ip and ip not in out:
            out.append(ip)
    return out


def relay_ips_from_candidates(candidates: Iterable) -> list[str]:
    """Same for aioice ``Candidate`` objects (SDP plus trickled candidates)."""
    out = []
    for c in candidates:
        if getattr(c, "type", None) == "relay":
            ip = _norm(str(getattr(c, "host", "")))
            if ip and ip not in out:
                out.append(ip)
    return out


def known_hosts(bot=None, accid=None) -> list[str]:
    """TURN host names to recognize: turn.delta.chat, CALL_TURN_HOSTS, the
    bot's relays and the relays it monitors (chatmail relays run TURN under
    their mail domain)."""
    hosts = [FALLBACK_TURN_HOST] + list(config.CALL_TURN_HOSTS)
    if bot is not None:
        try:
            import dc_helpers

            hosts += list(dc_helpers._get_bot_domains(bot, accid))
        except Exception as e:
            config.logger.debug(f"TURN names: bot domains unavailable: {e}")
    try:
        import database

        hosts += list(database.get_all_cmping_monitors())
    except Exception as e:
        config.logger.debug(f"TURN names: monitor list unavailable: {e}")
    out = []
    for h in hosts:
        if not isinstance(h, str):
            continue
        h = h.strip().lower().rstrip(".")
        if h and h not in out:
            out.append(h)
    return out


def _resolve(host: str) -> set[str]:
    try:
        infos = socket.getaddrinfo(host, 3478, proto=socket.IPPROTO_UDP)
    except OSError:
        return set()
    return {ip for ip in (_norm(i[4][0]) for i in infos) if ip}


def ip_names(hosts: list[str]) -> dict[str, str]:
    """{ip: host name} for ``hosts``, resolved in parallel and cached for an hour.

    The first host listed wins when two names share an address."""
    now = time.time()
    with _lock:
        if _cache["hosts"] == tuple(hosts) and now - _cache["at"] < CACHE_TTL_S:
            return dict(_cache["names"])
    names: dict[str, str] = {}
    pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="turn-dns")
    try:
        futures = {h: pool.submit(_resolve, h) for h in hosts}
        wait(list(futures.values()), timeout=RESOLVE_TIMEOUT_S)
        for h in hosts:  # in order: first name wins
            f = futures[h]
            if f.done():
                for ip in f.result():
                    names.setdefault(ip, h)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    with _lock:
        _cache.update(at=now, hosts=tuple(hosts), names=dict(names))
    return names


def name_relays(ips: list[str], names: dict[str, str]) -> list[str]:
    """Host names for relay addresses; unknown servers become "other"."""
    out = []
    for ip in ips:
        n = names.get(ip, OTHER)
        if n not in out:
            out.append(n)
    return out


def caller_turn(ips: list[str], bot=None, accid=None) -> list[str]:
    """Names of the TURN servers behind ``ips`` ([] when there were none)."""
    if not ips:
        return []
    return name_relays(ips, ip_names(known_hosts(bot, accid)))
