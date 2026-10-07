"""Real PostgreSQL parity for bounded legacy diagnostic memory reads.

Load only the selected pure readers (and their SQL projection helper), never the
service module or its startup/background workers.  Each reader runs against the
same isolated records with frozen historical queries and the production query.
The unchanged reducer must produce identical
results, including legacy Python truthiness and sample-before-filter behavior.
"""
import ast
import hashlib
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import unittest
import uuid

import veritas_learning_exports as VLE


DSN = os.getenv("VERITAS_QUALITY_TEST_DSN", "")
SOURCE = Path(__file__).with_name("veritas_intelligence.py")
LEGACY_SQL = {
    "setup_profitability_profile": """SELECT asset,horizon,direction,total_pnl_fraction,payload
        FROM shadow_trades
        WHERE status<>'ACTIVE' AND total_pnl_fraction IS NOT NULL
          AND asset=%s AND horizon=%s AND direction=%s
        ORDER BY closed_at DESC NULLS LAST LIMIT %s""",
    "trade_path_profile": """SELECT direction,entry_price,high_price,low_price,total_pnl_fraction,payload
        FROM shadow_trades
        WHERE status<>'ACTIVE' AND total_pnl_fraction IS NOT NULL
          AND asset=%s AND horizon=%s AND direction=%s
        ORDER BY closed_at DESC NULLS LAST LIMIT %s""",
    "structure_analog_board": """SELECT d.asset,d.horizon,d.payload dp,o.payload op
        FROM ledger_events d JOIN ledger_events o
          ON o.entity_key=d.entity_key AND o.event_type='outcome'
        WHERE d.event_type='decision' ORDER BY d.event_ts DESC LIMIT %s""",
}


# Frozen before v10: compare the new record scan with the preceding compact
# SQL as well as the older full-payload query. Neither baseline reads main.
def _baseline_jsonb_object(expr, fields):
    """Project trusted SQL expressions without changing JSON object truthiness.

    Empty objects and non-objects keep their original value. A nonempty object
    remains nonempty even when its relevant fields are absent, preserving the
    existing Python ``top_level or nested`` fallback. Expressions and keys are
    internal constants, never request parameters.
    """
    pairs = ','.join(("'" + key.replace("'", "''") + "'," + value for key, value in fields.items()))
    return f"CASE WHEN jsonb_typeof({expr})='object' AND {expr}<>'{{}}'::jsonb THEN jsonb_build_object({pairs}) ELSE {expr} END"

def _baseline_analog_sql():

    def structure(expr):
        return _baseline_jsonb_object(expr, {'lifecycle': f"({expr})->'lifecycle'", 'entry_quality': f"({expr})->'entry_quality'"})

    def impulse(expr):
        return _baseline_jsonb_object(expr, {'direction': f"({expr})->'direction'", 'entry_quality': f"({expr})->'entry_quality'", 'intraday_structure': structure(f"({expr})->'intraday_structure'")})
    dp = _baseline_jsonb_object('d.payload', {'research_decision': "d.payload->'research_decision'", 'trend_impulse': impulse("d.payload->'trend_impulse'"), 'features': _baseline_jsonb_object("d.payload->'features'", {'trend_impulse': impulse("d.payload#>'{features,trend_impulse}'"), 'intraday_structure': structure("d.payload#>'{features,intraday_structure}'")})})
    op = _baseline_jsonb_object('o.payload', {'forward_return': "o.payload->'forward_return'"})
    return f"SELECT d.asset,d.horizon,{dp} dp,{op} op\n                          FROM ledger_events d JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'\n                          WHERE d.event_type='decision' ORDER BY d.event_ts DESC LIMIT %s"

BASELINE_SQL = {
    "structure_analog_board": _baseline_analog_sql(),
    "_decision_memory_rows": """WITH recent_decisions AS (
      SELECT entity_key,event_ts,asset,horizon,payload
      FROM ledger_events WHERE event_type='decision'
      ORDER BY event_ts DESC LIMIT %s
    )
    SELECT d.entity_key,d.event_ts,d.asset,d.horizon,
      COALESCE(d.payload->>'research_decision',d.payload->>'decision','NO_TRADE') research_decision,
      NULLIF(d.payload->>'confidence','')::double precision confidence,
      NULLIF(d.payload#>>'{features,ret_4h}','')::double precision ret_4h,
      NULLIF(d.payload#>>'{features,ret_24h}','')::double precision ret_24h,
      NULLIF(d.payload#>>'{features,trend}','')::double precision trend,
      NULLIF(d.payload#>>'{features,momentum}','')::double precision momentum,
      NULLIF(d.payload#>>'{features,rv}','')::double precision rv,
      NULLIF(d.payload#>>'{features,relative_volume}','')::double precision relative_volume,
      NULLIF(d.payload#>>'{features,session_efficiency}','')::double precision session_efficiency,
      NULLIF(d.payload#>>'{features,session_persistence}','')::double precision session_persistence,
      NULLIF(d.payload#>>'{features,intraday_structure_score}','')::double precision structure_score,
      NULLIF(d.payload#>>'{features,trend_onset_score}','')::double precision onset_score,
      NULLIF(d.payload#>>'{features,impulse_score}','')::double precision impulse_score,
      NULLIF(d.payload#>>'{features,near_ath}','')::double precision near_ath,
      NULLIF(d.payload#>>'{features,breakout_hold}','')::double precision breakout_hold,
      NULLIF(d.payload#>>'{features,expected_move_pct}','')::double precision expected_move_pct,
      d.payload#>>'{features,regime}' regime,
      NULLIF(o.payload->>'forward_return','')::double precision forward_return,
      NULLIF(o.payload->>'mfe','')::double precision mfe,
      NULLIF(o.payload->>'mae','')::double precision mae
    FROM recent_decisions d
    JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'
    WHERE o.payload ? 'forward_return'
    ORDER BY d.event_ts DESC""",
}

