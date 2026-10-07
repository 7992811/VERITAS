"""Prospective producer contracts and isolated PostgreSQL pipeline recovery."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import json
import os
import time
import unittest
from unittest.mock import patch

import veritas_autonomous_learning as AUTO
import veritas_continuous_learning as C
import veritas_learning_bridge as BRIDGE
import veritas_learning_state as STORE
from test_veritas_autonomous_learning import NOW, observation
from test_veritas_learning_bridge import receiver
import test_veritas_learning_state as state_tests


class Lane:
    def __init__(self):
        self.callbacks = {}
        self.deadline = time.monotonic()+6
    def register_periodic(self, name, callback, **options):
        self.callbacks[name] = callback
    def current_budget(self):
        return {"sql_timeout_ms": 2000}
    def check_budget(self):
        if time.monotonic() >= self.deadline:
            raise TimeoutError("test microjob exceeded six seconds")
    def reset(self):
        self.deadline = time.monotonic()+6
    def snapshot(self):
        return {"periodic": {}}


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
            result, _ = self.call(self.app.candidates)
        self.assertEqual(result["counts"]["direction"], 1)
        # Simulate acknowledgement loss after the learner already committed.
        with self.connect() as c:
            c.execute("UPDATE learning_forecasts SET status='READY',learned_at=NULL")
        replay, _ = self.call(self.app.candidates)
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
