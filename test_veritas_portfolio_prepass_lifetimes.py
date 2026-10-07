"""Actual wrapper ASTs release completed SQL snapshots before nested execution.

No service import, database, or execution engine is started. Weak references
observe the payloads returned by the fixture reader; callbacks record only
scalar evidence, so the fixture does not itself retain the inspected rows.
"""
import ast
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import weakref


ROOT = Path(__file__).parent
VP, RT = 'veritas_portfolio.py', 'veritas_portfolio_runtime.py'
TREES = {name: ast.parse((ROOT/name).read_text()) for name in (VP, RT)}
CASES = {
    'PI': (VP, '_v90pi_base_step_one', {'positions', 'z0', 'z', 'payload'}),
    'CI': (VP, '_v90ci_base_step_one', {'positions', 'z0', 'z', 'payload'}),
    'R19': (VP, '_v90r19_base_step_one', {'_', 'positions', 'z0', 'z', 'management_book', 'mgmt'}),
    'R22': (VP, '_v90r22_base_step_one', {'p', 'pos', 'z0', 'z', 'mgmt'}),
    'R33': (VP, '_v90r33_base_step_one', {'p', 'pos'}),
    'R46': (RT, '_v90r46_base_step_one', {'p', 'pos'}),
    'R54': (RT, '_v90r54_base_step_one', {'p', 'pos', 'posmap', 'z'}),
    'R56': (RT, '_v90r56_base_step_one', {'p', 'pos', 'z0', 'z', 'zfresh'}),
}


def actual_node(layer):
    path, alias, _ = CASES[layer]
    matches = [node for node in TREES[path].body if isinstance(node, ast.FunctionDef)
               and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                       and call.func.id == alias for call in ast.walk(node))]
    if len(matches) != 1:
        raise AssertionError('Expected exactly one actual wrapper for '+alias)
    return matches[0]


def load_wrapper(layer, namespace, *, release=True):
    node = deepcopy(actual_node(layer))
    if not release:
        # Negative control: reproduce the original lifetime while retaining all
        # SQL, candidate transformations, management calls, and return values.
        names = CASES[layer][2]
        class OriginalLifetime(ast.NodeTransformer):
            removed = 0
            def visit_Assign(self, current):
                if (isinstance(current.value, ast.Constant) and current.value.value is None
                        and {n.id for n in current.targets if isinstance(n, ast.Name)} == names):
                    self.removed += 1
                    return None
                return current
        change = OriginalLifetime()
        node = change.visit(node)
        if change.removed != 1:
            raise AssertionError('Expected one dead-local release in '+layer)
    code = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    exec(compile(code, CASES[layer][0], 'exec'), namespace)
    return namespace[node.name]


class WatchedDict(dict):
    __slots__ = ('__weakref__',)


class FixtureCursor:
    def __init__(self, fixture):
        self.fixture = fixture

    def fetchall(self):
        return self.fixture.positions()

    def fetchone(self):
        rows = self.fixture.positions()
        return rows[0] if rows else None


class FixtureConnection:
    def __init__(self, fixture):
        self.fixture = fixture

    def execute(self, sql, params=None):
        self.fixture.sql.append((' '.join(sql.split()), deepcopy(params)))
        return FixtureCursor(self.fixture)


