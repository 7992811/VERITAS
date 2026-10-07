"""Resource-lifetime regressions without importing or starting the runtime.

Extract only the helpers under test. SQLite uses a temporary local database;
the cache tests use weak references, with no providers, network or trading loop.
"""
import ast
from contextlib import contextmanager
from copy import deepcopy
import gc
import json
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref


RUNTIME = Path(__file__).with_name("veritas_intelligence.py")


def isolated_helper(name, namespace):
    tree = ast.parse(RUNTIME.read_text(encoding="utf-8"), filename=str(RUNTIME))
    definitions = [node for node in tree.body
                   if isinstance(node, ast.FunctionDef) and node.name == name]
    if not definitions:
        raise AssertionError("runtime helper missing: " + name)
    unit = ast.Module(body=[definitions[-1]], type_ignores=[])
    exec(compile(unit, str(RUNTIME), "exec"), namespace)
    return namespace[name]


class SQLiteConnectionLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="veritas-memory-sqlite-")
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / "ledger.sqlite3")
        self.connections = []

        def connect(*args, **kwargs):
            connection = sqlite3.connect(*args, **kwargs)
            self.connections.append(connection)
            # Also release the connection if an assertion exposes the old leak.
            self.addCleanup(connection.close)
            return connection

        self.db = isolated_helper("db", {
            "sqlite3": SimpleNamespace(connect=connect, Row=sqlite3.Row),
            "DB_PATH": self.path,
            "contextmanager": contextmanager,
        })

    def assert_closed(self, connection):
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_success_commits_and_closes_even_when_cursor_is_retained(self):
        with self.db() as connection:
            connection.execute("CREATE TABLE events (value INTEGER NOT NULL)")
            retained_cursor = connection.execute("INSERT INTO events VALUES (7)")

        self.assert_closed(connection)
        with self.assertRaises(sqlite3.ProgrammingError):
            retained_cursor.execute("SELECT 1")

        with self.db() as reader:
            self.assertEqual(reader.execute("SELECT value FROM events").fetchone()[0], 7)
        self.assert_closed(reader)

    def test_exception_rolls_back_and_closes_without_masking_error(self):
        with self.db() as setup:
            setup.execute("CREATE TABLE events (value INTEGER NOT NULL)")
        self.assert_closed(setup)

        failure = RuntimeError("transaction failed")
        with self.assertRaises(RuntimeError) as caught:
            with self.db() as connection:
                connection.execute("INSERT INTO events VALUES (9)")
                raise failure

        self.assertIs(caught.exception, failure)
        self.assert_closed(connection)
        with self.db() as reader:
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
        self.assert_closed(reader)


class WeakRow(dict):
    __slots__ = ("__weakref__",)


def cached_function(value):
    def function():
        return None
    function._cache = value
    return function


def cache_namespace():
    return {
        "V90_MEMORY_CAUTION_MB": 280.0,
        "V90_MEMORY_PROTECT_MB": 340.0,
        "FULL_OVERVIEW_ENABLED": False,
        "analytics_cache": {},
        "analytics_cache_lock": threading.Lock(),
        "experience_cache": {"value": None, "at": 0.0},
        "trend_case_cache": {"value": None, "at": 0.0},
        "structure_analog_cache": {"value": None, "at": 0.0},
        "overview_cache": {"value": None, "at": 0.0},
        "overview_cache_lock": threading.Lock(),
        "market_cache": {},
        "market_cache_lock": threading.Lock(),
    }


def install_retained_vector_graph(namespace):
    row = WeakRow(asset="MOEX", horizon="1h", forward_return=0.01)
    vector = [0.1, 0.2, 0.3]
    rows = [row]
    vectors = [(row, vector)]
    # Match the real secondary index: new tuples share the original rows/vectors.
    index = {"1h": [(row, vector)]}
    namespace["_decision_memory_rows"] = cached_function((1.0, rows))
    namespace["_decision_memory_vectors"] = cached_function((1.0, vectors))
    namespace["_v842_vectors_by_horizon"] = cached_function(
        ((id(vectors), len(vectors)), index))
    return weakref.ref(row)


