"""Cold-start history recovery and independent published local candidates."""
import copy
import threading
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import veritas_market_history as H
import veritas_trend_entry as T
import veritas_intelligence as I
import veritas_portfolio_runtime as R
import veritas_execution as X
import veritas_quote_time as QT
from test_veritas_fresh_structure import page
from test_veritas_minute_entry import row, data, NOW, Frozen


class ColdHistory(unittest.TestCase):
    def test_parallel_backfill_does_not_serialize_three_required_hours(self):
        # Sequential retrieval cannot pass this barrier: the three required
        # historical hours have to be in flight together within one budget.
        barrier=threading.Barrier(3)
        end=int(NOW.timestamp())//300*300
        def fetch(params, remaining):
            right=datetime.fromisoformat(params['till']).replace(tzinfo=H.MOSCOW).timestamp()
            if 3600 <= end-right <= 10860:
                barrier.wait(timeout=1)
            return page(params,remaining)
        out=H.recent_moex_minutes(fetch,end,workers=4,budget_seconds=2)
        self.assertTrue(out['history_ready'],out.get('error'))
        self.assertGreaterEqual(out['contiguous_bars'],36)
        self.assertTrue(T.build_context(out['bars'],end,'MOEX')['status']=='OK')

    def test_one_missing_minute_is_never_fabricated_and_can_be_recovered(self):
        end=int(NOW.timestamp())//300*300
        missing=end-45*60
        def fetch(params,remaining):
            return [r for r in page(params,remaining) if H.moex_minute(r)['ts']!=missing]
        out=H.recent_moex_minutes(fetch,end,workers=4)
        self.assertTrue(out['fresh'])
        self.assertFalse(out['history_ready'])
        self.assertEqual(out['history_reason'],'RECENT_CANDLE_GAP')
        self.assertNotIn(missing,[r['ts'] for r in out['minutes']])
        fixed=H.recent_moex_minutes(page,end,out['minutes'],workers=4)
        self.assertTrue(fixed['history_ready'])
        self.assertIn(missing,[r['ts'] for r in fixed['minutes']])

    def test_24_bar_cache_is_retried_before_regular_60_second_ttl(self):
        bars,minutes=data();partial=bars[-24:]
        full=H.recent_moex_minutes(page,NOW.timestamp(),workers=4)
        cache=dict(at=NOW.timestamp()-10,bars=partial,minutes=minutes)
        with patch.object(I,'_v90r16_moex5_cache',cache),patch.object(I.time,'time',return_value=NOW.timestamp()), \
             patch.object(I.httpx,'Client'),patch.object(H,'recent_moex_minutes',return_value=full) as fetch:
            result=I._v90r16_moex_index_5m()
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.kwargs['workers'],4)
        self.assertTrue(H.local_history_status(result)['history_ready'])
        self.assertTrue(cache['history_ready'])

    def test_short_history_and_session_gap_have_different_diagnostics(self):
        bars,_=data()
        short=T.build_context(bars[-24:],NOW,'MOEX')
        gap=T.build_context(bars[:-12]+bars[-11:],NOW,'MOEX')
        for ctx,reason in ((short,'HISTORY_TOO_SHORT'),(gap,'RECENT_CANDLE_GAP')):
            gate=T.context_gate({'trend_entry_context':ctx},NOW)
            self.assertFalse(gate['eligible'])
            self.assertEqual(gate['history_reason'],reason)
            self.assertEqual(gate['required_bars'],36)
        self.assertEqual(short['bars'],24)
        self.assertEqual(gap['contiguous_bars'],11)

    def test_base_bundle_failure_releases_executor(self):
        pool=MagicMock();pool.submit.return_value.result.side_effect=TimeoutError('base history')
        with patch.object(I,'ThreadPoolExecutor',return_value=pool):
            with self.assertRaises(TimeoutError):I._moex_market()
        pool.shutdown.assert_called_once_with(wait=False,cancel_futures=True)


class OpposingLocalCandidate(unittest.TestCase):
    def select(self,local,old,mode='CORE'):
        with patch.object(R,'datetime',Frozen), \
             patch.object(R,'_v90r65_base_transition_book',return_value={'CNYRUBF':old}):
            return R._v90_trend_transition_candidate_book([old,local],{'CNYRUBF':old},mode)['CNYRUBF']

    def test_fresh_published_long_replaces_senior_short_in_every_paper_book(self):
        for mode in ('CORE','AGGRESSIVE','IMPULSE_ONLY','CHALLENGER'):
            long=row('CNYRUBF');short=row('CNYRUBF','SHORT','1h')
            old=copy.deepcopy(short)
            selected=self.select(long,short,mode)
            self.assertEqual(selected['research_decision'],'LONG')
            self.assertEqual(selected['horizon'],'1m')
            self.assertEqual(selected['_r77_prior_candidate_direction'],'SHORT')
            self.assertEqual(selected['trade_plan']['stop_price'],long['trend_entry_context']['event']['stop_price'])
            self.assertGreater(selected['trade_plan']['target_price'],selected['price'])
            self.assertEqual(short,old)
            with patch.object(R,'datetime',Frozen),patch.object(X,'datetime',Frozen),patch.object(QT,'datetime',Frozen):
                admission=R._signal_first_admission(selected,dict(mode=mode,max_fraction=5),0)
            self.assertTrue(admission['open'],admission)

    def test_stale_unconfirmed_or_unpublished_long_cannot_flip_direction(self):
        for defect in ('old','weak','no_direction','opposed'):
            local=row('CNYRUBF');short=row('CNYRUBF','SHORT','1h')
            if defect=='old':local['trend_entry_context']['event']['signal_at']-=600
            elif defect=='weak':local['trend_entry_context']['event']['activity_confirmed']=False
            elif defect=='no_direction':local['research_decision']='NO_TRADE'
            else:local['trend_entry_context']['event']['direction']='SHORT'
            self.assertEqual(self.select(local,short)['research_decision'],'SHORT',defect)

    def test_selected_long_still_requires_fresh_quote_and_economics(self):
        for defect in ('quote','economics','hard_veto'):
            local=row('CNYRUBF');short=row('CNYRUBF','SHORT','1h')
            if defect=='quote':local['market_observed_at']=NOW.timestamp()-960
            elif defect=='economics':local['trend_entry_context']['levels']=[dict(price=local['price']+.05,kind='resistance',timeframe='1h')]
            else:local['trade_plan']['trade_integrity']={'hard_invalidation':True}
            selected=self.select(local,short)
            if defect=='hard_veto':
                self.assertEqual(selected['research_decision'],'SHORT')
                continue
            with patch.object(R,'datetime',Frozen),patch.object(X,'datetime',Frozen),patch.object(QT,'datetime',Frozen):
                admission=R._signal_first_admission(selected,dict(mode='AGGRESSIVE',max_fraction=5),0)
            self.assertFalse(admission['open'],defect)

    def test_neutral_row_does_not_claim_an_opposite_direction(self):
        local=row('CNYRUBF');gate=T.event_gate(local,local['price'],'NO_TRADE',NOW)
        self.assertEqual(gate['reason'],'R77_NO_ENTRY_DIRECTION')
        self.assertEqual(gate['local_direction'],'LONG')

    def test_stale_minute_candidate_cannot_mask_fresh_five_minute_long(self):
        short=row('CNYRUBF','SHORT');short['trend_entry_context']['event']['signal_at']-=600
        selected=self.select(row('CNYRUBF',h='5m'),short)
        self.assertEqual(selected['research_decision'],'LONG')
        self.assertEqual(selected['horizon'],'5m')


if __name__=='__main__':unittest.main()
