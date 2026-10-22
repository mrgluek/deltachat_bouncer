"""Certificate Transparency: the "Certificates" part of the /dns report.

Every publicly trusted TLS certificate is logged in CT logs, so they show
which certificates exist for a domain right now - including ones its owner
never asked for, and subdomain names that are nowhere in DNS.

Data source: SSLMate Cert Spotter (api.certspotter.com/v1/issuances). It
returns only certificates that have not expired yet. Works without a key;
CERTSPOTTER_API_KEY in .env (free "Small" plan) gives documented limits:
10 full-domain queries (with subdomains) and 100 single-hostname queries an
hour. The bot counts its own queries and falls back to a single-hostname
query when the full-domain budget is spent, so the domain's own certificates
are still shown, only without the subdomains.

Censys is not used here: its free plan cannot search certificates by domain
(lookup by SHA-256 only) and every lookup costs one of 100 monthly credits.
"""
import calendar
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import config

CERTSPOTTER_URL = "https://api.certspotter.com/v1/issuances"
CACHE_SECONDS = 3600          # CT changes slowly; /dns itself caches for 5 minutes
FULL_PER_HOUR = 10            # Cert Spotter Small plan: full-domain queries / hour
SINGLE_PER_HOUR = 100         # ... and single-hostname queries / hour
EXPIRY_WARN_DAYS = 10         # Let's Encrypt renews at 1/3 of the lifetime left (30 days of 90)
MAX_NAMES_SHOWN = 15
FULL_PAGE = 50                # a page this big likely has more after it

# CA "friendly names" as Cert Spotter gives them -> the domains a CAA record names
# them by. A CA missing here is just not checked against CAA.
CAA_DOMAINS = {
    "Let's Encrypt": {"letsencrypt.org"},
    "Google Trust Services": {"pki.goog"},
    "SSL.com": {"ssl.com"},
    "Sectigo": {"sectigo.com", "comodoca.com", "comodo.com", "usertrust.com", "trust-provider.com"},
    "ZeroSSL": {"sectigo.com", "zerossl.com"},
    "DigiCert": {"digicert.com", "symantec.com", "thawte.com", "geotrust.com", "rapidssl.com"},
    "GlobalSign": {"globalsign.com"},
    "Buypass": {"buypass.com", "buypass.no"},
    "Amazon": {"amazon.com", "amazontrust.com", "awstrust.com", "amazonaws.com"},
    "Microsoft": {"microsoft.com"},
    "Entrust": {"entrust.net"},
    "GoDaddy": {"godaddy.com", "starfieldtech.com"},
    "HARICA": {"harica.gr"},
    "Certum": {"certum.pl", "certum.eu"},
}

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}
_sent: dict[str, list[float]] = {"full": [], "single": []}  # query times, last hour
_paused_until = 0.0  # after HTTP 429


def _api_key() -> str:
    return os.environ.get("CERTSPOTTER_API_KEY", "").strip().strip("'\"")


def _take(kind: str) -> bool:
    """Reserve one query of ``kind`` within the hourly budget."""
    limit = FULL_PER_HOUR if kind == "full" else SINGLE_PER_HOUR
    now = time.time()
    with _lock:
        if now < _paused_until:
            return False
        _sent[kind] = [t for t in _sent[kind] if now - t < 3600]
        if len(_sent[kind]) >= limit:
            return False
        _sent[kind].append(now)
        return True


def _fetch(domain: str, subdomains: bool) -> tuple[list | None, int, bool]:
    """-> (issuances, http_status, more_pages)."""
    global _paused_until
    q = urllib.parse.urlencode([("domain", domain), ("include_subdomains", "true" if subdomains else "false"),
                                ("expand", "dns_names"), ("expand", "issuer")])
    headers = {"User-Agent": f"BouncerBot/{config.VERSION}", "Accept": "application/json"}
    if _api_key():
        headers["Authorization"] = f"Bearer {_api_key()}"
    req = urllib.request.Request(f"{CERTSPOTTER_URL}?{q}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, list):
                return None, resp.status, False
            # One page only (each page is a query). The API sends a "next" link even
            # on a short last page, so only a big page counts as "there is more".
            more = 'rel="next"' in (resp.headers.get("Link") or "") and len(data) >= FULL_PAGE
            return data, resp.status, more
    except urllib.error.HTTPError as e:
        if e.code == 429:
            try:
                wait = min(int(e.headers.get("Retry-After") or 600), 3600)
            except ValueError:
                wait = 600
            with _lock:
                _paused_until = time.time() + wait
            config.logger.warning(f"/dns: Cert Spotter rate limit, pausing {wait} s")
        return None, e.code, False
    except Exception as e:
        config.logger.warning(f"/dns: Cert Spotter request failed: {e}")
        return None, 0, False


def lookup(domain: str) -> dict:
    """{"certs": [...], "subdomains": bool, "more": bool} or {"error": text}."""
    with _lock:
        hit = _cache.get(domain)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    result = None
    if _take("full"):
        certs, status, more = _fetch(domain, True)
        if certs is not None:
            result = {"certs": certs, "subdomains": True, "more": more}
    if result is None and _take("single"):
        certs, status, more = _fetch(domain, False)
        if certs is not None:
            result = {"certs": certs, "subdomains": False, "more": more}
    if result is None:
        return {"error": "Cert Spotter did not answer or its hourly limit is reached, try later"}
    with _lock:
        _cache[domain] = (time.time(), result)
        if len(_cache) > 300:
            _cache.pop(min(_cache, key=lambda k: _cache[k][0]), None)
    return result


def _parse_time(s: str) -> float:
    try:
        return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))  # the API gives UTC
    except (ValueError, TypeError):
        return 0.0


