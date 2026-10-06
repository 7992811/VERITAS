"""Regression contract: a missing secondary feed never blocks paper by itself."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import veritas_execution as VX
import veritas_intelligence as VI
import veritas_portfolio as VP


class SingleSourceTests(unittest.TestCase):
    def row(self, asset):
        raw = dict(asset=asset, price=100., source_gate_pass=True, market_open=True,
                   observed_at=datetime.now(timezone.utc).isoformat(), direct_sources=1,
                   best_bid=99.99, best_ask=100.01, secondary_price=None)
        gate = VI.execution_eligibility(asset, raw, {'ok': True})
        context={} if asset!='NQ' else {'status':'OK','closed_at':datetime.now(timezone.utc).timestamp(),
            'event':{'direction':'SHORT','trigger_level':100.2,'atr':1.,'stop_price':101.,
                     'bars_since_signal':0,'signal_price':100.}}
        return dict(raw, horizon='1h', market_observed_at=raw['observed_at'],trend_entry_context=context,
                    research_decision='SHORT', signal_tier='SUPER_SHORT', confidence=.9,
                    calibrated_probability=.9, _pwin=.9, _pwin_source='EMPIRICAL_CALIBRATION',
                    _alignment_count=3, entry_quality='CONFIRMED_TREND',
                    horizon_structure=dict(direction='SHORT', score=.9, state='CONFIRMED_TREND'),
                    institutional_signal=dict(evidence_independence=dict(independent_count=5), action='ENTER_CANDIDATE'),
                    execution_eligible=gate['eligible'], paper_eligible=gate['paper_eligible'],
                    production_eligible=gate['production_eligible'],
                    trade_plan=VI.final_execution_safety(asset, 'SHORT', dict(
                        eligible=True, entry_price=100., stop_price=101., target_price=97.,
                        expected_to_stop_ratio=3., expected_move_pct=.03,
                        stop_distance_pct=.01, initial_position_fraction=.1)))

    def test_all_assets_and_portfolios_accept_one_source_with_strict_flag_on(self):
        with patch.object(VI, 'STRICT_EXECUTION_SOURCE_GATE', True):
            for asset in VX.PAPER_ASSETS:
                for name, policy in VP.POLICIES.items():
                    # Currency is a separate CNYRUBF-only book and is still
                    # intentionally fail-closed until its setup is activated.
                    if str(policy.get('mode') or '') == 'CURRENCY':
                        continue
                    for lost_flag in (False, True):
                        with self.subTest(asset=asset, portfolio=name, lost_flag=lost_flag):
                            row = self.row(asset)
                            gate = VI.execution_eligibility(asset, row, {'ok': True})
                            self.assertTrue(gate['eligible'], gate)
                            self.assertTrue(gate['paper_eligible'])
                            self.assertFalse(gate['production_eligible'])
                            self.assertEqual(gate['minimum_sources'], 1)
                            self.assertEqual(gate['direct_sources'], 1)
                            if lost_flag:
                                row.pop('paper_eligible')
                            admitted = VP._signal_first_admission(row, policy, 0.)
                            self.assertTrue(admitted['open'], admitted)
                            self.assertGreater(admitted['fraction'], 0.)

    def test_bad_primary_or_closed_market_is_never_admitted(self):
        changes = [dict(price=x) for x in (None, 0, -1, float('nan'), float('inf'))]
        changes += [dict(source_gate_pass=False), dict(source_gate_pass=None),
                    dict(market_open=False), dict(market_open=None), dict(direct_sources=0)]
        for asset in VX.PAPER_ASSETS:
            for change in changes:
                with self.subTest(asset=asset, change=change):
                    row = self.row(asset); row.update(change)
                    self.assertFalse(VI.execution_eligibility(asset, row)['eligible'])
                    self.assertFalse(VP._signal_first_admission(row, VP.POLICIES['Aggressive'], 0.)['open'])

    def test_optional_cross_check_cannot_hide_contradictory_crypto_prices(self):
        row = self.row('BTC'); row.update(secondary_price=110, source_divergence=0.)
        gate = VI.execution_eligibility('BTC', row, {'ok': True})
        self.assertFalse(gate['eligible'])
        self.assertIn('DIRECT_QUOTE_DIVERGENCE_TOO_LARGE', gate['paper_source_blockers'])

    def test_crypto_still_needs_valid_book_and_clock(self):
        for change in (dict(best_bid=None), dict(best_ask=None), dict(best_bid=101.),
                       dict(best_ask=float('inf'))):
            row = self.row('BTC'); row.update(change)
            self.assertFalse(VI.execution_eligibility('BTC', row, {'ok': True})['eligible'])
        self.assertFalse(VI.execution_eligibility('BTC', self.row('BTC'), {'ok': False})['eligible'])

    def test_one_source_never_overrides_freshness_economics_or_explicit_denial(self):
        for asset in VX.PAPER_ASSETS:
            for mode in ('stale', 'bad_economics', 'explicit_denial'):
                with self.subTest(asset=asset, mode=mode):
                    row = self.row(asset)
                    if mode == 'stale':
                        row['market_observed_at'] = (datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()
                    elif mode == 'bad_economics':
                        row['trade_plan']['expected_to_stop_ratio'] = .10
                        row['trade_plan']['expected_move_pct'] = .001
                        row['trade_plan']['target_price'] = 99.9  # below 0.19%/cost floor after recomputation
                    else:
                        row['paper_eligible'] = False
                    self.assertFalse(VP._signal_first_admission(row, VP.POLICIES['Aggressive'], 0.)['open'])

    def test_source_policy_survives_durable_compaction(self):
        for asset in VX.PAPER_ASSETS:
            row = self.row(asset)
            gate = VI.execution_eligibility(asset, row)
            payload = VI._v90r37_compact_decision_payload(dict(
                asset=asset, horizon='1h', decision='SHORT', research_decision='SHORT',
                execution_eligibility=gate, features={'price':100.},
                trade_plan=row['trade_plan'], gates={'source':True,'time':True,'execution':True}))
            self.assertEqual(payload['execution_eligibility']['minimum_sources'], 1)
            conn = MagicMock()
            conn.__enter__.return_value.execute.return_value.fetchall.return_value = [
                dict(asset=asset, horizon='1h', event_ts=row['observed_at'], payload=payload)]
            with patch.object(VI, 'pg_enabled', return_value=True), patch.object(VI, 'pg_connect', return_value=conn):
                restored = VI.latest_signal_summary_pg()[0]
            self.assertTrue(restored['execution_eligible'])
            self.assertTrue(restored['paper_eligible'])
            self.assertEqual(restored['execution_reason'], 'paper_one_valid_source')

    def test_crypto_feed_survives_secondary_timeout_or_invalid_quote(self):
        stamp = int(datetime.now(timezone.utc).timestamp()*1000)
        bars = [[stamp-3600000, '100','101','99','100','10',stamp,'0','0','5']] * 201
        for secondary in (TimeoutError(), {}, {'price':'NaN'}, {'price':'0'}):
            def fetch(url, params=None):
                if '/klines' in url: return bars
                if '/bookTicker' in url: return {'bidPrice':'99.99','askPrice':'100.01'}
                if isinstance(secondary, Exception): raise secondary
                return secondary
            with self.subTest(secondary=secondary), patch.object(VI, 'get_json', side_effect=fetch):
                raw = VI.market('BTCUSDT', 'BTC-USD')
            self.assertIsNone(raw['secondary_price'])
            self.assertEqual(raw['direct_sources'], 1)
            self.assertTrue(VI.execution_eligibility('BTC', raw, {'ok':True})['eligible'])
            self.assertEqual(raw['source_quality'][1]['status'], 'UNAVAILABLE')

    def test_clock_needs_one_available_clock_but_rejects_measured_skew(self):
        for failed in ('binance', 'coinbase', 'both', None):
            def fetch(url):
                name = 'binance' if 'binance' in url else 'coinbase'
                if failed in (name, 'both'): raise TimeoutError()
                return {'serverTime':1000000} if name=='binance' else {'epoch':1000}
            with patch.object(VI, 'get_json', side_effect=fetch), patch.object(VI.time, 'time', return_value=1000):
                out = VI.source_clock_gate()
            self.assertEqual(out['ok'], failed!='both')
        with patch.object(VI, 'get_json', side_effect=[{'serverTime':1}, TimeoutError()]), patch.object(VI.time, 'time', return_value=1000):
            self.assertFalse(VI.source_clock_gate()['ok'])


if __name__ == '__main__':
    unittest.main()
