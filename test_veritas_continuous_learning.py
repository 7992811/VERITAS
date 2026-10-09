"""Prospective producer contracts and isolated PostgreSQL pipeline recovery."""
from contextlib import contextmanager, nullcontext
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import json
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import veritas_autonomous_learning as AUTO
import veritas_continuous_learning as C
import veritas_learning_bridge as BRIDGE
import veritas_learning_state as STORE
import veritas_trade_learning as TRADE
from test_veritas_autonomous_learning import NOW, observation
from test_veritas_learning_bridge import receiver
import test_veritas_learning_state as state_tests


class Lane:
    def __init__(self):
        self.callbacks = {}
        self.options = {}
        self.requests = []
        self.deadline = time.monotonic()+6
    def register_periodic(self, name, callback, **options):
        self.callbacks[name] = callback
        self.options[name] = options
    def current_budget(self):
        return {"sql_timeout_ms": 2000}
    def check_budget(self):
        if time.monotonic() >= self.deadline:
            raise TimeoutError("test microjob exceeded six seconds")
    def reset(self):
        self.deadline = time.monotonic()+6
    def snapshot(self):
        return {"periodic": {}}
    def request(self, name):
        self.requests.append(name)
        return True


def namespace(connect):
    def learning_progress():
        pass
    return {"pg_connect": connect, "_v90_background_maintenance": Lane(),
            "emit": lambda *a, **k: None, "learning_progress": learning_progress,
            "_learning_progress_state": {}}


def captured(entity="decision-1", at=None):
    at = at or NOW-timedelta(seconds=61)
    row = receiver()
    row.update(horizon="1m", market_observed_at=at.isoformat())
    with patch.object(BRIDGE.ENTRY, "policy_hash", return_value="immutable-policy"), \
         patch.object(BRIDGE.ENTRY, "version_identity", return_value={"strategy_entry_sha": "sha"}):
        provenance = BRIDGE.capture_decision(row, at=at)
    return {"entity_key": entity, "asset": "BTC", "horizon": "1m", "provenance": provenance}


def fresh_quote(**changes):
    return dict({"asset": "BTC", "price": 101., "source_names": {"primary": "Coinbase"},
                 "source_gate_pass": True, "market_open": True, "observed_at": NOW.isoformat()}, **changes)


class ProducerContracts(unittest.TestCase):
    def setUp(self):
        self.saved = deepcopy(BRIDGE._state)
        BRIDGE.update({"profiles": [], "candidates": []})
        self.addCleanup(self.restore)
    def restore(self):
        with BRIDGE._lock:
            BRIDGE._state.clear(); BRIDGE._state.update(self.saved)

    def test_frozen_capture_resolves_to_accepted_direction_evidence(self):
        forecast, reason = C.forecast_from_ledger(captured())
        self.assertIsNone(reason)
        result = C.resolve_forecast(forecast, fresh_quote(), now=NOW)
        self.assertEqual(result["status"], "READY")
        self.assertAlmostEqual(result["forward_return"], .01)
        normalized, reason = AUTO.normalize_observation(result, NOW)
        self.assertIsNone(reason)
        self.assertEqual(normalized["direction_correct"], 1.)
        self.assertNotIn("net_r", result)
        self.assertFalse(result["profitability_proven"])

    def test_missing_or_relabelled_provenance_is_never_backfilled(self):
        self.assertIsNone(C.forecast_from_ledger({"entity_key": "legacy"})[0])
        for field, value in (("policy_hash", "later-policy"), ("base_probability", .99),
                             ("decision_at", NOW.isoformat()), ("asset", "ETH")):
            record = captured(); record["provenance"][field] = value
            self.assertIsNone(C.forecast_from_ledger(record)[0])

    def test_malformed_sealed_quote_does_not_stall_the_ledger_cursor(self):
        for value in ("scalar", [1, 2], 123):
            record = captured(); evidence = record["provenance"]
            evidence["quote"] = value
            evidence.pop("evidence_hash")
            evidence["evidence_hash"] = BRIDGE.digest(evidence)
            self.assertIsNone(C.forecast_from_ledger(record)[0])

    def test_clock_and_source_lock_prevent_substituting_late_or_other_price(self):
        forecast, _ = C.forecast_from_ledger(captured())
        self.assertIsNone(C.resolve_forecast(forecast, fresh_quote(), now=forecast["due_at"]-timedelta(seconds=1)))
        wrong = fresh_quote(source_names={"primary": "Binance"})
        self.assertIsNone(C.resolve_forecast(forecast, wrong, now=NOW))
        future = fresh_quote(observed_at=(NOW+timedelta(seconds=1)).isoformat())
        self.assertIsNone(C.resolve_forecast(forecast, future, now=NOW))
        late = forecast["expires_at"]+timedelta(seconds=1)
        result = C.resolve_forecast(forecast, wrong, now=late)
        self.assertEqual(result["status"], "EXCLUDED")
        self.assertNotIn("forward_return", result)

    def test_old_or_unknown_horizon_is_explicitly_excluded(self):
        record = captured()
        record["horizon"] = record["provenance"]["horizon"] = "unknown"
        record["provenance"].pop("evidence_hash")
        record["provenance"]["evidence_hash"] = BRIDGE.digest(record["provenance"])
        self.assertIsNone(C.forecast_from_ledger(record)[0])

    def test_bootstrap_failure_can_retry_without_skipping_the_failed_stage(self):
        app = C.ContinuousLearning(namespace(lambda: None))
        with patch.object(AUTO, "ensure_schema", side_effect=RuntimeError("database offline")):
            with self.assertRaises(RuntimeError):
                app.bootstrap()
        self.assertEqual(app.boot_phase, 0)
        self.assertFalse(app.ready)
        with patch.object(AUTO, "ensure_schema") as ensure:
            result = app.bootstrap()
        ensure.assert_called_once()
        self.assertEqual(app.boot_phase, 1)
        self.assertEqual(result["status"], "PROGRESS")

    def test_failed_operation_never_publishes_a_new_cursor(self):
        app = C.ContinuousLearning(namespace(lambda: None)); app.ready = True
        lease = {"cursor": {"last_id": 7}}
        def operation(context, cursor):
            cursor["last_id"] = 999
            raise RuntimeError("failed after a row")
        with patch.object(STORE, "claim_job", return_value=lease), \
             patch.object(STORE, "checkpoint_job", return_value=True) as checkpoint:
            with self.assertRaisesRegex(RuntimeError, "failed after a row"):
                app._callback("test", operation)()
        self.assertEqual(checkpoint.call_args.kwargs["status"], "ERROR")
        self.assertNotIn("cursor", checkpoint.call_args.kwargs)

    def test_phase_success_requires_checkpoint_acceptance(self):
        app = C.ContinuousLearning(namespace(lambda: None)); app.ready = True
        with patch.object(STORE, "claim_job", return_value={"cursor": {}}), \
             patch.object(STORE, "checkpoint_job", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "lease expired"):
                app._callback("test", lambda context, cursor: ({"status": "OK"}, {"last_id": 4}))()
        self.assertIsNone(app._stats["last_success_at"])

    def test_deferred_operation_preserves_cursor_and_last_good_result(self):
        app = C.ContinuousLearning(namespace(lambda: None)); app.ready = True
        with patch.object(STORE, "claim_job", return_value={"cursor": {"last_id": 7}}), \
             patch.object(STORE, "checkpoint_job", return_value=True) as checkpoint:
            result = app._callback("test", lambda context, cursor: (
                {"status": "RETRY", "reason": "SHARED_WORKER_BUSY"}, {"last_id": 999}))()
        self.assertEqual(result["status"], "RETRY")
        self.assertEqual(checkpoint.call_args.kwargs["status"], "RETRY")
        self.assertNotIn("cursor", checkpoint.call_args.kwargs)
        self.assertIsNone(app._stats["last_success_at"])
        self.assertIsNone(app._stats["last_error"])

    def test_scorecard_and_daily_progress_both_receive_background_turns(self):
        app = C.ContinuousLearning(namespace(lambda: None))
        context = C.Budget(app.lane)
        with patch.object(C.INTELLIGENCE, 'refresh_snapshot', return_value={'status': 'RETRY'}) as score, \
             patch.object(C.SCORECARD, 'refresh_daily', return_value={'status': 'OK'}, create=True) as daily:
            result, cursor = app.intelligence(context, {})
            self.assertEqual(result['status'], 'RETRY')
            self.assertEqual(cursor, {})
            daily.assert_not_called()
            self.assertIs(score.call_args.args[0], app.connect)
            self.assertIsNone(score.call_args.kwargs['cursor'])
            pending = {'cycle_id': 'frozen-sample', 'stage': 'decisions', 'offset': 64}
            score.return_value = {'status': 'PROGRESS', 'cursor': pending}
            _, cursor = app.intelligence(context, cursor)
            self.assertEqual(cursor['scorecard_work'], pending)
            daily.assert_not_called()
            completed = dict(pending, stage='complete', offset=2200, next_refresh_at=5000.)
            score.return_value = {'status': 'OK', 'cursor': completed}
            _, cursor = app.intelligence(context, cursor)
            self.assertEqual(score.call_args.kwargs['cursor'], pending)
            self.assertEqual(cursor['phase'], 'daily')
            self.assertEqual(cursor['scorecard_work'], completed)
            _, cursor = app.intelligence(context, cursor)
            self.assertEqual(cursor['phase'], 'scorecard')
            self.assertEqual(cursor['scorecard_work'], completed)
            self.assertEqual(daily.call_count, 1)
            self.assertIs(daily.call_args.args[0], app.ns)
            self.assertIs(daily.call_args.args[1], app.connect)
            self.assertEqual(score.call_count, 3)
            score.return_value = {'status': 'NO_WORK', 'reason': 'SCORECARD_REFRESH_NOT_DUE', 'cursor': completed}
            _, cursor = app.intelligence(context, cursor)
            self.assertEqual(cursor['phase'], 'scorecard')
            self.assertEqual(daily.call_count, 1)
            self.assertEqual(app.lane.options['learning_intelligence'],
                             {'interval_seconds': 90, 'lightweight': True, 'estimated_peak_mb': 16, 'max_seconds': 6})

    def test_failed_daily_audit_keeps_completed_scorecard_cursor_for_retry(self):
        app = C.ContinuousLearning(namespace(lambda: None))
        cursor = {'phase': 'daily', 'scorecard_work': {'stage': 'complete', 'next_refresh_at': 5000.}}
        original = deepcopy(cursor)
        with patch.object(C.INTELLIGENCE, 'refresh_snapshot') as score, \
             patch.object(C.SCORECARD, 'refresh_daily', return_value={'status': 'DEFERRED_LEARNING_UNAVAILABLE'}) as daily:
            result, resumed = app.intelligence(C.Budget(app.lane), cursor)
            self.assertEqual(result['status'], 'DEFERRED_LEARNING_UNAVAILABLE')
            self.assertEqual(resumed, original)
            score.assert_not_called()
            daily.side_effect = RuntimeError('commit failed')
            with self.assertRaisesRegex(RuntimeError, 'commit failed'):
                app.intelligence(C.Budget(app.lane), cursor)
            self.assertEqual(cursor, original)


