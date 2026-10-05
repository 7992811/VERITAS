from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import unittest

import veritas_trend_entry as T
import veritas_portfolio_runtime as R
import veritas_portfolio as P


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

    def test_full_open_path_no_longer_calls_soft_veto_chain(self):
        r=self.row('LONG')
        c=MagicMock()
        c.execute.return_value.fetchone.return_value=None
        with patch.object(R,'_r72_event_reentry_gate',return_value={'eligible':True,'reason':'R72_NEW_EVENT'}),\
             patch.object(R,'_v90r59_base_open_or_add',return_value=.50) as mutation:
            out=R._open_or_add(c,{},'Aggressive','MOEX','LONG',2300.0,.50,1_000_000.0,
                               datetime.now(timezone.utc).isoformat(),r,'test')
        self.assertGreater(out,0)
        mutation.assert_called_once()


if __name__=='__main__':
    unittest.main()
