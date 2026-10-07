"""SQL, streaming and cache regressions for the two heavy decision-memory readers.

Only explicitly isolated PostgreSQL schemas are written. Runtime functions are
loaded as AST; service imports, market providers and trading loops never run.
The trend reducer below is frozen from 539ac490 before its streaming rewrite.
"""
import ast
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import hashlib
import math
import os
from pathlib import Path
import re
import threading
from types import SimpleNamespace
import unittest
import uuid

from veritas_learning_memory import ami_decision_rows

DSN = os.getenv("VERITAS_QUALITY_TEST_DSN", "")
RUNTIME = Path(__file__).with_name("veritas_intelligence.py")
AMI = Path(__file__).with_name("veritas_asset_management_intelligence.py")
NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)

_LEGACY_TREND_SOURCE = r'''def trend_case_learning_board(limit=500):
    """Learn whether onset / trend-day states continued after first detection.
    Consecutive five-minute snapshots are de-duplicated into independent phase episodes.
    The bootstrap NDX lesson is retained with zero statistical weight.
    """
    if not pg_enabled(): return {'status':'postgres_required','items':[],'bootstrap_lessons':BOOTSTRAP_CASE_LESSONS}
    with trend_case_cache_lock:
        z=trend_case_cache.get('value')
        if z and time.time()-float(trend_case_cache.get('at') or 0)<ANALYTICS_CACHE_SECONDS:
            return z
    with pg_connect() as c:
        rows=c.execute("""
          SELECT d.entity_key,d.asset,d.horizon,d.event_ts,d.payload AS dp,o.payload AS outcome_payload
          FROM ledger_events d JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'
          WHERE d.event_type='decision'
          ORDER BY d.asset,d.horizon,d.event_ts ASC
        """).fetchall()
    now_dt=datetime.now(timezone.utc); groups={}; episodes=[]; last_selected={}
    gap_s={'5m':300,'1h':1800,'4h':7200,'1d':21600,'3d':43200,'7d':86400}
    for r in rows:
        dp=r['dp'] if isinstance(r['dp'],dict) else json.loads(r['dp']); ti=dp.get('trend_impulse') or (dp.get('features') or {}).get('trend_impulse') or {}
        phase=str(ti.get('phase') or 'NONE'); direction=str(ti.get('direction') or 'NO_TRADE')
        if phase=='NONE' or direction not in ('LONG','SHORT'): continue
        ts=r['event_ts']
        if isinstance(ts,str): ts=datetime.fromisoformat(ts.replace('Z','+00:00'))
        if ts.tzinfo is None: ts=ts.replace(tzinfo=timezone.utc)
        k=(r['asset'],r['horizon'],phase,direction)
        prev=last_selected.get(k)
        if prev is not None and (ts-prev).total_seconds()<=gap_s.get(r['horizon'],86400):
            continue
        last_selected[k]=ts
        op=r['outcome_payload'] if isinstance(r['outcome_payload'],dict) else json.loads(r['outcome_payload']); fr=op.get('forward_return')
        if fr is None: continue
        fr=float(fr); sr=fr if direction=='LONG' else -fr
        age=max(0.0,(now_dt-ts).total_seconds()/86400.0); w=math.exp(-math.log(2.0)*age/TREND_CASE_HALF_LIFE_DAYS)
        b=groups.setdefault(k,{'n':0,'w':0.0,'wh':0.0,'wr':0.0,'missed':0,'mfe':[],'mae':[]})
        b['n']+=1; b['w']+=w; b['wh']+=w*(1.0 if sr>0 else 0.0); b['wr']+=w*sr
        research_dec=str(dp.get('research_decision') or dp.get('decision') or 'NO_TRADE')
        if research_dec=='NO_TRADE' and sr>0: b['missed']+=1
        if op.get('mfe') is not None: b['mfe'].append(float(op['mfe']))
        if op.get('mae') is not None: b['mae'].append(float(op['mae']))
        if len(episodes)<40:
            episodes.append({'asset':r['asset'],'horizon':r['horizon'],'phase':phase,'direction':direction,'decision':research_dec,
                             'signed_return':sr,'entry_quality':ti.get('entry_quality'),'onset_score':ti.get('onset_score'),'impulse_score':ti.get('impulse_score')})
    items=[]
    for (asset,h,phase,direction),b in groups.items():
        eff=b['w']; post=(b['wh']+5.0)/(eff+10.0) if eff>=0 else 0.5; avg=b['wr']/eff if eff else None
        state='BUILDING' if b['n']<TREND_CASE_MIN_N else 'SUPPORTED' if post>=0.54 and (avg or 0)>0 else 'WEAK' if post>=0.49 else 'DEGRADED'
        items.append({'asset':asset,'horizon':h,'phase':phase,'direction':direction,'n':b['n'],'effective_n':eff,
                      'posterior_continuation_rate':post,'decayed_avg_signed_return':avg,'missed_by_champion':b['missed'],
                      'avg_mfe':sum(b['mfe'])/len(b['mfe']) if b['mfe'] else None,
                      'avg_mae':sum(b['mae'])/len(b['mae']) if b['mae'] else None,
                      'learning_state':state,'automatic_weight_change':False if b['n']<TREND_CASE_MIN_N else True})
    items.sort(key=lambda x:(x['learning_state']!='SUPPORTED',-x['n']))
    out={'status':'ok','items':items,'recent_episodes':episodes[-40:],'bootstrap_lessons':BOOTSTRAP_CASE_LESSONS,
         'bootstrap_direct_weight':0.0,'min_n_for_statistical_learning':TREND_CASE_MIN_N,
         'policy':'current case changes architecture immediately; statistical confidence changes only after independent repeated phase episodes'}
    with trend_case_cache_lock:
        trend_case_cache['at']=time.time(); trend_case_cache['value']=out
    return out'''