BASELINE_SQL_SHA256 = {'structure_analog_board': '1d23b5f41609ea7942baa596a187c6bf4dcf41cd240977eab5e312f0bc300010', '_decision_memory_rows': '0247c73a3f6fedbe50f9296bbe68da27e8e79bfa742144ef7a84b479ae1a07ba'}
# Exact reducer source from deployed v9 abe3a4ce, independently frozen from git.
# Hash text, then compare both ASTs under the running Python version (3.12/3.14).
BASELINE_REDUCER_SOURCE = {
    "structure_analog_board": """b={}
for r in rows:
    dp=r['dp'] if isinstance(r['dp'],dict) else json.loads(r['dp']); op=r['op'] if isinstance(r['op'],dict) else json.loads(r['op'])
    ti=dp.get('trend_impulse') or (dp.get('features') or {}).get('trend_impulse') or {}; st=ti.get('intraday_structure') or (dp.get('features') or {}).get('intraday_structure') or {}
    life=str(st.get('lifecycle') or 'NONE'); eq=str(st.get('entry_quality') or ti.get('entry_quality') or 'UNKNOWN'); direction=str(ti.get('direction') or dp.get('research_decision') or 'NO_TRADE')
    fr=op.get('forward_return')
    if life=='NONE' or direction not in ('LONG','SHORT') or fr is None: continue
    sr=float(fr) if direction=='LONG' else -float(fr)
    key=(r['asset'],r['horizon'],life,eq,direction); z=b.setdefault(key,[]); z.append(sr)
items=[]
for (asset,h,life,eq,direction),vals in b.items():
    vals=sorted(vals); n=len(vals); mean=sum(vals)/n; med=vals[n//2]
    p_hit=sum(1 for x in vals if x>0)/n
    items.append({'asset':asset,'horizon':h,'lifecycle':life,'entry_quality':eq,'direction':direction,'n':n,
                  'hit_rate':p_hit,'mean_signed_return':mean,'median_signed_return':med,
                  'status':'MEASURABLE' if n>=ANALOG_MIN_N else 'BUILDING'})
items.sort(key=lambda x:(x['status']!='MEASURABLE',-x['n']))
out={'status':'ok','min_n':ANALOG_MIN_N,'items':items}
with structure_analog_cache_lock:
    structure_analog_cache['at']=time.time(); structure_analog_cache['limit']=lim; structure_analog_cache['value']=out
return out
""",
    "_decision_memory_rows": """try:
    with pg_connect() as c:
        rows=[dict(r) for r in c.execute(sql,(DECISION_MEMORY_MAX_EPISODES,)).fetchall()]
except Exception as ex:
    emit('decision_memory_error',error=f'{type(ex).__name__}: {ex}')
    rows=[]
_decision_memory_rows._cache=(time.time(),rows)
return rows
""",
}
BASELINE_REDUCER_SOURCE_SHA256 = {
    "structure_analog_board": "774543b66dc6603cf828c215b04561f3ae680ab6b3e0259e2e89ded2a96f87f2",
    "_decision_memory_rows": "c492dd949cc716eba070bfdcdb41097dfe68b16629992937294ab98e560839a3",
}


