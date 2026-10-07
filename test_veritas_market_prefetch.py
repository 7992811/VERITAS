"""Market fanout must not leave the last asset waiting behind slow feeds."""
import gc
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import weakref

from veritas_market_runtime import install_market_runtime_guard


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


class WeakRows(dict):
    __slots__ = ('__weakref__',)


class CompletedAssetProviderCacheTests(unittest.TestCase):
    def namespace(self, rss=400.0, soft_limit=320):
        events, trims = [], []
        market_cache = {}
        ns = {
            '_moex_block': lambda *_: [],
            '_moex_parse_dt': lambda *_: None,
            '_fetch_asset_bundle': lambda *_: {},
            'ASSETS': {},
            'market_cache': market_cache,
            'market_cache_lock': threading.Lock(),
            'MEMORY_SOFT_LIMIT_MB': soft_limit,
            'V90_MEMORY_PROTECT_MB': 340.0,
            'rss_mb': lambda: rss,
            'emit': lambda event, **kw: events.append((event, kw)),
            '_v90_trim_memory': lambda phase='unknown', force=False,
                preserve_active_cycle=False: trims.append((phase, force, preserve_active_cycle)) or {
                    'phase': phase, 'caches_cleared': 0},
        }
        install_market_runtime_guard(ns)
        return ns, market_cache, events, trims

    def test_pressure_releases_only_provider_rows_after_completed_asset(self):
        ns, cache, events, trims = self.namespace()
        rows = WeakRows(asset='NQ', bars=[float(i) for i in range(4096)])
        retained = weakref.ref(rows)
        cache[('NQ=F', '30d', '1h', False)] = {'rows': rows}
        active_decision = {'asset': 'NQ', 'source': 'PROFINANCE:NASD100_FUT'}
        ns['active_decision'] = active_decision
        del rows

        result = ns['_v90_trim_memory']('asset_NQ', force=False)
        gc.collect()

        self.assertIsNone(retained())
        self.assertEqual(cache, {})
        self.assertIs(ns['active_decision'], active_decision)
        self.assertEqual(trims, [('asset_NQ', False, False)])
        self.assertEqual(result['completed_asset_provider_cache_released'], 1)
        self.assertEqual(events, [('completed_asset_provider_cache_released', {
            'asset': 'NQ', 'entries': 1, 'rss_mb': 400.0})])

    def test_other_phases_low_pressure_and_larger_runtimes_keep_provider_rows(self):
        for phase, rss, soft_limit in (
                ('horizon_NQ_1m', 400.0, 320),
                ('asset_NQ', 300.0, 320),
                ('asset_NQ', 400.0, 400)):
            with self.subTest(phase=phase, rss=rss, soft_limit=soft_limit):
                ns, cache, events, _ = self.namespace(rss, soft_limit)
                cache['rows'] = [1, 2, 3]
                result = ns['_v90_trim_memory'](phase)
                self.assertEqual(cache, {'rows': [1, 2, 3]})
                self.assertNotIn('completed_asset_provider_cache_released', result)
                self.assertEqual(events, [])


if __name__ == '__main__':
    unittest.main()