class ScopedConnection:
    """Connection ownership only; native tests exercise the real transactions."""
    def __init__(self, *, autocommit=True, status="IDLE", close_error=None, suppress=False):
        self.autocommit = autocommit
        self.info = SimpleNamespace(transaction_status=SimpleNamespace(name=status))
        self.close_error, self.closed, self.exit_error = close_error, False, None
        self.suppress = suppress

    def __enter__(self):
        if self.closed:
            raise AssertionError("closed connection borrowed")
        return self

    def __exit__(self, kind, error, traceback):
        self.closed, self.exit_error = True, error
        if self.close_error is not None:
            raise self.close_error
        return self.suppress


class IntelligenceConnectionContracts(unittest.TestCase):
    def test_one_owned_connection_serves_scorecard_daily_and_returned_deferral(self):
        for phase, result in (("scorecard", {"status": "PROGRESS", "cursor": {"stage": "decisions", "offset": 64}}),
                              ("daily", {"status": "OK"}),
                              ("scorecard", {"status": "NO_WORK"}),
                              ("scorecard", {"status": "DEFERRED_SCORECARD_BUDGET"})):
            with self.subTest(phase=phase, status=result["status"]):
                raw, seen = ScopedConnection(), []
                original = Mock(return_value=raw)
                app = C.ContinuousLearning(namespace(original)); app.ready = True
                lease = {"cursor": {"phase": phase}}
                def borrowed(factory):
                    self.assertIs(app.connect, original)
                    self.assertIsNot(factory, original)
                    with factory() as c:
                        self.assertIs(c, raw)
                        self.assertFalse(c.closed)
                    self.assertFalse(raw.closed)
                    seen.append(factory)
                def claim(factory, *args, **kwargs):
                    borrowed(factory)
                    self.assertEqual(kwargs["lease_seconds"], 30)
                    return lease
                def scorecard(factory, *args, **kwargs):
                    borrowed(factory)
                    self.assertIs(kwargs["context"].lane, app.lane)
                    return result
                def daily(ns, factory, *args, **kwargs):
                    self.assertIs(ns, app.ns)
                    return scorecard(factory, *args, **kwargs)
                def checkpoint(factory, *args, **kwargs):
                    borrowed(factory)
                    self.assertEqual(kwargs["status"], "RETRY" if result["status"].startswith("DEFERRED") else "OK")
                    if kwargs["status"] == "RETRY":
                        self.assertNotIn("cursor", kwargs)
                    return True
                with patch.object(STORE, "claim_job", side_effect=claim), \
                     patch.object(STORE, "checkpoint_job", side_effect=checkpoint), \
                     patch.object(C.INTELLIGENCE, "refresh_snapshot", side_effect=scorecard) as score, \
                     patch.object(C.SCORECARD, "refresh_daily", side_effect=daily) as audit:
                    self.assertEqual(app.lane.callbacks["learning_intelligence"](), result)
                original.assert_called_once_with()
                self.assertEqual(len(seen), 3)
                self.assertTrue(all(factory is seen[0] for factory in seen))
                self.assertEqual(score.call_count, int(phase == "scorecard"))
                self.assertEqual(audit.call_count, int(phase == "daily"))
                self.assertTrue(raw.closed)
                self.assertIs(app.connect, original)
                self.assertIsNone(app._stats["last_error"])
                self.assertEqual(app._stats["last_success_at"] is None, result["status"].startswith("DEFERRED"))

    def test_not_ready_and_busy_paths_close_only_owned_connection_and_keep_state(self):
        for state in ("not_ready", "readiness_lost_at_checkout", "busy"):
            with self.subTest(state=state):
                raw = ScopedConnection()
                def connect():
                    if state == "readiness_lost_at_checkout":
                        app.ready = False
                    return raw
                original = Mock(side_effect=connect)
                app = C.ContinuousLearning(namespace(original)); app.ready = state != "not_ready"
                previous = deepcopy(app._stats)
                with patch.object(STORE, "claim_job", return_value=None) as claim, \
                     patch.object(STORE, "checkpoint_job") as checkpoint, \
                     patch.object(C.INTELLIGENCE, "refresh_snapshot") as work:
                    result = app.lane.callbacks["learning_intelligence"]()
                self.assertEqual(result["reason"], "DURABLE_JOB_LEASE_BUSY" if state == "busy" else "LEARNING_BOOTSTRAP_PENDING")
                self.assertEqual(original.call_count, int(state != "not_ready"))
                self.assertEqual(claim.call_count, int(state == "busy"))
                self.assertEqual(raw.closed, state != "not_ready")
                checkpoint.assert_not_called(); work.assert_not_called()
                self.assertEqual(app._stats, previous)

    def test_checkout_never_resets_deadline_or_accepts_an_outer_transaction(self):
        for mode in ("expired_checkout", "not_autocommit", "in_transaction"):
            with self.subTest(mode=mode):
                raw = ScopedConnection(autocommit=mode != "not_autocommit",
                                       status="INTRANS" if mode == "in_transaction" else "IDLE")
                now = [100.]
                def connect():
                    now[0] += 7. if mode == "expired_checkout" else 1.
                    return raw
                with patch.object(C.time, "monotonic", side_effect=lambda: now[0]):
                    original = Mock(side_effect=connect)
                    app = C.ContinuousLearning(namespace(original)); app.ready = True
                    deadline, previous = app.lane.deadline, deepcopy(app._stats)
                    with patch.object(STORE, "claim_job") as claim, \
                         patch.object(C.INTELLIGENCE, "refresh_snapshot") as work, \
                         self.assertRaises(TimeoutError if mode == "expired_checkout" else RuntimeError):
                        app.lane.callbacks["learning_intelligence"]()
                    self.assertEqual(app.lane.deadline, deadline)
                self.assertTrue(raw.closed)
                original.assert_called_once_with()
                claim.assert_not_called(); work.assert_not_called()
                self.assertEqual(app._stats, previous)

    def test_errors_use_fresh_recovery_and_close_cannot_replace_original_exception(self):
        for failure in ("work", "cooperative", "checkpoint_ack"):
            with self.subTest(failure=failure):
                error = (C.MaintenanceDeferred("DEFERRED_TIME_BUDGET", reason="synthetic_work")
                         if failure == "cooperative" else RuntimeError("synthetic "+failure))
                cleanup_error = type("CleanupFailure", (BaseException,), {})("synthetic close failure")
                primary = ScopedConnection(close_error=None if failure == "cooperative" else cleanup_error,
                                           suppress=failure == "cooperative")
                recovery = ScopedConnection()
                original = Mock(side_effect=[primary, recovery])
                app = C.ContinuousLearning(namespace(original)); app.ready = True
                lease = {"cursor": {"scorecard_work": {"stage": "decisions", "offset": 64}}}
                def work(factory, *args, **kwargs):
                    with factory() as c:
                        self.assertIs(c, primary)
                    if failure != "checkpoint_ack":
                        raise error
                    return {"status": "PROGRESS", "cursor": {"stage": "decisions", "offset": 128}}
                def checkpoint(factory, *args, **kwargs):
                    if kwargs["status"] == "OK":
                        self.assertIsNot(factory, original)
                        with factory() as c:
                            self.assertIs(c, primary)
                        raise error  # The work is never replayed after this ambiguity.
                    self.assertIs(factory, original)
                    self.assertNotIn("cursor", kwargs)
                    self.assertEqual(kwargs["status"], "RETRY" if failure == "cooperative" else "ERROR")
                    with factory() as c:
                        self.assertIs(c, recovery)
                        raise OSError("synthetic recovery failure")
                with patch.object(STORE, "claim_job", return_value=lease), \
                     patch.object(C.INTELLIGENCE, "refresh_snapshot", side_effect=work) as called, \
                     patch.object(STORE, "checkpoint_job", side_effect=checkpoint) as saved, \
                     self.assertRaises(type(error)) as caught:
                    app.lane.callbacks["learning_intelligence"]()
                self.assertIs(caught.exception, error)
                self.assertIs(primary.exit_error, error)
                self.assertTrue(primary.closed and recovery.closed)
                self.assertEqual(original.call_count, 2)
                called.assert_called_once()
                self.assertEqual(saved.call_count, 2 if failure == "checkpoint_ack" else 1)
                self.assertEqual(lease["cursor"]["scorecard_work"]["offset"], 64)
                self.assertIsNone(app._stats["last_success_at"])
                self.assertIs(app.connect, original)

    def test_close_failure_reports_error_without_replaying_committed_work(self):
        error = OSError("synthetic close after commit")
        raw = ScopedConnection(close_error=error)
        original = Mock(return_value=raw)
        app = C.ContinuousLearning(namespace(original)); app.ready = True
        def checkpoint(factory, *args, **kwargs):
            with factory() as c:
                self.assertIs(c, raw)
            return True
        with patch.object(STORE, "claim_job", return_value={"cursor": {}}), \
             patch.object(C.INTELLIGENCE, "refresh_snapshot", return_value={"status": "PROGRESS"}) as work, \
             patch.object(STORE, "checkpoint_job", side_effect=checkpoint) as saved, \
             self.assertRaises(OSError) as caught:
            app.lane.callbacks["learning_intelligence"]()
        self.assertIs(caught.exception, error)
        self.assertTrue(raw.closed)
        original.assert_called_once_with(); work.assert_called_once(); saved.assert_called_once()
        self.assertEqual(saved.call_args.kwargs["status"], "OK")
        self.assertIsNotNone(app._stats["last_success_at"])
        self.assertEqual(app._stats["last_error"], "OSError: synthetic close after commit")

    def test_lost_lease_never_marks_the_step_successful(self):
        primary, recovery = ScopedConnection(), ScopedConnection()
        original = Mock(side_effect=[primary, recovery])
        app = C.ContinuousLearning(namespace(original)); app.ready = True
        def checkpoint(factory, *args, **kwargs):
            with factory() as c:
                self.assertIs(c, primary if kwargs["status"] == "OK" else recovery)
            return False
        with patch.object(STORE, "claim_job", return_value={"cursor": {}}), \
             patch.object(C.INTELLIGENCE, "refresh_snapshot", return_value={"status": "PROGRESS"}) as work, \
             patch.object(STORE, "checkpoint_job", side_effect=checkpoint) as saved, \
             self.assertRaisesRegex(RuntimeError, "lease expired"):
            app.lane.callbacks["learning_intelligence"]()
        work.assert_called_once()
        self.assertEqual([call.kwargs["status"] for call in saved.call_args_list], ["OK", "ERROR"])
        self.assertIs(saved.call_args.args[0], original)
        self.assertIsNone(app._stats["last_success_at"])
        self.assertTrue(primary.closed and recovery.closed)

    def test_concurrent_calls_keep_connections_and_budgets_local(self):
        barrier, lock = threading.Barrier(2), threading.Lock()
        local, connections, seen, contexts = threading.local(), [], [], []
        def connect():
            raw = ScopedConnection()
            local.raw = raw
            with lock:
                connections.append(raw)
            return raw
        original = Mock(side_effect=connect)
        app = C.ContinuousLearning(namespace(original)); app.ready = True
        def borrowed(factory):
            self.assertIs(app.connect, original)
            with factory() as raw:
                self.assertIs(raw, local.raw)
                self.assertFalse(raw.closed)
            with lock:
                seen.append((raw, factory))
        def claim(factory, *args, **kwargs):
            borrowed(factory)
            return {"cursor": {}}
        def work(factory, *args, **kwargs):
            borrowed(factory)
            with lock:
                contexts.append(kwargs["context"])
            barrier.wait(timeout=3)
            self.assertFalse(local.raw.closed)
            return {"status": "PROGRESS"}
        def checkpoint(factory, *args, **kwargs):
            borrowed(factory)
            return True
        with patch.object(STORE, "claim_job", side_effect=claim), \
             patch.object(C.INTELLIGENCE, "refresh_snapshot", side_effect=work), \
             patch.object(STORE, "checkpoint_job", side_effect=checkpoint), \
             ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(app.lane.callbacks["learning_intelligence"]) for _ in range(2)]
            self.assertEqual([future.result(timeout=5)["status"] for future in futures], ["PROGRESS", "PROGRESS"])
        self.assertEqual(original.call_count, 2)
        self.assertEqual(len(connections), 2)
        self.assertIsNot(contexts[0], contexts[1])
        self.assertIs(app.connect, original)
        for raw in connections:
            factories = [factory for connection, factory in seen if connection is raw]
            self.assertEqual(len(factories), 3)
            self.assertTrue(all(factory is factories[0] for factory in factories))
            self.assertTrue(raw.closed)
        self.assertIsNot(seen[0][1], next(factory for raw, factory in seen if raw is not seen[0][0]))


