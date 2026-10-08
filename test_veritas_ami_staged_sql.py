"""Frozen AMI sample/chunk contracts and isolated PostgreSQL parity tests."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
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
        # Normalize only this exact, unbounded entity probe. Any changed key,
        # eligibility, OFFSET or per-entity LIMIT must fail the legacy contract.
        probe = ("CROSS JOIN LATERAL ( SELECT o.id FROM ledger_events o "
                 "WHERE o.entity_key=d.entity_key AND o.event_type='outcome' "
                 "AND o.payload ? 'forward_return' OFFSET 0 ) AS o WHERE d.event_type='decision'")
        original = ("JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome' "
                    "WHERE d.event_type='decision' AND o.payload ? 'forward_return'")
        self.assertIn(probe, tail(query))
        self.assertEqual(tail(query).replace(probe, original), tail(LEGACY_AMI_QUERY))
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


def legacy_chunk_sql():
    """The v91.8.8 flat joins, retaining the exact shared projection."""
    dp, op, joins = M._ami_projection_sql()
    return f"""WITH pairs AS MATERIALIZED (
        SELECT decision_id,outcome_id,ordinality
        FROM unnest(%s::bigint[],%s::bigint[]) WITH ORDINALITY
          AS p(decision_id,outcome_id,ordinality)
      )
      SELECT sample.event_ts,sample.asset,sample.horizon,{dp} AS dp,{op} AS op
      FROM (
        SELECT p.ordinality,d.event_ts,d.asset,d.horizon,
               d.payload AS decision_payload,o.payload AS outcome_payload
        FROM pairs p
        JOIN ledger_events d ON d.id=p.decision_id AND d.event_type='decision'
        JOIN ledger_events o ON o.id=p.outcome_id AND o.entity_key=d.entity_key AND o.event_type='outcome'
      ) AS sample
      {joins}
      ORDER BY sample.ordinality"""


def plan_nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from plan_nodes(child)


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
            c.execute("CREATE INDEX idx_ledger_type_ts ON ledger_events(event_type,event_ts DESC)")
            c.execute("CREATE INDEX idx_ledger_entity ON ledger_events(entity_key,event_type)")
            c.execute("CREATE INDEX idx_ledger_entity_type_ts ON ledger_events(entity_key,event_type,event_ts DESC)")
            c.execute("CREATE INDEX idx_ledger_asset_horizon ON ledger_events(asset,horizon,event_ts DESC)")
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

    def chunk_plan(self, c, sql, params, *, generic=False):
        if not generic:
            return c.execute("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) "+sql, params).fetchone()["QUERY PLAN"][0]
        prepared = sql.replace("%s", "$1", 1).replace("%s", "$2", 1)
        c.execute("PREPARE ami_chunk_test(bigint[],bigint[]) AS "+prepared)
        try:
            with c.transaction():
                # Test a long-lived prepared statement separately, without
                # changing any planner costs, scan/JIT flags or timeout.
                c.execute("SET LOCAL plan_cache_mode='force_generic_plan'")
                arrays = ["ARRAY["+",".join(str(i) for i in values)+"]" for values in params]
                return c.execute("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) EXECUTE ami_chunk_test("+
                                 ",".join(arrays)+")").fetchone()["QUERY PLAN"][0]
        finally:
            c.execute("DEALLOCATE ami_chunk_test")

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

    def test_real_sql_frozen_pair_type_and_payload_semantics_are_not_reinterpreted(self):
        with self.connect() as c:
            good = self.episode(c, "good", {"decision": "LONG"}, {"forward_return": None})
            missing_key = self.episode(c, "missing-key", None, {})
            scalar = self.episode(c, "scalar", ["legacy"], False)
            # Eligibility belongs to the original frozen sample. The chunk
            # preserves null/scalar/absent payload values instead of filtering
            # or silently shrinking a later immutable input list.
            pairs = [scalar, good, missing_key, scalar]
            params = ([p[0] for p in pairs], [p[1] for p in pairs])
            expected = c.execute(legacy_chunk_sql(), params).fetchall()
            self.assertEqual(M.ami_decision_chunk(c, pairs), expected)
            self.assertEqual(len(expected), 4)
            self.assertIsNone(expected[1]["op"]["forward_return"])
            for column, bad_type in ((0, "outcome"), (1, "decision")):
                wrong = self.episode(c, "wrong-"+str(column), {}, {"forward_return": 0.})
                c.execute("UPDATE ledger_events SET event_type=%s WHERE id=%s", (bad_type, wrong[column]))
                with self.subTest(column=column), self.assertRaisesRegex(RuntimeError, "frozen sample row missing"):
                    M.ami_decision_chunk(c, [good, wrong])

    def test_real_sql_64_frozen_pairs_use_bounded_pk_probes_amid_toasted_history(self):
        # Deterministic high-entropy text stays out of line after compression;
        # repeated plain characters alone would fit inline and miss TOAST cost.
        noise = "SYNTHETIC_CHUNK_HISTORY:"+"".join(
            hashlib.sha256(str(i).encode()).hexdigest() for i in range(256))
        with self.connect() as c:
            episodes = []
            for i in range(48):
                dp = dict(decision="LONG" if i % 2 else "SHORT", research_decision="", regime=None,
                          history=noise*8,
                          agents=[dict(direction="LONG", confidence=1e16, history=noise), None,
                                  dict(direction="SHORT", confidence=1.),
                                  dict(direction="LONG", confidence=-1e16)],
                          knowledge_cio_adjustment=dict(score=0 if i % 2 else None, history=noise),
                          knowledge_shadow_matches=[] if i % 3 else [dict(rule="synthetic", history=noise)])
                episodes.append(self.episode(c, "selected-"+str(i), dp,
                    dict(forward_return=None if i % 7 == 0 else (i % 3-1)/8, history=noise*8),
                    at=NOW+timedelta(seconds=i), asset="SELECTED_"+str(i)))
            # This permutation is neither timestamp nor ID order and contains
            # exactly sixteen repeated pairs. Duplicate inputs remain outputs.
            pairs = [episodes[(17*i+5) % 48] for i in range(64)]
            params = ([p[0] for p in pairs], [p[1] for p in pairs])
            expected = c.execute(legacy_chunk_sql(), params).fetchall()
            self.assertEqual(len(expected), 64)
            self.assertEqual([r["asset"] for r in expected], ["SELECTED_"+str((17*i+5) % 48) for i in range(64)])
            self.assertTrue(any(r["op"]["forward_return"] is None for r in expected))
            # Fixture construction and ANALYZE touch the synthetic archive.
            # Restore the real two-second contract before measuring any chunk.
            c.execute("SET statement_timeout='20s'")
            for start in range(0, 12000, 400):
                c.execute("""INSERT INTO ledger_events(event_key,entity_key,event_type,event_ts,asset,horizon,payload)
                    SELECT 'unrelated-'||n,'archive-'||(n/2),
                        CASE WHEN mod(n,2)=0 THEN 'decision' ELSE 'outcome' END,
                        %s::timestamptz-n*interval '1 second','ARCHIVE','1h',
                        CASE WHEN mod(n,2)=0 THEN jsonb_build_object('decision','LONG','history',%s::text)
                             ELSE jsonb_build_object('forward_return',0.125,'history',%s::text) END
                    FROM generate_series(%s::integer,%s::integer) n""", (NOW, noise, noise, start, start+399))
            c.execute("ANALYZE ledger_events")
            c.execute("SET statement_timeout='2s'")
            physical = c.execute("""SELECT count(*) AS n,
                (SELECT pg_relation_size(reltoastrelid) FROM pg_class
                 WHERE oid='ledger_events'::regclass) AS toast_bytes FROM ledger_events""").fetchone()
            self.assertEqual(physical["n"], 12096)
            self.assertGreater(physical["toast_bytes"], 1_000_000)
            self.assertEqual(c.execute("SHOW statement_timeout").fetchone()["statement_timeout"], "2s")
            capture = Capture([{}]*64)
            M.ami_decision_chunk(capture, pairs)
            sql, captured_params = capture.calls[0]
            self.assertEqual(captured_params, params)
            actual = M.ami_decision_chunk(c, pairs)
            self.assertEqual(actual, expected)
            self.assertNotIn("SYNTHETIC_CHUNK_HISTORY", json.dumps(actual, default=str))
            self.assertLess(len(json.dumps(actual, default=str)), 40000)
            for generic in (False, True):
                with self.subTest(plan="generic" if generic else "default"):
                    new = self.chunk_plan(c, sql, params, generic=generic)
                    probes = [n for n in plan_nodes(new["Plan"]) if n.get("Relation Name") == "ledger_events"]
                    references = []
                    for node in probes:
                        self.assertEqual(node["Node Type"], "Index Scan")
                        self.assertEqual(node.get("Index Name"), "ledger_events_pkey")
                        # PostgreSQL may rename a shadowed inner alias d_1/o_1.
                        # The required property is which frozen ID drives it.
                        matched = re.findall(r"\bid\s*=\s*p\.(decision_id|outcome_id)\b",
                                             node.get("Index Cond", ""))
                        self.assertEqual(len(matched), 1)
                        references.extend(matched)
                        self.assertGreater(node["Actual Loops"], 0)
                        self.assertLessEqual(node["Actual Loops"], 64)
                        self.assertLessEqual(node["Actual Rows"], 1)
                    self.assertCountEqual(references, ["decision_id", "outcome_id"])
                    diagnostic = dict(plan_mode="generic" if generic else "default",
                        rows=physical["n"], frozen_pairs=64, distinct_pairs=48,
                        toast_bytes=physical["toast_bytes"], new_execution_ms=new["Execution Time"],
                        statement_timeout="2s", old_timeout=False)
                    try:
                        old = self.chunk_plan(c, legacy_chunk_sql(), params, generic=generic)
                    except self.driver.errors.QueryCanceled:
                        diagnostic["old_timeout"] = True
                    else:
                        old_probes = [n for n in plan_nodes(old["Plan"]) if n.get("Relation Name") == "ledger_events"]
                        diagnostic.update(old_execution_ms=old["Execution Time"], old_ledger_nodes=[
                            dict(alias=n.get("Alias"), node=n["Node Type"], index=n.get("Index Name"),
                                 loops=n["Actual Loops"], rows=n["Actual Rows"]) for n in old_probes],
                            old_wide_scan_verified=any(n["Node Type"] in ("Seq Scan", "Bitmap Heap Scan") for n in old_probes))
                    # The mechanism and exact population are assertions; old timing is
                    # evidence, not a required slowdown or a universal latency promise.
                    print("AMI_CHUNK_SYNTHETIC_PLAN "+json.dumps(diagnostic), flush=True)

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