def load_reader(name, connect, *, legacy=False, baseline=False, max_episodes=1600):
    """Keep business logic identical; replace only the historical SELECT."""
    tree = ast.parse(SOURCE.read_text())
    definitions = {node.name: node for node in tree.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    function = deepcopy(definitions[name])
    if legacy or baseline:
        statements = [node for node in ast.walk(function)
                      if isinstance(node, ast.Call)
                      and isinstance(node.func, ast.Attribute)
                      and node.func.attr == "execute"]
        if len(statements) != 1:
            raise AssertionError("Legacy query replay needs exactly one SELECT")
        statements[0].args[0] = ast.Constant(BASELINE_SQL[name] if baseline else LEGACY_SQL[name])
    helper_name = "_v90_jsonb_project_object"
    nodes = ([deepcopy(definitions[helper_name])] if helper_name in definitions else [])
    nodes.append(function)
    namespace = {
        "pg_enabled": lambda: True, "pg_connect": connect,
        "json": json, "math": math, "time": time, "VLE": VLE,
        "ANALOG_MIN_N": 8, "ANALYTICS_CACHE_SECONDS": 300,
        "structure_analog_cache": {"at": 0, "limit": 0, "value": None},
        "structure_analog_cache_lock": threading.Lock(),
        "DECISION_MEMORY_MAX_EPISODES": max_episodes,
        "DECISION_MEMORY_CACHE_SECONDS": 180,
        "emit": lambda event, **values: None,
    }
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace[name]


_RECORD_JOIN = re.compile(
    r'''(?m)^[ \t]*CROSS JOIN LATERAL jsonb_to_record\(CASE WHEN jsonb_typeof\((?P<expr>[^\r\n]+?)\)='object' '''
    r'''THEN (?P=expr) ELSE '\{\}'::jsonb END\) AS "memory_json_\d+"\('''
    r'''"(?:[^"]|"")+" jsonb(?:,"(?:[^"]|"")+" jsonb)*\)[ \t]*\n?''')


def without_projection_joins(sql):
    # Remove only a complete emitted record scan, never an ordinary join,
    # predicate, original sampling window or arbitrary lateral expression.
    return _RECORD_JOIN.sub('', sql)


def query_tail(sql):
    """The SELECT projection may change; joins/filters/order/limit may not."""
    sql = without_projection_joins(sql)
    if re.search(r'\bFROM\s*\(\s*SELECT\b', sql, re.I):
        # The analog reader selects its original joined/ordered/limited sample
        # first. Permit exactly that wrapper and its repeated descending order;
        # a different sample, extra outer condition or residual JOIN is invalid.
        compact = ' '.join(sql.split())
        wrapped = re.fullmatch(
            r'SELECT sample\.asset,sample\.horizon,.*? FROM \(SELECT '
            r'd\.asset,d\.horizon,d\.event_ts, d\.payload AS decision_payload,'
            r'o\.payload AS outcome_payload (?P<tail>FROM ledger_events d JOIN ledger_events o .*)'
            r'\) sample ORDER BY sample\.event_ts DESC', compact)
        if wrapped is None:
            raise AssertionError('Unexpected analog sample boundary, columns or outer order')
        return wrapped.group('tail')
    if re.search(r'\bFROM\s+recent_decisions\b', sql, re.I):
        cte = re.search(r'WITH\s+recent_decisions\s+AS\s*\((.*?)\)\s*SELECT', sql, re.I | re.S)
        joined = re.search(r'\bFROM\s+recent_decisions\b.*', sql, re.I | re.S)
        if cte is None:
            raise AssertionError('Decision-memory window is no longer before the outcome join')
        return (' '.join(cte.group(1).split()), ' '.join(joined.group(0).split()))
    match = re.search(r"\bFROM\s+(?:shadow_trades|ledger_events)\b", sql, re.I)
    if not match:
        raise AssertionError("Profile query no longer reads the expected ledger")
    return " ".join(sql[match.start():].split())


def capture_query(name, max_episodes=1600):
    """Execute just the real query builder and empty-result reducer, no I/O."""
    records = []
    class Connection:
        def execute(self, sql, parameters=None):
            records.append((sql, parameters))
            return self
        def fetchall(self):
            return []
    @contextmanager
    def connect():
        yield Connection()
    reader = load_reader(name, connect, max_episodes=max_episodes)
    reader(**({'force_refresh': True} if name == 'structure_analog_board' else {'force': True}))
    if len(records) != 1:
        raise AssertionError('Expected the actual reader to issue exactly one query')
    return records[0]


class RecordProjectionContractTests(unittest.TestCase):
    def test_frozen_v9_queries_and_reducers_are_independent_and_unchanged(self):
        nodes = {n.name:n for n in ast.parse(SOURCE.read_text()).body if isinstance(n, ast.FunctionDef)}
        for name, sql in BASELINE_SQL.items():
            with self.subTest(reader=name):
                self.assertEqual(hashlib.sha256(sql.encode()).hexdigest(), BASELINE_SQL_SHA256[name])
                node = nodes[name]
                start = next(i for i,n in enumerate(node.body) if (
                    isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'b' for t in n.targets)
                    if name == 'structure_analog_board' else isinstance(n, ast.Try)))
                frozen = BASELINE_REDUCER_SOURCE[name]
                self.assertEqual(hashlib.sha256(frozen.encode()).hexdigest(),
                                 BASELINE_REDUCER_SOURCE_SHA256[name])
                tail = ast.dump(ast.Module(body=node.body[start:], type_ignores=[]), include_attributes=False)
                self.assertEqual(tail, ast.dump(ast.parse(frozen), include_attributes=False))

    def test_active_queries_preserve_every_nonprojection_join_filter_order_and_window(self):
        for name, count in (('structure_analog_board', 7), ('_decision_memory_rows', 3)):
            with self.subTest(reader=name):
                sql, parameters = capture_query(name, max_episodes=37)
                self.assertEqual(query_tail(sql), query_tail(BASELINE_SQL[name]))
                self.assertEqual(len(_RECORD_JOIN.findall(sql)), count)
                self.assertEqual(sql.count('jsonb_to_record('), count)
                if name == '_decision_memory_rows':
                    self.assertEqual(parameters, (37,))
                # The comparator must still reject an altered real JOIN or
                # sample order; allowing new record scans cannot mask either.
                changed = sql.replace("o.event_type='outcome'", "o.event_type='pending'")
                self.assertNotEqual(query_tail(changed), query_tail(BASELINE_SQL[name]))
                changed = sql.replace('ORDER BY event_ts DESC LIMIT', 'ORDER BY event_ts ASC LIMIT')
                if name == '_decision_memory_rows':
                    self.assertNotEqual(query_tail(changed), query_tail(BASELINE_SQL[name]))
                else:
                    for old, new in (("d.event_type='decision'", "d.event_type='pending'"),
                            ('LIMIT %s', 'LIMIT 1'),
                            ('ORDER BY d.event_ts DESC', 'ORDER BY d.event_ts ASC'),
                            ('ORDER BY sample.event_ts DESC', 'ORDER BY sample.event_ts ASC'),
                            ('d.payload AS decision_payload', 'o.payload AS decision_payload'),
                            ("THEN sample.decision_payload ELSE", "THEN sample.outcome_payload ELSE")):
                        with self.subTest(mutation=old):
                            changed = sql.replace(old, new)
                            self.assertNotEqual(changed, sql)
                            try:
                                observed = query_tail(changed)
                            except AssertionError:
                                continue
                            self.assertNotEqual(observed, query_tail(BASELINE_SQL[name]))


