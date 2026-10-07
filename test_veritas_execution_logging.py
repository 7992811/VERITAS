"""A refused entry reports its evidence without serializing the evidence graph."""
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import veritas_execution_logging as LOG


class UnreadableGraph(dict):
    def forbidden(self, *args, **kwargs):
        raise AssertionError('Logging traversed an execution evidence graph')
    __iter__ = items = values = keys = __str__ = __repr__ = __deepcopy__ = forbidden


class UnsafeValue:
    def forbidden(self, *args, **kwargs):
        raise AssertionError('Logging converted an unsupported diagnostic object')
    __str__ = __repr__ = __float__ = __int__ = __iter__ = __deepcopy__ = forbidden


def refusal():
    return {'eligible':False, 'status':'BLOCKED', 'reason':'FINAL_EXECUTION_ECONOMICS',
        'blockers':['TARGET_NOT_PROFITABLE_AFTER_COSTS'],
        'hard_blockers':['TARGET_NOT_PROFITABLE_AFTER_COSTS'],
        'gate':{'eligible':False, 'status':'BLOCK',
            'blockers':['TARGET_NOT_PROFITABLE_AFTER_COSTS'],
            'asset':'NQ', 'direction':'LONG', 'net_reward_risk':-.2,
            'net_reward_pct':-.001, 'net_risk_pct':.005,
            'minimum_reward_risk':0., 'modeled_entry_fill':24500.,
            'modeled_stop_fill':24400., 'modeled_target_fill':24520.,
            'modeled_round_trip_cost_pct':.0016,
            'quote_time_gate':{'eligible':True, 'reason':None, 'age_seconds':1.,
                'max_age_seconds':120., 'observed_at':'2026-10-07T16:00:00Z'},
            'trend_event':{'eligible':True, 'event_id':'STRUCTURAL_TEST',
                'timeframe':'5m', 'structural_timeframe':'1h', 'stop_timeframe':'1h',
                'atr_timeframe':'1h', 'trigger_level':24480., 'extension_atr':.2},
            'economics_policy':{'event_id':'STRUCTURAL_TEST', 'event_proof_hash':'sealed-proof'},
            'execution_snapshot':{'snapshot_id':'frozen-snapshot', 'quote_id':'frozen-quote',
                'structural_economics_context_id':'sealed-context', 'event_id':'STRUCTURAL_TEST',
                'timeframe':'5m', 'fraction_nav':.1,
                'source_identity':{'key':'TBANK:NQF-EXACT', 'contract_id':'exact-uid'},
                'quote':{'price':24490., 'observed_at':'2026-10-07T16:00:00Z',
                    'best_bid':24489., 'best_ask':24491.}},
            'target_ladder':[{'kind':'TP1', 'price':24520., 'fraction':.5},
                             {'kind':'TP2', 'price':24550., 'fraction':.5}]},
        'stop_risk_budget':{'eligible':False, 'reason':'STOP_RISK_CAP_EXCEEDED',
            'risk_cap_nav':.02, 'existing_stop_risk_nav':.02,
            'requested_fraction':.3, 'current_fraction':.2, 'add_fraction':0.}}