def _covers(cert: dict, name: str) -> bool:
    """Does the certificate cover ``name`` (exactly or by a one-label wildcard)?"""
    for n in cert.get("dns_names") or []:
        n = n.lower().rstrip(".")
        # "*.example.org" covers "a.example.org", not "example.org" or "a.b.example.org"
        if n == name or (n.startswith("*.") and "." in name and name.split(".", 1)[1] == n[2:]):
            return True
    return False


def _issuer(cert: dict) -> str:
    iss = cert.get("issuer") or {}
    return iss.get("friendly_name") or (iss.get("name") or "?").split("CN=")[-1]


def is_cloudflare(cert: dict) -> bool:
    # Universal SSL certificates are signed by "Cloudflare TLS Issuing ..." intermediates
    return "cloudflare" in ((cert.get("issuer") or {}).get("name") or "").lower()


def _caa_allows(cert: dict, caa: dict[str, list[str]]) -> bool | None:
    """True / False against the domain's CAA (issuewild for wildcards), None if unknown."""
    if not caa:
        return None
    domains = CAA_DOMAINS.get(_issuer(cert))
    if domains is None:
        return None
    wild = any(n.startswith("*.") for n in cert.get("dns_names") or [])
    allowed = caa.get("issuewild") if wild and "issuewild" in caa else caa.get("issue", [])
    return bool(domains & {a.lower() for a in allowed})


def report_lines(domain: str, data: dict, caa: dict[str, list[str]]) -> list[str]:
    """The "Certificates" section. ``caa``: {"issue": [...], "issuewild": [...]} of the domain."""
    lines = ["", "🔐 **Certificates** (CT logs, not expired)"]
    if data.get("error"):
        return lines + [f"ℹ️ {data['error']}"]
    certs = data["certs"]
    if not certs:
        return lines + ["ℹ️ No valid certificate in CT logs: no HTTPS with a public certificate"]
    now = time.time()
    count = f"{len(certs)}+" if data.get("more") else str(len(certs))
    latest = max(c.get("not_before") or "" for c in certs)[:10]
    lines.append(f"{count} certificate{'s' if len(certs) != 1 else ''}"
                 + ("" if data["subdomains"] else f" for {domain} (subdomains skipped: hourly limit)")
                 + f" · latest issued {latest}")

    by_issuer: dict[str, int] = {}
    for c in certs:
        label = _issuer(c) + (" via Cloudflare" if is_cloudflare(c) else "")
        by_issuer[label] = by_issuer.get(label, 0) + 1
    lines.append("Issuers: " + ", ".join(f"{k} ×{v}" for k, v in sorted(by_issuer.items(), key=lambda kv: -kv[1])))

    own = [c for c in certs if _covers(c, domain)]
    if own:
        until = max(_parse_time(c.get("not_after")) for c in own)
        days = int((until - now) // 86400)
        icon = "⚠️" if days < EXPIRY_WARN_DAYS else "✅"
        hint = " - renewal may be failing" if days < EXPIRY_WARN_DAYS else ""
        lines.append(f"{icon} {domain} is covered until {time.strftime('%Y-%m-%d', time.gmtime(until))}"
                     f" ({days} days){hint}")
    else:
        lines.append(f"ℹ️ No certificate names {domain} itself")

    bad = [c for c in certs if _caa_allows(c, caa) is False]
    for c in bad[:3]:
        names = ", ".join((c.get("dns_names") or [])[:3])
        lines.append(f"⚠️ {_issuer(c)} is not allowed by the current CAA: {names} "
                     f"(issued {(c.get('not_before') or '')[:10]}; CAA may have changed since)")
    if any(is_cloudflare(c) for c in certs):
        lines.append("☁️ Cloudflare Universal SSL: Cloudflare issues these itself for proxied names "
                     "and adds its CAs to the CAA answer")

    if data["subdomains"]:
        names = sorted({n.lower().removeprefix("*.") for c in certs for n in c.get("dns_names") or []}
                       - {domain}, key=lambda n: (n.count("."), n))
        names = [n for n in names if n.endswith("." + domain)]
        if names:
            more = f" and {len(names) - MAX_NAMES_SHOWN} more" if len(names) > MAX_NAMES_SHOWN else ""
            lines.append(f"🔤 Names in certificates: {', '.join(names[:MAX_NAMES_SHOWN])}{more}")
    return lines


def chatmail_covered(domain: str, data: dict) -> bool | None:
    """Does one valid Let's Encrypt certificate cover the relay names cmdeploy sets up?"""
    if data.get("error"):
        return None
    need = (domain, f"mta-sts.{domain}", f"www.{domain}")
    return any(_issuer(c) == "Let's Encrypt" and all(_covers(c, n) for n in need) for c in data["certs"])
