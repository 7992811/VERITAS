"""Exercise actual publication/archive bindings without starting the service.

The cycle test keeps a real maintenance worker blocked while the extracted
market tail returns. SQL fixtures verify operational retries and connection
lifetimes; they never connect to a database or run a trading loop.
"""
import ast
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

import veritas_maintenance as VM
import veritas_storage_guard as VSG
import veritas_signal_publication as VSP


RUNTIME = Path(__file__).with_name("veritas_intelligence.py")


def definition(name):
    nodes = [node for node in ast.parse(RUNTIME.read_text()).body
             if isinstance(node, ast.FunctionDef) and node.name == name]
    return deepcopy(nodes[-1])


def compile_function(node, ns):
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    exec(compile(module, str(RUNTIME), "exec"), ns)
    return ns[node.name]


class ProofRow(dict):
    pass


class CyclePublicationIntegrationTests(unittest.TestCase):
    def test_full_is_frozen_under_publication_lock_and_slow_archive_cannot_hold_cycle(self):
        node = definition("cycle")
        publication = next(i for i, block in enumerate(node.body)
            if isinstance(block, ast.With) and any(isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "_maintenance_request" for t in n.targets)
                for n in ast.walk(block)))
        self.assertTrue(any(isinstance(item.context_expr, ast.Name)
                            and item.context_expr.id == "lock" for item in node.body[publication].items))
        # Keep the real publication block, releases, GC boundary and complete tail.
        node.body = ast.parse("state = make_state()\nsummary = []\nmarket_bundles = {}").body + node.body[publication:]
        lock = threading.Lock()
        archive_entered, release_archive, cycle_finished = (threading.Event() for _ in range(3))
        trace, references, requests, captured, failures = [], {}, [], [], []
        frozen_at, fast_at = "2026-10-07T16:10:00Z", "2026-10-07T16:11:00Z"
        last = {"cycle_mode": "PREVIOUS", "summary": []}

        def learning_progress():
            self.fail("the completed learning cache should suffice")
        learning_progress._cache = (1., {"index_version": "TEST_INDEX"})

        def archive(cycle_snapshot=None, admission=None):
            captured.append(deepcopy(cycle_snapshot))
            archive_entered.set()
            if not release_archive.wait(5.):
                raise TimeoutError("test did not release archive")
            return {"status": "SAVED"}

        ns = {"emit": lambda *args, **kwargs: None, "rss_mb": lambda: 200.,
              "pg_enabled": lambda: True, "save_product_snapshot": archive,
              "learning_progress": learning_progress, "VLI": SimpleNamespace(INDEX_VERSION="TEST_INDEX"),
              "_v90_daily_intelligence_metrics": lambda lp: {"status": "OK"},
              "_v90r37_storage_retention": lambda: {"status": "OK"},
              "_v90_outcome_state": {"status": "IDLE"}}
        ns["_v90_run_outcome_refresh"] = lambda reason: ns["_v90_outcome_state"].update(status="OK")
        lane = VM.MaintenanceLane(ns, retry_seconds=.01)
        self.addCleanup(lane.close)
        self.addCleanup(release_archive.set)

        def make_state():
            row = ProofRow(asset="NQ", horizon="1h", decision="LONG", price=25000.,
                           native_history={"unused_proof": "do not retain" * 20000})
            references["row"] = weakref.ref(row)
            return {"status": "ok", "cycle_mode": "FULL", "at": frozen_at, "version": "TEST",
                    "signal_cells": 1, "decisions_written": 1, "outcomes_written": 0,
                    "summary": [row], "portfolio_autopilot": {"status": "OK"}}

        def prepare(state):
            self.assertTrue(lock.locked(), "FULL capture must happen in the publication critical section")
            self.assertEqual(last["cycle_mode"], "PREVIOUS")
            trace.append("prepare")
            request = lane.prepare(state)
            requests.append(request)
            return request

        def trim(phase, force=False):
            self.assertEqual((phase, force), ("cycle_end", True))
            self.assertFalse(lock.locked())
            trace.append("trim")
            # Reproduce the independent structural publisher replacing rows
            # while a finishing FULL cycle releases its previous owners.
            last.clear()
            last.update(cycle_mode="FAST_5M", at=fast_at, summary=[{"asset": "NQ", "price": 25010.}])
            self.assertIsNone(references["row"](), "the prepared request retained the old proof graph")

        def submit(request):
            self.assertFalse(lock.locked())
            self.assertIs(request, requests[0])
            trace.append("submit")
            return lane.submit(request)

        ns.update(make_state=make_state, lock=lock, last_cycle=last, VSP=VSP,
                  VBR=SimpleNamespace(publish_summary=lambda rows: list(rows), snapshot=lambda: {}),
                  _v90_background_maintenance=SimpleNamespace(prepare=prepare, submit=submit),
                  _v90_trim_memory=trim, made=1, outcomes=0, status="ok", storage={"ok": True},
                  telemetry={"elapsed_seconds": 1.}, heavy_learning_due=lambda: False)
        run = compile_function(node, ns)

        def market():
            try:
                run(cycle_mode="FULL")
            except BaseException as exc:
                failures.append(exc)
            finally:
                cycle_finished.set()

        thread = threading.Thread(target=market, daemon=True)
        with patch.dict("sys.modules", {"veritas_structural_lifecycle":
                SimpleNamespace(merge_reports=lambda current, previous: current)}):
            try:
                thread.start()
                self.assertTrue(cycle_finished.wait(2.), "archive held the market cycle open")
                if failures:
                    raise failures[0]
                self.assertTrue(archive_entered.wait(2.))
                self.assertFalse(release_archive.is_set())
                self.assertEqual(trace, ["prepare", "trim", "submit"])
                self.assertEqual(captured[0]["at"], frozen_at)
                self.assertEqual(captured[0]["cycle_mode"], "FULL")
                self.assertEqual(captured[0]["summary"][0]["price"], 25000.)
                self.assertNotIn("native_history", captured[0]["summary"][0])
                self.assertEqual(last["at"], fast_at)
                self.assertEqual(last["summary"][0]["price"], 25010.)
                self.assertIsInstance(requests[0], VM.CycleRequest)
                self.assertEqual(json.loads(requests[0].payload), captured[0])
            finally:
                release_archive.set()
                thread.join(2.)
                lane.close()

    def test_final_maintenance_install_follows_quota_wrappers_and_precedes_main(self):
        tree = ast.parse(RUNTIME.read_text())
        calls = {n.value.func.id: i for i, n in enumerate(tree.body)
                 if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                 and isinstance(n.value.func, ast.Name)}
        main = next(i for i, n in enumerate(tree.body) if isinstance(n, ast.If)
                    and "__name__" in ast.unparse(n.test))
        self.assertLess(calls["_v90_install_storage_guard"], calls["_install_maintenance"])
        self.assertLess(calls["_install_maintenance"], main)


