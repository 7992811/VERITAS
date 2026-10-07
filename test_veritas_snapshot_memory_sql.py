"""PostgreSQL parity and lifetime tests for archival performance snapshots.

The legacy reducer is frozen from d030a55. Only its isolated AST definition runs;
service startup, external market calls and production database access never run.
SQL writes require the explicit veritas_quality_test database and unique schema.
"""
import ast
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
import uuid

import veritas_learning_memory as LM
from test_veritas_trend_memory_sql import (
    FrozenDatetime, NOW, ReadTrace, TracedConnection, TracedCursor, tail as _legacy_tail,
)

DSN = os.getenv("VERITAS_QUALITY_TEST_DSN", "")
RUNTIME = Path(__file__).with_name("veritas_intelligence.py")

# Frozen v9 projected SQL: independent of JsonbProjection and current builders.
_V9_AMI_PROJECTED_SQL = r'''
      SELECT d.event_ts,d.asset,d.horizon,CASE WHEN jsonb_typeof(d.payload)='object' AND d.payload<>'{}'::jsonb THEN jsonb_build_object('research_decision',d.payload->'research_decision','decision',d.payload->'decision','regime',d.payload->'regime','agents',CASE WHEN jsonb_typeof(d.payload->'agents')='array' THEN
      (SELECT COALESCE(jsonb_agg(
         CASE WHEN jsonb_typeof(a.value)='object' THEN
           jsonb_build_object('direction',a.value->'direction','confidence',a.value->'confidence')
         ELSE a.value END ORDER BY a.ordinality),'[]'::jsonb)
       FROM jsonb_array_elements(d.payload->'agents') WITH ORDINALITY AS a(value,ordinality))
      ELSE d.payload->'agents' END,'knowledge_cio_adjustment',CASE WHEN jsonb_typeof(d.payload->'knowledge_cio_adjustment')='object' AND d.payload->'knowledge_cio_adjustment'<>'{}'::jsonb THEN jsonb_build_object('score_with_experience',d.payload->'knowledge_cio_adjustment'->'score_with_experience','score',d.payload->'knowledge_cio_adjustment'->'score') ELSE d.payload->'knowledge_cio_adjustment' END,'knowledge_shadow_matches',(d.payload->'knowledge_shadow_matches') NOT IN
      ('null'::jsonb,'false'::jsonb,'0'::jsonb,'""'::jsonb,'[]'::jsonb,'{}'::jsonb)) ELSE d.payload END AS dp,CASE WHEN jsonb_typeof(o.payload)='object' AND o.payload<>'{}'::jsonb THEN jsonb_build_object('forward_return',o.payload->'forward_return') ELSE o.payload END AS op
      FROM ledger_events d
      JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'
      WHERE d.event_type='decision' AND o.payload ? 'forward_return'
      ORDER BY d.event_ts DESC
      LIMIT 2200
    '''

_LEGACY_SOURCE = r'''def pg_live_performance():
    if not pg_enabled():
        return []
    with pg_connect() as c:
        rows=c.execute("""
          SELECT d.asset,d.horizon,d.payload decision,o.payload outcome
          FROM ledger_events d JOIN ledger_events o
            ON o.entity_key=d.entity_key AND o.event_type='outcome'
          WHERE d.event_type='decision'
        """).fetchall()
    b={}
    for r in rows:
        d=r['decision'] if isinstance(r['decision'],dict) else json.loads(r['decision'])
        o=r['outcome'] if isinstance(r['outcome'],dict) else json.loads(r['outcome'])
        dec=d.get('decision'); fr=o.get('forward_return')
        if fr is None: continue
        key=(r['asset'],r['horizon'],dec)
        z=b.setdefault(key,{'n':0,'hits':0,'signed':[],'raw':[],'mfe':[],'mae':[]})
        fr=float(fr); z['n']+=1; z['raw'].append(fr)
        if o.get('mfe') is not None: z['mfe'].append(float(o['mfe']))
        if o.get('mae') is not None: z['mae'].append(float(o['mae']))
        if dec in ('LONG','SHORT'):
            sr=fr if dec=='LONG' else -fr
            z['signed'].append(sr); z['hits'] += 1 if sr>0 else 0
    out=[]
    for (asset,horizon,dec),z in sorted(b.items()):
        dn=len(z['signed'])
        out.append({'asset':asset,'horizon':horizon,'decision':dec,'n':z['n'],
                    'directional_n':dn,'hit_rate':z['hits']/dn if dn else None,
                    'avg_signed_return':sum(z['signed'])/dn if dn else None,
                    'avg_raw_return':sum(z['raw'])/len(z['raw']) if z['raw'] else None,
                    'avg_mfe':sum(z['mfe'])/len(z['mfe']) if z['mfe'] else None,
                    'avg_mae':sum(z['mae'])/len(z['mae']) if z['mae'] else None})
    return out'''


