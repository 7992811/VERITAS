"""Recovery, audit atomicity and actual admission boundaries for learning jobs."""
from contextlib import contextmanager
from copy import deepcopy
import os
import threading
import types
import unittest
import uuid
from unittest.mock import Mock, patch

import veritas_evidence_learning as E
from veritas_maintenance import MaintenanceDeferred, MaintenanceLane


class MemoryLedger:
    def __init__(self):
        self.state, self.events, self.lock = None, {}, threading.Lock()
        self.fail_result = False

    @contextmanager
    def claim(self):
        held = self.lock.acquire(blocking=False)
        try:
            yield self if held else None
        finally:
            if held:
                self.lock.release()

    def load(self, c):
        return deepcopy(self.state)

    def save(self, c, state, event=None):
        if self.fail_result and state['status'] != 'RUNNING':
            raise OSError('audit unavailable')
        self.state = deepcopy(state)
        if event:
            self.events.setdefault((state['run_id'], event), deepcopy(state))


def namespace():
    ns = dict(VERSION='test-release', VR=types.SimpleNamespace(deployment_sha=lambda: 'test-sha'),
              HEAVY_LEARNING_INTERVAL_SECONDS=3600, HEAVY_LEARNING_START_DELAY_SECONDS=300,
              V90_HEAVY_LEARNING_MAX_START_MB=260, MEMORY_SOFT_LIMIT_MB=280,
              V90_MEMORY_CAUTION_MB=280, emit=Mock(), rss_mb=lambda: 240,
              _v90_trim_memory=Mock(), EVENT_LEARNING_ENABLED=False,
              learning_progress=Mock(), pg_connect=Mock())
    ns['learning_progress']._cache = None
    ns['_v90_background_maintenance'] = MaintenanceLane(ns)
    return ns


