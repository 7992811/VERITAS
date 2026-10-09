from datetime import datetime, timedelta, timezone
import inspect
import json
import os
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
import unittest

import veritas_learning_v2_registry as REG

import veritas_learning_v2_replay_eval as E

T=datetime(2026,10,9,10,tzinfo=timezone.utc)
IDENTITY={"key":"TEST:PX","contract_id":"C1","asset":"NQ","primary_source":"Test"}


def cached(asset,timeframe,identity,now=None,limit=500):
    self_source=identity or {}
    if self_source.get("key")!=IDENTITY["key"] or str(self_source.get("contract_id") or "")!="C1":
        return []
    if timeframe!="5m": return []
    rows=[]
    start=T
    for i in range(12):
        opened=start+timedelta(minutes=5*i)
        rows.append({"opened_at":opened.isoformat(),"closed_at":(opened+timedelta(minutes=5)).isoformat(),
                     "open":100.0,"high":101.0 if i<2 else 102.5 if i==2 else 104.5,
                     "low":99.0,"close":100.5 if i<2 else 102.2 if i==2 else 104.0,
                     "source_key":IDENTITY["key"],"contract_id":"C1"})
    return rows


def row(runner=True):
    event={"direction":"LONG","source_identity":IDENTITY,"stop_anchor":98.0,"atr":2.0,
           "stop_price":97.7,"target_price":102.0,
           "policy":{"stop_buffer_atr":.15,"target_one_fraction":.50}}
    if runner:event["runner_target_price"]=104.0
    return {"trade_id":"T1","opened_at":T.isoformat(),"closed_at":(T+timedelta(hours=1)).isoformat(),
            "asset":"NQ","direction":"LONG","horizon":"5m","regime":"TREND",
            "avg_entry_price":100.0,"entry_event_snapshot":event,
            "entry_execution_model":{"fill_price":100.0},
            "price_source_lock":IDENTITY,"entry_execution_source_identity":IDENTITY,
            "strategy_policy_hash":"p","initial_stop_price":"97.7","entry_atr":"2.0",
            "entry_order_count":1}


