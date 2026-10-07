"""Frozen AMI sample/chunk contracts and isolated PostgreSQL parity tests."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
import unittest
import uuid

import veritas_learning_memory as M
from test_veritas_snapshot_memory_sql import _V9_AMI_PROJECTED_SQL
from test_veritas_trend_memory_sql import LEGACY_AMI_QUERY, tail


class Capture:
    def __init__(self, rows=()):
        self.rows, self.calls = list(rows), []

    def execute(self, sql, parameters=None):
        self.calls.append((sql, parameters))
        return self

    def fetchall(self):
        return list(self.rows)


class AMIStagedContractTests(unittest.TestCase):
    def test_metadata_sample_preserves_original_join_filter_order_and_limit(self):
        c = Capture()
        self.assertEqual(M.ami_decision_sample(c), [])
        query = c.calls[0][0]
        self.assertEqual(tail(query), tail(LEGACY_AMI_QUERY))
        self.assertNotIn("d.payload", query)
        self.assertNotIn("DISTINCT", query)
        self.assertIn("d.id AS decision_id,o.id AS outcome_id,d.event_ts", query)

    def test_chunk_accepts_compact_or_metadata_pairs_and_preserves_duplicates(self):
        c = Capture([{}, {}, {}])
        pairs = [{"decision_id": 3, "outcome_id": 7, "event_ts": "ignored"}, [2, 8], (3, 7)]
        M.ami_decision_chunk(c, pairs)
        sql, params = c.calls[0]
        self.assertEqual(params, ([3, 2, 3], [7, 8, 7]))
        self.assertIn("WITH ORDINALITY", sql)
        self.assertIn("ORDER BY sample.ordinality", sql)
        self.assertNotIn("LIMIT", sql)
        dp, op, joins = M._ami_projection_sql()
        self.assertIn(dp+" AS dp,"+op+" AS op", sql)
        self.assertIn(joins, sql)

    def test_invalid_or_oversized_chunk_never_executes_sql(self):
        c = Capture()
        self.assertEqual(M.ami_decision_chunk(c, []), [])
        for pairs in (None, iter([]), [[1, 2]]*65, [[True, 2]], [[1, 0]], [[-1, 2]],
                      [[2**63, 2]], [[1., 2]], [["1", 2]], [[1]], [[1, 2, 3]],
                      [{"decision_id": 1}], [{}]):
            with self.subTest(pairs=str(pairs)[:60]), self.assertRaises(ValueError):
                M.ami_decision_chunk(c, pairs)
        self.assertEqual(c.calls, [])

    def test_missing_frozen_row_is_explicit_and_never_returns_partial_sample(self):
        with self.assertRaisesRegex(RuntimeError, "frozen sample row missing"):
            M.ami_decision_chunk(Capture([{}]), [[1, 2], [3, 4]])


DSN = os.getenv("VERITAS_QUALITY_TEST_DSN", "")
NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)


@unittest.skipUnless(DSN, "isolated PostgreSQL test database not configured")
class AMIStagedSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg.rows import dict_row
        cls.driver, cls.row_factory = psycopg, staticmethod(dict_row)
        cls.schema = "ami_staged_test_"+uuid.uuid4().hex
        with psycopg.connect(DSN) as c:
            if c.execute("SELECT current_database()").fetchone()[0] != "veritas_quality_test":
                raise RuntimeError("Refusing writes outside veritas_quality_test")
            c.execute(f"CREATE SCHEMA {cls.schema}")
            c.execute(f"SET search_path TO {cls.schema}")
            c.execute("""CREATE TABLE ledger_events(
                id BIGSERIAL PRIMARY KEY,event_key TEXT UNIQUE NOT NULL,
                entity_key TEXT NOT NULL,event_type TEXT NOT NULL,event_ts TIMESTAMPTZ NOT NULL,
                asset TEXT,horizon TEXT,payload JSONB NOT NULL)""")
            c.execute("CREATE INDEX ledger_type_ts ON ledger_events(event_type,event_ts DESC)")
            c.execute("CREATE INDEX ledger_entity ON ledger_events(entity_key,event_type)")
        cls.addClassCleanup(cls.drop_schema)

    @classmethod
    def drop_schema(cls):
        with cls.driver.connect(DSN) as c:
            c.execute(f"DROP SCHEMA IF EXISTS {cls.schema} CASCADE")

    @contextmanager
    def connect(self):
        with self.driver.connect(DSN, autocommit=True, row_factory=self.row_factory) as c:
            c.execute(f"SET search_path TO {self.schema}")
            c.execute("SET statement_timeout='2s'")
            yield c

    def setUp(self):
        with self.connect() as c:
            c.execute("TRUNCATE ledger_events RESTART IDENTITY")

    def put(self, c, key, entity, kind, payload, *, at=NOW, asset="BTC"):
        return c.execute("""INSERT INTO ledger_events(event_key,entity_key,event_type,event_ts,asset,horizon,payload)
            VALUES(%s,%s,%s,%s,%s,'1h',%s::jsonb) RETURNING id""",
            (key, entity, kind, at, asset, json.dumps(payload))).fetchone()["id"]

    def episode(self, c, key, dp, op, *, at=NOW, asset="BTC"):
        return [self.put(c, "d-"+key, key, "decision", dp, at=at, asset=asset),
                self.put(c, "o-"+key, key, "outcome", op, at=at+timedelta(hours=1), asset=asset)]

    def chunks(self, c, sample):
        return [row for offset in range(0, len(sample), 64)
                for row in M.ami_decision_chunk(c, sample[offset:offset+64])]

    def test_real_sql_projected_shape_truthiness_and_agent_order_match_legacy(self):
        missing = object()
        matches = [missing, None, False, 0, 0., "", [], {}, True, "rule", {"rule": 1}]
        adjustments = [None, {}, False, 0, "", [], {"unused": 1}, {"score": 0},
                       {"score": None}, {"score_with_experience": .125}, {"score": .25, "history": [1]}]
        with self.connect() as c:
            for i, match in enumerate(matches):
                dp = dict(decision="LONG", research_decision="", regime=None,
                          agents=[dict(direction="LONG", confidence=1e16, unused="x"), None,
                                  dict(direction="SHORT", confidence=1.), dict(direction="LONG", confidence=-1e16)],
                          knowledge_cio_adjustment=adjustments[i])
                if match is not missing:
                    dp["knowledge_shadow_matches"] = match
                self.episode(c, str(i), dp, {"forward_return": None if i % 2 else 0.},
                             at=NOW+timedelta(seconds=i), asset="CASE_"+str(i))
            for i, dp in enumerate((None, False, [], "encoded legacy payload", {}, 0), start=20):
                self.episode(c, str(i), dp, {"forward_return": .125}, at=NOW+timedelta(seconds=i))
            self.episode(c, "missing-return", {}, {}, at=NOW+timedelta(days=1))
            expected = c.execute(_V9_AMI_PROJECTED_SQL).fetchall()
            frozen = M.ami_decision_sample(c)
            actual = self.chunks(c, frozen)
            self.assertEqual(actual, expected)
            self.assertEqual(M.ami_decision_rows(c), expected)
            self.assertEqual(len(actual), 17)
            self.assertTrue(all(set(r) == {"decision_id", "outcome_id", "event_ts"} for r in frozen))

    def test_real_sql_sample_keeps_original_2200_joined_row_window(self):
        with self.connect() as c:
            c.execute("""INSERT INTO ledger_events(event_key,entity_key,event_type,event_ts,asset,horizon,payload)
                SELECT 'd-'||i,'e-'||i,'decision',%s::timestamptz+i*interval '1 second',
                    'CASE_'||i,'1h','{"decision":"LONG","agents":[]}'::jsonb
                FROM generate_series(1,2205) AS i""", (NOW,))
            c.execute("""INSERT INTO ledger_events(event_key,entity_key,event_type,event_ts,asset,horizon,payload)
                SELECT 'o-'||i,'e-'||i,'outcome',%s::timestamptz+i*interval '1 second'+interval '1 hour',
                    'CASE_'||i,'1h',jsonb_build_object('forward_return',i::float/10000)
                FROM generate_series(1,2205) AS i""", (NOW,))
            # Newer unmatched and absent-return decisions must not consume the
            # limit, which belongs after the original outcome join/filter.
            self.put(c, "unmatched", "unmatched", "decision", {}, at=NOW+timedelta(days=2))
            self.episode(c, "absent", {}, {}, at=NOW+timedelta(days=3))
            frozen = M.ami_decision_sample(c)
            self.assertEqual(len(frozen), 2200)
            actual = self.chunks(c, frozen)
            self.assertEqual(actual, c.execute(_V9_AMI_PROJECTED_SQL).fetchall())
            self.assertEqual(actual[0]["asset"], "CASE_2205")
            self.assertEqual(actual[-1]["asset"], "CASE_6")

    def test_real_sql_frozen_pairs_ignore_later_decisions_and_new_matching_outcomes(self):
        with self.connect() as c:
            self.episode(c, "one", {"decision": "LONG"}, {"forward_return": .1})
            self.episode(c, "two", {"decision": "SHORT"}, {"forward_return": -.2}, at=NOW+timedelta(seconds=1))
            frozen = M.ami_decision_sample(c)
            before = self.chunks(c, frozen)
            self.episode(c, "new", {"decision": "LONG"}, {"forward_return": .9}, at=NOW+timedelta(days=1))
            self.put(c, "o-extra", "one", "outcome", {"forward_return": -.5}, at=NOW+timedelta(hours=2))
            self.assertEqual(self.chunks(c, frozen), before)
            self.assertEqual(len(M.ami_decision_sample(c)), 4)

    def test_real_sql_tied_join_pairs_and_repeated_pair_follow_input_ordinality(self):
        with self.connect() as c:
            d1 = self.put(c, "d-one", "shared", "decision", {"decision": "LONG"}, asset="FIRST")
            d2 = self.put(c, "d-two", "shared", "decision", {"decision": "SHORT"}, asset="SECOND")
            o1 = self.put(c, "o-one", "shared", "outcome", {"forward_return": .1})
            o2 = self.put(c, "o-two", "shared", "outcome", {"forward_return": -.2})
            frozen = M.ami_decision_sample(c)
            self.assertEqual({(r["decision_id"], r["outcome_id"]) for r in frozen},
                             {(d1, o1), (d1, o2), (d2, o1), (d2, o2)})
            # Stable sort preserves the DB-selected tie order. Chunk order is
            # entirely the frozen caller order, not a new timestamp/ID sort.
            stable = sorted(frozen, key=lambda r: str(r["event_ts"]))
            self.assertEqual(stable, frozen)
            pairs = [[d2, o1], [d1, o2], [d2, o1], [d1, o1]]
            rows = M.ami_decision_chunk(c, pairs)
            self.assertEqual([(r["asset"], r["op"]["forward_return"]) for r in rows],
                             [("SECOND", .1), ("FIRST", -.2), ("SECOND", .1), ("FIRST", .1)])

    def test_real_sql_missing_or_cross_entity_pair_never_publishes_partial_rows(self):
        with self.connect() as c:
            one = self.episode(c, "one", {"decision": "LONG"}, {"forward_return": .1})
            two = self.episode(c, "two", {"decision": "SHORT"}, {"forward_return": -.2})
            with self.assertRaisesRegex(RuntimeError, "frozen sample row missing"):
                M.ami_decision_chunk(c, [one, [one[0], two[1]]])
            c.execute("DELETE FROM ledger_events WHERE id=%s", (two[1],))
            with self.assertRaisesRegex(RuntimeError, "frozen sample row missing"):
                M.ami_decision_chunk(c, [one, two])

    def test_real_sql_toasted_histories_stay_out_of_metadata_and_chunk_results(self):
        heavy = "UNUSED_AMI_STAGED_HISTORY:"*50000
        with self.connect() as c:
            for i in range(3):
                dp = dict(decision="LONG", history=heavy,
                          agents=[dict(direction="SHORT", confidence=.25, history=heavy)],
                          knowledge_cio_adjustment=dict(score=.1, history=heavy),
                          knowledge_shadow_matches=[dict(rule_id="rule", history=heavy)])
                self.episode(c, str(i), dp, {"forward_return": .125, "history": heavy},
                             at=NOW+timedelta(seconds=i))
            sizes = c.execute("SELECT octet_length(payload::text) n FROM ledger_events").fetchall()
            self.assertTrue(all(r["n"] > 1_000_000 for r in sizes))
            frozen = M.ami_decision_sample(c)
            rows = M.ami_decision_chunk(c, frozen)
            self.assertEqual(rows, c.execute(_V9_AMI_PROJECTED_SQL).fetchall())
            self.assertNotIn("UNUSED_AMI_STAGED_HISTORY", json.dumps(frozen, default=str))
            compact = json.dumps(rows, default=str)
            self.assertNotIn("UNUSED_AMI_STAGED_HISTORY", compact)
            self.assertLess(len(compact), 4096)


if __name__ == "__main__":
    unittest.main()