_LEGACY_AGENT_SOURCE = r'''def pg_agent_performance():
    """Durable agent learning with regime conditioning and exponential time decay."""
    if not pg_enabled():
        return performance_rows()
    with pg_connect() as c:
        rows=c.execute("""WITH recent_decisions AS (
                            SELECT entity_key,event_ts,asset,horizon,payload
                            FROM ledger_events WHERE event_type='decision'
                            ORDER BY event_ts DESC LIMIT %s
                          )
                          SELECT d.asset,d.horizon,d.event_ts AS decision_ts,
                                 d.payload AS decision_payload,o.event_ts AS outcome_ts,o.payload AS outcome_payload
                          FROM recent_decisions d
                          JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'
                          ORDER BY d.event_ts ASC""",(LIVE_LEARNING_MAX_EPISODES,)).fetchall()
    buckets={}
    now_dt=datetime.now(timezone.utc)
    half=max(1.0,AGENT_DECAY_HALF_LIFE_DAYS)
    for r in rows:
        dp=r['decision_payload'] if isinstance(r['decision_payload'],dict) else json.loads(r['decision_payload'])
        op=r['outcome_payload'] if isinstance(r['outcome_payload'],dict) else json.loads(r['outcome_payload'])
        fr=op.get('forward_return')
        if fr is None:
            continue
        ts=r.get('outcome_ts') or r.get('decision_ts')
        if isinstance(ts,str):
            ts=datetime.fromisoformat(ts.replace('Z','+00:00'))
        if ts is None:
            ts=now_dt
        if ts.tzinfo is None:
            ts=ts.replace(tzinfo=timezone.utc)
        age_days=max(0.0,(now_dt-ts).total_seconds()/86400.0)
        w=math.exp(-math.log(2.0)*age_days/half)
        fr=float(fr); regime=dp.get('regime') or '*'
        for av in dp.get('agents') or []:
            direction=av.get('direction')
            if direction not in ('LONG','SHORT'):
                continue
            sr=fr if direction=='LONG' else -fr
            hit=1.0 if sr>0 else 0.0
            for rg in (regime,'*'):
                key=(av.get('agent'),r['asset'],r['horizon'],rg)
                b=buckets.setdefault(key,{'n':0,'hits':0,'signed':[],'w':0.0,'wh':0.0,'wr':0.0,
                                          'recent_n':0,'recent_hits':0,'recent_ret':0.0,
                                          'prior_n':0,'prior_hits':0,'prior_ret':0.0})
                b['n']+=1; b['hits']+=int(hit); b['signed'].append(sr)
                b['w']+=w; b['wh']+=w*hit; b['wr']+=w*sr
                if age_days<=30:
                    b['recent_n']+=1; b['recent_hits']+=int(hit); b['recent_ret']+=sr
                else:
                    b['prior_n']+=1; b['prior_hits']+=int(hit); b['prior_ret']+=sr
    out=[]
    for (a,asset,h,rg),b in sorted(buckets.items()):
        n=b['n']; hr=b['hits']/n if n else None
        recent_hr=b['recent_hits']/b['recent_n'] if b['recent_n'] else None
        prior_hr=b['prior_hits']/b['prior_n'] if b['prior_n'] else None
        out.append({'agent':a,'asset':asset,'horizon':h,'regime':rg,'n':n,
                    'hit_rate':hr,'bayes_hit_rate':(b['hits']+5)/(n+10) if n else None,
                    'avg_signed_return':sum(b['signed'])/n if n else None,
                    'decayed_hit_rate':b['wh']/b['w'] if b['w'] else None,
                    'decayed_avg_signed_return':b['wr']/b['w'] if b['w'] else None,
                    'effective_n':b['w'],
                    'recent_n':b['recent_n'],'recent_hit_rate':recent_hr,
                    'recent_avg_signed_return':b['recent_ret']/b['recent_n'] if b['recent_n'] else None,
                    'prior_n':b['prior_n'],'prior_hit_rate':prior_hr,
                    'prior_avg_signed_return':b['prior_ret']/b['prior_n'] if b['prior_n'] else None})
    return out'''


_LEGACY_DRIFT_SOURCE = r'''def model_drift_status():
    if not pg_enabled():
        return {'rules':[],'agents':[],'status':'unavailable'}
    rules=[]
    with pg_connect() as c:
        rr=c.execute("""SELECT rule_id,asset,horizon,n,ew_hit_rate,ew_avg_signed_return,
                               recent_n,recent_hit_rate,recent_avg_signed_return,
                               prior_n,prior_hit_rate,prior_avg_signed_return,decay_ratio
                        FROM knowledge_rule_decay_stats
                        WHERE sample='OOS' AND n>=20
                        ORDER BY recent_n DESC""").fetchall()
    for r in rr:
        x=dict(r); rn=int(x.get('recent_n') or 0); pn=int(x.get('prior_n') or 0)
        state='INSUFFICIENT'
        if rn>=20 and pn>=20:
            rar=float(x.get('recent_avg_signed_return') or 0); par=float(x.get('prior_avg_signed_return') or 0)
            rhr=float(x.get('recent_hit_rate') or 0.5); phr=float(x.get('prior_hit_rate') or 0.5)
            if rar<0 and par>0:
                state='DECAYING'
            elif rhr<phr-0.08 or rar<par-0.01:
                state='WEAKENING'
            elif rar>0 and rhr>=phr-0.03:
                state='STABLE'
            else:
                state='MIXED'
        x['drift_state']=state; rules.append(x)
    agents=[]
    for x in pg_agent_performance():
        rn=int(x.get('recent_n') or 0); pn=int(x.get('prior_n') or 0)
        if x.get('regime')!='*': continue
        state='INSUFFICIENT'
        if rn>=DRIFT_MIN_N and pn>=DRIFT_MIN_N:
            rar=float(x.get('recent_avg_signed_return') or 0); par=float(x.get('prior_avg_signed_return') or 0)
            rhr=float(x.get('recent_hit_rate') or 0.5); phr=float(x.get('prior_hit_rate') or 0.5)
            if rar<0 and par>0: state='DECAYING'
            elif rhr<phr-0.08 or rar<par-0.01: state='WEAKENING'
            elif rar>0 and rhr>=phr-0.03: state='STABLE'
            else: state='MIXED'
        y=dict(x); y['drift_state']=state; agents.append(y)
    bad=sum(1 for x in rules if x['drift_state'] in ('DECAYING','WEAKENING'))
    return {'status':'WARN' if bad else 'OK','rule_drift_count':bad,'rules':rules[:100],'agents':agents[:100]}'''


def load_drift_reader(connect, *, legacy=False, agents=lambda: [], enabled=True):
    tree = ast.parse(_LEGACY_DRIFT_SOURCE if legacy else RUNTIME.read_text(encoding="utf-8"))
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "model_drift_status")
    namespace = {"pg_enabled":lambda:enabled, "pg_connect":connect,
                 "pg_agent_performance":agents, "DRIFT_MIN_N":20}
    exec(compile(ast.Module(body=[deepcopy(node)], type_ignores=[]), str(RUNTIME), "exec"), namespace)
    return namespace["model_drift_status"]


