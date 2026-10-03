"""Market fanout must not leave the last asset waiting behind slow feeds."""
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

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


if __name__ == '__main__':
    unittest.main()
