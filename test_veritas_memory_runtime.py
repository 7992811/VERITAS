"""Resource-lifetime regressions without importing or starting the runtime.

Extract only the helpers under test. SQLite uses a temporary local database;
the cache tests use weak references, with no providers, network or trading loop.
"""
import ast
from contextlib import contextmanager
import gc
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
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


if __name__ == "__main__":
    unittest.main()
