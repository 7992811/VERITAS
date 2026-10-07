"""Expired checkout and committed-learning recovery; synthetic isolated data only."""
import ast
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import inspect
import os
import textwrap
import unittest
from unittest.mock import Mock, patch

import veritas_continuous_learning as C
import veritas_autonomous_learning as AUTO
from veritas_maintenance import MaintenanceDeferred
import test_veritas_continuous_learning as PIPELINE
from test_veritas_continuous_learning import NOW, fresh_quote, namespace
from test_veritas_autonomous_learning import observation


class Deadline:
    def __init__(self):
        self.expired = False
    @property
    def sql_timeout_ms(self):
        return 1 if self.expired else 2000
    def check(self):
        if self.expired:
            raise MaintenanceDeferred("DEFERRED_TIME_BUDGET", reason="synthetic_deadline")


class Checkout:
    def __init__(self, budget, phase):
        self.budget, self.phase = budget, phase
        self.calls, self.begins, self.closed, self.active = [], 0, False, False
    def __enter__(self):
        self.budget.expired = self.phase == "checkout"
        return self
    def __exit__(self, *args):
        self.closed = True
    @contextmanager
    def transaction(self):
        self.begins += 1
        self.active = True
        if self.phase == "begin":
            self.budget.expired = True
        try:
            yield
        finally:
            self.active = False
    def execute(self, sql, args=None):
        self.calls.append((sql, args))
        if self.phase == "settings" and "lock_timeout" in sql:
            self.budget.expired = True


