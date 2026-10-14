import os
import sys
import unittest
import time
import tempfile
import hashlib
import ipaddress
from unittest.mock import MagicMock, patch

# Setup test environment
TEST_DB = "test_whois_bouncer.db"
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
import iplookup
import whoislookup



from datetime import datetime, timedelta, timezone

RULES = {"uk", "co.uk", "ru", "msk.ru", "com", "chat", "*.ck", "!www.ck", "xn--p1ai"}

RDAP_DOMAIN = {
    "objectClassName": "domain", "ldhName": "CHATMAIL.UK",
    "status": ["client transfer prohibited"],
    "events": [{"eventAction": "registration", "eventDate": "2026-05-07T10:00:00Z"},
               {"eventAction": "expiration", "eventDate": "2099-05-07T10:00:00Z"}],
    "nameservers": [{"ldhName": "RAY.NS.CLOUDFLARE.COM"}, {"ldhName": "emma.ns.cloudflare.com"}],
    "secureDNS": {"delegationSigned": True},
    "entities": [
        {"roles": ["registrar"], "vcardArray": ["vcard", [["fn", {}, "text", "Cloudflare, Inc."]]],
         "entities": [{"roles": ["abuse"], "vcardArray": ["vcard", [["email", {}, "text", "abuse@cf.example"]]]}]},
        {"roles": ["registrant"], "vcardArray": ["vcard", [["fn", {}, "text", "REDACTED FOR PRIVACY"],
                                                           ["adr", {"cc": "GB"}, "text", ["", "", "", "", "", "", ""]]]]},
    ],
}

RU_WHOIS = """% TCI Whois Service. Terms of use:
domain:        YANDEX.RU
nserver:       ns1.yandex.ru. 213.180.193.1, 2a02:6b8::1
nserver:       ns2.yandex.ru. 93.158.134.1
state:         REGISTERED, DELEGATED, VERIFIED
org:           YANDEX, LLC.
taxpayer-id:   7736207543
registrar:     RU-CENTER-RU
created:       1997-09-23T09:45:07Z
paid-till:     2099-09-30T21:00:00Z
free-date:     2099-11-01
source:        TCI
"""

RDAP_IP = {
    "objectClassName": "ip network", "name": "APNIC-LABS", "type": "ASSIGNED PORTABLE", "country": "AU",
    "cidr0_cidrs": [{"v4prefix": "1.1.1.0", "length": 24}],
    "events": [{"eventAction": "registration", "eventDate": "2011-08-10T23:12:35Z"}],
    "entities": [
        {"roles": ["administrative"], "vcardArray": ["vcard", [["fn", {}, "text", "Admin Contact"]]]},
        {"roles": ["registrant"], "vcardArray": ["vcard", [["fn", {}, "text", "APNIC Research and Development"]]]},
        {"roles": ["abuse"], "vcardArray": ["vcard", [["email", {}, "text", "helpdesk@apnic.example"]]]},
    ],
}


def no_wait(host):
    pass


class TestRegisteredDomain(unittest.TestCase):
    def test_public_suffix_rules(self):
        cases = {"chatmail.uk": "chatmail.uk", "mail.chatmail.uk": "chatmail.uk",
                 "a.b.example.co.uk": "example.co.uk", "x.example.msk.ru": "example.msk.ru",
                 "delta.chat": "delta.chat", "foo.bar.ck": "foo.bar.ck", "a.www.ck": "www.ck",
                 "xn--e1afmkfd.xn--p1ai": "xn--e1afmkfd.xn--p1ai", "new.unknowntld": "new.unknowntld"}
        for name, expected in cases.items():
            self.assertEqual(whoislookup.registered_domain(name, RULES), expected, name)

    def test_public_suffix_itself(self):
        for name in ("co.uk", "ru", "msk.ru", "bar.ck"):
            self.assertIsNone(whoislookup.registered_domain(name, RULES), name)

    def test_without_list(self):
        self.assertEqual(whoislookup.registered_domain("mail.chatmail.uk", None), "chatmail.uk")


