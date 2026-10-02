import copy
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from unittest.mock import MagicMock,patch
from contextlib import ExitStack
import unittest

import veritas_trend_entry as T
import veritas_quote_time as QT
import veritas_execution as X
import veritas_portfolio as P
import veritas_portfolio_runtime as R
import veritas_intelligence as I
import veritas_profinance as PF

NOW=datetime(2026,10,2,12,0,tzinfo=timezone.utc)


def row(direction='LONG'):
    return {'asset':'BTC','horizon':'5m','price':100.,'research_decision':direction,
            'market_observed_at':NOW.isoformat(),'best_bid':99.99,'best_ask':100.01,
            'trade_plan':{'stop_price':99 if direction=='LONG' else 101,
                          'target_price':105 if direction=='LONG' else 95,
                          'expected_move_pct':.05,'expected_to_stop_ratio':5},
            'trend_entry_context':{'status':'OK','closed_at':NOW.timestamp(),'atr':.25,
                'last_two_closes':[100,100], 'levels':[],'event':{
                'direction':direction,'trigger_level':99.8 if direction=='LONG' else 100.2,
                'signal_price':100,'signal_at':NOW.timestamp()-300,'atr':.25,
                'stop_price':99 if direction=='LONG' else 101,'bars_since_signal':1,
                'event_id':'same-origin','confirmed_at':NOW.timestamp(),
                'continuation_confirmed':True,'held_bars':1}}}


class ClosedEventTests(unittest.TestCase):
    def bars(self):
        start=NOW.timestamp()-300*100
        return [{'ts':start+300*i,'open':100.,'high':100.1,'low':99.9,'close':100.,'volume':100.} for i in range(101)]

    def test_partial_and_future_candles_cannot_change_context(self):
        bars=self.bars();a=T.build_context(bars[:100],NOW,'BTC')
        bars[-1].update(close=150,high=151,volume=1e9)
        self.assertEqual(a,T.build_context(bars,NOW,'BTC'))

    def test_higher_timeframe_buckets_require_all_candles(self):
        bars=T.closed_bars(self.bars(),NOW)
        full=T.aggregate(bars,3600)
        broken=T.aggregate(bars[:-2]+bars[-1:],3600)
        self.assertLess(len(broken),len(full))
        self.assertTrue(all(b['available_at']<=NOW.timestamp() for b in full))

    def test_confirmed_pivot_is_not_visible_two_bars_early(self):
        bars=[dict(high=x,low=x-1,ts=i,available_at=i+1) for i,x in enumerate([1,2,5,3,2])]
        self.assertEqual(T.confirmed_levels(bars[:4],'1h'),[])
        self.assertEqual(T.confirmed_levels(bars,'1h')[0]['available_at'],5)

    def test_trigger_is_not_rebased_to_latest_price(self):
        r=row();self.assertTrue(T.event_gate(r,100,'LONG',NOW)['eligible'])
        self.assertFalse(T.event_gate(r,101,'LONG',NOW)['eligible'])
        self.assertEqual(r['trend_entry_context']['event']['trigger_level'],99.8)

    def test_old_and_opposite_events_cannot_start_risk(self):
        r=row();r['trend_entry_context']['event']['bars_since_signal']=6
        self.assertFalse(T.event_gate(r,100,'LONG',NOW)['eligible'])
        self.assertFalse(T.event_gate(row(),100,'SHORT',NOW)['eligible'])

    def test_wide_price_range_is_not_volume_confirmation(self):
        bars=self.bars()[:80]
        raw={'asset':'BTC','price':100,'intraday_bars':bars}
        structure={'5m':{'state':'BREAKOUT_ENTRY','volatility_expansion_ratio':3.,'quality_score':.8}}
        with patch.object(I,'_v90_base_features',return_value={}),\
             patch.object(I,'_v90_5m_horizon_structure',return_value={'direction':'LONG','score':.8}):
            f=I._v90_5m_features(raw,{'structure_breakout_grid':structure})
        self.assertEqual(f['intraday_structure']['relative_volume'],1.)
        self.assertFalse(f['intraday_structure']['volume_confirmed'])


class GeometryTests(unittest.TestCase):
    def test_near_senior_obstacle_cannot_be_skipped_for_a_distant_target(self):
        for direction,level in [('LONG',100.3),('SHORT',99.7)]:
            r=row(direction);r['trend_entry_context']['levels']=[{'price':level,'timeframe':'4h','kind':'resistance' if direction=='LONG' else 'support'}]
            g=T.geometry(r);self.assertAlmostEqual(g['remaining_move_pct'],.003)
            self.assertEqual(g['target_price'],level)

    def test_crossed_senior_level_needs_closed_bar_hold(self):
        r=row();r['trend_entry_context']['levels']=[{'price':100.2,'kind':'resistance','timeframe':'1h'}]
        self.assertFalse(T.geometry(r,100.3)['eligible'])
        r['trend_entry_context']['last_two_closes']=[100.25,100.3]
        self.assertTrue(T.geometry(r,100.3)['eligible'])

    def test_missing_target_is_not_fabricated(self):
        r=row();r['trade_plan'].pop('target_price');self.assertFalse(T.geometry(r)['eligible'])

    def test_final_setup_override_cannot_restore_target_beyond_senior_barrier(self):
        r=row();r['trend_entry_context']['levels']=[{'price':100.3,'kind':'resistance','timeframe':'4h'}]
        p=dict(r['trade_plan'],entry_price=100.,horizon='5m',setup='TACTICAL_REVERSAL',
               tactical_target_price=110.,trend_entry_context=r['trend_entry_context'],eligible=True)
        final=I.final_execution_safety('BTC','LONG',p)
        self.assertEqual(final['target_price'],100.3)
        self.assertFalse(final['final_economics_gate']['eligible'])

    def test_compaction_retains_execution_context(self):
        r=row();r['trade_plan']['trend_entry_context']=r.pop('trend_entry_context')
        small=I._v90_compact_live_row(r)
        self.assertEqual(T.context_of(small)['event']['event_id'],'same-origin')


