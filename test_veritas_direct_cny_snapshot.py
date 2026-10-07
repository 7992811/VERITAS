"""CNY fast quotes stay cheap; diagnostic readiness validates current history."""
from datetime import datetime, timedelta, timezone
from threading import RLock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import veritas_direct_cny as D
import veritas_tbank as TB


NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
ASSET = 'CNYRUBF'
UID = 'exact-cny-contract'


def connection():
    at = NOW.isoformat()
    candles = {}
    for tf, count, seconds in (('1m', 30, 60), ('5m', 40, 300), ('1h', 120, 3600)):
        candles[(ASSET, tf)] = {
            'instrument_uid': UID, 'status': 'OK',
            'candles': [
                {'time': (NOW - timedelta(seconds=(count - i) * seconds)).isoformat(),
                 'open': 12.0, 'high': 12.01, 'low': 11.99, 'close': 12.0, 'volume_lots': 2}
                for i in range(count)
            ],
        }
    return SimpleNamespace(
        lock=RLock(),
        instruments={ASSET: {
            'uid': UID, 'ticker': ASSET, 'real_exchange': 'REAL_EXCHANGE_MOEX',
            'min_price_increment': .001, 'min_price_increment_amount': 1.,
            'basic_asset_size': 1000.,
        }},
        quotes={ASSET: {'instrument_uid': UID, 'price': 12., 'observed_at': at}},
        books={ASSET: {
            'instrument_uid': UID, 'orderbook_ts': at, 'is_consistent': True,
            'bids': [{'price': 11.999, 'quantity': 10}],
            'asks': [{'price': 12.001, 'quantity': 10}],
        }},
        trading_states={ASSET: {
            'instrument_uid': UID, 'checked_at': at,
            'trading_status': 'SECURITY_TRADING_STATUS_NORMAL_TRADING',
            'api_trade_available_flag': True,
        }},
        candles=candles,
    )


class HistoryCopyForbidden(dict):
    def __deepcopy__(self, memo):
        raise AssertionError('quote-only snapshot copied a candle row')