class ForecastBudgetTests(unittest.TestCase):
    def test_expired_checkout_or_begin_never_issues_one_millisecond_sql(self):
        for phase in ("checkout", "begin", "settings"):
            with self.subTest(phase=phase):
                budget = Deadline()
                connection = Checkout(budget, phase)
                with self.assertRaises(MaintenanceDeferred):
                    with C.transaction(lambda: connection, budget) as c:
                        c.execute("DELETE must_not_run")
                self.assertTrue(connection.closed)
                self.assertFalse(connection.active)
                self.assertEqual(connection.begins, 0 if phase == "checkout" else 1)
                self.assertEqual(len(connection.calls), 2 if phase == "settings" else 0)
                self.assertFalse(any(args == ("1",) for _, args in connection.calls))

    def test_both_learners_must_commit_before_ack_can_be_retained(self):
        app = C.ContinuousLearning(namespace(lambda: None))
        rows = [{"id": 9, "outcome": {"direction": "LONG", "knowledge_trials": ["synthetic"]}}]
        connection = Mock()
        connection.execute.return_value.fetchall.return_value = rows
        @contextmanager
        def transaction(*unused):
            yield connection
        with patch.object(C, "transaction", transaction), \
             patch.object(AUTO, "run_batch", return_value={"counts": {"direction": 1}, "profiles": []}), \
             patch.object(C.KNOWLEDGE, "run_batch", side_effect=RuntimeError("synthetic rollback")):
            with self.assertRaisesRegex(RuntimeError, "synthetic rollback"):
                app.candidates(Deadline(), {})
        self.assertIsNone(app._pending_candidate_ack)
        self.assertEqual(connection.execute.call_count, 1)

    def test_next_turn_acknowledges_completed_batch_without_repeating_exhausting_work(self):
        app = C.ContinuousLearning(namespace(lambda: None))
        original = deepcopy(app._snapshot)
        rows = [{"id": 9, "outcome": {"direction": "LONG", "knowledge_trials": ["synthetic"]}}]
        connection = Mock()
        connection.execute.return_value.fetchall.return_value = rows
        @contextmanager
        def transaction(*unused):
            yield connection
        budget = Deadline()
        completed = {"counts": {"direction": 1}, "profiles": []}
        def knowledge(*args, **kwargs):
            budget.expired = True  # Both learner commits used this entire turn.
        cursor = {"abstentions": 4}
        with patch.object(C, "transaction", transaction), \
             patch.object(AUTO, "run_batch", return_value=completed) as learn, \
             patch.object(C.KNOWLEDGE, "run_batch", side_effect=knowledge) as validate, \
             patch.object(C.STORE, "load_snapshot_in_transaction", return_value=None), \
             patch.object(C.BRIDGE, "update") as publish:
            with self.assertRaises(MaintenanceDeferred):
                app.candidates(budget, cursor)
            self.assertEqual(app._snapshot, original)
            self.assertEqual(app._pending_candidate_ack["ids"], (9,))
            publish.assert_not_called()
            completed["counts"]["direction"] = 999
            result, advanced = app.candidates(Deadline(), cursor)
            self.assertEqual(result["counts"], {"direction": 1})
            self.assertEqual(advanced, cursor)
            self.assertEqual(learn.call_count, 1)
            self.assertEqual(validate.call_count, 1)
            publish.assert_called_once()
        self.assertIsNone(app._pending_candidate_ack)
        self.assertEqual(connection.execute.call_count, 3)  # One read, acknowledgement, cleanup.

    def test_delayed_ack_does_not_republish_a_model_older_than_another_job(self):
        app = C.ContinuousLearning(namespace(lambda: None))
        app._pending_candidate_ack = {"ids": (9,), "snapshot": {
            "updated_at": "2040-01-01T00:00:00+00:00", "counts": {"direction": 1}, "profiles": []},
            "observations": 1, "abstentions": 0}
        newer = {"updated_at": "2040-01-01T00:01:00+00:00", "counts": {"direction": 2}, "profiles": []}
        app._snapshot = deepcopy(newer)
        @contextmanager
        def transaction(*unused):
            yield Mock()
        with patch.object(C, "transaction", transaction), patch.object(C.BRIDGE, "update") as publish, \
             patch.object(C.STORE, "load_snapshot_in_transaction", return_value=None), \
             patch.object(AUTO, "run_batch", side_effect=AssertionError("learner repeated")):
            result, _ = app.candidates(Deadline(), {})
        self.assertEqual(result["counts"], newer["counts"])
        self.assertEqual(app._snapshot, newer)
        self.assertIsNone(app._pending_candidate_ack)
        publish.assert_not_called()  # Leave the other job's already-published bridge intact.

    def test_budget_deferral_checkpoints_retry_without_advancing_cursor(self):
        app = C.ContinuousLearning(namespace(lambda: None))
        app.ready = True
        error = MaintenanceDeferred("DEFERRED_TIME_BUDGET", reason="synthetic_deadline")
        with patch.object(C.STORE, "claim_job", return_value={"cursor": {"abstentions": 4}}), \
             patch.object(C.STORE, "checkpoint_job", return_value=True) as checkpoint:
            with self.assertRaises(MaintenanceDeferred):
                app._callback("learning_candidates", Mock(side_effect=error))()
        self.assertEqual(checkpoint.call_args.kwargs["status"], "RETRY")
        self.assertNotIn("cursor", checkpoint.call_args.kwargs)
        self.assertIsNone(app._stats["last_error"])


def cleanup_sql():
    tree = ast.parse(textwrap.dedent(inspect.getsource(C.ContinuousLearning.candidates)))
    return next(node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                and node.value.startswith("DELETE FROM learning_forecasts"))


