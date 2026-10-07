"""Synthetic latency invariants; never broker orders or performance promises."""
from copy import deepcopy
from datetime import timedelta
import threading
import unittest
from unittest.mock import Mock, patch

import veritas_structural_breakout as SB
import veritas_structural_validation_cache as SVC
import veritas_breakout_runtime as BR
import veritas_timeframe_data as TFD
from veritas_book_lock import PriorityRLock
from test_veritas_structural_breakout import raw_at, POLICY


def context_row():
    raw, clock = raw_at()
    context = SB.build_context(raw, '1h', clock, config=POLICY)
    assert context.get('event') and SB.validate_event(context['event'])['eligible']
    return dict(raw, horizon='1h', research_decision='LONG',
                timeframe_entry_context=context, trade_plan={}), clock


class ProofMemoTests(unittest.TestCase):
    def setUp(self):
        self.row, self.clock = context_row()
        self.event = self.row['timeframe_entry_context']['event']
        SVC.clear()

    def tearDown(self):
        SVC.clear()

    def test_identical_static_proof_reuses_success_not_admission(self):
        expected = SB._validate_event_uncached(self.event)
        self.assertEqual(SB.validate_event(self.event), expected)
        with patch.object(SB, '_validate_event_uncached', side_effect=AssertionError('proof recomputed')):
            self.assertEqual(SB.validate_event(deepcopy(self.event)), expected)
        self.assertEqual(SVC.snapshot()['hits'], 1)
        stale = SB.entry_gate(self.row['timeframe_entry_context'], self.row['price'],
                              'LONG', self.clock+timedelta(days=1))
        self.assertFalse(stale['eligible'])
        self.assertEqual(stale['reason'], 'EXECUTION_QUOTE_STALE')

    def test_all_nested_mutations_and_supplied_contract_revalidate(self):
        SB.validate_event(self.event)
        cases=[]
        for field in ('stop_price','target_price','trigger_level','signal_at'):
            e=deepcopy(self.event);e[field]+=0.001;cases.append(e)
        e=deepcopy(self.event);e['target_ladder'][0]['price']+=1;cases.append(e)
        e=deepcopy(self.event);e['atr_proof']['bars'][-1]['close']+=1;cases.append(e)
        e=deepcopy(self.event);e['source_identity']['contract_id']='FOREIGN';cases.append(e)
        e=deepcopy(self.event);e['policy']['stop_buffer_atr']+=.1;cases.append(e)
        e=deepcopy(self.event);e['proof_hash']='INVALID';cases.append(e)
        for event in cases:
            with self.subTest(event=event.get('proof_hash')):
                self.assertEqual(SB.validate_event(event), SB._validate_event_uncached(event))
                self.assertFalse(SB.validate_event(event)['eligible'])
        foreign=dict(self.event['source_identity'], contract_id='FOREIGN')
        self.assertFalse(SB.validate_event(self.event,foreign)['eligible'])
        self.assertTrue(SB.validate_event(self.event,self.event['source_identity'])['eligible'])

    def test_result_is_detached_and_failures_are_not_promoted(self):
        result=SB.validate_event(self.event);result['eligible']=False
        self.assertTrue(SB.validate_event(self.event)['eligible'])
        for value in (None, {}, {'proof_hash':'INVALID'}, {'target_ladder':['bad']}):
            self.assertEqual(SB.validate_event(value),SB._validate_event_uncached(value))
        self.assertLessEqual(SVC.snapshot()['entries'], 2)

    def test_cache_is_bounded_and_retains_no_event_graph(self):
        # Extra non-sealed evidence does not alter validity but must participate
        # in the exact input fingerprint. Values deliberately remain synthetic.
        for n in range(SVC.MAX_ENTRIES+10):
            event=dict(self.event, test_marker=n)
            self.assertTrue(SB.validate_event(event)['eligible'])
        self.assertEqual(SVC.snapshot()['entries'], SVC.MAX_ENTRIES)
        with SVC._lock:
            self.assertTrue(all(type(key) is bytes and len(key)==32 for key in SVC._cache))
            self.assertTrue(all(set(value)=={'eligible','reason'} for value in SVC._cache.values()))
        before=SVC.snapshot()['entries']
        huge=dict(self.event, test_only_padding='x'*(SVC.MAX_FINGERPRINT_BYTES+1))
        self.assertTrue(SB.validate_event(huge)['eligible'])
        self.assertEqual(SVC.snapshot()['entries'],before)

    def test_changed_defaults_and_event_spending_do_not_reuse_an_old_key(self):
        SB.validate_event(self.event)
        misses=SVC.snapshot()['misses']
        with patch.dict(SB.DEFAULT_POLICY, {'target_one_fraction':.45}):
            self.assertTrue(SB.validate_event(self.event)['eligible'])
        self.assertGreater(SVC.snapshot()['misses'],misses)
        context=deepcopy(self.row['timeframe_entry_context'])
        context['event'].update(spent=True,spent_reason='STRUCTURAL_TARGET_ALREADY_REACHED')
        gate=SB.entry_gate(context,self.row['price'],'LONG',self.clock)
        self.assertFalse(gate['eligible'])
        self.assertEqual(gate['reason'],'STRUCTURAL_TARGET_ALREADY_REACHED')