class ReplayEvaluatorTests(unittest.TestCase):
    def stop_candidate(self,buffer=.20):
        return {"candidate_id":"CSTOP","kind":"STOP_GEOMETRY",
                "scope":{"asset":"NQ","horizon":"5m","regime":"TREND",
                         "source_key":IDENTITY["key"],"contract_id":"C1","policy_hash":"p"},
                "proposal":{"stop_buffer_atr":buffer,"baseline_stop_buffer_atr":.15}}

    def exit_candidate(self,fraction=.25):
        return {"candidate_id":"CEXIT","kind":"EXIT_CAPTURE",
                "scope":{"asset":"NQ","horizon":"5m","regime":"TREND",
                         "source_key":IDENTITY["key"],"contract_id":"C1","policy_hash":"p"},
                "proposal":{"first_target_fraction":fraction,"baseline_first_target_fraction":.5}}

    def test_trade_query_uses_outcome_evidence_tier(self):
        source=inspect.getsource(E._trade_rows)
        self.assertIn("outcome_learning_eligible",source)
        self.assertIn("outcome_diagnostics_version",source)
        self.assertIn("outcome_evidence_hash",source)
        self.assertNotIn("WHERE e.learning_eligible=TRUE",source)

    def test_replay_query_deduplicates_market_ideas(self):
        source=inspect.getsource(E._trade_rows)
        self.assertIn("independent_episode_key",source)
        self.assertIn("ROW_NUMBER() OVER",source)
        self.assertIn("idea_rank=1",source)

    def test_trade_query_requires_entry_after_registration(self):
        source=inspect.getsource(E._trade_rows)
        self.assertIn("t.opened_at>%s",source)
        self.assertIn("e.closed_at>%s",source)

    def test_gap_after_entry_defers_replay(self):
        def late_path(asset,timeframe,identity,now=None,limit=500):
            if timeframe!="5m": return []
            opened=T+timedelta(minutes=30)
            return [{"opened_at":opened.isoformat(),"closed_at":(opened+timedelta(minutes=5)).isoformat(),
                     "open":100,"high":101,"low":99,"close":100.5,
                     "source_key":"TEST:PX","contract_id":"C1"}]
        result=E.evaluate_trade(self.stop_candidate(),row(),late_path)
        self.assertEqual(result["status"],"DEFERRED")
        self.assertEqual(result["reason"],"CACHED_PATH_COVERAGE_INCOMPLETE")

    def test_partial_entry_bar_touching_barrier_is_ambiguous(self):
        def path(asset,timeframe,identity,now=None,limit=500):
            if timeframe!="5m": return []
            rows=[{"opened_at":(T-timedelta(minutes=2)).isoformat(),
                   "closed_at":(T+timedelta(minutes=3)).isoformat(),
                   "open":100.0,"high":103.0,"low":97.0,"close":100.5,
                   "source_key":"TEST:PX","contract_id":"C1"}]
            for i in range(11):
                opened=T+timedelta(minutes=3+5*i)
                rows.append({"opened_at":opened.isoformat(),
                             "closed_at":(opened+timedelta(minutes=5)).isoformat(),
                             "open":100.5,"high":101.0,"low":99.5,"close":100.7,
                             "source_key":"TEST:PX","contract_id":"C1"})
            return rows
        result=E.evaluate_trade(self.stop_candidate(),row(),path)
        self.assertEqual(result["status"],"AMBIGUOUS")
        self.assertEqual(result["reason"],"ENTRY_BAR_BARRIER_ORDER_UNKNOWN")

    def test_stop_candidate_replays_same_path_and_cost_model(self):
        result=E.evaluate_trade(self.stop_candidate(),row(),cached)
        self.assertEqual(result["status"],"COMPARABLE")
        self.assertIsNotNone(result["baseline_net"])
        self.assertIsNotNone(result["candidate_net"])
        self.assertEqual(result["payload"]["source_identity"]["contract_id"],"C1")
        self.assertFalse(result["payload"]["production_influence"])

    def test_exit_candidate_uses_explicit_partial_runner_policy(self):
        result=E.evaluate_trade(self.exit_candidate(.25),row(),cached)
        self.assertEqual(result["status"],"COMPARABLE")
        self.assertEqual(result["payload"]["geometry"]["baseline_first_target_fraction"],.5)
        self.assertEqual(result["payload"]["geometry"]["candidate_first_target_fraction"],.25)

    def test_stop_baseline_policy_mismatch_is_invalid(self):
        x=row();x["entry_event_snapshot"]["policy"]["stop_buffer_atr"]=.25
        result=E.evaluate_trade(self.stop_candidate(),x,cached)
        self.assertEqual(result["status"],"INVALID")
        self.assertEqual(result["reason"],"BASELINE_STOP_POLICY_MISMATCH")

    def test_exit_baseline_policy_mismatch_is_invalid(self):
        x=row();x["entry_event_snapshot"]["policy"]["target_one_fraction"]=.25
        result=E.evaluate_trade(self.exit_candidate(),x,cached)
        self.assertEqual(result["status"],"INVALID")
        self.assertEqual(result["reason"],"BASELINE_EXIT_POLICY_MISMATCH")

    def test_exit_without_observed_runner_target_is_invalid(self):
        result=E.evaluate_trade(self.exit_candidate(),row(runner=False),cached)
        self.assertEqual(result["status"],"INVALID")
        self.assertEqual(result["reason"],"EXIT_REPLAY_FIELDS_MISSING")

    def test_foreign_contract_is_invalid_before_path_replay(self):
        x=row();x["price_source_lock"]=dict(IDENTITY,contract_id="OTHER")
        result=E.evaluate_trade(self.stop_candidate(),x,cached)
        self.assertEqual(result["status"],"INVALID")
        self.assertEqual(result["reason"],"SOURCE_OR_CONTRACT_SCOPE_MISMATCH")

    def test_missing_cached_history_is_deferred_not_negative_evidence(self):
        result=E.evaluate_trade(self.stop_candidate(),row(),lambda *a,**k:[])
        self.assertEqual(result["status"],"DEFERRED")
        self.assertEqual(result["reason"],"CACHED_PATH_COVERAGE_INCOMPLETE")

if __name__=="__main__":
    unittest.main()


DSN=os.getenv("VERITAS_QUALITY_TEST_DSN","")

