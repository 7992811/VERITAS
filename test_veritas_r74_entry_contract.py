"""Entry-contract regressions, not a strategy profitability backtest."""
import copy
import json
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
import unittest

import veritas_trend_entry as T
import veritas_execution as X
import veritas_intelligence as I
import veritas_portfolio_runtime as R
import veritas_quote_time as QT
from test_veritas_minute_entry import row, data, NOW, Frozen


class CoherentPlans(unittest.TestCase):
    def test_old_reversal_cannot_replace_local_stop_or_target(self):
        for direction in ('LONG','SHORT'):
            r=row('GOLD',direction,'3d');ev=T.context_of(r)['event']
            r['trade_plan'].update(stop_price=80 if direction=='LONG' else 150,
                target_price=r['price']+(.01 if direction=='LONG' else -.01),
                tactical_target_price=200 if direction=='LONG' else 10,
                expected_move_pct=.00001,take_profit_1={'price':777})
            p=T.prepare_row(r)['trade_plan']
            sign=1 if direction=='LONG' else -1
            self.assertEqual(p['stop_price'],ev['stop_price'])
            self.assertAlmostEqual(p['target_price'],ev['signal_price']+sign*2*abs(ev['signal_price']-ev['stop_price']))
            self.assertEqual(p['tactical_target_price'],p['target_price'])
            self.assertEqual(p['take_profit_1']['price'],p['target_price'])
            self.assertGreater(p['expected_move_pct'],.01)
            self.assertEqual(p['entry_event_id'],ev['event_id'])

    def test_projection_and_identity_do_not_move_with_price(self):
        r=row();a=T.prepare_row(r)['trade_plan'];b=T.prepare_row(r,r['price']+.2)['trade_plan']
        self.assertEqual(a['target_price'],b['target_price'])
        self.assertEqual(a['entry_event_id'],b['entry_event_id'])
        self.assertLess(b['expected_move_pct'],a['expected_move_pct'])

    def test_consumed_target_does_not_generate_a_new_distant_target(self):
        r=row();target=T.geometry(r)['target_price']
        self.assertEqual(T.geometry(r,target+.1)['reason'],'R74_EVENT_TARGET_REACHED')
        with patch.object(X,'datetime',Frozen),patch.object(QT,'datetime',Frozen):
            g=X.entry_gate(r,target+.1,'LONG',.1)
        self.assertFalse(g['eligible'])
        self.assertIn('R74_EVENT_TARGET_REACHED',g['blockers'])

    def test_nearest_level_remains_authoritative(self):
        r=row();level=r['price']+.05
        r['trend_entry_context']['levels']=[dict(price=level,kind='resistance',timeframe='1h')]
        p=T.prepare_row(r)['trade_plan']
        self.assertEqual(p['target_price'],level)
        self.assertFalse(X.economics_gate('BTC',dict(p,entry_price=r['price'],direction='LONG'))['eligible'])

    def test_existing_stop_override_is_never_widened_by_new_event(self):
        r=row();held_stop=r['price']-.2
        self.assertEqual(T.geometry(r,stop_override=held_stop)['stop_price'],held_stop)

    def test_final_overlay_resets_only_obsolete_plan_state(self):
        r=row('GOLD','SHORT','3d');p=dict(r['trade_plan'],eligible=False,reason='invalidated',
            entry_quality='INVALIDATED',horizon='3d',market_observed_at=NOW.isoformat(),
            trend_entry_context=r['trend_entry_context'],
            profitability_gate={'status':'NOT_APPLICABLE','allow':False},
            trade_integrity={'hard_invalidation':False,'soft_reasons':['OLD_OR_CURRENT_SETUP_FAILURE']})
        with patch.object(I,'datetime',Frozen),patch.object(QT,'datetime',Frozen):
            p=I.final_execution_safety('GOLD','SHORT',p)
        self.assertTrue(p['eligible'],p['final_economics_gate'])
        self.assertEqual(p['entry_quality'],'FRESH_BREAKOUT')
        self.assertEqual(p['trade_integrity']['soft_reasons'],[])
        self.assertNotEqual(I.trade_decision_stage('SHORT',p,{}, {'entry_quality':'INVALIDATED'}),'INVALIDATED')

    def test_hard_invalidation_negative_history_and_reentry_are_preserved(self):
        for veto in ({'trade_integrity':{'hard_invalidation':True}},
                     {'profitability_gate':{'status':'NEGATIVE_EDGE','allow':False}},
                     {'reentry_intelligence':{'allowed':False}},
                     {'trade_integrity':{'fast_tf_conflict':True}}):
            r=row();p=dict(r['trade_plan'],eligible=False,reason='invalidated',horizon='1m',
                market_observed_at=NOW.isoformat(),trend_entry_context=r['trend_entry_context'],**veto)
            with patch.object(I,'datetime',Frozen),patch.object(QT,'datetime',Frozen):
                p=I.final_execution_safety('BTC','LONG',p)
            self.assertFalse(p['eligible'],veto)

    def test_not_applicable_history_does_not_veto_an_otherwise_valid_entry(self):
        for mode in ('CORE','AGGRESSIVE','CHALLENGER','IMPULSE_ONLY'):
            r=row();r['trade_plan'].update(eligible=True,profitability_gate={'status':'NOT_APPLICABLE','allow':False})
            with patch.object(R,'datetime',Frozen),patch.object(X,'datetime',Frozen),patch.object(QT,'datetime',Frozen):
                result=R._signal_first_admission(r,dict(mode=mode,max_fraction=5),0)
            self.assertTrue(result['open'],result)

    def test_new_quote_price_cannot_use_old_signal_distance(self):
        r=row();r['_execution_quote']={'price':102.,'observed_at':NOW.isoformat()}
        with patch.object(R,'datetime',Frozen):out=R._signal_first_admission(r,dict(mode='CORE',max_fraction=1),0)
        self.assertFalse(out['open']);self.assertEqual(out['reason'],'R66_WAIT_RETEST')