class TradeCallbackContracts(unittest.TestCase):
    def setUp(self):
        self.ns = namespace(Mock())
        self.ns.update(VP=SimpleNamespace(_v90r29_upsert_episode=Mock()), emit=Mock())
        self.app = C.ContinuousLearning(self.ns)
        self.app.ready = True
        self.app.trade = TRADE.TradeLearning(self.ns)
        self.connection = Mock()
        self.connection.execute.return_value.fetchall.return_value = []
        transaction = patch.object(self.app.trade, "_transaction", return_value=nullcontext(self.connection))
        transaction.start(); self.addCleanup(transaction.stop)
        verification = patch.object(C.KNOWLEDGE, "update_integrity_verification")
        verification.start(); self.addCleanup(verification.stop)
        self.run_job = self.app.lane.callbacks["learning_trade_evidence"]

    def test_only_inner_lease_commits_phase_with_existing_limits(self):
        lease = {"cursor": {"phase": "materialize", "after": ["immutable-close", "trade-id"]}}
        with patch.object(STORE, "claim_job", return_value=lease) as claim, \
             patch.object(STORE, "checkpoint_job", return_value=True) as checkpoint:
            result = self.run_job()
        claim.assert_called_once_with(self.ns["pg_connect"], TRADE.JOB_NAME, TRADE.VERSION, lease_seconds=60)
        checkpoint.assert_called_once()
        self.assertEqual(checkpoint.call_args.kwargs["cursor"],
                         {"phase": "revalidate", "after": ["immutable-close", "trade-id"]})
        self.assertEqual(lease["cursor"]["phase"], "materialize")
        self.assertEqual(result["stage"], "materialize")
        self.assertEqual(result["materialized"], 0)
        self.assertEqual(result["batch_limit"], TRADE.BATCH_SIZE)
        self.assertIsNotNone(self.app._stats["last_success_at"])
        self.assertEqual(self.app.lane.options["learning_trade_evidence"],
                         {"interval_seconds": 45, "lightweight": True, "estimated_peak_mb": 16, "max_seconds": 6})
        self.assertEqual(self.ns["emit"].call_args.kwargs,
                         {"stage": "materialize", "status": "OK", "next_stage": "revalidate", "materialized": 0})

    def test_bootstrap_and_busy_lease_never_record_completion(self):
        with patch.object(STORE, "claim_job", return_value=None) as claim, \
             patch.object(STORE, "checkpoint_job") as checkpoint:
            self.app.ready = False
            self.assertEqual(self.run_job()["reason"], "LEARNING_BOOTSTRAP_PENDING")
            claim.assert_not_called()
            self.app.ready = True
            self.assertEqual(self.run_job()["status"], "DEFERRED_BUSY")
            claim.assert_called_once()
            checkpoint.assert_not_called()
        self.assertIsNone(self.app._stats["last_success_at"])

    def test_rejected_inner_checkpoint_does_not_advance_or_mark_success(self):
        lease = {"cursor": {"phase": "materialize"}}
        with patch.object(STORE, "claim_job", return_value=lease) as claim, \
             patch.object(STORE, "checkpoint_job", side_effect=[False, True]) as checkpoint:
            with self.assertRaisesRegex(RuntimeError, "lease expired"):
                self.run_job()
        claim.assert_called_once()
        self.assertEqual(checkpoint.call_count, 2)
        self.assertEqual(checkpoint.call_args.kwargs["status"], "ERROR")
        self.assertNotIn("cursor", checkpoint.call_args.kwargs)
        self.assertEqual(checkpoint.call_args.kwargs["retry_after_seconds"], 15)
        self.assertEqual(lease["cursor"], {"phase": "materialize"})
        self.assertIsNone(self.app._stats["last_success_at"])
        self.assertEqual(self.ns["emit"].call_args.kwargs,
                         {"stage": "materialize", "status": "ERROR", "error_type": "RuntimeError"})

    def test_sql_failure_keeps_inner_retry_and_no_outer_error_checkpoint(self):
        self.connection.execute.side_effect = RuntimeError("query failed")
        with patch.object(STORE, "claim_job", return_value={"cursor": {}}) as claim, \
             patch.object(STORE, "checkpoint_job", return_value=True) as checkpoint:
            with self.assertRaisesRegex(RuntimeError, "query failed"):
                self.run_job()
        claim.assert_called_once(); checkpoint.assert_called_once()
        self.assertEqual(checkpoint.call_args.kwargs["status"], "ERROR")
        self.assertNotIn("cursor", checkpoint.call_args.kwargs)
        self.assertIsNone(self.app._stats["last_success_at"])
        self.assertIn("query failed", self.app._stats["last_error"])

    def test_deadline_after_inner_commit_preserves_progress_but_reports_failure(self):
        def checkpoint(*args, **kwargs):
            self.app.lane.deadline = time.monotonic()-1
            return True
        with patch.object(STORE, "claim_job", return_value={"cursor": {}}), \
             patch.object(STORE, "checkpoint_job", side_effect=checkpoint) as saved:
            with self.assertRaises(TimeoutError):
                self.run_job()
        saved.assert_called_once()
        self.assertEqual(saved.call_args.kwargs["cursor"]["phase"], "revalidate")
        self.assertIsNone(self.app._stats["last_success_at"])

    def test_cooperative_inner_deferral_keeps_one_retry_and_previous_diagnostics(self):
        deferred = C.MaintenanceDeferred("DEFERRED_TIME_BUDGET", reason="synthetic_deadline")
        self.connection.execute.side_effect = deferred
        self.app._stats.update(last_error="previous diagnostic", last_success_at="previous success")
        lease = {"cursor": {"phase": "materialize"}}
        with patch.object(STORE, "claim_job", return_value=lease) as claim, \
             patch.object(STORE, "checkpoint_job", return_value=True) as checkpoint:
            with self.assertRaises(C.MaintenanceDeferred) as raised:
                self.run_job()
        self.assertIs(raised.exception, deferred)
        claim.assert_called_once(); checkpoint.assert_called_once()
        self.assertEqual(checkpoint.call_args.kwargs["status"], "RETRY")
        self.assertEqual(checkpoint.call_args.kwargs["result"], deferred.result())
        self.assertNotIn("cursor", checkpoint.call_args.kwargs)
        self.assertEqual(lease["cursor"], {"phase": "materialize"})
        self.assertEqual(self.app._stats["last_error"], "previous diagnostic")
        self.assertEqual(self.app._stats["last_success_at"], "previous success")
        self.assertFalse(any(call.kwargs.get("status") == "ERROR" for call in self.ns["emit"].call_args_list))

    def test_cooperative_deferral_after_inner_commit_keeps_progress_without_outer_error(self):
        deferred = C.MaintenanceDeferred("DEFERRED_TIME_BUDGET", reason="synthetic_after_commit")
        def committed(*args, **kwargs):
            self.app.lane.check_budget = Mock(side_effect=deferred)
            return True
        with patch.object(STORE, "claim_job", return_value={"cursor": {}}) as claim, \
             patch.object(STORE, "checkpoint_job", side_effect=committed) as checkpoint:
            with self.assertRaises(C.MaintenanceDeferred) as raised:
                self.run_job()
        self.assertIs(raised.exception, deferred)
        claim.assert_called_once(); checkpoint.assert_called_once()
        self.assertEqual(checkpoint.call_args.kwargs["status"], "OK")
        self.assertEqual(checkpoint.call_args.kwargs["cursor"]["phase"], "revalidate")
        self.assertIsNone(self.app._stats["last_success_at"])
        self.assertIsNone(self.app._stats["last_error"])
        self.assertEqual(self.ns["emit"].call_args.kwargs["status"], "OK")

    def test_diagnostic_failure_cannot_rollback_a_successful_phase(self):
        self.ns["emit"].side_effect = RuntimeError("logging unavailable")
        with patch.object(STORE, "claim_job", return_value={"cursor": {}}), \
             patch.object(STORE, "checkpoint_job", return_value=True) as checkpoint:
            self.assertEqual(self.run_job()["status"], "OK")
        checkpoint.assert_called_once()