def drift_rule_fixture():
    # All bad rules sort beyond the 100-row display cap. Distinct recent_n
    # values make comparison independent of unspecified PostgreSQL tie order.
    rows = []
    for i in range(150):
        rows.append(dict(rule_id=f"rule-{i:03d}", asset="CNYRUBF", horizon="1h", n=600,
            ew_hit_rate=.625, ew_avg_signed_return=.0125, recent_n=500-i,
            recent_hit_rate=.625 if i<100 or i%2==0 else .4,
            recent_avg_signed_return=.0125 if i<100 or i%2 else -.0125,
            prior_n=100, prior_hit_rate=.625, prior_avg_signed_return=.0125, decay_ratio=1.))
    # Retain falsey/null conversion and insufficient/mixed classifications.
    rows[0].update(prior_n=None, recent_hit_rate=0.)
    rows[1].update(recent_hit_rate=0., prior_hit_rate=0.)
    rows[2].update(recent_avg_signed_return=None, prior_avg_signed_return=None)
    return rows


def drift_agent_fixture():
    return [dict(agent=f"agent-{i:03d}", asset="ETH", horizon="1h",
                 regime="TREND" if i%7==0 else "*", recent_n=30, prior_n=30,
                 recent_hit_rate=.625, prior_hit_rate=.625,
                 recent_avg_signed_return=-.125 if i%3==0 else .125,
                 prior_avg_signed_return=.125) for i in range(125)]


def load_reader(connect, *, legacy=False, enabled=True):
    tree = ast.parse(_LEGACY_SOURCE if legacy else RUNTIME.read_text(encoding="utf-8"))
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "pg_live_performance")
    namespace = {"pg_enabled": lambda: enabled, "pg_connect": connect, "json": json}
    module = ast.Module(body=[deepcopy(node)], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(RUNTIME), "exec"), namespace)
    return namespace["pg_live_performance"]


def load_agent_reader(connect, *, legacy=False, limit=5000):
    tree = ast.parse(_LEGACY_AGENT_SOURCE if legacy else RUNTIME.read_text(encoding="utf-8"))
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "pg_agent_performance")
    namespace = {"pg_enabled":lambda:True, "pg_connect":connect, "json":json,
                 "datetime":FrozenDatetime, "timezone":timezone, "math":math,
                 "AGENT_DECAY_HALF_LIFE_DAYS":60., "LIVE_LEARNING_MAX_EPISODES":limit}
    module = ast.Module(body=[deepcopy(node)], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(RUNTIME), "exec"), namespace)
    return namespace["pg_agent_performance"]


_RECORD_JOIN = re.compile(
    r'''(?m)^[ \t]*CROSS JOIN LATERAL jsonb_to_record\(CASE WHEN jsonb_typeof\((?P<expr>[^\r\n]+?)\)='object' '''
    r'''THEN (?P=expr) ELSE '\{\}'::jsonb END\) AS "memory_json_\d+"\('''
    r'''"(?:[^"]|"")+" jsonb(?:,"(?:[^"]|"")+" jsonb)*\)[ \t]*\n?''')


def without_projection_joins(sql):
    # Ignore only complete generated record scans. Original relations, JOIN
    # predicates, sampling windows, WHERE, ORDER BY and LIMIT still compare.
    return _RECORD_JOIN.sub('', sql)


def tail(sql):
    return _legacy_tail(without_projection_joins(sql))


def agent_query_contract(sql):
    sql = without_projection_joins(sql)
    cte = re.search(r"WITH\s+recent_decisions\s+AS\s*\((.*?)\)\s*SELECT", sql, re.I|re.S)
    joined = re.search(r"\bFROM\s+recent_decisions\b.*", sql, re.I|re.S)
    if cte is None or joined is None:
        raise AssertionError("Agent reader lost its pre-JOIN decision window")
    return " ".join(cte.group(1).split()), " ".join(joined.group(0).split())


def assert_record_plan(test, connection, sql, parameters, count, maximum_loops):
    plan = connection.execute('EXPLAIN (ANALYZE,VERBOSE,FORMAT JSON) '+sql, parameters).fetchone()['QUERY PLAN'][0]['Plan']
    pending, scans, retained = [plan], [], []
    while pending:
        node = pending.pop()
        pending.extend(node.get('Plans', []))
        if node.get('Node Type') == 'Function Scan' and node.get('Function Name') == 'jsonb_to_record':
            scans.append(node)
        if node.get('Node Type') in ('Sort', 'Incremental Sort'):
            for output in node.get('Output', []):
                match = re.fullmatch(r'"?(memory_json_\d+)"?\."?(agents|knowledge_cio_adjustment|knowledge_shadow_matches|features|trend_impulse|intraday_structure)"?',output.strip())
                if match:
                    retained.append('.'.join(match.groups()))
    test.assertFalse(retained, 'Sort retains expanded JSON record columns: '+', '.join(sorted(set(retained))))
    test.assertEqual(len(scans), count)
    for node in scans:
        test.assertEqual(node['Actual Rows'], 1)
        test.assertGreater(node['Actual Loops'], 0)
        test.assertLessEqual(node['Actual Loops'], maximum_loops)


class AgentTracedCursor(TracedCursor):
    def observe(self, row):
        result = super().observe(row)
        self.record["keys"][-1] = tuple(row.get(k) for k in
            ("asset","horizon","decision_ts","outcome_ts"))
        dp = row.get("decision_payload")
        agents = dp.get("agents") if isinstance(dp,dict) else None
        sequence = ([(a.get("agent"),a.get("direction")) if isinstance(a,dict) else a
                     for a in agents] if isinstance(agents,list) else agents)
        self.record.setdefault("agent_sequences", []).append(sequence)
        return result


class AgentTracedConnection(TracedConnection):
    def execute(self, sql, parameters=None):
        return AgentTracedCursor(self.connection.cursor(), self.trace).execute(sql, parameters)

    def cursor(self, *args, **kwargs):
        return AgentTracedCursor(self.connection.cursor(*args, **kwargs), self.trace, server=True)


class AgentReadTrace(ReadTrace):
    @contextmanager
    def connect(self):
        with self.raw_connect() as c:
            self.connections.append(c)
            yield AgentTracedConnection(c, self)


class MemoryCursor:
    """Small offline smoke fixture; real SQL is separately tested below."""
    def __init__(self, connection, name):
        self.connection, self.name = connection, name
        self.itersize, self.closed = None, False

    def __enter__(self):
        if not self.connection.in_transaction:
            raise AssertionError("autocommit server cursor requires explicit transaction")
        return self

    def __exit__(self, *args):
        self.closed = True

    def execute(self, sql, parameters=None):
        self.connection.sql = sql
        self.connection.parameters = parameters

    def __iter__(self):
        yield from self.connection.rows

    def fetchall(self):
        raise AssertionError("named memory reader must stream, never fetchall")


