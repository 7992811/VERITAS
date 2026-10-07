"""Operational timing must describe elapsed work, not counts or cached timings."""
import ast
import copy
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest


class CycleTelemetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(Path('veritas_intelligence.py').read_text())

    def test_streamed_fetch_time_excludes_analysis_between_assets(self):
        cycle = next(n for n in self.tree.body
                     if isinstance(n, ast.FunctionDef) and n.name == 'cycle')
        loop = next(n for n in cycle.body if isinstance(n, ast.For)
                    and ast.unparse(n.iter) == 'ASSETS.items()')
        fetch = next(n for n in ast.walk(loop) if isinstance(n, ast.If)
                     and isinstance(n.test, ast.Name)
                     and n.test.id == '_low_memory_streaming')
        keys = {'market_prefetch_wall', 'market_fetch_sum', 'market_parallel_saved_estimate'}
        program = []
        for node in cycle.body:
            if node is loop:
                reduced = copy.deepcopy(loop)
                reduced.body = [copy.deepcopy(fetch), ast.Expr(value=ast.Call(
                    func=ast.Name(id='analyse', ctx=ast.Load()), args=[], keywords=[]))]
                reduced.orelse = []
                program.append(reduced)
            elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name) and target.value.id == 'phase_seconds'
                and isinstance(target.slice, ast.Constant) and target.slice.value in keys
                for target in node.targets
            ):
                program.append(copy.deepcopy(node))
        clock = [0.0]
        def market(symbol, asset, product):
            clock[0] += {'A': 2.0, 'B': 3.0}[asset]
            return {'raw': {}, 'deriv': {}, 'elapsed_seconds': 700.0}
        def analyse():
            clock[0] += 100.0
        ns = {'ASSETS': {'a': ('A', None), 'b': ('B', None)},
              '_low_memory_streaming': True, '_fetch_asset_bundle': market,
              '_stream_fetch_started': 0.0, 'analyse': analyse,
              'time': SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0]),
              'phase_seconds': {}, 'prefetch_stats': {
                  'sum_asset_seconds': 0.0, 'wall_seconds': 0.0,
                  'parallel_wait_saved_estimate_seconds': 0.0}}
        exec(compile(ast.fix_missing_locations(ast.Module(body=program, type_ignores=[])),
                     '<real cycle fetch and metric statements>', 'exec'), ns)
        self.assertEqual(clock[0], 205.0)
        self.assertEqual(ns['prefetch_stats']['wall_seconds'], 5.0)
        self.assertEqual(ns['prefetch_stats']['sum_asset_seconds'], 5.0)
        self.assertEqual(ns['phase_seconds']['market_prefetch_wall'], 5.0)
        self.assertEqual(ns['phase_seconds']['market_fetch_sum'], 5.0)
        self.assertEqual(ns['bundle']['elapsed_seconds'], 3.0)

    def status(self, elapsed, phases=None):
        function = next(n for n in self.tree.body if isinstance(n, ast.FunctionDef)
                        and n.name == 'architecture_efficiency_status')
        ns = {'lock': threading.RLock(), 'cycle_telemetry_lock': threading.RLock(),
              'last_cycle': {'telemetry': {'elapsed_seconds': elapsed, 'phase_seconds': phases or {}}},
              'cycle_telemetry_history': [{'elapsed_seconds': elapsed}],
              'heavy_learning_snapshot': lambda: {}, 'FAST_LOOP_TARGET_SECONDS': 30.0,
              '_v90_background_maintenance': SimpleNamespace(snapshot=lambda: {})}
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<real status function>', 'exec'), ns)
        return ns['architecture_efficiency_status']()

    def test_one_slow_cycle_is_reported_as_delayed(self):
        self.assertEqual(self.status(90.0)['target_status'], 'DELAYED')
        self.assertEqual(self.status(20.0)['target_status'], 'ON_TARGET')

    def test_legacy_event_count_cannot_be_a_duration(self):
        report = self.status(90.0, {'portfolio_total': 12.0, 'decision_total': 8.0,
                                    'pg_batch_events': 5000})
        self.assertEqual(report['slowest_stage'], 'portfolio_total')
        self.assertEqual(report['slowest_stage_seconds'], 12.0)

    def test_parallel_aggregates_and_previous_background_work_are_not_cycle_stages(self):
        phases = {'market_prefetch_wall': 5.0, 'market_fetch_sum': 20.0,
                  'market_parallel_saved_estimate': 15.0, 'decision_total': 9.0,
                  'outcomes_background_last_seconds': 40.0}
        report = self.status(18.0, phases)
        self.assertEqual(report['slowest_stage'], 'decision_total')
        self.assertEqual(report['slowest_stage_seconds'], 9.0)
        self.assertEqual(report['phase_seconds'], phases)


if __name__ == '__main__':
    unittest.main()
