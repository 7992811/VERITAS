"""Market fanout must not leave the last asset waiting behind slow feeds."""
import threading
import time
import unittest
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from veritas_market_runtime import install_market_runtime_guard, normalize_moex_index_session


def runtime(fetch, assets=None, cache=None):
    ns = {
        '_moex_block': lambda *_: [],
        '_moex_parse_dt': lambda *_: None,
        '_fetch_asset_bundle': fetch,
        'ASSETS': assets or {a: (a, None) for a in
                            ('BTC', 'ETH', 'NQ', 'BRENT', 'GOLD', 'MOEX', 'CNYRUBF')},
        '_v90r62_bundle_cache': cache or {},
        '_v90r62_bundle_cache_lock': threading.Lock(),
    }
    install_market_runtime_guard(ns)
    return ns['prefetch_market_bundles']


class MarketPrefetchTests(unittest.TestCase):
    def test_eth_starts_even_while_all_six_other_feeds_are_blocked(self):
        release = threading.Event()
        eth_started = threading.Event()
        started = set()
        lock = threading.Lock()

        def fetch(symbol, asset, product):
            with lock:
                started.add(asset)
            if asset == 'ETH':
                eth_started.set()
            else:
                release.wait(5)
            return {'asset': asset, 'raw': {'price': 100}, 'error': None}

        with ThreadPoolExecutor(max_workers=1) as caller:
            pending = caller.submit(runtime(fetch))
            try:
                self.assertTrue(eth_started.wait(2), 'ETH was queued behind another feed')
                with lock:
                    self.assertEqual(len(started), 7)
            finally:
                release.set()
            bundles, stats = pending.result(timeout=5)
        self.assertEqual(set(bundles), started)
        self.assertEqual(stats['timed_out_assets'], [])

    def test_timeout_cache_keeps_original_market_timestamp(self):
        observed = '2026-10-03T10:00:00+00:00'
        bundle = {'asset': 'ETH', 'raw': {'price': 100, 'observed_at': observed}, 'error': None}
        cache = {'ETH': {'at': time.time() - 40, 'bundle': bundle}}
        fetch = lambda *args: bundle
        prefetch = runtime(fetch, {'ETH': ('ETH', None)}, cache)
        # Force the deadline branch, independent of worker scheduling or clocks.
        with patch('veritas_market_runtime.wait', side_effect=lambda futures, **kw: (set(), set(futures))):
            bundles, stats = prefetch()
        self.assertEqual(bundles['ETH']['raw']['observed_at'], observed)
        self.assertTrue(bundles['ETH']['reused_market_bundle_timeout_fallback'])
        self.assertGreaterEqual(bundles['ETH']['reused_market_bundle_age_seconds'], 40)
        self.assertEqual(stats['timeout_cache_fallback_assets'], ['ETH'])
        self.assertNotIn('reused_market_bundle', bundle)

    def test_expired_cache_cannot_hide_timeout(self):
        bundle = {'raw': {'price': 100}, 'error': None}
        cache = {'ETH': {'at': time.time() - 601, 'bundle': bundle}}
        prefetch = runtime(lambda *args: bundle, {'ETH': ('ETH', None)}, cache)
        with patch('veritas_market_runtime.wait', side_effect=lambda futures, **kw: (set(), set(futures))):
            bundles, stats = prefetch()
        self.assertIsNone(bundles['ETH']['raw'])
        self.assertEqual(bundles['ETH']['error'], 'MARKET_PREFETCH_TIMEOUT_25S')
        self.assertEqual(stats['timed_out_assets'], ['ETH'])


class MoexExtendedSessionTests(unittest.TestCase):
    def bundle(self, observed='2026-10-09T19:22:35+00:00', source='MOEX ISS IMOEX'):
        return {'asset':'MOEX','raw':{
            'asset':'MOEX','price':2370.6,'source_names':{'primary':source},
            'market_observed_at':observed,'market_open':False,
            'source_gate_pass':False,'data_latency_class':'DELAYED_RESEARCH',
            'direct_sources':1,
        },'error':None}

    def test_fresh_official_imoex_quote_repairs_old_evening_session_flag(self):
        now=datetime(2026,10,9,19,22,44,tzinfo=timezone.utc)  # 22:22:44 Moscow
        out=normalize_moex_index_session(self.bundle(),now)
        raw=out['raw']
        self.assertTrue(raw['market_open'])
        self.assertTrue(raw['source_gate_pass'])
        self.assertEqual(raw['data_latency_class'],'LIVE_EXCHANGE')
        self.assertTrue(raw['moex_session_repaired'])
        self.assertEqual(raw['moex_session_policy'],'IMOEX_EXTENDED_2026_09_26')

    def test_epoch_exchange_time_is_accepted_but_not_retrieval_time(self):
        now=datetime(2026,10,9,19,22,44,tzinfo=timezone.utc)
        b=self.bundle(observed=now.timestamp()-9)
        self.assertTrue(normalize_moex_index_session(b,now)['raw']['market_open'])
        stale=self.bundle(observed=now.timestamp()-121)
        self.assertFalse(normalize_moex_index_session(stale,now)['raw']['market_open'])

    def test_repair_is_fail_closed_outside_verified_scope(self):
        cases=[
            (datetime(2026,10,9,3,59,tzinfo=timezone.utc), self.bundle()),  # before 07:00 MSK
            (datetime(2026,10,9,20,50,tzinfo=timezone.utc), self.bundle(observed='2026-10-09T20:49:55+00:00')),
            (datetime(2026,10,10,12,0,tzinfo=timezone.utc), self.bundle(observed='2026-10-10T11:59:55+00:00')),
            (datetime(2026,9,25,19,0,tzinfo=timezone.utc), self.bundle(observed='2026-09-25T18:59:55+00:00')),
            (datetime(2026,10,9,19,22,44,tzinfo=timezone.utc), self.bundle(source='Yahoo Finance')),
        ]
        for now,bundle in cases:
            with self.subTest(now=now,source=bundle['raw']['source_names']['primary']):
                raw=normalize_moex_index_session(bundle,now)['raw']
                self.assertFalse(raw['market_open'])
                self.assertFalse(raw['source_gate_pass'])
                self.assertNotIn('moex_session_repaired',raw)

    def test_missing_direct_source_or_stale_snapshot_is_never_promoted(self):
        now=datetime(2026,10,9,19,22,44,tzinfo=timezone.utc)
        for change in ({'direct_sources':0},{'snapshot_stale':True}):
            b=self.bundle();b['raw'].update(change)
            raw=normalize_moex_index_session(b,now)['raw']
            self.assertFalse(raw['market_open'])
            self.assertFalse(raw['source_gate_pass'])



if __name__ == '__main__':
    unittest.main()
