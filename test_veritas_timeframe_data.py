"""Source attachment, completed aggregation and persisted entry provenance."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

import veritas_price_source as VPS
import veritas_timeframe_data as TFD
from test_veritas_timeframe_structure import example


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


if __name__=='__main__':
    unittest.main()
