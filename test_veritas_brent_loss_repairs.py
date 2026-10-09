from datetime import datetime,timedelta,timezone
import unittest

import veritas_canonical_constitution as CTC
import veritas_execution_efficiency as EFF
import veritas_observation_path as PATH
import veritas_price_source as SOURCE
import veritas_protective_io as PIO
import veritas_thesis_guard as TG
import veritas_trade_journal_read_model as JOURNAL
import veritas_user_teaching as UT

OPEN=datetime(2026,10,9,9,0,tzinfo=timezone.utc)
FEED={'primary_source':'TEST_NATIVE','contract_id':'BRENT-TEST','source_gate_pass':True}

def stamp(seconds):
    return (OPEN+timedelta(seconds=seconds)).isoformat()

def trade():
    return {'trade_id':'brent-repair','active_trade_id':'brent-repair','asset':'BRENT',
            'direction':'LONG','horizon':'5m','opened_at':stamp(0),'avg_entry_price':100.,
            'units':10.,'payload':{'price_source_lock':SOURCE.identity('BRENT',FEED),
            'initial_stop_price':98.,'entry_atr':1.,
            'entry_execution_model':{'asset':'BRENT','side':'BUY','fill_price':100.}}}

def quote(seconds,price):
    return dict(FEED,price=price,observed_at=stamp(seconds))

class BrentLossRepairTests(unittest.TestCase):
    def test_gap_invalidates_continuity_but_keeps_fresh_extrema(self):
        row=trade()
        row['payload']['observation_path']=PATH.observe(row,quote(0,100.),stamp(0),at_entry=True)
        row['payload']['observation_path']=PATH.observe(row,quote(60,105.),stamp(60))
        witness=row['payload']['observation_path']
        self.assertGreater(witness['invalid_observation_count'],0)
        self.assertEqual(witness['observation_count'],2)
        self.assertEqual(witness['max_price'],105.)
        self.assertAlmostEqual(witness['mfe_pct'],5.)
        row.update(status='CLOSED',closed_at=stamp(60))
        self.assertFalse(PATH.assessment(row)['eligible'])

    def test_adaptive_mfe_timer_survives_bounded_protective_reads(self):
        for field in ('r_accel_mfe_pct','r_accel_mfe_candidate_at',
                      'r_accel_mfe_candidate_pct','r_accel_mfe_candidate_timeframe',
                      'r_accel_mfe_candidate_elapsed_seconds',
                      'r_accel_mfe_profit_lock_active',
                      'r_accel_mfe_learning_teaching_id',
                      'r_accel_mfe_execution_required',
                      'r_accel_mfe_protection_state',
                      'r_accel_mfe_exit_authority'):
            self.assertIn(field,PIO.PROTECTION_FIELDS)
        policy=CTC.TREND_ACCELERATION_POLICY['profit_protection']
        self.assertTrue(policy['structural_stop_is_exit_authority'])
        self.assertEqual(policy['persistence_failure_action'],'EXIT_TO_CASH')
        self.assertTrue(policy['execution_receipt_required'])

    def test_owner_policy_consistency_is_explicit(self):
        self.assertEqual(UT.runtime_consistency()['status'],'OK')
        snap=UT.observation_integrity_policy_snapshot()
        self.assertEqual(snap['runtime_consistency']['status'],'OK')
        self.assertIn('0.15%',snap['requirements']['mfe'])
        self.assertIn('Owner-authored',snap['requirements']['precedence'])

    def test_fast_reversal_requires_same_source_confirmed_fast_evidence(self):
        self.assertIn('5m',TG.PROTECTED)
        exact={'asset':'NQ','horizon':'1h','research_decision':'LONG',
               'primary_source':'TEST_NATIVE','contract_id':'NQ-TEST'}
        fast={'asset':'NQ','horizon':'5m','research_decision':'LONG',
              'primary_source':'TEST_NATIVE','contract_id':'NQ-TEST',
              'source_gate_pass':True,'confidence':.8,'independent_evidence_families':4,
              'horizon_structure':{'direction':'LONG','state':'CONFIRMED_TREND'},
              'trade_plan':{'expected_move_pct':.008}}
        result=TG._fast_reversal_confirmation([fast],exact,'NQ','SHORT','1h')
        self.assertTrue(result['eligible'],result)
        wrong=dict(fast,primary_source='OTHER')
        self.assertFalse(TG._fast_reversal_confirmation([wrong],exact,'NQ','SHORT','1h')['eligible'])
        self.assertFalse(TG._fast_reversal_confirmation([fast],exact,'NQ','SHORT','4h')['eligible'])

    def test_repeated_add_churn_not_first_add_is_capped(self):
        existing={'units':10.,'avg_entry_price':100.,'payload':{}}
        policy={'max_repeated_add_fee_to_positive_edge':.25,
                'block_add_on_fast_opposite_confirmation':True}
        first=EFF.add_precheck(existing,{},102.,'LONG',.0005,policy)
        self.assertTrue(first['eligible'],first)
        existing['payload']={'add_count':2,'add_fee_rub':6.}
        repeated=EFF.add_precheck(existing,{},102.,'LONG',.0005,policy)
        self.assertFalse(repeated['eligible'],repeated)
        self.assertEqual(repeated['reason'],'ADD_CHURN_COST_LIMIT')

    def test_add_analysis_tracks_fee_and_pre_add_mfe(self):
        opened={'units':15.,'avg_entry_price':101.,'payload':{'add_count':1,'add_fee_rub':1.}}
        patch=EFF.entry_analysis_patch(opened,False,
              {'units':10.,'avg_entry_price':100.,'mfe_pct':.7},101.,stamp(60),.0005)
        self.assertEqual(patch['add_count'],2)
        self.assertGreater(patch['add_fee_rub'],1.)
        self.assertEqual(patch['mfe_before_last_add_pct'],.7)

    def test_closed_journal_exposes_stop_and_add_review_fields(self):
        for field in ('exit_effective_stop_price','exit_trailing_stop_price','add_count',
                      'add_fee_rub','mfe_before_last_add_pct','mfe_since_last_add_pct',
                      'initial_tranche_final_exit_net_proxy_rub',
                      'r_accel_mfe_learning_teaching_id',
                      'r_accel_mfe_execution_required',
                      'r_accel_mfe_protection_state',
                      'r_accel_mfe_exit_authority',
                      'r_accel_mfe_exit_authority_reason'):
            self.assertIn(field,JOURNAL.SCALAR_FIELDS)

if __name__=='__main__':
    unittest.main()
