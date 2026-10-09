from datetime import datetime,timedelta,timezone
from unittest.mock import MagicMock
import unittest

import veritas_observation_path as PATH
import veritas_price_source as SOURCE
import veritas_protective_io as PIO
import veritas_thesis_guard as TG
import veritas_user_teaching as UT
import veritas_trade_journal_read_model as JOURNAL


OPEN=datetime(2026,10,9,9,0,tzinfo=timezone.utc)
FEED={'primary_source':'TEST_NATIVE','contract_id':'BRENT-TEST','source_gate_pass':True}


def stamp(seconds):
    return (OPEN+timedelta(seconds=seconds)).isoformat()


def trade():
    return {'trade_id':'brent-loss-repair','active_trade_id':'brent-loss-repair',
            'asset':'BRENT','direction':'LONG','horizon':'5m','opened_at':stamp(0),
            'avg_entry_price':100.,'units':1.,
            'payload':{'price_source_lock':SOURCE.identity('BRENT',FEED),
                       'initial_stop_price':98.,'entry_atr':1.,
                       'entry_execution_model':{'asset':'BRENT','side':'BUY','fill_price':100.}}}


def quote(seconds,price):
    return dict(FEED,price=price,observed_at=stamp(seconds))


class BrentLossRepairTests(unittest.TestCase):
    def test_missed_guard_check_downgrades_path_but_keeps_fresh_extrema(self):
        row=trade()
        row['payload']['observation_path']=PATH.observe(row,quote(0,100.),stamp(0),at_entry=True)
        row['payload']['observation_path']=PATH.observe(row,quote(60,105.),stamp(60))
        witness=row['payload']['observation_path']
        self.assertEqual(witness['observation_count'],2)
        self.assertEqual(witness['max_price'],105.)
        self.assertAlmostEqual(witness['mfe_pct'],5.)
        self.assertGreater(witness['invalid_observation_count'],0)
        row['closed_at']=stamp(60)
        self.assertFalse(PATH.assessment(row)['eligible'])

    def test_owner_mfe_timer_state_is_in_bounded_protective_read(self):
        for field in ('r_accel_mfe_candidate_at','r_accel_mfe_candidate_pct',
                      'r_accel_mfe_candidate_timeframe','r_accel_mfe_pct',
                      'mfe_since_last_add_pct','add_count','add_fee_rub'):
            self.assertIn(field,PIO.PROTECTION_FIELDS)

    def test_active_runtime_policy_matches_owner_teaching(self):
        result=UT.assert_runtime_consistency()
        self.assertEqual(result['status'],'OK',result)
        snap=UT.observation_integrity_policy_snapshot()
        self.assertEqual(snap['runtime_consistency']['status'],'OK')
        self.assertIn('MFE/MAE',snap['requirements']['mfe'])

    def test_fast_5m_reversal_plus_senior_confirmation_exits_stale_1h(self):
        db=MagicMock()
        db.execute.return_value.fetchone.return_value={'horizon':'1h'}
        z={'asset':'NQ','direction':'SHORT','active_trade_id':'t','payload':{}}
        fast={'asset':'NQ','horizon':'5m','research_decision':'LONG','confidence':.82,
              'independent_evidence_families':4,
              'horizon_structure':{'direction':'LONG','state':'CONFIRMED_TREND'},
              'trade_plan':{'expected_move_pct':.008,'trade_integrity':{
                  'hard_invalidation':False,'hard_reasons':[]}}}
        senior={'asset':'NQ','horizon':'4h','research_decision':'LONG','confidence':.35,
                'horizon_structure':{'direction':'LONG','state':'BUILDING_TREND'},
                'trade_plan':{'trade_integrity':{'hard_invalidation':False,'hard_reasons':[]}}}
        _,_,meta=TG.guard_open_position(db,z,{'NQ':fast},[fast,senior],now=OPEN)
        self.assertTrue(meta['fast_reversal']['eligible'],meta)
        self.assertTrue(meta['hard_exit_allowed'],meta)
        self.assertFalse(meta['active'])

    def test_closed_journal_projects_stop_add_and_counterfactual_fields(self):
        for field in ('exit_effective_stop_price','exit_trailing_stop_price','add_count',
                      'add_fee_rub','mfe_before_last_add_pct','mfe_since_last_add_pct',
                      'initial_tranche_final_exit_net_proxy_rub'):
            self.assertIn(field,JOURNAL.SCALAR_FIELDS)


if __name__=='__main__':
    unittest.main()