class RetentionDatabase:
    def __init__(self, failure=None):
        self.failure, self.connections, self.queries = failure, [], []

    def connection(self, kind):
        owner = self
        class Connection:
            closed = False
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.closed = True
            def execute(self, sql, params=None):
                text = " ".join(sql.split())
                owner.queries.append((kind, text, params))
                if owner.failure == "delete" and "DELETE FROM ledger_events" in text:
                    raise RuntimeError("delete fixture failure")
                if owner.failure == "vacuum" and text.startswith("VACUUM"):
                    raise RuntimeError("vacuum fixture failure")
                return SimpleNamespace(fetchall=lambda: [{"count": 1}, {"count": 1}])
        connection = Connection()
        self.connections.append((kind, connection))
        return connection


class RetentionIntegrationTests(unittest.TestCase):
    def fixture(self, failure=None):
        db, clock, events = RetentionDatabase(failure), [7200.], []
        state = {"last": 0.}
        ns = {"pg_enabled": lambda: True, "pg_connect": lambda: db.connection("delete"),
              "psycopg": SimpleNamespace(connect=lambda *args, **kwargs: db.connection("vacuum")),
              "DATABASE_URL": "unused-test-url", "dict_row": object(),
              "time": SimpleNamespace(time=lambda: clock[0]), "_v90r37_maintenance_state": state,
              "emit": lambda event, **kw: events.append((event, kw))}
        return compile_function(definition("_v90r37_storage_retention"), ns), db, clock, state, events

    def test_retention_keeps_existing_limits_and_two_hour_success_interval(self):
        retain, db, clock, state, events = self.fixture()
        result = retain()
        self.assertEqual(result["status"], "OK")
        specs = [("decision", 1500), ("setup_learning", 500), ("admission_learning", 500),
                 ("trade_counterfactual_lab", 300), ("impulse_genesis_learning", 300), ("meta_signal", 200)]
        context = [(sql, args) for kind, sql, args in db.queries if "DELETE FROM ledger_events" in sql]
        self.assertEqual([args for sql, args in context], [(name, limit, name) for name, limit in specs])
        for sql, args in context:
            self.assertIn("LIMIT 10000", sql)
            self.assertIn("o.event_type='outcome'", sql)
            self.assertIn("o.entity_key=ledger_events.entity_key", sql)
        cleanup = [sql for kind, sql, args in db.queries if sql.startswith("DELETE")]
        self.assertEqual(cleanup, [
            "DELETE FROM product_snapshots WHERE snapshot_id NOT IN ( SELECT snapshot_id FROM product_snapshots ORDER BY created_at DESC LIMIT 24 )",
            "DELETE FROM model_drift_snapshots WHERE snapshot_id NOT IN ( SELECT snapshot_id FROM model_drift_snapshots ORDER BY created_at DESC LIMIT 48 )",
            "DELETE FROM product_alerts WHERE created_at<NOW()-INTERVAL '24 hours'",
            "DELETE FROM paper_nav_history WHERE observed_at<NOW()-INTERVAL '7 days'"])
        self.assertEqual([sql for kind, sql, args in db.queries if kind == "vacuum"], [
            "SET search_path TO veritas_v90", "SET statement_timeout='60000ms'", "VACUUM (ANALYZE) ledger_events"])
        self.assertTrue(all(connection.closed for kind, connection in db.connections))
        self.assertEqual(state["last"], clock[0])
        count = len(db.connections)
        self.assertEqual(retain(), {"status": "NOT_DUE"})
        self.assertEqual(len(db.connections), count)
        clock[0] += 7200.
        self.assertEqual(retain()["status"], "OK")
        self.assertEqual(state["last"], clock[0])

    def test_delete_and_vacuum_failures_close_connections_without_consuming_retry(self):
        for failure in ("delete", "vacuum"):
            with self.subTest(failure=failure):
                retain, db, clock, state, events = self.fixture(failure)
                result = retain()
                self.assertEqual(result["status"], "ERROR")
                self.assertIn(failure + " fixture failure", result["error"])
                self.assertEqual(state["last"], 0.)
                self.assertTrue(all(connection.closed for kind, connection in db.connections))
                self.assertFalse(any(event == "v90_r37_storage_retention" for event, _ in events))
                if failure == "delete":
                    self.assertFalse(any(kind == "vacuum" for kind, connection in db.connections))
                db.failure = None
                self.assertEqual(retain()["status"], "OK")
                self.assertEqual(state["last"], clock[0])
                self.assertTrue(all(connection.closed for kind, connection in db.connections))


