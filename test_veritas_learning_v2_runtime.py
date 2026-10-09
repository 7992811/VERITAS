from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from pathlib import Path
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
    def fetchone(self): return self.rows[0] if self.rows else None

class Cursor:
    def __init__(self, decisions, trades, external=None):
        self.decisions=decisions; self.trades=trades; self.external=external; self.calls=[]
    def execute(self, sql, args=()):
        self.calls.append((sql,args))
        if "FROM learning_v2_snapshots" in sql:
            return Rows([self.external] if self.external else [])
        if "WITH recent AS MATERIALIZED" in sql:
            self.assert_materialized = "FROM v90_decision_episodes" in sql
            return Rows(self.decisions)
        if "FROM v90_learning_episodes" in sql:
            return Rows(self.trades)
        raise AssertionError(sql)

class LearningV2RuntimeTests(unittest.TestCase):
    def external_row(self,now,**overrides):
        payload={
            "version":V2.VERSION,"status":"SHADOW_READY",
            "producer":"EXTERNAL_LEARNING_WORKER",
            "worker_protocol":C.EXTERNAL_LEARNING_WORKER_PROTOCOL,
            "process_role":"learning","source_errors":[],
            "automatic_production_promotion":False,
            "hypotheses":[{"hypothesis_id":"h"}],"counts":{"ENTRY_BLOCKER_RELAXATION":1},
        }
        payload.update(overrides.pop("payload",{}))
        row={"version":V2.VERSION,"generated_at":now-timedelta(seconds=30),"payload":payload}
        row.update(overrides)
        return row

    def test_external_worker_state_accepts_only_fresh_verified_producer(self):
        now=datetime(2026,10,9,16,0,tzinfo=timezone.utc)
        state=C.external_worker_state(Cursor([],[],self.external_row(now)),now=now)
        self.assertTrue(state["active"])
        self.assertEqual(state["reason"],"FRESH_VERIFIED_EXTERNAL_WORKER")
        self.assertEqual(state["age_seconds"],30)

    def test_external_worker_state_rejects_stale_degraded_and_wrong_version(self):
        now=datetime(2026,10,9,16,0,tzinfo=timezone.utc)
        stale=self.external_row(now,generated_at=now-timedelta(seconds=C.EXTERNAL_LEARNING_MAX_AGE_SECONDS+1))
        self.assertEqual(C.external_worker_state(Cursor([],[],stale),now=now)["reason"],"EXTERNAL_SNAPSHOT_STALE")
        degraded=self.external_row(now,payload={"status":"DEGRADED","source_errors":["DECISIONS:NQ:Timeout"]})
        self.assertEqual(C.external_worker_state(Cursor([],[],degraded),now=now)["reason"],"EXTERNAL_SOURCE_DEGRADED")
        wrong=self.external_row(now,version="WRONG")
        self.assertEqual(C.external_worker_state(Cursor([],[],wrong),now=now)["reason"],"EXTERNAL_VERSION_MISMATCH")

    def test_job_uses_external_worker_without_running_local_research(self):
        now=datetime.now(timezone.utc)
        external=self.external_row(now)
        events=[]
        ns=namespace(lambda:None)
        ns["emit"]=lambda event,**values:events.append((event,values))
        app=C.ContinuousLearning(ns);app.ready=True
        @contextmanager
        def tx(connect,context):
            yield Cursor([],[],external)
        context=SimpleNamespace(check=lambda:None,sql_timeout_ms=2000)
        with patch.object(C,"transaction",tx), patch.object(C.LEARNING_V2,"research_snapshot",
             side_effect=AssertionError("local research ran during external handoff")):
            result,cursor=app.learning_v2_shadow(context,{"asset_index":3})
        self.assertEqual(result["mode"],"EXTERNAL_WORKER_ACTIVE")
        self.assertEqual(cursor["asset_index"],3)
        self.assertTrue(app._external_worker_active)
        self.assertEqual(app.snapshot()["learning_v2"]["handoff"]["mode"],"EXTERNAL_WORKER")
        self.assertEqual(events[-1][0],"learning_v2_handoff")
        self.assertFalse(events[-1][1]["production_influence"])

    def test_blueprint_uses_internal_database_reference_and_current_worker_limit(self):
        text=Path("render.yaml").read_text()
        self.assertIn("name: veritas-knowledge",text)
        self.assertIn("property: connectionString",text)
        self.assertIn("VERITAS_LEARNING_V2_PER_ASSET_LIMIT",text)
        self.assertNotIn("VERITAS_LEARNING_V2_LIMIT",text)
        dedicated=Path("render-learning.yaml").read_text()
        self.assertIn("name: veritas-learning-v2",dedicated)
        self.assertIn("name: veritas-knowledge",dedicated)
        self.assertIn("property: connectionString",dedicated)
        worker=Path("veritas_learning_worker.py").read_text()
        self.assertIn('producer"]="EXTERNAL_LEARNING_WORKER"',worker)
        self.assertIn('EXPECTED_DATABASE=os.getenv("VERITAS_LEARNING_EXPECTED_DATABASE"',worker)
        self.assertIn('LEARNING_DATABASE_REJECTED',worker)
        self.assertIn("SET statement_timeout='5000ms'",worker)
        self.assertIn("SET lock_timeout='500ms'",worker)
        self.assertNotIn("veritas_broker",worker)
        self.assertNotIn("veritas_live",worker)

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
        self.assertIn("outcome_evidence_trades",events[-1][1])
        self.assertIn("path_evidence_trades",events[-1][1])
        self.assertIn("stop_replay_ready",events[-1][1])
        self.assertIn("exit_replay_ready",events[-1][1])
        snap=app.snapshot()["learning_v2"]
        self.assertFalse(snap["automatic_production_promotion"])
        self.assertEqual(snap["registry"],registry)
        self.assertTrue(any(h["kind"]=="ENTRY_BLOCKER_RELAXATION" for h in snap["hypotheses"]))
        self.assertTrue(all(h["mode"]=="SHADOW_ONLY" for h in snap["hypotheses"]))
        publish.assert_called_once()
        self.assertEqual(publish.call_args.args[1],C.LEARNING_V2_SNAPSHOT_NAME)
        self.assertEqual(publish.call_args.args[2],V2.VERSION)

    def test_decision_projection_preserves_admission_block(self):
        source=inspect.getsource(C.ContinuousLearning.learning_v2_shadow)
        self.assertIn("CASE WHEN e.decision IN ('LONG','SHORT')",source)
        self.assertIn("d.payload->>'plan_eligible'",source)
        self.assertIn("AS admission_eligible",source)
        self.assertIn("AS final_gate_status",source)

    def test_decision_reader_accepts_legacy_nested_admission_fields(self):
        source=inspect.getsource(C.ContinuousLearning.learning_v2_shadow)
        self.assertIn("d.payload#>>'{trade_plan,eligible}'",source)
        self.assertIn("d.payload#>>'{execution_eligibility,eligible}'",source)
        self.assertIn("d.payload#>>'{trade_plan,reason}'",source)
        self.assertIn("d.payload#>>'{execution_eligibility,paper_execution_reason}'",source)
        self.assertIn("d.payload#>'{execution_eligibility,paper_source_blockers}'",source)

    def test_trade_research_uses_outcome_tier_without_relaxing_path_flag(self):
        source=inspect.getsource(C.ContinuousLearning.learning_v2_shadow)
        self.assertIn("outcome_learning_eligible",source)
        self.assertIn("AS outcome_evidence_eligible",source)
        self.assertIn("e.learning_eligible AS path_evidence_eligible",source)
        self.assertIn("AS stop_replay_ready",source)
        self.assertIn("AS exit_replay_ready",source)

    def test_trade_cohort_provenance_comes_from_original_trade(self):
        source=inspect.getsource(C.ContinuousLearning.learning_v2_shadow)
        self.assertIn("JOIN paper_trades t ON t.trade_id=e.trade_id",source)
        self.assertIn("t.payload#>>'{price_source_lock,key}'",source)
        self.assertIn("t.payload#>>'{price_source_lock,contract_id}'",source)
        self.assertIn("t.payload->>'strategy_policy_hash'",source)

    def test_decision_reader_materializes_one_latest_decision_set(self):
        source=inspect.getsource(C.ContinuousLearning.learning_v2_shadow)
        self.assertIn("decision_keys AS MATERIALIZED",source)
        self.assertIn("latest_decisions AS MATERIALIZED",source)
        self.assertIn("JOIN latest_decisions d ON d.entity_key=e.entity_key",source)
        self.assertNotIn("CROSS JOIN LATERAL",source)

    def test_periodic_job_is_registered_with_bounded_budget(self):
        app=C.ContinuousLearning(namespace(lambda: None))
        lane=app.lane
        self.assertEqual(C.LEARNING_V2_INPUT_LIMIT,128)
        self.assertIn("learning_v2_shadow",lane.callbacks)
        self.assertIn("learning_v2_replay",lane.callbacks)
        self.assertEqual(lane.options["learning_v2_shadow"]["max_seconds"],5)
        self.assertEqual(lane.options["learning_v2_replay"]["max_seconds"],5)
        self.assertEqual(lane.options["learning_v2_replay"]["interval_seconds"],180)
        self.assertEqual(lane.options["learning_v2_shadow"]["interval_seconds"],60)
        self.assertTrue(lane.options["learning_v2_shadow"]["lightweight"])

if __name__=="__main__":
    unittest.main()
