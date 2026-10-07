"""Learning queues commit bounded prefixes and resume immutable proof stages."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import veritas_continuous_learning as C
import veritas_autonomous_learning as AUTO
import veritas_learning_bridge as BRIDGE
import veritas_learning_state as STORE
import test_veritas_continuous_learning as FIXTURE

NOW = FIXTURE.NOW


def outcome(direction='LONG', stamps=()):
    forecast, _ = C.forecast_from_ledger(FIXTURE.captured())
    raw = C.resolve_forecast(forecast, FIXTURE.fresh_quote(), now=NOW)
    raw.update(direction=direction, knowledge_trials=list(stamps))
    raw.pop('evidence_hash')
    raw['evidence_hash'] = BRIDGE.digest(raw)
    return raw


class Budget:
    sql_timeout_ms = 2000
    def __init__(self, remaining=6.):
        self.remaining_seconds = remaining
    def check(self):
        if self.remaining_seconds <= 0:
            raise TimeoutError('synthetic six-second deadline')


class Result:
    def __init__(self, rows=()):
        self.rows = list(rows)
    def fetchall(self):
        return deepcopy(self.rows)
    def fetchone(self):
        return deepcopy(self.rows[0]) if self.rows else None


class QueueDB:
    def __init__(self, rows=()):
        self.rows = {row['id']:deepcopy(row) for row in rows}
        self.calls = []
        self.fail_commit = False
        self.budget = None
        self.update_cost = 0.
    @contextmanager
    def connect(self):
        db = self
        class Connection:
            @contextmanager
            def transaction(self):
                self.pending = deepcopy(db.rows)
                try:
                    yield
                    if db.fail_commit:
                        raise OSError('commit interrupted')
                    db.rows = self.pending
                finally:
                    self.pending = None
            def execute(self, sql, args=None):
                db.calls.append((sql,args))
                args = args or ()
                if 'statement_timeout' in sql or 'lock_timeout' in sql:
                    return Result()
                if sql.startswith('SELECT'):
                    rows = list(self.pending.values())
                    if 'WHERE id=%s' in sql:
                        rows = [row for row in rows if row['id']==args[0]]
                    elif "status='READY'" in sql:
                        rows = [row for row in rows if row['status']=='READY'][:1]
                    elif "status='PENDING'" in sql:
                        rows = [row for row in rows if row['status']=='PENDING' and row['due_at']<=args[0]][:args[1]]
                    return Result(rows)
                if sql.startswith('UPDATE'):
                    row = self.pending[args[-1]]
                    if "status='LEARNED'" in sql:
                        row['status']='LEARNED'
                    else:
                        row.update(status=args[0],outcome=json.loads(args[1]),learned_at=args[2])
                    if db.budget:
                        db.budget.remaining_seconds -= db.update_cost
                    return Result([row])
                if sql.startswith('DELETE'):
                    return Result()
                raise AssertionError(sql)
        yield Connection()


class BacklogContracts(unittest.TestCase):
    def setUp(self):
        self.db = QueueDB()
        self.ns = FIXTURE.namespace(self.db.connect)
        self.app = C.ContinuousLearning(self.ns)
        self.app.ready = True
        self.context = Budget()
        self.clock = patch.object(C,'clock',return_value=NOW)
        self.clock.start(); self.addCleanup(self.clock.stop)
        self.bridge = patch.object(BRIDGE,'update')
        self.bridge.start(); self.addCleanup(self.bridge.stop)
        saved = patch.object(STORE,'load_snapshot_in_transaction',return_value=None)
        saved.start(); self.addCleanup(saved.stop)

    def ready(self, direction='LONG', stamps=()):
        raw = outcome(direction,stamps)
        self.db.rows = {1:{'id':1,'entity_key':raw['episode_key'],'status':'READY','outcome':raw}}
        return raw

    def step(self, cursor=None):
        return self.app.candidates(self.context,cursor or {})

    def test_outcomes_commit_prefix_and_drain_same_32_rows_without_deadline_rollback(self):
        forecast,_ = C.forecast_from_ledger(FIXTURE.captured(at=NOW-timedelta(minutes=10)))
        self.db.rows = {i:dict(deepcopy(forecast),id=i,status='PENDING') for i in range(1,33)}
        self.db.budget = self.context
        self.db.update_cost = .4
        with patch.object(self.app,'_quote',side_effect=AssertionError('expired row fetched quote')):
            first,cursor = self.app.outcomes(self.context,{})
            self.assertEqual(first['status'],'PROGRESS')
            self.assertGreater(first['excluded'],0)
            self.assertLess(first['excluded'],32)
            self.assertEqual(sum(row['status']=='EXCLUDED' for row in self.db.rows.values()),first['excluded'])
            for _ in range(8):
                self.context.remaining_seconds=6.
                _,cursor = self.app.outcomes(self.context,cursor)
                if all(row['status']=='EXCLUDED' for row in self.db.rows.values()):
                    break
        self.assertEqual(cursor['excluded'],32)
        self.assertTrue(all(row['status']=='EXCLUDED' for row in self.db.rows.values()))
        self.assertTrue(any('statement_timeout' in sql and args and int(args[0])<2000 for sql,args in self.db.calls))

    def test_outcome_no_budget_defers_and_commit_failure_preserves_queue(self):
        forecast,_ = C.forecast_from_ledger(FIXTURE.captured(at=NOW-timedelta(minutes=10)))
        self.db.rows={1:dict(forecast,id=1,status='PENDING')}
        before=deepcopy(self.db.rows)
        self.context.remaining_seconds=2.1
        result,_=self.app.outcomes(self.context,{})
        self.assertEqual(result['status'],'DEFERRED_OUTCOME_BUDGET')
        self.assertEqual(self.db.rows,before)
        self.context.remaining_seconds=6.
        self.db.fail_commit=True
        with self.assertRaises(OSError):
            self.app.outcomes(self.context,{})
        self.assertEqual(self.db.rows,before)

    def test_eight_original_stamps_each_get_their_own_phase_before_ack(self):
        raw=self.ready(stamps=[{'trial_id':str(i)} for i in range(8)])
        with patch.object(AUTO,'run_batch',return_value={'counts':{'direction':1},'profiles':[]}) as auto, \
             patch.object(C.KNOWLEDGE,'run_batch',return_value={}) as knowledge:
            result,cursor=self.step()
            self.assertEqual(result['stage'],'PINNED')
            auto.assert_not_called(); knowledge.assert_not_called()
            _,cursor=self.step(cursor)
            self.assertEqual(auto.call_args.args[1],[raw])
            for i in range(8):
                _,cursor=self.step(cursor)
                self.assertEqual(knowledge.call_args.kwargs['trial_window'],(i,i+1))
                self.assertEqual(knowledge.call_args.args[1],[raw])
                self.assertEqual(self.db.rows[1]['status'],'READY')
            result,cursor=self.step(cursor)
            self.assertEqual(result['stage'],'ACK')
            self.assertEqual(self.db.rows[1]['status'],'LEARNED')
            self.assertEqual(cursor['candidate_work'],{'phase':'retention'})
            _,cursor=self.step(cursor)
            self.assertNotIn('candidate_work',cursor)
            self.assertEqual(auto.call_count,1)
            self.assertEqual(knowledge.call_count,8)

    def test_failed_trial_preserves_original_cursor_and_retries_same_stamp(self):
        raw=self.ready(stamps=[{'trial_id':str(i)} for i in range(3)])
        with patch.object(AUTO,'run_batch',return_value={'counts':{},'profiles':[]}), \
             patch.object(C.KNOWLEDGE,'run_batch',return_value={}) as knowledge:
            _,cursor=self.step(); _,cursor=self.step(cursor); _,cursor=self.step(cursor)
            before=deepcopy(cursor)
            knowledge.side_effect=TimeoutError('trial transaction interrupted')
            with self.assertRaises(TimeoutError):
                self.step(cursor)
            self.assertEqual(cursor,before)
            self.assertEqual(self.db.rows[1]['status'],'READY')
            knowledge.side_effect=None
            _,cursor=self.step(cursor)
            self.assertEqual(knowledge.call_args.kwargs['trial_window'],(1,2))
            self.assertEqual(knowledge.call_args.args[1],[raw])
            self.assertEqual(cursor['candidate_work']['trial_index'],2)

    def test_missing_or_changed_evidence_fails_closed_before_consumption_and_ack(self):
        raw=self.ready()
        _,cursor=self.step()
        for changed in ('missing','unsealed','resealed'):
            self.db.rows={1:{'id':1,'entity_key':raw['episode_key'],'status':'READY','outcome':deepcopy(raw)}}
            if changed=='missing':
                del self.db.rows[1]
            else:
                payload=self.db.rows[1]['outcome']
                payload['source_identity']={'key':'different-source'}
                if changed=='resealed':
                    payload.pop('evidence_hash'); payload['evidence_hash']=BRIDGE.digest(payload)
            with self.subTest(changed=changed), patch.object(AUTO,'run_batch') as auto, self.assertRaises(RuntimeError):
                self.step(cursor)
            auto.assert_not_called()
            self.assertFalse(any(row['status']=='LEARNED' for row in self.db.rows.values()))

    def test_lost_ack_checkpoint_recovers_abstention_count_exactly_once(self):
        self.ready(direction='NO_TRADE')
        with patch.object(AUTO,'run_batch') as auto:
            _,cursor=self.step({'abstentions':7,'next_auto_maintenance_at':NOW.timestamp()+120})
            _,cursor=self.step(cursor)
            original=deepcopy(cursor)
            first,advanced=self.step(original)
            self.assertEqual(first['stage'],'ACK')
            self.assertEqual(advanced['abstentions'],8)
            replay,recovered=self.step(original)
            self.assertEqual(replay['stage'],'ACK')
            self.assertEqual(recovered,advanced)
            self.assertEqual(sum(sql.startswith('UPDATE') for sql,args in self.db.calls),1)
            auto.assert_not_called()

    def test_original_stamp_list_is_fully_validated_before_pin_or_consumption(self):
        for malformed in ({}, False, '', [{'trial_id':'first'}, {'oversized':'x'*2049}]):
            raw=self.ready()
            raw['knowledge_trials']=malformed
            raw.pop('evidence_hash'); raw['evidence_hash']=BRIDGE.digest(raw)
            self.db.rows[1]['outcome']=raw
            with self.subTest(stamps=str(malformed)[:40]), patch.object(AUTO,'run_batch') as auto, \
                 patch.object(C.KNOWLEDGE,'run_batch') as knowledge, self.assertRaises(ValueError):
                self.step()
            auto.assert_not_called(); knowledge.assert_not_called()
            self.assertEqual(self.db.rows[1]['status'],'READY')

    def test_empty_queue_keeps_bounded_aging_and_retention_cadence(self):
        with patch.object(AUTO,'run_batch',return_value={'counts':{},'profiles':[]}) as auto:
            result,cursor=self.step()
            self.assertEqual(result['stage'],'MAINTENANCE_DUE')
            auto.assert_not_called()
            _,cursor=self.step(cursor)
            self.assertEqual(auto.call_args.args[1],())
            _,cursor=self.step(cursor)
            self.assertNotIn('candidate_work',cursor)
            result,cursor=self.step(cursor)
            self.assertEqual(result['status'],'NO_WORK')
            self.assertEqual(auto.call_count,1)
            self.assertEqual(sum(sql.startswith('DELETE') for sql,args in self.db.calls),1)

    def test_continuous_abstention_backlog_cannot_postpone_due_auto_aging(self):
        raw=self.ready(direction='NO_TRADE')
        second=deepcopy(raw)
        second['episode_key']='second-forecast'
        second.pop('evidence_hash'); second['evidence_hash']=BRIDGE.digest(second)
        self.db.rows[2]=dict(self.db.rows[1],id=2,entity_key=second['episode_key'],outcome=second)
        with patch.object(AUTO,'run_batch',return_value={'counts':{},'profiles':[]}) as auto:
            _,cursor=self.step()
            _,cursor=self.step(cursor)
            result,cursor=self.step(cursor)
            self.assertEqual(result['stage'],'ACK')
            self.assertEqual(cursor['candidate_work'],{'phase':'maintenance'})
            self.assertEqual(self.db.rows[2]['status'],'READY')
            _,cursor=self.step(cursor)
            self.assertEqual(auto.call_args.args[1],())
            _,cursor=self.step(cursor)
            result,cursor=self.step(cursor)
            self.assertEqual(result['stage'],'PINNED')
            self.assertEqual(cursor['candidate_work']['id'],2)
            self.assertEqual(auto.call_count,1)

    def test_directional_backlog_retention_cadence_is_independent_of_auto_refresh(self):
        self.ready()
        with patch.object(AUTO,'run_batch',return_value={'counts':{},'profiles':[]}):
            _,cursor=self.step()
            _,cursor=self.step(cursor)
            self.assertGreater(cursor['next_auto_maintenance_at'],NOW.timestamp())
            result,cursor=self.step(cursor)
            self.assertEqual(result['stage'],'ACK')
            self.assertEqual(cursor['candidate_work'],{'phase':'retention'})
            result,cursor=self.step(cursor)
            self.assertEqual(result['stage'],'RETENTION')
            self.assertEqual(cursor['next_retention_at'],NOW.timestamp()+120)
            self.assertEqual(sum(sql.startswith('DELETE') for sql,args in self.db.calls),1)

    def test_fair_rapid_request_occurs_only_after_successful_fenced_checkpoint(self):
        with patch.object(STORE,'claim_job',return_value={'cursor':{}}), \
             patch.object(STORE,'checkpoint_job',return_value=True) as checkpoint:
            self.app._callback('learning_candidates',lambda context,cursor:({'status':'PROGRESS'},cursor))()
            self.assertEqual(self.app.lane.requests,['learning_candidates'])
            checkpoint.return_value=False
            with self.assertRaises(RuntimeError):
                self.app._callback('learning_candidates',lambda context,cursor:({'status':'PROGRESS'},cursor))()
            self.assertEqual(self.app.lane.requests,['learning_candidates'])


@unittest.skipUnless(os.getenv('VERITAS_QUALITY_TEST_DSN'),'isolated PostgreSQL test database not configured')
class BacklogSQLTests(unittest.TestCase):
    setUp = FIXTURE.ContinuousPipelineSQLTests.setUp
    connect = FIXTURE.ContinuousPipelineSQLTests.connect
    tearDown = FIXTURE.ContinuousPipelineSQLTests.tearDown
    restore_bridge = FIXTURE.ContinuousPipelineSQLTests.restore_bridge

    def insert_ready(self, direction='LONG'):
        raw=outcome(direction)
        with self.connect() as c:
            row=c.execute("""INSERT INTO learning_forecasts(entity_key,decision_at,due_at,expires_at,asset,horizon,
                 source_key,evidence,status,outcome) VALUES(%s,%s,%s,%s,'BTC','1m','source','{}','READY',%s::jsonb) RETURNING id""",
                 (raw['episode_key'],NOW-timedelta(seconds=61),NOW-timedelta(seconds=1),NOW+timedelta(seconds=89),json.dumps(raw))).fetchone()
        return row['id'],raw

    def step(self,cursor=None):
        self.app.lane.reset()
        return self.app.candidates(self.context,cursor or {})

    def test_real_sql_direction_auto_replay_is_idempotent_before_ack(self):
        row_id,raw=self.insert_ready()
        _,cursor=self.step()
        original=deepcopy(cursor)
        first,advanced=self.step(cursor)
        self.assertEqual(first['counts']['direction'],1)
        replay,recovered=self.step(original)
        self.assertEqual(replay['counts']['direction'],1)
        self.assertEqual(recovered['candidate_work'],advanced['candidate_work'])
        _,done=self.step(recovered)
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT status FROM learning_forecasts WHERE id=%s',(row_id,)).fetchone()['status'],'LEARNED')
            self.assertEqual(c.execute('SELECT count(*) n FROM autonomous_learning_seen').fetchone()['n'],1)
        self.assertEqual(done['completed_forecasts'],1)

    def test_real_sql_ack_lost_checkpoint_preserves_abstention_and_rejects_changed_seal(self):
        row_id,raw=self.insert_ready('NO_TRADE')
        _,cursor=self.step({'abstentions':4})
        _,cursor=self.step(cursor)
        original=deepcopy(cursor)
        _,advanced=self.step(cursor)
        _,replayed=self.step(original)
        self.assertEqual(replayed,advanced)
        self.assertEqual(replayed['abstentions'],5)
        with self.connect() as c:
            c.execute("UPDATE learning_forecasts SET outcome=jsonb_set(outcome,'{forward_return}','0.9') WHERE id=%s",(row_id,))
        with self.assertRaisesRegex(RuntimeError,'SEAL_MISMATCH'):
            self.step(original)

    def test_real_sql_outcome_prefix_commits_before_budget_and_eventually_excludes_all(self):
        forecast,_=C.forecast_from_ledger(FIXTURE.captured(at=NOW-timedelta(minutes=10)))
        with self.connect() as c:
            for i in range(32):
                c.execute("""INSERT INTO learning_forecasts(entity_key,decision_at,due_at,expires_at,asset,horizon,source_key,evidence)
                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",('expired-'+str(i),forecast['decision_at'],forecast['due_at'],forecast['expires_at'],
                    forecast['asset'],forecast['horizon'],forecast['source_key'],json.dumps(forecast['evidence'])))
        budget=Budget()
        real_connect=self.app.connect
        @contextmanager
        def measured_connect():
            with real_connect() as connection:
                class Measured:
                    transaction=connection.transaction
                    def execute(self,sql,args=None):
                        result=connection.execute(sql,args) if args is not None else connection.execute(sql)
                        if sql.startswith('UPDATE learning_forecasts'):
                            budget.remaining_seconds-=.4
                        return result
                yield Measured()
        with patch.object(self.app,'connect',measured_connect), patch.object(C,'clock',return_value=NOW):
            first,cursor=self.app.outcomes(budget,{})
            self.assertEqual(first['status'],'PROGRESS')
            self.assertTrue(0<first['excluded']<32)
            for _ in range(8):
                budget.remaining_seconds=6.
                _,cursor=self.app.outcomes(budget,cursor)
                if cursor['excluded']==32:
                    break
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) n FROM learning_forecasts WHERE status='EXCLUDED'").fetchone()['n'],32)
        self.assertEqual(cursor['excluded'],32)


if __name__=='__main__':
    unittest.main()
