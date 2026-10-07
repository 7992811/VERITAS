"""Display-only readiness must not promote a source or refresh old decisions."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from veritas_signal_readiness import readiness_fields


NOW = datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc)


def signal():
    return {'asset': 'ETH', 'horizon': '5m', 'research_decision': 'SHORT',
            'source_gate_pass': True, 'market_open': True, 'paper_eligible': True,
            'execution_eligible': True, 'market_observed_at': '2026-10-07T07:59:55+00:00',
            'trade_plan': {'eligible': True,
                'entry_timing_gate': {'eligible': True, 'reason': 'SAME_TF_ENTRY_READY'},
                'final_economics_gate': {'eligible': True, 'status': 'PASS', 'blockers': [],
                    'net_reward_risk': 1.3, 'context_freshness': {
                        'closed_at': NOW.timestamp()-300, 'age_seconds': 300.}}}}


class SignalReadinessTests(unittest.TestCase):
    def test_source_admission_alone_is_not_trade_readiness(self):
        row=signal();row.pop('trade_plan')
        before=copy.deepcopy(row)
        result=readiness_fields(row)
        self.assertFalse(result['trade_entry_eligible'])
        self.assertEqual(result['trade_entry_reason'],'TRADE_PLAN_NOT_CHECKED')
        self.assertIsNone(result['trade_entry_checked_at'])
        self.assertEqual(row,before)
        self.assertTrue(row['execution_eligible'])

    def test_complete_plan_retains_actual_decision_time_not_market_or_current_time(self):
        row=signal();before=copy.deepcopy(row)
        result=readiness_fields(row)
        self.assertTrue(result['trade_entry_eligible'])
        self.assertEqual(result['trade_entry_reason'],'TRADE_PLAN_READY')
        self.assertEqual(result['trade_entry_checked_at'],NOW.isoformat())
        self.assertEqual(result['trade_entry_basis'],'FINAL_PLAN')
        self.assertEqual(row,before)

    def test_timing_reason_precedes_but_does_not_hide_net_cost_blockers(self):
        for reason in ('SAME_TF_CONTEXT_STALE','SAME_TF_DIRECTION_CONFLICT',
                       'SAME_TF_TARGET_ALREADY_REACHED','SAME_TF_EVENT_EXPIRED'):
            row=signal();p=row['trade_plan'];p['eligible']=False
            p['entry_timing_gate']={'eligible':False,'reason':reason}
            p['final_economics_gate'].update(eligible=False,status='BLOCK',blockers=[
                'TARGET_NOT_PROFITABLE_AFTER_COSTS','NET_REWARD_RISK_BELOW_FLOOR',reason])
            result=readiness_fields(row)
            self.assertFalse(result['trade_entry_eligible'])
            self.assertEqual(result['trade_entry_reason'],reason)
            self.assertEqual(result['trade_entry_blockers'],[reason,
                'TARGET_NOT_PROFITABLE_AFTER_COSTS','NET_REWARD_RISK_BELOW_FLOOR'])

    def test_no_direction_or_source_failure_cannot_claim_ready(self):
        for field,value,reason in (
            ('research_decision','NO_TRADE','NO_DIRECTION'),
            ('source_gate_pass',False,'PRIMARY_SOURCE_GATE_FAILED'),
            ('market_open',False,'MARKET_SESSION_CLOSED'),
            ('paper_eligible',False,'PAPER_SOURCE_NOT_ELIGIBLE')):
            row=signal();row[field]=value
            result=readiness_fields(row)
            self.assertFalse(result['trade_entry_eligible'])
            self.assertEqual(result['trade_entry_reason'],reason)

    def test_production_denial_does_not_change_paper_readiness(self):
        row=signal();row.update(execution_eligible=False,production_eligible=False)
        self.assertTrue(readiness_fields(row)['trade_entry_eligible'])

    def test_missing_or_invalid_decision_time_is_not_replaced_by_quote_time(self):
        for freshness in ({}, {'closed_at':NOW.isoformat(),'age_seconds':float('nan')},
                          {'closed_at':NOW.timestamp(),'age_seconds':True}):
            row=signal();row['trade_plan']['final_economics_gate']['context_freshness']=freshness
            result=readiness_fields(row)
            self.assertFalse(result['trade_entry_eligible'])
            self.assertEqual(result['trade_entry_reason'],'TRADE_ENTRY_TIME_MISSING')
            self.assertIsNone(result['trade_entry_checked_at'])

    def test_explicit_saved_clock_and_canonical_decline_override_an_old_ready_plan(self):
        row=signal()
        canonical={'open':False,'reason':'SAME_TF_CONTEXT_STALE',
                   'checked_at':'2026-10-07T08:00:30+00:00','hard_blockers':['SAME_TF_CONTEXT_STALE']}
        result=readiness_fields(row,canonical=canonical)
        self.assertFalse(result['trade_entry_eligible'])
        self.assertEqual(result['trade_entry_reason'],'SAME_TF_CONTEXT_STALE')
        self.assertEqual(result['trade_entry_checked_at'],canonical['checked_at'])
        self.assertEqual(result['trade_entry_basis'],'CANONICAL_ADMISSION')

    def test_canonical_permission_is_separate_from_an_executed_order(self):
        result=readiness_fields(signal(),canonical={'open':True,'checked_at':NOW.isoformat()})
        self.assertTrue(result['trade_entry_eligible'])
        self.assertEqual(result['trade_entry_reason'],'CANONICAL_ENTRY_ADMITTED')
        self.assertNotIn('execution',result)
        self.assertNotIn('EXECUTED',json.dumps(result))

    def test_canonical_permission_cannot_borrow_a_different_plan_clock(self):
        result=readiness_fields(signal(),canonical={'open':True})
        self.assertFalse(result['trade_entry_eligible'])
        self.assertEqual(result['trade_entry_reason'],'TRADE_ENTRY_TIME_MISSING')
        self.assertIsNone(result['trade_entry_checked_at'])

    def test_incomplete_or_contradictory_final_economics_does_not_become_ready(self):
        for gate in ({'status':'PASS'}, {'eligible':True,'status':'BLOCK'}):
            row=signal();row['trade_plan']['final_economics_gate']=gate
            result=readiness_fields(row,checked_at=NOW)
            self.assertFalse(result['trade_entry_eligible'])
            self.assertEqual(result['trade_entry_reason'],'TRADE_PLAN_NOT_CHECKED')

    def test_complete_fields_are_bounded_and_do_not_copy_market_history(self):
        row=signal();row['native_daily_bars']=[{'close':123}]*1000
        row['trade_plan']['timeframe_entry_context']={'bars':[{'close':456}]*1000}
        row['trade_plan']['final_economics_gate']['blockers']=['FAIL_'+str(i) for i in range(100)]
        row['trade_plan']['eligible']=False
        result=readiness_fields(row)
        self.assertLessEqual(len(result['trade_entry_blockers']),24)
        self.assertLess(len(json.dumps(result)),1500)
        self.assertFalse(any(isinstance(v,dict) for v in result.values()))
        self.assertNotIn('native_daily_bars',result)

    def test_legacy_reason_prefix_preserves_codes_without_free_text(self):
        row=signal();row['trade_plan'].update(eligible=False,
            reason='final_economics_gate:NET_REWARD_RISK_BELOW_FLOOR,SAME_TF_DIRECTION_CONFLICT')
        row['trade_plan']['final_economics_gate']={}
        result=readiness_fields(row,checked_at=NOW)
        self.assertEqual(result['trade_entry_blockers'],[
            'NET_REWARD_RISK_BELOW_FLOOR','SAME_TF_DIRECTION_CONFLICT'])
        self.assertEqual(result['trade_entry_checked_at'],NOW.isoformat())

    @unittest.skipUnless(shutil.which('node'),'Node required for entry readiness UI regression')
    def test_ui_keeps_research_direction_separate_from_entry_decision(self):
        result=subprocess.run(['node','tools/test_signal_readiness_ui.cjs'],
            cwd=Path(__file__).resolve().parent,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__=='__main__':
    unittest.main()