class DurableLearningTests(unittest.TestCase):
    def setUp(self):
        self.ns, self.ledger, self.now = namespace(), MemoryLedger(), 1000.
        self.loop = E.LearningLoop(self.ns, self.ledger, lambda: self.now)
        self.calls = []
        self.loop._stage = lambda name: self.calls.append(name) or {'status': 'OK'}

    def due(self):
        self.loop.tick()
        self.now += 300

    def test_restart_keeps_due_time_and_does_not_repeat_completed_stage(self):
        self.due()
        self.loop.tick()
        run_id = self.ledger.state['run_id']
        restarted = E.LearningLoop(self.ns, self.ledger, lambda: self.now)
        restarted._stage = self.loop._stage
        for _ in E.STAGES[1:]:
            restarted.tick()
        self.assertEqual(self.calls, list(E.STAGES))
        self.assertEqual(self.ledger.state['run_id'], run_id)
        self.assertEqual(self.ledger.state['status'], 'OK')
        self.assertEqual(self.ledger.state['runs'], 1)
        self.assertFalse(restarted.tick())
        self.now += 3600
        restarted.tick()
        self.assertNotEqual(self.ledger.state['run_id'], run_id)
        self.assertEqual(self.calls[-1], E.STAGES[0])

    def test_restarts_during_startup_delay_do_not_postpone_learning(self):
        self.loop.tick()
        due = self.ledger.state['next_due_at']
        self.now += 250
        restarted = E.LearningLoop(self.ns, self.ledger, lambda: self.now)
        restarted._stage = self.loop._stage
        restarted.tick()
        self.assertEqual(self.ledger.state['next_due_at'], due)
        self.now += 50
        restarted.tick()
        self.assertEqual(self.calls, [E.STAGES[0]])

    def test_interrupted_uncheckpointed_stage_is_replayed_and_disclosed(self):
        self.due()
        self.ledger.fail_result = True
        self.loop.tick()
        self.assertEqual(self.ledger.state['status'], 'RUNNING')
        self.assertEqual(self.loop.snapshot()['status'], 'DEFERRED_STORAGE')
        self.ledger.fail_result = False
        self.loop.tick()
        self.assertEqual(self.calls, [E.STAGES[0]]*2)
        self.assertEqual(self.ledger.state['interrupted_stage_replays'], 1)
        self.assertEqual(self.ledger.state['next_stage'], E.STAGES[1])

    def test_completed_partial_work_cannot_be_reported_as_success(self):
        self.due()
        self.loop._stage = lambda name: {'status': 'DEGRADED'} if name == E.STAGES[0] else {'status': 'OK'}
        for _ in range(3):
            self.loop.tick()
            self.now += 60
        for _ in E.STAGES[1:]:
            self.loop.tick()
        self.assertEqual(self.ledger.state['status'], 'DEGRADED')
        self.assertEqual(self.ledger.state['attempts'][E.STAGES[0]], 3)

    def test_actual_shared_memory_and_busy_permits_prevent_stage_execution(self):
        self.due()
        self.ns['rss_mb'] = lambda: 290
        self.loop.tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.ledger.state['attempts'], {})
        self.assertEqual(self.ledger.state['status'], 'DEFERRED_MEMORY')
        self.ns['rss_mb'] = lambda: 240
        entered, release = threading.Event(), threading.Event()
        def competing_reader():
            with self.ns['_v90_background_maintenance'].permit('history'):
                entered.set()
                release.wait(3)
        t = threading.Thread(target=competing_reader)
        t.start()
        try:
            self.assertTrue(entered.wait(2))
            self.loop.tick()
            self.assertEqual(self.calls, [])
            self.assertEqual(self.ledger.state['status'], 'DEFERRED_BUSY')
        finally:
            release.set()
            t.join()
        self.loop.tick()
        self.assertEqual(self.calls, [E.STAGES[0]])

    def test_concurrent_instances_cannot_execute_one_stage_together(self):
        self.due()
        entered, release = threading.Event(), threading.Event()
        def stage(name):
            entered.set()
            release.wait(3)
            return {'status': 'OK'}
        self.loop._stage = stage
        second = E.LearningLoop(self.ns, self.ledger, lambda: self.now)
        second._stage = Mock(return_value={'status': 'OK'})
        t = threading.Thread(target=self.loop.tick)
        t.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertFalse(second.tick())
            second._stage.assert_not_called()
        finally:
            release.set()
            t.join()

    def test_version_change_records_old_run_and_never_mixes_stages(self):
        self.due()
        self.loop.tick()
        old_id = self.ledger.state['run_id']
        self.ns['VERSION'] = 'next-release'
        self.loop.tick()
        self.assertEqual(self.ledger.events[(old_id, 'superseded')]['status'], 'SUPERSEDED')
        self.assertNotEqual(self.ledger.state['run_id'], old_id)
        self.assertEqual(self.ledger.state['stages'], {})

    def test_exceptions_do_not_publish_credentials_or_advance_as_success(self):
        self.due()
        self.loop._stage = Mock(side_effect=RuntimeError('postgres://private:password@host'))
        self.loop.tick()
        state = self.loop.snapshot()
        self.assertNotIn('password', repr(state))
        self.assertEqual(state['status'], 'RETRY')
        self.assertEqual(state['stages'][E.STAGES[0]]['error_code'], 'RuntimeError')

    def test_nested_paper_failure_and_async_stale_quality_are_not_success(self):
        result = E.compact({'status': 'OK', 'paper_execution_learning': {'status': 'DEGRADED'}})
        self.assertEqual(result['status'], 'DEGRADED')
        result = E.compact({'status': 'MEASURABLE', 'background_refresh': True})
        self.assertNotIn(result['status'], E.SUCCESS)

    def test_adapters_call_canonical_callbacks_and_keep_only_bounded_results(self):
        loop = E.LearningLoop(self.ns, self.ledger)
        for name in ('_v90_backfill_decision_episodes', 'refresh_rule_stats',
                     'refresh_experience_lessons', 'setup_memory_board'):
            self.ns[name] = Mock(return_value={'status': 'OK', 'items': ['large proof']*200})
        for stage in (E.STAGES[0], E.STAGES[2], E.STAGES[3], E.STAGES[4]):
            result = loop._stage(stage)
            self.assertEqual(result, {'status': 'OK', 'items_count': 200})
        self.ns['setup_memory_board'].assert_called_once_with(force=True)
        self.assertEqual(loop._stage('event_outcomes'), {'status': 'DISABLED'})
        self.assertFalse(loop.snapshot()['model_weights_trained'])

    def test_quality_snapshot_exposes_scope_without_invented_improvement(self):
        result = E.quality_snapshot({'status': 'MEASURABLE', 'index_vs_start': 120,
                                     'huge_raw_history': ['do not persist']*10000})
        self.assertFalse(result['causal_improvement_established'])
        self.assertNotIn('huge_raw_history', result)

    def test_install_replaces_timer_requests_and_status_with_same_coordinator(self):
        one = E.install(self.ns)
        self.assertIs(E.install(self.ns), one)
        self.assertIs(self.ns['heavy_learning_maintenance_loop'].__self__, one)
        self.assertIs(self.ns['run_heavy_learning_maintenance'].__self__, one)
        self.assertIs(self.ns['heavy_learning_snapshot'].__self__, one)
        self.assertFalse(self.ns['maybe_schedule_heavy_learning'](force=True))
        self.assertTrue(one.wake.is_set())
        self.assertEqual(self.ns['heavy_learning_snapshot']()['status'], 'RESTORING')