class TimingContract(unittest.TestCase):
    def test_five_minute_close_has_two_bar_validity(self):
        r=row(h='1h');r['trend_entry_context']['event']['confirmation']='5m_CLOSE'
        for seconds,allowed in ((180,True),(600,True),(601,False)):
            out=T.event_gate(r,r['price'],'LONG',NOW+timedelta(seconds=seconds))
            self.assertEqual(out['eligible'],allowed,out)

    def test_minute_event_does_not_inherit_hourly_validity(self):
        r=row(h='7d')
        self.assertFalse(T.event_gate(r,r['price'],'LONG',NOW+timedelta(seconds=121))['eligible'])

    def test_five_minute_retest_requires_past_closed_confirmation(self):
        r=row(h='1h');ev=r['trend_entry_context']['event'];ev['signal_at']-=1200
        ev.update(retest_at=NOW.timestamp()-180,retest_resolution_seconds=300)
        self.assertTrue(T.event_gate(r,r['price'],'LONG',NOW)['eligible'])
        ev['retest_at']=NOW.timestamp()+1
        self.assertFalse(T.event_gate(r,r['price'],'LONG',NOW)['eligible'])

    def test_quote_freshness_not_relaxed_with_event_window(self):
        r=row('CNYRUBF',h='1h');r['trend_entry_context']['event']['confirmation']='5m_CLOSE'
        r['market_observed_at']=(NOW-timedelta(minutes=16)).isoformat()
        with patch.object(X,'datetime',Frozen),patch.object(QT,'datetime',Frozen):
            out=X.entry_gate(r,r['price'],'LONG',.1)
        self.assertFalse(out['eligible']);self.assertIn('QUOTE_TOO_OLD_FOR_HORIZON',out['blockers'])

    def test_missing_local_event_is_reported_in_final_plan(self):
        r=row('GOLD',h='3d');r['trend_entry_context']['event']=None
        p=dict(r['trade_plan'],eligible=True,trend_entry_context=r['trend_entry_context'],horizon='3d')
        with patch.object(I,'datetime',Frozen):p=I.final_execution_safety('GOLD','LONG',p)
        self.assertFalse(p['eligible']);self.assertFalse(p['execution_levels_ready'])
        self.assertEqual(p['entry_timing_gate']['reason'],'R69_WAIT_LOCAL_BREAKOUT')

    def test_compaction_retains_all_diagnostics(self):
        r=row();p=r['trade_plan'];p.update(entry_event_id='R69_test',entry_plan_version=T.PLAN_VERSION,
            entry_timing_gate={'eligible':False,'reason':'R66_WAIT_RETEST'},
            execution_quote_gate={'eligible':False,'age_seconds':960},execution_levels_ready=False)
        compact=I._v90_compact_live_row(r)['trade_plan']
        for key in ('entry_event_id','entry_plan_version','entry_timing_gate','execution_quote_gate','execution_levels_ready'):
            self.assertEqual(compact[key],p[key])