class DirectCnySnapshotTests(unittest.TestCase):
    def test_quote_does_not_deepcopy_candle_rows_and_keeps_execution_contract(self):
        c = connection()
        for history in c.candles.values():
            history['candles'] = [HistoryCopyForbidden(row) for row in history['candles']]
        q = D.quote(c, now=NOW)
        self.assertEqual(q['asset'], ASSET)
        self.assertEqual(q['price'], 12.)
        self.assertEqual((q['best_bid'], q['best_ask']), (11.999, 12.001))
        self.assertEqual(q['observed_at'], NOW.isoformat())
        self.assertEqual(q['book_observed_at'], NOW.isoformat())
        self.assertEqual(q['source_names']['primary'], 'TBANK_GRPC CNYRUBF')
        self.assertEqual(q['contract'], {
            'secid': ASSET, 'instrument_uid': UID, 'lot': 1000,
            'price_tick': .001, 'tick_value_rub': 1., 'price_unit': 'RUB_PER_CNY',
            'broker_price_unit': 'POINTS', 'normalization_factor': 1.,
        })
        self.assertTrue(q['source_gate_pass'])
        self.assertTrue(q['paper_eligible'])
        self.assertFalse(q['production_eligible'])
        self.assertFalse(q['orders_enabled'])
        self.assertFalse(q['paper_is_live_fill_evidence'])
        for key in ('candles', 'closes', 'intraday_1m', 'intraday_5m', 'canonical_hourly_bars'):
            self.assertNotIn(key, q)

    def test_quote_only_snapshot_keeps_nested_market_data_isolated(self):
        c = connection()
        snap = D._snapshot(c, include_history=False)
        self.assertNotIn('candles', snap)
        c.quotes[ASSET]['price'] = 13.
        c.books[ASSET]['bids'][0]['price'] = 12.999
        self.assertEqual(snap['quote']['price'], 12.)
        self.assertEqual(snap['book']['bids'][0]['price'], 11.999)
        snap['book']['asks'][0]['price'] = 1.
        snap['instrument']['uid'] = 'changed'
        snap['trading']['api_trade_available_flag'] = False
        self.assertEqual(c.books[ASSET]['asks'][0]['price'], 12.001)
        self.assertEqual(c.instruments[ASSET]['uid'], UID)
        self.assertTrue(c.trading_states[ASSET]['api_trade_available_flag'])

    def test_default_snapshot_retains_valid_full_history_and_deep_isolation(self):
        c = connection()
        snap = D._snapshot(c)
        full = D.validate_snapshot(snap, now=NOW)
        self.assertEqual(set(snap['candles']), {'1m', '5m', '1h'})
        self.assertEqual(len(full['intraday_1m']), 30)
        self.assertEqual(len(full['intraday_5m']), 40)
        self.assertEqual(len(full['canonical_hourly_bars']), 120)
        self.assertEqual(full['source_quality'][0]['history_source'], 'SAME_BROKER_SAME_CONTRACT')
        c.candles[(ASSET, '1h')]['candles'][0]['close'] = 99.
        self.assertEqual(snap['candles']['1h']['candles'][0]['close'], 12.)
        snap['candles']['1m']['candles'][0]['volume_lots'] = 999
        self.assertEqual(c.candles[(ASSET, '1m')]['candles'][0]['volume_lots'], 2)
        c.books[ASSET]['bids'][0]['price'] = 99.
        self.assertEqual(snap['book']['bids'][0]['price'], 11.999)

    def test_quote_needs_no_history_but_default_full_validation_still_does(self):
        c = connection()
        c.candles.clear()
        self.assertTrue(D.quote(c, now=NOW)['paper_eligible'])
        with self.assertRaisesRegex(TB.TBankError, '^CNY_1M_HISTORY_NOT_READY$'):
            D.validate_snapshot(D._snapshot(c), now=NOW)

    def test_quote_preserves_exact_identity_venue_and_unit_validation(self):
        cases = (
            ('instruments', 'ticker', 'CNYRUBF_OTHER', 'EXACT_CNYRUBF_REQUIRED'),
            ('instruments', 'real_exchange', 'REAL_EXCHANGE_RTS', 'CNY_VENUE_MISMATCH'),
            ('instruments', 'min_price_increment', .01, 'CNY_PRICE_UNIT_UNVERIFIED'),
            ('instruments', 'basic_asset_size', 100., 'CNY_CONTRACT_SIZE_MISMATCH'),
            ('quotes', 'instrument_uid', 'other', 'CNY_QUOTE_IDENTITY_OR_PRICE_INVALID'),
            ('books', 'instrument_uid', 'other', 'CNY_BOOK_IDENTITY_OR_CONSISTENCY'),
            ('trading_states', 'instrument_uid', 'other', 'CNY_SESSION_STATUS_UNAVAILABLE'),
        )
        for section, field, value, reason in cases:
            with self.subTest(section=section, field=field):
                c = connection()
                getattr(c, section)[ASSET][field] = value
                with self.assertRaisesRegex(TB.TBankError, '^' + reason + '$'):
                    D.quote(c, now=NOW)

    def test_quote_preserves_staleness_future_and_session_checks(self):
        cases = (
            ('quotes', 'observed_at', (NOW - timedelta(seconds=121)).isoformat(), 'CNY_DIRECT_QUOTE_STALE'),
            ('quotes', 'observed_at', (NOW + timedelta(seconds=6)).isoformat(), 'CNY_DIRECT_QUOTE_STALE'),
            ('quotes', 'observed_at', None, 'CNY_DIRECT_QUOTE_STALE'),
            ('books', 'orderbook_ts', (NOW - timedelta(seconds=121)).isoformat(), 'CNY_BOOK_STALE'),
            ('trading_states', 'checked_at', (NOW - timedelta(seconds=121)).isoformat(), 'CNY_SESSION_STATUS_UNAVAILABLE'),
            ('trading_states', 'trading_status', 'SECURITY_TRADING_STATUS_BREAK_IN_TRADING', 'CNY_SESSION_NOT_TRADABLE'),
            ('trading_states', 'api_trade_available_flag', False, 'CNY_SESSION_NOT_TRADABLE'),
        )
        for section, field, value, reason in cases:
            with self.subTest(section=section, field=field, value=value):
                c = connection()
                getattr(c, section)[ASSET][field] = value
                with self.assertRaisesRegex(TB.TBankError, '^' + reason + '$'):
                    D.quote(c, now=NOW)
        c = connection()
        c.quotes[ASSET]['observed_at'] = (NOW - timedelta(seconds=120)).isoformat()
        self.assertTrue(D.quote(c, now=NOW)['paper_eligible'])

    def test_each_quote_uses_current_snapshot_without_cached_admission(self):
        c = connection()
        first = D.quote(c, now=NOW)
        later = NOW + timedelta(seconds=1)
        c.quotes[ASSET].update(price=12.0005, observed_at=later.isoformat())
        second = D.quote(c, now=later)
        self.assertEqual(first['price'], 12.)
        self.assertEqual(second['price'], 12.0005)
        self.assertEqual(second['observed_at'], later.isoformat())
        c.trading_states[ASSET]['api_trade_available_flag'] = False
        with self.assertRaisesRegex(TB.TBankError, '^CNY_SESSION_NOT_TRADABLE$'):
            D.quote(c, now=later)


