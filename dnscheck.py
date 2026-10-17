"""DNS lookups: the /dns command.

Syntax follows dig - ``/dns <name|IP> [TYPE] [@server|@auth]`` in any order:

- ``/dns chatmail.uk`` - a full report: addresses with reverse DNS, zone (NS,
  DNSSEC, CAA) and, for a mail domain, MX / SPF / DKIM / DMARC / MTA-STS /
  TLS-RPT / SRV checks. A domain laid out like a chatmail relay (MX = the
  domain itself, DKIM selector "opendkim") also gets the list of records
  ``cmdeploy dns`` expects, split into required and recommended.
- ``/dns chatmail.uk MX`` - one record type, raw, with TTL (like dig).
- ``/dns 213.165.89.146`` - reverse lookup (PTR), like ``dig -x``.
- ``@1.1.1.1`` / ``@dns.google`` asks that resolver; ``@auth`` asks the zone's
  authoritative name server directly, bypassing caches (as cmdeploy does).

``/host <name|IP> [@server|@auth]`` is the short form, like the ``host`` tool:
A, AAAA and MX of a name (following a CNAME), or PTR of an IP address.

Besides DNS, the report makes one HTTPS request: the MTA-STS policy at
https://mta-sts.<domain>/.well-known/mta-sts.txt - only to a public address,
without redirects, at most 64 KB.

Reports run on a small pool (DNS_WORKERS at a time); the rest wait in the
queue. Results are cached for DNS_CACHE_SECONDS.
"""
import base64
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import dns.exception
import dns.rdatatype
import dns.resolver
import dns.reversename
from deltachat2 import events

import config
import dc_helpers
import state

SUPPORTED_TYPES = ("A", "AAAA", "CNAME", "MX", "NS", "TXT", "SOA", "CAA", "SRV", "PTR",
                   "DS", "DNSKEY", "TLSA", "HTTPS", "SVCB")
# Selectors tried for DKIM: there is no way to list them, so the common ones
DKIM_SELECTORS = ("opendkim", "default", "dkim", "mail", "selector1", "selector2",
                  "google", "k1", "s1", "s2")
SRV_SERVICES = (("_submission._tcp", 587), ("_submissions._tcp", 465),
                ("_imap._tcp", 143), ("_imaps._tcp", 993))
MTA_STS_MAX_BYTES = 64 * 1024

USAGE = (
    "Usage (like dig; `/dig` works too):\n"
    "• `/dns <domain>` — Full report: addresses, zone, mail (MX, SPF, DKIM, DMARC, MTA-STS); "
    "chatmail relays are checked against what cmdeploy expects\n"
    "• `/dns <name> <TYPE>` — One record type, e.g. `/dns chatmail.uk MX`\n"
    "• `/dns <IP>` — Reverse DNS (PTR)\n"
    "• Add `@1.1.1.1` to ask a specific resolver, or `@auth` to ask the domain's "
    "authoritative name server (bypasses caches, e.g. right after a change)\n"
    f"Types: {', '.join(SUPPORTED_TYPES)}\n"
    "Just addresses and mail servers? `/host <name>`"
)

HOST_USAGE = (
    "Usage (like the `host` tool):\n"
    "• `/host <name>` — Addresses (A, AAAA) and mail servers (MX), e.g. `/host chatmail.uk`\n"
    "• `/host <IP>` — Reverse DNS (PTR)\n"
    "• Add `@1.1.1.1` to ask a specific resolver, or `@auth` to ask the domain's "
    "authoritative name server\n"
    "For other record types or a full mail check use `/dns`."
)
HOST_TYPES = ("A", "AAAA", "MX")
# host(1) prints these for a failed lookup, e.g. "Host x not found: 3(NXDOMAIN)"
_HOST_RCODES = {"nxdomain": "3(NXDOMAIN)", "servfail": "2(SERVFAIL)"}

