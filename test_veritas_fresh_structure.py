"""Regressions for fresh quote / stale structure and NQ proxy contamination."""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
import veritas_market_history as H
import veritas_trend_entry as T
import veritas_execution as X
import veritas_position_guard as G
from test_veritas_trend_entry import row, NOW
import veritas_portfolio_runtime as R


def page(params, remaining):
    left=datetime.fromisoformat(params['from']).replace(tzinfo=H.MOSCOW).timestamp()
    right=datetime.fromisoformat(params['till']).replace(tzinfo=H.MOSCOW).timestamp()
    return [{'begin':datetime.fromtimestamp(t,H.MOSCOW).isoformat(),
             'open':100,'high':102,'low':99,'close':101,'value':1000,'volume':0}
            for t in range(int(left),int(right)+1,60)]


class RecentHistoryTests(unittest.TestCase):
    def test_newest_slice_survives_older_timeout(self):
        calls=[]
        def fetch(params, remaining):
            calls.append(params)
            if len(calls)>1:raise TimeoutError('older backfill')
            return page(params,remaining)
        out=H.recent_moex_minutes(fetch,NOW.timestamp())
        self.assertTrue(out['fresh'])
        self.assertEqual(out['last_closed_at'],NOW.timestamp())
        self.assertEqual(len(out['bars']),12)
        self.assertIn('TimeoutError',out['error'])
        self.assertEqual(out['bars'][-1]['volume'],5000)
        self.assertEqual(calls[0]['till'],'2026-10-02 14:59:00')

    def test_hour_slices_have_no_pagination_loss_or_gaps(self):
        out=H.recent_moex_minutes(page,NOW.timestamp(),monotonic=lambda:0)
        self.assertTrue(out['fresh'])
        self.assertEqual(len(out['bars']),500)
        self.assertTrue(all(b['ts']-a['ts']==300 for a,b in zip(out['bars'],out['bars'][1:])))

    def test_partial_bucket_can_complete_on_next_refresh(self):
        now=NOW.timestamp()+120
        params={'from':'2026-10-02 15:00:00','till':'2026-10-02 15:04:00'}
        minutes=[H.moex_minute(x) for x in page(params,1)]
        self.assertEqual(H.complete_five_minutes(minutes,now),[])
        self.assertEqual(len(H.complete_five_minutes(minutes,now+180)),1)
        self.assertEqual(H.complete_five_minutes(minutes[:2]+minutes[3:],now+180),[])

    def test_new_request_does_not_refresh_old_candles(self):
        old=H.recent_moex_minutes(page,NOW.timestamp()-86400,monotonic=lambda:0)
        out=H.recent_moex_minutes(lambda *_:[],NOW.timestamp(),old['minutes'],monotonic=lambda:0)
        self.assertFalse(out['fresh'])
        self.assertGreater(out['history_age_seconds'],900)

    def test_invalid_prices_are_rejected(self):
        sample=page({'from':'2026-10-02 15:00:00','till':'2026-10-02 15:00:00'},1)[0]
        for value in (float('nan'),float('inf'),-1):
            self.assertIsNone(H.moex_minute(dict(sample,close=value)))


class FreshStructureTests(unittest.TestCase):
    def test_stale_context_without_event_cannot_take_legacy_path(self):
        r=row();r['trend_entry_context'].update(closed_at=NOW.timestamp()-1800,event=None)
        self.assertEqual(T.event_gate(r,100,'LONG',NOW)['reason'],'R66_CLOSED_CONTEXT_STALE')

    def test_snapshot_event_age_is_recomputed(self):
        r=row();r['trend_entry_context']['event'].update(signal_at=NOW.timestamp()-1500,bars_since_signal=0)
        gate=T.event_gate(r,100,'LONG',NOW)
        self.assertFalse(gate['eligible']);self.assertEqual(gate['bars_since_signal'],5)

    def test_future_retest_cannot_revive_an_old_event(self):
        r=row();r['trend_entry_context']['event'].update(signal_at=NOW.timestamp()-1500,
                      bars_since_signal=5,retest_at=NOW.timestamp()+60)
        self.assertFalse(T.event_gate(r,100,'LONG',NOW)['eligible'])

    def test_fresh_valid_event_is_admitted_by_preliminary_gate(self):
        with patch.object(R,'datetime') as clock:
            clock.now.return_value=NOW
            self.assertTrue(R._v90r56_late_entry_gate(row(),100)['eligible'])

    def test_same_old_event_is_blocked_by_preliminary_gate(self):
        r=row();r['trend_entry_context']['event'].update(bars_since_signal=11)
        with patch.object(R,'datetime') as clock:
            clock.now.return_value=NOW
            self.assertEqual(R._v90r56_late_entry_gate(r,100)['reason'],'R66_WAIT_RETEST')

    def test_context_marks_old_history_stale(self):
        bars=[dict(ts=NOW.timestamp()-86400+i*300,open=100,high=101,low=99,close=100,volume=10)
              for i in range(100)]
        self.assertEqual(T.build_context(bars,NOW,'MOEX')['status'],'STALE')


class ProxyBoundaryTests(unittest.TestCase):
    def quote(self,primary='QQQ proxy bridge'):
        return {'price':100,'observed_at':NOW.isoformat(),'source_gate_pass':True,
                'market_open':True,'source_names':{'primary':primary}}

    def test_proxy_cannot_supply_entry_mark_or_exit_quote(self):
        q=self.quote()
        self.assertFalse(X.paper_source_gate('NQ',q)['eligible'])
        self.assertEqual(G.latest_prices([dict(q,asset='NQ')],NOW),{})
        self.assertEqual(G.exit_execution_quote({'asset':'NQ','_execution_quote':q},NOW),{})

    def test_proxy_cannot_replace_direct_cached_quote(self):
        with patch.dict(G._quotes,{},clear=True):
            G.publish_quote('NQ',self.quote('ProFinance NASD100_FUT'))
            G.publish_quote('NQ',dict(self.quote(),price=777))
            self.assertEqual(G._quotes['NQ']['price'],100)
            self.assertEqual(G._quotes['NQ']['source_names']['primary'],'ProFinance NASD100_FUT')

    def test_one_direct_source_remains_sufficient(self):
        q=self.quote('ProFinance NASD100_FUT')
        self.assertTrue(X.paper_source_gate('NQ',q)['eligible'])
        self.assertEqual(G.latest_prices([dict(q,asset='NQ')],NOW),{'NQ':100})
        self.assertTrue(G.exit_execution_quote({'asset':'NQ','_execution_quote':q},NOW))


if __name__=='__main__':unittest.main()