class DerivedVectorCacheLifetimeTests(unittest.TestCase):
    def test_pressure_cleanup_releases_rows_held_by_primary_and_derived_caches(self):
        namespace = cache_namespace()
        row = install_retained_vector_graph(namespace)
        prune = isolated_helper("_v90_prune_low_priority_caches", namespace)
        gc.collect()
        self.assertIsNotNone(row())

        prune(level_mb=300.0, preserve_active_cycle=False)
        gc.collect()

        self.assertIsNone(row(), "the horizon index still retains discarded episode rows")

    def test_asset_phase_preserves_the_current_cycle_vector_graph(self):
        namespace = cache_namespace()
        row = install_retained_vector_graph(namespace)
        prune = isolated_helper("_v90_prune_low_priority_caches", namespace)

        prune(level_mb=400.0, preserve_active_cycle=True)
        gc.collect()

        self.assertIsNotNone(row(), "asset cleanup must preserve active-cycle inputs")
        self.assertIsNotNone(namespace["_decision_memory_rows"]._cache)
        self.assertIsNotNone(namespace["_decision_memory_vectors"]._cache)
        self.assertIsNotNone(namespace["_v842_vectors_by_horizon"]._cache)


class ActiveCycleAllocatorBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.namespace = cache_namespace()
        self.trace, self.events = [], []
        self.rss = 500.0
        self.namespace["market_cache"]["CNYRUBF"] = {"bars": [12.75, 12.74]}
        self.namespace["analytics_cache"]["warm"] = {"score": 0.7}
        self.namespace["experience_cache"].update(value=[{"pnl": 1.25}], at=9.0)
        self.namespace["last_cycle"] = {"portfolios": [{"name": "Currency",
            "nav": 10010.15, "positions": [{"qty": -391.98, "entry_price": 12.756}]}]}
        self.financial = deepcopy(self.namespace["last_cycle"])
        self.warm = {key: self.namespace[key] for key in
                     ("market_cache", "analytics_cache", "experience_cache", "last_cycle")}
        self.warm_values = deepcopy(self.warm)
        self.vector_row = install_retained_vector_graph(self.namespace)

        def emit(event, **kw):
            self.trace.append(event)
            self.events.append((event, kw))
            return "emitted"

        def malloc_trim(value):
            self.assertEqual(value, 0)
            self.trace.append("malloc_trim")
            self.rss = 410.0
            return 1

        self.namespace.update(rss_mb=lambda: self.rss, emit=emit,
                              time=__import__('time'),
                              gc=SimpleNamespace(collect=lambda: self.trace.append("gc")))
        libc = SimpleNamespace(malloc_trim=malloc_trim)
        self.ctypes_patch = patch.dict("sys.modules", {
            "ctypes": SimpleNamespace(CDLL=lambda name: libc)})
        self.ctypes_patch.start()
        self.addCleanup(self.ctypes_patch.stop)
        for name in ("_v90_prune_low_priority_caches", "_v90_trim_memory", "_v90_emit_portfolio"):
            isolated_helper(name, self.namespace)

    def assert_warm_state(self):
        for key, value in self.warm.items():
            self.assertIs(self.namespace[key], value)
            self.assertEqual(value, self.warm_values[key])
        self.assertIsNotNone(self.vector_row())
        self.assertEqual(self.namespace["last_cycle"], self.financial)

    def test_explicit_boundary_and_legacy_asset_preserve_live_data_at_pressure(self):
        for phase, kwargs in (("horizon_CNYRUBF_1h", {"preserve_active_cycle": True}),
                              ("asset_CNYRUBF", {})):
            with self.subTest(phase=phase):
                self.trace.clear()
                self.rss = 500.0
                result = self.namespace["_v90_trim_memory"](phase, force=True, **kwargs)
                self.assertEqual(self.trace, ["gc", "malloc_trim", "memory_trim"])
                self.assertEqual(result["caches_cleared"], 0)
                self.assertEqual(result["after_mb"], 410.0)
                self.assertTrue(result["trimmed"])
                self.assert_warm_state()

    def test_low_pressure_unforced_call_retains_existing_early_return(self):
        self.rss = 200.0
        result = self.namespace["_v90_trim_memory"]("maintenance")
        self.assertEqual(result, {"phase": "maintenance", "before_mb": 200.0,
                                  "after_mb": 200.0, "trimmed": False})
        self.assertEqual(self.trace, [])
        self.assert_warm_state()

    def test_portfolio_phase_trims_before_emission_with_current_rss_and_original_kwargs(self):
        for phase in ("calibration_r33_done", "calibration_r29_done", "book_done",
                      "book_failed", "book_uncertain"):
            with self.subTest(phase=phase):
                self.trace.clear()
                self.rss = 500.0
                detail = {"nav": 10010.15}
                result = self.namespace["_v90_emit_portfolio"](
                    "paper_portfolio_phase", phase=phase, portfolio="Currency", detail=detail,
                    rss_mb=-1)
                self.assertEqual(self.trace, ["gc", "malloc_trim", "memory_trim",
                                              "paper_portfolio_phase"])
                self.assertEqual(result, "emitted")
                self.assertEqual(self.events[-2][1]["phase"], "portfolio_" + phase)
                self.assertGreaterEqual(self.events[-1][1]['cleanup_seconds'], 0)
                self.assertEqual({k: v for k, v in self.events[-1][1].items()
                                  if k != 'cleanup_seconds'}, {
                    "phase": phase, "portfolio": "Currency", "detail": detail, "rss_mb": 410.0})
                self.assertIs(self.events[-1][1]["detail"], detail)
                self.assert_warm_state()
        self.trace.clear()
        self.namespace["_v90_emit_portfolio"]("paper_order", qty=-391.98)
        self.assertEqual(self.trace, ["paper_order"])
        self.assertEqual(self.events[-1], ("paper_order", {"qty": -391.98, "rss_mb": 410.0}))

    def test_active_accounting_phases_do_not_collect_while_the_book_is_locked(self):
        for phase in ('book_start', 'protection_start', 'protection_done', 'future_phase'):
            with self.subTest(phase=phase):
                self.trace.clear()
                self.namespace['_v90_emit_portfolio']('paper_portfolio_phase', phase=phase)
                self.assertEqual(self.trace, ['paper_portfolio_phase'])
                self.assertEqual(self.events[-1][1], {'phase': phase, 'rss_mb': 500.0})
                self.assert_warm_state()

    def test_completed_horizon_releases_dead_locals_before_trim_and_next_features(self):
        tree = ast.parse(RUNTIME.read_text(encoding="utf-8"), filename=str(RUNTIME))
        cycle = next(node for node in tree.body
                     if isinstance(node, ast.FunctionDef) and node.name == "cycle")
        horizon = next(node for node in ast.walk(cycle) if isinstance(node, ast.For)
                       and isinstance(node.target, ast.Name) and node.target.id == "horizon"
                       and isinstance(node.iter, ast.Name) and node.iter.id == "processing_horizons")
        start = next(i for i, node in enumerate(horizon.body)
                     if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                     and isinstance(node.value.func, ast.Attribute)
                     and isinstance(node.value.func.value, ast.Name)
                     and node.value.func.value.id == "summary" and node.value.func.attr == "append")
        feature_assignment = next(node for node in horizon.body if isinstance(node, ast.Assign)
                                  and any(isinstance(t, ast.Name) and t.id == "f" for t in node.targets))
        ns = self.namespace
        released = {}
        for name in ("f", "v84_row", "z", "trade_plan", "cur", "dcur"):
            ns[name] = WeakRow(asset="CNYRUBF", horizon="1h", price=12.74, regime="TREND")
            released[name] = weakref.ref(ns[name])
        active = {name: {"still_needed": name} for name in
                  ("bundle", "raw", "deriv", "common_structure")}
        ns.update(active)
        ns.update(summary=[], entity_key="CNYRUBF:1h", asset="CNYRUBF", horizon="1h",
                  dec="SELL", conf=0.7, score=-0.5, kmatches=[], horizon_wall_t0=10.0,
                  time=SimpleNamespace(time=lambda: 12.0), horizon_timings={},
                  asset_timings={"CNYRUBF": {"horizons": 0.0}},
                  maybe_create_alert=lambda *a: self.trace.append("alert"),
                  pg_enabled=lambda: True,
                  _v90_compact_live_row=lambda row: {"price": row["price"]},
                  _v90_compact_decision_log=lambda row: {"price": row["price"]})
        observed = []

        def collect():
            observed.append({name: ref() is None for name, ref in released.items()})
            self.trace.append("gc")

        def features(raw, horizon_name, common):
            self.assertTrue(all(ref() is None for ref in released.values()))
            self.assertIs(raw, active["raw"])
            self.assertIs(common, active["common_structure"])
            self.assertEqual(horizon_name, "1h")
            self.trace.append("features")
            return {"next": True}

        ns["gc"] = SimpleNamespace(collect=collect)
        ns["features"] = features
        exec(compile(ast.Module(body=horizon.body[start:], type_ignores=[]), str(RUNTIME), "exec"), ns)
        self.assertEqual(observed, [{name: True for name in released}])
        self.assertEqual(ns["summary"], [{"price": 12.74}])
        self.assertEqual(ns["horizon_timings"], {"1h": 2.0})
        self.assertEqual(ns["asset_timings"]["CNYRUBF"]["horizons"], 2.0)
        self.assertEqual(self.events[-1][1]["phase"], "horizon_CNYRUBF_1h")
        for name, value in active.items():
            self.assertIs(ns[name], value)
        exec(compile(ast.Module(body=[feature_assignment], type_ignores=[]), str(RUNTIME), "exec"), ns)
        self.assertEqual(self.trace, ["alert", "decision", "gc", "malloc_trim", "memory_trim", "features"])
        self.assert_warm_state()