class TestParsing(unittest.TestCase):
    def test_rdap_domain(self):
        f = whoislookup.parse_rdap_domain(RDAP_DOMAIN)
        self.assertEqual(f["registrar"], "Cloudflare, Inc.")
        self.assertEqual(f["abuse"], "abuse@cf.example")
        self.assertIsNone(f["org"])  # "REDACTED FOR PRIVACY" is not shown as a name
        self.assertEqual(f["country"], "GB")
        self.assertEqual(f["ns"], ["ray.ns.cloudflare.com", "emma.ns.cloudflare.com"])
        self.assertEqual(f["dnssec"], "signed")
        text = whoislookup.format_domain("chatmail.uk", f)
        self.assertIn("📇 **chatmail.uk** · RDAP", text)
        self.assertIn("👤 Registrant: GB", text)
        self.assertIn("📅 Created: 2026-05-07", text)
        self.assertIn("⏳ Expires: 2099-05-07", text)

    def test_ru_whois_text(self):
        f = whoislookup.parse_whois_text(RU_WHOIS)
        self.assertEqual(f["registrar"], "RU-CENTER-RU")
        self.assertEqual(f["org"], "YANDEX, LLC.")
        self.assertEqual(f["ns"], ["ns1.yandex.ru", "ns2.yandex.ru"])
        self.assertEqual(f["status"], ["REGISTERED, DELEGATED, VERIFIED"])
        text = whoislookup.format_domain("yandex.ru", f)
        self.assertIn("👤 Registrant: YANDEX, LLC.", text)
        self.assertIn("🔓 Free after: 2099-11-01", text)

    def test_gtld_whois_text(self):
        f = whoislookup.parse_whois_text(
            "Domain Name: example.io\nRegistrar: NameCheap, Inc.\n"
            "Registry Expiry Date: 2099-11-22T00:00:00Z\n"
            "Domain Status: clientTransferProhibited https://icann.org/epp#clientTransferProhibited\n"
            "Name Server: NS1.DIGITALOCEAN.COM\nRegistrant Organization: Privacy service provided by Withheld for Privacy ehf\n")
        self.assertEqual(f["status"], ["clientTransferProhibited"])
        self.assertEqual(f["ns"], ["ns1.digitalocean.com"])
        self.assertIsNone(f["org"])

    def test_expiry_warnings(self):
        soon = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        past = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
        self.assertIn("⚠️ Expires", whoislookup.format_domain("a.ru", {"source": "WHOIS", "expires": soon}))
        self.assertIn("❌ Expires", whoislookup.format_domain("a.ru", {"source": "WHOIS", "expires": past}))
        self.assertIn("expired 3 days ago", whoislookup.format_domain("a.ru", {"source": "WHOIS", "expires": past}))


@patch.object(whoislookup, "_wait_turn", side_effect=no_wait)
@patch.object(whoislookup, "_load_psl", return_value=RULES)
class TestDomainLookup(unittest.TestCase):
    def test_rdap(self, *_):
        with patch.object(whoislookup, "rdap_base_for_tld", return_value="https://rdap.example/uk/"), \
                patch.object(whoislookup, "_get_json", return_value=(RDAP_DOMAIN, 200)) as get:
            text = whoislookup.lookup_domain("mail.chatmail.uk")
        get.assert_called_once_with("https://rdap.example/uk/domain/chatmail.uk")
        self.assertIn("mail.chatmail.uk is part of chatmail.uk", text)
        self.assertIn("Cloudflare, Inc.", text)

    def test_rdap_not_registered(self, *_):
        with patch.object(whoislookup, "rdap_base_for_tld", return_value="https://rdap.example/com/"), \
                patch.object(whoislookup, "_get_json", return_value=(None, 404)):
            self.assertIn("🆓 **free-zz.com** is not registered", whoislookup.lookup_domain("free-zz.com"))

    def test_port43_for_tld_without_rdap(self, *_):
        with patch.object(whoislookup, "rdap_base_for_tld", return_value=None), \
                patch.object(whoislookup, "whois_server_for_tld", return_value="whois.tcinet.ru"), \
                patch.object(whoislookup, "whois43", return_value=RU_WHOIS) as w:
            text = whoislookup.lookup_domain("yandex.ru")
        w.assert_called_once_with("whois.tcinet.ru", "yandex.ru")
        self.assertIn("📇 **yandex.ru** · WHOIS", text)
        self.assertIn("RU-CENTER-RU", text)

    def test_port43_not_found(self, *_):
        with patch.object(whoislookup, "rdap_base_for_tld", return_value=None), \
                patch.object(whoislookup, "whois_server_for_tld", return_value="whois.tcinet.ru"), \
                patch.object(whoislookup, "whois43", return_value="% TCI Whois\nNo entries found for the selected source(s).\n"):
            self.assertIn("is not registered (whois.tcinet.ru", whoislookup.lookup_domain("free-zz.ru"))

    def test_rdap_down_falls_back_to_port43(self, *_):
        with patch.object(whoislookup, "rdap_base_for_tld", return_value="https://rdap.example/"), \
                patch.object(whoislookup, "_get_json", return_value=(None, 503)), \
                patch.object(whoislookup, "whois_server_for_tld", return_value="whois.example"), \
                patch.object(whoislookup, "whois43", return_value="Domain Name: a.com\nRegistrar: R\n"):
            self.assertIn("· WHOIS", whoislookup.lookup_domain("a.com"))

    def test_public_suffix_goes_to_iana(self, *_):
        iana = "domain:       RU\norganisation: Coordination Center for TLD RU\nwhois:        whois.tcinet.ru\n"
        with patch.object(whoislookup, "whois43", return_value=iana), \
                patch.object(whoislookup, "rdap_base_for_tld", return_value=None):
            text = whoislookup.lookup_domain("msk.ru")
        self.assertIn("**.ru** · IANA", text)
        self.assertIn("msk.ru is a public suffix", text)
        self.assertIn("Coordination Center for TLD RU", text)


