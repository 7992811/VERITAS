"""Successful-but-stale chart responses cannot pin a session indefinitely."""
import unittest

import httpx
import veritas_profinance_history as H
from test_veritas_profinance_history import Clock, REFRESH, history_text


class HistorySessionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.calls = []
        self.sessions = 0
        self.failure = None
        self.cache = H.HistoryCache(self.fetch, lambda: self.clock.now, lambda: self.clock.elapsed)

    def fetch(self, url, params, remaining):
        self.calls.append((url.rsplit('/', 1)[-1], params.copy()))
        self.clock.advance(.1)
        if self.failure:
            raise self.failure
        if url.endswith('refresh'):
            self.sessions += 1
            return REFRESH.format(ticker='NASD100_FUT').replace('publicSession', f'session{self.sessions}')
        # The first session returns valid OHLC but an obsolete window. This is
        # a deterministic provider fault model, not a claim about a live SID.
        end = self.clock.now - 1200 if self.sessions == 1 else self.clock.now
        return history_text(params['tt'], end)

    def test_old_successful_window_is_retired_then_recovers_at_next_bounded_poll(self):
        first = self.cache.fetch_bundle('NQ', ('1m', '5m'))
        original_end = first['status_by_timeframe']['1m']['last_closed_at']
        self.assertEqual(first['status_by_timeframe']['1m']['status'], 'STALE')
        self.assertEqual(first['status_by_timeframe']['1m']['reason'], 'STALE_HISTORY_SESSION_RETIRED')
        self.assertEqual(self.sessions, 1)
        self.clock.advance(6)
        second = self.cache.fetch_bundle('NQ', ('1m', '5m'))
        self.assertEqual(self.sessions, 2)
        self.assertEqual(second['status_by_timeframe']['1m']['status'], 'READY')
        self.assertGreater(second['status_by_timeframe']['1m']['last_closed_at'], original_end)
        self.assertEqual(second['source_key'], first['source_key'])
        self.assertEqual([kind for kind, _ in self.calls].count('refresh'), 2)

    def test_failed_recovery_keeps_original_candle_and_fetch_times(self):
        first = self.cache.fetch_bundle('NQ', ('1m',))
        self.clock.advance(6)
        self.failure = TimeoutError('bounded public provider request failed')
        later = self.cache.fetch_bundle('NQ', ('1m',))
        for key in ('fetched_at', 'last_closed_at'):
            self.assertEqual(later['status_by_timeframe']['1m'][key], first['status_by_timeframe']['1m'][key])
        self.assertEqual(later['status_by_timeframe']['1m']['status'], 'STALE')
        self.assertGreater(later['status_by_timeframe']['1m']['age_seconds'],
                           first['status_by_timeframe']['1m']['age_seconds'])

    def test_recovery_never_overrides_access_denial_backoff(self):
        self.cache.fetch_bundle('NQ', ('1m',))
        self.clock.advance(6)
        request = httpx.Request('GET', H.BASE + 'refresh')
        response = httpx.Response(403, request=request)
        self.failure = httpx.HTTPStatusError('Forbidden', request=request, response=response)
        self.cache.fetch_bundle('NQ', ('1m', '5m'))
        count = len(self.calls)
        self.clock.advance(30)
        result = self.cache.fetch_bundle('NQ', ('1m', '5m'))
        self.assertEqual(len(self.calls), count)
        self.assertTrue(all(s['reason'] == 'ACCESS_DENIED_BACKOFF'
                            for s in result['status_by_timeframe'].values()))


if __name__ == '__main__':
    unittest.main()
