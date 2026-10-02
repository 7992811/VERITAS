import copy
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import unittest

import veritas_trend_entry as T
import veritas_portfolio as P
import veritas_portfolio_runtime as R
import veritas_intelligence as I
import veritas_execution as X
import veritas_position_guard as G

DATA=json.loads((Path(__file__).parent/'tests/fixtures/nq_20261002_breakout.json').read_text())


def clock(hm):
    return datetime.fromisoformat('2026-10-02T'+hm+':00+00:00')


def case(hm='12:31',price=31047.25,horizon='4h'):
    c=T.build_context(DATA['5m'],clock(hm),'NQ',DATA['1m'])
    return {'asset':'NQ','horizon':horizon,'price':price,'research_decision':'LONG',
            'market_observed_at':clock(hm).isoformat(),'trend_entry_context':c,
            'trade_plan':{'stop_price':30000.,'target_price':32500.,'expected_move_pct':.05}}


class LocalBreakoutTests(unittest.TestCase):
    def test_protection_uses_fresh_futures_quote_and_rejects_index_or_stale_quote(self):
        now=datetime.now(timezone.utc)
        q={'raw_label':'NASD100_FUT','price':31040.,'observed_at':now.isoformat(),
           'execution_eligible':False,'reference_only':True}
        yahoo=MagicMock(return_value=([{'ts':now.timestamp()-900,'close':31000.}],{}))
        fetch=MagicMock(return_value=q)
        ns={'_v90r61_profinance_quote':fetch,'_yahoo_series':yahoo}
        result=G.fetch_guard_quote(ns,'NQ',[])
        self.assertEqual(result['price'],31040.)
        self.assertTrue(result['paper_only']);self.assertFalse(result['execution_eligible'])
        yahoo.assert_not_called()
        for change in ({'raw_label':'NASD100'},{'observed_at':(now-timedelta(minutes=5)).isoformat()}):
            fetch.return_value=dict(q,**change)
            result=G.fetch_guard_quote(ns,'NQ',[])
            self.assertEqual(result['price'],31000.)
            self.assertFalse(G.quote_gate(result['observed_at'],execution=True,asset='NQ')['eligible'])

    def test_historical_minute_trigger_precedes_five_minute_overshoot(self):
        before=case('12:29',30990)
        self.assertIsNone(T.context_of(before)['event'])
        r=case();e=T.context_of(r)['event']
        self.assertEqual(e['trigger_level'],31035.5)
        self.assertEqual(e['signal_at'],clock('12:31').timestamp())
        self.assertEqual(e['impulse_origin'],30913.25)
        self.assertLess(e['stop_price'],e['impulse_origin'])
        self.assertTrue(T.event_gate(r,31047.25,'LONG',clock('12:31'))['eligible'])
        self.assertFalse(T.event_gate(case('12:35',31118.25),31118.25,'LONG',clock('12:35'))['eligible'])

    def test_original_breakout_does_not_move_to_later_highs(self):
        early=T.context_of(case())['event']
        for h in ('5m','1h','4h','1d','7d'):
            r=case('13:22',31205.09,h);e=T.context_of(r)['event']
            self.assertEqual((e['event_id'],e['trigger_level'],e['stop_price']),
                             (early['event_id'],early['trigger_level'],early['stop_price']))
            self.assertFalse(T.event_gate(r,31205.09,'LONG',clock('13:22'))['eligible'])

    def test_late_candle_without_minutes_is_still_remembered(self):
        c=T.build_context(DATA['5m'],clock('13:22'),'NQ')
        self.assertEqual(c['event']['trigger_level'],31035.5)
        self.assertEqual(c['event']['confirmation'],'5m_CLOSE')
        self.assertFalse(T.event_gate({'asset':'NQ','trend_entry_context':c},31205,'LONG',clock('13:22'))['eligible'])

    def test_fresh_minutes_advance_cached_five_minute_history(self):
        cached=[b for b in DATA['5m'] if b['ts']<clock('12:25').timestamp()]
        c=T.build_context(cached,clock('13:22'),'NQ',DATA['1m'])
        self.assertEqual(c['closed_at'],clock('13:20').timestamp())
        self.assertEqual(c['event']['trigger_level'],31035.5)
        self.assertEqual(c['event']['signal_at'],clock('12:31').timestamp())

    def test_prefix_invariance_and_partial_bars(self):
        for hm in ('12:29','12:31','12:35','13:22'):
            now=clock(hm);end=now.timestamp()
            expected=T.build_context(DATA['5m'],now,'NQ',DATA['1m'])
            actual=T.build_context([b for b in DATA['5m'] if b['ts']+300<=end],now,'NQ',
                                   [b for b in DATA['1m'] if b['ts']+60<=end])
            self.assertEqual(expected,actual)
        bars=copy.deepcopy(DATA['5m'])
        bars.append(dict(ts=clock('12:30').timestamp()+32,open=999,high=999,low=999,close=999))
        self.assertEqual(T.build_context(bars,clock('13:22'),'NQ',DATA['1m']),
                         T.build_context(DATA['5m'],clock('13:22'),'NQ',DATA['1m']))

    def test_proxy_and_quote_anchors_do_not_become_structure(self):
        bars=copy.deepcopy(DATA['5m'])
        for b in bars[-10:]:b['source']='PROXY_TIMING_BRIDGE'
        closed=T.closed_bars(bars,clock('14:00'))
        self.assertTrue(all(b['ts']<bars[-10]['ts'] for b in closed))
        r={'asset':'NQ','trend_entry_context':{'status':'INSUFFICIENT'}}
        self.assertFalse(T.event_gate(r,100,'LONG',clock('14:00'))['eligible'])
        self.assertFalse(T.event_gate({'asset':'NQ'},100,'LONG',clock('14:00'))['eligible'])
        self.assertFalse(T.event_gate(dict(case(),source_names={'primary':'QQQ proxy bridge'}),
                                      31047,'LONG',clock('12:31'))['eligible'])

    def test_same_logic_for_short_and_scaled_prices(self):
        def reflect(b):
            return dict(b,open=65000-b['open'],close=65000-b['close'],
                        high=65000-b['low'],low=65000-b['high'])
        c=T.build_context([reflect(b) for b in DATA['5m']],clock('12:31'),'NQ',
                          [reflect(b) for b in DATA['1m']])
        self.assertEqual(c['event']['direction'],'SHORT')
        self.assertEqual(c['event']['trigger_level'],65000-31035.5)
        self.assertTrue(T.event_gate({'asset':'NQ','trend_entry_context':c},65000-31047.25,'SHORT',clock('12:31'))['eligible'])
        def scaled(b):return dict(b,**{k:b[k]/100 for k in ('open','high','low','close')})
        c=T.build_context([scaled(b) for b in DATA['5m']],clock('12:31'),'NQ',[scaled(b) for b in DATA['1m']])
        self.assertAlmostEqual(c['event']['trigger_level'],310.355)

    def test_direct_quote_can_cross_prepared_level_but_proxy_cannot(self):
        now=clock('12:30')+timedelta(seconds=10)
        q={'price':31040.,'observed_at':now.isoformat(),'direct':True}
        c=T.build_context(DATA['5m'],now,'NQ',quote=q)
        self.assertEqual(c['event']['confirmation'],'LIVE_QUOTE_PROVISIONAL')
        self.assertEqual(c['event']['trigger_level'],31035.5)
        self.assertIsNone(T.build_context(DATA['5m'],now,'NQ',quote=dict(q,direct=False))['event'])
        self.assertIsNone(T.build_context(DATA['5m'],now,'NQ',quote=dict(q,price=31205))['event'])

    def test_structural_stop_and_first_senior_barrier_override_4h_projection(self):
        r=case();g=T.geometry(r)
        self.assertEqual(g['target_price'],31151.5)
        self.assertAlmostEqual(g['stop_price'],30909.831875)
        for h in ('5m','1h','4h'):
            prepared=R._v90r56_prepare_entry_row(dict(r,horizon=h))
            self.assertEqual(prepared['trade_plan']['stop_price'],g['stop_price'])
            self.assertEqual(prepared['trade_plan']['target_price'],g['target_price'])
        plan=T.prepare_row(r)['trade_plan']
        econ=X.economics_gate('NQ',dict(plan,entry_price=r['price'],direction='LONG',initial_position_fraction=.75,horizon='5m'))
        self.assertFalse(econ['eligible'])
        self.assertIn('NET_REWARD_RISK_BELOW_FLOOR',econ['blockers'])

    def test_admission_and_actual_order_reject_missing_or_late_context(self):
        for r in ({'asset':'NQ','price':31205,'research_decision':'LONG'},case('13:22',31205)):
            for policy in P.POLICIES.values():
                with patch.object(R,'_v90r65_base_admission',return_value={'open':True,'fraction':1}):
                    self.assertFalse(R._signal_first_admission(r,policy,0)['open'])
        r=case('13:22',31205);c=MagicMock();c.execute.return_value.fetchone.return_value=None
        with patch.object(R,'_v90r65_base_open_or_add') as mutation:
            R._open_or_add(c,{},'Aggressive','NQ','LONG',31205,.75,1e6,clock('13:22').isoformat(),r,'test')
        mutation.assert_not_called()

    def test_stale_history_and_opposite_event_are_not_entry_triggers(self):
        r=case()
        self.assertFalse(T.event_gate(r,31047,'LONG',clock('12:50'))['eligible'])
        self.assertFalse(T.event_gate(r,31047,'SHORT',clock('12:31'))['eligible'])

    def test_qualified_first_order_keeps_large_allocation_and_caps_wide_stop_risk(self):
        now=datetime.now(timezone.utc)
        r=case();ctx=r['trend_entry_context'];ctx['closed_at']=now.timestamp()
        ctx['event'].update(signal_at=now.timestamp()-60,bars_since_signal=0)
        r['market_observed_at']=now.isoformat()
        c=MagicMock();c.execute.return_value.fetchone.return_value=None
        with patch.object(R,'_v90r65_base_open_or_add') as mutation:
            R._open_or_add(c,{},'Aggressive','NQ','LONG',r['price'],.75,1e6,now.isoformat(),r,'test')
        self.assertAlmostEqual(mutation.call_args.args[6],.75)
        ctx['event']['stop_price']=10000.
        with patch.object(R,'_v90r65_base_open_or_add') as mutation:
            R._open_or_add(c,{},'Aggressive','NQ','LONG',r['price'],.75,1e6,now.isoformat(),r,'test')
        mutation.assert_not_called()


if __name__=='__main__':unittest.main()