@patch.object(whoislookup, "_wait_turn", side_effect=no_wait)
class TestNumberLookup(unittest.TestCase):
    def test_ip_prefers_registrant(self, _):
        with patch.object(whoislookup, "rdap_base_for_ip", return_value="https://rdap.apnic.net/"), \
                patch.object(whoislookup, "_get_json", return_value=(RDAP_IP, 200)):
            text = whoislookup.lookup_ip(ipaddress.ip_address("1.1.1.1"))
        self.assertIn("APNIC RDAP", text)
        self.assertIn("🧱 Block: 1.1.1.0/24 (APNIC-LABS)", text)
        self.assertIn("🏢 Holder: APNIC Research and Development", text)
        self.assertIn("🚨 Abuse: helpdesk@apnic.example", text)

    def test_asn(self, _):
        data = {"objectClassName": "autnum", "name": "unspecified", "entities": [
            {"roles": ["registrant"], "vcardArray": ["vcard", [["fn", {}, "text", "Vodafone Limited"],
                                                               ["adr", {"label": "Street\nUNITED KINGDOM"}, "text", ["", "", "", "", "", "", ""]]]]}]}
        with patch.object(whoislookup, "rdap_base_for_asn", return_value="https://rdap.db.ripe.net/"), \
                patch.object(whoislookup, "_get_json", return_value=(data, 200)):
            text = whoislookup.lookup_asn(5378)
        self.assertIn("📇 **AS5378** · RIPE NCC RDAP", text)  # "unspecified" name hidden
        self.assertIn("Country: United Kingdom", text)
        self.assertIn("no contact listed", text)

    def test_bootstrap_longest_prefix(self, _):
        services = [[["1.0.0.0/8"], ["https://rdap.apnic.net/"]], [["1.1.0.0/16"], ["http://x/", "https://more.specific/"]]]
        with patch.object(whoislookup, "_bootstrap", return_value=services):
            self.assertEqual(whoislookup.rdap_base_for_ip(ipaddress.ip_network("1.1.1.1/32")), "https://more.specific/")
        asn = [[["1-1876", "5377-5631"], ["https://rdap.db.ripe.net"]]]
        with patch.object(whoislookup, "_bootstrap", return_value=asn):
            self.assertEqual(whoislookup.rdap_base_for_asn(5378), "https://rdap.db.ripe.net/")
            self.assertIsNone(whoislookup.rdap_base_for_asn(4000))


class TestPacing(unittest.TestCase):
    @patch.object(config, "WHOIS_HOST_INTERVAL_SECONDS", 0.2)
    def test_same_server_waits_other_does_not(self):
        state._whois_host_last.clear()
        start = time.time()
        whoislookup._wait_turn("whois.tcinet.ru")
        whoislookup._wait_turn("rdap.example")
        self.assertLess(time.time() - start, 0.1)
        whoislookup._wait_turn("whois.tcinet.ru")
        self.assertGreaterEqual(time.time() - start, 0.2)