class PrepassFixture:
    def __init__(self, layer, *, empty=False, read_error=False, mode='AGGRESSIVE'):
        self.layer, self.empty, self.read_error = layer, empty, read_error
        self.refs, self.sql, self.events, self.logs = [], [], [], []
        self.connection = FixtureConnection(self)
        self.stop = 95.0
        self.policy = {'mode': mode}
        self.candidates = {'CNYRUBF': {'asset': 'CNYRUBF', 'horizon': '1h',
            'research_decision': 'SHORT' if layer == 'R19' else 'LONG', 'price': 101.0,
            'contract': {'secid': 'new-contract'}}}
        self.prices = {'CNYRUBF': 200.0 if layer == 'PI' else 101.0}
        self.summary = [dict(self.candidates['CNYRUBF'])]
        self.result = {'name': 'Aggressive', 'fixture_result': 'UNCHANGED'}
        self.base_calls, self.live_at_base = 0, None
        self.namespace = self.environment()

    def watched(self, **values):
        item = WatchedDict(values)
        self.refs.append(weakref.ref(item))
        return item

    def positions(self):
        if self.read_error:
            raise RuntimeError('fixture read unavailable')
        if self.empty:
            return []
        payload = self.watched(data_integrity_status='OK', mfe_pct=0,
                               original_evidence={'bars': [{'close': 100.0}]})
        return [self.watched(asset='CNYRUBF', direction='LONG', units=10.0,
            avg_entry_price=100.0, last_price=100.0, stop_price=self.stop,
            active_trade_id='unchanged-trade', payload=payload)]

    def portfolio_rows(self, c, name):
        self.events.append(('read_book', name))
        if self.read_error:
            raise RuntimeError('fixture read unavailable')
        return self.watched(name=name, initial_nav_rub=10000.0,
                            high_water_nav_rub=10000.0), self.positions()

    def note(self, label, *values):
        self.events.append((label, *values))

    def base(self, *args):
        self.base_calls += 1
        self.live_at_base = sum(ref() is not None for ref in self.refs)
        self.base_identity = (args[0] is self.connection, args[2] is self.policy,
                              args[9] is self.summary)
        self.base_values = deepcopy(args[1:])
        self.base_caps = dict(self.namespace['_v90r22_edge_caps'])
        return self.result

    def environment(self):
        def mark(p, pos, prices):
            self.note('mark', p['name'], sum(z['units'] for z in pos))
            return 10000.0, 0.0, 0.0, 0.0
        def management(summary, z):
            self.note('management', z['asset'], z['stop_price'])
            return {'asset': z['asset'], 'management': True}
        def tighten(c, name, z, row, px, ts):
            self.note('tighten', z['asset'], z['stop_price'], px)
            return {'asset': z['asset'], 'new_stop': 98.0}
        def migrate(c, name, z, row, px, nav, ts):
            self.note('migrate', z['asset'], z['stop_price'])
            self.stop = 97.0
        def backfill(c, name, z, row, px, ts):
            self.note('backfill', z['asset'], z['stop_price'])
        ns = {
            'COMMISSION': 0.0004, 'INITIAL_NAV_RUB': 10000.0, 'json': json,
            'print': lambda *args, **kwargs: self.logs.append(args),
            '_portfolio_rows': self.portfolio_rows, '_mark_nav': mark,
            '_v90j_json': lambda value: value if isinstance(value, dict) else {},
            '_v90j_iso': lambda value: value,
            '_v90pi_jump_limit': lambda asset: 0.04,
            'VTM': SimpleNamespace(owns_position=lambda z: True),
            '_v90ci_same_contract': lambda payload, row: False,
            '_v90ci_cross_source_disagreement': lambda row: None,
            '_v842_management_row': management,
            '_v90tr_apply': lambda c, name, book, prices, ts: self.note('trailing', name, tuple(book)),
            '_v90r19_prepare_candidate': lambda summary, row, mode: dict(row, prepared_mode=mode),
            '_v90r19_flip_confirmed': lambda summary, z, row, now: True,
            '_v90r22_edge_caps': {},
            '_v90r22_edge_decay_reduce': lambda c, p, name, z, mgmt, px, nav, ts, mode: 0.04,
            '_v90_execution_candidate_rank': lambda row: dict(row, _rank=7.0),
            '_v90r33_harvest': lambda c, p, name, prices, nav, ts: self.note('r33_harvest', name, nav),
            '_v90j_update_excursions': lambda c, name, prices, ts: self.note('excursions', name),
            '_v90r46_mark_trend_hold': lambda c, name, candidates, summary, ts: self.note('trend_hold', name),
            '_v90r46_giveback_harvest': lambda c, p, name, prices, nav, ts: self.note('r46_harvest', name, nav),
            '_v90r54_apply_structural_stop': lambda row: (dict(row, prepared_stop=98.0), {}),
            '_v90r54_dynamic_fraction': lambda row, current: (0.05, {'stage': 'REDUCE'}),
            '_v90r54_initial_fraction': lambda row: (0.10, {'stage': 'OPEN'}),
            '_v90r54_tighten_position_stop': tighten,
            '_v90r56_migrate_legacy_senior_position': migrate,
            '_v90r56_backfill_missing_tp': backfill,
        }
        ns[CASES[self.layer][1]] = self.base
        return ns

    def run(self, *, release=True):
        fn = load_wrapper(self.layer, self.namespace, release=release)
        return fn(self.connection, 'Aggressive', self.policy, self.candidates,
                  self.prices, 16.0, 80.0, '2026-10-07T12:10:00Z', 0.0004, self.summary)


