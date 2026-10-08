"""Exact AMI reducers survive bounded staged reads and interrupted checkpoints."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import veritas_asset_management_intelligence as AMI
import veritas_scorecard_delivery as DELIVERY
import veritas_learning_state as STORE
from veritas_maintenance import MaintenanceDeferred
from test_veritas_management_intelligence import _FakeConn
import test_veritas_scorecard_delivery as DELIVERY_TESTS
from test_veritas_scorecard_delivery import QueryCanceled, EPOCH


class ScorecardStageTests(unittest.TestCase):
    setUp = DELIVERY_TESTS.ScorecardDeliveryTests.setUp
    load_saved = DELIVERY_TESTS.ScorecardDeliveryTests.load_saved
    save = DELIVERY_TESTS.ScorecardDeliveryTests.save
    sample_rows = DELIVERY_TESTS.ScorecardDeliveryTests.sample_rows
    chunk_rows = DELIVERY_TESTS.ScorecardDeliveryTests.chunk_rows
    connect = DELIVERY_TESTS.ScorecardDeliveryTests.connect
    step = DELIVERY_TESTS.ScorecardDeliveryTests.step
    refresh = DELIVERY_TESTS.ScorecardDeliveryTests.refresh
    completed = DELIVERY_TESTS.ScorecardDeliveryTests.completed

    def history(self, n=2200):
        base = datetime(2026,9,1,tzinfo=timezone.utc)
        rows = []
        for i in range(n):
            direction = ('LONG','LONG','SHORT','NO_TRADE')[(i//11)%4]
            row = {'event_ts':base+timedelta(seconds=(i//3)*600),
                   'asset':('BTC','ETH','GOLD')[i%3], 'horizon':('5m','1h')[(i//3)%2],
                   'dp':{'decision':direction,'regime':('UP','DOWN')[(i//17)%2],
                         'agents':[{'direction':'LONG','confidence':.7},{'direction':'SHORT','confidence':.2}],
                         'knowledge_shadow_matches': bool(i%3),
                         'knowledge_cio_adjustment':{'score_with_experience': None if i%7 else 0,
                                                     'score':.02 if i%5 else 0}},
                   'op':{'forward_return': None if i%29==0 else 'bad' if i%31==0 else .025 if i%2 else -.018}}
            rows.append(row)
        return rows

    def test_complete_2200_sample_exactly_matches_original_score_and_all_evidence(self):
        self.rows = self.history()
        legacy = _FakeConn()
        legacy.decisions = sorted(deepcopy(self.rows),key=lambda row:row['event_ts'],reverse=True)
        expected = AMI._build_scorecard_unlocked(lambda:legacy,
            {'status':'MEASURABLE','index_vs_start':110},EPOCH,cache_seconds=0,publish=False)
        while True:
            result = self.step()
            self.assertEqual(len(self.connections[-1].pending) <= 2,True)
            if result['status'] == 'OK':
                break
            self.assertEqual(result['status'],'PROGRESS')
            self.assertIsNone(AMI.cached_scorecard(EPOCH)['score'])
            work = self.durable.get(DELIVERY.WORK_SLOT,{}).get('payload')
            if work:
                self.assertLessEqual(len(work.get('episodes',[])),360)
                self.assertLessEqual(len(STORE._json(work,DELIVERY.WORK_BYTES).encode()),DELIVERY.WORK_BYTES)
        actual = AMI.cached_scorecard(EPOCH)
        # Baseline creation clocks differ; every score/evidence/comparison value
        # must still match the existing monolithic reducer exactly.
        actual['benchmarks']['rollout_absolute_score']['captured_at'] = expected['benchmarks']['rollout_absolute_score']['captured_at']
        for key,value in expected.items():
            self.assertEqual(actual[key],value,key)
        self.assertEqual(result['processed'],2200)
        self.assertEqual(self.sample.call_count,1)
        self.assertEqual(sum(len(call.args[1]) for call in self.chunk.call_args_list),2200)
        self.assertTrue(all(len(call.args[1]) <= 64 for call in self.chunk.call_args_list))

    def test_equal_timestamps_and_gap_boundaries_match_existing_reducer_across_chunks(self):
        rows = self.history(250)
        same = datetime(2026,9,1,tzinfo=timezone.utc)
        for i,row in enumerate(rows):
            row['asset']='BTC'; row['horizon']='5m'
            row['event_ts']=same+timedelta(seconds=(i//67)*900+(1 if i==134 else 0))
        ordered = sorted(rows,key=lambda row:str(row['event_ts']))
        expected = AMI._independent_episodes(ordered,360)
        work={'episodes':[],'previous':[]}
        for start in range(0,len(ordered),64):
            DELIVERY._consume_decision_chunk(vars(AMI),work,deepcopy(ordered[start:start+64]))
            # Round-trip every intermediate state through the strict durable
            # JSON codec, including timestamp and tuple-key reconstruction.
            import json
            work=json.loads(STORE._json(work,DELIVERY.WORK_BYTES))
        self.assertEqual([{key:e[key] for key in ('asset','horizon','regime','decision','forward_return','reference_decision')}
                          for e in work['episodes']],
                         [{key:e[key] for key in ('asset','horizon','regime','decision','forward_return','reference_decision')}
                          for e in expected])

    def test_restart_or_lost_outer_cursor_resumes_committed_offset_without_new_selection(self):
        self.rows=self.history()
        self.step(); self.step()
        first=self.step()
        before=first['processed']
        self.assertGreater(before,0)
        AMI._SNAPSHOT=None; AMI._RESTORED_EPOCH=None
        self.assertEqual(self.step({'stage':'sample'})['status'],'PROGRESS')
        after=self.step({'stage':'sample','offset':0})
        self.assertGreater(after['processed'],before)
        self.assertEqual(self.sample.call_count,1)
        self.assertEqual(sum(len(call.args[1]) for call in self.chunk.call_args_list),after['processed'])
        self.refresh()
        self.assertEqual(AMI.cached_scorecard(EPOCH)['status'],'OK')

    def test_failed_chunk_transaction_cannot_advance_or_publish_partial_score(self):
        self.rows=self.history()
        original=self.completed()
        self.step(); self.step()
        before=deepcopy(self.durable[DELIVERY.WORK_SLOT])
        calls=0
        def fail_second(connection,pairs):
            nonlocal calls
            calls+=1
            if calls==2:
                raise QueryCanceled('injected second chunk failure')
            return self.chunk_rows(connection,pairs)
        self.chunk.side_effect=fail_second
        with self.assertRaises(QueryCanceled):
            self.step()
        self.assertEqual(self.durable[DELIVERY.WORK_SLOT],before)
        self.assertEqual(AMI.cached_scorecard(EPOCH)['score'],original['score'])
        self.chunk.side_effect=self.chunk_rows
        result=self.step()
        self.assertGreater(result['processed'],0)
        self.assertNotIn('intelligence_scorecard',self.durable)

    def test_sample_failure_reports_actual_phase_and_keeps_durable_state_and_last_good(self):
        original=self.completed()
        for expired,broken_emit in ((False,False),(True,False),(True,True)):
            with self.subTest(expired=expired,broken_emit=broken_emit):
                self.durable.clear(); AMI._RESTORED_EPOCH=None
                self.sample.side_effect=self.sample_rows; self.query_failure=None
                self.step()
                if expired:
                    self.step()
                    self.durable[DELIVERY.WORK_SLOT]['payload']['started_at']=DELIVERY.time.time()-1801
                    self.assertEqual(self.durable[DELIVERY.WORK_SLOT]['payload']['stage'],'decisions')
                before=deepcopy(self.durable)
                events=[]
                def emit(event,**fields):
                    events.append((event,fields))
                    if broken_emit:
                        raise OSError('private logging failure')
                self.context.sql_timeout_ms=350
                self.context.lane=SimpleNamespace(current_budget=lambda:{'remaining_seconds':3.2},emit=emit)
                self.query_failure='synthetic_sample_failure'
                self.sample.side_effect=lambda c:c.execute('SELECT synthetic_sample_failure')
                with self.assertRaises(QueryCanceled):
                    self.step()
                self.assertEqual(self.durable,before)
                self.assertTrue(self.connections[-1].rolled_back)
                current=AMI.cached_scorecard(EPOCH)
                self.assertEqual(current['score'],original['score'])
                self.assertEqual(current['refresh_stage'],'sample')
                self.assertEqual(current['last_refresh_error'],'QueryCanceled')
                self.assertEqual(current['refresh_last_sql_timeout_ms'],350)
                self.assertEqual(current['refresh_last_sql_budget_seconds'],3.2)
                self.assertEqual(events,[('ami_refresh_error',dict(status='ERROR',error_type='QueryCanceled',
                    stage='sample',last_sql_timeout_ms=350,last_sql_budget_seconds=3.2))])
                self.assertNotIn('private',str((current,events)))
                self.query_failure=None

    def test_error_diagnostic_keeps_actual_reduced_cap_and_unknown_failed_setup_cap(self):
        self.rows=self.history(130)
        self.context.sql_timeout_ms=2000
        self.step(); self.step()
        budget={'remaining_seconds':3.2}
        self.context.lane=SimpleNamespace(current_budget=lambda:dict(budget))
        self.query_failure='synthetic_chunk_failure'
        def fail_chunk(c,pairs):
            self.context.sql_timeout_ms=2000
            c.execute('SELECT synthetic_chunk_failure')
        self.chunk.side_effect=fail_chunk
        with self.assertRaises(QueryCanceled):
            self.step()
        current=AMI.cached_scorecard(EPOCH)
        self.assertEqual(current['refresh_stage'],'decisions')
        self.assertEqual(current['refresh_last_sql_timeout_ms'],1000)
        self.assertEqual(current['refresh_last_sql_budget_seconds'],3.2)
        self.query_failure='SET LOCAL statement_timeout'
        with self.assertRaises(QueryCanceled):
            self.step()
        current=AMI.cached_scorecard(EPOCH)
        self.assertEqual(current['refresh_stage'],'work_state')
        self.assertIsNone(current['refresh_last_sql_timeout_ms'])
        self.assertIsNone(current['refresh_last_sql_budget_seconds'])

    def test_cooperative_chunk_deferral_rolls_back_and_does_not_trust_exception_class_name(self):
        self.rows=self.history(130)
        self.context.sql_timeout_ms=350
        self.completed()
        previous_cache=deepcopy(AMI._CACHE)
        previous_snapshot=deepcopy(AMI._SNAPSHOT)
        lookalike=type('MaintenanceDeferred',(Exception,),{})('private error details')
        lookalike.status='DEFERRED_TIME_BUDGET'
        for error,cooperative in ((MaintenanceDeferred('DEFERRED_TIME_BUDGET',reason='private budget details'),True),
                                  (lookalike,False)):
            with self.subTest(cooperative=cooperative):
                self.durable.clear(); AMI._RESTORED_EPOCH=None
                self.context.check.side_effect=None
                self.chunk.side_effect=self.chunk_rows
                events=[]
                self.context.lane=SimpleNamespace(current_budget=lambda:{'remaining_seconds':6.},
                    emit=lambda event,**fields:events.append((event,fields)))
                self.step(); self.step()
                before=deepcopy(self.durable)
                consumed={'chunk':False}
                def chunk(c,pairs):
                    c.execute('SELECT d.event_ts FROM ledger_events d LIMIT 1')
                    rows=self.chunk_rows(c,pairs)
                    consumed['chunk']=True
                    return rows
                def check():
                    if consumed['chunk']:
                        raise error
                self.chunk.side_effect=chunk
                self.context.check.side_effect=check
                with self.assertRaises(type(error)) as raised:
                    self.step()
                self.assertIs(raised.exception,error)
                self.assertTrue(consumed['chunk'])
                self.assertTrue(self.connections[-1].rolled_back)
                self.assertFalse(self.connections[-1].committed)
                self.assertEqual(self.durable,before)
                self.assertEqual(AMI._CACHE,previous_cache)
                self.assertEqual(AMI._SNAPSHOT,previous_snapshot)
                current=AMI.cached_scorecard(EPOCH)
                status='DEFERRED_TIME_BUDGET' if cooperative else 'ERROR'
                error_type=None if cooperative else 'MaintenanceDeferred'
                self.assertEqual(current['refresh_status'],status)
                self.assertEqual(current['last_refresh_error'],error_type)
                self.assertEqual(current['refresh_stage'],'decisions')
                self.assertIsNone(current['refresh_processed'])
                self.assertEqual(events,[('ami_refresh_deferred' if cooperative else 'ami_refresh_error',
                    dict(status=status,error_type=error_type,stage='decisions',
                         last_sql_timeout_ms=350,last_sql_budget_seconds=6.))])
                self.assertNotIn('private',str((current,events)))
        self.context.check.side_effect=None

    def test_cooperative_deferral_after_commit_recovers_completed_score_without_recomputation(self):
        self.completed()
        previous_cache=deepcopy(AMI._CACHE)
        previous_snapshot=deepcopy(AMI._SNAPSHOT)
        events=[]
        self.context.lane=SimpleNamespace(current_budget=lambda:{'remaining_seconds':6.},
            emit=lambda event,**fields:events.append((event,fields)))
        for _ in range(20):
            result=self.step()
            if result.get('stage')=='publish':
                break
        else:
            self.fail('publish phase missing')
        deferred=MaintenanceDeferred('DEFERRED_TIME_BUDGET',reason='private checkpoint details')
        def after_commit():
            if self.durable.get(DELIVERY.WORK_SLOT,{}).get('payload',{}).get('stage')=='complete':
                raise deferred
        self.context.check.side_effect=after_commit
        with self.assertRaises(MaintenanceDeferred) as raised:
            self.step(result['cursor'])
        self.assertIs(raised.exception,deferred)
        self.assertTrue(self.connections[-1].committed)
        self.assertFalse(self.connections[-1].rolled_back)
        complete=deepcopy(self.durable['intelligence_scorecard'])
        self.assertEqual(self.durable[DELIVERY.WORK_SLOT]['payload']['stage'],'complete')
        self.assertEqual(AMI._CACHE,previous_cache)
        self.assertEqual(AMI._SNAPSHOT,previous_snapshot)
        current=AMI.cached_scorecard(EPOCH)
        self.assertEqual(current['refresh_status'],'DEFERRED_TIME_BUDGET')
        self.assertIsNone(current['last_refresh_error'])
        self.assertEqual(current['refresh_stage'],'commit')
        self.assertIsNone(current['refresh_processed'])
        self.assertEqual(events,[('ami_refresh_deferred',dict(status='DEFERRED_TIME_BUDGET',
            error_type=None,stage='commit',last_sql_timeout_ms=current['refresh_last_sql_timeout_ms'],
            last_sql_budget_seconds=current['refresh_last_sql_budget_seconds']))])
        self.assertNotIn('private',str((current,events)))
        queries=self.sample.call_count,self.chunk.call_count
        self.context.check.side_effect=None
        recovered=self.step(result['cursor'])
        self.assertEqual(recovered['status'],'OK')
        self.assertEqual((self.sample.call_count,self.chunk.call_count),queries)
        self.assertEqual(AMI._SNAPSHOT[2],complete['payload']['scorecard'])
        self.assertEqual(AMI._SNAPSHOT[1],complete['observed_at'].timestamp())
        self.assertEqual(AMI.cached_scorecard(EPOCH)['refresh_status'],'OK')
        self.assertEqual(len(events),1)

    def test_final_commit_before_budget_exception_is_recovered_without_recomputation(self):
        for _ in range(10):
            result=self.step()
            if result.get('stage')=='publish':
                break
        else:
            self.fail('publish phase missing')
        def after_commit():
            if self.durable.get(DELIVERY.WORK_SLOT,{}).get('payload',{}).get('stage')=='complete':
                raise TimeoutError('outer checkpoint interrupted after durable publication')
        self.context.check.side_effect=after_commit
        with self.assertRaises(TimeoutError):
            self.step(result['cursor'])
        self.assertIn('intelligence_scorecard',self.durable)
        self.assertIsNone(AMI._SNAPSHOT)
        query_calls=self.sample.call_count,self.chunk.call_count
        self.context.check.side_effect=None
        recovered=self.step(result['cursor'])
        self.assertEqual(recovered['status'],'OK')
        self.assertEqual((self.sample.call_count,self.chunk.call_count),query_calls)
        self.assertEqual(AMI.cached_scorecard(EPOCH)['status'],'OK')

    def test_chunk_loop_reserves_query_and_checkpoint_budget(self):
        self.rows=self.history()
        self.step(); self.step()
        budget={'remaining_seconds':5.2}
        self.context.sql_timeout_ms=2000
        self.context.lane=SimpleNamespace(current_budget=lambda:dict(budget))
        def timed(connection,pairs):
            budget['remaining_seconds']-=.7
            return self.chunk_rows(connection,pairs)
        self.chunk.side_effect=timed
        result=self.step()
        self.assertEqual([len(call.args[1]) for call in self.chunk.call_args_list],[64,64,51,28])
        self.assertEqual(result['processed'],207)
        self.assertGreater(budget['remaining_seconds'],2)

    def test_checkpoint_cap_uses_post_serialization_budget_and_preserves_frozen_progress(self):
        self.rows=self.history(130)
        self.completed()
        previous_cache=deepcopy(AMI._CACHE)
        previous_snapshot=deepcopy(AMI._SNAPSHOT)
        # requested, slice budget, serialized budget, chunk cap, write cap, pairs
        cases=((2000,3.2,2.25,1000,2000,32),(350,2.45,.6,250,350,8),
               (9999,3.2,2.25,1000,2000,32),(2000,3.2,2.1,1000,1000,32),
               (2000,6.,.3,2000,2000,64),(350,6.,.3,350,350,11))
        for requested,slice_budget,serialized_budget,chunk_cap,write_cap,count in cases:
            with self.subTest(requested=requested,serialized_budget=serialized_budget):
                self.durable.clear(); AMI._RESTORED_EPOCH=None
                self.load.side_effect=self.load_saved
                self.publish.side_effect=self.save
                self.context.sql_timeout_ms=requested
                budget={'remaining_seconds':6.}
                self.context.lane=SimpleNamespace(current_budget=lambda:dict(budget))
                self.step(); self.step()
                before=deepcopy(self.durable[DELIVERY.WORK_SLOT]['payload'])
                seen={}; trace=[]
                def load(c,name,version,**kwargs):
                    saved=self.load_saved(c,name,version,**kwargs)
                    self.assertTrue(kwargs['for_update'])
                    seen.update(connection=c,saved=saved)
                    return saved
                def one_chunk(c,pairs):
                    budget['remaining_seconds']=2.3
                    return self.chunk_rows(c,pairs)
                encode=STORE._json
                def serialize(payload,limit):
                    encoded=encode(payload,limit)
                    if limit==STORE.MAX_SNAPSHOT_BYTES:
                        self.assertEqual(seen['connection'].sql_timeout_ms,chunk_cap)
                        budget['remaining_seconds']=serialized_budget
                        trace.append('serialized')
                    return encoded
                def persist(c,name,version,payload,**kwargs):
                    self.assertEqual(name,DELIVERY.WORK_SLOT)
                    self.assertIs(c,seen['connection'])
                    self.assertIs(kwargs['locked_snapshot'],seen['saved'])
                    self.assertEqual(kwargs['locked_snapshot']['name'],DELIVERY.WORK_SLOT)
                    before_write=kwargs['before_write']
                    def write_boundary():
                        self.assertEqual(trace,['serialized'])
                        before_write()
                        self.assertEqual(c.sql_timeout_ms,write_cap)
                        trace.append('write')
                    kwargs['before_write']=write_boundary
                    return self.save(c,name,version,payload,**kwargs)
                self.load.side_effect=load
                self.chunk.reset_mock(); self.chunk.side_effect=one_chunk
                self.publish.side_effect=persist
                budget['remaining_seconds']=slice_budget
                with patch.object(STORE,'_json',side_effect=serialize):
                    result=self.step()
                self.assertEqual(trace,['serialized','write'])
                self.assertEqual(result['status'],'PROGRESS')
                self.assertEqual(result['processed'],count)
                self.assertEqual(self.chunk.call_count,1)
                self.assertEqual(self.chunk.call_args.args[1],before['pairs'][:count])
                after=self.durable[DELIVERY.WORK_SLOT]['payload']
                for key in ('pairs','cycle_id','started_at','learning_progress'):
                    self.assertEqual(after[key],before[key],key)
                self.assertEqual(after['offset'],count)
                self.assertEqual(AMI._CACHE,previous_cache)
                self.assertEqual(AMI._SNAPSHOT,previous_snapshot)
                settings=[sql for sql,args in self.connections[-1].calls
                          if sql.startswith('SET LOCAL statement_timeout') and ';' not in sql]
                expected=[]
                if chunk_cap<min(requested,2000):
                    expected.append("SET LOCAL statement_timeout = '"+str(chunk_cap)+"ms'")
                if write_cap>chunk_cap:
                    expected.append("SET LOCAL statement_timeout = '"+str(write_cap)+"ms'")
                self.assertEqual(settings,expected)

    def test_checkpoint_headroom_lost_during_set_defers_before_write_and_retries_same_work(self):
        self.rows=self.history(130)
        self.context.sql_timeout_ms=2000
        self.completed()
        previous_cache=deepcopy(AMI._CACHE)
        previous_snapshot=deepcopy(AMI._SNAPSHOT)
        self.step(); self.step()
        before=deepcopy(self.durable)
        events=[]; seen={}; consume={'enabled':True}
        budget={'remaining_seconds':3.2}
        self.context.lane=SimpleNamespace(current_budget=lambda:dict(budget),
            emit=lambda event,**fields:events.append((event,fields)))
        connect=self.connect
        def timed_connect():
            connection=connect()
            execute=connection.execute
            def timed_execute(sql,args=None):
                result=execute(sql,args)
                if sql=="SET LOCAL statement_timeout = '2000ms'" and consume['enabled']:
                    budget['remaining_seconds']=2.19
                return result
            connection.execute=timed_execute
            return connection
        self.connect=timed_connect
        def one_chunk(c,pairs):
            budget['remaining_seconds']=2.3
            return self.chunk_rows(c,pairs)
        def persist(c,name,version,payload,**kwargs):
            seen['connection']=c
            before_write=kwargs['before_write']
            def guard():
                try:
                    before_write()
                except MaintenanceDeferred as exc:
                    seen['exception']=exc
                    raise
            kwargs['before_write']=guard
            return self.save(c,name,version,payload,**kwargs)
        self.chunk.side_effect=one_chunk
        self.publish.side_effect=persist
        with self.assertRaises(MaintenanceDeferred) as raised:
            self.step()
        self.assertIs(raised.exception,seen['exception'])
        self.assertEqual(raised.exception.result(),{'status':'DEFERRED_SCORECARD_BUDGET',
                                                    'reason':'CHECKPOINT_SQL_HEADROOM'})
        self.assertEqual(seen['connection'].sql_timeout_ms,2000)
        self.assertTrue(self.connections[-1].rolled_back)
        self.assertFalse(self.connections[-1].committed)
        self.assertEqual(self.connections[-1].pending,{})
        self.assertEqual(self.durable,before)
        self.assertEqual(AMI._CACHE,previous_cache)
        self.assertEqual(AMI._SNAPSHOT,previous_snapshot)
        current=AMI.cached_scorecard(EPOCH)
        self.assertEqual(current['refresh_status'],'DEFERRED_SCORECARD_BUDGET')
        self.assertIsNone(current['last_refresh_error'])
        self.assertIsNone(current['refresh_processed'])
        self.assertEqual(events,[('ami_refresh_deferred',dict(status='DEFERRED_SCORECARD_BUDGET',
            error_type=None,stage='checkpoint',last_sql_timeout_ms=1000,last_sql_budget_seconds=2.3))])
        consume['enabled']=False; budget['remaining_seconds']=3.2
        retried=self.step({'stage':'decisions','offset':999})
        self.assertEqual(retried['status'],'PROGRESS')
        self.assertEqual(retried['processed'],32)
        self.assertEqual(self.sample.call_count,1)
        self.assertEqual([call.args[1] for call in self.chunk.call_args_list],
                         [before[DELIVERY.WORK_SLOT]['payload']['pairs'][:32]]*2)

    def test_checkpoint_set_and_short_cap_write_failures_remain_original_sql_errors(self):
        self.rows=self.history(130)
        self.context.sql_timeout_ms=2000
        self.completed()
        previous_cache=deepcopy(AMI._CACHE)
        previous_snapshot=deepcopy(AMI._SNAPSHOT)
        connect=self.connect
        for failed_at,checkpoint_budget in (('set',2.3),('write',2.1)):
            with self.subTest(failed_at=failed_at):
                self.connect=connect
                self.durable.clear(); AMI._RESTORED_EPOCH=None
                self.publish.side_effect=self.save
                self.chunk.side_effect=self.chunk_rows
                budget={'remaining_seconds':6.}; events=[]; seen={}
                self.context.lane=SimpleNamespace(current_budget=lambda:dict(budget),
                    emit=lambda event,**fields:events.append((event,fields)))
                self.step(); self.step()
                before=deepcopy(self.durable)
                error=QueryCanceled('private checkpoint SQL details')
                write_sql='SELECT d.event_ts FROM ledger_events d LIMIT 1'
                failed_sql="SET LOCAL statement_timeout = '2000ms'" if failed_at=='set' else write_sql
                def failing_connect():
                    connection=connect()
                    execute=connection.execute
                    def fail(sql,args=None):
                        if sql==failed_sql:
                            seen['attempted']=True
                            raise error
                        return execute(sql,args)
                    connection.execute=fail
                    return connection
                def one_chunk(c,pairs):
                    budget['remaining_seconds']=checkpoint_budget
                    return self.chunk_rows(c,pairs)
                def persist(c,name,version,payload,**kwargs):
                    seen['connection']=c
                    before_write=kwargs['before_write']
                    def write_boundary():
                        before_write()
                        # The unit connection uses a supported reader as the
                        # stand-in for the snapshot SQL immediately after hook.
                        c.execute(write_sql)
                    kwargs['before_write']=write_boundary
                    return self.save(c,name,version,payload,**kwargs)
                self.connect=failing_connect
                self.chunk.side_effect=one_chunk
                self.publish.side_effect=persist
                budget['remaining_seconds']=3.2
                with self.assertRaises(QueryCanceled) as raised:
                    self.step()
                self.assertIs(raised.exception,error)
                self.assertTrue(seen['attempted'])
                self.assertEqual(seen['connection'].sql_timeout_ms,1000)
                self.assertTrue(self.connections[-1].rolled_back)
                self.assertEqual(self.connections[-1].pending,{})
                self.assertEqual(self.durable,before)
                self.assertEqual(AMI._CACHE,previous_cache)
                self.assertEqual(AMI._SNAPSHOT,previous_snapshot)
                current=AMI.cached_scorecard(EPOCH)
                self.assertEqual(current['refresh_status'],'ERROR')
                self.assertEqual(current['last_refresh_error'],'QueryCanceled')
                self.assertIsNone(current['refresh_processed'])
                self.assertEqual(events,[('ami_refresh_error',dict(status='ERROR',error_type='QueryCanceled',
                    stage='checkpoint',last_sql_timeout_ms=1000,last_sql_budget_seconds=checkpoint_budget))])
                self.assertNotIn('private',str((current,events)))

    def test_short_budget_can_decode_with_reduced_timeout_instead_of_starving(self):
        self.rows=self.history()
        self.step(); self.step()
        budget={'remaining_seconds':3.2}
        self.context.sql_timeout_ms=2000
        self.context.lane=SimpleNamespace(current_budget=lambda:dict(budget))
        def timed(connection,pairs):
            budget['remaining_seconds']=3.2 if self.chunk.call_count==1 else 2.1
            return self.chunk_rows(connection,pairs)
        self.chunk.side_effect=timed
        result=self.step()
        self.assertEqual(result['status'],'PROGRESS')
        self.assertEqual(result['processed'],64)
        self.assertEqual([len(call.args[1]) for call in self.chunk.call_args_list],[32,32])
        self.assertIn(("SET LOCAL statement_timeout = '1000ms'",None),self.connections[-1].calls)
        before=deepcopy(self.durable[DELIVERY.WORK_SLOT])
        calls=self.chunk.call_count
        publications=self.publish.call_count
        deferred=self.step()
        self.assertEqual(deferred['status'],'DEFERRED_SCORECARD_BUDGET')
        self.assertEqual(deferred['processed'],64)
        self.assertEqual(self.chunk.call_count,calls)
        self.assertEqual(self.publish.call_count,publications)
        self.assertEqual(self.connections[-1].calls,
                         [("SET LOCAL statement_timeout = '2000ms'; SET LOCAL lock_timeout = '250ms'",None)])
        self.assertEqual(self.durable[DELIVERY.WORK_SLOT],before)

    def test_chunk_size_uses_the_configured_cap_without_exceeding_64(self):
        self.rows=self.history(130)
        cases=((0,1,None),(200,6,None),(350,11,None),(1000,32,None),
               (2000,64,None),(9999,64,None),(350,11,2000))
        for requested,expected,later_estimate in cases:
            with self.subTest(requested=requested,later_estimate=later_estimate):
                self.durable.clear(); AMI._RESTORED_EPOCH=None
                self.load.side_effect=self.load_saved
                self.context.sql_timeout_ms=requested
                budget={'remaining_seconds':6.}
                self.context.lane=SimpleNamespace(current_budget=lambda:dict(budget))
                self.step(); self.step()
                frozen=deepcopy(self.durable[DELIVERY.WORK_SLOT]['payload']['pairs'])
                if later_estimate is not None:
                    def after_configuration(c,name,version,**kwargs):
                        self.context.sql_timeout_ms=later_estimate
                        return self.load_saved(c,name,version,**kwargs)
                    self.load.side_effect=after_configuration
                self.chunk.reset_mock()
                def one_chunk(c,pairs):
                    budget['remaining_seconds']=2.1
                    return self.chunk_rows(c,pairs)
                self.chunk.side_effect=one_chunk
                result=self.step()
                self.assertEqual(result['status'],'PROGRESS')
                self.assertEqual(result['processed'],expected)
                self.assertEqual(self.chunk.call_count,1)
                self.assertEqual(self.chunk.call_args.args[1],frozen[:expected])

    def test_repeated_short_slices_resume_all_2200_pairs_and_match_legacy_score(self):
        self.rows=self.history()
        def sample_with_boundary_duplicates(c):
            selected=sorted(self.sample_rows(c),key=lambda row:str(row['event_ts']))
            selected[32]=deepcopy(selected[31])
            selected[64]=deepcopy(selected[63])
            return selected
        self.sample.side_effect=sample_with_boundary_duplicates
        self.context.sql_timeout_ms=2000
        self.step(); self.step()
        frozen=deepcopy(self.durable[DELIVERY.WORK_SLOT]['payload']['pairs'])
        self.assertEqual(len(frozen),2200)
        self.assertEqual(frozen[31],frozen[32])
        self.assertEqual(frozen[63],frozen[64])
        legacy=_FakeConn()
        legacy.decisions=[deepcopy(self.rows[pair[0]-1]) for pair in frozen]
        expected=AMI._build_scorecard_unlocked(lambda:legacy,
            {'status':'MEASURABLE','index_vs_start':110},EPOCH,cache_seconds=0,publish=False)
        budget={'remaining_seconds':3.2}
        self.context.lane=SimpleNamespace(current_budget=lambda:dict(budget))
        def one_short_chunk(c,pairs):
            budget['remaining_seconds']=2.1
            return self.chunk_rows(c,pairs)
        self.chunk.side_effect=one_short_chunk
        cursor=None; restarted=False
        for _ in range(100):
            budget['remaining_seconds']=3.2
            result=self.step(cursor)
            cursor=result['cursor']
            if result['status']=='OK':
                break
            self.assertEqual(result['status'],'PROGRESS')
            self.assertIsNone(AMI._SNAPSHOT)
            if not restarted and result.get('processed',0)>=160:
                # Lose only process-local state and the caller cursor. The
                # committed frozen sample and reducer remain the source.
                AMI._SNAPSHOT=None; AMI._RESTORED_EPOCH=None
                cursor={}; restarted=True
        else:
            self.fail('short-slice scorecard did not finish')
        self.assertTrue(restarted)
        self.assertEqual(self.sample.call_count,1)
        batches=[call.args[1] for call in self.chunk.call_args_list]
        self.assertTrue(all(0<len(pairs)<=32 for pairs in batches))
        self.assertEqual([pair for pairs in batches for pair in pairs],frozen)
        self.assertEqual(result['processed'],2200)
        actual=AMI.cached_scorecard(EPOCH)
        actual['benchmarks']['rollout_absolute_score']['captured_at']=expected['benchmarks']['rollout_absolute_score']['captured_at']
        for key,value in expected.items():
            self.assertEqual(actual[key],value,key)

    def test_oversized_work_is_rejected_without_truncation_or_partial_publication(self):
        original=self.completed()
        self.rows=self.history(64)
        for row in self.rows:
            row['dp']['regime']='large-regime-'*10000
        self.step();self.step()
        before=deepcopy(self.durable[DELIVERY.WORK_SLOT])
        with self.assertRaisesRegex(ValueError,'payload exceeds byte limit'):
            self.step()
        self.assertEqual(self.durable[DELIVERY.WORK_SLOT],before)
        self.assertEqual(AMI.cached_scorecard(EPOCH)['score'],original['score'])

    def test_completed_cursor_cooldown_does_not_open_database(self):
        result=self.refresh()
        count=len(self.connections)
        repeat=self.step(result['cursor'])
        self.assertEqual(repeat['status'],'NO_WORK')
        self.assertEqual(repeat['reason'],'SCORECARD_REFRESH_NOT_DUE')
        self.assertEqual(len(self.connections),count)


if __name__=='__main__':
    unittest.main()
