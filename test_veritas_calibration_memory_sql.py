"""Calibration sample/math parity and bounded SQL reads, without service startup.

The legacy comparison changes only the reader back to its full-payload SELECT;
both runs execute the current, unchanged calibration reducer. SQL fixtures are
restricted to an explicitly named test database and a unique temporary schema.
"""
import ast
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import unittest
import uuid

from test_veritas_snapshot_memory_sql import MemoryConnection, agent_query_contract, assert_record_plan
from test_veritas_trend_memory_sql import ReadTrace


DSN = os.getenv("VERITAS_QUALITY_TEST_DSN", "")
RUNTIME = Path(__file__).with_name("veritas_intelligence.py")
LEGACY_SQL = """WITH recent_decisions AS (
    SELECT entity_key,event_ts,asset,horizon,payload
    FROM ledger_events WHERE event_type='decision'
    ORDER BY event_ts DESC LIMIT %s
  )
  SELECT d.asset,d.horizon,d.payload AS decision_payload,o.payload AS outcome_payload
  FROM recent_decisions d
  JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'"""


def load_reader(connect, *, legacy=False, limit=5000, enabled=True):
    tree = ast.parse(RUNTIME.read_text())
    node = deepcopy(next(n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "pg_calibration_map"))
    if legacy:
        stream = next(n for n in node.body if isinstance(n, ast.With))
        # Recreate the original full-payload read while keeping all grouping,
        # probability, Brier and Wilson computations in the same reducer.
        replacement = ast.parse("""with pg_connect() as c:
    rows = c.execute(LEGACY_SQL, (LIVE_LEARNING_MAX_EPISODES,)).fetchall()
""").body[0]
        replacement.body.extend(stream.body)
        node.body[node.body.index(stream)] = replacement
    ns = {"pg_connect": connect, "pg_enabled": lambda: enabled,
          "LIVE_LEARNING_MAX_EPISODES": limit, "LEGACY_SQL": LEGACY_SQL,
          "json": json, "math": math}
    unit = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    exec(compile(unit, str(RUNTIME), "exec"), ns)
    return ns["pg_calibration_map"]


def sample_row(index):
    return {"asset": ("BTC", "ETH", "CNYRUBF")[index % 3],
            "horizon": ("1m", "5m", "1h")[index % 3],
            "decision_payload": {"decision": ("LONG", "SHORT", "NO_TRADE")[index % 3],
                                 "confidence": (index % 8) / 8},
            "outcome_payload": {"forward_return": ((index % 5) - 2) / 128}}


class CalibrationStreamTests(unittest.TestCase):
    def test_stream_keeps_calibration_math_and_closes_owned_resources(self):
        original = MemoryConnection([sample_row(i) for i in range(257)])
        streamed = MemoryConnection(sample_row(i) for i in range(257))
        expected = load_reader(original.connect, legacy=True)()
        actual = load_reader(streamed.connect)()
        self.assertEqual(actual, expected)
        self.assertTrue(actual)
        self.assertEqual(streamed.parameters, (5000,))
        self.assertEqual(agent_query_contract(streamed.sql), agent_query_contract(LEGACY_SQL))
        self.assertEqual(streamed.server_cursor.name, "veritas_calibration")
        self.assertEqual(streamed.server_cursor.itersize, 64)
        self.assertTrue(streamed.server_cursor.closed)
        self.assertTrue(streamed.closed)
        self.assertFalse(streamed.in_transaction)
        self.assertEqual(streamed.sql.count('jsonb_to_record('), 1)
        self.assertIn('"decision" jsonb,"confidence" jsonb', streamed.sql)
        self.assertIn("o.payload->'forward_return'", streamed.sql)
        self.assertLessEqual(streamed.sql.count('d.payload'), 5)
        self.assertLessEqual(streamed.sql.count('o.payload'), 5)
        self.assertNotIn("d.payload AS decision_payload", streamed.sql)

    def test_reducer_error_releases_stream_and_does_not_return_partial_learning(self):
        invalid = sample_row(0)
        invalid["decision_payload"]["confidence"] = "invalid recorded confidence"
        connection = MemoryConnection([sample_row(1), invalid])
        with self.assertRaises(ValueError):
            load_reader(connection.connect)()
        self.assertTrue(connection.server_cursor.closed)
        self.assertTrue(connection.closed)
        self.assertFalse(connection.in_transaction)

    def test_disabled_database_has_no_reader_side_effect(self):
        def unavailable():
            raise AssertionError("disabled calibration must not open a connection")
        self.assertEqual(load_reader(unavailable, enabled=False)(), [])