class ScaleAndStopTests(unittest.TestCase):
    def position(self):
        return {'asset':'BTC','direction':'LONG','units':5000.,'avg_entry_price':100.,
                'stop_price':100.1,'opened_at':(NOW-timedelta(minutes=10)).isoformat(),
                'payload':{'opening_fraction':.5,'initial_stop_price':99.}}

    def test_distinct_confirmations_allow_50_75_100_and_same_bar_cannot_repeat(self):
        z=self.position();r=row();r['price']=100.6;r['trade_plan']['target_price']=104
        first=T.scale_decision(z,r,100.6,5,1e6);self.assertTrue(first['eligible']);self.assertEqual(first['fraction'],.75)
        z['units']=.75e6/100.6;z['payload']['r66_last_confirmation_at']=NOW.timestamp()
        self.assertFalse(T.scale_decision(z,r,100.6,5,1e6)['eligible'])
        r['trend_entry_context']['event']['confirmed_at']+=300
        second=T.scale_decision(z,r,100.6,5,1e6);self.assertTrue(second['eligible']);self.assertEqual(second['fraction'],1.)

    def test_losing_position_or_partial_take_cannot_be_refilled(self):
        z=self.position();r=row()
        self.assertFalse(T.scale_decision(z,r,99.8,2,1e6)['eligible'])
        z['payload']['r17_tp1_done']=True
        self.assertFalse(T.scale_decision(z,r,101,2,1e6)['eligible'])

    def test_add_total_stop_risk_is_bounded(self):
        z=self.position();z['stop_price']=90
        result=T.scale_decision(z,row(),101,5,1e6,risk_cap=.005)
        self.assertFalse(result['eligible'])

    def test_trailing_never_widens_or_arms_on_one_profitable_tick(self):
        z=self.position();r=row();r['trend_entry_context']['local_support']=100.5
        self.assertIsNone(T.trailing_stop(z,r,100.1))
        stop=T.trailing_stop(z,r,101.5);self.assertGreater(stop,z['stop_price']);self.assertLess(stop,101.5)
        z['stop_price']=100.8;self.assertIsNone(T.trailing_stop(z,r,101.5))


class ExecutionIntegrationTests(unittest.TestCase):
    def test_profinance_exact_futures_label_and_quote_time(self):
        text='1;I=1;S=NASD100;LP=31000;T=15:00:00\n1;I=57;S=NASD100_FUT;LP=31084.5;T=14:59:57\n1;I=27;S=Brent oil;LP=99.56;T=14:59:54'
        q=PF.parse_quotes(text,NOW)
        self.assertEqual(q['NQ']['price'],31084.5)
        self.assertEqual(q['NQ']['observed_at'],'2026-10-02T11:59:57+00:00')
        self.assertFalse(q['NQ']['execution_eligible'])
        self.assertEqual(q['BRENT']['price'],99.56)
        self.assertEqual(PF.parse_quotes(text,NOW+timedelta(minutes=4)),{})

    def test_quote_age_is_execution_specific(self):
        old=(NOW-timedelta(seconds=84)).isoformat()
        self.assertTrue(QT.quote_gate(old,'1h',NOW)['eligible'])
        self.assertFalse(QT.quote_gate(old,'1h',NOW,execution=True,asset='ETH')['eligible'])
        self.assertFalse(QT.quote_gate((NOW-timedelta(seconds=941)).isoformat(),now=NOW,execution=True,asset='BRENT')['eligible'])

    def test_stale_refresh_failure_does_not_open(self):
        r=row();r['market_observed_at']=(NOW-timedelta(seconds=84)).isoformat()
        c=MagicMock();c.execute.return_value.fetchone.return_value=None
        with patch.object(R,'_v90r65_base_open_or_add') as mutation:
            R._open_or_add(c,{},'Champion','ETH','LONG',100,.05,1e6,NOW.isoformat(),r,'test')
        mutation.assert_not_called()

    def test_opposite_held_direction_is_reported_and_not_ordered(self):
        r=row();r['_rank']=1
        position={'asset':'BTC','direction':'SHORT','units':1000.,'last_price':100.,'avg_entry_price':100.,
                  'stop_price':110.,'payload':{},'opened_at':NOW.isoformat()}
        book={'high_water_nav_rub':1e6,'benchmark_nav_rub':1e6,'last_mark_at':None}
        c=MagicMock();c.execute.return_value.fetchone.return_value=position
        with ExitStack() as s:
            for key,value in {'_portfolio_rows':(book,[position]),'_mark_nav':(1e6,0,.1,-.1),
               '_apply_funding':0,'_risk_governor':{'new_risk':True,'max_gross':2},'_desired_fraction':.05,
               '_v842_management_row':{},'_v842_hard_thesis_exit':False,'_v90_structure_exit_signal':False,'_stats':{}}.items():
                s.enter_context(patch.object(P,key,return_value=value))
            mutation=s.enter_context(patch.object(P,'_open_or_add'))
            P._v90j_base_step_one(c,'Champion',P.POLICIES['Champion'],{'BTC':r},{'BTC':100},14,83,NOW.isoformat(),.0005,[r])
        mutation.assert_not_called()
        self.assertEqual(r['_execution_audit']['reason'],'DIRECTION_FLIP_NOT_CONFIRMED')
        self.assertEqual(r['_execution_audit']['held_direction'],'SHORT')


if __name__=='__main__':unittest.main()