@unittest.skipUnless(os.getenv('VERITAS_QUALITY_TEST_DSN'), 'isolated PostgreSQL required')
class LedgerSQLTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        self.psycopg, self.row = psycopg, dict_row
        self.dsn = os.environ['VERITAS_QUALITY_TEST_DSN']
        self.schema = 'evidence_learning_test_'+uuid.uuid4().hex
        with psycopg.connect(self.dsn) as c:
            if c.execute('SELECT current_database()').fetchone()[0] != 'veritas_quality_test':
                raise RuntimeError('isolated test database required')
            c.execute('CREATE SCHEMA '+self.schema)
            c.execute('CREATE TABLE '+self.schema+'''.ledger_events (
                event_key text PRIMARY KEY, entity_key text, event_type text,
                event_ts timestamptz, payload jsonb, model_version text)''')
        self.ledger = E.Ledger(self.connect, 'sql-test-release')

    def tearDown(self):
        with self.psycopg.connect(self.dsn) as c:
            c.execute('DROP SCHEMA '+self.schema+' CASCADE')

    @contextmanager
    def connect(self):
        with self.psycopg.connect(self.dsn, autocommit=True, row_factory=self.row) as c:
            c.execute('SET search_path TO '+self.schema)
            yield c

    def test_database_excludes_two_sessions_and_releases_after_failure(self):
        with self.assertRaisesRegex(RuntimeError, 'crash'):
            with self.ledger.claim() as first:
                self.assertIsNotNone(first)
                with self.ledger.claim() as second:
                    self.assertIsNone(second)
                raise RuntimeError('crash')
        with self.ledger.claim() as recovered:
            self.assertIsNotNone(recovered)

    def test_stage_checkpoints_are_atomic_with_immutable_audit_events(self):
        state = {'run_id': 'one', 'status': 'RUNNING', 'stages': {}}
        with self.ledger.claim() as c:
            self.ledger.save(c, state, 'start')
            self.ledger.save(c, dict(state, status='OK'), 'start')
            rows = c.execute("SELECT payload FROM ledger_events WHERE event_type='evidence_learning_audit'").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['payload']['status'], 'RUNNING')
            c.execute("ALTER TABLE ledger_events ADD CONSTRAINT reject_failure CHECK (entity_key <> 'bad')")
            with self.assertRaises(self.psycopg.errors.CheckViolation):
                self.ledger.save(c, dict(state, run_id='bad', status='ERROR'), 'bad')
            self.assertEqual(self.ledger.load(c)['status'], 'OK')
        with E.Ledger(self.connect, 'restart').claim() as c:
            self.assertEqual(self.ledger.load(c)['run_id'], 'one')


if __name__ == '__main__':
    unittest.main()