class MemoryConnection:
    def __init__(self, rows):
        self.rows, self.closed, self.in_transaction = rows, False, False
        self.server_cursor, self.sql = None, None

    @contextmanager
    def connect(self):
        try:
            yield self
        finally:
            self.closed = True

    @contextmanager
    def transaction(self):
        self.in_transaction = True
        try:
            yield
        finally:
            self.in_transaction = False

    def cursor(self, *, name):
        self.server_cursor = MemoryCursor(self, name)
        return self.server_cursor

    def execute(self, sql, parameters=None):
        self.sql = sql
        self.parameters = parameters
        return SimpleNamespace(fetchall=lambda: list(self.rows))


class SnapshotReaderSmokeTests(unittest.TestCase):
    def test_real_helper_import_query_and_reducer_execute_without_service_startup(self):
        rows = [{"asset":"ETH", "horizon":"1h", "decision":{"decision":"SHORT"},
                 "outcome":{"forward_return":-.125, "mfe":0., "mae":-.25}},
                {"asset":"ETH", "horizon":"1h", "decision":{"decision":"SHORT"},
                 "outcome":{"forward_return":0., "mfe":.5, "mae":None}}]
        old, current = MemoryConnection(rows), MemoryConnection(rows)
        expected = load_reader(old.connect, legacy=True)()
        actual = load_reader(current.connect)()
        self.assertEqual(actual, expected)
        self.assertEqual(actual[0]["n"], 2)
        self.assertEqual(actual[0]["hit_rate"], .5)
        self.assertEqual(tail(current.sql), tail(old.sql))
        self.assertEqual(current.server_cursor.name, "veritas_live_performance")
        self.assertEqual(current.server_cursor.itersize, 64)
        self.assertTrue(current.server_cursor.closed)
        self.assertTrue(current.closed)
        self.assertFalse(current.in_transaction)

    def test_reducer_failure_also_closes_the_real_helper_context(self):
        c = MemoryConnection([{"asset":"ETH", "horizon":"1h", "decision":{"decision":"LONG"},
                               "outcome":{"forward_return":"invalid"}}])
        with self.assertRaises(ValueError):
            load_reader(c.connect)()
        self.assertTrue(c.server_cursor.closed)
        self.assertTrue(c.closed)
        self.assertFalse(c.in_transaction)


@unittest.skipUnless(DSN, "isolated PostgreSQL test database not configured")
class _LedgerSQLFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg.rows import dict_row
        cls.driver, cls.row_factory = psycopg, staticmethod(dict_row)
        cls.schema = "snapshot_memory_test_" + uuid.uuid4().hex
        with psycopg.connect(DSN) as c:
            if c.execute("SELECT current_database()").fetchone()[0] != "veritas_quality_test":
                raise RuntimeError("Refusing writes outside veritas_quality_test")
            c.execute(f"CREATE SCHEMA {cls.schema}")
            cls.addClassCleanup(cls.drop_schema)
            c.execute(f"SET search_path TO {cls.schema}")
            c.execute("""CREATE TABLE ledger_events(
                event_key text PRIMARY KEY,entity_key text,event_type text,
                asset text,horizon text,payload jsonb,event_ts timestamptz)""")

    @classmethod
    def drop_schema(cls):
        with cls.driver.connect(DSN) as c:
            c.execute(f"DROP SCHEMA IF EXISTS {cls.schema} CASCADE")

    @contextmanager
    def connect(self):
        with self.driver.connect(DSN, row_factory=self.row_factory, autocommit=True) as c:
            c.execute(f"SET search_path TO {self.schema}")
            yield c

    def setUp(self):
        self.pending = []
        self.clear()

    def clear(self):
        with self.connect() as c:
            c.execute("TRUNCATE ledger_events")

    def add_event(self, key, kind, payload, *, asset="ETH", horizon="1h", suffix="", at=None):
        self.pending.append((f"{key}:{kind}:{suffix}",str(key),kind,asset,horizon,json.dumps(payload),at))

    def episode(self, key, decision, outcome, *, asset="ETH", horizon="1h",
                decision_type="decision", outcome_type="outcome", decision_ts=None, outcome_ts=None):
        self.add_event(key, decision_type, decision, asset=asset, horizon=horizon, at=decision_ts)
        self.add_event(key, outcome_type, outcome, asset=asset, horizon=horizon, at=outcome_ts)

    def save(self):
        with self.connect() as c:
            with c.transaction():
                with c.cursor() as cursor:
                    cursor.executemany("INSERT INTO ledger_events VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s)", self.pending)
        self.pending.clear()

    def read(self, *, legacy=False):
        trace = ReadTrace(self.connect)
        result = load_reader(trace.connect, legacy=legacy)()
        self.assertEqual(len(trace.queries), 1)
        return result, trace

    def assert_closed(self, trace):
        self.assertTrue(trace.cursors)
        self.assertTrue(all(c.cursor.closed for c in trace.cursors))
        self.assertTrue(all(c.closed for c in trace.connections))
        self.assertEqual(trace.transactions, 0)


