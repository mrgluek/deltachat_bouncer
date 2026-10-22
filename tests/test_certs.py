import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

# Mock packages if not installed (certs imports config, which needs deltabot_cli)
try:
    import deltabot_cli  # noqa: F401
except ImportError:
    sys.modules['deltabot_cli'] = MagicMock()

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import certs

D = "chatmail.example"
LE = {"friendly_name": "Let's Encrypt", "name": "C=US, O=Let's Encrypt, CN=R13"}
# Cloudflare Universal SSL as Cert Spotter shows it for a real zone
CF = {"friendly_name": "SSL.com", "name": "C=US, O=SSL Corporation, CN=Cloudflare TLS Issuing ECC CA 4"}


def cert(names, issuer=LE, days_left=60, issued="2026-10-20T00:00:00Z"):
    until = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + days_left * 86400))
    return {"dns_names": names, "issuer": issuer, "not_before": issued, "not_after": until}


def data(*cs, subdomains=True, more=False):
    return {"certs": list(cs), "subdomains": subdomains, "more": more}


class TestReportLines(unittest.TestCase):
    def test_relay_with_cloudflare_universal_ssl(self):
        caa = {"issue": ["letsencrypt.org", "ssl.com", "pki.goog"], "issuewild": ["letsencrypt.org", "ssl.com"]}
        d = data(cert([D, f"mta-sts.{D}", f"www.{D}"], days_left=88),
                 cert([f"*.{D}", D], issuer=CF, issued="2026-09-14T00:00:00Z"))
        text = "\n".join(certs.report_lines(D, d, caa))
        self.assertIn("2 certificates · latest issued 2026-10-20", text)
        self.assertIn("Issuers: Let's Encrypt ×1, SSL.com via Cloudflare ×1", text)
        self.assertIn(f"✅ {D} is covered until", text)
        self.assertIn("☁️ Cloudflare Universal SSL", text)
        self.assertNotIn("not allowed by the current CAA", text)  # Cloudflare put ssl.com in CAA
        self.assertIn(f"Names in certificates: mta-sts.{D}, www.{D}", text)
        self.assertTrue(certs.chatmail_covered(D, d))

    def test_issuer_not_in_caa(self):
        d = data(cert([f"*.{D}"], issuer={"friendly_name": "Google Trust Services"}))
        text = "\n".join(certs.report_lines(D, d, {"issue": ["letsencrypt.org"], "issuewild": [";"]}))
        self.assertIn("⚠️ Google Trust Services is not allowed by the current CAA", text)
        # Unknown CA or no CAA at all: no verdict
        d = data(cert([D], issuer={"friendly_name": "Some New CA"}))
        self.assertNotIn("not allowed", "\n".join(certs.report_lines(D, d, {"issue": ["letsencrypt.org"]})))
        d = data(cert([D], issuer={"friendly_name": "Google Trust Services"}))
        self.assertNotIn("not allowed", "\n".join(certs.report_lines(D, d, {})))

    def test_expiring_soon(self):
        text = "\n".join(certs.report_lines(D, data(cert([D], days_left=3)), {}))
        self.assertIn("⚠️", text)
        self.assertIn("renewal may be failing", text)

    def test_wildcard_coverage(self):
        c = cert([f"*.{D}"])
        self.assertTrue(certs._covers(c, f"www.{D}"))
        self.assertFalse(certs._covers(c, D))
        self.assertFalse(certs._covers(c, f"a.b.{D}"))
        self.assertFalse(certs.chatmail_covered(D, data(c)))

    def test_single_hostname_fallback_and_errors(self):
        text = "\n".join(certs.report_lines(D, data(cert([D]), subdomains=False, more=True), {}))
        self.assertIn("1+ certificate", text)
        self.assertIn("subdomains skipped", text)
        self.assertNotIn("Names in certificates", text)
        self.assertIn("ℹ️ Cert Spotter", "\n".join(certs.report_lines(D, {"error": "Cert Spotter did not answer"}, {})))
        self.assertIn("No valid certificate", "\n".join(certs.report_lines(D, data(), {})))
        self.assertIsNone(certs.chatmail_covered(D, {"error": "x"}))


class TestLookup(unittest.TestCase):
    def setUp(self):
        certs._cache.clear()
        certs._sent["full"].clear()
        certs._sent["single"].clear()
        certs._paused_until = 0.0

    def test_budget_falls_back_to_single_hostname_and_caches(self):
        calls = []

        def fetch(domain, subdomains):
            calls.append(subdomains)
            return [cert([domain])], 200, False

        with patch.object(certs, "_fetch", side_effect=fetch):
            for i in range(certs.FULL_PER_HOUR + 1):
                r = certs.lookup(f"d{i}.example")
            self.assertFalse(r["subdomains"])  # the 11th domain: full-domain budget spent
            self.assertEqual(calls.count(True), certs.FULL_PER_HOUR)
            certs.lookup("d0.example")  # cached: no new query
        self.assertEqual(len(calls), certs.FULL_PER_HOUR + 1)

    def test_rate_limited(self):
        certs._paused_until = time.time() + 60
        with patch.object(certs, "_fetch") as fetch:
            self.assertIn("error", certs.lookup("x.example"))
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
