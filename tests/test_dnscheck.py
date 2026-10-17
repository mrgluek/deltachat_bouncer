import os
import sys
import unittest
import time
import tempfile
import hashlib
import base64
import ipaddress
from unittest.mock import MagicMock, patch

# Setup test environment
TEST_DB = "test_dns_bouncer.db"
os.environ["DB_PATH"] = TEST_DB

# Mock packages if not installed
try:
    import deltachat2
except ImportError:
    mock_deltachat2 = MagicMock()
    class MsgData:
        def __init__(self, text="", file="", override_sender_name=None):
            self.text = text
            self.file = file
            self.override_sender_name = override_sender_name
    mock_deltachat2.MsgData = MsgData
    class SystemMessageType:
        MEMBER_ADDED_TO_GROUP = 1
        MEMBER_REMOVED_FROM_GROUP = 2
        GROUP_NAME_CHANGED = 3
        GROUP_IMAGE_CHANGED = 4
    mock_deltachat2.SystemMessageType = SystemMessageType
    sys.modules['deltachat2'] = mock_deltachat2

try:
    import deltabot_cli
except ImportError:
    class MockBotCli:
        def __init__(self, *args, **kwargs):
            pass
        def on(self, *args, **kwargs):
            return lambda func: func
        def on_init(self, func):
            return func
        def on_start(self, func):
            return func
        def start(self):
            pass
    mock_deltabot_cli = MagicMock()
    mock_deltabot_cli.BotCli = MockBotCli
    sys.modules['deltabot_cli'] = mock_deltabot_cli

try:
    import qrcode
except ImportError:
    sys.modules['qrcode'] = MagicMock()

# Add parent directory to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import bot
import config
import dc_helpers
import state
import dnscheck



import dns.rdata
import dns.rdataclass
import dns.rdatatype
import dns.resolver
import dns.reversename