class ObservedCases(unittest.TestCase):
    def test_october_fifth_cases_remain_blocked_without_fabricating_edge(self):
        fixture=json.loads((Path(__file__).parent/'tests/fixtures/r74_blocked_entries.json').read_text())
        for r in fixture['signals']:
            before=copy.deepcopy(r);p=T.prepare_row(r)['trade_plan']
            self.assertFalse(X.economics_gate(r['asset'],p)['eligible'])
            self.assertFalse(T.event_gate(r,r['price'],r['research_decision'],fixture['observed_at'])['eligible'])
            self.assertEqual(before,r)
            if r['asset']=='CNYRUBF':
                self.assertEqual(p['target_price'],12.593)
                self.assertAlmostEqual(p['stop_price'],12.53913)
                self.assertGreater(p['target_price'],r['trade_plan']['target_price'])


class ContinuationIdentity(unittest.TestCase):
    def series(self,count):
        bars,minutes=data()
        for i in range(count):
            op=100.7 if i==0 else 101.8
            minutes.append(dict(ts=NOW.timestamp()+60*i,open=op,high=101.9,
                low=min(op,101.7),close=101.8,volume=100.))
        return bars,minutes

    def test_running_price_is_not_a_new_event(self):
        bars,minutes=self.series(10)
        original=T.build_context(bars,NOW,'BTC',minute_bars=minutes)['event']
        later=T.build_context(bars,NOW+timedelta(minutes=10),'BTC',minute_bars=minutes)['event']
        self.assertEqual(original['event_id'],later['event_id'])
        self.assertEqual(original['trigger_level'],later['trigger_level'])

    def test_closed_new_base_can_create_distinct_continuation(self):
        bars,minutes=self.series(80)
        original=T.build_context(bars,NOW,'BTC',minute_bars=minutes)['event']
        at=NOW+timedelta(minutes=80)
        minutes.append(dict(ts=at.timestamp(),open=101.8,high=102.1,low=101.75,close=102.05,volume=200.))
        end=at+timedelta(minutes=1)
        context=T.build_context(bars,end,'BTC',minute_bars=minutes)
        event=context['event']
        self.assertEqual(event['event_type'],'LOCAL_CONTINUATION_BREAKOUT')
        self.assertEqual(event['parent_event_id'],original['event_id'])
        self.assertNotEqual(event['event_id'],original['event_id'])
        self.assertAlmostEqual(event['trigger_level'],101.9)
        self.assertGreater(event['stop_price'],original['stop_price'])
        minutes.append(dict(minutes[-1],ts=end.timestamp(),high=150,close=150))
        self.assertEqual(context,T.build_context(bars,end,'BTC',minute_bars=minutes))


if __name__=='__main__':unittest.main()
