"""Recover selected CNY data from memory without crossing source or time gates."""
from copy import deepcopy
from datetime import datetime, timedelta
from threading import RLock
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import veritas_breakout_runtime as BR
import veritas_direct_cny as D
import veritas_price_source as VPS
from test_veritas_direct_cny_snapshot import ASSET, NOW, UID, connection


def research_market():
    quote = {'asset': ASSET, 'price': 99., 'observed_at': NOW.isoformat(),
             'market_open': True, 'source_gate_pass': False,
             'source_names': {'primary': 'MOEX ISS CNYRUBF'},
             'contract': {'secid': ASSET}, 'paper_eligible': False}
    identity = VPS.identity(ASSET, quote)
    bar = dict(ts=(NOW-timedelta(minutes=5)).timestamp(), open=99., high=100.,
               low=98., close=99., volume=2, timeframe='5m', source_identity=identity)
    return dict(quote, structure_source_identity=identity,
                structure_bars_by_timeframe={'5m': [bar]})


class CnySourceRecoveryTests(unittest.TestCase):
    def setUp(self):
        with BR._cache_lock:
            self.previous = (BR._markets.copy(), BR._latest_rows.copy())
            BR._markets.clear()
            BR._latest_rows.clear()
        self.addCleanup(self.restore)
        self.c = connection()
        self.built = []
        self.ns = {'VTB': SimpleNamespace(connection=self.c), 'lock': RLock(),
                   'last_cycle': {'summary': []}}
        self.runtime = BR.BreakoutRuntime(self.ns, lambda *_: self.fail('unexpected entry'),
            context_builder=lambda raw, tf, clock: self.built.append((deepcopy(raw), tf)) or {})
        self.addCleanup(self.runtime.close)
        p = patch.object(D, '_verify_async', side_effect=AssertionError('network verification'))
        p.start(); self.addCleanup(p.stop)
        p = patch.object(D.TB.GrpcReader, 'call', side_effect=AssertionError('broker RPC'))
        p.start(); self.addCleanup(p.stop)
        p = patch.object(D, 'enabled', return_value=True)
        p.start(); self.addCleanup(p.stop)
        p = patch.object(BR.VPG, 'publish_quote')
        p.start(); self.addCleanup(p.stop)
        p = patch.object(D, '_record_state')
        p.start(); self.addCleanup(p.stop)
        # Model a pre-existing research cache before selecting the broker.
        # Once TBANK is selected, new MOEX publications are rejected outright.
        with patch.object(D, 'enabled', return_value=False):
            self.assertTrue(BR.publish_market(research_market()))

    def restore(self):
        with BR._cache_lock:
            BR._markets.clear(); BR._markets.update(self.previous[0])
            BR._latest_rows.clear(); BR._latest_rows.update(self.previous[1])

    def test_ready_cache_recovers_without_another_market_cycle_or_foreign_bars(self):
        old_position = {'asset': ASSET, 'payload': {'price_source_lock':
                        deepcopy(research_market()['structure_source_identity'])}}
        before = deepcopy(old_position)
        quote = D.quote(self.c, now=NOW)
        result = self.runtime.run_once(NOW, quotes={ASSET: quote})
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(result['direct_cny_recoveries'], 1)
        self.assertIsNone(result['direct_cny_recovery_reason'])
        self.assertEqual({tf for _, tf in self.built}, {'1m', '5m', '1h'})
        for raw, _ in self.built:
            self.assertEqual(raw['structure_source_identity']['key'], 'TBANK_GRPC:CNYRUBF')
            self.assertEqual(raw['structure_source_identity']['contract_id'], UID)
            self.assertEqual(raw['observed_at'], NOW.isoformat())
            for tf, bars in raw['structure_bars_by_timeframe'].items():
                self.assertEqual(len(bars), len(self.c.candles[(ASSET, tf)]['candles']))
                self.assertTrue(all(bar['source_identity']['contract_id'] == UID for bar in bars))
                self.assertTrue(all(bar['close'] == 12. for bar in bars))
        self.assertFalse(VPS.matches(old_position, quote))
        self.assertEqual(old_position, before)

    def test_empty_startup_cache_is_seeded_and_ready_cache_is_not_recopied_each_tick(self):
        BR._markets.clear()
        with patch.object(D, 'market_snapshot', wraps=D.market_snapshot) as read:
            self.runtime.run_once(NOW, quotes={ASSET: D.quote(self.c, now=NOW)})
            self.runtime.run_once(NOW+timedelta(seconds=5), quotes={})
        self.assertEqual(read.call_count, 1)
        self.assertEqual(len(self.built), 3)

    def test_stale_quote_recovery_does_not_copy_candle_history(self):
        class HistoryMustStayInCache(dict):
            def __deepcopy__(self, memo):
                raise AssertionError('stale quote copied history')
        self.c.quotes[ASSET]['observed_at'] = (NOW-timedelta(seconds=121)).isoformat()
        for snapshot in self.c.candles.values():
            snapshot['candles'] = [HistoryMustStayInCache(bar) for bar in snapshot['candles']]
        result = self.runtime.run_once(NOW, quotes={})
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(result['direct_cny_recovery_reason'], 'CNY_DIRECT_QUOTE_STALE')
        self.assertEqual(self.built, [])

    def test_unready_direct_data_does_not_make_even_fresh_moex_quotes_executable(self):
        cases = ('quote_stale', 'book_stale', 'session', 'quote_uid',
                 'history_uid', 'history_missing', 'history_stale', 'history_short')
        for case in cases:
            with self.subTest(case=case):
                self.c = connection()
                self.ns['VTB'].connection = self.c
                if case == 'quote_stale':
                    self.c.quotes[ASSET]['observed_at'] = (NOW-timedelta(seconds=121)).isoformat()
                elif case == 'book_stale':
                    self.c.books[ASSET]['orderbook_ts'] = (NOW-timedelta(seconds=121)).isoformat()
                elif case == 'session':
                    self.c.trading_states[ASSET]['api_trade_available_flag'] = False
                elif case == 'quote_uid':
                    self.c.quotes[ASSET]['instrument_uid'] = 'foreign'
                elif case == 'history_uid':
                    self.c.candles[(ASSET, '5m')]['instrument_uid'] = 'foreign'
                elif case == 'history_missing':
                    del self.c.candles[(ASSET, '1m')]
                elif case == 'history_stale':
                    for bar in self.c.candles[(ASSET, '5m')]['candles']:
                        bar['time'] = (datetime.fromisoformat(bar['time'])-timedelta(hours=1)).isoformat()
                else:
                    self.c.candles[(ASSET, '1h')]['candles'] = []
                before = deepcopy(BR._markets[ASSET])
                fallback = dict(research_market(), source_gate_pass=True, paper_eligible=True)
                result = self.runtime.run_once(NOW, quotes={ASSET: fallback})
                self.assertEqual(result['execution']['status'], 'NO_STRUCTURAL_EVENTS')
                self.assertTrue(result['direct_cny_recovery_reason'].startswith('CNY_'))
                self.assertEqual(result.get('direct_cny_recoveries', 0), 0)
                self.assertEqual(BR._markets[ASSET], before)
                self.assertEqual(self.built, [])
        # Data becoming valid is sufficient; no restart or slow fetch is needed.
        self.ns['VTB'].connection = connection()
        self.runtime.run_once(NOW, quotes={ASSET: D.quote(self.ns['VTB'].connection, now=NOW)})
        self.assertEqual(len(self.built), 3)

    def test_explicit_moex_configuration_is_not_changed(self):
        with patch.object(D, 'enabled', return_value=False), \
                patch.object(D, 'market_snapshot', side_effect=AssertionError('source override')):
            result = self.runtime.run_once(NOW, quotes={})
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(BR._markets[ASSET]['structure_source_identity']['key'], 'MOEX:CNYRUBF')

    def test_slow_fallback_published_during_recovery_stays_out_of_execution(self):
        recover = self.runtime._recover_direct_cny
        def interleaved(clock):
            ready = recover(clock)
            # An in-flight publisher selected MOEX; the execution pass still
            # requires TBANK when it takes its atomic descriptor snapshot.
            with patch.object(D, 'enabled', return_value=False):
                self.assertTrue(BR.publish_market(research_market()))
            return ready
        fallback = dict(research_market(), source_gate_pass=True, paper_eligible=True)
        with patch.object(self.runtime, '_recover_direct_cny', side_effect=interleaved):
            result = self.runtime.run_once(NOW, quotes={ASSET: fallback})
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(self.built, [])

    def test_recovered_event_drops_foreign_source_analytics_and_old_veto(self):
        self.runtime.run_once(NOW, quotes={ASSET: D.quote(self.c, now=NOW)})
        quote = D.quote(self.c, now=NOW)
        template = dict(research_market(), research_decision='LONG', confidence=.99,
                        sma18=99., hard_veto=True, trade_plan={'hard_invalidation': True})
        context = {'status': 'OK', 'event': {'direction': 'LONG', 'event_id': 'new-event'}}
        with patch.object(BR.TFP, 'prepare_row', side_effect=lambda row, **_: row), \
                patch.object(BR.TFP, 'final_plan', side_effect=lambda asset, direction, plan, **_: plan):
            row = self.runtime._row(BR._markets[ASSET], quote, '5m', context, template, NOW)
            same_source = self.runtime._row(BR._markets[ASSET], quote, '5m', context,
                dict(template, **quote), NOW)
        self.assertTrue(row['source_gate_pass'])
        self.assertTrue(row['paper_eligible'])
        self.assertEqual(VPS.identity(ASSET, row)['contract_id'], UID)
        self.assertIsNone(row['confidence'])
        self.assertEqual(same_source['confidence'], .99)
        self.assertEqual(same_source['sma18'], 99.)
        for key in ('sma18', 'hard_veto', 'hard_invalidation'):
            self.assertNotIn(key, row)
            self.assertNotIn(key, row['trade_plan'])

    def test_busy_subtype_is_preserved_in_runtime_diagnostics(self):
        self.runtime.entry_pass = lambda *_: {'status': 'BUSY', 'reason': 'LOCAL_PAPER_BOOK_BUSY'}
        row = dict(D.quote(self.c, now=NOW), horizon='1m', timeframe_entry_context={})
        with patch.object(self.runtime, '_row', return_value=row):
            result = self.runtime.run_once(NOW, quotes={ASSET: row})
        self.assertEqual(result['execution_status'], 'BUSY')
        self.assertEqual(result['execution_reason'], 'LOCAL_PAPER_BOOK_BUSY')

    def test_slow_fallback_completion_does_not_overwrite_recovered_current_source(self):
        self.runtime.entry_pass = lambda *_: {'status': 'OK'}
        quote = D.quote(self.c, now=NOW)
        row = dict(quote, horizon='1m', market_observed_at=quote['observed_at'],
                   timeframe_entry_context={})
        with patch.object(self.runtime, '_row', return_value=row):
            self.runtime.run_once(NOW, quotes={ASSET: quote})
        old = dict(research_market(), horizon='1m',
                   market_observed_at=(NOW-timedelta(seconds=20)).isoformat())
        self.assertEqual(VPS.identity(ASSET, BR.publish_summary([old])[0])['contract_id'], UID)
        # A disabled source or a newer source/contract publication cannot be
        # overridden by a remembered fast row from an old broker context.
        with patch.object(D, 'enabled', return_value=False):
            self.assertEqual(BR.publish_summary([old])[0], old)
        replacement = deepcopy(BR._markets[ASSET])
        replacement['structure_source_identity']['contract_id'] = 'another-uid'
        BR._markets[ASSET] = replacement
        self.assertEqual(BR.publish_summary([old])[0], old)


if __name__ == '__main__':
    unittest.main()