class CausalConsumptionIndexTests(unittest.TestCase):
    def test_index_equals_original_scan_at_every_clock_including_delayed_bars(self):
        rows={'1m':[
            {'ts':i*60.,'available_at':i*60.+60.+(180. if i%7==0 else 0.),
             'high':10.+(i%9)*.1,'low':9.+(i%6)*.1}
            for i in range(60)]}
        levels=[{'level_id':str(i),'timeframe':'1m','available_at':i*35.,
                 'price':10.2 if i%2 else 9.2,
                 'kind':'resistance' if i%2 else 'support'} for i in range(40)]
        indexed=SB._first_cross_times(rows,levels)
        for before in range(0,4000,23):
            original=set()
            for level in levels:
                for bar in rows[level['timeframe']]:
                    if bar['ts']<level['available_at'] or bar['available_at']>before:continue
                    crossed=bar['high']>level['price'] if level['kind']=='resistance' else bar['low']<level['price']
                    if crossed:original.add(level['level_id']);break
            self.assertEqual({key for key,at in indexed.items() if at<=before},original)

    def test_native_input_and_levels_are_not_mutated(self):
        row,clock=context_row()
        facts,issue=SB._native_facts(row,clock.timestamp(),row['structure_source_identity'],SB._policy(POLICY))
        self.assertIsNone(issue)
        original=deepcopy((facts['rows_by_tf'],facts['levels']))
        SB._first_cross_times(facts['rows_by_tf'],facts['levels'])
        self.assertEqual((facts['rows_by_tf'],facts['levels']),original)


class BoundedRetryTests(unittest.TestCase):
    def setUp(self):
        self.row,self.clock=context_row()
        self.previous_markets,self.previous_rows=BR._markets.copy(),BR._latest_rows.copy()
        BR._markets.clear();BR._latest_rows.clear()
        self.assertTrue(BR.publish_market(self.row))
        self.ns={'lock':threading.RLock(),'last_cycle':{'summary':[]}}
        self.runtime=BR.BreakoutRuntime(self.ns,Mock(return_value={'status':'OK'}),
                                       context_builder=Mock(side_effect=AssertionError('history rebuilt during retry')))
    def tearDown(self):
        self.runtime.close()
        BR._markets.clear();BR._markets.update(self.previous_markets)
        BR._latest_rows.clear();BR._latest_rows.update(self.previous_rows)

    def test_pending_retry_bypasses_context_and_preserves_original_event(self):
        original=deepcopy(self.row['timeframe_entry_context']['event'])
        self.runtime._pending_entry_rows=[self.row]
        result=self.runtime.run_once(self.clock+timedelta(seconds=1),quotes={})
        self.runtime.context_builder.assert_not_called()
        self.runtime.entry_pass.assert_called_once()
        passed=self.runtime.entry_pass.call_args.args[0][0]
        self.assertEqual(passed['timeframe_entry_context']['event'],original)
        self.assertEqual(result['context_seconds'],0.)
        self.assertEqual(result['pending_entry_rows'],0)

    def test_repeated_busy_keeps_one_batch_and_reports_wait_not_success(self):
        self.runtime.entry_pass.return_value={'status':'BUSY','reason':'LOCAL_PAPER_BOOK_BUSY','entry_turn_reserved':True}
        self.runtime._pending_entry_rows=[self.row]
        for n in range(1,5):
            result=self.runtime.run_once(self.clock+timedelta(seconds=n),quotes={})
            self.assertEqual(result['status'],'WAITING_FOR_BOOK',result)
            self.assertEqual(result['pending_entry_rows'],1)
            self.assertEqual(result['execution_reason'],'LOCAL_PAPER_BOOK_BUSY')
        self.assertEqual(result['retry_passes'],4)
        self.runtime.context_builder.assert_not_called()
        self.runtime.entry_pass.return_value={'status':'OK'}
        self.runtime.run_once(self.clock+timedelta(seconds=5),quotes={})
        self.assertEqual(self.runtime._pending_entry_rows,[])

    def test_expired_pending_does_not_extend_signal_or_quote_time(self):
        def admission(rows,clock):
            gate=SB.entry_gate(rows[0]['timeframe_entry_context'],rows[0]['price'],'LONG',clock)
            return {'status':'OK','reason':gate['reason'],'admitted':gate['eligible']}
        self.runtime.entry_pass=admission
        self.runtime._pending_entry_rows=[self.row]
        result=self.runtime.run_once(self.clock+timedelta(hours=1),quotes={})
        self.assertFalse(result['execution']['admitted'])
        self.assertEqual(result['execution']['reason'],'EXECUTION_QUOTE_STALE')
        self.assertEqual(self.runtime._pending_entry_rows,[])

    def test_nonreserved_and_failed_callbacks_cannot_publish_execution_success(self):
        self.runtime.entry_pass.return_value={'status':'BUSY','entry_turn_reserved':False}
        self.runtime._pending_entry_rows=[self.row]
        self.runtime.run_once(self.clock,quotes={})
        self.assertEqual(self.runtime._pending_entry_rows,[])
        self.runtime.entry_pass.side_effect=RuntimeError('synthetic failure')
        self.runtime._pending_entry_rows=[self.row]
        result=self.runtime.run_once(self.clock,quotes={})
        self.assertEqual(result['status'],'ERROR')
        self.assertEqual(self.runtime._pending_entry_rows,[])

    def test_lock_diagnostics_do_not_change_priority_reentry_or_ownership(self):
        lock=PriorityRLock()
        self.assertIsNone(lock.snapshot()['owner'])
        with lock:
            with lock:
                snap=lock.snapshot()
                self.assertEqual(snap['depth'],2)
                self.assertEqual(snap['owner'],threading.current_thread().name)
                self.assertGreaterEqual(snap['held_seconds'],0.)
            self.assertEqual(lock.snapshot()['depth'],1)
        self.assertEqual(lock.snapshot()['depth'],0)
        self.assertIsNone(lock.snapshot()['owner'])
        self.assertGreaterEqual(lock.snapshot()['last_hold_seconds'],0.)


if __name__=='__main__':
    unittest.main()
