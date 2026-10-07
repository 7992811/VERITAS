"""Real PostgreSQL parity for bounded legacy diagnostic memory reads.

Load only the three pure readers (and their SQL projection helper), never the
service module or its startup/background workers.  Each reader runs against the
same isolated records twice: once with its historical full-payload query, once
with the production query.  The unchanged reducer must produce identical
results, including legacy Python truthiness and sample-before-filter behavior.
"""
import ast
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


def load_reader(name, connect, *, legacy=False):
    """Keep business logic identical; replace only the historical SELECT."""
    tree = ast.parse(SOURCE.read_text())
    definitions = {node.name: node for node in tree.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    function = deepcopy(definitions[name])
    if legacy:
        statements = [node for node in ast.walk(function)
                      if isinstance(node, ast.Call)
                      and isinstance(node.func, ast.Attribute)
                      and node.func.attr == "execute"]
        if len(statements) != 1:
            raise AssertionError("Legacy query replay needs exactly one SELECT")
        statements[0].args[0] = ast.Constant(LEGACY_SQL[name])
    helper_name = "_v90_jsonb_project_object"
    nodes = ([deepcopy(definitions[helper_name])] if helper_name in definitions else [])
    nodes.append(function)
    namespace = {
        "pg_enabled": lambda: True, "pg_connect": connect,
        "json": json, "math": math, "time": time, "VLE": VLE,
        "ANALOG_MIN_N": 8, "ANALYTICS_CACHE_SECONDS": 300,
        "structure_analog_cache": {"at": 0, "limit": 0, "value": None},
        "structure_analog_cache_lock": threading.Lock(),
    }
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace[name]


def query_tail(sql):
    """The SELECT projection may change; joins/filters/order/limit may not."""
    match = re.search(r"\bFROM\s+(?:shadow_trades|ledger_events)\b", sql, re.I)
    if not match:
        raise AssertionError("Profile query no longer reads the expected ledger")
    return " ".join(sql[match.start():].split())


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
        cls.driver, cls.row_factory = psycopg, dict_row
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

    def run_reader(self, name, *args, legacy=False, **kwargs):
        records = []

        @contextmanager
        def recorded_connect():
            with self.connect() as c:
                yield RecordedConnection(c, records)

        result = load_reader(name, recorded_connect, legacy=legacy)(*args, **kwargs)
        self.assertEqual(len(records), 1)
        self.assertIn("rows", records[0], result)
        self.assertEqual(query_tail(records[0]["sql"]), query_tail(LEGACY_SQL[name]))
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

    def test_analog_fallback_preserves_empty_falsey_and_nonempty_unrelated_objects(self):
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
        for limit in (1, 8, 31, 1200):
            with self.subTest(limit=limit):
                result, _, _ = self.assert_parity("structure_analog_board", limit=limit, force_refresh=True)
                self.assertEqual(result["status"], "ok")
        self.assertGreater(len(result["items"]), 3)
        group = next(x for x in result["items"] if x["lifecycle"] == "FEATURE_ST_LIFE"
                     and x["direction"] == "LONG")
        self.assertGreater(group["n"], 0)
        self.assertNotIn("NONE", [x["lifecycle"] for x in result["items"]])

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