class PortfolioPrepassLifetimeTests(unittest.TestCase):
    def assert_same_prepass(self, layer, *, require_retention=True, **options):
        old, new = PrepassFixture(layer, **options), PrepassFixture(layer, **options)
        self.assertIs(old.run(release=False), old.result)
        self.assertIs(new.run(), new.result)
        self.assertEqual((old.base_calls, new.base_calls), (1, 1))
        if require_retention:
            self.assertGreater(old.live_at_base, 0, 'negative control must actually retain SQL rows')
        self.assertEqual(new.live_at_base, 0, 'completed prepass still retains a decoded SQL graph')
        self.assertEqual(new.base_identity, (True, True, True))
        self.assertEqual(new.base_values, old.base_values)
        self.assertEqual(new.base_caps, old.base_caps)
        self.assertEqual(new.sql, old.sql)
        self.assertEqual(new.events, old.events)
        self.assertEqual(new.logs, old.logs)
        self.assertEqual(new.prices, old.prices)
        self.assertEqual(new.candidates, old.candidates)
        self.assertEqual(new.namespace['_v90r22_edge_caps'], old.namespace['_v90r22_edge_caps'])
        return new

    def test_price_integrity_release_preserves_frozen_price_and_candidate_rejection(self):
        state = self.assert_same_prepass('PI')
        self.assertEqual(state.base_values[2], {})
        self.assertEqual(state.base_values[3], {'CNYRUBF': 100.0})
        self.assertEqual(sum(q.startswith('UPDATE ') for q, _ in state.sql), 2)

    def test_contract_integrity_release_preserves_source_quarantine(self):
        state = self.assert_same_prepass('CI')
        self.assertEqual(state.base_values[2], {})
        self.assertEqual(state.base_values[3], {'CNYRUBF': 100.0})
        self.assertTrue(any('CONTRACT_IDENTITY_CHANGED' in str(params) for _, params in state.sql))

    def test_r19_release_preserves_trailing_and_confirmed_flip(self):
        state = self.assert_same_prepass('R19')
        self.assertTrue(state.base_values[2]['CNYRUBF']['_flip_confirmed'])
        self.assertIn(('trailing', 'Aggressive', ('CNYRUBF',)), state.events)

    def test_r22_release_keeps_edge_caps_until_base_returns_then_cleans_them(self):
        state = self.assert_same_prepass('R22')
        self.assertEqual(state.base_caps, {('Aggressive', 'CNYRUBF'): 0.04})
        self.assertEqual(state.namespace['_v90r22_edge_caps'], {})
        self.assertIsNone(state.namespace['_v90r22_active_policy'])

    def test_r33_release_preserves_harvest_and_candidate_rank(self):
        state = self.assert_same_prepass('R33')
        self.assertEqual(state.base_values[2]['CNYRUBF']['_rank'], 7.0)
        self.assertIn(('r33_harvest', 'Aggressive', 10000.0), state.events)

    def test_r46_release_preserves_prepass_order_and_harvest(self):
        state = self.assert_same_prepass('R46')
        self.assertEqual([x[0] for x in state.events],
                         ['excursions', 'trend_hold', 'read_book', 'mark', 'r46_harvest'])

    def test_r54_release_preserves_stop_and_dynamic_reduction_patch(self):
        state = self.assert_same_prepass('R54')
        row = state.base_values[2]['CNYRUBF']
        self.assertEqual(row['_r54_target_fraction'], 0.05)
        self.assertEqual(row['prepared_stop'], 98.0)
        self.assertEqual(json.loads(state.sql[0][1][0])['r54_dynamic_reduction_target'], 0.05)

    def test_r56_release_keeps_reread_after_stop_migration(self):
        state = self.assert_same_prepass('R56')
        self.assertIn(('migrate', 'CNYRUBF', 95.0), state.events)
        self.assertIn(('backfill', 'CNYRUBF', 97.0), state.events)
        self.assertEqual(len(state.sql), 1)
        self.assertIn('SELECT * FROM paper_positions', state.sql[0][0])

    def test_empty_books_and_disabled_aggressive_prepasses_remain_valid(self):
        for layer in CASES:
            with self.subTest(layer=layer, empty=True):
                self.assert_same_prepass(layer, empty=True, require_retention=False)
        for layer in ('R54', 'R56'):
            with self.subTest(layer=layer, mode='CURRENCY'):
                state = self.assert_same_prepass(layer, mode='CURRENCY', require_retention=False)
                self.assertEqual(state.events, [])
                self.assertEqual(state.sql, [])

    def test_existing_caught_read_errors_still_delegate_with_unchanged_fallbacks(self):
        for layer in ('PI', 'CI', 'R19', 'R46', 'R54', 'R56'):
            with self.subTest(layer=layer):
                self.assert_same_prepass(layer, read_error=True, require_retention=False)

    def test_inspected_wrappers_are_reachable_from_current_final_step_one(self):
        def bindings(tree, env, path):
            for node in tree.body:
                if isinstance(node, ast.FunctionDef):
                    env[node.name] = (path, node)
                elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Name) and node.value.id in env:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            env[target.id] = env[node.value.id]
            return env
        vp = bindings(TREES[VP], {}, VP)
        rt = bindings(TREES[RT], dict(vp), RT)
        # Runtime exports its captured compatibility aliases back to VP.
        vp.update({k: v for k, v in rt.items() if k.startswith(('_v90', '_r'))})
        seen = set()
        def walk(value):
            path, node = value
            key = (path, node.lineno)
            if key in seen:
                return
            seen.add(key)
            env = vp if path == VP else rt
            for call in ast.walk(node):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                        and 'step_one' in call.func.id and call.func.id in env):
                    walk(env[call.func.id])
        walk(rt['FINAL_STEP_ONE'])
        for layer in CASES:
            with self.subTest(layer=layer):
                self.assertIn((CASES[layer][0], actual_node(layer).lineno), seen)


