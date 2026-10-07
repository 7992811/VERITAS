"""Resource-lifetime regressions without importing or starting the runtime.

Extract only the helpers under test. SQLite uses a temporary local database;
the cache tests use weak references, with no providers, network or trading loop.
"""
import ast
from contextlib import contextmanager
from copy import deepcopy
import gc
from pathlib import Path
import sqlite3
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
        for phase in ("calibration_r33_done", "calibration_r29_done", "book_start",
                      "book_done", "future_phase"):
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
                self.assertEqual(self.events[-1], ("paper_portfolio_phase", {
                    "phase": phase, "portfolio": "Currency", "detail": detail, "rss_mb": 410.0}))
                self.assertIs(self.events[-1][1]["detail"], detail)
                self.assert_warm_state()
        self.trace.clear()
        self.namespace["_v90_emit_portfolio"]("paper_order", qty=-391.98)
        self.assertEqual(self.trace, ["paper_order"])
        self.assertEqual(self.events[-1], ("paper_order", {"qty": -391.98, "rss_mb": 410.0}))

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


if __name__ == "__main__":
    unittest.main()