@unittest.skipUnless(DSN,"isolated PostgreSQL test database not configured")
class ReplayEvaluatorSQLTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        self.psycopg=psycopg; self.dict_row=dict_row
        self.schema="replay_v2_"+uuid.uuid4().hex
        with psycopg.connect(DSN,row_factory=dict_row) as conn:
            if conn.execute("select current_database() AS n").fetchone()["n"]!="veritas_quality_test":
                raise RuntimeError("Refusing replay test outside veritas_quality_test")
            conn.execute(f'CREATE SCHEMA "{self.schema}"')
            conn.execute(f'SET search_path TO "{self.schema}"')
            REG.ensure_schema(conn); E.ensure_schema(conn)
            conn.execute("""CREATE TABLE v90_learning_episodes(
                trade_id text PRIMARY KEY,closed_at timestamptz,asset text,direction text,horizon text,
                regime text,setup_family text,learning_eligible boolean,primary_attribution text,payload jsonb)""")
            conn.execute("""CREATE TABLE paper_trades(
                trade_id text PRIMARY KEY,opened_at timestamptz,avg_entry_price float8,status text,payload jsonb)""")
            conn.execute("""CREATE TABLE paper_orders(trade_id text,side text)""")
            registered=T-timedelta(days=1)
            scope={"asset":"NQ","horizon":"5m","regime":"TREND","source_key":IDENTITY["key"],
                   "contract_id":"C1","policy_hash":"p"}
            contract={"version":"LEARNING_V2_SHADOW_1","kind":"STOP_GEOMETRY","scope":scope,
                      "proposal":{"stop_buffer_atr":.20,"baseline_stop_buffer_atr":.15}}
            conn.execute("""INSERT INTO learning_v2_registry(
                candidate_id,version,kind,asset,horizon,regime,source_key,contract_id,policy_hash,
                registered_at,decision_cutoff_id,status,contract,training_evidence,prospective)
                VALUES('CSTOP',%s,'STOP_GEOMETRY','NQ','5m','TREND',%s,'C1','p',
                       %s,0,'AWAIT_REPLAY',%s::jsonb,'{}'::jsonb,'{}'::jsonb)""",
                (REG.VERSION,IDENTITY["key"],registered,json.dumps(contract)))
            event={"direction":"LONG","source_identity":IDENTITY,"stop_anchor":98.0,"atr":2.0,
                   "stop_price":97.7,"target_price":102.0,
                   "policy":{"stop_buffer_atr":.15,"target_one_fraction":.50}}
            payload={"entry_event_snapshot":event,"entry_execution_model":{"fill_price":100.0},
                     "price_source_lock":IDENTITY,"entry_execution_source_identity":IDENTITY,
                     "strategy_policy_hash":"p","initial_stop_price":97.7,"entry_atr":2.0}
            conn.execute("""INSERT INTO paper_trades VALUES(
                'TSQL',%s,100,'CLOSED',%s::jsonb)""",(T,json.dumps(payload)))
            episode_payload={"outcome_learning_eligible":True,
                             "outcome_diagnostics_version":E.DIAGNOSTICS.VERSION,
                             "outcome_evidence_hash":"verified-hash",
                             "independent_episode_key":"IDEA-1"}
            conn.execute("""INSERT INTO v90_learning_episodes VALUES(
                'TSQL',%s,'NQ','LONG','5m','TREND','BREAKOUT',FALSE,'OK',%s::jsonb)""",
                (T+timedelta(hours=1),json.dumps(episode_payload)))
            conn.execute("INSERT INTO paper_orders VALUES('TSQL','BUY')")

    @contextmanager
    def connect(self):
        with self.psycopg.connect(DSN,row_factory=self.dict_row) as conn:
            conn.execute(f'SET search_path TO "{self.schema}"')
            yield conn

    def tearDown(self):
        with self.psycopg.connect(DSN) as conn:
            conn.execute(f'DROP SCHEMA "{self.schema}" CASCADE')

    def test_receipt_and_registry_update_are_idempotent(self):
        result=E.process(self.connect,cached,now=T+timedelta(hours=2),
                         context=SimpleNamespace(check=lambda:None))
        self.assertEqual(result["status"],"OK")
        self.assertEqual(result["receipts_written"],1)
        self.assertEqual(result["registry_status"],"REPLAY_BUILDING")
        with self.connect() as conn:
            receipt=conn.execute("SELECT status,baseline_net,candidate_net FROM learning_v2_replay_receipts").fetchone()
            registry=conn.execute("SELECT status,prospective FROM learning_v2_registry WHERE candidate_id='CSTOP'").fetchone()
        self.assertEqual(receipt["status"],"COMPARABLE")
        self.assertEqual(registry["prospective"]["replay"]["n"],1)
        again=E.process(self.connect,cached,now=T+timedelta(hours=3),
                        context=SimpleNamespace(check=lambda:None))
        self.assertEqual(again["status"],"OK")
        self.assertEqual(again["receipts_written"],0)
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM learning_v2_replay_receipts").fetchone()["n"],1)
            self.assertEqual(conn.execute("SELECT prospective#>>'{replay,n}' AS n FROM learning_v2_registry WHERE candidate_id='CSTOP'").fetchone()["n"],"1")