class CalibrationBoundaryTests(unittest.TestCase):
    def node(self, layer):
        alias = f'_v90{layer}_base_step_all'
        matches = [node for node in TREES[VP].body if isinstance(node, ast.FunctionDef)
                   and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                           and call.func.id == alias for call in ast.walk(node))]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def exercise(self, layer, *, emit_enabled=True, refresh_error=None, base_error=None):
        events, calls, refs, logs = [], [], [], []
        result, connection = object(), object()
        summary = [{'asset': 'CNYRUBF', 'decision': 'NO_TRADE'}]
        cache = {'profiles': {'fixture': [1, 2, 3]}}
        def refresh(pg_connect):
            self.assertIs(pg_connect, connection)
            events.append('refresh')
            if refresh_error is not None:
                raise refresh_error
            snapshot = WatchedDict(rows=[{'payload': {'proof': 'fixture'}}])
            refs.append(weakref.ref(snapshot))
            return snapshot
        def emit(event, **fields):
            self.assertTrue(all(ref() is None for ref in refs), 'refresh return must not survive to boundary')
            events.append('emit')
            calls.append((event, fields))
        callback = emit if emit_enabled else None
        arguments = (summary, connection, 'fixture-model', '2026-10-07T12:50:00Z', 0.0004, callback)
        def base(*args):
            self.assertEqual(args, arguments)
            self.assertTrue(all(a is b for a, b in zip(args, arguments)))
            self.assertTrue(all(ref() is None for ref in refs))
            events.append('base')
            if base_error is not None:
                raise base_error
            return result
        namespace = {'COMMISSION': 0.0004, 'json': json,
                     'print': lambda *args, **kwargs: logs.append(args),
                     f'_v90{layer}_refresh': refresh, f'_v90{layer}_base_step_all': base,
                     f'_v90{layer}_cache': cache}
        module = ast.fix_missing_locations(ast.Module(body=[deepcopy(self.node(layer))], type_ignores=[]))
        exec(compile(module, VP, 'exec'), namespace)
        propagated = base_error or (refresh_error if not isinstance(refresh_error, Exception) else None)
        if propagated is not None:
            with self.assertRaises(type(propagated)) as caught:
                namespace['step_all'](*arguments)
            self.assertIs(caught.exception, propagated)
        else:
            self.assertIs(namespace['step_all'](*arguments), result)
        self.assertIs(namespace[f'_v90{layer}_cache'], cache)
        self.assertEqual(cache, {'profiles': {'fixture': [1, 2, 3]}})
        self.assertEqual(summary, [{'asset': 'CNYRUBF', 'decision': 'NO_TRADE'}])
        return events, calls, logs

    def test_refresh_boundaries_are_scalar_and_precede_base(self):
        for layer in ('r33', 'r29'):
            with self.subTest(layer=layer):
                events, calls, logs = self.exercise(layer)
                self.assertEqual(events, ['refresh', 'emit', 'base'])
                self.assertEqual(calls, [('paper_portfolio_phase', {'phase': f'calibration_{layer}_done'})])
                self.assertEqual(logs, [])

    def test_caught_refresh_failures_keep_existing_logs_and_still_reach_boundary(self):
        for layer in ('r33', 'r29'):
            with self.subTest(layer=layer):
                events, calls, logs = self.exercise(layer, refresh_error=RuntimeError('fixture refresh failed'))
                self.assertEqual(events, ['refresh', 'emit', 'base'])
                self.assertEqual(calls, [('paper_portfolio_phase', {'phase': f'calibration_{layer}_done'})])
                if layer == 'r29':
                    self.assertEqual(json.loads(logs[0][0]),
                                     {'event': 'V90_R29_REFRESH_ERROR', 'error': 'fixture refresh failed'})
                else:
                    self.assertEqual(logs, [])

    def test_optional_emitter_does_not_change_success_or_caught_failure(self):
        for layer in ('r33', 'r29'):
            for failure in (None, RuntimeError('fixture refresh failed')):
                with self.subTest(layer=layer, failure=type(failure).__name__):
                    events, calls, _ = self.exercise(layer, emit_enabled=False, refresh_error=failure)
                    self.assertEqual(events, ['refresh', 'base'])
                    self.assertEqual(calls, [])

    def test_uncaught_refresh_and_base_exceptions_keep_their_identity(self):
        for layer in ('r33', 'r29'):
            with self.subTest(layer=layer):
                events, calls, _ = self.exercise(layer, refresh_error=KeyboardInterrupt('fixture interruption'))
                self.assertEqual(events, ['refresh'])
                self.assertEqual(calls, [])
                events, calls, _ = self.exercise(layer, base_error=RuntimeError('fixture base failed'))
                self.assertEqual(events, ['refresh', 'emit', 'base'])
                self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