class ExecutionLoggingTests(unittest.TestCase):
    def test_context_summary_preserves_original_event_and_quote_clocks(self):
        from test_veritas_weighted_structural_economics import structural_plan
        _,_,plan=structural_plan()
        context=plan['timeframe_entry_context']
        before=deepcopy(context)
        out=LOG.context_summary(context)
        self.assertEqual(out['source_identity']['key'],context['source_identity']['key'])
        self.assertEqual(out['quote_observed_at'],context['quote_observed_at'])
        self.assertEqual(out['quote']['price'],context['quote']['price'])
        for key in ('event_id','proof_hash','signal_at','stop_price','target_price',
                    'runner_target_price','atr','stop_timeframe','atr_timeframe'):
            self.assertEqual(out['event'][key],context['event'][key])
        self.assertEqual(out['entry_gate']['eligible'],context['entry_gate']['eligible'])
        self.assertEqual(out['entry_gate']['reason'],context['entry_gate']['reason'])
        self.assertLess(len(json.dumps(out)),len(json.dumps(context))/4)
        self.assertEqual(context,before)
        out['event']['stop_price']=1.
        out['source_identity']['key']='LOG_ONLY'
        self.assertEqual(context,before)

    def test_context_summary_never_reads_structural_or_ma_proof_graphs(self):
        proof=UnreadableGraph(bars=[UnsafeValue()]*100000,native_history='x'*2000000)
        proof['cycle']=proof
        context={'version':'CONTEXT_TEST','status':'OK','timeframe':'5m',
            'event':{'event_id':'ORIGINAL_EVENT','proof_hash':'SEALED_ID',
                'direction':'LONG','stop_price':99.,'target_price':103.,
                'atr_proof':proof,'ma_proof':proof,'trigger':proof,
                'target_zones':proof,'target_ladder':proof},
            'quote_state':proof,'levels':proof,'structure_atr_proof':proof,
            'protected_leg':proof,'native_history':proof,
            'entry_gate':{'eligible':False,'reason':'STRUCTURAL_ENTRY_TOO_LATE_TO_TARGET'}}
        out=LOG.context_summary(context)
        encoded=json.dumps(out,allow_nan=False)
        self.assertLess(len(encoded),2000)
        self.assertEqual(out['event']['event_id'],'ORIGINAL_EVENT')
        self.assertEqual(out['entry_gate']['reason'],'STRUCTURAL_ENTRY_TOO_LATE_TO_TARGET')
        for key in ('atr_proof','ma_proof','quote_state','native_history','target_zones'):
            self.assertNotIn(key,encoded)
        self.assertIs(context['quote_state'],proof)

    def test_context_summary_malformed_fields_remain_bounded_without_conversion(self):
        values=[None,UnsafeValue(),[],{'status':UnsafeValue(),'atr':float('nan'),
            'event':{'event_id':'x'*100000,'stop_price':10**1000,'source_identity':UnsafeValue()},
            'quote_gate':{'quote_time_gate':UnsafeValue()},'entry_gate':UnsafeValue()}]
        for value in values:
            out=LOG.context_summary(value)
            self.assertLess(len(json.dumps(out,allow_nan=False)),2000)
        self.assertIsNone(out['status'])
        self.assertIsNone(out['atr'])
        self.assertIsNone(out['event']['stop_price'])
        self.assertEqual(len(out['event']['event_id']),LOG.MAX_TEXT_LENGTH)

    def test_reason_source_clocks_and_frozen_economics_remain_visible(self):
        prepared = refusal()
        before = deepcopy(prepared)
        out = LOG.blocked_entry_event('Aggressive', 'NQ', prepared)
        self.assertEqual((out['event'],out['portfolio'],out['asset']),
                         ('PAPER_ENTRY_BLOCKED_FINAL','Aggressive','NQ'))
        self.assertEqual(out['log_projection_version'],LOG.VERSION)
        decision, gate = out['decision'], out['decision']['gate']
        self.assertEqual(decision['reason'],prepared['reason'])
        self.assertEqual(decision['blockers'],prepared['blockers'])
        self.assertEqual(decision['hard_blockers'],prepared['hard_blockers'])
        self.assertEqual(gate['net_reward_risk'],-.2)
        self.assertEqual(gate['modeled_round_trip_cost_pct'],.0016)
        self.assertEqual(gate['trend_event']['event_id'],'STRUCTURAL_TEST')
        self.assertEqual(gate['trend_event']['stop_timeframe'],'1h')
        self.assertEqual(gate['quote_time_gate']['age_seconds'],1.)
        saved = gate['execution_snapshot']
        self.assertEqual(saved['snapshot_id'],'frozen-snapshot')
        self.assertEqual(saved['quote_id'],'frozen-quote')
        self.assertEqual(saved['structural_economics_context_id'],'sealed-context')
        self.assertEqual(saved['source_identity']['contract_id'],'exact-uid')
        self.assertEqual(decision['stop_risk_budget']['risk_cap_nav'],.02)
        self.assertLess(len(json.dumps(out)),6000)
        self.assertEqual(prepared,before)
        # Output containers cannot change the canonical refusal after logging.
        decision['blockers'].append('LOG_ONLY')
        saved['quote']['price']=1.
        gate['target_ladder'][0]['price']=1.
        self.assertEqual(prepared,before)

    def test_large_opaque_and_cyclic_proofs_are_never_walked_or_converted(self):
        prepared = refusal()
        proof = UnreadableGraph(bars=[UnsafeValue()]*100000, metadata='x'*2000000)
        proof['recursive']=proof
        gate = prepared['gate']
        prepared['native_history']=proof
        gate['structural_economics_context']=proof
        gate['entry_geometry']={'history':proof, 'proof':proof}
        gate['trend_event']['event_proof']=proof
        gate['execution_snapshot']['quote']['native_history']=proof
        prepared['stop_risk_budget']['existing_position_risk']={'payload':proof}
        encoded=json.dumps(LOG.blocked_entry_event('Aggressive','NQ',prepared),allow_nan=False)
        self.assertLess(len(encoded),6000)
        self.assertNotIn('native_history',encoded)
        self.assertNotIn('structural_economics_context"',encoded)
        self.assertIs(gate['structural_economics_context'],proof)

    def test_growing_blocker_and_target_lists_have_explicit_bounded_output(self):
        prepared=refusal()
        long='x'*100000
        many=[long]*10000
        prepared.update(reason=long,blockers=many,hard_blockers=many)
        prepared['gate'].update(blockers=many,canonical_overridden_blockers=many,
            canonical_hard_blockers=many,source_gate={'blockers':many},
            target_ladder=[{'kind':long,'price':42.,'fraction':.5,'proof':UnsafeValue()}]*10000)
        prepared['stop_risk_budget'].update(blockers=many,economics_blockers=many)
        out=LOG.blocked_entry_event(long,long,prepared)
        decision=out['decision']; gate=decision['gate']
        self.assertLess(len(json.dumps(out)),30000)
        self.assertEqual(len(decision['reason']),LOG.MAX_TEXT_LENGTH)
        self.assertEqual(len(decision['blockers']),LOG.MAX_BLOCKERS)
        self.assertEqual(decision['blockers_count'],10000)
        self.assertEqual(decision['blockers_omitted'],10000-LOG.MAX_BLOCKERS)
        self.assertEqual(len(gate['target_ladder']),LOG.MAX_TARGETS)
        self.assertEqual(gate['target_ladder_omitted'],10000-LOG.MAX_TARGETS)
        self.assertIs(prepared['blockers'],many)

    def test_malformed_diagnostics_remain_json_safe_without_casting_objects(self):
        for value in (None,UnsafeValue(),[],float('inf')):
            out=LOG.blocked_entry_event(value,value,value)
            json.dumps(out,allow_nan=False)
        prepared=refusal()
        prepared.update(reason=UnsafeValue(),blockers=UnsafeValue())
        prepared['gate'].update(net_reward_risk=float('nan'),net_reward_pct=10**1000,
            quote_time_gate=UnsafeValue(),execution_snapshot={'quote':UnsafeValue()},
            target_ladder=UnsafeValue(),source_gate={'blockers':[UnsafeValue()]})
        out=LOG.blocked_entry_event('Aggressive','NQ',prepared)
        decision=out['decision']; gate=decision['gate']
        json.dumps(out,allow_nan=False)
        self.assertIsNone(decision['reason'])
        self.assertTrue(decision['blockers_invalid'])
        self.assertIsNone(gate['net_reward_risk'])
        self.assertIsNone(gate['net_reward_pct'])
        self.assertEqual(gate['source_gate']['blockers'],[None])

    def test_real_causal_economics_context_is_omitted_without_losing_its_ids(self):
        import veritas_execution as VX
        import veritas_paper_entry as VPE
        from test_veritas_weighted_structural_economics import structural_plan
        raw,clock,plan=structural_plan()
        row=dict(raw,horizon='1h',research_decision='LONG',trade_plan=plan)
        gate=VX.entry_gate(row,raw['price'],'LONG',.5,now=clock)
        self.assertTrue(gate['structural_economics_context'])
        prepared=VPE._failure('STOP_RISK_CAP_EXCEEDED',gate)
        original=json.dumps(prepared,sort_keys=True,default=str)
        out=LOG.blocked_entry_event('Currency','CNYRUBF',prepared)
        projected=out['decision']['gate']
        self.assertEqual(projected['execution_snapshot']['snapshot_id'],
                         gate['execution_snapshot']['snapshot_id'])
        self.assertEqual(projected['economics_policy']['event_proof_hash'],
                         gate['economics_policy']['event_proof_hash'])
        self.assertEqual(projected['target_ladder'][0]['price'],gate['target_ladder'][0]['price'])
        self.assertLess(len(json.dumps(out)),len(original)/3)
        self.assertEqual(json.dumps(prepared,sort_keys=True,default=str),original)

    def test_real_accounting_refusal_logs_projection_and_preserves_full_audits(self):
        import veritas_portfolio as VP
        import veritas_trend_entry as VTE
        from test_veritas_execution_snapshot import EntryAccountingDB
        clock=datetime(2026,10,7,16,0,tzinfo=timezone.utc)
        row={'asset':'ETH','horizon':'1h','research_decision':'LONG','price':2700.,
             'source_names':{'primary':'Binance spot'},'source_gate_pass':True,
             'market_open':True,'observed_at':clock.isoformat(),
             '_execution_audit':{},'_admission_audit':{}}
        prepared=refusal()
        proof=UnreadableGraph(bars=[UnsafeValue()]*100000)
        prepared['gate']['structural_economics_context']=proof
        db=EntryAccountingDB()
        stdout=io.StringIO()
        with patch.object(VP.VPE,'prepare',return_value=prepared), \
                patch.object(VTE,'prepare_row',side_effect=lambda value,*args:value), \
                patch.object(VP.VPG,'publish_quote'), \
                patch.object(VP.VAT,'record_fill',wraps=VP.VAT.record_fill) as record, \
                redirect_stdout(stdout):
            result=VP.CANONICAL_ACCOUNTING_OPEN_OR_ADD(db,{},'Champion','ETH','LONG',
                2700.,.1,1000000.,clock,row,'TEST_REFUSAL_LOG')
        self.assertEqual(result,0.)
        self.assertEqual(len(stdout.getvalue().splitlines()),1)
        out=json.loads(stdout.getvalue())
        self.assertEqual(out['event'],'PAPER_ENTRY_BLOCKED_FINAL')
        self.assertEqual(out['decision']['reason'],'FINAL_EXECUTION_ECONOMICS')
        self.assertLess(len(stdout.getvalue()),6000)
        record.assert_called_once()
        self.assertIs(record.call_args.args[1],prepared)
        audit=row['_execution_audit']
        self.assertEqual(audit['status'],'BLOCKED')
        self.assertIs(audit['fill_economics_gate'],prepared['gate'])
        self.assertIs(audit['fill_economics_gate']['structural_economics_context'],proof)
        self.assertFalse(row['_admission_audit']['FILL']['open'])
        self.assertFalse(db.orders)
        self.assertIsNone(db.position)
        self.assertTrue(all(query.startswith('SELECT ') for query,_ in db.queries),db.queries)


if __name__ == '__main__':
    unittest.main()