@unittest.skipUnless(DSN, "isolated PostgreSQL test database not configured")
class CalibrationProjectionSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg.rows import dict_row
        cls.driver = psycopg
        cls.row_factory = staticmethod(dict_row)
        cls.schema = "calibration_memory_test_" + uuid.uuid4().hex
        with psycopg.connect(DSN) as c:
            if c.execute("SELECT current_database()").fetchone()[0] != "veritas_quality_test":
                raise RuntimeError("Refusing writes outside veritas_quality_test")
            c.execute(f"CREATE SCHEMA {cls.schema}")
            cls.addClassCleanup(cls.drop_schema)
            c.execute(f"SET search_path TO {cls.schema}")
            c.execute("""CREATE TABLE ledger_events(
                entity_key text,event_type text,event_ts timestamptz,
                asset text,horizon text,payload jsonb)""")

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
        start = datetime(2026, 10, 7, tzinfo=timezone.utc)
        pending = []
        for i in range(180):
            row = sample_row(i)
            dp, op = row["decision_payload"], row["outcome_payload"]
            # Large sealed execution evidence is irrelevant to calibration.
            dp["UNUSED_HEAVY_FIELD"] = {"atr_proof": "candle-evidence" * 1500,
                                         "target_zones": [list(range(100))] * 8}
            op["UNUSED_HEAVY_FIELD"] = "retained-outcome-evidence" * 500
            if i % 11 == 0:
                dp.pop("confidence")
            if i % 13 == 0:
                op["forward_return"] = None
            if i % 17 == 0:
                dp["confidence"] = "0.75"  # Preserve existing numeric-string handling.
            for kind, payload in (("decision", dp),
                                  ("pending" if i % 7 == 0 else "outcome", op)):
                pending.append((str(i), kind, start + timedelta(seconds=i),
                                row["asset"], row["horizon"], json.dumps(payload)))
        with self.connect() as c:
            c.execute("TRUNCATE ledger_events")
            with c.transaction():
                with c.cursor() as rows:
                    rows.executemany("INSERT INTO ledger_events VALUES(%s,%s,%s,%s,%s,%s::jsonb)", pending)

    def run_reader(self, *, legacy=False, limit=5000):
        trace = ReadTrace(self.connect)
        result = load_reader(trace.connect, legacy=legacy, limit=limit)()
        self.assertEqual(len(trace.queries), 1)
        return result, trace

    def test_projection_preserves_window_before_join_sample_and_all_calibration_metrics(self):
        for limit in (1, 17, 129, 5000):
            with self.subTest(limit=limit):
                old, old_trace = self.run_reader(legacy=True, limit=limit)
                new, new_trace = self.run_reader(limit=limit)
                self.assertEqual(new, old)
                before, after = old_trace.queries[0], new_trace.queries[0]
                self.assertEqual(agent_query_contract(after["sql"]), agent_query_contract(before["sql"]))
                self.assertEqual(after["parameters"], before["parameters"])
                self.assertEqual(after["rows"], before["rows"])
                self.assertCountEqual(after["keys"], before["keys"])
                self.assertTrue(before["heavy"])
                self.assertFalse(after["heavy"])
                self.assertLess(after["bytes"], before["bytes"] / 40)
                self.assertTrue(after["server"])
                self.assertEqual(new_trace.cursors[0].itersize, 64)
                self.assertTrue(new_trace.cursors[0].cursor.closed)
                self.assertTrue(all(c.closed for c in new_trace.connections))

    def test_interrupted_server_read_closes_transaction_and_can_retry(self):
        trace = ReadTrace(self.connect)
        trace.fail_after = 3
        with self.assertRaisesRegex(RuntimeError, "interrupted memory stream"):
            load_reader(trace.connect)()
        self.assertEqual(trace.transactions, 0)
        self.assertTrue(trace.cursors[0].cursor.closed)
        self.assertTrue(all(c.closed for c in trace.connections))
        expected, _ = self.run_reader(legacy=True)
        retry, _ = self.run_reader()
        self.assertEqual(retry, expected)

    def test_large_toasted_decision_uses_one_record_scan_and_preserves_calibration(self):
        with self.connect() as c:
            c.execute("""UPDATE ledger_events SET payload=payload||jsonb_build_object(
                'UNUSED_HEAVY_FIELD',repeat('sealed-candle-proof:',80000))
                WHERE entity_key='178' AND event_type='decision'""")
            size = c.execute("""SELECT pg_column_size(payload) AS stored,
                octet_length(payload::text) AS expanded FROM ledger_events
                WHERE entity_key='178' AND event_type='decision'""").fetchone()
        self.assertGreater(size['expanded'],1_000_000)
        self.assertLess(size['stored'],size['expanded']/8)
        old, old_trace = self.run_reader(legacy=True)
        actual, trace = self.run_reader()
        self.assertEqual(actual,old)
        query = trace.queries[0]
        self.assertEqual(query['rows'],old_trace.queries[0]['rows'])
        self.assertEqual(agent_query_contract(query['sql']),agent_query_contract(LEGACY_SQL))
        self.assertFalse(query['heavy'])
        with self.connect() as c:
            assert_record_plan(self,c,query['sql'],query['parameters'],1,180)


if __name__ == "__main__":
    unittest.main()
