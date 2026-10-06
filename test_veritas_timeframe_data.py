"""Source attachment, completed aggregation and persisted entry provenance."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
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
                'status_by_timeframe':{'1h':{'status':'UNAVAILABLE'}}}), \
             patch('veritas_native_daily.fetch_native_daily', return_value={
                'source_identity':identity, 'bars':[], 'status':'UNAVAILABLE'}):
            attached = TFD.attach(raw, now)
        self.assertEqual(attached['structure_bars_by_timeframe'],
                         {'1d':[], '3d':[], '7d':[]})
        self.assertEqual(attached['canonical_hourly_bars'],rows)
        self.assertEqual(attached['intraday_bars'],rows)
        self.assertEqual(attached['structure_minute_bars'],[])
        self.assertEqual(attached['structure_intraday_bars'],[])
        self.assertEqual(attached['native_daily_bars'],[])
        for horizon in ('1h','1d','3d','7d'):
            self.assertIsNone(TFD.context(attached,horizon,now)['event'])
        for horizon in ('1d','3d','7d'):
            status = attached['structure_history_status'][horizon]
            self.assertEqual(status['status'],'UNAVAILABLE')
            self.assertEqual(status['reason'],'NATIVE_DAILY_INTERVAL_UNVERIFIED')
            self.assertFalse(status['interval_boundary_verified'])

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
        from veritas_native_daily import parse_moex
        # These native interval24 observations explicitly report a full Moscow
        # calendar day. Their provider begin/end establish the non-UTC phase.
        local_start = datetime(2026,9,28)
        start = datetime(2026,9,27,21,tzinfo=timezone.utc).timestamp()
        now = start+14*86400
        raw = {'asset':'CNYRUBF','source':'MOEX CNYRUBF'}
        identity = VPS.identity('CNYRUBF',raw)
        self.assertEqual(identity['key'],'MOEX:CNYRUBF')
        data = []
        for i in range(14):
            begin = local_start+timedelta(days=i)
            end = begin+timedelta(days=1,seconds=-1)
            data.append([begin.isoformat(),end.isoformat(),100+i,102+i,99+i,101+i,1.])
        rows = parse_moex({'candles':{
            'columns':['begin','end','open','high','low','close','volume'],
            'data':data}},identity,now)
        self.assertEqual(rows[0]['ts'],start)
        self.assertEqual(rows[0]['end_ts'],start+86400)
        self.assertEqual(rows[0]['provider_end_ts'],start+86400-1)
        self.assertEqual(rows[0]['native_interval'],24)
        raw['canonical_daily_bars'] = rows
        with patch('veritas_native_daily.fetch_native_daily',return_value={
                'source_identity':identity,'bars':rows,'status':'READY'}):
            result = TFD.attach(raw,now)['structure_bars_by_timeframe']['7d']
        self.assertEqual([b['ts'] for b in result],[start,start+7*86400])
        self.assertEqual(result[0]['open'],100)
        self.assertEqual(result[0]['close'],107)

    def test_profinance_daily_date_labels_remain_ma_context_not_structural_clock(self):
        from test_veritas_ma_rebound import pf_example
        _,daily,now = pf_example()
        foreign, _ = example('1h')
        raw = {'asset':'NQ','source':'ProFinance NASD100_FUT',
               'canonical_hourly_bars':foreign,'intraday_bars':foreign}
        identity = VPS.identity('NQ',raw)
        for bar in daily:
            bar['source_identity'] = deepcopy(identity)
            bar['completion_proof']['source_identity'] = {
                'key':identity['key'],'contract_id':identity.get('contract_id')}
        original = deepcopy(daily)
        with patch('veritas_profinance_history.fetch_history_bundle',return_value={
                'source_identity':identity,'bars_by_timeframe':{'1d':daily}}), \
             patch('veritas_native_daily.fetch_native_daily',return_value={
                'source_identity':identity,'bars':daily,'status':'READY'}):
            attached = TFD.attach(raw,now)
        self.assertEqual(daily,original)
        self.assertEqual(attached['native_daily_bars'],original)
        self.assertNotIn('1h',attached['structure_bars_by_timeframe'])
        for horizon in ('1d','3d','7d'):
            self.assertEqual(attached['structure_bars_by_timeframe'][horizon],[])
            status = attached['structure_history_status'][horizon]
            self.assertEqual(status['reason'],'NATIVE_DAILY_INTERVAL_UNVERIFIED')
            self.assertEqual(status['status'],'UNAVAILABLE')
            self.assertFalse(status['interval_boundary_verified'])
            self.assertIsNone(TFD.context(attached,horizon,now)['event'])
        daily_context = TFD.daily_context(attached,now)
        self.assertEqual(daily_context['status'],'OK')
        self.assertEqual(daily_context['periods']['200']['sample_count'],200)
        self.assertEqual(daily_context['periods']['200']['value'],100.)
        self.assertEqual(daily_context['daily_asof_basis'],'PROVIDER_DATE_LABEL_ONLY')
        self.assertIsNone(daily_context['provenance']['last_closed_at'])

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
