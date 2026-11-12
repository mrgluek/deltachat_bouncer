"""Regression tests for bounded remote input and network work."""
import asyncio
import importlib
import importlib.util
import ipaddress
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from netutil import NoRedirect, is_public_ip
from ttl_cache import TTLCache


class TestAudit(unittest.TestCase):
    def setUp(self):
        mocks = {}
        if 'deltachat2' not in sys.modules and importlib.util.find_spec('deltachat2') is None:
            mocks['deltachat2'] = MagicMock()
        if 'deltabot_cli' not in sys.modules and importlib.util.find_spec('deltabot_cli') is None:
            cli = MagicMock()
            cli.on.side_effect = lambda *a, **kw: lambda f: f
            mocks['deltabot_cli'] = MagicMock(BotCli=MagicMock(return_value=cli))
        with patch.dict(sys.modules, mocks):
            self.database = importlib.import_module('database')
            self.ap = importlib.import_module('activitypub')
            self.dns = importlib.import_module('dnscheck')
            self.mod = importlib.import_module('moderation')
            self.whois = importlib.import_module('whoislookup')
            self.ip = importlib.import_module('iplookup')
            self.helpers = importlib.import_module('dc_helpers')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database.close_db()
        self.patch(self.database, 'DB_PATH', os.path.join(self.tmp.name, 'test.db'))
        self.database.init_db()
        self.addCleanup(self.database.close_db)

    def patch(self, obj, name, *args, **kwargs):
        p = patch.object(obj, name, *args, **kwargs)
        value = p.start()
        self.addCleanup(p.stop)
        return value

    def test_public_addresses_agree(self):
        blocked = ['10.0.0.1', '127.0.0.1', '169.254.1.1', '224.0.0.1',
                   '240.0.0.1', '0.0.0.0', '100.64.0.1', '::', '::1',
                   'fe80::1', 'fc00::1', 'ff02::1', '2001::808:808',
                   '::ffff:127.0.0.1', '::ffff:100.64.0.1',
                   '64:ff9b::7f00:1', '64:ff9b::a00:1',
                   '64:ff9b:1:7f00:0:100::', '64:ff9b:1:a00:0:100::',
                   '2002:7f00:1::', '2002:6440:1::']
        public = ['8.8.8.8', '2606:4700:4700::1111', '::ffff:8.8.8.8',
                  '64:ff9b::808:808', '64:ff9b:1:808:8:800::', '2002:808:808::']
        for text in blocked + public:
            with self.subTest(address=text):
                expected = text in public
                self.assertEqual(is_public_ip(text), expected)
                self.assertEqual(self.ap._is_public_address(text), expected)
                self.assertEqual(bool(self.dns._public_ip(text)), expected)
                self.assertEqual(self.ip._is_public(ipaddress.ip_address(text)), expected)

    def test_public_resolver_rejects_transition_to_private(self):
        answer = [(10, 1, 6, '', ('64:ff9b::7f00:1', 443, 0, 0))]
        with patch.object(self.ap.socket, 'getaddrinfo', return_value=answer):
            with self.assertRaises(OSError):
                asyncio.run(self.ap.PublicResolver().resolve('remote.example', 443))

    def test_actor_and_status_eviction(self):
        for name in ('_remote_actor_cache', '_last_fetch_status'):
            cache = TTLCache(max_entries=2)
            self.patch(self.ap, name, cache)
            with patch('ttl_cache.time.time', return_value=100):
                cache['first'] = (110, {})
                cache['second'] = (120, {})
                cache['third'] = (130, {})
                self.assertNotIn('first', cache)
                self.assertEqual(len(cache), 2)
            with patch('ttl_cache.time.time', return_value=125):
                self.assertNotIn('second', cache)
                self.assertEqual(len(cache), 1)

    def test_replay_markers_survive_skew_window_and_overflow_evicts_oldest(self):
        cache = TTLCache(max_entries=2)
        self.patch(self.ap, '_recent_signatures', cache)
        with patch.object(self.ap.time, 'time', return_value=100):
            self.assertTrue(self.ap.check_and_record_signature_replay('first'))
            self.assertTrue(self.ap.check_and_record_signature_replay('second'))
            # Full cache never blocks new traffic: the oldest marker is evicted.
            self.assertTrue(self.ap.check_and_record_signature_replay('third'))
            self.assertEqual(len(cache), 2)
        with patch.object(self.ap.time, 'time', return_value=401):
            self.assertFalse(self.ap.check_and_record_signature_replay('third'))
        with patch.object(self.ap.time, 'time', return_value=701):
            self.assertTrue(self.ap.check_and_record_signature_replay('third'))

    def test_redirects_never_reach_target(self):
        class RedirectResponse:
            def info(self):
                return {'Location': 'http://127.0.0.1/private'}
            def read(self):
                return b''
            def close(self):
                pass
        for module, func in ((self.whois, '_get_json'), (self.ip, '_http_get_json')):
            self.patch(module, 'urlopen', urllib.request.build_opener(NoRedirect()).open)
            if module is self.whois:
                self.patch(module, '_wait_turn')
            for code in (301, 302, 307, 308):
                with self.subTest(module=module.__name__, code=code):
                    with patch.object(urllib.request.HTTPSHandler, 'https_open') as transport:
                        response = RedirectResponse()
                        response.code = response.status = code
                        response.msg = 'Redirect'
                        transport.return_value = response
                        self.assertEqual(getattr(module, func)('https://public.example/data'), (None, code))
                        self.assertEqual(transport.call_count, 1)

    def test_join_retry_checks_last_of_large_group_with_pacing(self):
        fp = 'B' * 40
        self.database.add_banned_fingerprint(fp)
        self.patch(self.mod, '_ban_fingerprint_cache', {})
        self.patch(self.mod, '_ban_scan_offset', {})
        self.patch(self.mod, '_ban_retry_pending', set())
        bot = MagicMock()
        members = list(range(10, 140))
        bot.rpc.get_basic_chat_info.return_value = {'chat_type': 'Group'}
        bot.rpc.get_chat_contacts.return_value = members
        self.patch(self.helpers, '_is_dc_admin', return_value=False)
        resolve = self.patch(self.mod, '_fingerprints', side_effect=lambda b, a, cid: [fp] if cid == 139 else ['A' * 40])
        timer = self.patch(self.mod.threading, 'Timer')
        sleep = self.patch(self.mod.time, 'sleep')
        self.mod.enforce_ban_on_join(bot, 1, 100, None)
        timer.call_args.args[1]()
        bot.rpc.remove_contact_from_chat.assert_called_once_with(1, 100, 139)
        self.assertEqual(resolve.call_count, 130)
        self.assertEqual(sleep.call_count, 2)

    def test_spf_lookup_and_depth_limits(self):
        calls = []
        def query(res, host, rtype):
            calls.append((host, rtype))
            return self.dns.Res('ok', ['v=spf1 include:next' + host + ' -all'])
        self.patch(self.dns, 'query', side_effect=query)
        _, notes = self.dns._spf_networks('v=spf1 ' + ' '.join('a:h%d.example' % i for i in range(100)), 'example.org', {}, None)
        self.assertEqual(len(calls), 10)
        self.assertIn('SPF lookup limit reached', notes)
        calls.clear()
        _, notes = self.dns._spf_networks('v=spf1 include:child.example', 'example.org', {}, None)
        self.assertEqual(len(calls), 5)
        self.assertIn('SPF depth limit reached', notes)

    def test_dns_report_lookup_budget_and_ptr_caps(self):
        budget = self.dns.ReportBudget()
        underlying = MagicMock(lifetime=6)
        underlying.resolve.side_effect = self.dns.dns.resolver.NoAnswer
        resolver = self.dns.BudgetResolver(underlying, budget)
        for _ in range(200):
            self.dns.query(resolver, 'example.org', 'A')
        self.assertEqual(underlying.resolve.call_count, self.dns.REPORT_LOOKUPS)
        self.assertTrue(budget.reached)
        budget = self.dns.ReportBudget()
        resolver = self.dns.BudgetResolver(underlying, budget)
        query = self.patch(self.dns, 'query', return_value=self.dns.Res('ok', ['ptr%d.example.' % i for i in range(100)]))
        result = self.dns._reverse_many(['8.8.8.%d' % i for i in range(30)], resolver)
        self.assertEqual(query.call_count, 5)
        self.assertEqual(len(result), 5)
        self.assertTrue(all(len(names) == 5 for names in result.values()))
        self.assertTrue(budget.reached)

    def test_report_wall_clock_budget(self):
        release = threading.Event()
        finished = threading.Event()
        def slow(*args):
            release.wait(2)
            finished.set()
            return 'late report'
        self.patch(self.dns, '_build_report', side_effect=slow)
        self.patch(self.dns, 'REPORT_SECONDS', 0.02)
        try:
            started = time.monotonic()
            result = self.dns.build_report('example.org', MagicMock(), 'test', MagicMock())
            self.assertLess(time.monotonic() - started, 1)
            self.assertIn('time limit reached', result)
        finally:
            release.set()
            self.assertTrue(finished.wait(1))

    def test_mail_limits_are_visible(self):
        r = {key: self.dns.Res('nodata') for key in
             ['MX', 'TXT', 'DMARC', 'STS', 'TLSRPT'] +
             ['DKIM:' + sel for sel in self.dns.DKIM_SELECTORS] +
             [svc for svc, _ in self.dns.SRV_SERVICES]}
        hosts = [SimpleNamespace(preference=i, exchange=SimpleNamespace(
            to_text=lambda i=i: 'mx%d.example.' % i)) for i in range(100)]
        r['MX'] = self.dns.Res('ok', rdata=hosts)
        r['TXT'] = self.dns.Res('ok', ['v=spf1 ' + ' '.join('a:h%d.example' % i for i in range(100))])
        query = self.patch(self.dns, 'query', return_value=self.dns.Res('nodata'))
        lines, _ = self.dns._mail_section('example.org', r, None)
        self.assertEqual(query.call_count, 30)  # ten MX hosts (A/AAAA), ten SPF lookups
        self.assertIn('MX hosts: limit reached', '\n'.join(lines))
        self.assertIn('SPF lookup limit reached', '\n'.join(lines))

    def test_expired_report_does_no_more_dns(self):
        budget = self.dns.ReportBudget()
        budget.deadline = time.monotonic() - 1
        transport = MagicMock()
        result = self.dns.query(self.dns.BudgetResolver(transport, budget), 'example.org', 'A')
        self.assertEqual(result.status, 'timeout')
        transport.resolve.assert_not_called()
        self.assertTrue(budget.reached)

    def test_resolver_none_lifetime_uses_remaining_budget(self):
        budget = self.dns.ReportBudget()
        resolver = MagicMock(lifetime=6)
        self.dns.BudgetResolver(resolver, budget).resolve('example.org', 'SOA', lifetime=None)
        lifetime = resolver.resolve.call_args.kwargs['lifetime']
        self.assertGreater(lifetime, 0)
        self.assertLessEqual(lifetime, 6)

    def test_spf_include_and_redirect_share_lookup_budget(self):
        calls = []
        def query(res, host, rtype):
            calls.append((host, rtype))
            if rtype == 'TXT':
                return self.dns.Res('ok', ['v=spf1 ' + ' '.join('a:h%d.example' % i for i in range(100))])
            return self.dns.Res('nodata')
        self.patch(self.dns, 'query', side_effect=query)
        _, notes = self.dns._spf_networks('v=spf1 include:child.example redirect=other.example',
                                         'example.org', {}, None)
        self.assertEqual(len(calls), 10)
        self.assertIn('SPF lookup limit reached', notes)