class SnapshotArchiveLifetimeTests(unittest.TestCase):
    """Exercise the real archive coordinator with disposable reader graphs."""
    def setUp(self):
        enabled = gc.isenabled()
        gc.disable()
        self.addCleanup(lambda: gc.enable() if enabled else None)
        self.addCleanup(gc.collect)
        self.ns = cache_namespace()
        self.trace, self.queries, self.events, self.refs = [], [], [], {}
        self.failure = None
        self.now_ts = 7200.0
        self.expected_drift = {"status": "WARN", "rule_drift_count": 120,
                               "rules": [{"rule": "retained"}], "agents": [{"agent": "macro"}]}
        self.expected_performance = [{"asset": "CNYRUBF", "horizon": "1h", "n": 50001,
                                     "avg_signed_return": -.00125, "avg_mae": 0.0}]
        self.expected_health = {"status": "ok", "entry_context_by_asset": {"CNYRUBF": {"eligible": True}},
                                "backtest": {"finished": True}}
        self.ns["market_cache"]["CNYRUBF"] = {"native": [12.75, 12.74]}
        self.ns["analytics_cache"]["warm"] = {"quality": .7}
        self.warm_values = deepcopy({k: self.ns[k] for k in ("market_cache", "analytics_cache")})
        self.replacement = {"status": "ok", "summary": [], "current_quote": 12.74}
        fields = {"asset": "CNYRUBF", "horizon": "1h", "decision": "SHORT",
                  "research_decision": "SHORT", "confidence": .7, "price": 12.74,
                  "regime": "TREND", "signal_tier": "SHORT", "execution_eligible": True,
                  "decision_stage": "READY", "horizon_structure_state": "CONFIRMED_TREND",
                  "horizon_structure_score": .8, "entry_quality": "FRESH_BREAKOUT",
                  "stop_price": 12.9, "target_price": 12.5, "expected_move_pct": .02,
                  "expected_to_stop_ratio": 1.5}
        ns, replacement = self.ns, self.replacement

        class ReplacedRow(WeakRow):
            def get(row, key, default=None):
                value = super().get(key, default)
                if key == "expected_to_stop_ratio":
                    # A fast-lane publisher may replace last_cycle while the
                    # archive still owns the old shallow snapshot.
                    ns["last_cycle"] = replacement
                return value

        row = ReplacedRow(fields, native_history="unused proof" * 20000)
        self.refs["old_row"] = weakref.ref(row)
        cycle = {"status": "ok", "at": "2026-10-07T13:25:24Z", "version": "TEST",
                 "cycle_mode": "FULL", "signal_cells": 49, "decisions_written": 49,
                 "outcomes_written": 0}
        self.expected_cycle = dict(cycle, summary=[fields])
        self.ns.update(last_cycle=dict(cycle, summary=[None, row]), lock=threading.Lock(),
                       pg_enabled=lambda: True, pg_connect=self.connect,
                       model_drift_status=lambda: self.reader("drift"),
                       pg_live_performance=lambda: self.reader("performance"),
                       product_health=lambda: self.reader("health"),
                       time=SimpleNamespace(time=lambda: self.now_ts),
                       now=lambda: "2026-10-07T13:25:24Z", json=json,
                       _v90r39_snapshot_state={"last_at": 0.0},
                       rss_mb=lambda: 500.0, gc=gc,
                       emit=lambda event, **kw: self.events.append((event, kw)),
                       _v90_memory_checkpoint=self.checkpoint)
        libc = SimpleNamespace(malloc_trim=lambda _: self.trace.append("malloc_trim"))
        self.ctypes = patch.dict(sys.modules, {"ctypes": SimpleNamespace(CDLL=lambda _: libc)})
        self.ctypes.start()
        self.addCleanup(self.ctypes.stop)
        isolated_helper("_v90_prune_low_priority_caches", self.ns)
        actual_trim = isolated_helper("_v90_trim_memory", self.ns)

        def trim(phase, **kw):
            self.trace.append(phase)
            result = actual_trim(phase, **kw)
            self.assertEqual(result["caches_cleared"], 0)
            if phase == "snapshot_end":
                self.assert_transients_released()
            return result

        self.ns["_v90_trim_memory"] = trim
        self.save = isolated_helper("save_product_snapshot", self.ns)

    def reader(self, name):
        self.trace.append(name)
        self.assertIsNone(self.refs["old_row"](),
                          "the archive must freeze and release live rows before historical reads")
        if name != "drift":
            previous = "drift" if name == "performance" else "performance"
            self.assertIsNone(self.refs[previous + "_scratch"]())
            self.assertIsNone(self.refs["old_row"]())
        scratch = WeakRow(proof="temporary reader state")
        scratch["cycle"] = scratch
        self.refs[name + "_scratch"] = weakref.ref(scratch)
        if self.failure == name:
            raise ValueError(name + " failed")
        value = deepcopy(getattr(self, "expected_" + name))
        result = WeakRow(value) if isinstance(value, dict) else WeakList(value)
        self.refs[name + "_result"] = weakref.ref(result)
        return result

    @contextmanager
    def connect(self):
        self.trace.append("db_open")
        self.assertIsNone(self.refs["health_scratch"]())
        owner = self

        class Connection:
            def execute(connection, sql, params=None):
                text = " ".join(sql.split())
                owner.queries.append((text, params))
                for name in ("drift", "performance", "health"):
                    owner.assertIsNotNone(owner.refs[name + "_result"]())
                if owner.failure == "persist":
                    raise RuntimeError("archive write failed")

        connection = Connection()
        self.refs["connection"] = weakref.ref(connection)
        try:
            yield connection
        finally:
            self.trace.append("db_close")

    def assert_transients_released(self):
        for name, ref in self.refs.items():
            if name != "old_row":
                self.assertIsNone(ref(), name + " outlived final cleanup")

    def checkpoint(self, phase, asset):
        self.trace.append(phase)
        if phase == "snapshot_ready":
            self.assert_transients_released()
            self.assertLess(self.trace.index("snapshot_end"), self.trace.index("snapshot_ready"))

    def test_archive_fields_sql_retention_and_reader_lifetimes_are_preserved(self):
        self.assertEqual(self.save(), {"status": "SAVED"})
        self.assertEqual(self.ns["_v90r39_snapshot_state"]["last_at"], self.now_ts)
        self.assertEqual([x for x in self.trace if x != "malloc_trim"], [
            "snapshot_start", "drift", "snapshot_drift_done", "performance",
            "snapshot_performance_done", "health", "snapshot_health_done",
            "db_open", "db_close", "snapshot_end", "snapshot_ready"])
        self.assertEqual([q for q, _ in self.queries], [
            "INSERT INTO product_snapshots(created_at,snapshot_type,payload) VALUES(%s,%s,%s::jsonb)",
            "INSERT INTO model_drift_snapshots(created_at,payload) VALUES(%s,%s::jsonb)",
            "DELETE FROM product_snapshots WHERE snapshot_id NOT IN ( SELECT snapshot_id FROM product_snapshots ORDER BY created_at DESC LIMIT 24 )",
            "DELETE FROM model_drift_snapshots WHERE snapshot_id NOT IN ( SELECT snapshot_id FROM model_drift_snapshots ORDER BY created_at DESC LIMIT 48 )"])
        first, second = self.queries[0][1], self.queries[1][1]
        self.assertEqual(first[:2], ("2026-10-07T13:25:24Z", "overview"))
        self.assertEqual(json.loads(first[2]), {"cycle": self.expected_cycle,
            "performance": self.expected_performance, "health": self.expected_health,
            "drift": self.expected_drift})
        self.assertEqual(json.loads(second[1]), self.expected_drift)
        self.assertIsNone(self.refs["old_row"]())
        self.assertIs(self.ns["last_cycle"], self.replacement)
        for name, value in self.warm_values.items():
            self.assertEqual(self.ns[name], value)
        self.assertFalse(any(e == "snapshot_error" for e, _ in self.events))

    def test_archive_failure_cleans_completed_stages_and_preserves_retry(self):
        for failure in ("drift", "performance", "health", "persist"):
            with self.subTest(failure=failure):
                case = SnapshotArchiveLifetimeTests("runTest")
                case.setUp()
                try:
                    case.failure = failure
                    result = case.save()
                    self.assertEqual(result["status"], "ERROR")
                    self.assertIn("archive write failed" if failure == "persist" else failure,
                                  result["error"])
                    case.assert_transients_released()
                    self.assertEqual(case.ns["_v90r39_snapshot_state"]["last_at"], 0.0)
                    self.assertNotIn("snapshot_ready", case.trace)
                    self.assertEqual(case.trace[-2:], ["snapshot_end", "malloc_trim"])
                    self.assertEqual(sum(e == "snapshot_error" for e, _ in case.events), 1)
                    if failure == "persist":
                        self.assertIn("db_close", case.trace)
                finally:
                    case.doCleanups()

    def test_archive_throttle_and_disabled_database_do_not_run_readers_or_gc(self):
        self.ns["_v90r39_snapshot_state"]["last_at"] = self.now_ts - 3599
        self.assertEqual(self.save(), {"status": "NOT_DUE"})
        self.ns["_v90r39_snapshot_state"]["last_at"] = 0.0
        self.ns["pg_enabled"] = lambda: False
        self.assertEqual(self.save(), {"status": "DEFERRED_STORAGE"})
        self.assertEqual(self.trace, [])
        self.assertEqual(self.queries, [])
        self.assertEqual(self.events, [])

    def test_supplied_full_projection_keeps_original_cycle_and_stamps_later_statistics(self):
        frozen = deepcopy(self.expected_cycle)
        self.ns["last_cycle"] = dict(self.replacement, cycle_mode="FAST_5M",
                                     at="2026-10-07T13:30:00Z")
        observed_at = "2026-10-07T13:30:31Z"
        self.ns["now"] = lambda: observed_at
        admitted = []

        result = self.save(cycle_snapshot=frozen, admission=admitted.append)

        self.assertEqual(result, {"status": "SAVED"})
        self.assertEqual(frozen, self.expected_cycle)
        payload = json.loads(self.queries[0][1][2])
        self.assertEqual(payload["cycle"], self.expected_cycle)
        self.assertEqual(payload["capture"], {
            "requested_cycle_at": self.expected_cycle["at"],
            "health_observed_at": observed_at,
            "statistics_finished_at": observed_at,
            "basis": "FROZEN_FULL_CYCLE_WITH_SEPARATELY_MEASURED_STATISTICS"})
        self.assertEqual(admitted, ["snapshot_drift", "snapshot_performance",
                                    "snapshot_health", "snapshot_persist"])
        self.assertEqual(self.ns["last_cycle"]["cycle_mode"], "FAST_5M")
        self.assert_transients_released()

    def test_memory_deferral_releases_partial_reports_and_preserves_hourly_retry(self):
        from veritas_maintenance import MaintenanceDeferred

        for stage in ("snapshot_drift", "snapshot_performance", "snapshot_health", "snapshot_persist"):
            with self.subTest(stage=stage):
                case = SnapshotArchiveLifetimeTests("runTest")
                case.setUp()
                try:
                    def admission(current):
                        if current == stage:
                            raise MaintenanceDeferred("DEFERRED_MEMORY", stage=current,
                                                      rss_mb=301., memory_start_limit_mb=280.)

                    result = case.save(admission=admission)
                    self.assertEqual(result, {"status": "DEFERRED_MEMORY", "stage": stage,
                                              "rss_mb": 301., "memory_start_limit_mb": 280.})
                    self.assertEqual(case.ns["_v90r39_snapshot_state"]["last_at"], 0.)
                    self.assertEqual(case.queries, [])
                    self.assertNotIn("snapshot_ready", case.trace)
                    self.assertFalse(any(event == "snapshot_error" for event, _ in case.events))
                    case.assert_transients_released()
                    self.assertEqual(case.save(cycle_snapshot=case.expected_cycle,
                                               admission=lambda current: None), {"status": "SAVED"})
                    self.assertEqual(case.ns["_v90r39_snapshot_state"]["last_at"], case.now_ts)
                finally:
                    case.doCleanups()


