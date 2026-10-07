"""Decision journals preserve execution facts without re-running admission."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import veritas_admission_trace as VAT
import veritas_canonical_runtime as VCR
import veritas_canonical_constitution as CTC
import veritas_portfolio as VP
import veritas_portfolio_runtime as VPR

AT = datetime(2026,10,7,8,0,tzinfo=timezone.utc)


def row(horizon='5m'):
    return {'asset':'ETH','horizon':horizon,'research_decision':'SHORT',
        'price':100.,'source_names':{'primary':'Binance spot'},
        'source_gate_pass':True,'market_open':True,'paper_eligible':True,
        'market_observed_at':AT.isoformat(),'trade_plan':{'expected_to_stop_ratio':2.},
        '_pwin':.75,'_pwin_source':'MODEL_QUALITY_SCORE_UNCALIBRATED',
        'timeframe_entry_context':{'closed_at':AT.timestamp(),
            'source_identity':{'key':'BINANCE:ETHUSDT'},
            'event':{'event_id':'held-original-event'}}}


def admission(open=True, reason='CANONICAL_SIGNAL_ENTRY'):
    return {'open':open,'fraction':.1 if open else 0.,'reason':reason,'checked_at':AT.isoformat(),
        'hard_veto':not open,'canonical_stage':'SIZE' if open else 'TIMING',
        'economics':{'forecast_reward_risk':2.,'net_reward_risk':1.4,
                     'modeled_round_trip_cost_pct':.0016}}


class AdmissionEvidenceTests(unittest.TestCase):
    def test_executed_survives_later_stale_projection_without_report_gate_calls(self):
        candidate=row()
        VAT.begin_cycle(candidate,AT)
        class Connection:
            def execute(self,*args):return SimpleNamespace(fetchone=lambda:None)
        def account(*args):
            VP._record_entry_outcome(args[9],'EXECUTED','ORDER_RECORDED',
                                     fill_price=99.9,order_id='recorded-order')
        with patch.object(VCR,'evaluate',return_value=admission()), \
                patch.object(VPR.TFP,'prepare_row',side_effect=lambda r,*a,**k:r), \
                patch.object(VP,'CANONICAL_ACCOUNTING_OPEN_OR_ADD',side_effect=account):
            VPR.canonical_open_or_add(Connection(),{'high_water_nav_rub':1000},'Impulse',
                'ETH','SHORT',100,.1,1000,AT,candidate,'TEST')
        with patch.object(VCR,'evaluate',return_value=admission(False,'SAME_TF_CONTEXT_STALE')):
            VP._signal_first_admission(candidate,VP.POLICIES['Impulse'],0.)
        with patch.object(VCR,'evaluate',side_effect=AssertionError('report re-evaluated gate')), \
                patch.object(VCR.VPG,'refresh_execution_row',side_effect=AssertionError('report refreshed quote')):
            result=VP._portfolio_admission_trace({'ETH':candidate},{},0)[0]
            self.assertEqual(result,VPR._portfolio_admission_trace({'ETH':candidate},{},0)[0])
        self.assertEqual(result['execution']['status'],'EXECUTED')
        self.assertFalse(result['hard_veto'])
        self.assertEqual(result['reason'],'ORDER_RECORDED')
        self.assertEqual(result['execution']['order_id'],'recorded-order')
        self.assertEqual(result['admission']['phase'],'EXECUTION')
        self.assertEqual(result['admission']['reason'],'CANONICAL_SIGNAL_ENTRY')
        self.assertEqual(result['gross_rr'],2.)
        self.assertEqual(result['net_rr'],1.4)
        self.assertTrue(result['checked_at'])
        candidate['_admission_audit']['EXECUTION']['reason']='later mutation'
        self.assertEqual(result['admission']['reason'],'CANONICAL_SIGNAL_ENTRY')

    def test_actual_execution_rejection_preserved_through_canonical_copies(self):
        candidate=row()
        VAT.record_fill(candidate,{'eligible':False,'status':'BLOCKED','reason':'OLD_ATTEMPT',
            'gate':{'net_reward_risk':-.5}},AT)
        with patch.object(VCR,'evaluate',return_value=admission(False,'SAME_TF_EVENT_EXPIRED')), \
                patch.object(VPR.TFP,'prepare_row',side_effect=lambda r,*a,**k:r):
            # A rejected gate must not touch accounting or SQL.
            VPR.canonical_open_or_add(None,{},'Impulse','ETH','SHORT',100,.1,1000,AT,candidate,'TEST')
        result=VAT.build({'ETH':candidate})[0]
        self.assertEqual(result['execution']['status'],'BLOCKED')
        self.assertEqual(result['reason'],'SAME_TF_EVENT_EXPIRED')
        self.assertEqual(result['admission']['checked_at'],AT.isoformat())
        self.assertEqual(result['admission']['event_id'],'held-original-event')
        self.assertNotIn('fill_admission',result)
        self.assertEqual(result['net_rr'],1.4)

    def test_allocation_gate_result_is_not_recomputed_for_zero_target(self):
        candidate=row()
        VAT.begin_cycle(candidate,AT)
        with patch.object(VCR,'evaluate',return_value=admission(False,'SAME_TF_CONTEXT_STALE')) as gate:
            self.assertEqual(VPR._canonical_desired_fraction(candidate,{},0),0.)
            result=VP._portfolio_admission_trace({'ETH':candidate},{},0)[0]
        self.assertEqual(gate.call_count,1)
        self.assertEqual(result['admission']['phase'],'ALLOCATION')
        self.assertEqual(result['execution']['status'],'NOT_REQUESTED')
        self.assertEqual(result['reason'],'SAME_TF_CONTEXT_STALE')
        self.assertTrue(result['checked_at'])

    def test_final_fill_denial_keeps_projected_admission_and_actual_economics_distinct(self):
        candidate=row()
        VAT.record(candidate,admission(),AT,'EXECUTION')
        gate={'forecast_reward_risk':1.3,'net_reward_risk':.9,
              'blockers':['NET_REWARD_RISK_BELOW_FLOOR']}
        VAT.record_fill(candidate,{'eligible':False,'status':'BLOCKED',
            'reason':'FINAL_EXECUTION_ECONOMICS','gate':gate,'blockers':gate['blockers']},AT)
        VP._record_entry_outcome(candidate,'BLOCKED','FINAL_EXECUTION_ECONOMICS',
                                 checked_at=AT,blockers=gate['blockers'])
        result=VAT.build({'ETH':candidate})[0]
        self.assertTrue(result['admission']['open'])
        self.assertTrue(result['hard_veto'])
        self.assertFalse(result['fill_admission']['open'])
        self.assertEqual(result['execution']['blockers'],['NET_REWARD_RISK_BELOW_FLOOR'])
        self.assertEqual(result['gross_rr'],1.3)
        self.assertEqual(result['net_rr'],.9)

    def test_missing_snapshot_is_explicit_unknown_and_never_fabricates_net_rr(self):
        result=VAT.build({'ETH':row()})[0]
        self.assertEqual(result['reason'],'ADMISSION_NOT_RECORDED')
        self.assertEqual(result['execution']['status'],'UNKNOWN')
        self.assertIsNone(result['net_rr'])
        self.assertIsNone(result['checked_at'])

    def test_checked_quote_time_comes_only_from_gate_evidence(self):
        candidate=row()
        candidate['_execution_quote']={'observed_at':'2026-10-07T07:58:00+00:00'}
        decision=admission()
        saved=VAT.record(candidate,decision,AT,'ALLOCATION')
        self.assertIsNone(saved['quote_observed_at'])
        economic_time='2026-10-07T07:59:50+00:00'
        decision['economics']['quote_time_gate']={'observed_at':economic_time}
        self.assertEqual(VAT.snapshot(candidate,decision,AT,'ALLOCATION')['quote_observed_at'],
                         economic_time)
        direct_time='2026-10-07T07:59:55+00:00'
        decision['quote_time_gate']={'observed_at':direct_time}
        VAT.record(candidate,decision,AT,'ALLOCATION')
        result=VAT.build({'ETH':candidate})[0]
        self.assertEqual(result['admission']['quote_observed_at'],direct_time)
        self.assertEqual(result['signal_observed_at'],candidate['market_observed_at'])

    def test_actual_execution_stays_authoritative_without_an_admission_snapshot(self):
        candidate=row()
        for status,reason,veto in (('EXECUTED','ORDER_RECORDED',False),
                                  ('HELD','TARGET_ALREADY_REACHED',False),
                                  ('BLOCKED','SOURCE_IDENTITY_MISSING',True)):
            with self.subTest(status=status):
                VP._record_entry_outcome(candidate,status,reason,checked_at=AT)
                result=VAT.build({'ETH':candidate})[0]
                self.assertEqual((result['reason'],result['hard_veto']),(reason,veto))
                self.assertEqual(result['checked_at'],AT.isoformat())

    def test_gate_records_pre_refresh_clock_and_keeps_optional_refresh_semantics(self):
        candidate=row()
        with patch.object(VCR,'datetime',SimpleNamespace(now=lambda tz:AT)), \
                patch.object(VCR.VPG,'refresh_execution_row',return_value=candidate) as refresh, \
                patch.object(VCR.VX,'paper_source_gate',return_value={'eligible':False,'blockers':['SOURCE_TEST']}):
            result=VCR.evaluate(candidate,{},0.)
            refresh.assert_called_once_with(candidate,now=AT)
            self.assertEqual(result['checked_at'],AT.isoformat())
            self.assertEqual(result['reason'],'SOURCE_TEST')
            refresh.reset_mock()
            result=VCR.evaluate(candidate,{},0.,AT)
            refresh.assert_not_called()
            self.assertEqual(result['checked_at'],AT.isoformat())

    def test_json_is_finite_bounded_and_excludes_candles_and_full_row(self):
        candidate=row()
        candidate['_rank']=float('inf');candidate['_pwin']=float('nan')
        decision=admission();decision['economics']['net_reward_risk']=float('nan')
        decision['economics']['bars']=[{'close':100}]*10000
        decision['prepared_plan']={'expected_to_stop_ratio':2.,'candles':[{'close':100}]*10000}
        saved=VAT.record(candidate,decision,AT,'ALLOCATION')
        candidate['_canonical_route_trace']=[dict(saved,horizon='5m',direction='SHORT')]*1000
        candidate['_execution_audit']={'status':'BLOCKED','reason':'X',
            'blockers':['X'*1000]*1000,'row':candidate}
        result=VAT.build({'ETH':candidate})[0]
        encoded=json.dumps(result,allow_nan=False)
        self.assertIsNone(result['rank']);self.assertIsNone(result['net_rr'])
        self.assertLess(len(encoded),16000)
        self.assertEqual(len(result['route_trace']),VAT.MAX_ROUTE_ROWS)
        self.assertEqual(len(result['execution']['blockers']),VAT.MAX_ITEMS)
        self.assertNotIn('candles',encoded);self.assertNotIn('"bars"',encoded)
        self.assertNotIn('"row"',encoded)


class RoutingEvidenceTests(unittest.TestCase):
    def candidates(self):
        senior=row('1h');junior=row('5m')
        senior.update(confidence=.9,signal_tier='SUPER_SHORT')
        junior.update(confidence=.4,signal_tier='SHORT')
        return [senior,junior]

    def route(self, all_blocked=False):
        rows=self.candidates()
        calls=[]
        def evaluate(candidate,*args):
            calls.append(candidate['horizon'])
            eligible=candidate['horizon']=='5m' and not all_blocked
            return admission(eligible,'CANONICAL_SIGNAL_ENTRY' if eligible else 'SAME_TF_EVENT_EXPIRED')
        with patch.object(VCR,'evaluate',side_effect=evaluate), \
                patch.object(VCR.TFP,'candidate_priority',return_value=0), \
                patch.object(VCR.VROLE,'gate',return_value={'eligible':True,'role':'TEST'}):
            book=VCR.transition_candidate_book(rows,{},CTC.runtime_portfolio_policy('Aggressive')['mode'])
        return book,calls

    def test_admissible_lower_timeframe_remains_selected_and_route_is_reported(self):
        book,calls=self.route()
        self.assertEqual(calls,['1h','5m'])
        self.assertEqual(book['ETH']['horizon'],'5m')
        result=VAT.build(book)[0]
        self.assertEqual([r['open'] for r in result['route_trace']],[False,True])
        self.assertEqual([r['selected'] for r in result['route_trace']],[False,True])
        self.assertTrue(result['admission']['open'])
        self.assertEqual(result['admission']['phase'],'ROUTING')

    def test_all_blocked_retains_priority_row_and_every_attempt_reason(self):
        book,calls=self.route(all_blocked=True)
        self.assertEqual(calls,['1h','5m'])
        self.assertEqual(book['ETH']['horizon'],'1h')
        result=VAT.build(book)[0]
        self.assertTrue(result['hard_veto'])
        self.assertEqual(result['reason'],'SAME_TF_EVENT_EXPIRED')
        self.assertEqual(len(result['route_trace']),2)
        self.assertTrue(all(r['checked_at'] and not r['open'] for r in result['route_trace']))


if __name__=='__main__':unittest.main()