class StorageWrapperIntegrationTests(unittest.TestCase):
    def fixture(self, base_retention):
        used = [1024 * 1024]
        calls = []
        class Connection:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def execute(self, sql):
                if not (sql.startswith("SET statement_timeout") or sql.startswith("SELECT pg_database_size")):
                    raise AssertionError("unexpected storage fixture SQL: " + sql)
                return SimpleNamespace(fetchone=lambda: (used[0],))
        ns = {"DATABASE_URL": "unused-test-url",
              "psycopg": SimpleNamespace(connect=lambda *args, **kw: Connection()),
              "emit": lambda *args, **kw: None, "_v90r37_maintenance_state": {"last": 123.},
              "_v90r37_storage_retention": base_retention,
              "save_product_snapshot": lambda *args, **kw: calls.append((args, kw)) or {"status": "SAVED"}}
        with patch.dict("os.environ", {"VERITAS_STORAGE_SOFT_BYTES": str(128 * 1024 * 1024),
                                       "VERITAS_STORAGE_HARD_BYTES": str(200 * 1024 * 1024)}):
            VSG.install_storage_guard(ns)
        return ns, used, calls

    def test_quota_wrapper_forwards_frozen_context_and_admission_and_reports_deferral(self):
        ns, used, calls = self.fixture(lambda: {"status": "OK"})
        frozen, admission = {"cycle_mode": "FULL", "at": "frozen"}, lambda stage: None
        self.assertEqual(ns["save_product_snapshot"](cycle_snapshot=frozen, admission=admission), {"status": "SAVED"})
        self.assertIs(calls[0][1]["cycle_snapshot"], frozen)
        self.assertIs(calls[0][1]["admission"], admission)
        used[0] = 160 * 1024 * 1024
        ns["_v90r42_storage_guard"](force=True)
        self.assertEqual(ns["save_product_snapshot"](cycle_snapshot=frozen, admission=admission),
                         {"status": "DEFERRED_STORAGE_QUOTA", "tier": "AMBER"})
        self.assertEqual(len(calls), 1)

    def test_failed_quota_retention_retries_before_success_starts_thirty_minute_interval(self):
        attempts = []
        def retain():
            attempts.append(ns["_v90r37_maintenance_state"]["last"])
            return {"status": "ERROR" if len(attempts) == 1 else "OK"}
        with patch.object(VSG, "time", SimpleNamespace(time=lambda: 7200.)):
            ns, used, calls = self.fixture(retain)
            used[0] = 160 * 1024 * 1024
            self.assertEqual(ns["_v90r37_storage_retention"](), {"status": "ERROR"})
            self.assertEqual(ns["_v90r37_storage_retention"](), {"status": "OK"})
            self.assertEqual(ns["_v90r37_storage_retention"]()["status"], "NOT_DUE_QUOTA_GUARD")
        self.assertEqual(attempts, [0., 0.])


