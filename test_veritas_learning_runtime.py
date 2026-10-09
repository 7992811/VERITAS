"""Learning integration contracts without starting a production service."""
import ast
import os
import json
import uuid
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import veritas_learning_bridge as BRIDGE
import veritas_learning_index as INDEX
import veritas_decision_signature as DS


def functions(*names, scope=None):
    tree = ast.parse(Path('veritas_intelligence.py').read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    scope = dict(scope or {})
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'learning_runtime', 'exec'), scope)
    return scope


class LearningRuntimeTests(unittest.TestCase):
    def test_bootstrap_progress_cannot_create_daily_baseline_or_score_history(self):
        connect = Mock(side_effect=AssertionError('uncomputed progress opened a database'))
        namespace = functions('learning_progress', '_v90_daily_intelligence_metrics', scope={
            'time': time, 'datetime': datetime, 'timedelta': timedelta, 'timezone': timezone,
            'ZoneInfo': ZoneInfo, 'ANALYTICS_CACHE_SECONDS': 60, 'VLI': INDEX,
            'pg_enabled': lambda: True, 'pg_connect': connect,
            '_learning_progress_state': {'status': 'NOT_STARTED'},
            '_v90_daily_intelligence_cache': {'at': 0., 'value': None}})
        progress = namespace['learning_progress']()
        self.assertEqual(progress['status'], 'BUILDING')
        self.assertEqual(progress['index_version'], INDEX.INDEX_VERSION)
        self.assertNotIn('mode', progress)
        self.assertEqual(namespace['_v90_daily_intelligence_metrics'](),
                         {'status': 'LEARNING_UNAVAILABLE', 'trend': 'BUILDING'})
        connect.assert_not_called()
        self.assertIsNone(namespace['_v90_daily_intelligence_cache']['value'])

    def test_truncated_compiler_reply_has_an_explicit_reason_and_cannot_parse_as_success(self):
        namespace=functions('_response_text',scope={})
        for reason in ('max_output_tokens','max_tokens'):
            with self.assertRaisesRegex(ValueError,'COMPILER_OUTPUT_LIMIT'):
                namespace['_response_text']({'status':'incomplete','incomplete_details':{'reason':reason},
                                             'output':[{'content':[{'type':'output_text','text':'{}'}]}]})

    def test_read_marks_existing_queue_without_starting_thread(self):
        lane = Mock()
        namespace = functions('learning_progress', scope={
            'time': time, 'ANALYTICS_CACHE_SECONDS': 60, 'VLI': INDEX,
            '_v90_background_maintenance': lane,
            '_learning_progress_state': {'status': 'ERROR'},
            'threading': Mock(Thread=Mock(side_effect=AssertionError('HTTP spawned worker')))})
        fn = namespace['learning_progress']
        fn._cache = (time.time()-100, {'status': 'MEASURABLE', 'index_vs_start': 101.2})
        result = fn()
        self.assertEqual(result['index_vs_start'], 101.2)
        self.assertTrue(result['background_refresh'])
        lane.request.assert_called_once_with('learning_progress')

    def test_failed_compute_cannot_publish_success_or_replace_last_good(self):
        fn = lambda: None
        old = (123., {'status': 'MEASURABLE', 'index_vs_start': 102.})
        fn._cache = old
        state = {}
        namespace = functions('_learning_progress_refresh_sync', scope={
            'learning_progress': fn, '_learning_progress_refresh_lock': threading.Lock(),
            '_learning_progress_state': state, '_learning_progress_v2_compute': lambda: {'status': 'error', 'error': 'QueryCanceled'},
            'time': time, 'now': lambda: datetime.now(timezone.utc).isoformat(), 'emit': Mock()})
        result = namespace['_learning_progress_refresh_sync']()
        self.assertEqual(state['status'], 'ERROR')
        self.assertIs(fn._cache, old)
        self.assertEqual(result['refresh_status'], 'ERROR')
        self.assertEqual(result['index_vs_start'], 102.)

    def test_ledger_compaction_keeps_new_source_seal_without_bar_history(self):
        namespace = functions('_v90r37_features_compact', '_v90r37_compact_decision_payload', scope={
            'VLB': BRIDGE, '_v90_small_dict': lambda d, keys: {k: (d or {})[k] for k in keys if k in (d or {})}})
        payload = {'asset': 'BTC', 'horizon': '1m', 'created_at': '2026-10-07T20:00:01+00:00',
                   'decision': 'LONG', 'research_decision': 'LONG', 'regime': 'TREND',
                   'calibration': {'probability_correct': .56}, 'gates': {'source': True},
                   '_execution_quote': BRIDGE.compact_quote({'asset': 'BTC', 'price': 100.,
                       'observed_at': '2026-10-07T20:00:00+00:00', 'source_names': {'primary': 'Coinbase'},
                       'source_gate_pass': True, 'market_open': True, 'history': [0]*10000}),
                   'features': {'price': 100., 'history': [0]*10000}}
        result = namespace['_v90r37_compact_decision_payload'](payload)
        evidence = result['learning_provenance']
        self.assertTrue(evidence['eligible'])
        self.assertEqual(evidence['base_probability'], .56)
        self.assertEqual(evidence['quote']['source_identity']['key'], 'COINBASE:BTC-USD')
        self.assertNotIn('history', str(result))
        sealed = deepcopy(evidence)
        digest = sealed.pop('evidence_hash')
        self.assertEqual(BRIDGE.digest(sealed), digest)

    def test_ledger_compaction_persists_admission_evidence(self):
        namespace = functions('_v90r37_features_compact', '_v90r37_compact_decision_payload', scope={
            'VLB': BRIDGE, '_v90_small_dict': lambda d, keys: {k: (d or {})[k] for k in keys if k in (d or {})}})
        payload = {
            'asset':'NQ','horizon':'5m','created_at':'2026-10-09T07:00:01+00:00',
            'decision':'LONG','research_decision':'LONG','regime':'TREND',
            'gates':{'source':True},
            '_execution_quote': BRIDGE.compact_quote({
                'asset':'NQ','price':25000.,'observed_at':'2026-10-09T07:00:00+00:00',
                'source_names':{'primary':'ProFinance'},'source_gate_pass':True,'market_open':True}),
            'trade_plan':{'eligible':False,'reason':'IMPULSE_ALREADY_PASSED'},
            'execution_eligibility':{
                'eligible':False,'reason':'TIMING_NOT_READY',
                'paper_eligible':False,'paper_execution_reason':'WAIT_RETEST',
                'paper_source_blockers':['PRIMARY_SOURCE_GATE_FAILED']}}
        result=namespace['_v90r37_compact_decision_payload'](payload)
        self.assertIs(result['plan_eligible'],False)
        self.assertEqual(result['plan_reason'],'IMPULSE_ALREADY_PASSED')
        self.assertIs(result['trade_entry_eligible'],False)
        self.assertEqual(result['trade_entry_reason'],'TIMING_NOT_READY')
        self.assertEqual(result['paper_execution_reason'],'WAIT_RETEST')
        self.assertEqual(result['final_gate_status'],'BLOCK')
        self.assertEqual(result['final_gate_blockers'],['PRIMARY_SOURCE_GATE_FAILED'])
        self.assertEqual(result['trade_plan']['eligible'],False)
        self.assertEqual(result['execution_eligibility']['eligible'],False)

    def test_decision_signature_changes_on_execution_admission_state(self):
        base={
            'research_decision':'LONG','decision':'LONG','regime':'TREND',
            'trade_plan':{'eligible':True,'reason':'ok','entry_event_id':'E1'},
            'execution_eligibility':{
                'eligible':True,'reason':'OK','paper_eligible':True,
                'paper_execution_reason':'OK','paper_source_blockers':[]},
            'gates':{'source':True,'time':True}}
        a=DS.decision_signature('decision',base)
        variants=[]
        for patch in (
            {'eligible':False},
            {'reason':'TIMING_NOT_READY'},
            {'paper_eligible':False},
            {'paper_execution_reason':'WAIT_RETEST'},
            {'paper_source_blockers':['PRIMARY_SOURCE_GATE_FAILED']},
        ):
            row=deepcopy(base)
            row['execution_eligibility'].update(patch)
            variants.append(DS.decision_signature('decision',row))
        self.assertTrue(all(x!=a for x in variants))
        self.assertEqual(len(set(variants)),len(variants))

    def test_decision_episode_materialization_contains_admission_and_exact_source(self):
        source=Path('veritas_intelligence.py').read_text()
        materialize=source[source.index('def _v90_materialize_decision_episode'):
                           source.index('def _v90_backfill_decision_episodes')]
        for field in ('decision_id','setup_family','policy_hash','source_key','contract_id',
                      'candidate_direction','admission_eligible','final_gate_status',
                      'final_gate_blockers','plan_reason','trade_entry_reason',
                      'execution_reason','paper_execution_reason'):
            self.assertIn(field,materialize)
        self.assertIn("{learning_provenance,quote,source_identity,key}",materialize)
        self.assertIn("{learning_provenance,quote,source_identity,contract_id}",materialize)
        self.assertNotIn("{learning_provenance,source_identity,key}",materialize)

    def test_decision_episode_schema_has_materialized_learning_columns_and_asset_index(self):
        source=Path('veritas_intelligence.py').read_text()
        schema=source[source.index('CREATE TABLE IF NOT EXISTS v90_decision_episodes'):
                      source.index('CREATE TABLE IF NOT EXISTS knowledge_sources')]
        for field in ('decision_id BIGINT','setup_family TEXT','policy_hash TEXT',
                      'source_key TEXT','contract_id TEXT','candidate_direction TEXT',
                      'admission_eligible BOOLEAN','final_gate_status TEXT',
                      'final_gate_blockers JSONB'):
            self.assertIn(field,schema)
        self.assertIn('idx_v90_decision_episodes_asset_ts',schema)

    def test_memory_trim_preserves_small_last_good_progress(self):
        tree = ast.parse(Path('veritas_intelligence.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_v90_prune_low_priority_caches')
        function_names = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        self.assertNotIn('learning_progress', function_names)


if __name__ == '__main__':
    unittest.main()


@unittest.skipUnless(os.getenv("VERITAS_QUALITY_TEST_DSN"), "isolated PostgreSQL test database not configured")
class DecisionEpisodeMaterializationSQLTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        self.psycopg,self.dict_row=psycopg,dict_row
        self.dsn=os.environ["VERITAS_QUALITY_TEST_DSN"]
        self.schema="episode_materialize_"+uuid.uuid4().hex
        with psycopg.connect(self.dsn,row_factory=dict_row) as conn:
            if conn.execute("select current_database() AS n").fetchone()["n"]!="veritas_quality_test":
                raise RuntimeError("Refusing decision episode test outside veritas_quality_test")
            conn.execute(f'CREATE SCHEMA "{self.schema}"')
            conn.execute(f'SET search_path TO "{self.schema}"')
            conn.execute("""CREATE TABLE ledger_events(
                id bigserial primary key,event_key text unique not null,entity_key text not null,
                event_type text not null,event_ts timestamptz not null,asset text,horizon text,
                payload jsonb not null,model_version text not null)""")
            conn.execute("""CREATE TABLE v90_decision_episodes(
                entity_key text primary key,decision_id bigint,decision_ts timestamptz not null,
                outcome_ts timestamptz not null,asset text not null,horizon text not null,
                regime text not null,decision text not null,forward_return float8 not null,
                mfe float8,mae float8,model_version text,knowledge_shadow_matches jsonb not null default '[]',
                setup_family text,policy_hash text,source_key text,contract_id text,candidate_direction text,
                admission_eligible boolean,final_gate_status text,final_gate_blockers jsonb not null default '[]',
                plan_reason text,trade_entry_reason text,execution_reason text,paper_execution_reason text,
                updated_at timestamptz not null default now())""")
        @contextmanager
        def connect():
            with psycopg.connect(self.dsn,row_factory=dict_row) as conn:
                conn.execute(f'SET search_path TO "{self.schema}"')
                yield conn
        self.connect=connect

    def tearDown(self):
        with self.psycopg.connect(self.dsn) as conn:
            conn.execute(f'DROP SCHEMA "{self.schema}" CASCADE')

    def test_outcome_materializes_compact_admission_provenance(self):
        payload={
          "regime":"TREND","research_decision":"LONG","setup_family":"BREAKOUT",
          "learning_provenance":{
            "policy_hash":"POLICY1",
            "quote":{"source_identity":{"key":"TEST:NQ","contract_id":"NQZ6"}}},
          "plan_eligible":False,"plan_reason":"TIMING_NOT_READY",
          "trade_entry_eligible":False,"trade_entry_reason":"TIMING_NOT_READY",
          "paper_execution_reason":"WAIT_RETEST","final_gate_status":"BLOCK",
          "final_gate_blockers":["TIMING_NOT_READY"],
          "knowledge_shadow_matches":[]}
        with self.connect() as conn:
            row=conn.execute("""INSERT INTO ledger_events(
                event_key,entity_key,event_type,event_ts,asset,horizon,payload,model_version)
                VALUES('decision:E1','E1','decision','2026-10-09T08:00:00Z','NQ','5m',%s::jsonb,'M1')
                RETURNING id""",(json.dumps(payload),)).fetchone()
            decision_id=row["id"]
        ns=functions('_v90_materialize_decision_episode',scope={
            'pg_enabled':lambda:True,'pg_connect':self.connect,
            'now':lambda:'2026-10-09T08:05:00+00:00','emit':Mock()})
        self.assertTrue(ns['_v90_materialize_decision_episode'](
            'E1',{"forward_return":.01,"mfe":.015,"mae":-.003},
            '2026-10-09T08:05:00+00:00'))
        with self.connect() as conn:
            row=conn.execute("SELECT * FROM v90_decision_episodes WHERE entity_key='E1'").fetchone()
        self.assertEqual(row["decision_id"],decision_id)
        self.assertEqual(row["decision"],"LONG")
        self.assertEqual(row["candidate_direction"],"LONG")
        self.assertEqual(row["setup_family"],"BREAKOUT")
        self.assertEqual(row["policy_hash"],"POLICY1")
        self.assertEqual(row["source_key"],"TEST:NQ")
        self.assertEqual(row["contract_id"],"NQZ6")
        self.assertIs(row["admission_eligible"],False)
        self.assertEqual(row["final_gate_status"],"BLOCK")
        self.assertEqual(row["final_gate_blockers"],["TIMING_NOT_READY"])
        self.assertEqual(row["plan_reason"],"TIMING_NOT_READY")
        self.assertEqual(row["paper_execution_reason"],"WAIT_RETEST")
