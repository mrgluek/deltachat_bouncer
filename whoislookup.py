"""Registration data: the /whois command.

- Domains: RDAP (the JSON successor of WHOIS) when the TLD has it - IANA
  publishes which server serves which TLD - else classic WHOIS on TCP port 43,
  the server named by whois.iana.org (.ru -> whois.tcinet.ru, .io, .de ...).
  Both are shown in the same fields. A subdomain is looked up as its
  registered domain, found with the Public Suffix List (mail.chatmail.uk ->
  chatmail.uk, a.example.msk.ru -> example.msk.ru).
- IP addresses, networks and AS numbers: RDAP of the regional registry
  (RIPE, ARIN, APNIC, LACNIC, AFRINIC) - holder, country, block and the
  abuse contact.

Registries throttle per client IP (tcinet and DENIC block quickly), so
requests to one server are at least WHOIS_HOST_INTERVAL_SECONDS apart;
lookups run on a small pool, the rest wait in the queue, answers are cached.
"""
import ipaddress
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from deltachat2 import events

import config
import dc_helpers
import iplookup
import state

IANA_RDAP = "https://data.iana.org/rdap/{}.json"  # dns, ipv4, ipv6, asn
PSL_URL = "https://publicsuffix.org/list/public_suffix_list.dat"
PSL_FILE = os.path.join(os.getenv("DC_DB_DIR", "data"), "public_suffix_list.dat")
BOOTSTRAP_TTL = 24 * 3600
WHOIS_MAX_BYTES = 64 * 1024
EXPIRY_WARN_DAYS = 30

USAGE = (
    "Usage:\n"
    "• `/whois <domain>` — Registrar, dates, status, name servers, owner if public "
    "(any TLD: .com .net .org .info .xyz .cc .ru .рф .su .io …)\n"
    "• `/whois <IP | prefix | AS>` — Holder of an address block or AS, country and abuse contact\n"
    "• Reply to a message with `/whois` to check the first address, AS or link in it"
)

# Lines of a port-43 answer that say the domain is not registered
_NOT_FOUND_RE = re.compile(
    r"no entries found|^%? ?no match|not found|no data found|no object found|is free|"
    r"^status:\s*(free|available)|available for registration|domain not registered|object does not exist",
    re.IGNORECASE | re.MULTILINE)

# Port-43 field names (lower case) -> our field; first match wins
_WHOIS_FIELDS = {
    "registrar": ("registrar", "sponsoring registrar", "registrar name", "registrar organization"),
    "created": ("created", "creation date", "registered on", "registration time", "registered", "domain registration date"),
    "updated": ("updated date", "last updated", "last-update", "changed", "modified", "last modified"),
    "expires": ("paid-till", "registry expiry date", "registrar registration expiration date", "expiry date",
                "expiration date", "expire date", "expires", "expires on", "valid until"),
    "free_date": ("free-date",),
    "status": ("state", "domain status", "status"),
    "ns": ("nserver", "name server", "nameserver", "nameservers", "name servers"),
    "org": ("org", "registrant organization", "registrant organisation", "organization", "registrant"),
    "country": ("registrant country", "registrant country code", "country"),
    "dnssec": ("dnssec",),
    "abuse": ("registrar abuse contact email", "abuse-mailbox", "abuse contact"),
}
_REDACTED_RE = re.compile(r"redacted|privacy|withheld|not disclosed|data protected|gdpr", re.IGNORECASE)


# ── HTTP / port 43 with per-server spacing ──