D = "relay.example"
# A real 2048-bit key, as opendkim would publish it (base64 of the DER public key)
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
KEY = base64.b64encode(rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key().public_bytes(
    serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()


def txt(value):
    """TXT rdata text: split into 255-character strings, as cmdeploy prints DKIM records."""
    return " ".join(f'"{value[i:i + 255]}"' for i in range(0, len(value), 255))


def rd(rtype, text):
    return dns.rdata.from_text(dns.rdataclass.IN, dns.rdatatype.from_text(rtype), text)


def ok(rtype, *texts, chain=None):
    rds = [rd(rtype, t) for t in texts]
    return dnscheck.Res("ok", [dnscheck._rdata_text(x) for x in rds], 300, chain, rdata=rds)


def chatmail_zone(**overrides):
    """A zone laid out exactly like `cmdeploy dns` prints it."""
    ptr = dns.reversename.from_address("203.0.113.5").to_text().rstrip(".")
    zone = {
        (D, "A"): ok("A", "203.0.113.5"),
        (D, "MX"): ok("MX", f"10 {D}."),
        (D, "TXT"): ok("TXT", '"v=spf1 a ~all"'),
        (D, "CAA"): ok("CAA", '0 issue "letsencrypt.org;accounturi=https://acme.example/acct/1"'),
        (f"_dmarc.{D}", "TXT"): ok("TXT", '"v=DMARC1;p=reject;adkim=s;aspf=s"'),
        (f"_mta-sts.{D}", "TXT"): ok("TXT", '"v=STSv1; id=202601010000"'),
        (f"mta-sts.{D}", "CNAME"): ok("CNAME", f"{D}."),
        (f"www.{D}", "CNAME"): ok("CNAME", f"{D}."),
        (f"_adsp._domainkey.{D}", "TXT"): ok("TXT", '"dkim=discardable"'),
        (f"opendkim._domainkey.{D}", "TXT"): ok("TXT", txt(f"v=DKIM1;k=rsa;p={KEY};s=email;t=s")),
        (f"_submission._tcp.{D}", "SRV"): ok("SRV", f"0 1 587 {D}."),
        (f"_submissions._tcp.{D}", "SRV"): ok("SRV", f"0 1 465 {D}."),
        (f"_imap._tcp.{D}", "SRV"): ok("SRV", f"0 1 143 {D}."),
        (f"_imaps._tcp.{D}", "SRV"): ok("SRV", f"0 1 993 {D}."),
        (ptr, "PTR"): ok("PTR", f"{D}."),
        ("example", "NS"): ok("NS", "ns1.example.net."),
    }
    zone.update(overrides)
    return zone


class FakeDNS:
    """Patches dnscheck.query / zone lookup / MTA-STS fetch with a dict of answers."""

    def __init__(self, zone, policy=None, policy_err=None):
        self.zone = zone
        self.policy = policy if policy is not None else {"version": "STSv1", "mode": "enforce", "mx": [D]}
        self.policy_err = policy_err

    def query(self, res, name, rtype):
        return self.zone.get((name.rstrip("."), rtype)) or dnscheck.Res("nodata")

    def __enter__(self):
        self.patches = [
            patch.object(dnscheck, "query", side_effect=self.query),
            patch.object(dnscheck.dns.resolver, "zone_for_name", return_value=dns.name.from_text("example")),
            patch.object(dnscheck, "fetch_mta_sts_policy",
                         return_value=(None, self.policy_err) if self.policy_err else (self.policy, None)),
        ]
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *a):
        for p in self.patches:
            p.stop()


def report(zone, **kw):
    with FakeDNS(zone, **kw):
        return dnscheck.build_report(D, MagicMock(), "bot's resolver", MagicMock())


import dns.name  # noqa: E402


class TestParseArgs(unittest.TestCase):
    def test_dig_like_order_free(self):
        self.assertEqual(dnscheck.parse_args("chatmail.uk"), ("chatmail.uk", None, None))
        self.assertEqual(dnscheck.parse_args("mx Chatmail.UK."), ("chatmail.uk", "MX", None))
        self.assertEqual(dnscheck.parse_args("@1.1.1.1 chatmail.uk TXT"), ("chatmail.uk", "TXT", "1.1.1.1"))
        self.assertEqual(dnscheck.parse_args("chatmail.uk @auth"), ("chatmail.uk", None, "auth"))

    def test_underscore_and_idn_names(self):
        self.assertEqual(dnscheck.parse_args("_dmarc.chatmail.uk TXT")[0], "_dmarc.chatmail.uk")
        self.assertEqual(dnscheck.parse_args("пример.рф")[0], "xn--e1afmkfd.xn--p1ai")

    def test_ip_means_ptr(self):
        name, rtype, _ = dnscheck.parse_args("213.165.89.146")
        self.assertEqual((str(name), rtype), ("213.165.89.146", "PTR"))
        self.assertIsInstance(dnscheck.parse_args("1.2.3.4 MX"), str)

    def test_errors(self):
        for text in ("chatmail.uk ANY", "a.org b.org", "chatmail.uk MX TXT", "chatmail.uk @",
                     "MX", "not_a_domain!", "x @1.1.1.1 @8.8.8.8"):
            self.assertIsInstance(dnscheck.parse_args(text), str, text)


class TestServer(unittest.TestCase):
    def test_private_resolver_refused(self):
        for server in ("10.0.0.1", "127.0.0.1", "192.168.1.1"):
            self.assertIsInstance(dnscheck._resolve_server(server, "a.org", MagicMock()), str, server)

    def test_public_resolver(self):
        res, via = dnscheck._resolve_server("1.1.1.1", "a.org", MagicMock())
        self.assertEqual(res.nameservers, ["1.1.1.1"])
        self.assertEqual(via, "resolver 1.1.1.1")

    def test_auth_uses_zone_name_server(self):
        zone = {("chatmail.uk", "NS"): ok("NS", "ray.ns.cloudflare.com."),
                ("ray.ns.cloudflare.com", "A"): ok("A", "173.245.59.1")}
        with FakeDNS(zone):
            dnscheck.dns.resolver.zone_for_name.return_value = dns.name.from_text("chatmail.uk")
            res, via = dnscheck._resolve_server("auth", "chatmail.uk", MagicMock())
        self.assertEqual(res.nameservers, ["173.245.59.1"])
        self.assertIn("authoritative ray.ns.cloudflare.com", via)

    def test_default_resolvers_are_public(self):
        with patch.object(config, "DNS_RESOLVERS", ["1.1.1.1", "8.8.8.8"]):
            res, via = dnscheck._resolve_server(None, "a.org", dnscheck.make_resolver())
            self.assertEqual(res.nameservers, ["1.1.1.1", "8.8.8.8"])
            self.assertEqual(via, "resolver 1.1.1.1, 8.8.8.8")
        with patch.object(config, "DNS_RESOLVERS", []):
            self.assertEqual(dnscheck.default_label(), "bot's resolver")

    def test_auth_needs_a_name(self):
        self.assertIn("domain names only", dnscheck.run(ipaddress.ip_address("1.1.1.1"), "PTR", "auth"))


class TestRecords(unittest.TestCase):
    def test_cname_chain_and_ttl(self):
        zone = {(f"www.{D}", "MX"): ok("MX", f"10 {D}.", chain=[(f"www.{D}.", f"{D}.")])}
        with FakeDNS(zone):
            text = dnscheck.format_records(f"www.{D}", "MX", MagicMock(), "bot's resolver")
        self.assertIn(f"↪ www.{D}. CNAME {D}.", text)
        self.assertIn(f"10 {D}.", text)
        self.assertIn("TTL 300", text)

    def test_nxdomain(self):
        with FakeDNS({(D, "A"): dnscheck.Res("nxdomain")}):
            self.assertIn("NXDOMAIN", dnscheck.format_records(D, "A", MagicMock(), "x"))

    def test_long_txt_joined(self):
        rec = rd("TXT", '"v=DKIM1;k=rsa;" "p=ABC"')
        self.assertEqual(dnscheck._rdata_text(rec), "v=DKIM1;k=rsa;p=ABC")


class TestReport(unittest.TestCase):
    def test_complete_chatmail_relay(self):
        text = report(chatmail_zone())
        self.assertIn(f"✅ MX 10 {D} → 203.0.113.5", text)
        self.assertIn(f"✅ PTR 203.0.113.5 → {D} (matches the MX name; resolves back)", text)
        self.assertIn("✅ SPF: `v=spf1 a ~all`", text)
        self.assertIn("✅ covers the MX addresses", text)
        self.assertIn("✅ DKIM `opendkim`: RSA 2048 bit", text)
        self.assertIn("✅ DMARC: p=reject, adkim=s, aspf=s", text)
        self.assertIn("✅ MTA-STS id=202601010000: mode enforce", text)
        self.assertIn("Chatmail relay", text)
        self.assertIn("✅ Required: 7/7", text)
        self.assertIn("✅ Recommended: 5/5", text)
        self.assertIn("All entries are in place", text)

    def test_chatmail_missing_entries(self):
        zone = chatmail_zone()
        del zone[(f"www.{D}", "CNAME")]
        del zone[(f"_imaps._tcp.{D}", "SRV")]
        zone[(f"_dmarc.{D}", "TXT")] = ok("TXT", '"v=DMARC1;p=none"')
        text = report(zone)
        self.assertIn(f"❌ Required: 6/7 — missing: www CNAME → {D}", text)
        self.assertIn("⚠️ Recommended: 3/5 — missing: DMARC p=reject, adkim=s, aspf=s, SRV 587 / 465 / 143 / 993", text)
        self.assertIn("cmdeploy dns", text)
        self.assertIn("⚠️ DMARC: p=none", text)

    def test_no_chatmail_block_for_other_domains(self):
        zone = chatmail_zone()
        zone[(D, "MX")] = ok("MX", "10 mx.provider.example.")
        zone[("mx.provider.example", "A")] = ok("A", "203.0.113.5")
        self.assertNotIn("Chatmail", report(zone))
        zone = chatmail_zone()
        del zone[(f"opendkim._domainkey.{D}", "TXT")]
        self.assertNotIn("Chatmail", report(zone))

    def test_mail_problems(self):
        zone = chatmail_zone()
        zone[(D, "TXT")] = ok("TXT", '"v=spf1 +all"', '"v=spf1 -all"')
        del zone[(dns.reversename.from_address("203.0.113.5").to_text().rstrip("."), "PTR")]
        text = report(zone, policy={"version": "STSv1", "mode": "enforce", "mx": ["other.example"]})
        self.assertIn("❌ SPF: 2 records", text)
        self.assertIn("❌ 203.0.113.5: no PTR", text)
        self.assertIn(f"policy does not list MX {D}", text)

    def test_spf_coverage(self):
        zone = chatmail_zone()
        zone[(D, "TXT")] = ok("TXT", '"v=spf1 ip4:198.51.100.0/24 include:_spf.provider.example ~all"')
        text = report(zone)
        self.assertIn("does not directly cover 203.0.113.5 (may be covered by include:_spf.provider.example)", text)
        zone[(D, "TXT")] = ok("TXT", '"v=spf1 ip4:203.0.113.0/24 -all"')
        self.assertIn("✅ covers the MX addresses", report(zone))
        zone[(D, "TXT")] = ok("TXT", '"v=spf1 +all"')
        self.assertIn("❌ SPF: `v=spf1 +all` — `+all` lets anyone send", report(zone))

    def test_mta_sts_fetch_error_and_wildcard(self):
        self.assertIn("❌ MTA-STS id=202601010000: policy URL answered HTTP 404",
                      report(chatmail_zone(), policy_err="policy URL answered HTTP 404"))
        self.assertTrue(dnscheck._sts_mx_match("*.example.org", "mx1.example.org"))
        self.assertFalse(dnscheck._sts_mx_match("*.example.org", "a.mx1.example.org"))
        self.assertFalse(dnscheck._sts_mx_match("*.example.org", "example.org"))

    def test_null_mx_and_no_mx(self):
        zone = chatmail_zone()
        zone[(D, "MX")] = ok("MX", "0 .")
        self.assertIn("Null MX", report(zone))
        del zone[(D, "MX")]
        text = report(zone)
        self.assertIn("No MX records", text)
        self.assertNotIn("SPF", text)

    def test_nonexistent_name(self):
        self.assertIn("NXDOMAIN", report({(D, "A"): dnscheck.Res("nxdomain")}))
        self.assertIn("No A, AAAA, MX or TXT records", report({}))

    def test_dkim_key_sizes(self):
        self.assertEqual(dnscheck._dkim_key_info(f"v=DKIM1;k=rsa;p={KEY}"), ("✅", "RSA 2048 bit"))
        self.assertEqual(dnscheck._dkim_key_info("v=DKIM1;p=")[0], "⚠️")
        self.assertEqual(dnscheck._dkim_key_info("v=DKIM1;k=ed25519;p=11qYAYKxCrfVS/7TyWQHOg7hcvPapiMlrwIaaPcHURo=")[1], "Ed25519")
        self.assertEqual(dnscheck._dkim_key_info("v=DKIM1;p=!!!")[0], "❌")

    def test_caa_grouped(self):
        caa = ok("CAA", '0 issue "letsencrypt.org;accounturi=x"', '0 issue "pki.goog"', '0 issuewild ";"')
        self.assertEqual(dnscheck._caa_line(caa), "CAA issue: letsencrypt.org, pki.goog · issuewild: ;")


class TestDnsCommand(unittest.TestCase):
    def setUp(self):
        self.bot = MagicMock()
        state._dns_cache.clear()
        state._dns_pending = 0

    def _event(self, payload):
        ev = MagicMock()
        ev.msg.chat_id, ev.msg.id = 100, 200
        ev.payload = payload
        return ev

    def test_usage_and_errors(self):
        with patch.object(dc_helpers, "_send") as send:
            dnscheck.dns_command(self.bot, 1, self._event(""))
            self.assertIn("Usage", send.call_args[0][3])
            dnscheck.dns_command(self.bot, 1, self._event("chatmail.uk ANY"))
            self.assertIn("ANY", send.call_args[0][3])

    def test_queued_then_answered_and_cached(self):
        with patch.object(dc_helpers, "_send") as send, patch.object(dc_helpers, "_react"), \
                patch.object(dnscheck, "run", return_value="report text") as run, \
                patch.object(dnscheck, "_pool") as pool:
            pool.submit.side_effect = lambda fn, *a: fn(*a)  # run the queued job right away
            dnscheck.dns_command(self.bot, 1, self._event("chatmail.uk"))
            dnscheck.dns_command(self.bot, 1, self._event("chatmail.uk"))
        self.assertEqual(run.call_count, 1)  # the second came from the cache
        self.assertEqual(send.call_args[0][3], "report text")
        self.assertEqual(state._dns_pending, 0)

    def test_queue_limit(self):
        state._dns_pending = config.DNS_MAX_QUEUE
        with patch.object(dc_helpers, "_send") as send, patch.object(dnscheck, "_pool") as pool:
            dnscheck.dns_command(self.bot, 1, self._event("chatmail.uk"))
        self.assertIn("queue is full", send.call_args[0][3])
        pool.submit.assert_not_called()


    def test_host_usage_and_type_refused(self):
        with patch.object(dc_helpers, "_send") as send:
            dnscheck.host_command(self.bot, 1, self._event(""))
            self.assertIn("like the `host` tool", send.call_args[0][3])
            dnscheck.host_command(self.bot, 1, self._event("chatmail.uk TXT"))
            self.assertIn("`/dns chatmail.uk TXT`", send.call_args[0][3])
            dnscheck.host_command(self.bot, 1, self._event("not_a host!"))
            self.assertIn("❌", send.call_args[0][3])

    def test_host_cached_apart_from_dns(self):
        with patch.object(dc_helpers, "_send") as send, patch.object(dc_helpers, "_react"), \
                patch.object(dnscheck, "run", return_value="dns text"), \
                patch.object(dnscheck, "run_host", return_value="host text") as run_host, \
                patch.object(dnscheck, "_pool") as pool:
            pool.submit.side_effect = lambda fn, *a: fn(*a)
            dnscheck.dns_command(self.bot, 1, self._event("chatmail.uk"))
            dnscheck.host_command(self.bot, 1, self._event("chatmail.uk"))
            self.assertEqual(send.call_args[0][3], "host text")
            dnscheck.host_command(self.bot, 1, self._event("chatmail.uk"))
        self.assertEqual(run_host.call_count, 1)
        self.assertEqual(state._dns_pending, 0)


class TestHostReport(unittest.TestCase):
    def host(self, zone, name=D):
        with FakeDNS(zone):
            return dnscheck.host_report(name, MagicMock(), "bot's resolver")

    def test_addresses_and_mail_sorted(self):
        text = self.host({(D, "A"): ok("A", "203.0.113.5"),
                          (D, "AAAA"): ok("AAAA", "2001:db8::5"),
                          (D, "MX"): ok("MX", f"20 mx2.{D}.", f"10 mx1.{D}.")})
        self.assertEqual(text.splitlines()[1:], [
            f"{D} has address 203.0.113.5",
            f"{D} has IPv6 address 2001:db8::5",
            f"{D} mail is handled by 10 mx1.{D}.",
            f"{D} mail is handled by 20 mx2.{D}.",
        ])

    def test_cname_shown_once(self):
        chain = [(f"www.{D}.", f"{D}.")]
        text = self.host({(f"www.{D}", "A"): ok("A", "203.0.113.5", chain=chain),
                          (f"www.{D}", "MX"): ok("MX", f"10 {D}.", chain=chain)}, name=f"www.{D}")
        self.assertEqual(text.count("is an alias for"), 1)
        self.assertIn(f"www.{D} is an alias for {D}.", text)
        self.assertIn(f"{D} has address 203.0.113.5", text)

    def test_nxdomain(self):
        text = self.host({(D, t): dnscheck.Res("nxdomain") for t in ("A", "AAAA", "MX")})
        self.assertIn(f"Host {D} not found: 3(NXDOMAIN)", text)

    def test_no_records_and_errors(self):
        self.assertIn("has no A, AAAA or MX records", self.host({}))
        text = self.host({(D, "A"): ok("A", "203.0.113.5"), (D, "AAAA"): dnscheck.Res("timeout")})
        self.assertIn(f"{D} AAAA: timed out", text)
        self.assertNotIn("has no", text)

    def test_null_mx(self):
        self.assertIn("accepts no mail (null MX)", self.host({(D, "MX"): ok("MX", "0 .")}))

    def test_ptr(self):
        ip = ipaddress.ip_address("203.0.113.5")
        q = "5.113.0.203.in-addr.arpa"
        self.assertIn(f"{q} domain name pointer {D}.", self.host({(q, "PTR"): ok("PTR", f"{D}.")}, name=ip))
        self.assertIn(f"{q} has no PTR record", self.host({}, name=ip))


if __name__ == "__main__":
    unittest.main()