class DriftRuleReaderSmokeTests(unittest.TestCase):
    def test_full_count_beyond_display_cap_and_resources_closed_before_agents(self):
        rows = drift_rule_fixture()
        old, current = MemoryConnection(rows), MemoryConnection(rows)
        agents = drift_agent_fixture()
        calls = []

        def after_rules():
            self.assertTrue(current.server_cursor.closed)
            self.assertTrue(current.closed)
            self.assertFalse(current.in_transaction)
            calls.append("agents")
            return agents

        expected = load_drift_reader(old.connect, legacy=True, agents=lambda: agents)()
        actual = load_drift_reader(current.connect, agents=after_rules)()
        self.assertEqual(actual, expected)
        self.assertEqual((actual["status"],actual["rule_drift_count"]), ("WARN",50))
        self.assertEqual((len(actual["rules"]),len(actual["agents"])), (100,100))
        self.assertFalse(any(r["drift_state"] in ("DECAYING","WEAKENING") for r in actual["rules"]))
        self.assertEqual(" ".join(current.sql.split()), " ".join(old.sql.split()))
        self.assertEqual(current.server_cursor.name, "veritas_rule_drift")
        self.assertEqual(current.server_cursor.itersize, 64)
        self.assertEqual(calls, ["agents"])

    def test_invalid_row_after_display_cap_still_raises_and_closes_resources(self):
        rows = drift_rule_fixture()
        rows[130]["prior_n"] = "invalid"
        old, current = MemoryConnection(rows), MemoryConnection(rows)
        calls = []
        with self.assertRaises(ValueError) as expected:
            load_drift_reader(old.connect, legacy=True)()
        with self.assertRaises(ValueError) as actual:
            load_drift_reader(current.connect, agents=lambda: calls.append("agents"))()
        self.assertEqual(str(actual.exception), str(expected.exception))
        self.assertTrue(current.server_cursor.closed)
        self.assertTrue(current.closed)
        self.assertFalse(current.in_transaction)
        self.assertEqual(calls, [])