HEALTH_CLOCK = datetime(2026, 10, 7, 16, 30, tzinfo=timezone.utc)


class HealthDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return HEALTH_CLOCK.astimezone(tz) if tz else HEALTH_CLOCK.replace(tzinfo=None)


LATEST_BACKTEST_SQL = "SELECT run_id,started_at,finished_at,status,days,sample_step_hours,rules_tested,observations,details FROM backtest_runs ORDER BY started_at DESC LIMIT 1"
BACKTEST_BOARD_SQL = "SELECT rule_id,asset,horizon,action,method,n,hit_rate,avg_signed_return,avg_mfe,avg_mae,period_start,period_end FROM knowledge_backtest_stats WHERE n>=20 ORDER BY n DESC,hit_rate DESC NULLS LAST LIMIT 40"
BACKTEST_OOS_SQL = "SELECT rule_id,asset,horizon,action,sample,n,hit_rate,avg_signed_return,avg_mfe,avg_mae,std_signed_return,t_stat,profit_factor,p_value,p_bonferroni,period_start,period_end FROM knowledge_backtest_oos_stats WHERE sample='OOS' AND n>=20 ORDER BY p_bonferroni ASC NULLS LAST,n DESC LIMIT 60"


class BacktestProjectionTests(unittest.TestCase):
    def fixture(self, enabled=True, failure=None):
        latest = dict(run_id=17, started_at="start", finished_at="finish", status="ok",
                      days=90, sample_step_hours=4, rules_tested=25, observations=700,
                      details={"method": "original", "oos_share": .2})
        boards = [{"rule_id": "in-sample", "n": 42}]
        oos = [{"rule_id": "out-of-sample", "sample": "OOS", "n": 31}]
        queries, closed = [], []
        class Connection:
            def __enter__(self):
                if failure == "connect":
                    raise RuntimeError("connect fixture")
                return self
            def __exit__(self, *args):
                closed.append(True)
            def execute(self, sql):
                queries.append(sql)
                if failure == "latest" and sql == LATEST_BACKTEST_SQL:
                    raise RuntimeError("latest fixture")
                if sql not in (LATEST_BACKTEST_SQL, BACKTEST_BOARD_SQL, BACKTEST_OOS_SQL):
                    raise AssertionError("backtest population or query changed")
                return SimpleNamespace(fetchone=lambda: deepcopy(latest),
                    fetchall=lambda: deepcopy(boards if sql == BACKTEST_BOARD_SQL else oos))
        state = {"status": "starting", "reason": "cached-state"}
        ns = {"backtest_state": state, "BACKTEST_ENABLED": True, "BACKTEST_DAYS": 90,
              "BACKTEST_SAMPLE_STEP_HOURS": 4, "BACKTEST_REFRESH_HOURS": 24,
              "pg_enabled": lambda: enabled, "pg_connect": Connection}
        return ns, compile_function(definition("backtest_status"), ns), queries, closed, latest, boards, oos

    def test_health_option_reads_only_unchanged_latest_run_and_default_keeps_both_boards(self):
        ns, read, queries, closed, latest, boards, oos = self.fixture()
        before = deepcopy(ns["backtest_state"])
        health = read(include_leaderboards=False)
        self.assertEqual(queries, [LATEST_BACKTEST_SQL])
        self.assertEqual(health["latest_run"], latest)
        self.assertNotIn("top", health)
        self.assertNotIn("oos_top", health)
        queries.clear()
        complete = read()
        self.assertEqual(queries, [LATEST_BACKTEST_SQL, BACKTEST_BOARD_SQL, BACKTEST_OOS_SQL])
        self.assertEqual(complete, dict(health, top=boards, oos_top=oos))
        self.assertEqual(ns["backtest_state"], before)
        self.assertEqual(closed, [True, True])

    def test_failed_and_unavailable_backtest_keep_cached_metadata_and_health_output(self):
        for enabled, failure in ((False, None), (True, "connect"), (True, "latest")):
            with self.subTest(enabled=enabled, failure=failure):
                ns, read, queries, closed, latest, boards, oos = self.fixture(enabled, failure)
                full = read()
                projected = read(include_leaderboards=False)
                self.assertEqual(projected, full)
                self.assertEqual(projected["status"], "starting")
                self.assertEqual(projected["reason"], "cached-state")
                self.assertIsNone(projected.get("latest_run"))
                if failure:
                    self.assertEqual(projected["db_error"], "RuntimeError: " + failure + " fixture")
                else:
                    self.assertEqual(queries, [])
                ns.update(lock=threading.Lock(), last_cycle={"at": "2026-10-07T16:25:00Z",
                          "status": "ok", "summary": []}, datetime=HealthDatetime, timezone=timezone,
                          pg_storage_status=lambda: {"ok": True}, VERSION="TEST", PRODUCT_STALE_MINUTES=10)
                health = compile_function(definition("product_health"), ns)()
                self.assertIsNone(health["backtest"])
                self.assertEqual(health["status"], "ok")
                self.assertEqual(health["cycle_age_min"], 5.)
                self.assertEqual(health["entry_context_by_asset"], {})


