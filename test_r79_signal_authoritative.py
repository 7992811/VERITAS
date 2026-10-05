from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import unittest

import veritas_trend_entry as T
import veritas_portfolio_runtime as R
import veritas_portfolio as P
import veritas_position_guard as VPG


class SignalAuthoritativeR79Tests(unittest.TestCase):
    def row(self, tier='LONG'):
        now=datetime.now(timezone.utc)
        return {
          'asset':'MOEX','horizon':'1h','price':2300.0,
          'decision':'LONG','research_decision':'LONG','signal_tier':tier,
          'investor_signal':'BUY','confidence':.78,
          'source_gate_pass':True,'market_open':True,
          'execution_eligible':True,'paper_eligible':True,'production_eligible':False,
          'market_observed_at':now.isoformat(),
          '_execution_quote':{'price':2300.0,'observed_at':now.isoformat()},
          'horizon_structure':{'direction':'LONG','score':.82,'state':'BUILDING_TREND'},
          'institutional_signal':{'evidence_independence':{'independent_count':5}},
          'trade_plan':{
            'stop_price':2285.0,'target_price':2340.0,
            'expected_move_pct':.01739,'expected_to_stop_ratio':2.66,
            'eligible':False,'reason':'R66_WAIT_RETEST',
          },
          'trend_entry_context':{
            'status':'STALE','closed_at':(now-timedelta(minutes=25)).timestamp(),
            'atr':8.0,'last_close':2298.0,'last_two_closes':[2295.0,2298.0],
            'local_support':2288.0,'local_resistance':2305.0,'levels':[],
            'event':{
              'direction':'LONG','trigger_level':2240.0,'signal_price':2240.0,
              'signal_at':(now-timedelta(hours=2)).timestamp(),'atr':8.0,
              'stop_price':2225.0,'bars_since_signal':24,
              'event_id':'R69_OLD_SIGNAL','activity_confirmed':True,
            }
          }
        }

    def test_displayed_signal_rebases_stale_parent_event(self):
        r=T.prepare_row(self.row(),2300.0,datetime.now(timezone.utc))
        e=T.context_of(r)['event']
        self.assertTrue(e.get('signal_authoritative'),e)
        self.assertTrue(str(e.get('event_id')).startswith('R79_SIG_'),e)
        self.assertEqual(e.get('parent_event_id'),'R69_OLD_SIGNAL')
        self.assertAlmostEqual(e.get('trigger_level'),2300.0)
        self.assertEqual(r['trade_plan']['setup'],'SIGNAL_CONTINUATION')
        self.assertTrue(T.context_gate(r,datetime.now(timezone.utc))['eligible'])
        g=T.event_gate(r,2300.0,'LONG',datetime.now(timezone.utc))
        self.assertTrue(g['eligible'],g)
        self.assertEqual(g['extension_atr'],0.0)

    def test_normal_signal_opens_staged_aggressive_position(self):
        r=self.row('LONG')
        out=R._signal_first_admission(r,P.POLICIES['Aggressive'],0.0)
        self.assertTrue(out.get('open'),out)
        self.assertGreaterEqual(out.get('fraction',0),.50)
        self.assertIn(out.get('reason'),('R79_SIGNAL_ENTRY','R79_SIGNAL_PROBE'))

    def test_super_signal_targets_full_aggressive_start_subject_to_risk_cap(self):
        r=self.row('SUPER_LONG')
        out=R._signal_first_admission(r,P.POLICIES['Aggressive'],0.0)
        self.assertTrue(out.get('open'),out)
        self.assertGreaterEqual(out.get('fraction',0),.50)
        self.assertLessEqual(out.get('fraction',0),1.0)

    def test_weak_economics_signal_still_opens_small_probe(self):
        r=self.row('LONG')
        r['trend_entry_context']['local_support']=2299.0
        r['trade_plan']['stop_price']=2299.0
        r['trade_plan']['target_price']=2300.5
        r['trade_plan']['expected_move_pct']=0.0002
        r['trade_plan']['expected_to_stop_ratio']=0.2
        out=R._signal_first_admission(r,P.POLICIES['Aggressive'],0.0)
        self.assertTrue(out.get('open'),out)
        self.assertEqual(out.get('reason'),'R79_SIGNAL_PROBE')
        self.assertGreater(out.get('fraction',0),0.0)
        self.assertLessEqual(out.get('fraction',0),0.10)

    def test_gold_invalidated_parent_label_does_not_block_current_short(self):
        r=self.row('SHORT')
        r.update({
          'asset':'GOLD','price':4184.0,'research_decision':'SHORT','decision':'SHORT',
          'investor_signal':'SELL','signal_tier':'SHORT','confidence':.60,
          'horizon':'1h','entry_quality':'INVALIDATED','decision_stage':'WAIT_LOCAL_ENTRY',
          'horizon_structure':{'direction':'SHORT','score':.61,'state':'CONFIRMED_TREND'},
        })
        r['_execution_quote']={'price':4184.0,'observed_at':datetime.now(timezone.utc).isoformat()}
        r['trade_plan'].update({'entry_quality':'INVALIDATED','stop_price':4191.0,
                                'target_price':4170.0,'expected_move_pct':.0033,
                                'expected_to_stop_ratio':2.0})
        r['trend_entry_context'].update({
          'status':'STALE',
          'closed_at':(datetime.now(timezone.utc)-timedelta(minutes=20)).timestamp(),
          'local_resistance':4191.0,'local_support':4175.0,
        })
        out=R._signal_first_admission(r,P.POLICIES['Aggressive'],0.0)
        self.assertTrue(out.get('open'),out)
        work=T.prepare_row(r,4184.0,datetime.now(timezone.utc))
        self.assertEqual(work.get('entry_quality'),'CURRENT_SIGNAL')
        self.assertEqual((work.get('trade_plan') or {}).get('entry_quality'),'CURRENT_SIGNAL')

    def test_brent_slow_horizon_signal_rebases_now_not_old_observation(self):
        now=datetime.now(timezone.utc)
        r=self.row('LONG')
        r.update({
          'asset':'BRENT','price':102.65,'research_decision':'LONG','decision':'LONG',
          'investor_signal':'BUY','signal_tier':'LONG','confidence':.18,
          'horizon':'4h','entry_quality':'INVALIDATED','decision_stage':'WAIT_LOCAL_ENTRY',
          'market_observed_at':(now-timedelta(minutes=25)).isoformat(),
          'horizon_structure':{'direction':'LONG','score':.56,'state':'BUILDING_TREND'},
        })
        r['_execution_quote']={'price':102.65,'observed_at':now.isoformat()}
        r['trade_plan'].update({'entry_quality':'INVALIDATED','stop_price':101.8,
                                'target_price':104.35,'expected_move_pct':.0165,
                                'expected_to_stop_ratio':2.0})
        r['trend_entry_context'].update({
          'status':'STALE','closed_at':(now-timedelta(minutes=25)).timestamp(),
          'local_support':101.9,'local_resistance':102.8,
        })
        work=T.prepare_row(r,102.65,now)
        ev=T.context_of(work)['event']
        self.assertTrue(ev.get('signal_authoritative'),ev)
        self.assertLess(abs(ev['signal_at']-now.timestamp()),5.0)
        self.assertTrue(T.context_gate(work,now)['eligible'])
        out=R._signal_first_admission(r,P.POLICIES['Aggressive'],0.0)
        self.assertTrue(out.get('open'),out)

    def test_slow_cny_signal_uses_fresh_fast_same_asset_quote(self):
        now=datetime.now(timezone.utc)
        slow=self.row('LONG')
        slow.update({
          'asset':'CNYRUBF','horizon':'1d','price':12.60,
          'market_observed_at':(now-timedelta(minutes=20)).isoformat(),
          'contract':{'secid':'OLD_CONTINUOUS'},
        })
        fast={
          'asset':'CNYRUBF','horizon':'5m','price':12.707,
          'research_decision':'NO_TRADE','decision':'NO_TRADE',
          'source_gate_pass':True,'market_open':True,
          'market_observed_at':now.isoformat(),
          'contract':{'secid':'CURRENT_CONTINUOUS'},
        }
        old_ns=VPG._entry_namespace
        VPG._entry_namespace={}
        try:
            out=VPG.refresh_entry_quotes([slow,fast])
        finally:
            VPG._entry_namespace=old_ns
        q=out[0].get('_execution_quote') or {}
        self.assertAlmostEqual(q.get('price'),12.707)
        self.assertEqual(q.get('observed_at'),fast['market_observed_at'])

    def test_full_open_path_no_longer_calls_soft_veto_chain(self):
        r=self.row('LONG')
        c=MagicMock()
        c.execute.return_value.fetchone.return_value=None
        work,ev,direction,active=R._v90r79_signal_state(dict(r,price=2300.0))
        print('R79_DIAG',{'active':active,'direction':direction,'event':ev,
              'source':R.VX.paper_source_gate('MOEX',work),
              'quote':R.VPG.quote_gate((work.get('_execution_quote') or {}).get('observed_at'),
                                       now=datetime.now(timezone.utc),execution=True,asset='MOEX'),
              'event_gate':T.event_gate(work,2300.0,'LONG',datetime.now(timezone.utc)),
              'stop':(work.get('trade_plan') or {}).get('stop_price')})
        with patch.object(R,'_r72_event_reentry_gate',return_value={'eligible':True,'reason':'R72_NEW_EVENT'}),\
             patch.object(R,'_v90pr_base_open_or_add',return_value=.50) as mutation:
            out=R._open_or_add(c,{},'Aggressive','MOEX','LONG',2300.0,.50,1_000_000.0,
                               datetime.now(timezone.utc).isoformat(),r,'test')
        self.assertGreater(out,0)
        mutation.assert_called_once()


if __name__=='__main__':
    unittest.main()