class RecordedCursor:
    def __init__(self, cursor, record):
        self.cursor, self.record = cursor, record

    def fetchall(self):
        rows = self.cursor.fetchall()
        self.record["rows"] = rows
        self.record["decoded_json_bytes"] = len(json.dumps(
            rows, ensure_ascii=False, default=str).encode("utf-8"))
        return rows


class RecordedConnection:
    def __init__(self, connection, records):
        self.connection, self.records = connection, records

    def execute(self, sql, parameters=None):
        record = {"sql": sql, "parameters": parameters}
        self.records.append(record)
        return RecordedCursor(self.connection.execute(sql, parameters), record)


@unittest.skipUnless(DSN, "isolated PostgreSQL test database not configured")
class MemoryProjectionSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg.rows import dict_row
        cls.driver = psycopg
        cls.row_factory = staticmethod(dict_row)
        cls.schema = "memory_projection_test_" + uuid.uuid4().hex
        with psycopg.connect(DSN) as c:
            if c.execute("SELECT current_database()").fetchone()[0] != "veritas_quality_test":
                raise RuntimeError("Refusing writes outside veritas_quality_test")
            c.execute(f"CREATE SCHEMA {cls.schema}")
            cls.addClassCleanup(cls.drop_schema)
            c.execute(f"SET search_path TO {cls.schema}")
            c.execute("""CREATE TABLE shadow_trades(
                trade_id text PRIMARY KEY, asset text, horizon text, direction text,
                status text, closed_at timestamptz, total_pnl_fraction float8,
                entry_price float8, high_price float8, low_price float8, payload jsonb)""")
            c.execute("""CREATE TABLE ledger_events(
                id bigserial PRIMARY KEY, entity_key text, event_type text,
                event_ts timestamptz, asset text, horizon text, payload jsonb)""")

    @classmethod
    def drop_schema(cls):
        with cls.driver.connect(DSN) as c:
            c.execute(f"DROP SCHEMA IF EXISTS {cls.schema} CASCADE")

    @contextmanager
    def connect(self):
        with self.driver.connect(DSN, row_factory=self.row_factory) as c:
            c.execute(f"SET search_path TO {self.schema}")
            yield c

    def setUp(self):
        with self.connect() as c:
            c.execute("TRUNCATE shadow_trades, ledger_events")
        self.started = datetime(2026, 10, 1, tzinfo=timezone.utc)

    def run_reader(self, name, *args, legacy=False, baseline=False, max_episodes=1600, **kwargs):
        records = []

        @contextmanager
        def recorded_connect():
            with self.connect() as c:
                yield RecordedConnection(c, records)

        result = load_reader(name, recorded_connect, legacy=legacy, baseline=baseline,
                             max_episodes=max_episodes)(*args, **kwargs)
        self.assertEqual(len(records), 1)
        self.assertIn("rows", records[0], result)
        expected = BASELINE_SQL[name] if name in BASELINE_SQL else LEGACY_SQL[name]
        self.assertEqual(query_tail(records[0]["sql"]), query_tail(expected))
        return result, records[0]

    def assert_parity(self, name, *args, **kwargs):
        original, old_query = self.run_reader(name, *args, legacy=True, **kwargs)
        projected, new_query = self.run_reader(name, *args, **kwargs)
        self.assertEqual(projected, original)
        self.assertNotEqual(str(original.get("status")).upper(), "ERROR", original)
        self.assertEqual(new_query["parameters"], old_query["parameters"])
        self.assertEqual(len(new_query["rows"]), len(old_query["rows"]))
        # Exact row sequence for all unchanged scalar columns, not just counts
        # or an aggregate that could accidentally hide reordered LIMIT samples.
        scalar = lambda rows: [{k: v for k, v in r.items() if k not in ("payload", "dp", "op")}
                               for r in rows]
        self.assertEqual(scalar(new_query["rows"]), scalar(old_query["rows"]))
        return projected, old_query, new_query

    def add_shadow(self, key, payload, *, direction="LONG", asset="MOEX", horizon="1h",
                   status="CLOSED", pnl=.01, entry=100., high=104., low=98., null_time=False):
        with self.connect() as c:
            c.execute("""INSERT INTO shadow_trades VALUES
                (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                (str(key), asset, horizon, direction, status,
                 None if null_time else self.started + timedelta(minutes=int(key)),
                 pnl, entry, high, low, json.dumps(payload)))

    def add_analog(self, key, decision, outcome, *, asset="MOEX", horizon="1h",
                   decision_type="decision", outcome_type="outcome"):
        at = self.started + timedelta(minutes=int(key))
        with self.connect() as c:
            for kind, payload in ((decision_type, decision), (outcome_type, outcome)):
                c.execute("""INSERT INTO ledger_events
                    (entity_key,event_type,event_ts,asset,horizon,payload)
                    VALUES(%s,%s,%s,%s,%s,%s::jsonb)""",
                    (str(key), kind, at, asset, horizon, json.dumps(payload)))

    def seed_shadow_legacy(self):
        shapes = [
            {"setup": "BREAKOUT", "regime": "UP"},
            {"trade_plan_setup": "BREAKOUT", "regime": "UP"},
            {"entry_setup": "BREAKOUT"},
            {"setup": "", "trade_plan_setup": "BREAKOUT", "entry_setup": "OTHER", "regime": None},
            {"setup": None, "trade_plan_setup": "", "entry_setup": "BREAKOUT", "regime": ""},
            {"setup": False, "trade_plan_setup": 0, "entry_setup": "BREAKOUT"},
            {"setup": [], "trade_plan_setup": {}, "entry_setup": "BREAKOUT"},
            {"setup": "OTHER", "regime": "UP"},
            {"setup": "BREAKOUT", "regime": "DOWN"},
            {}, None, [], "invalid legacy encoding",
            json.dumps({"entry_setup": "BREAKOUT", "regime": "UP"}),
            {"setup": True, "entry_setup": "BREAKOUT", "regime": 1},
        ]
        for direction_index, direction in enumerate(("LONG", "SHORT")):
            for i in range(45):
                self.add_shadow(1000 * direction_index + i, shapes[i % len(shapes)],
                    direction=direction, pnl=(i % 7 - 2) / 1000.,
                    entry=0. if i == 1 else 100., high=None if i == 2 else 104.,
                    low=None if i == 3 else 98., null_time=i == 4)
        # Newest excluded records must not consume the qualifying sample limit.
        self.add_shadow(9001, {}, status="ACTIVE")
        self.add_shadow(9002, {}, pnl=None)
        self.add_shadow(9003, {}, asset="GOLD")
        self.add_shadow(9004, {}, horizon="5m")
        self.add_shadow(9005, {}, direction="NO_TRADE")

    def test_profitability_keeps_legacy_setup_priority_regime_and_sample_order(self):
        self.seed_shadow_legacy()
        for direction in ("LONG", "SHORT"):
            for setup, regime in (("BREAKOUT", "UP"), ("BREAKOUT", None), (None, None),
                                  ("OTHER", "UP"), ("True", "1")):
                for limit in (1, 7, 240):
                    with self.subTest(direction=direction, setup=setup, regime=regime, limit=limit):
                        self.assert_parity("setup_profitability_profile", "MOEX", "1h",
                                           direction, setup, regime, limit=limit)

    def test_trade_paths_keep_setup_priority_both_directions_and_observed_extrema(self):
        self.seed_shadow_legacy()
        for direction in ("LONG", "SHORT"):
            for setup in ("BREAKOUT", "OTHER", None, "True"):
                for limit in (1, 9, 300):
                    with self.subTest(direction=direction, setup=setup, limit=limit):
                        self.assert_parity("trade_path_profile", "MOEX", "1h",
                                           direction, setup, limit=limit)

    def seed_analog_fallback(self):
        features = {"trend_impulse": {"direction": "SHORT", "entry_quality": "FEATURE_TI",
                    "intraday_structure": {"lifecycle": "FEATURE_TI_LIFE"}},
                    "intraday_structure": {"lifecycle": "FEATURE_ST_LIFE", "entry_quality": "FEATURE_ST"}}
        shapes = [
            {"trend_impulse": {"direction": "LONG", "entry_quality": "TOP_TI",
             "intraday_structure": {"lifecycle": "TOP_LIFE", "entry_quality": "TOP_ST"}}},
            {"features": features},
            {"trend_impulse": None, "features": features},
            {"trend_impulse": {}, "features": features},
            {"trend_impulse": {"unrelated": 1}, "features": features, "research_decision": "LONG"},
            {"trend_impulse": {"direction": "SHORT", "intraday_structure": {}}, "features": features},
            {"trend_impulse": {"direction": "LONG", "intraday_structure": {"unrelated": 1}}, "features": features},
            {"trend_impulse": {"direction": "", "entry_quality": "", "intraday_structure":
             {"lifecycle": "EMPTY_SCALARS", "entry_quality": ""}}, "research_decision": "SHORT"},
            {"trend_impulse": {"direction": "LONG", "entry_quality": "TI_FALLBACK", "intraday_structure":
             {"lifecycle": "FALSEY_QUALITY", "entry_quality": 0}}},
            {"trend_impulse": [], "features": features},
            {"trend_impulse": False, "features": features},
            {"trend_impulse": 0, "features": features},
            {"trend_impulse": "", "features": features},
            {"features": {}, "research_decision": "LONG"},
            {"trend_impulse": {"direction": "LONG", "intraday_structure": None}, "features": features},
            {"trend_impulse": {"direction": "LONG", "intraday_structure": False}, "features": features},
        ]
        for i in range(80):
            self.add_analog(i, shapes[i % len(shapes)],
                            {"forward_return": (i % 7 - 3) / 1000.})
        # A string root historically decoded one additional time by Python.
        self.add_analog(90, json.dumps(shapes[0]), json.dumps({"forward_return": "0.0123"}))
        self.add_analog(91, shapes[0], {"forward_return": None})
        self.add_analog(92, shapes[0], {"not_forward_return": .2})
        self.add_analog(93, shapes[0], {"forward_return": 0.})
        self.add_analog(100, shapes[0], {"forward_return": 5.}, decision_type="meta_signal")
        self.add_analog(101, shapes[0], {"forward_return": 5.}, outcome_type="pending")

    def test_analog_fallback_preserves_empty_falsey_and_nonempty_unrelated_objects(self):
        self.seed_analog_fallback()
        for limit in (1, 8, 31, 1200):
            with self.subTest(limit=limit):
                result, _, _ = self.assert_parity("structure_analog_board", limit=limit, force_refresh=True)
                self.assertEqual(result["status"], "ok")
        self.assertGreater(len(result["items"]), 3)
        group = next(x for x in result["items"] if x["lifecycle"] == "FEATURE_ST_LIFE"
                     and x["direction"] == "LONG")
        self.assertGreater(group["n"], 0)
        self.assertNotIn("NONE", [x["lifecycle"] for x in result["items"]])

    def assert_record_parity(self, name, **kwargs):
        before, old = self.run_reader(name, baseline=True, **kwargs)
        after, new = self.run_reader(name, **kwargs)
        self.assertEqual(new['parameters'], old['parameters'])
        self.assertEqual(new['rows'], old['rows'])
        self.assertEqual(after, before)
        return after, new

    def test_analog_records_match_frozen_v9_rows_reducer_and_sample_order_exactly(self):
        self.seed_analog_fallback()
        for limit in (1, 8, 31, 1200):
            with self.subTest(limit=limit):
                result, _ = self.assert_record_parity('structure_analog_board',
                                                       limit=limit, force_refresh=True)
                self.assertEqual(result['status'], 'ok')
        self.assertGreater(len(result['items']), 3)

    def test_analog_record_root_shapes_preserve_sql_rows_including_invalid_reducer_inputs(self):
        # The projection must not turn historically invalid roots into a
        # valid-looking observation. Reducer AST equality is checked separately.
        roots = (None, {}, [], [1], False, 0, '', 'bad', json.dumps({'research_decision': 'LONG'}))
        for i, value in enumerate(roots):
            self.add_analog(i, value, value)
        with self.connect() as c:
            c.execute("UPDATE ledger_events SET payload=NULL WHERE entity_key='0'")
            for name in ('structure_analog_board', '_decision_memory_rows'):
                sql, parameters = capture_query(name)
                old = c.execute(BASELINE_SQL[name], parameters).fetchall()
                new = c.execute(sql, parameters).fetchall()
                self.assertEqual(new, old)
                if name == 'structure_analog_board':
                    self.assertEqual(len(new), len(roots))

    def test_decision_records_preserve_json_scalar_text_and_numeric_null_empty_values(self):
        numeric = ('ret_4h', 'ret_24h', 'trend', 'momentum', 'rv', 'relative_volume',
                   'session_efficiency', 'session_persistence', 'intraday_structure_score',
                   'trend_onset_score', 'impulse_score', 'near_ath', 'breakout_hold', 'expected_move_pct')
        choices = (None, '', 0, 1.25, '-2.5e-3')
        for i in range(10):
            fields = {key: choices[(i+j) % len(choices)] for j,key in enumerate(numeric)}
            fields['regime'] = (None, '', False, 0, {}, [], {'unrelated': 1}, 'UP', True, ['DOWN'])[i]
            decision = {'research_decision': (None, '', False, 0, {}, [], 'SHORT', 'LONG', True, 'NO_TRADE')[i],
                        'decision': 'LONG', 'confidence': choices[i % len(choices)], 'features': fields}
            self.add_analog(i, decision, {'forward_return': choices[i % len(choices)],
                                         'mfe': '', 'mae': None})
        for i, root in enumerate(({}, None, [], False, 0, '', 'bad'), 20):
            self.add_analog(i, {'features': root}, {'forward_return': 0.})
        self.add_analog(30, {}, {'forward_return': None})
        self.add_analog(31, None, {'forward_return': ''})
        rows, _ = self.assert_record_parity('_decision_memory_rows', force=True)
        by_key = {row['entity_key']: row for row in rows}
        self.assertEqual(len(rows), 19)
        self.assertEqual(by_key['0']['research_decision'], 'LONG')
        self.assertEqual(by_key['1']['research_decision'], '')
        self.assertEqual(by_key['2']['research_decision'], 'false')
        self.assertEqual(by_key['4']['research_decision'], '{}')
        self.assertIsNone(by_key['30']['forward_return'])
        self.assertIsNone(by_key['31']['confidence'])
        self.assertEqual([r['event_ts'] for r in rows], sorted((r['event_ts'] for r in rows), reverse=True))

    def test_decision_window_precedes_join_and_retains_outcome_presence_and_multiplicity(self):
        for i in range(1, 5):
            self.add_analog(i, {'decision': 'LONG'}, {'forward_return': i/100.})
        self.add_analog(5, {'decision': 'LONG'}, {'unrelated': True})
        self.add_analog(6, {'decision': 'LONG'}, {}, outcome_type='pending')
        self.add_analog(99, {}, {'forward_return': 9.}, decision_type='meta_signal')
        with self.connect() as c:
            c.execute("""INSERT INTO ledger_events(entity_key,event_type,event_ts,asset,horizon,payload)
                SELECT entity_key,event_type,event_ts,asset,horizon,payload FROM ledger_events
                WHERE entity_key='4' AND event_type='outcome'""")
        for limit, keys in ((2, []), (3, ['4', '4']), (4, ['4', '4', '3']),
                            (20, ['4', '4', '3', '2', '1'])):
            with self.subTest(limit=limit):
                rows, _ = self.assert_record_parity('_decision_memory_rows',
                                                     max_episodes=limit, force=True)
                self.assertEqual([row['entity_key'] for row in rows], keys)

    def test_numeric_cast_errors_keep_sqlstate_and_existing_empty_error_cache_behavior(self):
        self.add_analog(1, {}, {'forward_return': .01})
        sql, parameters = capture_query('_decision_memory_rows')
        for location in ('confidence', 'ret_4h', 'forward_return'):
            for value in (True, [], {'bad': 1}, 'not-a-number', ' '):
                with self.subTest(field=location, value=value):
                    decision, outcome = {}, {'forward_return': .01}
                    if location == 'confidence': decision['confidence'] = value
                    elif location == 'ret_4h': decision['features'] = {'ret_4h': value}
                    else: outcome['forward_return'] = value
                    with self.connect() as c:
                        for kind, payload in (('decision', decision), ('outcome', outcome)):
                            c.execute('UPDATE ledger_events SET payload=%s::jsonb WHERE event_type=%s',
                                      (json.dumps(payload), kind))
                    errors = []
                    for query in (BASELINE_SQL['_decision_memory_rows'], sql):
                        with self.assertRaises(self.driver.errors.InvalidTextRepresentation) as caught:
                            with self.connect() as c:
                                c.execute(query, parameters).fetchall()
                        errors.append(caught.exception.sqlstate)
                    self.assertEqual(errors, ['22P02', '22P02'])
        for baseline in (True, False):
            events = []
            reader = load_reader('_decision_memory_rows', self.connect, baseline=baseline)
            reader.__globals__['emit'] = lambda event, **values: events.append((event, values))
            self.assertEqual(reader(force=True), [])
            self.assertEqual(reader._cache[1], [])
            self.assertEqual(events[0][0], 'decision_memory_error')
            self.assertTrue(events[0][1]['error'].startswith('InvalidTextRepresentation:'))

    def test_record_scans_execute_once_without_sort_retaining_expanded_proof_objects(self):
        proof = {'history': [hashlib.sha256(str(i).encode()).hexdigest() for i in range(1500)]}
        structure = dict(proof, lifecycle='BREAKOUT', entry_quality='FRESH')
        impulse = dict(proof, direction='LONG', entry_quality='FRESH', intraday_structure=structure)
        features = dict(proof, ret_4h=.01, trend_impulse=impulse, intraday_structure=structure)
        self.add_analog(1, dict(proof, research_decision='LONG', features=features, trend_impulse=impulse),
                        dict(proof, forward_return=.02, mfe=.03, mae=-.01))
        with self.connect() as c:
            before = c.execute('SELECT id,md5(payload::text) AS digest FROM ledger_events ORDER BY id').fetchall()
        for name, count in (('structure_analog_board', 7), ('_decision_memory_rows', 3)):
            with self.subTest(reader=name):
                kwargs = {'force_refresh': True} if name == 'structure_analog_board' else {'force': True}
                _, query = self.assert_record_parity(name, **kwargs)
                self.assertNotIn(proof['history'][0], json.dumps(query['rows'], default=str))
                with self.connect() as c:
                    plan = c.execute('EXPLAIN (ANALYZE,VERBOSE,FORMAT JSON) '+query['sql'],
                                     query['parameters']).fetchone()['QUERY PLAN'][0]['Plan']
                pending = [plan]; scans = []; sorts = []
                while pending:
                    node = pending.pop(); pending.extend(node.get('Plans', []))
                    if node.get('Node Type') == 'Function Scan' and node.get('Function Name') == 'jsonb_to_record':
                        scans.append(node)
                    if node.get('Node Type') in ('Sort', 'Incremental Sort'):
                        sorts.append(node)
                self.assertEqual(len(scans), count)
                self.assertTrue(all(n['Actual Loops'] == 1 and n['Actual Rows'] == 1 for n in scans))
                self.assertTrue(sorts, 'Fixture must exercise the actual ordered query')
                # Original d.payload/o.payload may be cheap TOAST references.
                # Standalone expanded objects would keep full proof graphs alive
                # through Sort and defeat the intended database-memory boundary.
                retained = [(n['Node Type'], item) for n in sorts for item in n.get('Output', [])
                    if re.fullmatch(r'memory_json_\d+\.(features|trend_impulse|intraday_structure)',
                                    item.replace('"', '').strip('()'))]
                self.assertEqual(retained, [], 'Sort retains expanded record objects: '+repr(retained))
        with self.connect() as c:
            after = c.execute('SELECT id,md5(payload::text) AS digest FROM ledger_events ORDER BY id').fetchall()
        self.assertEqual(after, before)

    def test_bulky_histories_stay_in_database_and_returned_bytes_drop_substantially(self):
        history = [{"ts": i * 60, "open": 100., "high": 101., "low": 99., "close": 100.5,
                    "volume": i + 100} for i in range(256)]
        bulky = {"candles": history, "provider_response": "provider-observation:" * 2500}
        for i in range(36):
            self.add_shadow(i, dict(bulky, setup="BREAKOUT", regime="UP"),
                            pnl=(i % 7 - 2) / 1000.)
            decision = dict(bulky, trend_impulse={"direction": "LONG", "entry_quality": "FRESH",
                "intraday_structure": dict(bulky, lifecycle="UP", entry_quality="FRESH")},
                features=dict(bulky, trend_impulse=dict(bulky, direction="SHORT")))
            self.add_analog(i, decision, dict(bulky, forward_return=(i % 7 - 2) / 1000.))
        with self.connect() as c:
            before = c.execute("""SELECT (SELECT sum(octet_length(payload::text)) FROM shadow_trades) AS shadow_bytes,
                (SELECT sum(octet_length(payload::text)) FROM ledger_events) AS ledger_bytes""").fetchone()
        calls = [
            ("setup_profitability_profile", ("MOEX", "1h", "LONG", "BREAKOUT", "UP"), {}),
            ("trade_path_profile", ("MOEX", "1h", "LONG", "BREAKOUT"), {}),
            ("structure_analog_board", (), {"force_refresh": True}),
        ]
        for name, args, kwargs in calls:
            with self.subTest(reader=name):
                _, old, projected = self.assert_parity(name, *args, **kwargs)
                self.assertGreater(old["decoded_json_bytes"], 1_000_000)
                self.assertLess(projected["decoded_json_bytes"], old["decoded_json_bytes"] * .10)
                self.assertNotIn("provider-observation:", json.dumps(projected["rows"]))
        with self.connect() as c:
            after = c.execute("""SELECT (SELECT sum(octet_length(payload::text)) FROM shadow_trades) AS shadow_bytes,
                (SELECT sum(octet_length(payload::text)) FROM ledger_events) AS ledger_bytes""").fetchone()
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
