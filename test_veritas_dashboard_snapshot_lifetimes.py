"""Run extracted production AST without importing or starting the application."""
import ast
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
import weakref


TREE = ast.parse(Path(__file__).with_name('veritas_intelligence.py').read_text())


def function(name):
    return deepcopy(next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == name))


def compile_function(node, namespace):
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    exec(compile(module, '<actual-dashboard-ast>', 'exec'), namespace)
    return namespace[node.name]


class TrackedSnapshot(dict):
    pass


class BootstrapLifetimeTests(unittest.TestCase):
    def test_bootstrap_envelope_released_while_main_frame_and_api_cache_stay_alive(self):
        main = function('main')
        block = next(n for n in ast.walk(main) if isinstance(n, ast.Try)
                     and any(isinstance(child, ast.Assign)
                             and any(isinstance(t, ast.Name) and t.id == '_boot_ui' for t in child.targets)
                             for child in n.body))
        # Keep the enclosing frame alive after the real selftest, like main's
        # server_thread.join(), without starting any application services.
        main.body = [block, ast.parse('yield None').body[0]]
        for failure in (None, 'bootstrap', 'emit'):
            with self.subTest(failure=failure):
                references = {}; events = []
                cache = {'value': {'portfolios': [{'name': 'Currency', 'positions': [], 'nav_rub': 10000.}]}}
                original_cache = cache['value']
                def bootstrap():
                    signal = TrackedSnapshot(history={'proof': 'startup-only'})
                    snapshot = TrackedSnapshot(status='OK', signals=[signal], signal_count=1,
                        portfolios=cache['value']['portfolios'], portfolio_count=1,
                        positions=[], open_position_count=0, trades=[], trade_count=0)
                    references.update(envelope=weakref.ref(snapshot), signal=weakref.ref(signal))
                    if failure == 'bootstrap':
                        raise RuntimeError('bootstrap fixture failure')
                    return snapshot
                def emit(event, **fields):
                    events.append((event, fields))
                    if failure == 'emit' and fields.get('status') == 'OK':
                        raise RuntimeError('selftest log fixture failure')
                run = compile_function(main, {'_v90r26_dashboard_bootstrap': bootstrap, 'emit': emit})
                frame = run()
                try:
                    next(frame)
                    self.assertIsNotNone(frame.gi_frame)
                    self.assertIsNone(references['envelope'](), 'main still retains the startup response')
                    self.assertIsNone(references['signal'](), 'startup-only graph remains reachable')
                    self.assertIs(cache['value'], original_cache)
                    self.assertEqual(cache['value']['portfolios'][0]['nav_rub'], 10000.)
                    self.assertEqual(events[-1][0], 'v90_dashboard_bootstrap_selftest')
                    self.assertEqual(events[-1][1]['status'], 'OK' if failure is None else 'ERROR')
                finally:
                    frame.close()


class WindowDB:
    def __init__(self, early, recent, include_payload):
        self.rows = {'ASC': early, 'DESC': recent}
        self.include_payload = include_payload
        self.queries = []

    @contextmanager
    def connect(self):
        yield self

    def execute(self, sql, args):
        sql = ' '.join(sql.split())
        self.queries.append((sql, args))
        order = 'ASC' if 'ORDER BY closed_at ASC' in sql else 'DESC'
        rows = [dict(asset='CNYRUBF', horizon='1h', direction='LONG', status='CLOSED',
                     total_pnl_fraction=value, closed_at=str(i), payload={'unused': ['proof', i]})
                for i, value in enumerate(self.rows[order])]
        if not self.include_payload:
            rows = [{key: value for key, value in row.items() if key != 'payload'} for row in rows]
        return SimpleNamespace(fetchall=lambda: rows)


class ShadowWindowProjectionTests(unittest.TestCase):
    def compare(self, early, recent):
        outputs = []
        for include_payload in (True, False):
            db = WindowDB(early, recent, include_payload)
            read = compile_function(function('_shadow_trade_learning_windows'), {
                'pg_enabled': lambda: True, 'pg_connect': db.connect,
                'LEARNING_PROGRESS_WINDOW': 17, 'LEARNING_INDEX_TRADE_MIN_N': 3})
            outputs.append(read())
            self.assertEqual(db.queries, [("SELECT asset,horizon,direction,status,total_pnl_fraction,closed_at "
                "FROM shadow_trades WHERE status<>'ACTIVE' AND total_pnl_fraction IS NOT NULL "
                f"ORDER BY closed_at {order} LIMIT %s", (17,)) for order in ('ASC', 'DESC')])
        self.assertEqual(outputs[0], outputs[1], 'Removing unused payload changed learning metrics')
        return outputs[1]

    def test_full_and_projected_rows_preserve_both_learning_windows(self):
        result = self.compare(['0.1', '-0.02', '0'], ['-0.04', '0.08', '0.12'])
        self.assertEqual(result['status'], 'MEASURABLE')
        self.assertEqual(result['baseline']['n'], 3)
        self.assertEqual(result['current']['n'], 3)
        self.assertAlmostEqual(result['baseline']['positive_rate'], 1/3)
        self.assertAlmostEqual(result['current']['positive_rate'], 2/3)
        self.assertAlmostEqual(result['baseline']['avg_pnl'], .08/3)
        self.assertAlmostEqual(result['current']['avg_pnl'], .16/3)

    def test_empty_and_small_windows_remain_building_without_zero_evidence(self):
        result = self.compare([], ['-0.2'])
        self.assertEqual(result['status'], 'BUILDING')
        self.assertEqual(result['baseline'], {'n': 0, 'positive_rate': None, 'avg_pnl': None})
        self.assertEqual(result['current'], {'n': 1, 'positive_rate': 0., 'avg_pnl': -.2})


if __name__ == '__main__':
    unittest.main()