def _wait_turn(host: str):
    """Hold this thread until ``host`` was last asked WHOIS_HOST_INTERVAL_SECONDS ago."""
    with state._whois_lock:
        lock = state._whois_host_locks.setdefault(host, threading.Lock())
    with lock:
        wait = config.WHOIS_HOST_INTERVAL_SECONDS - (time.time() - state._whois_host_last.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        state._whois_host_last[host] = time.time()


def _get_json(url: str, timeout: int = 20) -> tuple[dict | None, int]:
    _wait_turn(urllib.parse.urlsplit(url).hostname or url)
    req = urllib.request.Request(url, headers={
        "User-Agent": f"BouncerBot/{config.VERSION}",
        "Accept": "application/rdap+json, application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read(2 * 1024 * 1024).decode("utf-8")), resp.status
    except urllib.error.HTTPError as e:
        return None, e.code
    except Exception as e:
        config.logger.warning(f"/whois: {url.split('?')[0]} failed: {e}")
        return None, 0


def whois43(server: str, query: str, timeout: int = 15) -> str | None:
    """Raw answer of a port-43 WHOIS server, or None."""
    _wait_turn(server)
    try:
        with socket.create_connection((server, 43), timeout=timeout) as s:
            s.sendall(query.encode("idna" if not query.isascii() else "ascii") + b"\r\n")
            chunks, size = [], 0
            while size < WHOIS_MAX_BYTES:
                data = s.recv(8192)
                if not data:
                    break
                chunks.append(data)
                size += len(data)
    except Exception as e:
        config.logger.warning(f"/whois: {server}:43 failed: {e}")
        return None
    raw = b"".join(chunks)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


# ── Bootstrap data (IANA RDAP registry, WHOIS servers, Public Suffix List) ──

def _bootstrap(kind: str) -> list:
    """IANA's RDAP services for 'dns' | 'ipv4' | 'ipv6' | 'asn': [[keys], [urls]]."""
    with state._whois_lock:
        hit = state._whois_bootstrap.get(kind)
        if hit and time.time() - hit[0] < BOOTSTRAP_TTL:
            return hit[1]
    data, _ = _get_json(IANA_RDAP.format(kind))
    services = (data or {}).get("services") or []
    if services:
        with state._whois_lock:
            state._whois_bootstrap[kind] = (time.time(), services)
    elif hit:
        return hit[1]  # stale is better than nothing
    return services


def _base(urls: list[str]) -> str:
    url = next((u for u in urls if u.startswith("https://")), urls[0])
    return url if url.endswith("/") else url + "/"


def rdap_base_for_tld(tld: str) -> str | None:
    for tlds, urls in _bootstrap("dns"):
        if tld in tlds:
            return _base(urls)
    return None


def rdap_base_for_ip(net) -> str | None:
    best = None
    for prefixes, urls in _bootstrap(f"ipv{net.version}"):
        for p in prefixes:
            try:
                cand = ipaddress.ip_network(p)
            except ValueError:
                continue
            if net.subnet_of(cand) and (best is None or cand.prefixlen > best[0].prefixlen):
                best = (cand, _base(urls))
    return best[1] if best else None


def rdap_base_for_asn(asn: int) -> str | None:
    for ranges, urls in _bootstrap("asn"):
        for r in ranges:
            lo, _, hi = r.partition("-")
            if int(lo) <= asn <= int(hi or lo):
                return _base(urls)
    return None


def whois_server_for_tld(tld: str) -> str | None:
    """Port-43 server of a TLD, as whois.iana.org names it (cached)."""
    with state._whois_lock:
        if tld in state._whois_servers:
            return state._whois_servers[tld]
    answer = whois43("whois.iana.org", tld) or ""
    m = re.search(r"^whois:\s*(\S+)", answer, re.MULTILINE)
    server = m.group(1) if m else None
    if answer:  # cache "no server" too, but not a failed request
        with state._whois_lock:
            state._whois_servers[tld] = server
    return server


def _load_psl() -> set[str] | None:
    """ICANN section of the Public Suffix List as a set of rules ('co.uk', '*.ck', '!www.ck')."""
    with state._whois_lock:
        hit = state._whois_psl
        if hit and time.time() - hit[0] < BOOTSTRAP_TTL:
            return hit[1]
    text = None
    try:
        req = urllib.request.Request(PSL_URL, headers={"User-Agent": f"BouncerBot/{config.VERSION}"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = resp.read().decode("utf-8")
        os.makedirs(os.path.dirname(PSL_FILE) or ".", exist_ok=True)
        with open(PSL_FILE, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        config.logger.warning(f"/whois: Public Suffix List download failed ({e}), using the saved copy")
        try:
            with open(PSL_FILE, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            pass
    if not text:
        return hit[1] if hit else None
    rules = set()
    for line in text.split("===END ICANN DOMAINS===")[0].splitlines():
        line = line.strip()
        if line and not line.startswith("//"):
            rules.add(line.encode("idna").decode("ascii") if not line.isascii() else line.lower())
    with state._whois_lock:
        state._whois_psl = (time.time(), rules)
    return rules


def registered_domain(name: str, rules: set[str] | None) -> str | None:
    """'mail.chatmail.uk' -> 'chatmail.uk'; None when the name is itself a public suffix.

    Standard PSL algorithm: the longest matching rule, '*' matches one label,
    '!' rules are exceptions. Without the list, the last two labels.
    """
    labels = name.split(".")
    if not rules:
        return ".".join(labels[-2:]) if len(labels) >= 2 else None
    suffix_len = 1  # unlisted TLD: the default rule "*"
    for i in range(len(labels)):
        cand = ".".join(labels[i:])
        wild = ".".join(["*"] + labels[i + 1:])
        if "!" + cand in rules:
            suffix_len = len(labels) - i - 1
            break
        if cand in rules or wild in rules:
            suffix_len = max(suffix_len, len(labels) - i)
    if len(labels) <= suffix_len:
        return None
    return ".".join(labels[-(suffix_len + 1):])


# ── Parsing ──

def _vcard(entity: dict) -> dict:
    """{'fn': ..., 'org': ..., 'email': ..., 'country': ...} from an RDAP entity's jCard."""
    out = {}
    card = entity.get("vcardArray")
    if not (isinstance(card, list) and len(card) == 2):
        return out
    for prop in card[1]:
        if not isinstance(prop, list) or len(prop) < 4:
            continue
        name, params, value = prop[0], prop[1] or {}, prop[3]
        if name in ("fn", "org", "email") and value and name not in out:
            out[name] = value[0] if isinstance(value, list) else value
        elif name == "adr":
            cc = params.get("cc")
            if not cc and isinstance(value, list) and value and value[-1]:
                cc = value[-1]
            if not cc and isinstance(params.get("label"), str):
                cc = params["label"].splitlines()[-1]
            if cc and isinstance(cc, str):
                out["country"] = cc
    return out


def _walk(entities, roles=()):
    """RDAP entities (nested too) with any of ``roles``: [(entity, roles)]."""
    for e in entities or []:
        r = e.get("roles") or []
        if not roles or set(r) & set(roles):
            yield e, r
        yield from _walk(e.get("entities"), roles)


def _event(obj: dict, action: str) -> str | None:
    for ev in obj.get("events") or []:
        if ev.get("eventAction") == action:
            return ev.get("eventDate")
    return None


def _public(value) -> str | None:
    if not value or not isinstance(value, str) or _REDACTED_RE.search(value):
        return None
    return value.strip()


def parse_rdap_domain(data: dict) -> dict:
    f = {"name": (data.get("unicodeName") or data.get("ldhName") or "").lower(), "source": "RDAP"}
    for e, _ in _walk(data.get("entities"), ("registrar",)):
        vc = _vcard(e)
        f["registrar"] = vc.get("fn") or vc.get("org") or e.get("handle")
        for a, _ in _walk(e.get("entities"), ("abuse",)):
            f["abuse"] = _vcard(a).get("email")
        break
    for e, _ in _walk(data.get("entities"), ("registrant",)):
        vc = _vcard(e)
        f["org"] = _public(vc.get("org") or vc.get("fn"))
        f["country"] = _public(vc.get("country"))
        break
    f["created"] = _event(data, "registration")
    f["updated"] = _event(data, "last changed")
    f["expires"] = _event(data, "expiration")
    f["status"] = data.get("status") or []
    f["ns"] = [(n.get("ldhName") or "").lower().rstrip(".") for n in data.get("nameservers") or [] if n.get("ldhName")]
    sec = data.get("secureDNS") or {}
    if "delegationSigned" in sec:
        f["dnssec"] = "signed" if sec["delegationSigned"] else "unsigned"
    return f


def parse_whois_text(text: str) -> dict:
    """Fields of a port-43 answer (key: value lines; 'Domain Status' etc. may repeat)."""
    values: dict[str, list[str]] = {}
    for line in text.splitlines():
        if line.lstrip().startswith(("%", "#", ">>>")) or ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        if v:
            values.setdefault(k, []).append(v)
    f = {"source": "WHOIS"}
    for field, keys in _WHOIS_FIELDS.items():
        for k in keys:
            if k in values:
                f[field] = values[k] if field in ("status", "ns") else values[k][0]
                break
    if "status" in f:  # "clientTransferProhibited https://icann.org/epp#..." -> the code
        f["status"] = [s.split()[0] if s.split()[0][:1].islower() else s for s in f["status"]]
    if "ns" in f:  # .ru: "ns1.yandex.ru. 213.180.193.1, 2a02:6b8::1" -> the name
        f["ns"] = list(dict.fromkeys(n.split()[0].lower().rstrip(".") for n in f["ns"]))
    for k in ("org", "country"):
        f[k] = _public(f.get(k))
    return f


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d", "%d.%m.%Y", "%Y.%m.%d", "%d-%b-%Y", "%Y/%m/%d"):
        try:
            d = datetime.fromisoformat(v) if fmt is None else datetime.strptime(v.split()[0], fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _day(value: str | None) -> str | None:
    d = _date(value)
    return d.strftime("%Y-%m-%d") if d else value


def format_domain(name: str, f: dict) -> str:
    shown = iplookup._display_host(f.get("name") or name)
    lines = [f"📇 **{shown}** · {f['source']}"]
    if f.get("registrar"):
        lines.append(f"🏢 Registrar: {f['registrar']}")
    if f.get("org") or f.get("country"):
        who = ", ".join(x for x in (f.get("org"), f.get("country")) if x)
        lines.append(f"👤 Registrant: {who}")
    elif f["source"] == "RDAP" or "org" in f:
        lines.append("👤 Registrant: not public")

    if f.get("created"):
        lines.append(f"📅 Created: {_day(f['created'])}")
    if f.get("updated"):
        lines.append(f"✏️ Updated: {_day(f['updated'])}")
    exp = _date(f.get("expires"))
    if exp:
        # Calendar days: timedelta.days floors, so "3.5 days ago" would read "4 days ago"
        days = (exp.date() - datetime.now(timezone.utc).date()).days
        icon = "❌" if days < 0 else ("⚠️" if days < EXPIRY_WARN_DAYS else "⏳")
        when = f"expired {-days} days ago" if days < 0 else f"in {days} days"
        lines.append(f"{icon} Expires: {exp.strftime('%Y-%m-%d')} ({when})")
    elif f.get("expires"):
        lines.append(f"⏳ Expires: {f['expires']}")
    if f.get("free_date"):
        lines.append(f"🔓 Free after: {_day(f['free_date'])}")

    if f.get("status"):
        lines.append(f"🔖 Status: {', '.join(f['status'][:6])}")
    if f.get("ns"):
        lines.append(f"🧭 Name servers: {', '.join(f['ns'][:6])}")
    if f.get("dnssec"):
        lines.append(f"🔐 DNSSEC: {f['dnssec']}")
    if f.get("abuse"):
        lines.append(f"🚨 Registrar abuse: {f['abuse']}")
    lines += ["", f"💡 `/dns {shown}` — DNS records and mail checks"]
    return "\n".join(lines)


def lookup_domain(query_name: str) -> str:
    rules = _load_psl()
    name = registered_domain(query_name, rules)
    if name is None:
        return lookup_tld(query_name)
    tld = name.rsplit(".", 1)[-1]
    note = f"ℹ️ {iplookup._display_host(query_name)} is part of {iplookup._display_host(name)}\n" \
        if name != query_name else ""

    base = rdap_base_for_tld(tld)
    if base:
        data, status = _get_json(f"{base}domain/{urllib.parse.quote(name)}")
        if status == 404:
            return note + f"🆓 **{iplookup._display_host(name)}** is not registered (the registry has no record of it)."
        if data and data.get("objectClassName") == "domain":
            return note + format_domain(name, parse_rdap_domain(data))
        # RDAP down or odd: fall through to port 43, many registries run both

    server = whois_server_for_tld(tld)
    if not server:
        if base:
            return f"❌ The registry of .{tld} did not answer. Try again later."
        return f"❌ No RDAP or WHOIS server is known for .{iplookup._display_host(tld)}."
    text = whois43(server, name)
    if not text:
        return f"❌ {server} did not answer. Try again later."
    if _NOT_FOUND_RE.search(text) and not re.search(r"^\s*(domain name|domain):\s*\S", text, re.IGNORECASE | re.MULTILINE):
        return note + f"🆓 **{iplookup._display_host(name)}** is not registered ({server} has no record of it)."
    f = parse_whois_text(text)
    if len([k for k in f if f.get(k)]) <= 2:
        # Some registries (DENIC) answer only "Domain / Status" to anonymous queries
        f["status"] = f.get("status") or ["registered"]
    return note + format_domain(name, f)


def lookup_tld(tld: str) -> str:
    """'/whois ru' or '/whois co.uk': who runs the zone (IANA's root database)."""
    top = tld.rsplit(".", 1)[-1]
    text = whois43("whois.iana.org", top) or ""
    if not text or "domain:" not in text.lower():
        return f"❌ {iplookup._display_host(tld)} is a public suffix, not a registrable domain."
    vals = {}
    for line in text.splitlines():
        if ":" in line and not line.startswith("%"):
            k, v = line.split(":", 1)
            if v.strip():
                vals.setdefault(k.strip().lower(), v.strip())
    lines = [f"📇 **.{iplookup._display_host(top)}** · IANA root zone database"]
    if tld != top:
        lines.append(f"ℹ️ {iplookup._display_host(tld)} is a public suffix: names under it are registered, not it")
    if vals.get("organisation"):
        lines.append(f"🏢 Manager: {vals['organisation']}")
    for key, label in (("whois", "WHOIS server"), ("created", "Created"), ("changed", "Updated"), ("status", "Status")):
        if vals.get(key):
            lines.append(f"{label}: {vals[key]}")
    rdap = rdap_base_for_tld(top)
    lines.append(f"RDAP: {rdap}" if rdap else "RDAP: none (WHOIS only)")
    return "\n".join(lines)


def _abuse_and_holder(data: dict) -> tuple[str | None, str | None, str | None]:
    """Holder (the registrant; an admin contact only if there is none), its country, abuse e-mail."""
    holder = country = abuse = None
    for role in ("registrant", "administrative"):
        for e, _ in _walk(data.get("entities"), (role,)):
            vc = _vcard(e)
            holder, country = vc.get("org") or vc.get("fn"), vc.get("country")
            break
        if holder:
            break
    for e, _ in _walk(data.get("entities"), ("abuse",)):
        abuse = _vcard(e).get("email")
        if abuse:
            break
    return holder, country, abuse


def _country(code_or_name: str | None) -> str | None:
    """'GB' stays (gets a flag); 'UNITED KINGDOM' from an address label -> 'United Kingdom'."""
    if not code_or_name:
        return None
    if len(code_or_name) == 2:
        return iplookup._country_line(code_or_name)
    return f"🌍 Country: {code_or_name.title()}"


def _rir(base: str) -> str:
    for host, rir in (("ripe", "RIPE NCC"), ("arin", "ARIN"), ("apnic", "APNIC"),
                      ("lacnic", "LACNIC"), ("afrinic", "AFRINIC")):
        if host in base:
            return rir
    return urllib.parse.urlsplit(base).hostname or base


def lookup_ip(net_or_ip) -> str:
    net = ipaddress.ip_network(net_or_ip) if not isinstance(net_or_ip, (ipaddress.IPv4Network, ipaddress.IPv6Network)) else net_or_ip
    base = rdap_base_for_ip(net)
    if not base:
        return f"❌ No registry is known for {net_or_ip}."
    q = net_or_ip.compressed if hasattr(net_or_ip, "compressed") else str(net_or_ip)
    data, status = _get_json(f"{base}ip/{q}")
    if not data:
        return f"❌ {_rir(base)} did not answer (HTTP {status or '-'}). Try again later."
    holder, country, abuse = _abuse_and_holder(data)
    cidrs = [f"{c.get('v4prefix') or c.get('v6prefix')}/{c.get('length')}" for c in data.get("cidr0_cidrs") or []]
    block = ", ".join(cidrs) or f"{data.get('startAddress')} - {data.get('endAddress')}"
    lines = [f"📇 **{q}** · {_rir(base)} RDAP",
             f"🧱 Block: {block}" + (f" ({data['name']})" if data.get("name") else "")]
    if data.get("type"):
        lines.append(f"🔖 Type: {data['type']}")
    if holder:
        lines.append(f"🏢 Holder: {holder}")
    c = _country(data.get("country") or country)
    if c:
        lines.append(c)
    if _event(data, "registration"):
        lines.append(f"📅 Registered: {_day(_event(data, 'registration'))}")
    if _event(data, "last changed"):
        lines.append(f"✏️ Updated: {_day(_event(data, 'last changed'))}")
    lines.append(f"🚨 Abuse: {abuse}" if abuse else "🚨 Abuse: no contact listed")
    lines += ["", f"💡 `/ip {q}` — location, network, open ports"]
    return "\n".join(lines)


def lookup_asn(asn: int) -> str:
    base = rdap_base_for_asn(asn)
    if not base:
        return f"❌ AS{asn} is not allocated to any registry."
    data, status = _get_json(f"{base}autnum/{asn}")
    if status == 404:
        return f"❌ AS{asn} is not registered."
    if not data:
        return f"❌ {_rir(base)} did not answer (HTTP {status or '-'}). Try again later."
    holder, country, abuse = _abuse_and_holder(data)
    name = data.get("name") if (data.get("name") or "").lower() not in ("", "unspecified") else None
    lines = [f"📇 **AS{asn}**" + (f" — {name}" if name else "") + f" · {_rir(base)} RDAP"]
    if holder:
        lines.append(f"🏢 Holder: {holder}")
    c = _country(data.get("country") or country)
    if c:
        lines.append(c)
    if _event(data, "registration"):
        lines.append(f"📅 Registered: {_day(_event(data, 'registration'))}")
    if _event(data, "last changed"):
        lines.append(f"✏️ Updated: {_day(_event(data, 'last changed'))}")
    lines.append(f"🚨 Abuse: {abuse}" if abuse else "🚨 Abuse: no contact listed")
    lines += ["", f"💡 `/ip AS{asn}` — announced prefixes"]
    return "\n".join(lines)


def lookup(kind: str, value) -> str:
    if kind == "asn":
        return lookup_asn(value)
    if kind in ("ip", "net"):
        return lookup_ip(value)
    return lookup_domain(value)


# ── Command, queue and cache ──

def find_target(text: str, whole: bool):
    """('ip' | 'net' | 'asn' | 'host', value): an address or AS first, then a domain."""
    target = iplookup.find_target(text, bare_asn=whole)
    if target:
        return target
    if whole:
        word = text.split()[0] if text.split() else ""
        host = iplookup.parse_host(word) or _tld_word(word)
        return ("host", host) if host else None
    return iplookup.find_host_target(text)


def _tld_word(word: str) -> str | None:
    """A bare TLD ('ru', '.рф') - parse_host wants at least two labels."""
    w = word.strip().strip(".").lower()
    try:
        w = w.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return None
    return w if re.fullmatch(r"[a-z]{2,63}|xn--[a-z0-9-]{1,59}", w) else None


def _cache_get(key):
    with state._whois_lock:
        hit = state._whois_cache.get(key)
        if hit and time.time() - hit[0] < config.WHOIS_CACHE_SECONDS:
            return hit[1]
    return None


def _cache_put(key, text):
    with state._whois_lock:
        state._whois_cache[key] = (time.time(), text)
        if len(state._whois_cache) > 300:
            oldest = min(state._whois_cache, key=lambda k: state._whois_cache[k][0])
            state._whois_cache.pop(oldest, None)


def _worker(bot, accid, chat_id, msg_id, kind, value, key):
    try:
        text = _cache_get(key)
        if text is None:
            text = lookup(kind, value)
            if not text.startswith("❌"):
                _cache_put(key, text)
        dc_helpers._react(bot, accid, msg_id, "❌" if text.startswith("❌") else "☑️")
        dc_helpers._send(bot, accid, chat_id, text, reply_to_id=msg_id)
    except Exception as e:
        config.logger.error(f"Error in /whois worker: {e}", exc_info=True)
        dc_helpers._react(bot, accid, msg_id, "❌")
        dc_helpers._send(bot, accid, chat_id, f"❌ WHOIS lookup failed: {e}", reply_to_id=msg_id)
    finally:
        with state._whois_lock:
            state._whois_pending -= 1


_pool = ThreadPoolExecutor(max_workers=config.WHOIS_WORKERS, thread_name_prefix="whois")


@config.dc_cli.on(events.NewMessage(command="/whois"))
def whois_command(bot, accid, event):
    msg = event.msg
    payload = (event.payload or "").strip()
    if payload:
        target = find_target(payload, whole=True)
        if not target:
            dc_helpers._send(bot, accid, msg.chat_id,
                             f"❌ `{payload[:60]}` is not a domain, IP address, network or AS number "
                             f"(private addresses have no public registration).\n\n{USAGE}", reply_to_id=msg.id)
            return
    else:
        quoted = iplookup._quoted_text(bot, accid, msg)
        if not quoted:
            dc_helpers._send(bot, accid, msg.chat_id, USAGE, reply_to_id=msg.id)
            return
        target = find_target(quoted, whole=False)
        if not target:
            dc_helpers._send(bot, accid, msg.chat_id,
                             "❌ No public IP address, network, AS number or link found in that message.",
                             reply_to_id=msg.id)
            return

    kind, value = target
    key = f"{kind}:{value}"
    cached = _cache_get(key)
    if cached is not None:
        dc_helpers._send(bot, accid, msg.chat_id, cached, reply_to_id=msg.id)
        return
    with state._whois_lock:
        full = state._whois_pending >= config.WHOIS_MAX_QUEUE
        if not full:
            state._whois_pending += 1
    if full:
        dc_helpers._send(bot, accid, msg.chat_id, "⏳ The WHOIS queue is full, please try again in a minute.",
                         reply_to_id=msg.id)
        return
    dc_helpers._react(bot, accid, msg.id, "⏳")
    _pool.submit(_worker, bot, accid, msg.chat_id, msg.id, kind, value, key)