LEGACY_AMI_QUERY = """
  SELECT d.event_ts,d.asset,d.horizon,d.payload AS dp,o.payload AS op
  FROM ledger_events d
  JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'
  WHERE d.event_type='decision' AND o.payload ? 'forward_return'
  ORDER BY d.event_ts DESC
  LIMIT 2200
"""


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


def clock_namespace():
    return {"datetime": FrozenDatetime, "timezone": timezone, "json": json,
            "math": math, "threading": threading,
            "time": SimpleNamespace(time=lambda: NOW.timestamp())}


def load_trend(connect, *, legacy=False):
    tree = ast.parse(RUNTIME.read_text(encoding="utf-8"))
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    ns = dict(clock_namespace(), pg_connect=connect, pg_enabled=lambda: True,
              trend_case_cache={"at": 0., "value": None},
              trend_case_cache_lock=threading.Lock(), trend_case_refresh_lock=threading.Lock(),
              ANALYTICS_CACHE_SECONDS=60, TREND_CASE_MIN_N=20,
              TREND_CASE_HALF_LIFE_DAYS=90.,
              BOOTSTRAP_CASE_LESSONS=[{"case_id": "zero_weight_original"}])
    node = (ast.parse(_LEGACY_TREND_SOURCE).body[0] if legacy
            else deepcopy(functions["trend_case_learning_board"]))
    module = ast.Module(body=[deepcopy(functions["_v90_jsonb_project_object"]), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(RUNTIME), "exec"), ns)
    return ns


def load_ami(*, legacy=False):
    # Constant assignments and function definitions have no service effects.
    # Imports are replaced by the explicit standard-library namespace above.
    tree = ast.parse(AMI.read_text(encoding="utf-8"))
    nodes = [deepcopy(n) for n in tree.body
             if isinstance(n, (ast.Assign, ast.AnnAssign, ast.FunctionDef))]
    ns = dict(clock_namespace(), ami_decision_rows=ami_decision_rows)
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(AMI), "exec"), ns)
    if legacy:
        def old_query(c):
            rows = c.execute(LEGACY_AMI_QUERY).fetchall()
            return ns["_independent_episodes"]([dict(r) for r in rows or []], 360)
        ns["_query_decision_episodes"] = old_query
    return ns


_RECORD_JOIN = re.compile(
    r'''(?m)^[ \t]*CROSS JOIN LATERAL jsonb_to_record\(CASE WHEN jsonb_typeof\((?P<expr>[^\r\n]+?)\)='object' '''
    r'''THEN (?P=expr) ELSE '\{\}'::jsonb END\) AS "memory_json_\d+"\('''
    r'''"(?:[^"]|"")+" jsonb(?:,"(?:[^"]|"")+" jsonb)*\)[ \t]*\n?''')


def without_projection_joins(sql):
    # Remove only the builder's complete, guarded, jsonb-typed record scans.
    # The original relation, JOIN predicate, filters, order and limits remain.
    return _RECORD_JOIN.sub('', sql)


def tail(sql):
    sql = without_projection_joins(sql)
    match = re.search(r"\bFROM\s+ledger_events\b", sql, re.I)
    if match is None:
        raise AssertionError("Unexpected memory reader relation")
    return " ".join(sql[match.start():].split())


def sampled_ami_tail(sql):
    # Only the original raw-row sample may move below the record scans. Keep
    # every join/filter/window clause and require explicit final row ordering.
    sampled = re.search(
        r"\bFROM\s*\(\s*SELECT\s+d\.event_ts\s*,\s*d\.asset\s*,\s*d\.horizon\s*,"
        r"\s*d\.payload\s+AS\s+decision_payload\s*,\s*o\.payload\s+AS\s+outcome_payload\s*"
        r"(?P<inner>FROM\s+ledger_events\b.*?)\)\s+AS\s+sample\s+"
        r"ORDER\s+BY\s+sample\.event_ts\s+DESC\s*$",
        without_projection_joins(sql), re.I | re.S)
    if sampled is None:
        raise AssertionError("AMI lost its raw-row sample or final descending order")
    return " ".join(sampled.group("inner").split())