@unittest.skipUnless(os.getenv("VERITAS_QUALITY_TEST_DSN"), "isolated PostgreSQL test database not configured")
class ForecastCleanupSQLTests(unittest.TestCase):
    setUp = PIPELINE.ContinuousPipelineSQLTests.setUp
    tearDown = PIPELINE.ContinuousPipelineSQLTests.tearDown
    connect = PIPELINE.ContinuousPipelineSQLTests.connect
    restore_bridge = PIPELINE.ContinuousPipelineSQLTests.restore_bridge
    call = PIPELINE.ContinuousPipelineSQLTests.call
    insert_decision = PIPELINE.ContinuousPipelineSQLTests.insert_decision

    def ready_forecast(self):
        _, cursor = self.call(self.app.ingest)
        self.insert_decision()
        self.call(self.app.ingest, cursor)
        with patch.object(C, "clock", return_value=NOW), \
             patch.object(self.app, "_quote", return_value=fresh_quote()):
            result, _ = self.call(self.app.outcomes)
        self.assertEqual(result["resolved"], 1)

    def forecasts(self):
        with self.connect() as c:
            return c.execute("SELECT entity_key,status,learned_at,evidence,outcome FROM learning_forecasts ORDER BY entity_key").fetchall()

    def old_rows(self, c, n=80):
        c.execute("""INSERT INTO learning_forecasts
            (entity_key,decision_at,due_at,expires_at,asset,horizon,source_key,evidence,status,learned_at)
            SELECT 'expired-'||n,now(),now(),now(),'SYNTHETIC','1m','synthetic-source',
                   jsonb_build_object('synthetic_proof',n),
                   CASE WHEN mod(n,2)=0 THEN 'LEARNED' ELSE 'EXCLUDED' END,
                   now()-interval '30 days'-n*interval '1 day'
            FROM generate_series(1,%s) n""", (n,))

    def expire_ack_checkout(self, budget):
        original = self.app.connect
        @contextmanager
        def delayed_connect():
            with original() as c:
                if self.app._pending_candidate_ack is not None:
                    budget.expired = True
                yield c
        self.app.connect = delayed_connect
        return original

    def test_retention_is_exactly_oldest_64_and_never_changes_retained_evidence(self):
        with self.connect() as c, c.transaction():
            self.old_rows(c)
            c.execute("""INSERT INTO learning_forecasts
                (entity_key,decision_at,due_at,expires_at,asset,horizon,source_key,evidence,status,learned_at)
                SELECT 'retained-'||n,now(),now(),now(),'SYNTHETIC','1m','synthetic-source',
                    jsonb_build_object('synthetic_proof',n),
                    CASE WHEN n%3=0 THEN 'PENDING' WHEN n%3=1 THEN 'READY' ELSE 'LEARNED' END,
                    CASE WHEN n%3=2 THEN now()-interval '30 days' ELSE NULL END
                FROM generate_series(1,10000) n""")
            before = c.execute("SELECT id,status,learned_at,evidence FROM learning_forecasts ORDER BY id").fetchall()
            expected = c.execute("SELECT id FROM learning_forecasts WHERE learned_at<now()-interval '30 days' ORDER BY learned_at LIMIT 64").fetchall()
            expected_ids = {r["id"] for r in expected}
            c.execute("ANALYZE learning_forecasts")
            plan = c.execute("EXPLAIN (ANALYZE, FORMAT JSON, TIMING FALSE) "+cleanup_sql()).fetchone()["QUERY PLAN"][0]
            after = c.execute("SELECT id,status,learned_at,evidence FROM learning_forecasts ORDER BY id").fetchall()
            self.assertEqual(after, [r for r in before if r["id"] not in expected_ids])
            self.assertEqual(len(before)-len(after), 64)
            def nodes(node):
                yield node
                for child in node.get("Plans", []):
                    yield from nodes(child)
            self.assertTrue(any(n.get("Index Name") == "learning_forecasts_learned" for n in nodes(plan["Plan"])))

    def test_expired_ack_retry_skips_learners_and_keeps_completed_snapshot_detached(self):
        self.ready_forecast()
        before = self.forecasts()
        original_snapshot = deepcopy(self.app._snapshot)
        budget = Deadline()
        original_connect = self.expire_ack_checkout(budget)
        returned = []
        actual_run = AUTO.run_batch
        def learn(*args, **kwargs):
            result = actual_run(*args, **kwargs)
            returned.append(result)
            return result
        cursor = {"abstentions": 7}
        with patch.object(AUTO, "run_batch", side_effect=learn):
            with self.assertRaises(MaintenanceDeferred):
                self.app.candidates(budget, cursor)
        self.assertEqual(self.forecasts(), before)
        self.assertEqual(self.app._snapshot, original_snapshot)
        self.assertEqual(cursor, {"abstentions": 7})
        self.assertLessEqual(len(self.app._pending_candidate_ack["ids"]), C.BATCH)
        self.assertEqual(AUTO.snapshot(self.connect)["counts"]["direction"], 1)
        returned[0]["counts"]["direction"] = 999
        self.assertEqual(self.app._pending_candidate_ack["snapshot"]["counts"]["direction"], 1)
        self.app.connect = original_connect
        with patch.object(AUTO, "run_batch", side_effect=AssertionError("learner repeated")), \
             patch.object(C.KNOWLEDGE, "run_batch", side_effect=AssertionError("knowledge repeated")):
            result, advanced = self.app.candidates(Deadline(), cursor)
        self.assertEqual(result["counts"]["direction"], 1)
        self.assertEqual(advanced["abstentions"], 7)
        self.assertIsNone(self.app._pending_candidate_ack)
        self.assertEqual(self.forecasts()[0]["status"], "LEARNED")

    def test_failed_cleanup_rolls_back_ack_and_preserves_pending_for_retry(self):
        self.ready_forecast()
        with self.connect() as c:
            self.old_rows(c, 1)
            c.execute("""CREATE FUNCTION refuse_cleanup() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'synthetic cleanup failure'; END $$""")
            c.execute("CREATE TRIGGER refuse_cleanup BEFORE DELETE ON learning_forecasts FOR EACH ROW EXECUTE FUNCTION refuse_cleanup()")
        before = self.forecasts()
        with self.assertRaisesRegex(Exception, "synthetic cleanup failure"):
            self.app.candidates(Deadline(), {})
        self.assertEqual(self.forecasts(), before)
        self.assertIsNotNone(self.app._pending_candidate_ack)
        self.assertEqual(AUTO.snapshot(self.connect)["counts"]["direction"], 1)
        with self.connect() as c:
            c.execute("DROP TRIGGER refuse_cleanup ON learning_forecasts")
        with patch.object(AUTO, "run_batch", side_effect=AssertionError("learner repeated")):
            self.app.candidates(Deadline(), {})
        self.assertEqual(len(self.forecasts()), 1)
        self.assertEqual(self.forecasts()[0]["status"], "LEARNED")

    def test_process_restart_replays_durable_dedup_before_ack_without_inflation(self):
        self.ready_forecast()
        budget = Deadline()
        self.expire_ack_checkout(budget)
        with self.assertRaises(MaintenanceDeferred):
            self.app.candidates(budget, {})
        recreated = C.ContinuousLearning(namespace(self.connect))
        self.assertIsNone(recreated._pending_candidate_ack)
        result, _ = recreated.candidates(Deadline(), {})
        self.assertEqual(result["counts"]["direction"], 1)
        self.assertEqual(AUTO.snapshot(self.connect)["counts"]["direction"], 1)
        self.assertEqual(self.forecasts()[0]["status"], "LEARNED")
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) AS n FROM autonomous_learning_seen").fetchone()["n"], 1)

    def test_external_committed_model_between_ack_attempts_is_read_without_relearning(self):
        self.ready_forecast()
        budget = Deadline()
        original_connect = self.expire_ack_checkout(budget)
        with self.assertRaises(MaintenanceDeferred):
            self.app.candidates(budget, {})
        earlier = C.BRIDGE.timestamp(self.app._pending_candidate_ack["snapshot"]["updated_at"])
        newer = AUTO.run_batch(self.connect, [observation(99)], now=earlier+timedelta(seconds=1))
        self.assertEqual(newer["counts"]["direction"], 2)
        # Simulate a different process: this instance's snapshot/bridge has not
        # received the newer model. The retry must consult its committed row.
        self.assertNotEqual(self.app._snapshot.get("updated_at"), newer["updated_at"])
        self.app.connect = original_connect
        with patch.object(AUTO, "run_batch", side_effect=AssertionError("learner repeated")):
            result, _ = self.app.candidates(Deadline(), {})
        self.assertEqual(result["counts"]["direction"], 2)
        self.assertEqual(self.app._snapshot, newer)
        self.assertEqual(C.BRIDGE._state["updated_at"], newer["updated_at"])
        self.assertEqual(AUTO.snapshot(self.connect)["counts"]["direction"], 2)
        self.assertEqual(self.forecasts()[0]["status"], "LEARNED")


if __name__ == "__main__":
    unittest.main()
