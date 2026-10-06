"""Source attachment, completed aggregation and persisted entry provenance."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

import veritas_price_source as VPS
import veritas_timeframe_data as TFD
from test_veritas_timeframe_structure import example


def nullable_profinance_market(asset='NQ'):
    """Use the actual public parser shape: Last OHLC, volume=None, unobserved."""
    import veritas_profinance_history as H
    from test_veritas_profinance_history import history_text, NOW
    clock = datetime.fromtimestamp(NOW,timezone.utc)
    identity = VPS.identity(asset, {'source':'ProFinance'})
    bars = {tf:H.parse_history(history_text(tt,NOW,500),asset,tf,NOW)['bars']
            for tf,tt in (('1m',1),('5m',3),('1h',6),('4h',8),('1d',9))}
    raw = dict(asset=asset,source='ProFinance',source_names={'primary':'ProFinance'},
               price=101.,closes=[101.]*600,highs=[102.]*600,lows=[99.]*600,
               vols=[1.]*600,taker_buy=[.5]*600,returns=[0.]*599,
               source_divergence=0.,observed_at=clock.isoformat(),
               binance_close_time_ms=int(NOW*1000),source_gate_pass=True,market_open=True,
               intraday_bars=bars['5m'],intraday_5m=bars['5m'])
    with patch('veritas_profinance_history.fetch_history_bundle',return_value={
            'source_identity':identity,'bars_by_timeframe':bars}):
        attached=TFD.attach(raw,clock)
    return attached, clock


class NativeTimeframeDataTests(unittest.TestCase):
    def test_native_exchange_opens_and_timestamps_are_preserved(self):
        self.assertEqual(TFD.native_ohlc([[1700000000000, '101', '104', '99', '103', '7']]),
            [dict(ts=1700000000., open=101., high=104., low=99., close=103., volume=7.)])

    def test_foreign_history_cannot_be_relabelled_as_profinance(self):
        rows, now = example('1h')
        raw = {'asset':'NQ','source':'ProFinance NASD100_FUT',
               'canonical_hourly_bars':rows, 'intraday_bars':rows}
        wrong = VPS.identity('NQ', {'source':'Yahoo CME NQ=F'})
        with patch('veritas_profinance_history.fetch_history_bundle', return_value={
                'source_identity':wrong, 'bars_by_timeframe':{'1h':rows}}):
            attached = TFD.attach(raw, now)
        self.assertEqual(attached['structure_bars_by_timeframe'], {})
        self.assertEqual(attached['structure_history_error'], 'SAME_TF_SOURCE_MISMATCH')

    def test_unavailable_native_provider_never_falls_back_to_forecast_history(self):
        rows, now = example('1h')
        raw = {'asset':'NQ','source':'ProFinance NASD100_FUT',
               'canonical_hourly_bars':rows, 'intraday_bars':rows}
        identity = VPS.identity('NQ', raw)
        with patch('veritas_profinance_history.fetch_history_bundle', return_value={
                'source_identity':identity, 'bars_by_timeframe':{},
                'status_by_timeframe':{'1h':{'status':'UNAVAILABLE'}}}):
            attached = TFD.attach(raw, now)
        self.assertEqual(attached['structure_bars_by_timeframe'], {})
        self.assertIsNone(TFD.context(attached,'1h',now)['event'])

    def test_context_is_recomputed_when_a_candle_closes_without_retrieval(self):
        rows, now = example()
        identity = VPS.identity('BTC', {'source':'Binance'})
        raw = {'asset':'BTC','structure_source_identity':identity,
               'structure_bars_by_timeframe':{'5m':rows}}
        self.assertIsNone(TFD.context(raw,'5m',now-1)['event'])
        closed = TFD.context(raw,'5m',now)
        self.assertIsNotNone(closed['event'])
        self.assertEqual(closed['event']['signal_at'],now)
        self.assertEqual(TFD.context(raw,'5m',now+1)['event']['event_id'],closed['event']['event_id'])

    def test_explicit_timeframe_contradiction_is_rejected_before_relabelling(self):
        rows, now = example('4h')
        raw={'asset':'BTC','source':'Binance','canonical_five_minute_bars':rows}
        attached=TFD.attach(raw,now)
        self.assertEqual(attached['structure_bars_by_timeframe']['5m'],[])
        self.assertIsNone(TFD.context(attached,'5m',now)['event'])

    def test_daily_native_phase_survives_fixed_weekly_aggregation(self):
        # Monday midnight Moscow is Sunday 21:00 UTC. Never slide this bucket.
        start = datetime(2026,9,27,21,tzinfo=timezone.utc).timestamp()
        rows = [dict(ts=start+i*86400,open=100+i,high=102+i,low=99+i,
                     close=101+i,timeframe='1d') for i in range(14)]
        raw = {'asset':'NQ','source':'ProFinance NASD100_FUT'}
        identity = VPS.identity('NQ',raw)
        with patch('veritas_profinance_history.fetch_history_bundle',return_value={
                'source_identity':identity,'bars_by_timeframe':{'1d':rows}}):
            result = TFD.attach(raw,start+14*86400)['structure_bars_by_timeframe']['7d']
        self.assertEqual([b['ts'] for b in result],[start,start+7*86400])
        self.assertEqual(result[0]['open'],100)
        self.assertEqual(result[0]['close'],107)

    def test_currency_5m_requires_all_five_observed_minutes(self):
        import veritas_intelligence as VI
        start=1770000000 // 300 * 300
        minute_indices=[0,1,2,3,4,5,6,8,9,10,11]
        klines=[[1000*(start+i*60),100,102,99,101,1] for i in minute_indices]
        with patch.object(VI,'_v90_cny5_cache',{'at':0.,'bars':[]}), \
             patch.object(VI,'_moex_futures_candles_between',return_value=klines), \
             patch.object(VI.time,'time',return_value=start+12*60):
            bars=VI._v90_cny_5m_bars(force=True)
        self.assertEqual(len(bars),1)
        self.assertEqual(bars[0]['ts'],start)
        self.assertEqual(bars[0]['volume'],5)

    def test_ledger_compactor_preserves_original_event_not_nearest_forecast(self):
        import veritas_intelligence as VI
        from test_veritas_timeframe_policy import valid_row
        import veritas_timeframe_policy as TFP
        row=TFP.prepare_row(valid_row())
        original=deepcopy(row['trade_plan']['entry_event_snapshot'])
        compact=VI._v90r37_compact_decision_payload(row)
        self.assertEqual(compact['trade_plan']['entry_event_snapshot'],original)
        self.assertEqual(compact['trade_plan']['entry_event_id'],original['event_id'])
        self.assertEqual(compact['trade_plan']['timeframe_entry_context']['event'],original)

    def test_nullable_volume_is_normalized_only_in_the_legacy_minute_copy(self):
        from veritas_local_breakout import closed_minutes
        raw, clock = nullable_profinance_market()
        native=raw['structure_minute_bars']
        original=deepcopy(native)
        legacy=closed_minutes(native,clock)
        self.assertEqual(len(legacy),len(native))
        self.assertTrue(all(b['volume']==0. and b['volume_available'] is False for b in legacy.values()))
        self.assertEqual(native,original)
        self.assertTrue(all(b['volume'] is None and b['volume_available'] is False
                            for b in raw['structure_bars_by_timeframe']['1m']))
        observed=dict(native[-1],volume=150.,volume_available=True)
        self.assertEqual(closed_minutes([observed],clock)[observed['ts']]['volume'],150.)
        for value in (None,float('nan'),float('inf'),-1.):
            with self.subTest(volume=value):
                bar=dict(observed,volume=value)
                safe=closed_minutes([bar],clock)[bar['ts']]
                self.assertEqual(safe['volume'],0.)
                self.assertFalse(safe['volume_available'])

    def test_nullable_volume_reaches_both_minute_and_senior_legacy_consumers(self):
        import veritas_minute_entry as M
        import veritas_trend_entry as VTE
        raw, clock = nullable_profinance_market()
        features=M.minute_features({},raw,clock)
        self.assertEqual(features['minute_data_status'],'OK')
        self.assertEqual(features['volume_ratio'],0.)
        context=VTE.build_context(raw['structure_intraday_bars'],clock,'NQ',
                                  minute_bars=raw['structure_minute_bars'])
        self.assertEqual(context['status'],'OK')
        if context.get('event'):
            self.assertEqual(context['event']['relative_volume'],0.)
            self.assertFalse(context['event']['activity_confirmed'])
        self.assertTrue(all(b['volume'] is None for b in raw['structure_minute_bars']))

    def test_full_feature_entrypoint_accepts_nullable_native_volume(self):
        import veritas_intelligence as VI
        for asset in ('NQ','GOLD','BRENT'):
            raw, clock = nullable_profinance_market(asset)
            class Frozen(datetime):
                @classmethod
                def now(cls,tz=None):
                    return clock
            with patch.object(VI,'datetime',Frozen),patch.object(TFD,'datetime',Frozen):
                for horizon in ('1m','5m','1h','4h','1d','3d','7d'):
                    with self.subTest(asset=asset,horizon=horizon):
                        features=VI.features(dict(raw),horizon)
                        context=features['timeframe_entry_context']
                        self.assertEqual(context['timeframe'],horizon)
                        self.assertEqual(context['source_identity'],raw['structure_source_identity'])
                        if horizon=='1m':
                            self.assertEqual(features['minute_data_status'],'OK')
                            self.assertEqual(features['volume_ratio'],0.)
            self.assertTrue(all(b['volume'] is None for b in raw['structure_minute_bars']))


if __name__=='__main__':
    unittest.main()