class TestWhoisCommand(unittest.TestCase):
    def setUp(self):
        self.bot = MagicMock()
        state._whois_cache.clear()
        state._whois_pending = 0

    def _event(self, payload="", quote=None):
        ev = MagicMock()
        ev.msg.chat_id, ev.msg.id = 100, 200
        ev.msg.quote = quote
        ev.payload = payload
        return ev

    def test_targets(self):
        self.assertEqual(whoislookup.find_target("chatmail.uk", whole=True), ("host", "chatmail.uk"))
        self.assertEqual(whoislookup.find_target("https://delta.chat/en/", whole=True), ("host", "delta.chat"))
        self.assertEqual(whoislookup.find_target("AS5378", whole=True), ("asn", 5378))
        self.assertEqual(whoislookup.find_target(".рф", whole=True), ("host", "xn--p1ai"))
        self.assertEqual(whoislookup.find_target("see https://chatmail.uk/x", whole=False), ("host", "chatmail.uk"))
        self.assertIsNone(whoislookup.find_target("192.168.1.1", whole=True))

    def test_usage_and_bad_argument(self):
        with patch.object(dc_helpers, "_send") as send:
            whoislookup.whois_command(self.bot, 1, self._event())
            self.assertIn("Usage", send.call_args[0][3])
            whoislookup.whois_command(self.bot, 1, self._event("10.0.0.1"))
            self.assertIn("not a domain", send.call_args[0][3])

    def test_queued_answered_cached(self):
        with patch.object(dc_helpers, "_send") as send, patch.object(dc_helpers, "_react"), \
                patch.object(whoislookup, "lookup", return_value="whois text") as lookup, \
                patch.object(whoislookup, "_pool") as pool:
            pool.submit.side_effect = lambda fn, *a: fn(*a)
            whoislookup.whois_command(self.bot, 1, self._event("chatmail.uk"))
            whoislookup.whois_command(self.bot, 1, self._event("chatmail.uk"))
        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(send.call_args[0][3], "whois text")
        self.assertEqual(state._whois_pending, 0)

    def test_queue_limit(self):
        state._whois_pending = config.WHOIS_MAX_QUEUE
        with patch.object(dc_helpers, "_send") as send, patch.object(whoislookup, "_pool") as pool:
            whoislookup.whois_command(self.bot, 1, self._event("chatmail.uk"))
        self.assertIn("queue is full", send.call_args[0][3])
        pool.submit.assert_not_called()


class TestCaseInsensitiveCommands(unittest.TestCase):
    """bot.case_insensitive_parser around deltachat2's real parser."""

    def setUp(self):
        from deltachat2 import Bot, events as dc_events
        self.dc_events = dc_events
        self.fake = MagicMock()
        self.fake._hooks = {dc_events.NewMessage: {(lambda e: None, dc_events.NewMessage(command=c))
                                                   for c in ("/dns", "/dig", "/ip", "/help", "/whois")}}
        self.parse = bot.case_insensitive_parser(self.fake, lambda accid, ev: Bot._parse_command(self.fake, accid, ev))

    def _parse(self, text):
        from deltachat2 import NewMsgEvent
        from deltachat2.types import Message
        event = NewMsgEvent(command="", payload="", msg=Message({"text": text}))
        self.parse(1, event)
        return event

    def test_upper_and_mixed_case(self):
        for text, cmd, payload in (("/DNS chatmail.uk MX", "/dns", "chatmail.uk MX"), ("/Ip 1.1.1.1", "/ip", "1.1.1.1"),
                                   ("/HELP", "/help", ""), ("/WhoIs Example.ORG", "/whois", "Example.ORG"),
                                   ("/dig a.org", "/dig", "a.org")):
            ev = self._parse(text)
            self.assertEqual((ev.command, ev.payload), (cmd, payload), text)
            self.assertEqual(ev.msg.text, text)  # the message itself is not changed

    def test_unknown_command_untouched(self):
        ev = self._parse("/CONTACT123")
        self.assertEqual(ev.command, "/CONTACT123")


if __name__ == "__main__":
    unittest.main()
