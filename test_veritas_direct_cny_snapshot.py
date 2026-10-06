"""CNY quote snapshots exclude history while preserving execution validation."""
from datetime import datetime, timedelta, timezone
from threading import RLock
from types import SimpleNamespace
import unittest

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


if __name__ == '__main__':
    unittest.main()