class ProjectionJoinContractTests(unittest.TestCase):
    def test_ami_samples_original_joined_rows_before_record_projection(self):
        class Capture:
            def execute(self, sql):
                self.sql = sql
                return self

            def fetchall(self):
                return []

        capture = Capture()
        self.assertEqual(ami_decision_rows(capture), [])
        query = capture.sql
        self.assertEqual(sampled_ami_tail(query), tail(LEGACY_AMI_QUERY))
        sample_end = query.index(") AS sample")
        joins = list(_RECORD_JOIN.finditer(query))
        self.assertEqual(len(joins), 2)
        self.assertTrue(all(join.start() > sample_end for join in joins))
        self.assertNotEqual(sampled_ami_tail(query.replace("LIMIT 2200", "LIMIT 2000")),
                            tail(LEGACY_AMI_QUERY))
        with self.assertRaisesRegex(AssertionError, "final descending order"):
            sampled_ami_tail(query.replace("ORDER BY sample.event_ts DESC", "ORDER BY sample.event_ts ASC"))

    def test_tail_removes_only_the_complete_guarded_record_joins(self):
        from veritas_learning_memory import JsonbProjection
        projection = JsonbProjection()
        fields = projection.fields("d.payload", ("features", "trend_impulse"))
        projection.fields(fields["features"], ("trend_impulse",))
        original = """SELECT d.entity_key FROM ledger_events d
          JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'
          WHERE d.event_type='decision' ORDER BY d.event_ts ASC LIMIT 500"""
        projected = original.replace("          WHERE", projection.joins_sql+"\n          WHERE")
        self.assertEqual(tail(projected), tail(original))
        mutations = (("LIMIT 500", "LIMIT 1"), ("d.event_ts ASC", "d.event_ts DESC"),
                     ("o.entity_key=d.entity_key", "o.entity_key<>d.entity_key"),
                     ("d.event_type='decision'", "d.event_type<>'decision'"),
                     ("THEN d.payload", "THEN o.payload"), ("\"features\" jsonb", "\"features\" text"))
        for before, after in mutations:
            with self.subTest(change=after):
                self.assertNotEqual(tail(projected.replace(before, after)), tail(original))


class ReadTrace:
    def __init__(self, connect):
        self.raw_connect = connect
        self.queries, self.cursors, self.connections = [], [], []
        self.transactions, self.fail_after, self.on_execute = 0, None, None

    @contextmanager
    def connect(self):
        with self.raw_connect() as c:
            self.connections.append(c)
            yield TracedConnection(c, self)


class TracedCursor:
    def __init__(self, cursor, trace, *, server=False):
        self.cursor, self.trace, self.server = cursor, trace, server
        self.record = None
        if server:
            trace.cursors.append(self)

    @property
    def itersize(self):
        return self.cursor.itersize

    @itersize.setter
    def itersize(self, value):
        self.cursor.itersize = value

    def __enter__(self):
        self.cursor.__enter__()
        return self

    def __exit__(self, *args):
        return self.cursor.__exit__(*args)

    def execute(self, sql, parameters=None):
        self.record = {"sql": sql, "parameters": parameters, "server": self.server,
                       "rows": 0, "bytes": 0, "keys": [], "heavy": False}
        self.trace.queries.append(self.record)
        self.cursor.execute(sql, parameters)
        if self.trace.on_execute:
            self.trace.on_execute(self.record)
        return self

    def observe(self, row):
        text = json.dumps(dict(row), ensure_ascii=False, default=str)
        self.record["rows"] += 1
        self.record["bytes"] += len(text.encode("utf-8"))
        self.record["heavy"] |= "UNUSED_HEAVY_FIELD" in text
        self.record["keys"].append(tuple(row.get(k) for k in ("entity_key", "asset", "horizon", "event_ts")))
        return row

    def __iter__(self):
        for row in self.cursor:
            if self.trace.fail_after is not None and self.record["rows"] == self.trace.fail_after:
                raise RuntimeError("injected interrupted memory stream")
            yield self.observe(row)

    def fetchall(self):
        if self.server:
            raise AssertionError("A named memory cursor must not materialize fetchall")
        return [self.observe(row) for row in self.cursor.fetchall()]

    def fetchone(self):
        return self.cursor.fetchone()


class TracedConnection:
    def __init__(self, connection, trace):
        self.connection, self.trace = connection, trace

    def execute(self, sql, parameters=None):
        return TracedCursor(self.connection.cursor(), self.trace).execute(sql, parameters)

    def cursor(self, *args, **kwargs):
        return TracedCursor(self.connection.cursor(*args, **kwargs), self.trace, server=True)

    @contextmanager
    def transaction(self):
        self.trace.transactions += 1
        try:
            with self.connection.transaction():
                yield
        finally:
            self.trace.transactions -= 1