class WeakList(list):
    __slots__ = ("__weakref__",)


class SnapshotPerformanceDenseBufferTests(unittest.TestCase):
    def test_binary64_buffers_preserve_legacy_aggregate_bits(self):
        from test_veritas_snapshot_memory_sql import load_reader, MemoryConnection

        def bits(value):
            if isinstance(value, float):
                return ("binary64", struct.pack("!d", value))
            if isinstance(value, dict):
                return {key: bits(item) for key, item in value.items()}
            if isinstance(value, list):
                return [bits(item) for item in value]
            return value

        cases = {"cancel": [1e16, 1.0, -1e16, 3.25, -8.5, 5e-324, -5e-324],
                 "negative_zero": [-0.0, "-0.0", 0.0],
                 "nan": [float("nan"), -0.0, 1.0],
                 "infinity": [float("inf"), 1.0],
                 "negative_infinity": [float("-inf"), -1.0],
                 "opposite_infinities": [float("inf"), float("-inf")]}
        for case, values in cases.items():
            with self.subTest(case=case):
                rows = [{"asset": "CNYRUBF", "horizon": "1h",
                         "decision": {"decision": direction},
                         "outcome": {"forward_return": value,
                                     "mfe": None if i % 3 else value,
                                     "mae": value if i % 2 else -0.0}}
                        for direction in ("LONG", "SHORT", "NO_TRADE")
                        for i, value in enumerate(values)]
                rows.append({"asset": "CNYRUBF", "horizon": "1h",
                             "decision": {"decision": "LONG"}, "outcome": {"forward_return": None}})
                old, current = MemoryConnection(rows), MemoryConnection(rows)
                self.assertEqual(bits(load_reader(current.connect)()),
                                 bits(load_reader(old.connect, legacy=True)()))

    def test_reader_retains_compact_double_buffers_for_all_observations(self):
        from array import array
        import veritas_learning_memory as memory
        refs, observed = [], {}
        count = 4096

        def buffer(typecode):
            self.assertEqual(typecode, "d")
            value = array(typecode)
            refs.append(weakref.ref(value))
            return value

        def rows():
            for i in range(count):
                yield {"asset": "CNYRUBF", "horizon": "1h", "decision": {"decision": "SHORT"},
                       "outcome": {"forward_return": -.1/(i+1), "mfe": .3/(i+1), "mae": -.2/(i+1)}}

        @contextmanager
        def stream(_):
            yield rows()
            buffers = [ref() for ref in refs if ref() is not None]
            observed.update(values=sum(map(len, buffers)),
                            bytes=sum(sys.getsizeof(value) for value in buffers))

        reader = isolated_helper("pg_live_performance", {
            "pg_enabled": lambda: True, "pg_connect": lambda: None, "json": json})
        with patch("array.array", side_effect=buffer), \
                patch.object(memory, "live_performance_rows", stream):
            result = reader()
        self.assertEqual(result[0]["n"], count)
        self.assertEqual(result[0]["directional_n"], count)
        self.assertEqual(observed["values"], count*4)
        # Includes allocated array capacity and object headers; Python float
        # objects plus list references would require at least 32 bytes/value.
        self.assertLess(observed["bytes"], observed["values"]*12)
        self.assertTrue(all(ref() is None for ref in refs))


if __name__ == "__main__":
    unittest.main()
