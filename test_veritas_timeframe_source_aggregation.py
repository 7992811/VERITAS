"""Aggregation retains proved candle provenance without certifying mixed input."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

import veritas_ma_rebound as MA
import veritas_price_source as SOURCE
import veritas_timeframe_data as DATA
import veritas_timeframe_structure as STRUCTURE
import veritas_timeframe_policy as POLICY
from test_veritas_ma_rebound import example


class AggregatedSourceTests(unittest.TestCase):
    providers = (('ETH', 'Binance spot'), ('MOEX', 'MOEX ISS IMOEX'),
                 ('NQ', 'ProFinance NASD100_FUT'))

    def fixture(self, asset, provider):
        local, daily, now = example(timeframe='4h')
        identity = SOURCE.identity(asset, {'source': provider})
        hourly = [dict(bar, ts=bar['ts']+i*3600, available_at=bar['ts']+(i+1)*3600,
                       timeframe='1h', source_identity=deepcopy(identity))
                  for bar in local for i in range(4)]
        daily = [dict(bar, source_identity=deepcopy(identity)) for bar in daily]
        return hourly, daily, now, identity

    def attach(self, asset, provider, hourly, daily, now, identity):
        raw = dict(asset=asset, source=provider, canonical_hourly_bars=hourly)
        if asset == 'NQ':
            raw.update(native_source_history_attached=True,
                       structure_source_identity=deepcopy(identity),
                       structure_bars_by_timeframe={'1h': hourly})
        with patch('veritas_native_daily.fetch_native_daily', return_value={'bars': daily}):
            return DATA.attach(raw, now)

    def test_eth_moex_nq_keep_source_and_ohlc_through_real_attachment(self):
        for asset, provider in self.providers:
            with self.subTest(asset=asset):
                hourly, daily, now, identity = self.fixture(asset, provider)
                original = deepcopy(hourly)
                attached = self.attach(asset, provider, hourly, daily, now, identity)
                aggregated = attached['structure_bars_by_timeframe']['4h']
                self.assertEqual(len(aggregated), 28)
                self.assertTrue(all(bar.get('source_identity') == identity for bar in aggregated))
                for index, bar in enumerate(aggregated):
                    native = hourly[index*4:index*4+4]
                    self.assertEqual(bar['ts'], native[0]['ts'])
                    self.assertEqual(bar['available_at'], native[-1]['available_at'])
                    self.assertEqual(bar['open'], native[0]['open'])
                    self.assertEqual(bar['close'], native[-1]['close'])
                    self.assertEqual(bar['high'], max(x['high'] for x in native))
                    self.assertEqual(bar['low'], min(x['low'] for x in native))
                aggregated[0]['source_identity']['key'] = 'MUTATED'
                self.assertEqual(hourly, original)
                self.assertEqual(attached['structure_bars_by_timeframe']['1h'][0]['source_identity'], identity)

    def test_eth_moex_aggregated_rebound_reaches_unchanged_provenance_and_entry_gates(self):
        for asset, provider in self.providers[:2]:
            with self.subTest(asset=asset):
                hourly, daily, now, identity = self.fixture(asset, provider)
                attached = self.attach(asset, provider, hourly, daily, now, identity)
                context = MA.build_context(attached['structure_bars_by_timeframe']['4h'], '4h', now,
                    daily_bars=daily, asset=asset, source_identity=identity)
                self.assertEqual(context['reason'], 'MA_REBOUND_READY')
                event = context['event']
                self.assertEqual(event['signal_at'], now)
                self.assertEqual(event['source_identity'], identity)
                self.assertTrue(MA.validate_event(event, identity)['eligible'])
                self.assertTrue(STRUCTURE.entry_gate(context, event['signal_price'], 'LONG', now)['eligible'])

    def test_missing_foreign_or_different_contract_source_cannot_certify_a_bucket(self):
        for asset, provider in self.providers:
            for replacement in (None, {'key': 'FOREIGN', 'asset': asset},
                                {'key': SOURCE.identity(asset, {'source': provider})['key'],
                                 'asset': asset, 'contract_id': 'DIFFERENT_CONTRACT'}):
                with self.subTest(asset=asset, replacement=replacement):
                    hourly, daily, now, identity = self.fixture(asset, provider)
                    hourly[1]['source_identity'] = replacement
                    aggregated = STRUCTURE.aggregate_closed_bars(hourly, '1h', '4h', now, anchor=0)
                    self.assertNotIn('source_identity', aggregated[0])
                    self.assertEqual(aggregated[1]['source_identity'], identity)
                    context = MA.build_context(aggregated, '4h', now,
                        daily_bars=daily, asset=asset, source_identity=identity)
                    self.assertEqual(context['reason'], 'MA_LOCAL_SOURCE_MISMATCH')
                    self.assertIsNone(context['event'])

    def test_conflicting_duplicate_provenance_cannot_borrow_the_other_rows_label(self):
        hourly, _, now, _ = self.fixture('NQ', 'ProFinance NASD100_FUT')
        for source in (None, {'key': 'YAHOO:NQ=F', 'asset': 'NQ'}):
            with self.subTest(source=source):
                duplicate = dict(hourly[1], source_identity=source)
                aggregated = STRUCTURE.aggregate_closed_bars(hourly+[duplicate], '1h', '4h', now, anchor=0)
                self.assertNotIn('source_identity', aggregated[0])

    def test_foreign_native_confirmation_is_filtered_without_manufacturing_a_closed_bucket(self):
        for asset, provider in self.providers:
            with self.subTest(asset=asset):
                hourly, daily, now, identity = self.fixture(asset, provider)
                hourly[-1]['source_identity'] = {'key': 'FOREIGN', 'asset': asset}
                attached = self.attach(asset, provider, hourly, daily, now, identity)
                aggregated = attached['structure_bars_by_timeframe']['4h']
                self.assertEqual(len(aggregated), 27)
                self.assertLess(aggregated[-1]['available_at'], now)
                self.assertTrue(all(bar['source_identity'] == identity for bar in aggregated))

    def test_unknown_history_stays_unlabelled_and_same_timeframe_preserves_known_source(self):
        hourly, _, now, identity = self.fixture('ETH', 'Binance spot')
        known = STRUCTURE.aggregate_closed_bars(hourly, '1h', '1h', now)
        self.assertTrue(all(bar['source_identity'] == identity for bar in known))
        for bar in hourly:
            bar.pop('source_identity')
        unknown = STRUCTURE.aggregate_closed_bars(hourly, '1h', '4h', now, anchor=0)
        self.assertEqual(len(unknown), 28)
        self.assertTrue(all('source_identity' not in bar for bar in unknown))


class ProFinanceMinuteTailTests(unittest.TestCase):
    def fixture(self, asset='NQ', native_count=28):
        now = datetime(2026, 10, 7, 8, 0, 30, tzinfo=timezone.utc).timestamp()
        identity = SOURCE.identity(asset, {'source': 'ProFinance'})
        end = int(now)//300*300
        minutes = [dict(ts=end-(200-i)*60, available_at=end-(199-i)*60,
                        open=100., high=101., low=99., close=100., volume=None,
                        volume_available=False, timeframe='1m', finalized=True,
                        source_identity=deepcopy(identity)) for i in range(200)]
        native = STRUCTURE.aggregate_closed_bars(minutes, '1m', '5m', now, anchor=0)[:native_count]
        for bar in native:
            bar.update(volume=None, volume_available=False, finalized=True)
        raw = dict(asset=asset, source='ProFinance', price=100., observed_at=now,
                   native_source_history_attached=True, structure_source_identity=deepcopy(identity),
                   structure_bars_by_timeframe={'1m': minutes, '5m': native},
                   structure_history_status={'1m': {'fetched_at': now-5},
                       '5m': {'status': 'STALE' if native else 'UNAVAILABLE',
                       'fetch_error': 'TimeoutError: native 5m request exceeded budget',
                       'fetched_at': now-1200}})
        return raw, now, identity

    def attach(self, raw, now):
        with patch('veritas_native_daily.fetch_native_daily', return_value={'bars': []}), \
             patch('veritas_profinance_history.fetch_history_bundle', side_effect=AssertionError('Extra HTTP fetch')):
            return DATA.attach(raw, now)

    def context_gate(self, attached, now):
        context = DATA.context(attached, '5m', now)
        return POLICY.context_gate(dict(asset=attached['asset'], horizon='5m',
                                        timeframe_entry_context=context), now)

    def test_timeout_or_stale_native_tail_is_completed_without_changing_native_ohlc(self):
        for asset in ('NQ', 'GOLD', 'BRENT'):
            for native_count in (0, 28):
                with self.subTest(asset=asset, native_count=native_count):
                    raw, now, identity = self.fixture(asset, native_count)
                    native = raw['structure_bars_by_timeframe']['5m']
                    if native:
                        native[-1].update(open=103., high=104., low=102., close=103.)
                    original = deepcopy(raw)
                    attached = self.attach(raw, now)
                    bars = attached['structure_bars_by_timeframe']['5m']
                    self.assertEqual(raw, original)
                    self.assertEqual(bars[:native_count], original['structure_bars_by_timeframe']['5m'])
                    self.assertEqual(len(bars), 40)
                    derived = bars[native_count:]
                    self.assertTrue(all(bar['source_identity'] == identity for bar in derived))
                    self.assertTrue(all(bar['volume'] is None and bar['volume_available'] is False for bar in derived))
                    self.assertEqual(attached['structure_intraday_bars'], bars)
                    self.assertTrue(self.context_gate(attached, now)['eligible'])
                    status = attached['structure_history_status']['5m']
                    self.assertEqual(status['status'], 'READY')
                    self.assertEqual(status['derived_closed_bars'], 40-native_count)
                    self.assertEqual(status['fetch_error'], original['structure_history_status']['5m']['fetch_error'])
                    self.assertEqual(status['fetched_at'], now-1200)
                    self.assertEqual(status['derived_source_fetched_at'], now-5)
                    self.assertEqual(status['native_history_status'], original['structure_history_status']['5m'])

    def test_gap_foreign_contract_and_forming_minute_cannot_make_current_5m(self):
        for problem in ('gap', 'foreign', 'contract', 'forming', 'missing',
                        'duplicate_foreign', 'duplicate_unknown'):
            with self.subTest(problem=problem):
                raw, now, identity = self.fixture()
                minutes = raw['structure_bars_by_timeframe']['1m']
                if problem == 'gap':
                    minutes.pop(-3)
                elif problem == 'foreign':
                    minutes[-3]['source_identity'] = {'key': 'YAHOO:NQ=F', 'asset': 'NQ'}
                elif problem == 'contract':
                    minutes[-3]['source_identity'] = dict(identity, contract_id='OTHER_CONTRACT')
                elif problem == 'forming':
                    minutes[-3]['finalized'] = False
                elif problem == 'missing':
                    minutes[-3].pop('source_identity')
                else:
                    duplicate = dict(minutes[-3], source_identity=(
                        {'key': 'YAHOO:NQ=F', 'asset': 'NQ'} if problem == 'duplicate_foreign' else None))
                    minutes.append(duplicate)
                raw.update(price=999., structure_quote={'price': 999., 'observed_at': now})
                attached = self.attach(raw, now)
                gate = self.context_gate(attached, now)
                self.assertFalse(gate['eligible'], gate)
                self.assertEqual(gate['reason'], 'SAME_TF_CONTEXT_STALE')
                self.assertLess(attached['structure_bars_by_timeframe']['5m'][-1]['available_at'], int(now)//300*300)

    def test_existing_native_forming_ohlc_is_neither_completed_nor_overwritten(self):
        raw, now, identity = self.fixture()
        end = int(now)//300*300
        forming = dict(ts=end-300, available_at=end, open=100., high=150., low=50., close=125.,
                       volume=None, finalized=False, timeframe='5m', source_identity=identity)
        raw['structure_bars_by_timeframe']['5m'].append(forming)
        attached = self.attach(raw, now)
        matching = [bar for bar in attached['structure_bars_by_timeframe']['5m'] if bar['ts'] == forming['ts']]
        self.assertEqual(matching, [forming])
        self.assertFalse(self.context_gate(attached, now)['eligible'])

    def test_current_native_5m_and_stale_minutes_are_not_replaced(self):
        for native_count, shift in ((40, 0), (28, 180)):
            with self.subTest(native_count=native_count):
                raw, now, _ = self.fixture(native_count=native_count)
                for bar in raw['structure_bars_by_timeframe']['1m']:
                    bar['ts'] -= shift
                    bar['available_at'] -= shift
                native = deepcopy(raw['structure_bars_by_timeframe']['5m'])
                attached = self.attach(raw, now)
                self.assertEqual(attached['structure_bars_by_timeframe']['5m'], native)
                self.assertNotIn('derived_closed_bars', attached['structure_history_status']['5m'])

    def test_completed_tail_keeps_the_five_hundred_bar_bound(self):
        raw, now, _ = self.fixture()
        native = raw['structure_bars_by_timeframe']['5m']
        first = native[0]
        older = [dict(first, ts=first['ts']-(472-i)*300,
                      available_at=first['available_at']-(472-i)*300) for i in range(472)]
        raw['structure_bars_by_timeframe']['5m'] = older + native
        attached = self.attach(raw, now)
        self.assertEqual(len(attached['structure_bars_by_timeframe']['5m']), 500)
        self.assertTrue(self.context_gate(attached, now)['eligible'])
        self.assertEqual(attached['structure_history_status']['5m']['derived_closed_bars'], 12)


if __name__ == '__main__':
    unittest.main()