class ProductHealthLifetimeTests(unittest.TestCase):
    def test_preferred_contexts_match_existing_gates_and_release_proofs_before_blocking_database(self):
        import veritas_trend_entry as VTE
        actual_gate = VTE.context_gate
        lock = threading.Lock()
        specs = [("NQ", "1h", 70), ("NQ", "5m", 80), ("NQ", "4h", 90),
                 ("BTC", "1h", 100), ("BTC", "4h", 110),
                 ("ETH", "5m", 120), ("ETH", "5m", 1100),
                 ("GOLD", "4h", 130), ("BRENT", "1h", 140), ("BRENT", "5m", 150),
                 ("MOEX", "1h", 160), ("CNYRUBF", "5m", 170)]
        expected_indices = [1, 3, 6, 7, 9, 10, 11]
        expected, references, rows = {}, [], []
        for index, (asset, tf, age) in enumerate(specs):
            proof = ProofRow(native_bars="unused history" * 10000)
            row = ProofRow(asset=asset, horizon=tf, fixture_index=index, native_history=proof,
                           trend_entry_context={"status": "OK", "closed_at": HEALTH_CLOCK.timestamp()-age})
            references.extend((weakref.ref(row), weakref.ref(proof)))
            rows.append(row)
            if index in expected_indices:
                expected[asset] = actual_gate(row, HEALTH_CLOCK)
        row = proof = None
        last = {"at": "2026-10-07T16:25:00Z", "status": "ok", "summary": rows}
        rows = None
        database_entered, release_database, finished = (threading.Event() for _ in range(3))
        calls, backtest_arguments, results, failures = [], [], [], []
        self.addCleanup(release_database.set)

        def gate(row, at):
            self.assertFalse(lock.locked(), "health gates must not hold the publication lock")
            calls.append(row["fixture_index"])
            return actual_gate(row, at)

        def storage():
            self.assertEqual(calls, expected_indices)
            with lock:
                last.clear()
                last.update(status="new-fast-state", at="2026-10-07T16:30:00Z", summary=[])
            self.assertTrue(all(ref() is None for ref in references),
                            "health still retains live proofs during its SQL phase")
            database_entered.set()
            if not release_database.wait(5.):
                raise TimeoutError("test did not release health database")
            return {"ok": True, "backend": "fixture"}

        def backtest(include_leaderboards=True):
            backtest_arguments.append(include_leaderboards)
            self.assertTrue(all(ref() is None for ref in references))
            return {"latest_run": {"run_id": 17}}

        ns = dict(lock=lock, last_cycle=last, datetime=HealthDatetime, timezone=timezone,
                  pg_storage_status=storage, backtest_status=backtest, VERSION="TEST", PRODUCT_STALE_MINUTES=10)
        read = compile_function(definition("product_health"), ns)
        def worker():
            try:
                results.append(read())
            except BaseException as exc:
                failures.append(exc)
            finally:
                finished.set()
        thread = threading.Thread(target=worker, daemon=True)
        with patch.object(VTE, "context_gate", gate):
            try:
                thread.start()
                entered = database_entered.wait(2.)
                if failures:
                    raise failures[0]
                self.assertTrue(entered)
                self.assertFalse(finished.is_set())
                self.assertTrue(all(ref() is None for ref in references))
            finally:
                release_database.set()
                thread.join(2.)
        if failures:
            raise failures[0]
        self.assertTrue(finished.is_set())
        self.assertEqual(results[0]["entry_context_by_asset"], expected)
        self.assertEqual(results[0]["cycle_status"], "ok")
        self.assertEqual(results[0]["cycle_age_min"], 5.)
        self.assertEqual(results[0]["status"], "ok")
        self.assertEqual(results[0]["backtest"], {"run_id": 17})
        self.assertEqual(backtest_arguments, [False])
        self.assertEqual(last["status"], "new-fast-state")