@unittest.skipUnless(os.getenv("VERITAS_QUALITY_TEST_DSN"), "isolated PostgreSQL test database not configured")
class IntelligenceConnectionSQLTests(unittest.TestCase):
    connect = state_tests.DurableStateSQLTests.connect
    tearDown = state_tests.DurableStateSQLTests.tearDown
    epoch = "2026-10-01T00:00:00+00:00"
    job_name = "learning_intelligence"

    def setUp(self):
        state_tests.DurableStateSQLTests.setUp(self)
        for key, value in (("_CACHE", {"at": 0., "epoch": None, "value": None}),
                           ("_SNAPSHOT", None), ("_RESTORED_EPOCH", self.epoch),
                           ("_BUILD_LOCK", threading.Lock()),
                           ("_REFRESH_STATE", {"status": "NOT_STARTED", "last_error": None})):
            item = patch.object(C.INTELLIGENCE, key, value)
            item.start(); self.addCleanup(item.stop)
        env = patch.dict(os.environ, {"VERITAS_PRODUCTION_CANDIDATE_EPOCH": self.epoch})
        env.start(); self.addCleanup(env.stop)
        with self.connect() as c:
            c.execute("CREATE TABLE intelligence_connection_probe (name text PRIMARY KEY, value integer NOT NULL)")
            c.execute("INSERT INTO intelligence_connection_probe VALUES ('work',1)")
        self.opened = []
        @contextmanager
        def configured_connect():
            with self.connect() as raw:
                self.opened.append(raw)
                raw.execute("SET statement_timeout='9s'; SET lock_timeout='7s'")
                yield raw
        self.original_connect = configured_connect
        self.seed_step()

    def seed_step(self):
        self.opened.clear()
        self.app = C.ContinuousLearning(namespace(self.original_connect)); self.app.ready = True
        self.app.lane.current_budget = lambda: {"sql_timeout_ms": 2000,
            "remaining_seconds": max(0., self.app.lane.deadline-time.monotonic())}
        stamp = C.clock()
        self.initial_work = {"status": "BUILDING", "epoch": self.epoch, "ami_version": C.INTELLIGENCE.VERSION,
            "cycle_id": "synthetic-connection-cycle", "started_at": stamp.timestamp(), "stage": "portfolio",
            "offset": 2200, "sample_n": 2200, "episodes": [], "learning_progress": {}}
        self.initial_cursor = {"phase": "scorecard", "scorecard_work": {
            "cycle_id": self.initial_work["cycle_id"], "stage": "portfolio", "offset": 2200}}
        with self.connect() as c:
            c.execute("DELETE FROM veritas_learning_jobs WHERE name=%s", (self.job_name,))
            c.execute("UPDATE intelligence_connection_probe SET value=1")
            self.assertTrue(STORE.publish_snapshot_in_transaction(c, C.SCORECARD.WORK_SLOT,
                C.SCORECARD.WORK_VERSION, self.initial_work, observed_at=stamp))
        lease = STORE.claim_job(self.connect, self.job_name, C.VERSION)
        self.assertTrue(STORE.checkpoint_job(self.connect, lease, status="OK", cursor=self.initial_cursor,
                                            result={"status": "OK", "marker": "previous"}))

    def job(self, c):
        return c.execute("SELECT status,cursor,last_good,owner,fence FROM veritas_learning_jobs WHERE name=%s",
                         (self.job_name,)).fetchone()

    def work(self, c):
        return c.execute("SELECT payload FROM veritas_learning_snapshots WHERE name=%s",
                         (C.SCORECARD.WORK_SLOT,)).fetchone()["payload"]

    def value(self, c):
        return c.execute("SELECT value FROM intelligence_connection_probe WHERE name='work'").fetchone()["value"]

    def assert_idle_settings(self, raw):
        from psycopg.pq import TransactionStatus
        self.assertTrue(raw.autocommit)
        self.assertEqual(raw.info.transaction_status, TransactionStatus.IDLE)
        self.assertEqual(raw.execute("SHOW statement_timeout").fetchone()["statement_timeout"], "9s")
        self.assertEqual(raw.execute("SHOW lock_timeout").fetchone()["lock_timeout"], "7s")

    def assert_unlocked(self, observer):
        # NOWAIT makes a leaked phase lock fail promptly, not wait for the lane.
        observer.execute("SELECT name FROM veritas_learning_jobs WHERE name=%s FOR UPDATE NOWAIT", (self.job_name,))
        observer.execute("SELECT name FROM veritas_learning_snapshots WHERE name=%s FOR UPDATE NOWAIT",
                         (C.SCORECARD.WORK_SLOT,))
        observer.execute("SELECT name FROM intelligence_connection_probe FOR UPDATE NOWAIT")
        self.assertTrue(observer.execute("SELECT pg_try_advisory_xact_lock(%s) AS held", (STORE._LOCK,)).fetchone()["held"])

    def test_real_ami_step_uses_one_connection_and_three_visible_commits_without_setting_or_lock_leaks(self):
        original_claim, original_checkpoint = STORE.claim_job, STORE.checkpoint_job
        original_transaction, original_refresh = STORE._transaction, C.INTELLIGENCE.refresh_snapshot
        transactions, factories = [], []
        def record(c, phase):
            row = c.execute("SELECT pg_current_xact_id()::text AS xid,pg_backend_pid() AS pid").fetchone()
            transactions.append((phase, row["xid"], row["pid"]))
        @contextmanager
        def recorded_transaction(factory):
            with original_transaction(factory) as c:
                record(c, "claim" if not transactions else "checkpoint")
                yield c
        with self.connect() as observer:
            def claim(factory, *args, **kwargs):
                factories.append(factory)
                lease = original_claim(factory, *args, **kwargs)
                self.assertEqual(self.job(observer)["status"], "RUNNING")
                self.assertEqual(self.job(observer)["fence"], lease["fence"])
                self.assertEqual(self.job(observer)["cursor"], self.initial_cursor)
                self.assert_idle_settings(self.opened[0]); self.assert_unlocked(observer)
                return lease
            def phase(c, epoch):
                self.assertEqual(epoch, self.epoch)
                record(c, "work")
                self.assertEqual(c.execute("SHOW statement_timeout").fetchone()["statement_timeout"], "2s")
                self.assertEqual(c.execute("SHOW lock_timeout").fetchone()["lock_timeout"], "250ms")
                c.execute("UPDATE intelligence_connection_probe SET value=2 WHERE name='work'")
                self.assertEqual(self.value(observer), 1)
                self.assertEqual(self.work(observer)["stage"], "portfolio")
                with self.assertRaises(self.driver.errors.LockNotAvailable):
                    observer.execute("SELECT name FROM intelligence_connection_probe FOR UPDATE NOWAIT")
                self.assertFalse(observer.execute("SELECT pg_try_advisory_xact_lock(%s) AS held", (STORE._LOCK,)).fetchone()["held"])
                return {"status": "OK", "n": 1}
            def refresh(factory, *args, **kwargs):
                factories.append(factory)
                result = original_refresh(factory, *args, **kwargs)
                self.assertEqual(result["status"], "PROGRESS")
                self.assertEqual(result["stage"], "learning")
                self.assertEqual(self.value(observer), 2)
                self.assertEqual(self.work(observer)["stage"], "learning")
                self.assert_idle_settings(self.opened[0]); self.assert_unlocked(observer)
                return result
            def checkpoint(factory, *args, **kwargs):
                factories.append(factory)
                accepted = original_checkpoint(factory, *args, **kwargs)
                self.assertTrue(accepted)
                self.assertEqual(self.job(observer)["status"], "OK")
                self.assertEqual(self.job(observer)["cursor"]["scorecard_work"]["stage"], "learning")
                self.assertIsNone(self.job(observer)["owner"])
                self.assert_idle_settings(self.opened[0]); self.assert_unlocked(observer)
                return accepted
            self.app.lane.reset()
            with patch.object(STORE, "_transaction", recorded_transaction), \
                 patch.object(STORE, "claim_job", side_effect=claim), \
                 patch.object(STORE, "checkpoint_job", side_effect=checkpoint), \
                 patch.object(C.INTELLIGENCE, "refresh_snapshot", side_effect=refresh), \
                 patch.object(C.INTELLIGENCE, "_query_fresh_portfolio", side_effect=phase):
                self.assertEqual(self.app.lane.callbacks[self.job_name]()["status"], "PROGRESS")
        self.assertEqual([phase for phase, _, _ in transactions], ["claim", "work", "checkpoint"])
        self.assertEqual(len({xid for _, xid, _ in transactions}), 3)
        self.assertEqual(len({pid for _, _, pid in transactions}), 1)
        self.assertEqual(len(self.opened), 1)
        self.assertTrue(self.opened[0].closed)
        self.assertEqual(len(factories), 3)
        self.assertTrue(all(factory is factories[0] for factory in factories))
        self.assertIs(self.app.connect, self.original_connect)
        self.assertIsNone(self.app._stats["last_error"])

    def test_real_work_timeout_rolls_back_only_work_and_recovers_on_a_fresh_connection(self):
        original_checkpoint = STORE.checkpoint_job
        errors, work_calls, recovery_factories = [], [], []
        with self.connect() as observer:
            before = self.job(observer)
            def phase(c, epoch):
                work_calls.append(True)
                self.assertEqual(self.job(observer)["status"], "RUNNING")
                c.execute("UPDATE intelligence_connection_probe SET value=2 WHERE name='work'")
                c.execute("SET LOCAL statement_timeout='30ms'")
                try:
                    c.execute("SELECT pg_sleep(.1)")
                except self.driver.errors.QueryCanceled as error:
                    errors.append(error)
                    raise
            def checkpoint(factory, *args, **kwargs):
                recovery_factories.append(factory)
                self.assertIs(factory, self.original_connect)
                self.assertEqual(kwargs["status"], "ERROR")
                self.assertNotIn("cursor", kwargs)
                # Claim committed before the failed AMI transaction. Its lease
                # is still visible, while every work write has rolled back.
                self.assertEqual(self.job(observer)["status"], "RUNNING")
                self.assertEqual(self.job(observer)["fence"], before["fence"]+1)
                self.assertEqual(self.job(observer)["cursor"], self.initial_cursor)
                self.assertEqual(self.value(observer), 1)
                self.assertEqual(self.work(observer), self.initial_work)
                self.assert_idle_settings(self.opened[0]); self.assert_unlocked(observer)
                return original_checkpoint(factory, *args, **kwargs)
            self.app.lane.reset()
            with patch.object(C.INTELLIGENCE, "_query_fresh_portfolio", side_effect=phase), \
                 patch.object(STORE, "checkpoint_job", side_effect=checkpoint), \
                 self.assertRaises(self.driver.errors.QueryCanceled) as caught:
                self.app.lane.callbacks[self.job_name]()
            after = self.job(observer)
            self.assertEqual(after["status"], "ERROR")
            self.assertEqual(after["cursor"], before["cursor"])
            self.assertEqual(after["last_good"], before["last_good"])
            self.assertIsNone(after["owner"])
        self.assertEqual(len(errors), 1)
        self.assertIs(caught.exception, errors[0])
        self.assertEqual(work_calls, [True])
        self.assertEqual(recovery_factories, [self.original_connect])
        self.assertEqual(len(self.opened), 2)
        self.assertIsNot(self.opened[0], self.opened[1])
        self.assertTrue(all(raw.closed for raw in self.opened))
        self.assertIsNone(self.app._stats["last_success_at"])
        self.assertEqual(C.INTELLIGENCE._REFRESH_STATE["last_error"], "QueryCanceled")

    def test_real_lost_fence_and_lost_commit_ack_never_repeat_or_undo_committed_work(self):
        original_checkpoint = STORE.checkpoint_job
        for failure in ("lost_fence", "lost_commit_ack"):
            with self.subTest(failure=failure):
                self.seed_step()
                acknowledgement_error = OSError("synthetic checkpoint acknowledgement lost")
                work_calls, checkpoint_factories, replacement = [], [], []
                with self.connect() as observer:
                    def phase(c, epoch):
                        work_calls.append(True)
                        c.execute("UPDATE intelligence_connection_probe SET value=value+1 WHERE name='work'")
                        return {"status": "OK", "n": 1}
                    def checkpoint(factory, lease, **kwargs):
                        checkpoint_factories.append((factory, kwargs["status"]))
                        self.assertEqual(self.value(observer), 2)
                        self.assertEqual(self.work(observer)["stage"], "learning")
                        if kwargs["status"] == "OK":
                            if failure == "lost_fence":
                                observer.execute("UPDATE veritas_learning_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE name=%s",
                                                 (self.job_name,))
                                replacement.append(STORE.claim_job(lambda: nullcontext(observer), self.job_name, C.VERSION))
                                self.assertIsNotNone(replacement[0])
                                self.assertFalse(original_checkpoint(factory, lease, **kwargs))
                                return False
                            self.assertTrue(original_checkpoint(factory, lease, **kwargs))
                            raise acknowledgement_error
                        self.assertIs(factory, self.original_connect)
                        self.assertNotIn("cursor", kwargs)
                        self.assertFalse(original_checkpoint(factory, lease, **kwargs))
                        return False
                    self.app.lane.reset()
                    with patch.object(C.INTELLIGENCE, "_query_fresh_portfolio", side_effect=phase), \
                         patch.object(STORE, "checkpoint_job", side_effect=checkpoint), \
                         self.assertRaises(RuntimeError if failure == "lost_fence" else OSError) as caught:
                        self.app.lane.callbacks[self.job_name]()
                    after = self.job(observer)
                    self.assertEqual(self.value(observer), 2)
                    self.assertEqual(self.work(observer)["stage"], "learning")
                    if failure == "lost_fence":
                        self.assertEqual(after["fence"], replacement[0]["fence"])
                        self.assertEqual(after["owner"], replacement[0]["owner"])
                        self.assertEqual(after["status"], "RUNNING")
                        self.assertEqual(after["cursor"], self.initial_cursor)
                        self.assertIn("lease expired", str(caught.exception))
                    else:
                        self.assertIs(caught.exception, acknowledgement_error)
                        self.assertEqual(after["status"], "OK")
                        self.assertIsNone(after["owner"])
                        self.assertEqual(after["cursor"]["scorecard_work"]["stage"], "learning")
                self.assertEqual(work_calls, [True])
                self.assertEqual([status for _, status in checkpoint_factories], ["OK", "ERROR"])
                self.assertEqual(len(self.opened), 2)
                self.assertTrue(all(raw.closed for raw in self.opened))
                self.assertIsNone(self.app._stats["last_success_at"])