def fixed_score_inputs(ns):
    portfolio = {"n": 64, "wins": 40, "win_rate": .625, "net_pnl_rub": 2000.,
        "avg_net_pnl_rub": 31.25, "avg_return_on_entry_nav": .0012,
        "profit_factor": 1.4, "max_drawdown": .06}
    learning = {"n": 90, "avg_capture_ratio": .4, "avg_movement_realization_ratio": .45,
        "avg_giveback_pct": .2, "entry_error_rate": .1, "cost_drag_rate": .1,
        "exit_capture_error_rate": .2, "stop_error_rate": .15, "overforecast_rate": .1,
        "early_bad_rate": .3, "recent_bad_rate": .15, "early_realization": .2,
        "recent_realization": .45}
    ns["_query_fresh_portfolio"] = lambda c, epoch: dict(portfolio)
    ns["_query_learning"] = lambda c: dict(learning)
    ns["_baseline"] = lambda c, score, components: {"score": 32., "components": {}, "captured_at": "fixed-origin"}


@unittest.skipUnless(DSN, "isolated PostgreSQL test database not configured")
class TrendMemorySQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg.rows import dict_row
        cls.driver = psycopg
        cls.row_factory = staticmethod(dict_row)
        cls.schema = "trend_memory_test_" + uuid.uuid4().hex
        with psycopg.connect(DSN) as c:
            if c.execute("SELECT current_database()").fetchone()[0] != "veritas_quality_test":
                raise RuntimeError("Refusing writes outside veritas_quality_test")
            c.execute(f"CREATE SCHEMA {cls.schema}")
            cls.addClassCleanup(cls.drop_schema)
            c.execute(f"SET search_path TO {cls.schema}")
            c.execute("""CREATE TABLE ledger_events(
                event_key text PRIMARY KEY,entity_key text,event_type text,event_ts timestamptz,
                asset text,horizon text,payload jsonb)""")
            c.execute("CREATE TABLE knowledge_sources(source_id text)")
            c.execute("CREATE TABLE knowledge_rules(rule_id text)")
            c.execute("""CREATE TABLE knowledge_backtest_oos_stats(
                rule_id text,sample text,n integer,hit_rate float8,avg_signed_return float8)""")
            c.execute("INSERT INTO knowledge_sources SELECT 's'||generate_series(1,17)")
            c.execute("INSERT INTO knowledge_rules SELECT 'r'||generate_series(1,25)")
            c.execute("INSERT INTO knowledge_backtest_oos_stats SELECT 'r'||generate_series(1,9),'OOS',30,.6,.01")
            c.execute("INSERT INTO knowledge_backtest_oos_stats VALUES('ineligible','TRAIN',99,.9,.1)")

    @classmethod
    def drop_schema(cls):
        with cls.driver.connect(DSN) as c:
            c.execute(f"DROP SCHEMA IF EXISTS {cls.schema} CASCADE")

    @contextmanager
    def connect(self):
        # Production pg_connect is autocommit. A named server cursor therefore
        # requires the explicit transaction that this test must exercise.
        with self.driver.connect(DSN, row_factory=self.row_factory, autocommit=True) as c:
            c.execute(f"SET search_path TO {self.schema}")
            yield c

    def setUp(self):
        with self.connect() as c:
            c.execute("TRUNCATE ledger_events")
        self.pending = []
        self.start = datetime(2026, 9, 1, tzinfo=timezone.utc)

    def episode(self, key, seconds, dp, op, *, asset="ETH", horizon="1h", outcome_type="outcome"):
        for kind, payload in (("decision", dp), (outcome_type, op)):
            self.pending.append((str(key)+":"+kind, str(key), kind,
                self.start+timedelta(seconds=seconds), asset, horizon, json.dumps(payload)))

    def save(self):
        with self.connect() as c:
            with c.transaction():
                with c.cursor() as cursor:
                    cursor.executemany("INSERT INTO ledger_events VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb)", self.pending)
        self.pending.clear()

    def decision(self, phase="ONSET", direction="LONG", quality="FRESH"):
        return {"decision": "NO_TRADE", "trend_impulse": {"phase": phase,
            "direction": direction, "entry_quality": quality, "onset_score": .72, "impulse_score": .8}}

    def run_trend(self, *, legacy=False, limit=500):
        trace = ReadTrace(self.connect)
        ns = load_trend(trace.connect, legacy=legacy)
        result = ns["trend_case_learning_board"](limit)
        self.assertEqual(len(trace.queries), 1)
        return result, trace

    def test_complete_history_over_500_rows_exact_gaps_order_and_missing_outcomes(self):
        for i in range(620):
            self.episode("history"+str(i), i*3601, self.decision(quality="episode-"+str(i)),
                         {"forward_return": .01 if i % 4 else -.005, "mfe": .02, "mae": -.01})
            if i % 10 == 0:
                self.episode("repeat"+str(i), i*3601+900, self.decision(quality="DUPLICATE"),
                             {"forward_return": 999., "mfe": 999., "mae": -999.})
        # In the original reducer a selected missing-outcome row still advances
        # last_selected. Exact-boundary rows stay excluded; +1 second is fresh.
        for i, (sec, fr) in enumerate(((0,None),(1800,.9),(1801,.02),(1802,.8),(3601,.7),(3602,.03))):
            self.episode("gap"+str(i), sec, self.decision("GAP_CASE"), {"forward_return":fr}, asset="BTC")
        self.episode("no_outcome", -1000, self.decision(), {"forward_return": 77.}, outcome_type="pending")
        self.save()
        old, old_trace = self.run_trend(legacy=True)
        actual, trace = self.run_trend(limit=1)  # Legacy limit never truncated history.
        self.assertEqual(actual, old)
        self.assertEqual(tail(trace.queries[0]["sql"]), tail(old_trace.queries[0]["sql"]))
        self.assertEqual(trace.queries[0]["keys"], old_trace.queries[0]["keys"])
        onset = next(x for x in actual["items"] if x["phase"] == "ONSET")
        gap = next(x for x in actual["items"] if x["phase"] == "GAP_CASE")
        self.assertEqual(onset["n"], 620)
        self.assertEqual(gap["n"], 2)
        self.assertEqual(len(actual["recent_episodes"]), 40)
        self.assertNotIn("DUPLICATE", [x["entry_quality"] for x in actual["recent_episodes"]])
        self.assertGreater(trace.queries[0]["rows"], 500)
        self.assertEqual(trace.cursors[0].itersize, 64)
        self.assertTrue(all(c.cursor.closed for c in trace.cursors))
        self.assertTrue(all(c.closed for c in trace.connections))
        self.assertEqual(trace.transactions, 0)

    def test_trend_legacy_truthiness_and_streamed_projection_drop_heavy_fields(self):
        heavy = "UNUSED_HEAVY_FIELD:"*600
        nested = self.decision()["trend_impulse"]
        shapes = [self.decision(), {"features":{"trend_impulse":nested}},
            {"trend_impulse":{}, "features":{"trend_impulse":nested}},
            {"trend_impulse":None, "features":{"trend_impulse":nested}},
            {"trend_impulse":False, "features":{"trend_impulse":nested}},
            {"trend_impulse":0, "features":{"trend_impulse":nested}},
            {"trend_impulse":"", "features":{"trend_impulse":nested}},
            {"trend_impulse":[], "features":{"trend_impulse":nested}},
            {"trend_impulse":{"unrelated":1}, "features":{"trend_impulse":nested}},
            self.decision("TREND_DAY","SHORT"),
            {"research_decision":"", "decision":"SHORT", "trend_impulse":nested}]
        for i in range(145):
            dp = deepcopy(shapes[i % len(shapes)])
            dp["diagnostic_history"] = heavy
            if isinstance(dp.get("trend_impulse"), dict) and dp["trend_impulse"]:
                dp["trend_impulse"]["diagnostic_history"] = heavy
            if dp.get("features"):
                dp["features"]["diagnostic_history"] = heavy
                dp["features"]["trend_impulse"]["diagnostic_history"] = heavy
            self.episode(i, i*7201, dp, {"forward_return":(-1 if i%3 else 1)*.01,
                "mfe":None if i%4 else .02, "mae":-.01, "diagnostic_history":heavy})
        self.save()
        old, old_trace = self.run_trend(legacy=True)
        actual, trace = self.run_trend()
        self.assertEqual(actual, old)
        self.assertEqual(trace.queries[0]["keys"], old_trace.queries[0]["keys"])
        self.assertEqual(trace.queries[0]["rows"], 145)
        self.assertFalse(trace.queries[0]["heavy"])
        self.assertGreater(old_trace.queries[0]["bytes"], 1_000_000)
        self.assertLess(trace.queries[0]["bytes"], old_trace.queries[0]["bytes"]*.10)

    def test_large_toasted_history_matches_legacy_and_does_not_sort_expanded_graphs(self):
        count = 528  # More than the legacy public limit; every row is an episode.
        def heavy(kind, index):
            raw = hashlib.shake_256(f"{kind}:{index}".encode()).digest(12288)
            return "UNUSED_HEAVY_FIELD:" + base64.b64encode(raw).decode("ascii")
        expected_keys = []
        for i in range(count):
            dp = self.decision(quality="large-"+str(i))
            dp["diagnostic_history"] = heavy("root", i)
            dp["trend_impulse"]["diagnostic_history"] = heavy("top-trend", i)
            nested = self.decision("TREND_DAY", "SHORT", "nested-"+str(i))["trend_impulse"]
            nested["diagnostic_history"] = heavy("nested-trend", i)
            dp["features"] = {"trend_impulse":nested, "diagnostic_history":heavy("features", i)}
            op = {"forward_return":.01 if i % 4 else -.005,
                  "mfe":.02, "mae":-.01, "diagnostic_history":heavy("outcome", i)}
            key = "large"+str(i)
            self.episode(key, i*3601, dp, op)
            expected_keys.append((key, "ETH", "1h", self.start+timedelta(seconds=i*3601)))
            if (i+1) % 64 == 0:
                self.save()  # Bound the fixture writer's pending JSON strings.
        self.save()
        with self.connect() as c:
            storage = dict(c.execute("""SELECT COUNT(*) AS n,
              MIN(pg_column_size(payload)) AS min_stored_bytes,
              MIN(octet_length(payload::text)) AS min_text_bytes,
              MIN(pg_column_size(payload)::float8 / octet_length(payload::text)) AS min_storage_ratio,
              MIN(octet_length((payload->'features')::text)) AS min_features_bytes,
              MIN(octet_length((payload#>'{features,trend_impulse}')::text)) AS min_nested_bytes
              FROM ledger_events WHERE event_type='decision'""").fetchone())
            storage["toast_relation_bytes"] = c.execute("""SELECT pg_relation_size(reltoastrelid) AS n
              FROM pg_class WHERE oid='ledger_events'::regclass""").fetchone()["n"]
        self.assertEqual(storage["n"], count)
        self.assertGreater(storage["min_text_bytes"], 64000)
        self.assertGreater(storage["min_stored_bytes"], 32000)
        self.assertGreater(storage["min_storage_ratio"], .5)
        self.assertGreater(storage["min_features_bytes"], 32000)
        self.assertGreater(storage["min_nested_bytes"], 16000)
        self.assertGreater(storage["toast_relation_bytes"], 0)
        old, old_trace = self.run_trend(legacy=True)
        actual, trace = self.run_trend(limit=1)
        self.assertEqual(actual, old)
        self.assertEqual(trace.queries[0]["keys"], expected_keys)
        self.assertEqual(trace.queries[0]["keys"], old_trace.queries[0]["keys"])
        self.assertEqual(tail(trace.queries[0]["sql"]), tail(old_trace.queries[0]["sql"]))
        self.assertEqual(trace.queries[0]["rows"], count)
        self.assertEqual(actual["items"][0]["n"], count)
        self.assertEqual([x["entry_quality"] for x in actual["recent_episodes"]],
                         ["large-"+str(i) for i in range(40)])
        self.assertGreater(old_trace.queries[0]["bytes"], 30*1024*1024)
        self.assertFalse(trace.queries[0]["heavy"])
        self.assertLess(trace.queries[0]["bytes"], old_trace.queries[0]["bytes"]*.02)
        self.assertEqual(trace.cursors[0].itersize, 64)
        self.assertTrue(all(cursor.cursor.closed for cursor in trace.cursors))
        self.assertTrue(all(c.closed for c in trace.connections))
        self.assertEqual(trace.transactions, 0)
        with self.connect() as c:
            plan = c.execute("EXPLAIN (ANALYZE,VERBOSE,FORMAT JSON) "+trace.queries[0]["sql"]).fetchone()["QUERY PLAN"][0]["Plan"]
        nodes, pending = [], [plan]
        while pending:
            node = pending.pop(); nodes.append(node); pending.extend(node.get("Plans", []))
        scans = [node for node in nodes if node.get("Node Type") == "Function Scan"
                 and node.get("Function Name") == "jsonb_to_record"]
        sorts = [node for node in nodes if node.get("Node Type") in ("Sort", "Incremental Sort")]
        diagnostic = {"fixture":storage, "selected_rows":count,
            "record_scans":[{k:n.get(k) for k in ("Alias", "Actual Loops", "Actual Rows")} for n in scans],
            "sorts":[{**{k:n.get(k) for k in ("Node Type", "Plan Width", "Actual Rows", "Sort Method", "Sort Space Used", "Sort Space Type")},
                "output":[v if len(v)<100 else "<projected expression>" for v in n.get("Output", [])]} for n in sorts]}
        print("trend_record_plan "+json.dumps(diagnostic, sort_keys=True), flush=True)
        self.assertEqual(len(scans), 5)
        self.assertEqual(len({node["Alias"] for node in scans}), 5)
        for node in scans:
            self.assertEqual(node["Actual Loops"], count)
            self.assertEqual(node["Actual Rows"], 1)
        self.assertTrue(sorts, "The complete-history ORDER BY must be exercised")
        # Original d.payload/o.payload can be cheap TOAST pointers. Extracted
        # features/trend objects are expanded datums and must be compacted
        # before a Sort retains them, even if the eventual wire JSON is small.
        for node in sorts:
            for value in node.get("Output", []):
                bare = re.sub(r'["()\s]', '', value)
                self.assertIsNone(re.fullmatch(r'memory_json_\d+\.(?:features|trend_impulse|intraday_structure)', bare),
                                  "Sort retained expanded record graph: "+value)

    def test_malformed_truthy_shapes_preserve_error_cache_and_close_the_stream(self):
        cases = (({"trend_impulse":["invalid"]}, AttributeError),
                 ({"features":"invalid"}, AttributeError), ([], TypeError))
        for index, (dp, error_type) in enumerate(cases):
            with self.subTest(shape=index):
                with self.connect() as c:
                    c.execute("TRUNCATE ledger_events")
                self.episode(index, 0, dp, {"forward_return":.01})
                self.save()
                for legacy in (True, False):
                    trace = ReadTrace(self.connect)
                    ns = load_trend(trace.connect, legacy=legacy)
                    previous = {"status":"previous_complete", "items":[{"n":99}]}
                    cache_at = NOW.timestamp()-300
                    ns["trend_case_cache"].update(at=cache_at, value=previous)
                    with self.assertRaises(error_type):
                        ns["trend_case_learning_board"]()
                    self.assertIs(ns["trend_case_cache"]["value"], previous)
                    self.assertEqual(ns["trend_case_cache"]["at"], cache_at)
                    self.assertTrue(all(cursor.cursor.closed for cursor in trace.cursors))
                    self.assertTrue(all(c.closed for c in trace.connections))
                    self.assertEqual(trace.transactions, 0)

    def test_interrupted_server_cursor_closes_resources_preserves_cache_and_can_retry(self):
        for i in range(4):
            self.episode(i, i*3601, self.decision(), {"forward_return":.01})
        self.save()
        trace = ReadTrace(self.connect)
        trace.fail_after = 2
        ns = load_trend(trace.connect)
        previous = {"status":"previous_complete_snapshot", "items":[{"n":99}]}
        ns["trend_case_cache"].update(at=NOW.timestamp()-300, value=previous)
        with self.assertRaisesRegex(RuntimeError, "interrupted memory stream"):
            ns["trend_case_learning_board"]()
        self.assertIs(ns["trend_case_cache"]["value"], previous)
        self.assertTrue(all(c.cursor.closed for c in trace.cursors))
        self.assertTrue(all(c.closed for c in trace.connections))
        self.assertEqual(trace.transactions, 0)
        trace.fail_after = None
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(ns["trend_case_learning_board"]).result(timeout=5)
        self.assertEqual(result["items"][0]["n"], 4)

    def test_concurrent_trend_readers_share_one_complete_server_stream(self):
        self.episode(1, 0, self.decision(), {"forward_return":.01})
        self.save()
        trace = ReadTrace(self.connect)
        entered, release, duplicate, second_started = (threading.Event() for _ in range(4))
        def block(record):
            if len(trace.queries) > 1:
                duplicate.set()
            entered.set()
            if not release.wait(4):
                raise AssertionError("test did not release the stream")
        trace.on_execute = block
        fn = load_trend(trace.connect)["trend_case_learning_board"]
        def second():
            second_started.set()
            return fn()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(fn)
            try:
                self.assertTrue(entered.wait(3))
                later = pool.submit(second)
                self.assertTrue(second_started.wait(2))
                self.assertFalse(duplicate.wait(.15))
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5), later.result(timeout=5))
        self.assertEqual(len(trace.queries), 1)

    def seed_ami(self):
        heavy = "UNUSED_HEAVY_FIELD:"*60
        match_values = [None, False, 0, "", [], {}, True, 1, "rule", {"r":1}, [{"rule_id":"r1","history":heavy}]]
        adjustments = [{"score_with_experience":.03,"score":-.1},
                       {"score_with_experience":0,"score":.03}, {"score_with_experience":None,"score":.02},
                       {}, None, {"unrelated":1}, {"score":"invalid"}, {"score":-.04}]
        votes = [[{"direction":"LONG","confidence":.8}, {"direction":"SHORT","confidence":.2}, None],
                 [{"direction":"SHORT","confidence":"0.9"}, {"direction":"LONG","confidence":.1}],
                 [{"direction":"LONG","confidence":.5}, {"direction":"SHORT","confidence":.5}],
                 [{"direction":"NO_TRADE","confidence":1}, {"unrelated":1}, "invalid"],
                 [], None, {"non_array":True}, "not_an_array"]
        for i in range(2400):
            agents = deepcopy(votes[i % len(votes)])
            if isinstance(agents, list):
                for a in agents:
                    if isinstance(a, dict):
                        a["rationale"] = heavy
            dp = {"research_decision":("LONG","SHORT","NO_TRADE","")[i%4],
                "decision":"LONG", "regime":"REGIME_"+str((i//7)%5), "agents":agents,
                "knowledge_cio_adjustment":adjustments[i%len(adjustments)],
                "knowledge_shadow_matches":match_values[i%len(match_values)], "history":heavy}
            op = {"forward_return":None if i%31==0 else (i%9-4)*.012, "history":heavy}
            self.episode(i, i*61, dp, op, asset=("ETH","BTC","MOEX")[i%3], horizon=("5m","1h")[i%2])
        self.save()

    def test_ami_projection_preserves_episode_order_reference_votes_knowledge_and_all_score_metrics(self):
        self.seed_ami()
        old_ns, new_ns = load_ami(legacy=True), load_ami()
        old_trace, new_trace = ReadTrace(self.connect), ReadTrace(self.connect)
        with old_trace.connect() as c:
            old = old_ns["_query_decision_episodes"](c)
        with new_trace.connect() as c:
            projected = new_ns["_query_decision_episodes"](c)
        scalar = lambda episodes: [{k:v for k,v in e.items() if k != "payload"} for e in episodes]
        self.assertEqual(scalar(projected), scalar(old))
        self.assertEqual(len(projected), 360)
        self.assertEqual(new_trace.queries[0]["rows"], 2200)
        self.assertEqual(new_trace.queries[0]["keys"], old_trace.queries[0]["keys"])
        self.assertEqual(sampled_ami_tail(new_trace.queries[0]["sql"]), tail(LEGACY_AMI_QUERY))
        self.assertFalse(new_trace.queries[0]["heavy"])
        self.assertLess(new_trace.queries[0]["bytes"], old_trace.queries[0]["bytes"]*.20)
        for old_episode, new_episode in zip(old, projected):
            self.assertEqual(new_ns["_static_ai_decision"](new_episode["payload"]),
                             old_ns["_static_ai_decision"](old_episode["payload"]))
        for key in ("decision","reference_decision"):
            self.assertEqual(new_ns["_decision_metrics"](projected,key), old_ns["_decision_metrics"](old,key))
        with self.connect() as c:
            old_knowledge = old_ns["_query_knowledge"](c, old)
            new_knowledge = new_ns["_query_knowledge"](c, projected)
        self.assertEqual(new_knowledge, old_knowledge)
        self.assertGreater(old_knowledge["application_n"], 0)
        for ns in (old_ns,new_ns):
            fixed_score_inputs(ns)
        args = (self.connect, {"index_vs_start":114}, "2026-09-01T00:00:00Z")
        self.assertEqual(new_ns["build_scorecard"](*args,cache_seconds=0),
                         old_ns["build_scorecard"](*args,cache_seconds=0))


class AMISingleFlightTests(unittest.TestCase):
    def test_ast_harness_executes_the_actual_projected_reader(self):
        from test_veritas_management_intelligence import _FakeConn
        old, actual = load_ami(legacy=True), load_ami()
        args = ({"index_vs_start":110.}, "2026-09-30T04:59:29+00:00")
        expected = old["build_scorecard"](_FakeConn, *args, cache_seconds=0)
        result = actual["build_scorecard"](_FakeConn, *args, cache_seconds=0)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result, expected)

    def namespace(self):
        ns = load_ami()
        fixed_score_inputs(ns)
        ns["_query_knowledge"] = lambda c,e: {"sources":17,"rules":25,"validated_oos_rules":9,
            "application_n":0,"application_rate":0.,"applied_hit_rate":None,"applied_utility":None}
        return ns

    @contextmanager
    def connect(self):
        yield SimpleNamespace(execute=lambda *args: None)

    def test_simultaneous_score_consumers_calculate_once_then_use_cache(self):
        ns = self.namespace()
        entered, release, duplicate, second_started = (threading.Event() for _ in range(4))
        calls = []
        def episodes(c):
            calls.append(1)
            if len(calls)>1:
                duplicate.set()
            entered.set()
            if not release.wait(4):
                raise AssertionError("test did not release AMI query")
            return []
        ns["_query_decision_episodes"] = episodes
        def run(second=False):
            if second:
                second_started.set()
            return ns["build_scorecard"](self.connect,{"index_vs_start":114},"fixed-epoch",cache_seconds=55)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run)
            try:
                self.assertTrue(entered.wait(2))
                later = pool.submit(run, True)
                self.assertTrue(second_started.wait(2))
                self.assertFalse(duplicate.wait(.15))
            finally:
                release.set()
            self.assertEqual(first.result(timeout=4),later.result(timeout=4))
        self.assertEqual(len(calls),1)

    def test_failed_score_refresh_releases_lock_without_publishing_partial_cache(self):
        ns = self.namespace()
        previous = {"score":31,"status":"complete-previous"}
        ns["_CACHE"].update(at=0,epoch="fixed-epoch",value=previous)
        def fail(c):
            raise RuntimeError("AMI input unavailable")
        ns["_query_decision_episodes"] = fail
        with self.assertRaisesRegex(RuntimeError,"AMI input unavailable"):
            ns["build_scorecard"](self.connect,{},"fixed-epoch")
        self.assertIs(ns["_CACHE"]["value"],previous)
        ns["_query_decision_episodes"] = lambda c: []
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(ns["build_scorecard"],self.connect,{},"fixed-epoch").result(timeout=3)
        self.assertEqual(result["status"],"OK")
        self.assertIsNot(ns["_CACHE"]["value"],previous)


if __name__ == "__main__":
    unittest.main()