# DNS names here may contain "_" (_dmarc, _mta-sts._tcp ...), unlike host names
_NAME_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?\.)+(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$")


class Res:
    """One DNS answer: status 'ok' | 'nodata' | 'nxdomain' | 'servfail' | 'timeout' | 'error'."""

    def __init__(self, status, records=None, ttl=None, chain=None, error=None, rdata=None):
        self.status = status
        self.records = records or []  # text form, TXT strings joined
        self.rdata = rdata or []      # dnspython rdata objects
        self.ttl = ttl
        self.chain = chain or []      # [(name, cname target)] followed to get here
        self.error = error

    @property
    def ok(self):
        return self.status == "ok"

    def problem(self) -> str:
        return {"nodata": "no records", "nxdomain": "name does not exist (NXDOMAIN)",
                "servfail": "server failure (SERVFAIL)", "timeout": "timed out"}.get(
            self.status, f"error: {self.error}")


def _rdata_text(rd) -> str:
    if rd.rdtype == dns.rdatatype.TXT:
        # A TXT record is a list of strings of up to 255 bytes; DKIM keys are split
        return b"".join(rd.strings).decode("utf-8", errors="replace")
    return rd.to_text()


def make_resolver(nameservers: list[str] | None = None) -> dns.resolver.Resolver:
    """Resolver for ``nameservers``; by default the public ones in DNS_RESOLVERS.

    Not the system resolver: on de2 that is systemd-resolved, which answers DS
    queries with nothing (so every zone looked unsigned) - and public resolvers
    show what the rest of the internet sees.
    """
    nameservers = nameservers or config.DNS_RESOLVERS
    r = dns.resolver.Resolver(configure=not nameservers)  # system config only if none set
    r.timeout = 3
    r.lifetime = 6
    r.cache = None
    if nameservers:
        r.nameservers = nameservers
    return r


def default_label() -> str:
    return f"resolver {', '.join(config.DNS_RESOLVERS)}" if config.DNS_RESOLVERS else "bot's resolver"


def query(res: dns.resolver.Resolver, name: str, rtype: str) -> Res:
    try:
        ans = res.resolve(name, rtype, raise_on_no_answer=False, search=False)
    except dns.resolver.NXDOMAIN:
        return Res("nxdomain")
    except dns.resolver.NoNameservers as e:
        return Res("servfail", error=str(e))
    except dns.exception.Timeout:
        return Res("timeout")
    except dns.exception.DNSException as e:
        return Res("error", error=str(e) or type(e).__name__)
    chain = []
    if rtype != "CNAME":
        for rrset in ans.response.answer:
            if rrset.rdtype == dns.rdatatype.CNAME:
                chain.append((rrset.name.to_text(), rrset[0].target.to_text()))
    rrset = ans.rrset
    if rrset is None:
        return Res("nodata", chain=chain)
    rdata = list(rrset)
    return Res("ok", [_rdata_text(r) for r in rdata], rrset.ttl, chain, rdata=rdata)


def _norm_name(text: str) -> str | None:
    t = text.strip().rstrip(".").lower()
    try:
        t = t.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return None
    return t if _NAME_RE.match(t) else None


def _public_ip(text: str):
    try:
        ip = ipaddress.ip_address(text.strip("[]"))
    except ValueError:
        return None
    return ip if ip.is_global else None


def parse_args(payload: str):
    """-> (name, rtype | None, server | None) or an error string.

    ``name`` is a normalized DNS name, or an ipaddress object for a PTR lookup.
    ``server`` is "auth" or the text after "@".
    """
    name = rtype = server = None
    for tok in payload.split():
        if tok.startswith("@"):
            if server is not None:
                return "Only one @server, please."
            server = tok[1:].lower()
            if not server:
                return "`@` needs a resolver address, e.g. `@1.1.1.1`, or `@auth`."
        elif tok.upper() in SUPPORTED_TYPES and "." not in tok:
            if rtype is not None:
                return "Only one record type, please."
            rtype = tok.upper()
        elif tok.upper() == "ANY":
            return "`ANY` is not supported (most servers no longer answer it); ask for one type."
        elif name is None:
            try:
                name = ipaddress.ip_address(tok.strip("[]"))
            except ValueError:
                name = _norm_name(tok)
                if name is None:
                    return f"`{tok[:60]}` is not a domain name, IP address or record type."
        else:
            return f"Unexpected `{tok[:60]}`."
    if name is None:
        return "Which name? E.g. `/dns chatmail.uk`."
    if not isinstance(name, str):
        if rtype not in (None, "PTR"):
            return "For an IP address only the reverse lookup (PTR) works."
        rtype = "PTR"
    return name, rtype, server


def _resolve_server(server: str | None, name: str, sysres) -> tuple[dns.resolver.Resolver, str] | str:
    """Resolver for ``@server`` and a label saying which one answered, or an error string."""
    if not server:
        return sysres, default_label()
    if server == "auth":
        try:
            zone = dns.resolver.zone_for_name(name, resolver=sysres).to_text().rstrip(".")
        except dns.exception.DNSException:
            return f"Could not find the zone of {name}."
        ns = query(sysres, zone, "NS")
        if not ns.ok:
            return f"Could not get the name servers of zone {zone}: {ns.problem()}."
        for ns_name in sorted(ns.records):
            ips = [r for t in ("A", "AAAA") for r in query(sysres, ns_name, t).records]
            if ips:
                return make_resolver(ips), f"authoritative {ns_name.rstrip('.')} (zone {zone})"
        return f"Name servers of zone {zone} have no addresses."
    ip = _public_ip(server)
    if ip is None:
        host = _norm_name(server)
        if host:
            addrs = query(sysres, host, "A").records or query(sysres, host, "AAAA").records
            ip = next((_public_ip(a) for a in addrs if _public_ip(a)), None)
        if ip is None:
            # Private resolvers are refused: /dns must not reach the bot's own network
            return f"`@{server[:60]}` is not a public resolver address."
    return make_resolver([ip.compressed]), f"resolver {server}"


# ── Single record type ──

def format_records(name, rtype: str, res: dns.resolver.Resolver, via: str) -> str:
    qname = dns.reversename.from_address(name.compressed).to_text() if not isinstance(name, str) else name
    shown = name.compressed if not isinstance(name, str) else _display(name)
    r = query(res, qname, rtype)
    lines = [f"🔎 **{shown} {rtype}** · {via}"]
    for src, target in r.chain:
        lines.append(f"↪ {src} CNAME {target}")
    if r.ok:
        lines += [f"  {rec}" for rec in r.records]
        lines.append(f"TTL {r.ttl}")
    else:
        lines.append(f"❌ {r.problem()}")
    return "\n".join(lines)


def _display(name: str) -> str:
    try:
        return name.encode("ascii").decode("idna") if "xn--" in name else name
    except UnicodeError:
        return name


# ── /host: the short form ──

def _host_problem(name: str, r: Res) -> str:
    if r.status in _HOST_RCODES:
        return f"Host {name} not found: {_HOST_RCODES[r.status]}"
    return f"{name}: {r.problem()}"


def host_report(name, res: dns.resolver.Resolver, via: str) -> str:
    """Lines as host(1) prints them: aliases, addresses and mail servers, or the PTR of an IP."""
    head = f"🔎 **host {name.compressed if not isinstance(name, str) else _display(name)}** · {via}"
    if not isinstance(name, str):
        qname = dns.reversename.from_address(name.compressed).to_text().rstrip(".")
        r = query(res, qname, "PTR")
        if r.ok:
            lines = [f"{qname} domain name pointer {p}" for p in r.records]
        elif r.status == "nodata":
            lines = [f"{qname} has no PTR record"]
        else:
            lines = [_host_problem(qname, r)]
        return "\n".join([head] + lines)

    with ThreadPoolExecutor(max_workers=len(HOST_TYPES)) as pool:
        futs = {t: pool.submit(query, res, name, t) for t in HOST_TYPES}
        results = {t: f.result() for t, f in futs.items()}
    if any(r.status == "nxdomain" for r in results.values()):
        return "\n".join([head, _host_problem(_display(name), results["A"])])

    # A CNAME applies to every type, so any answer shows the chain; host prints it once
    chain = next((r.chain for r in results.values() if r.chain), [])
    lines = [f"{_display(src.rstrip('.'))} is an alias for {_display(dst)}" for src, dst in chain]
    target = _display(chain[-1][1].rstrip(".")) if chain else _display(name)
    a, aaaa, mx = results["A"], results["AAAA"], results["MX"]
    lines += [f"{target} has address {x}" for x in a.records]
    lines += [f"{target} has IPv6 address {x}" for x in aaaa.records]
    # host sorts mail servers by preference; a null MX ("0 .") means the domain takes no mail
    for rd in sorted(mx.rdata, key=lambda r: (r.preference, r.exchange.to_text())):
        exchange = rd.exchange.to_text()
        if exchange == ".":
            lines.append(f"{target} accepts no mail (null MX)")
        else:
            lines.append(f"{target} mail is handled by {rd.preference} {_display(exchange)}")
    # Errors other than "no records" are worth showing per type (e.g. AAAA timed out)
    lines += [f"{target} {t}: {r.problem()}" for t, r in results.items() if r.status not in ("ok", "nodata")]
    if not any(r.ok for r in results.values()) and len(lines) == len(chain):
        lines.append(f"{target} has no A, AAAA or MX records")
    return "\n".join([head] + lines)


# ── MTA-STS policy over HTTPS ──

class _PinnedHTTPS(http.client.HTTPSConnection):
    """HTTPS to an address checked in advance, with SNI and certificate for the host name -
    so a DNS answer cannot change between the check and the connection."""

    def __init__(self, host, ip, **kw):
        super().__init__(host, 443, context=ssl.create_default_context(), **kw)
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def fetch_mta_sts_policy(domain: str, sysres) -> tuple[dict | None, str | None]:
    """-> (policy {mode, mx: [...], max_age}, error)."""
    host = f"mta-sts.{domain}"
    addrs = query(sysres, host, "A").records + query(sysres, host, "AAAA").records
    ips = [ipaddress.ip_address(a) for a in addrs]
    if not ips:
        return None, f"{host} does not resolve"
    if not all(ip.is_global for ip in ips):
        return None, f"{host} resolves to a private address"
    try:
        conn = _PinnedHTTPS(host, ips[0].compressed, timeout=10)
        conn.request("GET", "/.well-known/mta-sts.txt", headers={"User-Agent": f"BouncerBot/{config.VERSION}"})
        resp = conn.getresponse()
        body = resp.read(MTA_STS_MAX_BYTES + 1)
        conn.close()
    except ssl.SSLCertVerificationError as e:
        return None, f"TLS certificate of {host} is not valid: {e.verify_message}"
    except Exception as e:
        return None, f"could not fetch the policy: {e}"
    if resp.status != 200:  # redirects are not allowed for MTA-STS (RFC 8461)
        return None, f"policy URL answered HTTP {resp.status}"
    if len(body) > MTA_STS_MAX_BYTES:
        return None, "policy is larger than 64 KB"
    policy = {"mx": []}
    for line in body.decode("utf-8", errors="replace").splitlines():
        if ":" in line:
            k, v = (x.strip() for x in line.split(":", 1))
            if k == "mx":
                policy["mx"].append(v.lower())
            else:
                policy[k] = v
    if policy.get("version") != "STSv1":
        return None, "policy has no `version: STSv1`"
    return policy, None


def _sts_mx_match(pattern: str, host: str) -> bool:
    if pattern.startswith("*."):  # a wildcard matches exactly one label
        return host.count(".") > 0 and host.split(".", 1)[1] == pattern[2:]
    return host == pattern


# ── Checks ──

def _tags(record: str) -> dict:
    """'v=DMARC1; p=reject; adkim=s' -> {'v': 'DMARC1', 'p': 'reject', 'adkim': 's'}."""
    out = {}
    for part in record.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


def _dkim_key_info(record: str) -> tuple[str, str]:
    """-> (icon, 'RSA 2048 bit' ...) for a DKIM TXT record."""
    tags = _tags(record)
    k = tags.get("k", "rsa").lower()
    p = re.sub(r"\s", "", tags.get("p", ""))
    if not p:
        return "⚠️", "key revoked (empty p=)"
    try:
        der = base64.b64decode(p)
    except Exception:
        return "❌", "key is not valid base64"
    if k == "ed25519":
        return "✅", "Ed25519"
    try:
        from cryptography.hazmat.primitives.serialization import load_der_public_key
        bits = load_der_public_key(der).key_size
    except Exception:
        return "❌", "RSA key could not be parsed"
    icon = "✅" if bits >= 2048 else ("⚠️" if bits >= 1024 else "❌")
    return icon, f"RSA {bits} bit"


def _spf_networks(spf: str, domain: str, results: dict, sysres) -> tuple[list, list[str]]:
    """Networks an SPF record allows directly, and the mechanisms not expanded (include, exists ...)."""
    nets, other = [], []
    for term in spf.split()[1:]:
        q = term.lstrip("+")  # only pass terms authorize; -ip4: etc. do not
        if q[:1] in "-~?":
            continue
        mech, _, arg = q.partition(":")
        mech = mech.lower().split("/")[0]  # "a/24": the prefix length widens the match
        if mech in ("a", "mx"):
            arg = arg.split("/")[0]
        try:
            if mech in ("ip4", "ip6"):
                nets.append(ipaddress.ip_network(arg, strict=False))
            elif mech == "a":
                host = (arg or domain).rstrip(".")
                for t in ("A", "AAAA"):
                    nets += [ipaddress.ip_network(a) for a in query(sysres, host, t).records]
            elif mech == "mx" and not arg:
                for ip in results["mx_ips"]:
                    nets.append(ipaddress.ip_network(ip))
            elif mech in ("include", "exists", "ptr", "mx") or q.startswith("redirect="):
                other.append(q)
        except ValueError:
            other.append(q)
    return nets, other


def build_report(domain: str, res: dns.resolver.Resolver, via: str, sysres) -> str:
    jobs = {
        "A": (domain, "A"), "AAAA": (domain, "AAAA"), "MX": (domain, "MX"), "TXT": (domain, "TXT"),
        "CAA": (domain, "CAA"), "DMARC": (f"_dmarc.{domain}", "TXT"),
        "STS": (f"_mta-sts.{domain}", "TXT"), "STS_CNAME": (f"mta-sts.{domain}", "CNAME"),
        "WWW": (f"www.{domain}", "CNAME"), "TLSRPT": (f"_smtp._tls.{domain}", "TXT"),
        "ADSP": (f"_adsp._domainkey.{domain}", "TXT"),
    }
    for svc, _ in SRV_SERVICES:
        jobs[svc] = (f"{svc}.{domain}", "SRV")
    for sel in DKIM_SELECTORS:
        jobs[f"DKIM:{sel}"] = (f"{sel}._domainkey.{domain}", "TXT")

    with ThreadPoolExecutor(max_workers=12, thread_name_prefix="dns-q") as pool:
        futs = {k: pool.submit(query, res, n, t) for k, (n, t) in jobs.items()}
        zone_fut = pool.submit(_zone_info, domain, sysres)
        r = {k: f.result() for k, f in futs.items()}
        zone, zone_ns, signed = zone_fut.result()

    if r["A"].status == "nxdomain":
        return f"🌐 **{_display(domain)}** · {via}\n❌ Name does not exist (NXDOMAIN)."
    if all(r[k].status == "nodata" and not r[k].chain for k in ("A", "AAAA", "MX", "TXT")):
        # Cloudflare and other signed zones answer "no records" instead of NXDOMAIN
        return (f"🌐 **{_display(domain)}** · {via}\n"
                "❌ No A, AAAA, MX or TXT records - the name does not exist or is unused.")

    lines = [f"🌐 DNS report: **{_display(domain)}** · {via}", ""]

    # Addresses
    lines.append("📍 **Addresses**")
    for src, target in r["A"].chain or r["AAAA"].chain:
        lines.append(f"↪ {src.rstrip('.')} CNAME {target}")
    addrs = r["A"].records + r["AAAA"].records
    ptrs = _reverse_many(addrs, sysres)
    for t in ("A", "AAAA"):
        if r[t].ok:
            for a in r[t].records:
                lines.append(f"{t} {a}" + (f" → PTR {', '.join(map(_display, ptrs[a]))}" if ptrs.get(a) else " (no PTR)"))
        elif r[t].status in ("nodata", "nxdomain"):
            lines.append(f"ℹ️ {t}: none")
        else:
            lines.append(f"❌ {t}: {r[t].problem()}")

    # Zone
    lines += ["", "🗂 **Zone**"]
    if zone:
        lines.append(f"NS ({zone}): {', '.join(n.rstrip('.') for n in zone_ns) or '?'}")
    lines.append("✅ DNSSEC: signed (DS in parent zone)" if signed else "ℹ️ DNSSEC: not signed")
    lines.append(_caa_line(r["CAA"]))

    mail, chatmail = _mail_section(domain, r, sysres)
    lines += mail
    lines += chatmail

    lines += ["", f"💡 `/dns {_display(domain)} <TYPE>` for raw records, `@auth` to bypass caches"]
    lines.append(f"💡 `/whois {_display(domain)}` — registrar, expiry date")
    if addrs:
        lines.append(f"💡 `/ip {addrs[0]}` — who hosts it, open ports")
    return "\n".join(lines)


def _caa_line(caa: Res) -> str:
    """'CAA issue: letsencrypt.org, pki.goog · issuewild: …' - issuers grouped by tag."""
    if not caa.ok:
        return "ℹ️ CAA: none (any certificate authority may issue)"
    groups: dict[str, list[str]] = {}
    for rd in caa.rdata:
        tag = rd.tag.decode(errors="replace")
        value = rd.value.decode(errors="replace").split(";")[0].strip() or ";"  # ";" = nobody
        if value not in groups.setdefault(tag, []):
            groups[tag].append(value)
    return "CAA " + " · ".join(f"{t}: {', '.join(v)}" for t, v in groups.items())


def _zone_info(domain: str, sysres) -> tuple[str | None, list[str], bool]:
    """Zone apex, its NS names and whether it is DNSSEC-signed (has a DS in the parent)."""
    try:
        zone = dns.resolver.zone_for_name(domain, resolver=sysres).to_text().rstrip(".")
    except dns.exception.DNSException:
        return None, [], False
    ns = query(sysres, zone, "NS").records
    return zone, sorted(ns), query(sysres, zone, "DS").ok


def _reverse_many(addrs: list[str], sysres) -> dict[str, list[str]]:
    out = {}
    for a in addrs:
        try:
            rev = dns.reversename.from_address(a).to_text()
        except Exception:
            continue
        out[a] = [p.rstrip(".") for p in query(sysres, rev, "PTR").records]
    return out


def _mail_section(domain: str, r: dict, sysres) -> tuple[list[str], list[str]]:
    lines = ["", "✉️ **Mail**"]
    mx_hosts = []
    if r["MX"].ok:
        for rd in sorted(r["MX"].rdata, key=lambda x: x.preference):
            mx_hosts.append((rd.preference, rd.exchange.to_text().rstrip(".").lower()))
    if not mx_hosts or mx_hosts == [(0, "")]:  # "0 ." is a null MX (RFC 7505)
        null = bool(mx_hosts)
        lines.append("ℹ️ Null MX: the domain accepts no mail" if null else
                     f"ℹ️ No MX records{'' if r['MX'].status in ('nodata', 'nxdomain') else ': ' + r['MX'].problem()}"
                     " — the domain does not receive mail")
        return lines, []

    # MX hosts, their addresses and reverse DNS (forward-confirmed)
    mx_ips = []
    if len(mx_hosts) > 5:
        lines.append(f"ℹ️ {len(mx_hosts)} MX hosts, checking the first 5")
    for pref, host in mx_hosts[:5]:
        ips = [a for t in ("A", "AAAA") for a in query(sysres, host, t).records]
        mx_ips += ips
        lines.append(f"{'✅' if ips else '❌'} MX {pref} {_display(host)}" + (f" → {', '.join(ips)}" if ips else " — no address"))
        for ip in ips:
            lines.append("   " + _fcrdns_line(ip, host, sysres))
    r["mx_ips"] = mx_ips

    # SPF
    spfs = [t for t in r["TXT"].records if t.lower().startswith("v=spf1")]
    if not spfs:
        lines.append("❌ SPF: none")
    elif len(spfs) > 1:
        lines.append(f"❌ SPF: {len(spfs)} records (only one is allowed - receivers treat it as an error)")
    else:
        spf = spfs[0]
        all_m = next((t for t in spf.split() if t.lower().lstrip("+-~?") == "all"), None)
        icon, note = ("✅", "") if all_m in ("-all", "~all") else \
            ("❌", " — `+all` lets anyone send as this domain") if all_m in ("all", "+all") else \
            ("⚠️", " — `?all` is neutral") if all_m == "?all" else \
            ("ℹ️", "") if "redirect=" in spf else ("⚠️", " — no `all` at the end")
        lines.append(f"{icon} SPF: `{spf}`{note}")
        nets, other = _spf_networks(spf, domain, r, sysres)
        missing = [ip for ip in mx_ips if not any(ipaddress.ip_address(ip) in n for n in nets
                                                  if n.version == ipaddress.ip_address(ip).version)]
        if mx_ips and not missing:
            lines.append("   ✅ covers the MX addresses")
        elif missing:
            how = f"may be covered by {', '.join(other)}" if other else "fine only if mail is sent from elsewhere"
            lines.append(f"   {'ℹ️' if other else '⚠️'} does not directly cover {', '.join(missing)} ({how})")

    # DKIM
    dkim = []
    for sel in DKIM_SELECTORS:
        rec = next((t for t in r[f"DKIM:{sel}"].records if "p=" in t), None)
        if rec:
            icon, info = _dkim_key_info(rec)
            dkim.append(sel)
            lines.append(f"{icon} DKIM `{sel}`: {info}")
    if not dkim:
        lines.append(f"⚠️ DKIM: no key at the common selectors ({', '.join(DKIM_SELECTORS[:4])}…); "
                     "the real selector is in the DKIM-Signature header (s=) of a sent mail")

    # DMARC
    dmarcs = [t for t in r["DMARC"].records if t.lower().startswith("v=dmarc1")]
    if not dmarcs:
        lines.append("⚠️ DMARC: none")
    else:
        tags = _tags(dmarcs[0])
        p = tags.get("p", "?").lower()
        icon = "✅" if p in ("reject", "quarantine") else "⚠️"
        align = f", adkim={tags.get('adkim', 'r')}, aspf={tags.get('aspf', 'r')}"
        lines.append(f"{icon} DMARC: p={p}{align}" + (" — monitoring only" if p == "none" else ""))

    # MTA-STS
    sts = [t for t in r["STS"].records if t.lower().startswith("v=stsv1")]
    policy = None
    if not sts:
        lines.append("ℹ️ MTA-STS: none (optional; senders may then deliver without TLS)")
    else:
        sts_id = _tags(sts[0]).get("id", "?")
        policy, err = fetch_mta_sts_policy(domain, sysres)
        if err:
            lines.append(f"❌ MTA-STS id={sts_id}: {err}")
        else:
            mode = policy.get("mode", "?")
            unmatched = [h for _, h in mx_hosts if not any(_sts_mx_match(p, h) for p in policy["mx"])]
            icon = "❌" if unmatched or mode not in ("enforce", "testing") else ("✅" if mode == "enforce" else "⚠️")
            lines.append(f"{icon} MTA-STS id={sts_id}: mode {mode}, mx {', '.join(policy['mx']) or '-'}")
            if unmatched:
                lines.append(f"   ❌ policy does not list MX {', '.join(unmatched)} - senders will refuse to deliver")

    tlsrpt = [t for t in r["TLSRPT"].records if t.lower().startswith("v=tlsrptv1")]
    lines.append(f"✅ TLS-RPT: `{tlsrpt[0]}`" if tlsrpt else "ℹ️ TLS-RPT: none (optional)")

    srv = []
    for svc, _ in SRV_SERVICES:
        for rd in r[svc].rdata:
            label = svc.split(".")[0].lstrip("_")
            target = rd.target.to_text().rstrip(".")
            # target "." = the service is deliberately not offered (RFC 2782)
            srv.append(f"{label} {rd.port} → {target}" if target else f"{label}: not offered")
    lines.append(f"SRV: {'; '.join(srv)}" if srv else "ℹ️ SRV (client autoconfig): none")

    chatmail = []
    if "opendkim" in dkim and any(h == domain for _, h in mx_hosts):
        chatmail = _chatmail_section(domain, r, mx_hosts, policy)
    return lines, chatmail


def _fcrdns_line(ip: str, mx_host: str, sysres) -> str:
    """Reverse DNS of a mail server: big providers reject mail from addresses
    without a PTR whose name resolves back to the same address."""
    ptrs = _reverse_many([ip], sysres).get(ip, [])
    if not ptrs:
        return f"❌ {ip}: no PTR (reverse DNS) - many receivers reject mail from it"
    for name in ptrs:
        back = [a for t in ("A", "AAAA") for a in query(sysres, name, t).records]
        if ip in back:
            same = "matches the MX name" if name.lower() == mx_host else "differs from the MX name, which is fine"
            return f"✅ PTR {ip} → {_display(name)} ({same}; resolves back)"
    return f"⚠️ PTR {ip} → {', '.join(map(_display, ptrs))}, but that name does not resolve back to {ip}"


def _chatmail_section(domain: str, r: dict, mx_hosts, policy) -> list[str]:
    """What `cmdeploy dns` expects (cmdeploy/dns.py: required and recommended entries)."""
    d = domain + "."

    def check(ok: bool, label: str) -> tuple[bool, str]:
        return ok, f"{'✅' if ok else '❌'} {label}"

    dmarc = _tags(next((t for t in r["DMARC"].records if t.lower().startswith("v=dmarc1")), ""))
    srv_ok = all(any(rd.port == port and rd.target.to_text().lower() == d for rd in r[svc].rdata)
                 for svc, port in SRV_SERVICES)
    required = [
        check(bool(r["A"].records or r["AAAA"].records), "A / AAAA"),
        check(any(h == domain for _, h in mx_hosts), f"MX → {domain}"),
        check(any(t.lower().startswith("v=stsv1") for t in r["STS"].records), "_mta-sts TXT"),
        check([x.lower() for x in r["STS_CNAME"].records] == [d], f"mta-sts CNAME → {domain}"),
        check([x.lower() for x in r["WWW"].records] == [d], f"www CNAME → {domain}"),
        check(any("p=" in t for t in r["DKIM:opendkim"].records), "DKIM opendkim"),
    ]
    recommended = [
        check(any(t.lower().startswith("v=spf1") for t in r["TXT"].records), "SPF"),
        check(dmarc.get("p", "").lower() == "reject" and dmarc.get("adkim") == "s" and dmarc.get("aspf") == "s",
              "DMARC p=reject, adkim=s, aspf=s"),
        check(any("letsencrypt.org" in c for c in r["CAA"].records), "CAA letsencrypt.org"),
        check(any("dkim=discardable" in t for t in r["ADSP"].records), "_adsp dkim=discardable"),
        check(srv_ok, "SRV 587 / 465 / 143 / 993"),
    ]
    if policy is not None:
        required.append(check(policy.get("mode") == "enforce" and domain in policy["mx"],
                              f"MTA-STS policy: enforce, mx {domain}"))
    lines = ["", "🦉 **Chatmail relay** (what `cmdeploy dns` expects)"]
    for title, items, bad in (("Required", required, "❌"), ("Recommended", recommended, "⚠️")):
        missing = [label[2:] for ok, label in items if not ok]
        good = len(items) - len(missing)
        lines.append(f"{'✅' if not missing else bad} {title}: {good}/{len(items)}"
                     + (f" — missing: {', '.join(missing)}" if missing else ""))
    if all(ok for ok, _ in required + recommended):
        lines.append("🎉 All entries are in place.")
    else:
        lines.append("Run `cmdeploy dns` on the relay to print the exact records.")
    return lines


# ── Command, queue and cache ──

def _pick_resolver(name, server):
    """(resolver, label, system resolver) for ``@server``, or an error text starting with ❌."""
    sysres = make_resolver()
    if server == "auth" and not isinstance(name, str):
        return "❌ `@auth` works with domain names only."
    resolved = _resolve_server(server, name if isinstance(name, str) else "", sysres)
    if isinstance(resolved, str):
        return f"❌ {resolved}"
    return resolved[0], resolved[1], sysres


def run(name, rtype, server) -> str:
    picked = _pick_resolver(name, server)
    if isinstance(picked, str):
        return picked
    res, via, sysres = picked
    if rtype:
        return format_records(name, rtype, res, via)
    return build_report(name, res, via, sysres)


def run_host(name, server) -> str:
    picked = _pick_resolver(name, server)
    if isinstance(picked, str):
        return picked
    res, via, _ = picked
    return host_report(name, res, via)


def _cache_get(key):
    with state._dns_lock:
        hit = state._dns_cache.get(key)
        if hit and time.time() - hit[0] < config.DNS_CACHE_SECONDS:
            return hit[1]
    return None


def _cache_put(key, text):
    with state._dns_lock:
        state._dns_cache[key] = (time.time(), text)
        if len(state._dns_cache) > 300:
            oldest = min(state._dns_cache, key=lambda k: state._dns_cache[k][0])
            state._dns_cache.pop(oldest, None)


def _worker(bot, accid, chat_id, msg_id, job, key):
    """Run ``job()`` (returns the reply text) on the pool; /dns and /host share it."""
    try:
        text = _cache_get(key)  # an identical request ahead in the queue may have filled it
        if text is None:
            text = job()
            if not text.startswith("❌"):
                _cache_put(key, text)
        dc_helpers._react(bot, accid, msg_id, "❌" if text.startswith("❌") else "☑️")
        dc_helpers._send(bot, accid, chat_id, text, reply_to_id=msg_id)
    except Exception as e:
        config.logger.error(f"Error in /dns worker: {e}", exc_info=True)
        dc_helpers._react(bot, accid, msg_id, "❌")
        dc_helpers._send(bot, accid, chat_id, f"❌ DNS lookup failed: {e}", reply_to_id=msg_id)
    finally:
        with state._dns_lock:
            state._dns_pending -= 1


_pool = ThreadPoolExecutor(max_workers=config.DNS_WORKERS, thread_name_prefix="dns-report")


def _answer(bot, accid, msg, key, job):
    """Reply from the cache, or queue ``job`` (reacting ⏳) unless the queue is full."""
    cached = _cache_get(key)
    if cached is not None:
        dc_helpers._send(bot, accid, msg.chat_id, cached, reply_to_id=msg.id)
        return

    with state._dns_lock:
        full = state._dns_pending >= config.DNS_MAX_QUEUE
        if not full:
            state._dns_pending += 1
    if full:
        dc_helpers._send(bot, accid, msg.chat_id, "⏳ The DNS queue is full, please try again in a minute.",
                         reply_to_id=msg.id)
        return
    dc_helpers._react(bot, accid, msg.id, "⏳")
    _pool.submit(_worker, bot, accid, msg.chat_id, msg.id, job, key)


@config.dc_cli.on(events.NewMessage(command="/dig"))  # alias, for dig users
@config.dc_cli.on(events.NewMessage(command="/dns"))
def dns_command(bot, accid, event):
    msg = event.msg
    payload = (event.payload or "").strip()
    if not payload:
        dc_helpers._send(bot, accid, msg.chat_id, USAGE, reply_to_id=msg.id)
        return
    parsed = parse_args(payload)
    if isinstance(parsed, str):
        dc_helpers._send(bot, accid, msg.chat_id, f"❌ {parsed}\n\n{USAGE}", reply_to_id=msg.id)
        return
    name, rtype, server = parsed
    # A lambda, not run itself: run is looked up when the job starts (tests patch it)
    _answer(bot, accid, msg, f"{name}|{rtype}|{server}", lambda: run(name, rtype, server))


@config.dc_cli.on(events.NewMessage(command="/host"))
def host_command(bot, accid, event):
    msg = event.msg
    payload = (event.payload or "").strip()
    if not payload:
        dc_helpers._send(bot, accid, msg.chat_id, HOST_USAGE, reply_to_id=msg.id)
        return
    parsed = parse_args(payload)
    if isinstance(parsed, str):
        parsed = parsed.replace("/dns", "/host")  # parse_args' hints name /dns
    elif parsed[1] and isinstance(parsed[0], str):  # an IP always comes back as PTR: that one is fine
        parsed = f"`/host` shows A, AAAA and MX; for {parsed[1]} use `/dns {_display(parsed[0])} {parsed[1]}`."
    if isinstance(parsed, str):
        dc_helpers._send(bot, accid, msg.chat_id, f"❌ {parsed}\n\n{HOST_USAGE}", reply_to_id=msg.id)
        return
    name, _, server = parsed
    # "host|" keeps these apart from /dns answers for the same name in the shared cache
    _answer(bot, accid, msg, f"host|{name}|{server}", lambda: run_host(name, server))