class DirectCnyStatusTests(unittest.TestCase):
    def setUp(self):
        self.old_at = (NOW - timedelta(minutes=10)).isoformat()
        self.recorded = {
            'status': 'DIRECT_READY', 'reason': None, 'checked_at': self.old_at,
            'instrument_uid': 'previous-contract', 'quote_observed_at': self.old_at,
            'source': 'TBANK_GRPC', 'history_source': 'TBANK_GRPC', 'orders_enabled': False,
        }
        self.enterContext(patch.dict(D._STATE, self.recorded, clear=True))
        self.enterContext(patch.dict(D.os.environ, {'VERITAS_CNY_PRIMARY_SOURCE': 'TBANK'}))

    def test_old_recorded_state_with_fresh_reader_reports_current_complete_validation(self):
        c = connection()
        quote_at = (NOW - timedelta(seconds=17)).isoformat()
        book_at = (NOW - timedelta(seconds=6)).isoformat()
        session_at = (NOW - timedelta(seconds=25)).isoformat()
        c.quotes[ASSET]['observed_at'] = quote_at
        c.books[ASSET]['orderbook_ts'] = book_at
        c.trading_states[ASSET]['checked_at'] = session_at
        before = D._snapshot(c)
        with patch.object(TB, 'connection', c), patch.object(D, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = NOW
            result = D.status()
        self.assertEqual(result['status'], 'DIRECT_READY')
        self.assertIsNone(result['reason'])
        self.assertEqual(result['instrument_uid'], UID)
        self.assertEqual(result['quote_observed_at'], quote_at)
        self.assertEqual(result['book_observed_at'], book_at)
        self.assertEqual(result['session_checked_at'], session_at)
        self.assertEqual(result['checked_at'], TB.iso(NOW))
        self.assertEqual(result['last_market_check_at'], self.old_at)
        self.assertEqual(result['validation_basis'], 'CURRENT_READER_MEMORY')
        self.assertEqual(result['history_source'], 'TBANK_GRPC')
        self.assertEqual(result['history_last_candle_at'], {
            tf: c.candles[(ASSET, tf)]['candles'][-1]['time'] for tf in ('1m', '5m', '1h')})
        self.assertTrue(result['requires_same_quote_and_history_source'])
        self.assertFalse(result['orders_enabled'])
        self.assertEqual(result['version'], D.VERSION)
        self.assertIn('verifier', result)
        self.assertEqual(D._STATE, self.recorded)
        self.assertEqual(D._snapshot(c), before)

    def test_fresh_recorded_state_cannot_admit_stale_foreign_or_empty_reader(self):
        D._STATE.update(checked_at=NOW.isoformat(), quote_observed_at=NOW.isoformat(), instrument_uid=UID)
        cases = (
            ('quotes', 'observed_at', self.old_at, 'CNY_DIRECT_QUOTE_STALE'),
            ('quotes', 'observed_at', (NOW + timedelta(seconds=6)).isoformat(), 'CNY_DIRECT_QUOTE_STALE'),
            ('quotes', 'instrument_uid', 'foreign', 'CNY_QUOTE_IDENTITY_OR_PRICE_INVALID'),
            ('books', 'instrument_uid', 'foreign', 'CNY_BOOK_IDENTITY_OR_CONSISTENCY'),
            ('books', 'orderbook_ts', self.old_at, 'CNY_BOOK_STALE'),
            ('trading_states', 'instrument_uid', 'foreign', 'CNY_SESSION_STATUS_UNAVAILABLE'),
            ('trading_states', 'checked_at', self.old_at, 'CNY_SESSION_STATUS_UNAVAILABLE'),
            ('trading_states', 'api_trade_available_flag', False, 'CNY_SESSION_NOT_TRADABLE'),
            ('quotes', None, None, 'CNY_QUOTE_IDENTITY_OR_PRICE_INVALID'),
            ('instruments', None, None, 'EXACT_CNYRUBF_REQUIRED'),
        )
        for section, field, value, reason in cases:
            with self.subTest(section=section, field=field, reason=reason):
                c = connection()
                if field is None:
                    getattr(c, section).clear()
                else:
                    getattr(c, section)[ASSET][field] = value
                result = D.status(c, now=NOW)
                self.assertNotEqual(result['status'], 'DIRECT_READY')
                self.assertEqual(result['reason'], reason)
                self.assertIsNone(result['history_source'])
                self.assertFalse(result['orders_enabled'])
                if section == 'quotes' and field == 'observed_at':
                    self.assertEqual(result['quote_observed_at'], value)
        self.assertEqual(D._STATE['status'], 'DIRECT_READY')
        self.assertEqual(D._STATE['checked_at'], NOW.isoformat())

    def test_current_quote_does_not_certify_missing_foreign_stale_or_invalid_history(self):
        for tf in ('1m', '5m', '1h'):
            for defect in ('missing', 'foreign', 'error', 'stale', 'insufficient', 'geometry'):
                with self.subTest(timeframe=tf, defect=defect):
                    c = connection()
                    history = c.candles[(ASSET, tf)]
                    if defect == 'missing':
                        c.candles.pop((ASSET, tf))
                    elif defect == 'foreign':
                        history['instrument_uid'] = 'foreign'
                    elif defect == 'error':
                        history['status'] = 'ERROR'
                    elif defect == 'stale':
                        for bar in history['candles']:
                            bar['time'] = (datetime.fromisoformat(bar['time']) - timedelta(days=1)).isoformat()
                    elif defect == 'insufficient':
                        history['candles'].pop(0)
                    else:
                        history['candles'][-1]['low'] = 13.
                    result = D.status(c, now=NOW)
                    expected = {'missing': f'CNY_{tf.upper()}_HISTORY_NOT_READY',
                                'foreign': f'CNY_{tf.upper()}_HISTORY_NOT_READY',
                                'error': f'CNY_{tf.upper()}_HISTORY_NOT_READY',
                                'stale': f'CNY_{tf.upper()}_CANDLES_STALE',
                                'insufficient': f'CNY_{tf.upper()}_HISTORY_INSUFFICIENT',
                                'geometry': 'CNY_CANDLE_GEOMETRY'}[defect]
                    self.assertEqual(result['reason'], expected)
                    self.assertEqual(result['status'], 'RESEARCH_ONLY')
                    self.assertIsNone(result['history_source'])
                    self.assertNotIn('history_last_candle_at', result)

    def test_stale_quote_rejects_before_any_history_copy(self):
        c = connection()
        c.quotes[ASSET]['observed_at'] = self.old_at
        for history in c.candles.values():
            history['candles'] = [HistoryCopyForbidden(row) for row in history['candles']]
        result = D.status(c, now=NOW)
        self.assertEqual(result['status'], 'STALE')
        self.assertEqual(result['reason'], 'CNY_DIRECT_QUOTE_STALE')
        self.assertEqual(result['quote_observed_at'], self.old_at)

    def test_full_snapshot_rechecks_changed_quote_and_uses_the_same_clock(self):
        c = connection()
        early = D._snapshot(c, include_history=False)
        c.quotes[ASSET]['observed_at'] = self.old_at
        late = D._snapshot(c)
        with patch.object(D, '_snapshot', side_effect=[early, late]), \
                patch.object(D, 'validate_snapshot', wraps=D.validate_snapshot) as validate:
            result = D.status(c, now=NOW)
        self.assertEqual(result['reason'], 'CNY_DIRECT_QUOTE_STALE')
        self.assertEqual(result['quote_observed_at'], self.old_at)
        self.assertEqual([call.args[1] for call in validate.call_args_list], [NOW, NOW])
        self.assertEqual(D._STATE, self.recorded)

    def test_status_never_starts_refreshes_or_calls_broker_and_keeps_connection_error_visible(self):
        factory = Mock(side_effect=AssertionError('network factory forbidden'))
        c = TB.TBankConnection(environ={}, factory=factory)
        fixture = connection()
        for field in ('instruments', 'quotes', 'books', 'trading_states', 'candles'):
            setattr(c, field, getattr(fixture, field))
        c.state, c.error = 'ERROR', 'UNAVAILABLE'
        c.reader = Mock()
        c.reader.call.side_effect = AssertionError('RPC forbidden')
        c.reader.stream_prices.side_effect = AssertionError('stream forbidden')
        with patch.object(c, 'start', side_effect=AssertionError('start forbidden')) as start, \
                patch.object(c, 'refresh', side_effect=AssertionError('refresh forbidden')) as refresh:
            result = D.status(c, now=NOW)
            self.assertEqual(result['status'], 'DIRECT_READY')
            self.assertEqual(result['connection_status'], 'ERROR')
            self.assertEqual(result['connection_error_code'], 'UNAVAILABLE')
            c.quotes.clear()
            self.assertEqual(D.status(c, now=NOW)['status'], 'RESEARCH_ONLY')
        start.assert_not_called()
        refresh.assert_not_called()
        factory.assert_not_called()
        self.assertEqual(c.reader.mock_calls, [])

    def test_disabled_source_does_not_inspect_reader(self):
        with patch.dict(D.os.environ, {'VERITAS_CNY_PRIMARY_SOURCE': 'MOEX'}), \
                patch.object(D, '_snapshot', side_effect=AssertionError('disabled reader')) as snapshot:
            result = D.status(connection(), now=NOW)
        snapshot.assert_not_called()
        self.assertEqual(result['status'], 'NOT_CHECKED')
        self.assertEqual(result['reason'], 'CNY_DIRECT_SOURCE_DISABLED')
        self.assertFalse(result['enabled'])
        self.assertFalse(result['orders_enabled'])

    def test_market_or_fallback_does_not_revalidate_via_diagnostic_status(self):
        c = connection()
        with patch.object(D, 'datetime', wraps=datetime) as clock, \
                patch.object(D, '_snapshot', wraps=D._snapshot) as snapshot, \
                patch.object(D, 'status', side_effect=AssertionError('diagnostic recursion')) as status:
            clock.now.return_value = NOW
            fallback = Mock(return_value={'price': 12.})
            ready = D.market_or_fallback(fallback, connection=c)
            self.assertEqual(ready['feed_status']['status'], 'DIRECT_READY')
            self.assertEqual(snapshot.call_count, 1)
            fallback.assert_not_called()
            c.quotes.clear()
            blocked = D.market_or_fallback(fallback, connection=c)
            self.assertEqual(blocked['feed_status']['status'], 'RESEARCH_ONLY')
            self.assertFalse(blocked['source_gate_pass'])
            self.assertEqual(snapshot.call_count, 2)
        status.assert_not_called()


if __name__ == '__main__':
    unittest.main()
