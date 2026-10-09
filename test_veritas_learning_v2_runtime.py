from contextlib import contextmanager
import inspect
from types import SimpleNamespace
from unittest.mock import patch
import unittest

import veritas_continuous_learning as C
import veritas_learning_v2 as V2
from test_veritas_continuous_learning import namespace

class Rows:
    def __init__(self, rows): self.rows=rows
    def fetchall(self): return self.rows

class Cursor:
    def __init__(self, decisions, trades):
        self.decisions=decisions; self.trades=trades; self.calls=[]
    def execute(self, sql, args=()):
        self.calls.append((sql,args))
        if "FROM v90_decision_episodes" in sql:
            self.assertNotIn("ledger_events",sql)
            self.assertNotIn("LATERAL",sql.upper())
            return Rows(self.decisions)
        if "FROM v90_learning_episodes" in sql:
            return Rows(self.trades)
        raise AssertionError(sql)

class LearningV2RuntimeTests(unittest.TestCase):
    def test_job_publishes_shadow_snapshot_without_mutating_trading(self):
        decisions=[{
            "decision_id":100,"event_ts":"2026-10-08T10:00:00Z","asset":"BTC","horizon":"5m","regime":"TREND",
            "decision":"NO_TRADE","setup_family":"BREAKOUT","policy_hash":"p","source_key":"S",
            "candidate_direction":"LONG","final_gate_blockers":["IMPULSE_ALREADY_PASSED"],
            "forward_return":.01,
        } for _ in range(24)]
        trades=[{
            "closed_at":"2026-10-08T10:10:00Z","asset":"BTC","horizon":"5m","regime":"TREND",
            "setup_family":"BREAKOUT","policy_hash":"p","source_key":"S","mae":-.004,"mfe":.014,
            "capture_ratio":.2,"net_pnl_rub":10.0,"primary_attribution":"OK",
        } for _ in range(20)]
        events=[]
        ns=namespace(lambda: None)
        ns["emit"]=lambda event,**values: events.append((event,values))
        app=C.ContinuousLearning(ns); app.ready=True
        @contextmanager
        def tx(connect, context):
            yield Cursor(decisions,trades)
        context=SimpleNamespace(check=lambda:None)
        registry={"version":"test","counts":{},"candidates":[],"shadow_champions":[]}
        with patch.object(C,"transaction",tx), \
             patch.object(C.LEARNING_V2_REGISTRY,"sync",return_value=registry), \
             patch.object(C.STORE,"publish_snapshot",return_value=True) as publish:
            result,cursor=app.learning_v2_shadow(context,{})
        self.assertEqual(result["status"],"OK")
        self.assertEqual(result["asset"],"BTC")
        self.assertEqual(cursor["asset_index"],1)
        self.assertGreater(result["hypotheses"],0)
        self.assertEqual(events[-1][0],"learning_v2_shadow_snapshot")
        self.assertEqual(events[-1][1]["asset"],"BTC")
        self.assertFalse(events[-1][1]["production_influence"])
        self.assertIn("zero_candidate_reason",events[-1][1])
        self.assertIn("known_blockers",events[-1][1])
        snap=app.snapshot()["learning_v2"]
        self.assertFalse(snap["automatic_production_promotion"])
        self.assertEqual(snap["registry"],registry)
        self.assertTrue(any(h["kind"]=="ENTRY_BLOCKER_RELAXATION" for h in snap["hypotheses"]))
        self.assertTrue(all(h["mode"]=="SHADOW_ONLY" for h in snap["hypotheses"]))
        publish.assert_called_once()
        self.assertEqual(publish.call_args.args[1],C.LEARNING_V2_SNAPSHOT_NAME)
        self.assertEqual(publish.call_args.args[2],V2.VERSION)

    def test_decision_projection_is_materialized_and_has_no_raw_ledger_join(self):
        source=inspect.getsource(C.ContinuousLearning.learning_v2_shadow)
        self.assertIn("FROM v90_decision_episodes",source)
        self.assertIn("admission_eligible",source)
        self.assertIn("final_gate_status",source)
        self.assertIn("final_gate_blockers",source)
        self.assertIn("decision_id",source)
        self.assertNotIn("ledger_events",source)
        self.assertNotIn("LATERAL",source.upper())

    def test_legacy_enrichment_is_separate_and_bounded(self):
        source=inspect.getsource(C.ContinuousLearning.episode_enrichment)
        self.assertIn("batch_size=64",source)
        self.assertIn("max_batches=1",source)
        self.assertIn("max_seconds=3.0",source)

    def test_trade_cohort_provenance_comes_from_original_trade(self):
        source=inspect.getsource(C.ContinuousLearning.learning_v2_shadow)
        self.assertIn("JOIN paper_trades t ON t.trade_id=e.trade_id",source)
        self.assertIn("t.payload#>>'{price_source_lock,key}'",source)
        self.assertIn("t.payload#>>'{price_source_lock,contract_id}'",source)
        self.assertIn("t.payload->>'strategy_policy_hash'",source)

    def test_episode_enrichment_calls_bounded_backfill(self):
        calls=[]
        ns=namespace(lambda:None)
        ns["_v90_backfill_decision_episodes"]=lambda **kw: calls.append(kw) or {
            "status":"OK","inserted":1,"enriched":2,"batches":1}
        app=C.ContinuousLearning(ns)
        context=SimpleNamespace(check=lambda:None)
        result,cursor=app.episode_enrichment(context,{"x":1})
        self.assertEqual(result["status"],"OK")
        self.assertEqual(result["enriched"],2)
        self.assertEqual(cursor,{"x":1})
        self.assertEqual(calls,[{"batch_size":64,"max_batches":1,"max_seconds":3.0}])

    def test_periodic_job_is_registered_with_bounded_budget(self):
        app=C.ContinuousLearning(namespace(lambda: None))
        lane=app.lane
        self.assertEqual(C.LEARNING_V2_INPUT_LIMIT,128)
        self.assertIn("learning_episode_enrichment",lane.callbacks)
        self.assertIn("learning_v2_shadow",lane.callbacks)
        self.assertIn("learning_v2_replay",lane.callbacks)
        self.assertEqual(lane.options["learning_episode_enrichment"]["max_seconds"],4)
        self.assertEqual(lane.options["learning_episode_enrichment"]["interval_seconds"],120)
        self.assertEqual(lane.options["learning_v2_shadow"]["max_seconds"],5)
        self.assertEqual(lane.options["learning_v2_replay"]["max_seconds"],5)
        self.assertEqual(lane.options["learning_v2_replay"]["interval_seconds"],180)
        self.assertEqual(lane.options["learning_v2_shadow"]["interval_seconds"],60)
        self.assertTrue(lane.options["learning_v2_shadow"]["lightweight"])

if __name__=="__main__":
    unittest.main()