class DecisionLogProjectionIntegrationTests(unittest.TestCase):
    def test_actual_decision_log_projects_context_while_live_row_keeps_canonical_proof(self):
        from test_veritas_weighted_structural_economics import structural_plan
        raw, clock, plan = structural_plan()
        row = dict(raw, horizon="1h", research_decision="LONG", trade_plan=plan)
        original = deepcopy(row)
        identity_fields=next(node.value for node in ast.parse(RUNTIME.read_text()).body
                             if isinstance(node,ast.Assign) and any(isinstance(target,ast.Name)
                             and target.id=='_V90_QUOTE_IDENTITY_FIELDS' for target in node.targets))
        ns = {'_V90_QUOTE_IDENTITY_FIELDS':ast.literal_eval(identity_fields)}
        for name in ("_v90_small_dict", "_v90_compact_live_row", "_v90_compact_decision_log"):
            compile_function(definition(name), ns)
        live = ns["_v90_compact_live_row"](row)
        output = ns["_v90_compact_decision_log"](row)
        canonical = plan["timeframe_entry_context"]
        projected = output["timeframe_entry_context"]
        self.assertIs(live["trade_plan"]["timeframe_entry_context"], canonical)
        self.assertEqual(projected["event"]["event_id"], canonical["event"]["event_id"])
        self.assertEqual(projected["event"]["proof_hash"], canonical["event"]["proof_hash"])
        self.assertEqual(projected["quote_observed_at"], canonical["quote_observed_at"])
        self.assertLess(len(json.dumps(projected)), len(json.dumps(canonical))/4)
        self.assertEqual(row, original)
        projected["event"]["stop_price"] = -1.
        self.assertEqual(row, original)


if __name__ == "__main__":
    unittest.main()