@unittest.skipUnless(os.getenv("VERITAS_QUALITY_TEST_DSN"), "isolated PostgreSQL test database not configured")
class ContinuousPipelineSQLTests(unittest.TestCase):
    connect = state_tests.DurableStateSQLTests.connect
    tearDown = state_tests.DurableStateSQLTests.tearDown

    def setUp(self):
        state_tests.DurableStateSQLTests.setUp(self)
        self.saved_bridge = deepcopy(BRIDGE._state)
        self.addCleanup(self.restore_bridge)
        self.ns = namespace(self.connect)
        import veritas_portfolio as portfolio
        self.ns["VP"] = portfolio
        self.app = C.ContinuousLearning(self.ns)
        self.context = C.Budget(self.ns["_v90_background_maintenance"])
        with self.connect() as c:
            c.execute("""CREATE TABLE ledger_events(id bigserial PRIMARY KEY,entity_key text NOT NULL,
                event_type text NOT NULL,asset text,horizon text,payload jsonb NOT NULL,
                event_ts timestamptz NOT NULL DEFAULT now())""")
            c.execute("""CREATE TABLE v90_decision_episodes(entity_key text PRIMARY KEY,decision_ts timestamptz,
                outcome_ts timestamptz,asset text,horizon text,regime text,decision text,
                forward_return float8,mfe float8,mae float8)""")
            c.execute("""CREATE TABLE shadow_trades(trade_id text PRIMARY KEY,closed_at timestamptz,
                status text,total_pnl_fraction float8)""")
            c.execute("CREATE TABLE knowledge_sources(id serial PRIMARY KEY)")
            c.execute("CREATE TABLE knowledge_rules(id serial PRIMARY KEY)")
            c.execute("""CREATE TABLE paper_trades(trade_id text PRIMARY KEY,asset text,direction text,horizon text,
                status text,opened_at timestamptz,closed_at timestamptz,gross_pnl_rub float8,fees_rub float8,
                funding_rub float8,net_pnl_rub float8,portfolio_name text,setup text,max_fraction float8,
                avg_entry_price float8,avg_exit_price float8,payload jsonb)""")
        for _ in range(5):
            self.ns["_v90_background_maintenance"].reset()
            self.app.bootstrap()
        self.assertTrue(self.app.ready)

    def restore_bridge(self):
        with BRIDGE._lock:
            BRIDGE._state.clear(); BRIDGE._state.update(self.saved_bridge)

    def call(self, method, cursor=None):
        self.ns["_v90_background_maintenance"].reset()
        return method(self.context, cursor or {})

    def insert_decision(self, entity="new-decision"):
        record = captured(entity)
        with self.connect() as c:
            c.execute("INSERT INTO ledger_events(entity_key,event_type,asset,horizon,payload) VALUES(%s,'decision','BTC','1m',%s::jsonb)",
                      (entity, json.dumps({"learning_provenance": record["provenance"]})))

    def consume_candidate(self, cursor=None):
        for _ in range(16):
            result, cursor = self.call(self.app.candidates, cursor)
            if result.get("stage") == "ACK":
                return result, cursor
        self.fail("candidate did not reach ACK")

    def test_registered_trade_job_resumes_only_inner_durable_cursor(self):
        self.app.lane.reset()
        first = self.app.lane.callbacks["learning_trade_evidence"]()
        self.assertEqual(first["stage"], "materialize")
        with self.connect() as c:
            rows = c.execute("SELECT name,cursor,fence FROM veritas_learning_jobs").fetchall()
        self.assertEqual([r["name"] for r in rows], [TRADE.JOB_NAME])
        self.assertEqual(rows[0]["cursor"]["phase"], "revalidate")
        first_fence = rows[0]["fence"]
        # A recreated process resumes the sole authoritative phase without an
        # outer learning_trade_evidence job or a cursor copied into RAM.
        restored_ns = namespace(self.connect)
        restored_ns["VP"] = self.ns["VP"]
        restored = C.ContinuousLearning(restored_ns)
        restored.trade = TRADE.TradeLearning(restored_ns)
        restored.ready = True
        restored.lane.reset()
        second = restored.lane.callbacks["learning_trade_evidence"]()
        self.assertEqual(second["stage"], "revalidate")
        with self.connect() as c:
            rows = c.execute("SELECT name,cursor,fence,status FROM veritas_learning_jobs").fetchall()
        self.assertEqual([r["name"] for r in rows], [TRADE.JOB_NAME])
        self.assertEqual(rows[0]["cursor"]["phase"], "export")
        self.assertEqual(rows[0]["fence"], first_fence+1)
        self.assertEqual(rows[0]["status"], "OK")

    def test_frozen_ledger_quote_outcome_dedup_and_process_recreation(self):
        # Establish the new protocol's starting cursor before a new decision.
        _, cursor = self.call(self.app.ingest)
        self.insert_decision()
        result, cursor = self.call(self.app.ingest, cursor)
        self.assertEqual(result["imported"], 1)
        with patch.object(C, "clock", return_value=NOW), \
             patch.object(self.app, "_quote", return_value=fresh_quote()):
            result, _ = self.call(self.app.outcomes)
        self.assertEqual(result["resolved"], 1)
        with patch.object(AUTO, "_clock", wraps=AUTO._clock):
            result, _ = self.consume_candidate()
        self.assertEqual(result["counts"]["direction"], 1)
        # Simulate acknowledgement loss after the learner already committed.
        with self.connect() as c:
            c.execute("UPDATE learning_forecasts SET status='READY',learned_at=NULL")
        replay, _ = self.consume_candidate()
        self.assertEqual(replay["counts"]["direction"], 1)
        restored_ns = namespace(self.connect)
        restored_ns["VP"] = self.ns["VP"]
        restored = C.ContinuousLearning(restored_ns)
        for _ in range(5):
            restored.ns["_v90_background_maintenance"].reset()
            restored.bootstrap()
        self.assertTrue(restored.ready)
        self.assertEqual(restored.snapshot()["counts"]["direction"], 1)
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) AS n FROM autonomous_learning_seen").fetchone()["n"], 1)

    def test_malformed_record_advances_cursor_without_creating_example(self):
        _, cursor = self.call(self.app.ingest)
        with self.connect() as c:
            c.execute("INSERT INTO ledger_events(entity_key,event_type,asset,horizon,payload) VALUES('bad','decision','BTC','1m','{}'::jsonb)")
        result, cursor = self.call(self.app.ingest, cursor)
        self.assertEqual(result["excluded_missing_provenance"], 1)
        self.assertGreater(cursor["last_id"], 0)
        again, _ = self.call(self.app.ingest, cursor)
        self.assertEqual(again["excluded_missing_provenance"], 0)

    def test_initial_replay_is_bounded_and_new_decisions_have_priority(self):
        # A large legacy tail cannot consume the current quote's entire
        # observation window. Keep separate durable live/replay watermarks.
        with self.connect() as c:
            c.execute("""INSERT INTO ledger_events(entity_key,event_type,asset,horizon,payload)
                SELECT 'legacy-'||n,'decision','BTC','1m','{}'::jsonb
                FROM generate_series(1,%s) n""", (C.INGEST_BATCH*3,))
        first, cursor = self.call(self.app.ingest)
        self.assertEqual(first["scanned"], C.INGEST_BATCH)
        self.assertTrue(first["backfill_pending"])
        initial_live = cursor["last_id"]
        self.insert_decision("fresh-during-replay")
        current, cursor = self.call(self.app.ingest, cursor)
        self.assertEqual(current["imported"], 1)
        self.assertEqual(current["scanned"], C.INGEST_BATCH)
        self.assertGreater(cursor["last_id"], initial_live)
        for _ in range(4):
            result, cursor = self.call(self.app.ingest, cursor)
        self.assertFalse(result["backfill_pending"])
        self.assertEqual(cursor["scanned"], C.INGEST_BATCH*3+1)
        self.assertEqual(cursor["missing_provenance"], C.INGEST_BATCH*3)
        self.assertEqual(cursor["imported"], 1)
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) n FROM learning_forecasts").fetchone()["n"], 1)

    def test_legacy_forward_cursor_resumes_without_replaying_old_records(self):
        self.insert_decision("already-scanned")
        with self.connect() as c:
            previous = c.execute("SELECT max(id) n FROM ledger_events").fetchone()["n"]
        self.insert_decision("new-after-upgrade")
        result, cursor = self.call(self.app.ingest, {"last_id": previous, "imported": 5})
        self.assertEqual(result["imported"], 1)
        self.assertEqual(cursor["imported"], 6)
        self.assertFalse(result["backfill_pending"])
        with self.connect() as c:
            rows = c.execute("SELECT entity_key FROM learning_forecasts").fetchall()
            self.assertEqual([r["entity_key"] for r in rows], ["new-after-upgrade"])

    def test_replay_limit_uses_insert_order_when_event_clocks_differ(self):
        with self.connect() as c:
            c.execute("""INSERT INTO ledger_events(entity_key,event_type,asset,horizon,payload,event_ts)
                VALUES ('old-id-future-clock','decision','BTC','1m','{}','2030-01-01'),
                       ('middle-1','decision','BTC','1m','{}','2020-01-01'),
                       ('middle-2','decision','BTC','1m','{}','2021-01-01'),
                       ('newest-id','decision','BTC','1m','{}','2029-01-01')""")
        with patch.object(C, 'INITIAL_LOOKBACK', 2):
            result, cursor = self.call(self.app.ingest)
        self.assertEqual(result['scanned'], 2)
        self.assertEqual(cursor['missing_provenance'], 2)
        self.assertFalse(cursor['backfill_last_id'] < cursor['backfill_until_id'])

    def test_committed_seal_before_lost_checkpoint_is_counted_once_on_retry(self):
        _, cursor = self.call(self.app.ingest)
        self.insert_decision("committed-before-checkpoint")
        original = deepcopy(cursor)
        first, advanced = self.call(self.app.ingest, original)
        self.assertEqual(first["imported"], 1)
        # Recreate exactly the original durable job cursor after a process
        # interruption between forecast commit and job checkpoint commit.
        replay, recovered = self.call(self.app.ingest, cursor)
        self.assertEqual(replay["imported"], 1)
        self.assertEqual(recovered["imported"], advanced["imported"])
        self.assertEqual(recovered["last_id"], advanced["last_id"])
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) n FROM learning_forecasts").fetchone()["n"], 1)

    def test_repeated_unresolved_scope_preserves_first_seal_and_leaves_queue_capacity(self):
        _,cursor=self.call(self.app.ingest)
        self.insert_decision('first')
        first,cursor=self.call(self.app.ingest,cursor)
        self.assertEqual(first['imported'],1)
        self.insert_decision('second')
        duplicate,cursor=self.call(self.app.ingest,cursor)
        self.assertEqual(duplicate['imported'],0);self.assertEqual(duplicate['coalesced'],1)
        with self.connect() as c:
            rows=c.execute('SELECT entity_key,evidence FROM learning_forecasts').fetchall()
            self.assertEqual([r['entity_key'] for r in rows],['first'])
            self.assertEqual(rows[0]['evidence'],captured('first')['provenance'])
            c.execute("UPDATE learning_forecasts SET status='EXCLUDED',learned_at=now()")
        self.insert_decision('new-window')
        result,_=self.call(self.app.ingest,cursor)
        self.assertEqual(result['imported'],1)

    def test_small_progress_snapshot_can_be_persisted_without_full_payload_reads(self):
        value = self.app.compute_progress(self.context)
        self.assertEqual(value["status"], "BUILDING")
        self.assertIsNone(value["index_vs_start"])
        self.assertEqual(value["outcome_evidence"], "LEGACY_DIAGNOSTIC_INDEX")
        result, _ = self.call(self.app.progress)
        self.assertEqual(result["status"], "OK")
        self.assertIsNotNone(STORE.load_snapshot(self.connect, "learning_progress", C.PROGRESS_VERSION))

    def test_cold_cohort_persists_restores_and_revalidates_on_real_postgres(self):
        old = dict(observation(0, training=True), regime="FIRST")
        other = dict(observation(1, training=True), regime="SECOND")
        with patch.object(AUTO, "MAX_SCOPES", 1):
            AUTO.run_batch(self.connect, [old, other], now=NOW)
            with self.connect() as c:
                self.assertEqual(c.execute("SELECT count(*) n FROM autonomous_learning_cold_scopes").fetchone()["n"], 1)
            result = AUTO.run_batch(self.connect, [dict(observation(2, training=True), regime="FIRST")], now=NOW)
            self.assertEqual(result["lessons"][0]["independent_ideas"], 2)
            result = AUTO.run_batch(self.connect, [dict(other, evidence_valid=False)], now=NOW)
            self.assertEqual(result["lessons"][0]["scope"]["regime"], "SECOND")
            self.assertTrue(result["lessons"][0]["quarantined"])
            with self.connect() as c:
                self.assertEqual(c.execute("SELECT count(*) n FROM autonomous_learning_scope_archive").fetchone()["n"], 1)


if __name__ == "__main__":
    unittest.main()
