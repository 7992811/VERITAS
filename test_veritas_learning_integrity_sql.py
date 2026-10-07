"""Persisted-label regressions against an explicitly isolated PostgreSQL schema."""
import copy
import hashlib
import json
import os
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from queue import Queue
from unittest.mock import patch

import veritas_portfolio as P
import veritas_portfolio_runtime as R
import veritas_learning_integrity as LI
from test_veritas_strategy_quality_sql import observed_evidence
from test_veritas_readiness_fixtures import add_observed_path
from test_veritas_trade_diagnostics import closed_trade
import veritas_trade_diagnostics as DIAGNOSTICS

DSN = os.getenv("VERITAS_QUALITY_TEST_DSN", "")

@unittest.skipUnless(DSN, "isolated PostgreSQL test database not configured")
class PersistedLearningSQLTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        self.driver, self.row_factory = psycopg, dict_row
        self.schema = "learning_integrity_test_"+uuid.uuid4().hex
        # Direct/failed validation deliberately revokes process-local memory.
        # Restore this test's starting token without certifying any DB commit.
        for name in ("_GENERATION", "_REVOCATION_GENERATION", "_UNCONFIRMED"):
            self.enterContext(patch.object(LI, name, getattr(LI, name)))
        self.saved29, self.saved33 = copy.deepcopy(R._v90r29_cache), copy.deepcopy(R._v90r33_cache)
        self.saved_state = copy.deepcopy(R._v90r44_sanitize_state)
        with psycopg.connect(DSN) as c:
            if c.execute("SELECT current_database()").fetchone()[0] != "veritas_quality_test":
                raise RuntimeError("Refusing writes outside veritas_quality_test")
            c.execute(f"CREATE SCHEMA {self.schema}")
            c.execute(f"SET search_path TO {self.schema}")
            c.execute("""CREATE TABLE paper_trades(
              trade_id text PRIMARY KEY,asset text,direction text,horizon text,status text,
              opened_at timestamptz,closed_at timestamptz,gross_pnl_rub float8,
              fees_rub float8,funding_rub float8,net_pnl_rub float8,payload jsonb)""")
            c.execute("""CREATE TABLE v90_learning_episodes(
              trade_id text PRIMARY KEY,asset text,direction text,horizon text,closed_at timestamptz,
              setup_family text,regime text,net_pnl_rub float8,movement_realization_ratio float8,
              capture_ratio float8,primary_attribution text,attributions jsonb,
              learning_action text,learning_eligible boolean,payload jsonb)""")
        for key, kind, net in (("a_clean", "clean", 30), ("b_gold", "proxy", -3336),
                               ("c_unknown", "unknown", -120), ("d_synthetic", "synthetic", -80),
                               ("z_manual", "manual", -50)):
            self.add_trade(key, kind, net)

    @contextmanager
    def connect(self):
        with self.driver.connect(DSN, row_factory=self.row_factory) as c:
            c.execute(f"SET search_path TO {self.schema}")
            yield c

    @contextmanager
    def autocommit_connect(self):
        # Match the production connection factory, not the default test driver.
        with self.driver.connect(DSN, autocommit=True, row_factory=self.row_factory) as c:
            c.execute(f"SET search_path TO {self.schema}")
            yield c

    def tearDown(self):
        for target, saved in ((R._v90r29_cache, self.saved29), (R._v90r33_cache, self.saved33),
                              (R._v90r44_sanitize_state, self.saved_state)):
            target.clear(); target.update(saved)
        with self.driver.connect(DSN) as c:
            c.execute(f"DROP SCHEMA {self.schema} CASCADE")

    def add_trade(self, key, kind="clean", net=30):
        opened = datetime(2026,10,6,19,0,tzinfo=timezone.utc)
        asset = "GOLD" if kind=="proxy" else "ETH"
        p = observed_evidence(asset, "LONG", "STF_"+key, opened, "1h")
        p.update(mfe_pct=1.5, mae_pct=-.8, strategy_epoch="IMMUTABLE_OLD_EPOCH",
                 strategy_entry_sha="original-entry-sha", telemetry_completeness=1.0,
                 exit_reason="TAKE_PROFIT" if net > 0 else "MODEL_CLOSE")
        raw = add_observed_path(dict(trade_id=key,asset=asset,direction="LONG",horizon="1h",
                                    status="CLOSED",opened_at=opened,closed_at=opened+timedelta(hours=1),
                                    gross_pnl_rub=net+10,fees_rub=8,funding_rub=2,net_pnl_rub=net,payload=p))
        if kind=="proxy":
            for field in ("price_source_lock", "entry_execution_source_identity", "last_exit_source_identity"):
                p[field].update(key="PROXY:GLD", primary_source="PROXY BRIDGE")
            p["entry_event_snapshot"]["source_identity"] = copy.deepcopy(p["price_source_lock"])
        elif kind=="unknown":
            p.pop("data_integrity_status")
        elif kind=="synthetic":
            p["r66_event_id"] = "R79_SIG_"+key
            p["entry_event_snapshot"]["event_id"] = p["r66_event_id"]
            p["idea_id_verified"] = True
        self.add_raw_trade(raw,eligible=kind!="manual")

    def add_raw_trade(self, raw, *, eligible=True, attribution="GOOD_EXECUTION"):
        with self.connect() as c:
            c.execute("INSERT INTO paper_trades VALUES ("+",".join(["%s"]*len(LI.TRADE_FIELDS))+",%s::jsonb)",
                      tuple(raw[k] for k in LI.TRADE_FIELDS)+(json.dumps(raw["payload"]),))
            c.execute("""INSERT INTO v90_learning_episodes VALUES
              (%s,%s,%s,%s,%s,'BREAKOUT','TREND',%s,1.0,.7,%s,%s::jsonb,'RETAIN_RULE',%s,%s::jsonb)""",
              tuple(raw[k] for k in ("trade_id","asset","direction","horizon","closed_at","net_pnl_rub"))+
              (attribution,json.dumps([attribution]),eligible,
               json.dumps({"independent_episode_key":raw["trade_id"],"original_note":"preserve"})))

    def financials(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM paper_trades ORDER BY trade_id").fetchall()]

    def episodes(self):
        with self.connect() as c:
            return {r["trade_id"]:dict(r) for r in
                    c.execute("SELECT * FROM v90_learning_episodes ORDER BY trade_id").fetchall()}

    def sanitize(self):
        with patch.object(R, "_v90r29_ensure"):
            return R._v90r44_sanitize_learning(self.autocommit_connect, force=True)

    def revalidation_queries(self, c, batch_size=2):
        class RecordingConnection:
            def __init__(self): self.calls = []
            def execute(self, sql, args=()):
                self.calls.append((sql, args))
                return c.execute(sql, args)
        recording = RecordingConnection()
        c.execute("SAVEPOINT capture_revalidation")
        try:
            # Capture the actual SQL body without falsely treating this rolled
            # back savepoint as a committed public validation. The public
            # transaction/readiness protocol is exercised by native tests below.
            LI._revalidate_eligible(recording, batch_size=batch_size)
        finally:
            c.execute("ROLLBACK TO SAVEPOINT capture_revalidation")
            c.execute("RELEASE SAVEPOINT capture_revalidation")
        selected = []
        for marker in ("WITH candidates AS MATERIALIZED", "WITH selected AS MATERIALIZED"):
            matches = [call for call in recording.calls if marker in call[0]]
            self.assertEqual(len(matches), 1, marker)
            selected.append(matches[0])
        return selected

    def plan_nodes(self, plan):
        yield plan
        for child in plan.get("Plans", []):
            yield from self.plan_nodes(child)

    def previous_pending_query(self):
        # Production v91.7.19 reference: the same evidence expression was
        # independently expanded for the returned trade and its hash.
        evidence = LI._evidence_sql("t")
        return """SELECT e.trade_id AS episode_trade_id,
          e.payload->'learning_integrity' AS prior,
          """+evidence+""" AS trade,md5(("""+evidence+""")::text) AS evidence_hash
          FROM v90_learning_episodes e LEFT JOIN paper_trades t ON t.trade_id=e.trade_id
          WHERE e.learning_eligible=FALSE
            AND e.payload#>>'{learning_integrity,version}'=%s
            AND e.payload#>>'{learning_integrity,status}'='PENDING'
          ORDER BY e.payload#>>'{learning_integrity,requested_at}',e.trade_id
          LIMIT %s FOR UPDATE OF e SKIP LOCKED"""

    def test_materialized_staging_matches_prior_hashes_and_preserves_all_quarantine_rows(self):
        with self.connect() as c:
            original = dict(c.execute("SELECT * FROM paper_trades WHERE trade_id='a_clean'").fetchone())
        for index, payload in enumerate((None, [], "malformed", 7, {}, {"entry_event_snapshot": None})):
            raw = dict(original, trade_id="shape_"+str(index), payload=payload)
            self.add_raw_trade(raw)
        with self.connect() as c:
            c.execute("UPDATE paper_trades SET payload=NULL WHERE trade_id='shape_0'")
            count = c.execute("SELECT count(*) AS n FROM v90_learning_episodes WHERE learning_eligible=TRUE").fetchone()["n"]
            (query, args), unused = self.revalidation_queries(c)
            previous = query.replace("WITH candidates AS MATERIALIZED (", "WITH candidates AS (", 1)
            previous = previous.replace("e.trade_id=q.trade_id AND e.learning_eligible=TRUE", "e.trade_id=q.trade_id", 1)
            c.execute("SAVEPOINT compare_staging")
            old_plan = c.execute("EXPLAIN (ANALYZE, FORMAT JSON, TIMING FALSE) "+previous, args).fetchone()["QUERY PLAN"][0]
            old_rows = c.execute("SELECT * FROM v90_learning_episodes ORDER BY trade_id").fetchall()
            c.execute("ROLLBACK TO SAVEPOINT compare_staging")
            new_plan = c.execute("EXPLAIN (ANALYZE, FORMAT JSON, TIMING FALSE) "+query, args).fetchone()["QUERY PLAN"][0]
            new_rows = c.execute("SELECT * FROM v90_learning_episodes ORDER BY trade_id").fetchall()
            self.assertEqual(new_rows, old_rows)
            candidates = [p for p in self.plan_nodes(new_plan["Plan"]) if p.get("Subplan Name")=="CTE candidates"]
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["Actual Rows"], count)
            self.assertTrue(all(not row["learning_eligible"] for row in new_rows))
            manual = next(row for row in new_rows if row["trade_id"]=="z_manual")
            self.assertNotIn("learning_integrity", manual["payload"])
            print(json.dumps({"event":"synthetic_sanitizer_plan_comparison","phase":"staging",
                "old_planning_ms":old_plan["Planning Time"],"new_planning_ms":new_plan["Planning Time"],
                "old_execution_ms":old_plan["Execution Time"],"new_execution_ms":new_plan["Execution Time"]}))

    def test_pending_proof_is_bounded_before_projection_and_exactly_matches_previous_hash(self):
        with self.connect() as c:
            staging, (query, args) = self.revalidation_queries(c)
            c.execute(*staging)
            previous = self.previous_pending_query()
            old_rows = c.execute(previous, args).fetchall()
            new_rows = c.execute(query, args).fetchall()
            self.assertEqual(new_rows, old_rows)
            self.assertEqual(len(new_rows), 2)
            old_plan = c.execute("EXPLAIN (ANALYZE, FORMAT JSON, TIMING FALSE) "+previous, args).fetchone()["QUERY PLAN"][0]
            new_plan = c.execute("EXPLAIN (ANALYZE, FORMAT JSON, TIMING FALSE) "+query, args).fetchone()["QUERY PLAN"][0]
            nodes = list(self.plan_nodes(new_plan["Plan"]))
            for name in ("CTE selected", "CTE projected"):
                matched = [p for p in nodes if p.get("Subplan Name")==name]
                self.assertEqual(len(matched), 1, name)
                self.assertEqual(matched[0]["Actual Rows"], 2, name)
            self.assertEqual(new_plan["Plan"]["Actual Rows"], 2)
            self.assertLess(len(query), len(previous)*.60)
            print(json.dumps({"event":"synthetic_sanitizer_plan_comparison","phase":"pending",
                "old_planning_ms":old_plan["Planning Time"],"new_planning_ms":new_plan["Planning Time"],
                "old_execution_ms":old_plan["Execution Time"],"new_execution_ms":new_plan["Execution Time"]}))

    def test_materialized_pending_batch_keeps_skip_locked_and_original_priority(self):
        with self.connect() as c:
            staging, (query, args) = self.revalidation_queries(c)
            c.execute(*staging)
        with self.connect() as holder:
            holder.execute("SELECT trade_id FROM v90_learning_episodes WHERE trade_id='a_clean' FOR UPDATE")
            with self.connect() as worker:
                actual = worker.execute(query, args).fetchall()
                expected = worker.execute(self.previous_pending_query(), args).fetchall()
                self.assertEqual(actual, expected)
                self.assertEqual([row["episode_trade_id"] for row in actual], ["b_gold", "c_unknown"])

    def test_record_projection_preserves_exact_proof_and_hash_for_all_root_shapes(self):
        projected = LI._evidence_sql("t", root_field=lambda key: "evidence_payload."+key)
        record_join = LI.payload_record_sql("t")
        original = LI._evidence_sql("t")
        fields = []
        LI.payload_sql(root_field=lambda key: fields.append(key) or "unused")
        with self.connect() as c:
            valid = c.execute("SELECT payload FROM paper_trades WHERE trade_id='a_clean'").fetchone()["payload"]
            shapes = [None, [], ["object-like"], "scalar", 7, False, {}, valid]
            shapes += [{key: value for key in fields} for value in (None, [], {}, "bad", 7)]
            for lock in ({"asset": "ETH"}, {"asset": "ETH", "source_pin_version": None},
                         {"asset": "ETH", "provider_ticker": []}):
                shapes.append(dict(valid, price_source_lock=lock,
                                   source_locked_mark={"identity": lock}))
            oversized = dict(valid, entry_event_snapshot={
                "event_type": "VERIFIED_QUOTE_STRUCTURAL_BREAKOUT",
                "synthetic_oversized_proof": "oversized"*20000})
            shapes.append(oversized)
            # None is a SQL NULL parameter only in the first case; json.dumps
            # supplies explicit JSON null in the second case.
            for index, encoded in enumerate([None]+[json.dumps(p) for p in shapes]):
                with self.subTest(shape=index):
                    c.execute("UPDATE paper_trades SET payload=%s::jsonb WHERE trade_id='a_clean'", (encoded,))
                    comparison = c.execute("""WITH proofs AS MATERIALIZED (
                      SELECT """+original+""" AS original,"""+projected+""" AS projected
                      FROM paper_trades t """+record_join+""" WHERE t.trade_id='a_clean')
                      SELECT original=projected AS same_json,
                        original::text=projected::text AS same_text,
                        md5(original::text)=md5(projected::text) AS same_hash
                      FROM proofs""").fetchone()
                    self.assertEqual(dict(comparison), dict(same_json=True, same_text=True, same_hash=True))
            # Missing LEFT JOIN trades must still produce one null-filled proof.
            missing = c.execute("""SELECT """+original+""" AS original,"""+projected+""" AS projected
              FROM (VALUES ('missing')) p(trade_id)
              LEFT JOIN paper_trades t ON t.trade_id=p.trade_id """+record_join).fetchall()
            self.assertEqual(len(missing), 1)
            self.assertEqual(missing[0]["original"], missing[0]["projected"])

    def test_bounded_pending_reads_large_toasted_roots_once_under_production_timeout(self):
        with self.connect() as c:
            unused, (query, args) = self.revalidation_queries(c, batch_size=LI.BATCH_SIZE)
            projected = LI._evidence_sql("t", root_field=lambda key: "evidence_payload."+key)
            record_join = LI.payload_record_sql("t")
            self.assertEqual(query.count(projected), 1)
            previous = query.replace(projected, LI._evidence_sql("t"), 1).replace(record_join, "", 1)
            # High-entropy fabricated history is physically toasted, not a tiny
            # repeated-string fixture. It is not part of the learning proof.
            history = [{"bar": i, "close": 100+i/97,
                        "synthetic_hash": hashlib.sha256(("synthetic-bar-"+str(i)).encode()).hexdigest(),
                        "note": "fabricated historical telemetry"} for i in range(1800)]
            extra = json.dumps({"synthetic_unrelated_history": history})
            self.assertGreater(len(extra), 200000)
            c.execute("""INSERT INTO paper_trades
              SELECT 'synthetic_toast_'||lpad(n::text,3,'0'),t.asset,t.direction,t.horizon,t.status,
                t.opened_at,t.closed_at,t.gross_pnl_rub,t.fees_rub,t.funding_rub,t.net_pnl_rub,
                t.payload||%s::jsonb||jsonb_build_object('synthetic_row',n)
              FROM paper_trades t CROSS JOIN generate_series(1,%s) AS n
              WHERE t.trade_id='a_clean'""", (extra, LI.BATCH_SIZE))
            c.execute("""INSERT INTO v90_learning_episodes
              SELECT t.trade_id,t.asset,t.direction,t.horizon,t.closed_at,'BREAKOUT','TREND',
                t.net_pnl_rub,1.0,.7,'DATA_EVIDENCE_PENDING','["DATA_EVIDENCE_PENDING"]'::jsonb,
                'AWAIT_SOURCE_EVIDENCE_REVALIDATION',FALSE,
                jsonb_build_object('learning_integrity',jsonb_build_object(
                  'version',%s::text,'status','PENDING','requested_at','2042-01-01T00:00:00Z'))
              FROM paper_trades t WHERE t.trade_id LIKE 'synthetic_toast_%%'""", (LI.VERSION,))
            size = c.execute("""SELECT min(octet_length(payload::text)) AS source_bytes,
              min(pg_column_size(payload)) AS stored_bytes FROM paper_trades
              WHERE trade_id LIKE 'synthetic_toast_%'""").fetchone()
            self.assertGreater(size["source_bytes"], 200000)
            self.assertGreater(size["stored_bytes"], 8192)
            self.assertLess(size["stored_bytes"], size["source_bytes"])
            c.execute("SET LOCAL statement_timeout = '4000ms'")
            plan = c.execute("EXPLAIN (ANALYZE, VERBOSE, FORMAT JSON, TIMING FALSE) "+query, args).fetchone()["QUERY PLAN"][0]
            scans = [node for node in self.plan_nodes(plan["Plan"])
                     if node.get("Node Type")=="Function Scan" and node.get("Alias")=="evidence_payload"]
            self.assertEqual(len(scans), 1)
            self.assertEqual(scans[0]["Actual Loops"], LI.BATCH_SIZE)
            self.assertEqual(plan["Plan"]["Actual Rows"], LI.BATCH_SIZE)
            self.assertEqual(query.count("t.payload"), 2)  # Shape guard + one record scan.
            actual = c.execute(query, args).fetchall()
            self.assertEqual(len(actual), LI.BATCH_SIZE)
            self.assertNotIn("synthetic_unrelated_history", actual[0]["trade"]["payload"])
            for row in (actual[0], actual[-1]):
                expected = c.execute("SELECT "+LI.evidence_hash_sql("t")+" AS hash FROM paper_trades t WHERE trade_id=%s",
                                     (row["episode_trade_id"],)).fetchone()["hash"]
                self.assertEqual(row["evidence_hash"], expected)
            old_plan, old_timed_out = None, False
            try:
                with c.transaction():
                    old_plan = c.execute("EXPLAIN (ANALYZE, FORMAT JSON, TIMING FALSE) "+previous, args).fetchone()["QUERY PLAN"][0]
            except self.driver.errors.QueryCanceled:
                old_timed_out = True
            self.assertEqual(c.execute("SELECT 1 AS alive").fetchone()["alive"], 1)
            print(json.dumps({"event":"synthetic_sanitizer_root_projection", "rows":LI.BATCH_SIZE,
                "source_bytes_per_row":size["source_bytes"], "stored_bytes_per_row":size["stored_bytes"],
                "old_root_references":previous.count("t.payload"), "new_root_references":query.count("t.payload"),
                "old_timed_out_4000ms":old_timed_out,
                "old_execution_ms":old_plan["Execution Time"] if old_plan else None,
                "new_planning_ms":plan["Planning Time"], "new_execution_ms":plan["Execution Time"],
                "jit":plan.get("JIT"), "statement_timeout_ms":4000}))

    def test_controlled_jit_projection_reports_cost_without_changing_proof(self):
        with self.connect() as c:
            staging, (query, args) = self.revalidation_queries(c)
            c.execute(*staging)
            projected = LI._evidence_sql("t", root_field=lambda key: "evidence_payload."+key)
            record_join = LI.payload_record_sql("t")
            previous = query.replace(projected, LI._evidence_sql("t"), 1).replace(record_join, "", 1)
            self.assertTrue(c.execute("SELECT pg_jit_available() AS available").fetchone()["available"],
                            "Native JIT diagnostic requires PostgreSQL LLVM support")
            c.execute("SET LOCAL statement_timeout = '4000ms'")
            c.execute("SET LOCAL jit = off")
            expected = c.execute(previous, args).fetchall()
            self.assertEqual(c.execute(query, args).fetchall(), expected)
            off = c.execute("EXPLAIN (ANALYZE, FORMAT JSON) "+query, args).fetchone()["QUERY PLAN"][0]
            self.assertNotIn("JIT", off)
            c.execute("SET LOCAL jit = on")
            c.execute("SET LOCAL jit_above_cost = 0")
            c.execute("SET LOCAL jit_inline_above_cost = 0")
            c.execute("SET LOCAL jit_optimize_above_cost = 0")
            reports = {}
            for name, sql in (("repeated_root", previous), ("record_root", query)):
                try:
                    with c.transaction():
                        plan = c.execute("EXPLAIN (ANALYZE, FORMAT JSON) "+sql, args).fetchone()["QUERY PLAN"][0]
                        self.assertEqual(plan["Plan"]["Actual Rows"], len(expected))
                        self.assertIn("JIT", plan)
                        reports[name] = {"execution_ms":plan["Execution Time"], "jit":plan["JIT"]}
                except self.driver.errors.QueryCanceled:
                    # Deliberately forcing every LLVM optimization is diagnostic;
                    # the default-settings large-TOAST test must still finish.
                    reports[name] = {"timed_out_4000ms":True}
            c.execute("SET LOCAL jit = off")
            self.assertEqual(c.execute(query, args).fetchall(), expected)
            print(json.dumps({"event":"synthetic_sanitizer_jit_comparison",
                "forced_thresholds":0, "statement_timeout_ms":4000,
                "record_jit_off_execution_ms":off["Execution Time"], "forced_jit":reports}))

    def test_materialized_candidate_cannot_restage_a_concurrent_intentional_exclusion(self):
        ready = Queue()
        def revalidate():
            with self.connect() as worker:
                ready.put(worker.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
                return LI.revalidate_eligible(worker)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.connect() as holder:
                holder_pid = holder.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
                holder.execute("""UPDATE v90_learning_episodes
                    SET learning_eligible=FALSE,primary_attribution='MANUAL_EXCLUDED',
                        learning_action='EXCLUDE_FROM_LEARNING',
                        attributions='["MANUAL_EXCLUDED"]'::jsonb,
                        payload=payload||'{"manual_exclusion":"retained"}'::jsonb
                    WHERE trade_id='a_clean'""")
                future = pool.submit(revalidate)
                worker_pid = ready.get(timeout=5)
                deadline = time.monotonic()+5
                while time.monotonic()<deadline:
                    blockers = holder.execute("SELECT pg_blocking_pids(%s) AS pids", (worker_pid,)).fetchone()["pids"]
                    if holder_pid in blockers:
                        break
                    time.sleep(.01)
                else:
                    self.fail("revalidation never waited on the intentionally excluded candidate")
                # The candidate snapshot still saw the old eligible row. Commit
                # the explicit exclusion only after its UPDATE is really waiting.
            result = future.result(timeout=10)
        row = self.episodes()["a_clean"]
        self.assertFalse(row["learning_eligible"])
        self.assertEqual(row["primary_attribution"], "MANUAL_EXCLUDED")
        self.assertEqual(row["payload"]["manual_exclusion"], "retained")
        self.assertNotIn("learning_integrity", row["payload"])
        self.assertEqual(result["verified"], 0)

    def pending_marker(self, version):
        return {"version": version, "status": "PENDING",
                "requested_at": "2000-01-01T00:00:00+00:00",
                "prior_primary_attribution": "GOOD_EXECUTION",
                "prior_attributions": ["GOOD_EXECUTION"],
                "prior_learning_action": "RETAIN_RULE"}

    def exclude_fixture_episodes(self, c):
        c.execute("""UPDATE v90_learning_episodes SET learning_eligible=FALSE,
            primary_attribution='MANUAL_EXCLUDED',attributions='["MANUAL_EXCLUDED"]',
            learning_action='EXCLUDE_FROM_LEARNING'""")

    def test_old_and_current_pending_metadata_cannot_override_manual_classification(self):
        before_financials = self.financials()
        classifications = (("DATA_EVIDENCE_PENDING", "EXCLUDE_FROM_LEARNING"),
                           ("MANUAL_EXCLUDED", "AWAIT_SOURCE_EVIDENCE_REVALIDATION"),
                           ("MANUAL_EXCLUDED", "EXCLUDE_FROM_LEARNING"))
        for version in ("LEARNING_TF_PATH_EVIDENCE_V2", LI.VERSION):
            for attribution, action in classifications:
                with self.subTest(version=version, attribution=attribution, action=action):
                    with self.autocommit_connect() as c:
                        self.exclude_fixture_episodes(c)
                        c.execute("""UPDATE v90_learning_episodes SET
                            primary_attribution=%s,learning_action=%s,
                            payload=payload||%s::jsonb WHERE trade_id='a_clean'""",
                            (attribution, action, json.dumps({
                                "learning_integrity": self.pending_marker(version),
                                "manual_note": "synthetic explicit exclusion"})))
                        before = self.episodes()
                        with LI.revalidation_transaction(c):
                            result = LI.revalidate_eligible(c)
                        self.assertEqual(result["retry_staged"], 0)
                        self.assertEqual(result["staged"], 0)
                        self.assertEqual(result["processed"], 0)
                        self.assertEqual(result["pending"], 0)
                        self.assertEqual(self.episodes(), before)
                        self.assertFalse(c.execute("SELECT "+LI.readable_sql()+
                            " AS ok FROM v90_learning_episodes WHERE trade_id='a_clean'").fetchone()["ok"])
        self.assertEqual(self.financials(), before_financials)

    def test_manual_classification_after_pending_selection_prevents_final_promotion(self):
        before_financials = self.financials()
        for attribution, action in (("MANUAL_EXCLUDED", "AWAIT_SOURCE_EVIDENCE_REVALIDATION"),
                                    ("DATA_EVIDENCE_PENDING", "EXCLUDE_FROM_LEARNING")):
            with self.subTest(attribution=attribution, action=action), self.autocommit_connect() as c:
                self.exclude_fixture_episodes(c)
                c.execute("""UPDATE v90_learning_episodes SET
                    primary_attribution='DATA_EVIDENCE_PENDING',
                    learning_action='AWAIT_SOURCE_EVIDENCE_REVALIDATION',
                    payload=payload||%s::jsonb WHERE trade_id='a_clean'""",
                    (json.dumps({"learning_integrity": self.pending_marker(LI.VERSION)}),))
                changed = []
                class InterposedConnection:
                    def __getattr__(self, name): return getattr(c, name)
                    def execute(self, sql, args=()):
                        cursor = c.execute(sql, args)
                        if "WITH selected AS MATERIALIZED" not in sql:
                            return cursor
                        class SelectedRows:
                            def fetchall(self):
                                rows = cursor.fetchall()
                                # Row locks prevent another connection changing
                                # this row now. Exercise a same-transaction
                                # cancellation after selection, before relabel.
                                c.execute("""UPDATE v90_learning_episodes SET
                                    primary_attribution=%s,learning_action=%s
                                    WHERE trade_id='a_clean'""", (attribution, action))
                                LI.invalidate("synthetic explicit exclusion")
                                changed.append(True)
                                return rows
                        return SelectedRows()
                with LI.revalidation_transaction(c):
                    result = LI.revalidate_eligible(InterposedConnection())
                self.assertEqual(changed, [True])
                self.assertEqual(result["processed"], 0)
                self.assertEqual(result["verified"], 0)
                self.assertEqual(result["excluded"], 0)
                self.assertEqual(result["pending"], 0)
                row = self.episodes()["a_clean"]
                self.assertFalse(row["learning_eligible"])
                self.assertEqual((row["primary_attribution"], row["learning_action"]),
                                 (attribution, action))
                self.assertEqual(row["payload"]["learning_integrity"]["status"], "PENDING")
        self.assertEqual(self.financials(), before_financials)

    def test_legacy_backlog_and_pending_native_quote_keep_progress_and_commit_readiness(self):
        from test_veritas_quote_learning import quote_trade
        raw = quote_trade(); raw["trade_id"] = "native_quote_pending"
        self.assertIsNone(LI.trade_exclusion(raw))
        self.add_raw_trade(raw, eligible=False, attribution="UNVERIFIED_TRADE_EVIDENCE")
        before_financials = self.financials()
        with self.autocommit_connect() as c:
            self.exclude_fixture_episodes(c)
            c.execute("""UPDATE v90_learning_episodes SET
                primary_attribution='DATA_EVIDENCE_PENDING',
                learning_action='AWAIT_SOURCE_EVIDENCE_REVALIDATION',
                payload=payload||%s::jsonb WHERE trade_id='a_clean'""",
                (json.dumps({"learning_integrity": self.pending_marker("LEARNING_TF_PATH_EVIDENCE_V2")}),))
            quote_marker = self.pending_marker(LI.VERSION)
            quote_marker["requested_at"] = "2099-01-01T00:00:00+00:00"
            c.execute("""UPDATE v90_learning_episodes SET
                primary_attribution='UNVERIFIED_TRADE_EVIDENCE',
                learning_action='AWAIT_SOURCE_EVIDENCE_REVALIDATION',
                payload=payload||%s::jsonb WHERE trade_id='native_quote_pending'""",
                (json.dumps({"learning_integrity": quote_marker,
                             "diagnostics_version": "SAME_TF_TRADE_DIAGNOSTICS_V1",
                             "learning_exclusion_reason": "UNVERIFIED_EVENT_PROVENANCE"}),))
            LI.invalidate("synthetic migration")
            state = LI.memory_state()
            with LI.revalidation_transaction(c):
                first = LI.revalidate_eligible(c, batch_size=1)
                self.assertFalse(LI.memory_state()["ready"])
            self.assertTrue(LI.memory_state()["ready"])
            self.assertEqual((first["retry_staged"], first["processed"], first["verified"], first["pending"]),
                             (1, 1, 1, 1))
            self.assertTrue(self.episodes()["a_clean"]["learning_eligible"])
            with LI.revalidation_transaction(c):
                second = LI.revalidate_eligible(c, batch_size=1)
                self.assertFalse(LI.memory_state()["ready"])
            self.assertTrue(LI.memory_state()["ready"])
            self.assertEqual((second["staged"], second["processed"], second["verified"], second["pending"]),
                             (0, 1, 1, 0))
            self.assertTrue(self.episodes()["native_quote_pending"]["learning_eligible"])
            self.assertEqual(LI.memory_state()["process_epoch"], state["process_epoch"])
            self.assertEqual(LI.memory_state()["revocation_generation"], state["revocation_generation"])
        self.assertEqual(self.financials(), before_financials)

    def test_native_commit_failure_revokes_readiness_and_rolls_back_revalidation(self):
        before_financials, before_episodes = self.financials(), self.episodes()
        with self.autocommit_connect() as c:
            c.execute("CREATE TABLE commit_probe (id int UNIQUE DEFERRABLE INITIALLY DEFERRED)")
            with self.assertRaises(self.driver.errors.UniqueViolation):
                with LI.revalidation_transaction(c):
                    result = LI.revalidate_eligible(c)
                    self.assertGreater(result["processed"], 0)
                    c.execute("INSERT INTO commit_probe VALUES (1),(1)")
                    self.assertFalse(LI.memory_state()["ready"])
            self.assertFalse(LI.memory_state()["ready"])
            self.assertEqual(self.episodes(), before_episodes)
            self.assertEqual(self.financials(), before_financials)
            with LI.revalidation_transaction(c):
                result = LI.revalidate_eligible(c)
                self.assertGreater(result["processed"], 0)
                self.assertFalse(LI.memory_state()["ready"])
            self.assertTrue(LI.memory_state()["ready"])
        self.assertTrue(self.episodes()["a_clean"]["learning_eligible"])
        self.assertEqual(self.financials(), before_financials)

    def test_saved_proxy_unknown_and_synthetic_labels_are_revalidated_without_financial_edits(self):
        before = self.financials()
        result = self.sanitize()
        self.assertIsNone(result["last_error"], result)
        rows = self.episodes()
        self.assertTrue(rows["a_clean"]["learning_eligible"])
        for key in ("b_gold","c_unknown","d_synthetic","z_manual"):
            self.assertFalse(rows[key]["learning_eligible"], key)
        self.assertEqual(rows["b_gold"]["payload"]["learning_integrity"]["exclusion_reason"], "PROXY_PRICE")
        self.assertEqual(rows["c_unknown"]["payload"]["learning_integrity"]["exclusion_reason"], "SOURCE_UNVERIFIED")
        self.assertEqual(rows["d_synthetic"]["payload"]["learning_integrity"]["exclusion_reason"], "UNVERIFIED_EVENT")
        self.assertEqual(rows["a_clean"]["primary_attribution"], "VALID_PROFITABLE_TRADE")
        self.assertEqual(rows["a_clean"]["payload"]["trade_diagnostics"]["version"], DIAGNOSTICS.VERSION)
        self.assertEqual(rows["a_clean"]["payload"]["original_note"], "preserve")
        self.assertNotIn("learning_integrity", rows["z_manual"]["payload"])
        self.assertEqual(self.financials(), before)
        again = self.sanitize()
        self.assertIsNone(again["last_error"], again)
        self.assertEqual(self.episodes(), rows)
        self.assertEqual(self.financials(), before)

    def test_old_flags_cannot_feed_r29_or_r33_before_first_sanitizer(self):
        R._v90r29_cache.update(at=0,profiles={},summary={})
        R._v90r33_cache.update(at=0,n=0)
        with patch.object(P,"_v90r29_ensure"), patch.object(P,"_v90r29_backfill",return_value=0), \
             patch.object(P,"_v90r60_sanitize_duplicate_learning",return_value={}):
            r29 = P._v90r29_refresh(self.connect,force=True)
            r33 = P._v90r33_refresh(self.connect,force=True)
        self.assertEqual(r29["summary"]["eligible_episodes"],0)
        self.assertEqual(r29["profiles"],{})
        self.assertEqual(r33["n"],0)
        self.assertEqual(r33["edge_haircut"],1.0)

    def test_pending_backlog_is_quarantined_in_full_before_bounded_validation(self):
        before = self.financials()
        with self.connect() as c:
            result = LI.revalidate_eligible(c,batch_size=2)
            remaining_unsafe = c.execute("""SELECT count(*) AS n FROM v90_learning_episodes
              WHERE learning_eligible=TRUE AND NOT COALESCE(("""+LI.eligible_sql()+"""),FALSE)""").fetchone()["n"]
        self.assertEqual(result["staged"],4)
        self.assertEqual(result["processed"],2)
        self.assertEqual(result["pending"],2)
        self.assertEqual(remaining_unsafe,0)
        rows = self.episodes()
        self.assertFalse(rows["c_unknown"]["learning_eligible"])
        self.assertFalse(rows["d_synthetic"]["learning_eligible"])
        with self.connect() as c:
            complete = LI.revalidate_eligible(c,batch_size=2)
        self.assertEqual(complete["pending"],0)
        self.assertEqual(self.financials(),before)

    def test_failed_sanitizer_clears_cached_profiles_and_reader_gate_stays_closed(self):
        before = self.financials()
        R._v90r29_cache.update(at=10**12,profiles={"unsafe":{"n":100}},summary={"eligible_episodes":100})
        R._v90r33_cache.update(at=10**12,n=100,edge_haircut=.7)
        with patch.object(LI,"revalidate_eligible",side_effect=RuntimeError("validation unavailable")):
            result = self.sanitize()
        self.assertIn("validation unavailable",result["last_error"] or "")
        self.assertEqual(R._v90r29_cache["profiles"],{})
        self.assertEqual(R._v90r33_cache["n"],0)
        self.assertEqual(R._v90r33_cache["edge_haircut"],1.0)
        with self.connect() as c:
            readable = c.execute("SELECT count(*) AS n FROM v90_learning_episodes WHERE "+LI.readable_sql()).fetchone()["n"]
        self.assertEqual(readable,0)
        self.assertEqual(self.financials(),before)

    def test_production_autocommit_rolls_back_the_whole_failed_revalidation(self):
        before,episodes = self.financials(),self.episodes()
        revalidate = LI.revalidate_eligible
        def fail_after_updates(connection):
            setting=connection.execute("SHOW statement_timeout").fetchone()['statement_timeout']
            self.assertEqual(setting,'4s')
            result=revalidate(connection)
            self.assertGreater(result['processed'],0)
            raise RuntimeError('test failure after reclassification')
        with patch.object(R,'_v90r29_ensure'),patch.object(LI,'revalidate_eligible',side_effect=fail_after_updates):
            result=R._v90r44_sanitize_learning(self.autocommit_connect,force=True)
        self.assertIn('test failure after reclassification',result['last_error'])
        self.assertFalse(LI.memory_state()['ready'])
        self.assertEqual(self.episodes(),episodes)
        self.assertEqual(self.financials(),before)
        with patch.object(R,'_v90r29_ensure'):
            result=R._v90r44_sanitize_learning(self.autocommit_connect,force=True)
        self.assertIsNone(result['last_error'],result)
        self.assertTrue(LI.memory_state()['ready'])
        self.assertTrue(self.episodes()['a_clean']['learning_eligible'])
        self.assertEqual(self.financials(),before)

    def test_projection_omits_bar_arrays_and_changed_proof_is_immediately_unreadable(self):
        with self.connect() as c:
            c.execute("""UPDATE paper_trades SET payload=payload||%s::jsonb WHERE trade_id='a_clean'""",
              (json.dumps({"timeframe_entry_context":{"bars":[{"close":100}]*1000}}),))
            projected = c.execute("SELECT "+LI.trade_projection_sql()+" FROM paper_trades t WHERE trade_id='a_clean'").fetchone()
            self.assertNotIn("timeframe_entry_context",projected["payload"])
            self.assertIsNone(LI.trade_exclusion(dict(projected)))
        self.sanitize()
        with self.connect() as c:
            original = c.execute("SELECT "+LI.readable_sql()+" AS ok FROM v90_learning_episodes WHERE trade_id='a_clean'").fetchone()["ok"]
            self.assertTrue(original)
            c.execute("""UPDATE paper_trades SET payload=jsonb_set(payload,'{data_integrity_status}','"UNKNOWN"') WHERE trade_id='a_clean'""")
            stale = c.execute("SELECT "+LI.readable_sql()+" AS ok FROM v90_learning_episodes WHERE trade_id='a_clean'").fetchone()["ok"]
            self.assertFalse(stale)
        result = self.sanitize()
        self.assertIsNone(result["last_error"],result)
        self.assertFalse(self.episodes()["a_clean"]["learning_eligible"])

    def test_missing_accounting_or_observation_witness_cannot_be_recovered_from_old_metrics(self):
        with self.connect() as c:
            row = dict(c.execute("SELECT "+LI.trade_projection_sql()+" FROM paper_trades t WHERE trade_id='a_clean'").fetchone())
        self.assertIsNone(LI.trade_exclusion(row))
        for field in ("gross_pnl_rub","fees_rub","funding_rub","net_pnl_rub"):
            for value in (None,float("nan"),float("inf"),True):
                changed=copy.deepcopy(row); changed[field]=value
                self.assertEqual(LI.trade_exclusion(changed),"INCOMPLETE_ACCOUNTING")
        for field in ("mfe_pct","mae_pct"):
            changed=copy.deepcopy(row); changed["payload"][field]=None
            # Mutable percentages are not the recorded original quote witness.
            self.assertIsNone(LI.trade_exclusion(changed))
        changed=copy.deepcopy(row); changed["payload"].pop("observation_path")
        self.assertEqual(LI.trade_exclusion(changed),"MISSING_OBSERVATION_PATH")

    def test_prior_stop_error_is_recomputed_as_valid_loss_and_never_restored(self):
        raw = closed_trade(favorable_r=1.2)
        raw["trade_id"] = "loss_with_valid_initial_stop"
        raw["payload"].update(learning_label="RIGHT_DIRECTION_STOP_ERROR",
                              learning_conclusion="LEGACY_WIDEN_STOP_VERDICT")
        self.add_raw_trade(raw,attribution="RIGHT_DIRECTION_STOP_ERROR")
        before = self.financials()
        result = self.sanitize()
        self.assertIsNone(result["last_error"],result)
        saved = self.episodes()[raw["trade_id"]]
        self.assertTrue(saved["learning_eligible"])
        self.assertEqual(saved["primary_attribution"],"VALID_STRUCTURAL_STOP_LOSS")
        self.assertEqual(saved["learning_action"],"COUNT_STRATEGY_OUTCOME")
        self.assertNotIn("RIGHT_DIRECTION_STOP_ERROR",saved["attributions"])
        diagnosis=saved["payload"]["trade_diagnostics"]
        self.assertEqual(diagnosis["violations"],[])
        self.assertFalse(diagnosis["directional_error"])
        self.assertAlmostEqual(diagnosis["normalization"]["mfe_r"],1.2)
        self.assertAlmostEqual(diagnosis["normalization"]["mae_r"],-1.)
        # The obsolete verdict survives only as prior audit history, never as
        # the current learning attribution or an instruction to widen a stop.
        self.assertEqual(saved["payload"]["learning_integrity"]["prior_primary_attribution"],
                         "RIGHT_DIRECTION_STOP_ERROR")
        self.assertIsNone(self.sanitize()["last_error"])
        self.assertEqual(self.episodes()[raw["trade_id"]],saved)
        self.assertEqual(self.financials(),before)

    def test_missing_original_geometry_or_path_never_promotes_stale_positive_flags(self):
        cases = (
            ("first_fill",lambda p:p["entry_execution_model"].pop("fill_price")),
            ("initial_stop",lambda p:p.pop("initial_stop_price")),
            ("entry_atr",lambda p:p.pop("entry_atr")),
            ("observation_path",lambda p:p.pop("observation_path")),
            ("path_gap",lambda p:p["observation_path"].update(max_gap_seconds=120.,gap_count=1)),
            ("event_geometry",lambda p:p["entry_event_snapshot"].pop("stop_anchor")),
        )
        for key, mutate in cases:
            raw=closed_trade()
            raw["trade_id"]="missing_"+key
            mutate(raw["payload"])
            self.assertIsNotNone(LI.trade_exclusion(raw))
            self.add_raw_trade(raw,attribution="GOOD_EXECUTION")
        before=self.financials()
        self.assertIsNone(self.sanitize()["last_error"])
        saved=self.episodes()
        for key,_ in cases:
            row=saved["missing_"+key]
            self.assertFalse(row["learning_eligible"],key)
            self.assertEqual(row["payload"]["learning_integrity"]["status"],"EXCLUDED")
            self.assertNotEqual(row["primary_attribution"],"GOOD_EXECUTION")
        self.assertIsNone(self.sanitize()["last_error"])
        self.assertEqual(self.episodes(),saved)
        self.assertEqual(self.financials(),before)

    def test_first_fill_stop_atr_path_and_timeframe_are_bound_to_readable_hash(self):
        self.assertIsNone(self.sanitize()["last_error"])
        with self.connect() as c:
            original=dict(c.execute("SELECT * FROM paper_trades WHERE trade_id='a_clean'").fetchone())
            original_hash=c.execute("SELECT "+LI.evidence_hash_sql()+
                                    " AS h FROM paper_trades t WHERE trade_id='a_clean'").fetchone()["h"]
            mutations=(
                ("fill",lambda p:p["entry_execution_model"].update(fill_price=101.)),
                ("stop",lambda p:p.update(initial_stop_price=p["initial_stop_price"]-.1)),
                ("atr",lambda p:p.update(entry_atr=p["entry_atr"]+.1)),
                ("path",lambda p:p["observation_path"].update(max_price=102.)),
                ("tf",lambda p:p.update(execution_timeframe="5m")),
            )
            for key,mutate in mutations:
                with self.subTest(field=key):
                    p=copy.deepcopy(original["payload"]);mutate(p)
                    c.execute("UPDATE paper_trades SET payload=%s::jsonb WHERE trade_id='a_clean'",(json.dumps(p),))
                    changed_hash=c.execute("SELECT "+LI.evidence_hash_sql()+
                                           " AS h FROM paper_trades t WHERE trade_id='a_clean'").fetchone()["h"]
                    self.assertNotEqual(changed_hash,original_hash)
                    self.assertFalse(c.execute("SELECT "+LI.readable_sql()+
                        " AS ok FROM v90_learning_episodes WHERE trade_id='a_clean'").fetchone()["ok"])
                    c.execute("UPDATE paper_trades SET payload=%s::jsonb WHERE trade_id='a_clean'",
                              (json.dumps(original["payload"]),))
            c.execute("UPDATE paper_trades SET horizon='5m' WHERE trade_id='a_clean'")
            self.assertFalse(c.execute("SELECT "+LI.readable_sql()+
                " AS ok FROM v90_learning_episodes WHERE trade_id='a_clean'").fetchone()["ok"])
            c.execute("UPDATE paper_trades SET horizon=%s WHERE trade_id='a_clean'",(original["horizon"],))
            restored=dict(c.execute("SELECT * FROM paper_trades WHERE trade_id='a_clean'").fetchone())
            self.assertEqual(restored,original)

    def test_duplicate_observed_event_does_not_restore_two_learning_samples(self):
        self.add_trade("b_duplicate")
        with self.connect() as c:
            c.execute("""UPDATE paper_trades SET payload=(SELECT payload FROM paper_trades
              WHERE trade_id='a_clean') WHERE trade_id='b_duplicate'""")
        before=self.financials()
        with self.connect() as c:
            result=LI.revalidate_eligible(c,batch_size=32)
        self.assertEqual(result["verified"],1)
        rows=self.episodes()
        self.assertFalse(rows["b_duplicate"]["learning_eligible"])
        self.assertEqual(rows["b_duplicate"]["payload"]["learning_integrity"]["exclusion_reason"],
                         "DUPLICATE_OBSERVED_EVENT")
        self.assertEqual(self.financials(),before)

if __name__ == "__main__":
    unittest.main()