class DriftRuleMemorySQLTests(_LedgerSQLFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with cls.driver.connect(DSN) as c:
            c.execute(f"SET search_path TO {cls.schema}")
            c.execute("""CREATE TABLE knowledge_rule_decay_stats(
                rule_id text PRIMARY KEY,asset text,horizon text,n integer,
                ew_hit_rate float8,ew_avg_signed_return float8,recent_n integer,
                recent_hit_rate float8,recent_avg_signed_return float8,prior_n integer,
                prior_hit_rate float8,prior_avg_signed_return float8,decay_ratio float8,sample text)""")

    def clear(self):
        with self.connect() as c:
            c.execute("TRUNCATE knowledge_rule_decay_stats")

    def seed_rules(self):
        rows = drift_rule_fixture()
        values = [tuple(row.values())+("OOS",) for row in reversed(rows)]
        # Both rows outrank every admitted row but must remain filtered out.
        excluded = dict(rows[0],rule_id="excluded-is",recent_n=1000)
        values.append(tuple(excluded.values())+("IS",))
        excluded.update(rule_id="excluded-small",n=19)
        values.append(tuple(excluded.values())+("OOS",))
        with self.connect() as c, c.transaction(), c.cursor() as cursor:
            cursor.executemany("INSERT INTO knowledge_rule_decay_stats VALUES("+
                               ",".join(["%s"]*14)+")", values)

    def test_real_postgres_full_population_order_and_output_parity(self):
        self.seed_rules()
        old_trace, trace = ReadTrace(self.connect), ReadTrace(self.connect)
        agents = drift_agent_fixture()
        calls = []

        def after_rules():
            self.assert_closed(trace)
            calls.append("agents")
            return agents

        expected = load_drift_reader(old_trace.connect, legacy=True, agents=lambda: agents)()
        actual = load_drift_reader(trace.connect, agents=after_rules)()
        self.assertEqual(actual, expected)
        self.assertEqual(actual["rule_drift_count"], 50)
        self.assertEqual(actual["rules"][-1]["rule_id"], "rule-099")
        self.assertEqual(trace.queries[0]["rows"], 150)
        self.assertEqual(trace.queries[0]["rows"], old_trace.queries[0]["rows"])
        self.assertEqual(" ".join(trace.queries[0]["sql"].split()),
                         " ".join(old_trace.queries[0]["sql"].split()))
        self.assertEqual(trace.cursors[0].cursor.name, "veritas_rule_drift")
        self.assertEqual(trace.cursors[0].itersize, 64)
        self.assertEqual(calls, ["agents"])

    def test_real_postgres_interrupted_second_batch_closes_and_retry_reads_every_row(self):
        self.seed_rules()
        trace = ReadTrace(self.connect)
        trace.fail_after = 65
        calls = []
        reader = load_drift_reader(trace.connect, agents=lambda: calls.append("agents") or [])
        with self.assertRaisesRegex(RuntimeError, "interrupted memory stream"):
            reader()
        self.assertEqual(trace.queries[0]["rows"], 65)
        self.assertEqual(calls, [])
        self.assert_closed(trace)
        trace.fail_after = None
        result = reader()
        self.assertEqual(result["rule_drift_count"], 50)
        self.assertEqual(trace.queries[1]["rows"], 150)
        self.assertEqual(calls, ["agents"])
        self.assert_closed(trace)


class ProjectedHelperRecordSQLTests(_LedgerSQLFixture):
    def test_ami_exact_v9_projection_preserves_null_presence_and_nested_shapes(self):
        missing = object()
        matches = [missing, None, False, 0, '', [], {}, True, 'rule', {'rule':1}]
        adjustments = [None, {}, False, 0, '', [], {'unrelated':1},
                       {'score':0}, {'score':None}, {'score_with_experience':.125}]
        for i, match in enumerate(matches):
            dp = {'decision':'LONG', 'research_decision':'', 'regime':None,
                  'agents':[{'direction':'LONG','confidence':0.,'unrelated':[1,2]}, None],
                  'knowledge_cio_adjustment':adjustments[i]}
            if match is not missing:
                dp['knowledge_shadow_matches'] = match
            self.episode(i, dp, {'forward_return':None if i%2 else 0.},
                         asset='CASE_'+str(i), decision_ts=NOW+timedelta(seconds=i))
        for i, dp in enumerate((None, False, [], 'encoded legacy payload', {}), start=20):
            self.episode(i, dp, {'forward_return':.125},
                         asset='CASE_'+str(i), decision_ts=NOW+timedelta(seconds=i))
        # An explicit null forward return participates, an absent key does not.
        self.episode('absent-outcome', {'decision':'LONG'}, {},
                     decision_ts=NOW+timedelta(seconds=30))
        self.save()
        with self.connect() as c:
            old = c.execute(_V9_AMI_PROJECTED_SQL).fetchall()
            actual = LM.ami_decision_rows(c)
        self.assertEqual(actual, old)
        self.assertEqual(len(actual), 15)
        by_asset = {row['asset']:row['dp'] for row in actual}
        self.assertIsNone(by_asset['CASE_0']['knowledge_shadow_matches'])
        self.assertIs(by_asset['CASE_1']['knowledge_shadow_matches'], False)
        for i in range(2,7):
            self.assertIs(by_asset['CASE_'+str(i)]['knowledge_shadow_matches'], False)
        for i in range(7,10):
            self.assertIs(by_asset['CASE_'+str(i)]['knowledge_shadow_matches'], True)

    def test_toasted_histories_use_bounded_record_scans_without_changing_metrics(self):
        heavy = 'UNUSED_HEAVY_FIELD:'*60000
        for i in range(3):
            dp = {'decision':'LONG' if i%2 else 'SHORT', 'regime':'REGIME_'+str(i),
                  'confidence':.625, 'knowledge_shadow_matches':None,
                  'knowledge_cio_adjustment':{'score':i/16, 'history':heavy},
                  'agents':[{'agent':'A', 'direction':'LONG', 'confidence':.75,
                             'history':heavy}], 'history':heavy}
            op = {'forward_return':(i-1)/16, 'mfe':i/8, 'mae':-i/16, 'history':heavy}
            self.episode(i, dp, op, decision_ts=NOW+timedelta(seconds=i))
        self.save()
        with self.connect() as c:
            sizes = c.execute("""SELECT pg_column_size(payload) AS stored,
                octet_length(payload::text) AS expanded FROM ledger_events""").fetchall()
        self.assertTrue(all(r['expanded']>1_000_000 for r in sizes))
        self.assertTrue(all(r['stored']<r['expanded']/8 for r in sizes))
        for name, reader in (('live',load_reader), ('agent',load_agent_reader)):
            with self.subTest(reader=name):
                old_trace, trace = ReadTrace(self.connect), ReadTrace(self.connect)
                old = reader(old_trace.connect, legacy=True)()
                actual = reader(trace.connect)()
                self.assertEqual(actual, old)
                query = trace.queries[0]
                self.assertEqual(query['rows'], 3)
                self.assertFalse(query['heavy'])
                self.assertLessEqual(query['sql'].count('d.payload'), 5)
                self.assertLessEqual(query['sql'].count('o.payload'), 5)
                self.assert_closed(trace)
                with self.connect() as c:
                    assert_record_plan(self,c,query['sql'],query['parameters'],1,3)
        trace = ReadTrace(self.connect)
        with trace.connect() as c:
            actual = LM.ami_decision_rows(c)
        query = trace.queries[0]
        with self.connect() as c:
            self.assertEqual(actual, c.execute(_V9_AMI_PROJECTED_SQL).fetchall())
            assert_record_plan(self,c,query['sql'],query['parameters'],2,3)
        self.assertFalse(query['heavy'])
        self.assertEqual(query['rows'], 3)
        self.assertLessEqual(query['sql'].count('d.payload'), 6)
        self.assertLessEqual(query['sql'].count('o.payload'), 5)


class SnapshotMemorySQLTests(_LedgerSQLFixture):
    def test_all_history_over_2200_rows_full_metric_parity_and_large_payload_reduction(self):
        heavy = "UNUSED_HEAVY_FIELD:"*150
        assets = ("BTC","ETH","MOEX","GOLD","BRENT","NQ","CNYRUBF")
        horizons = ("1m","5m","1h","4h","1d","3d","7d")
        decisions = ("LONG","SHORT","NO_TRADE")
        raw = (.125,-.25,0.,.0625,-.03125)
        eligible = 0
        for i in range(2405):
            asset, horizon = assets[i%7], horizons[(i//7)%7]
            dec = decisions[(i//49)%3]
            fr = None if i%19==0 else raw[i%len(raw)]
            eligible += fr is not None
            dp = {"decision":dec, "research_decision":"IGNORED_RESEARCH_LABEL",
                  "features":{"history":heavy}, "history":[{"candles":heavy}]}
            op = {"forward_return":fr, "mfe":(None,0.,.25)[i%3],
                  "mae":(None,0.,-.125)[i%3], "history":heavy}
            self.episode("case-"+str(i), dp, op, asset=asset, horizon=horizon)
        # Preserve JOIN multiplicity and both event-type filters exactly.
        self.add_event("case-0", "outcome", {"forward_return":2.,"mfe":0.,"mae":0.},
                       asset=assets[0], horizon=horizons[0], suffix="second-label")
        self.episode("wrong-decision-type", {"decision":"LONG"}, {"forward_return":999.},
                     decision_type="meta_signal")
        self.episode("not-completed", {"decision":"LONG"}, {"forward_return":999.},
                     outcome_type="pending")
        self.save()
        old, old_trace = self.read(legacy=True)
        actual, trace = self.read()
        self.assertEqual(actual, old)
        self.assertEqual(len(actual), 7*7*3)
        self.assertEqual(sum(x["n"] for x in actual), eligible+1)
        self.assertGreater(sum(x["n"] for x in actual), 2200)
        self.assertEqual(trace.queries[0]["rows"], 2406)
        self.assertEqual(trace.queries[0]["rows"], old_trace.queries[0]["rows"])
        self.assertEqual(tail(trace.queries[0]["sql"]), tail(old_trace.queries[0]["sql"]))
        self.assertFalse(trace.queries[0]["heavy"])
        self.assertGreater(old_trace.queries[0]["bytes"], 10_000_000)
        self.assertLess(trace.queries[0]["bytes"], old_trace.queries[0]["bytes"]*.05)
        self.assertEqual(trace.cursors[0].cursor.name, "veritas_live_performance")
        self.assertEqual(trace.cursors[0].itersize, 64)
        self.assert_closed(trace)

    def test_legacy_null_zero_missing_and_encoded_root_shapes_preserve_outputs(self):
        cases = [
            ({"decision":"LONG"},{"forward_return":0,"mfe":0,"mae":0}),
            ({"decision":"SHORT"},{"forward_return":"-0.125","mfe":False,"mae":-.25}),
            ({"decision":"NO_TRADE"},{"forward_return":True,"mfe":None}),
            ({"decision":""},{"forward_return":False}),
            ({"decision":None},{"forward_return":.25}),
            ({},{"forward_return":.25}),
            ({"unrelated":{"nested":1}},{"forward_return":.25}),
            ({"research_decision":"LONG"},{"forward_return":.25}),
            ({"decision":False},{"forward_return":.25}),
            ({"decision":0},{"forward_return":.25}),
            ({"decision":7},{"forward_return":.25}),
            ({"decision":"long"},{"forward_return":.25}),
            ({"decision":"LONG"},{}),
            ({"decision":"LONG"},{"forward_return":None,"mfe":99}),
            ({"decision":"LONG"},{"unrelated":1}),
            (json.dumps({"decision":"SHORT","old_shape":{"ignored":1}}),
             json.dumps({"forward_return":-.5,"mfe":0.,"mae":-1.,"old_shape":True})),
        ]
        for i, (dp, op) in enumerate(cases):
            self.episode(i, dp, op, asset=f"SHAPE_{i:02d}")
        self.save()
        old, old_trace = self.read(legacy=True)
        actual, trace = self.read()
        self.assertEqual(actual, old)
        self.assertEqual(trace.queries[0]["rows"], len(cases))
        self.assertEqual(len(actual), len(cases)-3)
        by = {r["asset"]:r for r in actual}
        self.assertEqual(by["SHAPE_00"]["n"], 1)
        self.assertEqual(by["SHAPE_00"]["avg_mfe"], 0.)
        self.assertEqual(by["SHAPE_00"]["hit_rate"], 0.)
        self.assertIsNone(by["SHAPE_07"]["decision"])
        self.assertEqual(by["SHAPE_07"]["directional_n"], 0)
        self.assertIs(by["SHAPE_08"]["decision"], False)
        self.assertEqual(by["SHAPE_15"]["avg_signed_return"], .5)
        self.assert_closed(trace)

    def test_stream_failure_after_first_batch_closes_every_resource_and_retry_succeeds(self):
        for i in range(140):
            self.episode(i, {"decision":"LONG"}, {"forward_return":.125})
        self.save()
        trace = ReadTrace(self.connect)
        trace.fail_after = 65
        reader = load_reader(trace.connect)
        with self.assertRaisesRegex(RuntimeError, "interrupted memory stream"):
            reader()
        self.assertEqual(trace.queries[0]["rows"], 65)
        self.assert_closed(trace)
        trace.fail_after = None
        result = reader()
        self.assertEqual(result[0]["n"], 140)
        self.assertEqual(trace.queries[1]["rows"], 140)
        self.assert_closed(trace)

    def test_invalid_legacy_payloads_raise_the_same_error_and_release_stream_resources(self):
        cases = [(False,{"forward_return":.125}), ([],{"forward_return":.125}),
                 ("not JSON",{"forward_return":.125}),
                 ({"decision":"LONG"},None), ({"decision":"LONG"},[]),
                 ({"decision":"LONG"},{"forward_return":""}),
                 ({"decision":"LONG"},{"forward_return":.125,"mfe":""}),
                 ({"decision":[]},{"forward_return":.125})]
        for dp, op in cases:
            with self.subTest(decision=dp, outcome=op):
                self.clear()
                self.episode("invalid", dp, op)
                self.save()
                old_trace, trace = ReadTrace(self.connect), ReadTrace(self.connect)
                with self.assertRaises(Exception) as old:
                    load_reader(old_trace.connect, legacy=True)()
                with self.assertRaises(type(old.exception)) as current:
                    load_reader(trace.connect)()
                self.assertEqual(str(current.exception), str(old.exception))
                self.assert_closed(trace)


class AgentSnapshotReaderSmokeTests(unittest.TestCase):
    def test_real_agent_helper_freezes_decay_and_preserves_star_double_count(self):
        rows = [{"asset":"ETH", "horizon":"1h", "decision_ts":NOW-timedelta(days=40),
                 "outcome_ts":None, "decision_payload":{"regime":"*","agents":[{"agent":"A","direction":"LONG"}]},
                 "outcome_payload":{"forward_return":.125}},
                {"asset":"ETH", "horizon":"1h", "decision_ts":NOW-timedelta(days=35),
                 "outcome_ts":NOW-timedelta(days=30),
                 "decision_payload":{"regime":"TREND","agents":[{"agent":"A","direction":"SHORT"}]},
                 "outcome_payload":{"forward_return":-.125}}]
        old, current = MemoryConnection(rows), MemoryConnection(rows)
        actual = load_agent_reader(current.connect, limit=5000)()
        self.assertEqual(actual, load_agent_reader(old.connect, legacy=True, limit=5000)())
        star = next(x for x in actual if x["regime"] == "*")
        self.assertEqual((star["n"],star["prior_n"],star["recent_n"]), (3,2,1))
        self.assertEqual(agent_query_contract(current.sql), agent_query_contract(old.sql))
        self.assertEqual(current.parameters, (5000,))
        self.assertEqual(current.server_cursor.name, "veritas_agent_performance")
        self.assertEqual(current.server_cursor.itersize, 64)
        self.assertTrue(current.server_cursor.closed)
        self.assertTrue(current.closed)
        self.assertFalse(current.in_transaction)


class AgentSnapshotMemorySQLTests(_LedgerSQLFixture):
    def read_agents(self, *, legacy=False, limit=5000):
        trace = AgentReadTrace(self.connect)
        result = load_agent_reader(trace.connect, legacy=legacy, limit=limit)()
        self.assertEqual(len(trace.queries), 1)
        return result, trace

    def test_full_5000_decision_window_before_join_preserves_order_decay_and_metrics(self):
        heavy = "UNUSED_HEAVY_FIELD:"*80
        expected_star_n = 0
        for i in range(5050):
            dt = NOW-timedelta(hours=5049-i)
            regime = "*" if i%11==0 else ("TREND" if i%2 else "RANGE")
            agents = [{"agent":"OUTSIDE_WINDOW" if i<50 else "A",
                       "direction":"LONG" if i%2 else "SHORT", "history":heavy},
                      {"agent":"B", "direction":"NO_TRADE" if i%5 else "LONG", "history":heavy}]
            if i%13==0:
                agents.append({"agent":"A", "direction":"SHORT" if i%2 else "LONG", "history":heavy})
            fr = None if i%17==0 else (.125 if i%3 else -.25)
            joined = i<5047
            if i>=50 and joined and fr is not None:
                expected_star_n += (1+int(i%13==0))*(2 if regime=="*" else 1)
            self.episode(i, {"regime":regime,"agents":agents,"history":heavy},
                         {"forward_return":fr,"history":heavy},
                         decision_ts=dt, outcome_ts=None if i%29==0 else dt+timedelta(minutes=15),
                         outcome_type="outcome" if joined else "pending")
        self.save()
        old, old_trace = self.read_agents(legacy=True)
        actual, trace = self.read_agents()
        self.assertEqual(actual, old)
        self.assertEqual(trace.queries[0]["rows"], 4997)
        self.assertEqual(trace.queries[0]["parameters"], (5000,))
        self.assertEqual(trace.queries[0]["parameters"], old_trace.queries[0]["parameters"])
        self.assertEqual(trace.queries[0]["keys"], old_trace.queries[0]["keys"])
        self.assertEqual(trace.queries[0]["agent_sequences"], old_trace.queries[0]["agent_sequences"])
        self.assertEqual(agent_query_contract(trace.queries[0]["sql"]),
                         agent_query_contract(old_trace.queries[0]["sql"]))
        times = [key[2] for key in trace.queries[0]["keys"]]
        self.assertEqual(times, sorted(times))
        self.assertEqual(times[0], NOW-timedelta(hours=4999))
        self.assertNotIn("OUTSIDE_WINDOW", {x["agent"] for x in actual})
        star = next(x for x in actual if x["agent"]=="A" and x["regime"]=="*")
        self.assertEqual(star["n"], expected_star_n)
        self.assertGreater(star["recent_n"], 0)
        self.assertGreater(star["prior_n"], 0)
        self.assertEqual(star["n"], star["recent_n"]+star["prior_n"])
        self.assertLess(star["effective_n"], star["n"])
        self.assertFalse(trace.queries[0]["heavy"])
        self.assertGreater(old_trace.queries[0]["bytes"], 20_000_000)
        self.assertLess(trace.queries[0]["bytes"], old_trace.queries[0]["bytes"]*.10)
        self.assertEqual(trace.cursors[0].cursor.name, "veritas_agent_performance")
        self.assertEqual(trace.cursors[0].itersize, 64)
        self.assert_closed(trace)

    def test_outcome_timestamp_fallback_30_day_boundary_future_and_zero_return(self):
        agent = {"agents":[{"agent":"A","direction":"LONG"}]}
        cases = [(NOW-timedelta(days=30,seconds=1),None,.125),
                 (NOW-timedelta(days=50),NOW-timedelta(days=30),0.),
                 (NOW-timedelta(days=31),NOW+timedelta(days=1),-.25)]
        for i, (dt, ot, fr) in enumerate(cases):
            self.episode(i, agent, {"forward_return":fr}, decision_ts=dt, outcome_ts=ot)
        self.save()
        old, old_trace = self.read_agents(legacy=True)
        actual, trace = self.read_agents()
        self.assertEqual(actual, old)
        self.assertEqual(len(actual), 1)
        row = actual[0]
        self.assertEqual((row["regime"],row["n"],row["recent_n"],row["prior_n"]), ("*",6,4,2))
        self.assertEqual(row["recent_hit_rate"], 0.)
        self.assertEqual(row["prior_hit_rate"], 1.)
        expected_weight = 2*(math.exp(-math.log(2)*(30+1/86400)/60)+math.exp(-math.log(2)*30/60)+1)
        self.assertAlmostEqual(row["effective_n"], expected_weight, places=13)
        self.assert_closed(trace)

    def test_legacy_agent_shapes_preserve_empty_fallbacks_missing_labels_and_errors(self):
        av = [{"agent":"A","direction":"LONG"}]
        successes = [({},.125), ({"agents":None},.125), ({"agents":False},.125),
                     ({"agents":{}},.125), ({"agents":""},.125),
                     ({"agents":[{"unrelated":1}]},.125),
                     ({"regime":None,"agents":av},.125),
                     ({"regime":False,"agents":av},.125),
                     ({"regime":0,"agents":av},0.),
                     ({"regime":"","agents":av},.125),
                     ({"regime":{},"agents":av},.125),
                     ({"agents":[{"direction":"SHORT"}]},-.125),
                     ({"agents":[None]},None),
                     (json.dumps({"regime":"OLD","agents":av}),.125)]
        for i, (dp, fr) in enumerate(successes):
            with self.subTest(legacy_shape=i):
                self.clear()
                self.episode(i, dp, {"forward_return":fr}, decision_ts=NOW)
                self.save()
                old, _ = self.read_agents(legacy=True)
                actual, trace = self.read_agents()
                self.assertEqual(actual, old)
                self.assert_closed(trace)
        failures = [{"agents":[None]}, {"agents":["invalid"]},
                    {"agents":{"x":1}}, {"agents":"nonempty"}]
        for dp in failures:
            with self.subTest(invalid_agents=dp):
                self.clear()
                self.episode("invalid", dp, {"forward_return":.125}, decision_ts=NOW)
                self.save()
                old_trace, trace = AgentReadTrace(self.connect), AgentReadTrace(self.connect)
                with self.assertRaises(Exception) as old:
                    load_agent_reader(old_trace.connect, legacy=True)()
                with self.assertRaises(type(old.exception)) as current:
                    load_agent_reader(trace.connect)()
                self.assertEqual(str(current.exception), str(old.exception))
                self.assert_closed(trace)

    def test_agent_stream_interruption_closes_cursor_transaction_connection_and_allows_retry(self):
        for i in range(130):
            self.episode(i, {"regime":"RANGE","agents":[{"agent":"A","direction":"LONG"}]},
                         {"forward_return":.125}, decision_ts=NOW-timedelta(minutes=130-i))
        self.save()
        trace = AgentReadTrace(self.connect)
        trace.fail_after = 65
        reader = load_agent_reader(trace.connect)
        with self.assertRaisesRegex(RuntimeError, "interrupted memory stream"):
            reader()
        self.assertEqual(trace.queries[0]["rows"], 65)
        self.assert_closed(trace)
        trace.fail_after = None
        result = reader()
        self.assertTrue(all(row["n"]==130 for row in result))
        self.assertEqual(trace.queries[1]["rows"], 130)
        self.assert_closed(trace)


if __name__ == "__main__":
    unittest.main()
