"""R19/R56 omit only reads whose consumers cannot act.

The reference is the actual captured wrapper with only its new gate removed.
Native PostgreSQL checks all five financial tables, real trailing/migration/TP
writes and outer rollback. No production database or provider is contacted.
"""
import ast
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import inspect
import io
import json
import os
from pathlib import Path
import textwrap
import unittest
from unittest.mock import patch

import veritas_portfolio as VP
import veritas_portfolio_runtime as RT
import veritas_position_thesis as PT
import veritas_price_source as PS
import test_veritas_guard_observation_batch_sql as SQLH


DSN = os.getenv('VERITAS_QUALITY_TEST_DSN', '')
ALIASES = {'R19': '_v90r19_base_step_one', 'R56': '_v90r56_base_step_one'}
SQL_NULL = SQLH.SQL_NULL


def captured_wrappers():
    found, seen, pending = {}, set(), [RT.FINAL_STEP_ONE]
    while pending:
        fn = pending.pop()
        if fn in seen:
            continue
        seen.add(fn)
        for kind, alias in ALIASES.items():
            if alias in fn.__code__.co_names:
                if kind in found and found[kind] is not fn:
                    raise AssertionError('Ambiguous captured wrapper '+kind)
                found[kind] = fn
        for name in fn.__code__.co_names:
            target = fn.__globals__.get(name)
            if 'step_one' in name and inspect.isfunction(target):
                pending.append(target)
    if set(found) != set(ALIASES):
        raise AssertionError('Current final runtime lost an inspected wrapper')
    return found


def reference_wrapper(kind):
    fn = captured_wrappers()[kind]
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    class RemoveOnlyReadGate(ast.NodeTransformer):
        changes = 0
        def visit_IfExp(self, node):
            if kind == 'R19' and ast.dump(node.test) == ast.dump(ast.Name(id='book', ctx=ast.Load())):
                if not (isinstance(node.body, ast.Call)
                        and isinstance(node.body.func, ast.Name)
                        and node.body.func.id == '_portfolio_rows'):
                    raise AssertionError('R19 reference no longer matches its read')
                self.changes += 1
                return node.body
            return self.generic_visit(node)
        def visit_If(self, node):
            if (kind == 'R56' and isinstance(node.test, ast.Call)
                    and ast.unparse(node.test) == 'VTM.owns_position(z)'
                    and len(node.body) == 1 and isinstance(node.body[0], ast.Continue)):
                self.changes += 1
                return None
            return self.generic_visit(node)
    transform = RemoveOnlyReadGate()
    tree = ast.fix_missing_locations(transform.visit(tree))
    if transform.changes != 1:
        raise AssertionError('Reference must undo exactly one read gate: '+kind)
    namespace = dict(fn.__globals__)
    exec(compile(tree, '<pre-read-elision-'+kind+'>', 'exec'), namespace)
    return namespace[fn.__name__]


def invoke(kind, c, rows, candidates, summary, clock, *, legacy=False):
    """Keep production helpers; replace only the next wrapper with a boundary."""
    fn = reference_wrapper(kind) if legacy else captured_wrappers()[kind]
    policy = {'mode': 'AGGRESSIVE', 'nested_policy': {'immutable_limit': .015}}
    prices = {z['asset']: z['last_price'] for z in rows}
    candidates, summary = deepcopy(candidates), deepcopy(summary)
    inputs = deepcopy((policy, prices, candidates, summary))
    forwarded, helper_calls = [], {}
    def next_step(conn, name, pol, book, marks, ruonia, usdrub, ts, commission, context):
        forwarded.append(deepcopy((name, pol, book, marks, ruonia, usdrub, ts, commission, context)))
        if not (conn is c and pol is policy and marks is prices and context is summary and ts is clock):
            raise AssertionError('Wrapper changed the caller clock/context identity')
        return 'delegated'
    replacements = {ALIASES[kind]: next_step}
    names = (('_v90tr_apply', '_v90r19_flip_confirmed') if kind == 'R19' else
             ('_v90r56_migrate_legacy_senior_position', '_v90r56_backfill_missing_tp'))
    for name in names:
        original = fn.__globals__[name]
        def record(*args, _name=name, _original=original, **kwargs):
            # The connection is not copied; all financial/source/clock arguments are.
            start = 1 if _name != '_v90r19_flip_confirmed' else 0
            helper_calls.setdefault(_name, []).append(deepcopy((args[start:], kwargs)))
            return _original(*args, **kwargs)
        replacements[name] = record
    output = io.StringIO()
    with patch.dict(fn.__globals__, replacements), redirect_stdout(output):
        result = fn(c, 'Aggressive', policy, candidates, prices,
                    16., 80., clock, .0004, summary)
    if (policy, prices, candidates, summary) != inputs:
        raise AssertionError('Wrapper mutated original policy/marks/candidates/context')
    return result, forwarded, output.getvalue(), helper_calls


class RowsConnection:
    """Local boundary test only; native tests below execute actual PostgreSQL."""
    def __init__(self, rows):
        self.rows, self.statements = deepcopy(rows), []
    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if sql.startswith('SELECT * FROM paper_portfolios'):
            rows = [dict(initial_nav_rub=1000000., realized_pnl_rub=0., fees_rub=1., funding_rub=0.)]
        elif sql.startswith('SELECT * FROM paper_positions'):
            rows = [z for z in self.rows if len(params) < 2 or z['asset'] == params[1]]
        else:
            raise AssertionError('Unexpected local I/O: '+sql)
        class Cursor:
            def fetchall(self): return deepcopy(rows)
            def fetchone(self): return deepcopy(rows[0]) if rows else None
        return Cursor()


class BookReadElisionBindingTests(unittest.TestCase):
    def test_actual_captured_wrappers_and_single_gate_references(self):
        wrappers = captured_wrappers()
        self.assertEqual(Path(wrappers['R19'].__code__.co_filename).name, 'veritas_portfolio.py')
        self.assertEqual(Path(wrappers['R56'].__code__.co_filename).name, 'veritas_portfolio_runtime.py')
        self.assertIs(wrappers['R56'].__globals__['_v90r56_migrate_legacy_senior_position'],
                      RT._v90r56_migrate_legacy_senior_position)
        self.assertIs(wrappers['R56'].__globals__['_v90r56_backfill_missing_tp'],
                      RT._v90r56_backfill_missing_tp)
        self.assertIs(wrappers['R19'].__globals__['_v90tr_apply'], RT._v90tr_apply)
        for kind in ALIASES:
            self.assertTrue(callable(reference_wrapper(kind)))

    def test_empty_r19_still_delegates_same_arguments_with_one_fewer_book_read(self):
        clock = datetime(2026, 10, 7, tzinfo=timezone.utc)
        old, new = RowsConnection([]), RowsConnection([])
        self.assertEqual(invoke('R19', old, [], {}, [], clock, legacy=True),
                         invoke('R19', new, [], {}, [], clock))
        self.assertEqual(len(old.statements), 4)
        self.assertEqual(len(new.statements), 2)

    def test_owned_r56_skips_only_fresh_read_after_real_migrate(self):
        clock = datetime(2026, 10, 7, tzinfo=timezone.utc)
        rows = [dict(asset='ETH', direction='LONG', units=1., avg_entry_price=100.,
                     last_price=100., stop_price=95., active_trade_id='owned',
                     payload={'structural_policy_version': 'v', 'frozen_source': {'id': 'original'}})]
        candidates = {'ETH': {'asset': 'ETH', 'horizon': '4h'}}
        old, new = RowsConnection(rows), RowsConnection(rows)
        a = invoke('R56', old, rows, candidates, [], clock, legacy=True)
        b = invoke('R56', new, rows, candidates, [], clock)
        self.assertEqual(a[:3], b[:3])
        self.assertEqual(a[3]['_v90r56_migrate_legacy_senior_position'],
                         b[3]['_v90r56_migrate_legacy_senior_position'])
        self.assertEqual(len(old.statements), 3)
        self.assertEqual(len(new.statements), 2)
        self.assertNotIn('_v90r56_backfill_missing_tp', b[3])

    def test_owned_r56_still_converts_malformed_row_before_ownership_gate(self):
        clock = datetime(2026, 10, 7, tzinfo=timezone.utc)
        rows = [dict(asset='ETH', direction='LONG', units=1., avg_entry_price=100.,
                     last_price=100., payload={'structural_policy_version': True})]
        old, new = RowsConnection(rows), RowsConnection(rows)
        a = invoke('R56', old, rows, {'ETH': 7}, [], clock, legacy=True)
        b = invoke('R56', new, rows, {'ETH': 7}, [], clock)
        self.assertEqual(a, b)
        self.assertIn('R56_MIGRATION_ERROR', a[2])
        self.assertIn('TypeError', a[2])
        self.assertEqual(old.statements, new.statements)

    def test_legacy_r56_keeps_fresh_read_even_when_migration_returns_none(self):
        clock = datetime(2026, 10, 7, tzinfo=timezone.utc)
        rows = [dict(asset='ETH', direction='LONG', units=1., avg_entry_price=100.,
                     last_price=100., stop_price=95., active_trade_id='legacy',
                     payload={'r56_trade_frame_migrated': True, 'r56_tp1_price': 102.})]
        old, new = RowsConnection(rows), RowsConnection(rows)
        candidates = {'ETH': {'asset': 'ETH', 'horizon': '4h'}}
        self.assertEqual(invoke('R56', old, rows, candidates, [], clock, legacy=True),
                         invoke('R56', new, rows, candidates, [], clock))
        self.assertEqual(old.statements, new.statements)
        self.assertEqual(len(new.statements), 3)

    def test_nonempty_r19_keeps_second_read_for_existing_positions(self):
        clock = datetime(2026, 10, 7, tzinfo=timezone.utc)
        rows = [dict(asset='ETH', direction='LONG', last_price=100., payload={})]
        old, new = RowsConnection(rows), RowsConnection(rows)
        candidates = {'ETH': {'asset': 'ETH', 'research_decision': 'LONG'}}
        self.assertEqual(invoke('R19', old, rows, candidates, [], clock, legacy=True),
                         invoke('R19', new, rows, candidates, [], clock))
        self.assertEqual(old.statements, new.statements)
        self.assertEqual(len(new.statements), 4)


class SQLTrace:
    def __init__(self, connection):
        self.connection, self.reads, self.writes = connection, [], []
    def __getattr__(self, name):
        return getattr(self.connection, name)
    def execute(self, sql, params=()):
        normalized = ' '.join(sql.split())
        if normalized.startswith(('UPDATE ', 'INSERT ', 'DELETE ')):
            self.writes.append((normalized, deepcopy(params)))
        cursor = self.connection.execute(sql, params)
        if not normalized.startswith('SELECT * FROM paper_positions'):
            return cursor
        owner = self
        class Cursor:
            def record(self, rows):
                owner.reads.append((normalized, tuple(params),
                    [z['active_trade_id'] for z in rows], len(json.dumps(rows, default=str).encode())))
            def fetchall(self):
                rows = cursor.fetchall(); self.record(rows); return rows
            def fetchone(self):
                row = cursor.fetchone(); self.record([row] if row else []); return row
        return Cursor()


@unittest.skipUnless(DSN, 'isolated PostgreSQL test database not configured')
class BookReadElisionSQLTests(unittest.TestCase):
    # Reuse only the established isolated schema/ledger fixture, not its tests.
    verify_database = staticmethod(SQLH.GuardObservationBatchSQLTests.verify_database)
    connect = SQLH.GuardObservationBatchSQLTests.connect
    drop_schema = SQLH.GuardObservationBatchSQLTests.drop_schema
    insert = SQLH.GuardObservationBatchSQLTests.insert
    seed = SQLH.GuardObservationBatchSQLTests.seed
    snapshot = SQLH.GuardObservationBatchSQLTests.snapshot

    def setUp(self):
        SQLH.GuardObservationBatchSQLTests.setUp(self)
        owner = self
        class ReplayClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return owner.clock if tz else owner.clock.replace(tzinfo=None)
        self.enterContext(patch.object(PT, 'datetime', ReplayClock))

    def payload(self, **updates):
        value = {'execution_horizon': '1d', 'mfe_pct': 4., 'data_integrity_status': 'OK',
                 'entry_event_snapshot': {'event_id': 'original-held-event',
                                          'sealed_evidence': self.proof},
                 'source_locked_mark': {'price': 100., 'observed_at': 'original-clock'},
                 'trailing_history': [{'rule': 'original-confirmed-swing'}],
                 'frozen_entry': {'levels': [None, 0, True, {'atr': 1.25}]}}
        value.update(updates)
        return value

    def rows(self, payloads, direction='LONG'):
        rows = []
        for i, payload in enumerate(payloads):
            asset = ('ETH', 'BTC', 'CNYRUBF')[i] if i < 3 else 'OWNED_'+str(i)
            if isinstance(payload, dict):
                payload = deepcopy(payload)
                payload['price_source_lock'] = PS.identity(asset, {'source': 'Binance spot'})
            rows.append(dict(portfolio_name='Aggressive', asset=asset, direction=direction,
                units=1000., avg_entry_price=100., last_price=104. if direction == 'LONG' else 96.,
                stop_price=95. if direction == 'LONG' else 105., target_fraction=.1,
                opened_at=self.clock-timedelta(days=3), updated_at=self.clock-timedelta(days=3),
                active_trade_id='trade-'+str(i), payload=payload))
        return rows

    def summary(self, rows, *, opposite=False, horizon='4h'):
        return [dict(asset=z['asset'], horizon=horizon, source='Binance spot',
            research_decision=('SHORT' if z['direction'] == 'LONG' else 'LONG') if opposite else z['direction'],
            source_gate_pass=True, market_open=True, price=z['last_price'],
            market_observed_at=self.clock.isoformat(), realized_vol=.01,
            trade_plan={'multi_tf_levels': {'timeframes': {'1h': {
                'recent_support': 101., 'recent_resistance': 99.}}}}) for z in rows]

    def run_case(self, kind, rows, candidates, summary, *, legacy=False, rollback=False):
        self.seed(rows)
        before = self.snapshot()
        with self.connect() as c:
            trace = SQLTrace(c)
            with c.transaction(force_rollback=rollback):
                result = invoke(kind, trace, rows, candidates, summary, self.clock, legacy=legacy)
                inside = self.snapshot(c)
        return before, self.snapshot(), result, trace, inside

    def assert_parity(self, kind, rows, candidates, summary, *, rollback=False):
        old = self.run_case(kind, rows, candidates, summary, legacy=True, rollback=rollback)
        new = self.run_case(kind, rows, candidates, summary, rollback=rollback)
        self.assertEqual(old[:2], new[:2])
        self.assertEqual(old[2][:3], new[2][:3])
        self.assertEqual(old[3].writes, new[3].writes)
        self.assertEqual(old[4], new[4])
        for table in ('paper_portfolios', 'paper_orders', 'paper_nav_history'):
            self.assertEqual(new[0][table], new[1][table])
        for before, after in zip(new[0]['paper_trades'], new[1]['paper_trades']):
            self.assertEqual({k: v for k, v in before.items() if k != 'payload'},
                             {k: v for k, v in after.items() if k != 'payload'})
        if kind == 'R56':
            self.assertEqual(old[2][3].get('_v90r56_migrate_legacy_senior_position'),
                             new[2][3].get('_v90r56_migrate_legacy_senior_position'))
        return old, new

    def test_r19_empty_candidates_keep_actual_trailing_and_full_ledger(self):
        for direction in ('LONG', 'SHORT'):
            with self.subTest(direction=direction):
                rows = self.rows([self.payload(execution_horizon='1h')], direction)
                old, new = self.assert_parity('R19', rows, {}, self.summary(rows, horizon='1h'))
                self.assertEqual(len(old[3].reads), len(new[3].reads)+1)
                self.assertTrue(new[3].writes, 'fixture must execute real structural trailing')
                self.assertNotEqual(new[0]['paper_positions'][0]['stop_price'],
                                    new[1]['paper_positions'][0]['stop_price'])
                self.assertEqual(old[2][3], new[2][3])

    def test_r19_nonempty_flip_reads_real_post_trailing_stop_and_source(self):
        for direction in ('LONG', 'SHORT'):
            with self.subTest(direction=direction):
                rows = self.rows([self.payload(execution_horizon='1h')], direction)
                summary = self.summary(rows, opposite=True, horizon='1h')
                candidates = {summary[0]['asset']: summary[0]}
                old, new = self.assert_parity('R19', rows, candidates, summary)
                self.assertEqual(old[3].reads, new[3].reads)
                self.assertTrue(new[3].writes)
                self.assertEqual(old[2][3], new[2][3])
                ((args, kwargs),) = new[2][3]['_v90r19_flip_confirmed']
                observed = args[1]
                self.assertNotEqual(observed['stop_price'], rows[0]['stop_price'])
                self.assertEqual(observed, new[1]['paper_positions'][0])
                self.assertEqual(observed['payload']['entry_event_snapshot'],
                                 rows[0]['payload']['entry_event_snapshot'])
                self.assertEqual(kwargs['now'], self.clock)
                self.assertIn('_flip_confirmed', new[2][1][0][2]['ETH'])

    def test_r56_owned_book_removes_each_single_row_reread(self):
        for direction in ('LONG', 'SHORT'):
            with self.subTest(direction=direction):
                rows = self.rows([self.payload(structural_policy_version='v') for _ in range(23)], direction)
                summary = self.summary(rows)
                book = {r['asset']: r for r in summary}
                old, new = self.assert_parity('R56', rows, book, summary)
                self.assertEqual(len(old[3].reads), 24)
                self.assertEqual(len(new[3].reads), 1)
                self.assertEqual(new[0], new[1])
                self.assertFalse(new[3].writes)
                self.assertNotIn('_v90r56_backfill_missing_tp', new[2][3])

    def test_r56_mixed_book_keeps_migration_fresh_stop_and_missing_tp_backfill(self):
        for direction in ('LONG', 'SHORT'):
            for reverse in (False, True):
                with self.subTest(direction=direction, reverse=reverse):
                    payloads = [self.payload(structural_policy_version='v'), self.payload(),
                        self.payload(execution_horizon='4h', r56_trade_frame_migrated=True,
                                     r56_tp1_price=None, r56_management_horizon='4h'),
                        self.payload(execution_horizon='4h', r56_trade_frame_migrated=True,
                                     r56_tp1_price=106. if direction == 'LONG' else 94.)]
                    if reverse:
                        payloads.reverse()
                    rows = self.rows(payloads, direction)
                    summary = self.summary(rows)
                    old, new = self.assert_parity('R56', rows, {}, summary)
                    self.assertEqual(len(old[3].reads), len(new[3].reads)+1)
                    self.assertTrue(new[3].writes)
                    after = {z['asset']: z for z in new[1]['paper_positions']}
                    for (args, kwargs) in new[2][3]['_v90r56_backfill_missing_tp']:
                        position = args[1]
                        self.assertEqual(position['stop_price'], after[position['asset']]['stop_price'])
                        self.assertEqual(args[-1], self.clock)
                        self.assertTrue(after[position['asset']]['payload']['r56_tp1_price'])
                    for position in new[1]['paper_positions']:
                        before = next(z for z in rows if z['asset'] == position['asset'])
                        self.assertEqual(position['payload']['price_source_lock'], before['payload']['price_source_lock'])
                        self.assertEqual(position['payload']['entry_event_snapshot'], before['payload']['entry_event_snapshot'])

    def test_r56_all_malformed_ownership_shapes_keep_existing_actions_and_errors(self):
        tags = ['', ' ', 'unknown-policy', None, False, True, 0, 1, -1, 1.5, [], ['v'], {}, {'v': 1}]
        cases = [('tag_'+str(i), self.payload(structural_policy_version=tag)) for i, tag in enumerate(tags)]
        cases += [('missing', self.payload()), ('sql_null', SQL_NULL), ('json_null', None),
                  ('empty_object', {}), ('array', []), ('pair_array', [['structural_policy_version', 'v']]),
                  ('object_array', [{'structural_policy_version': 'v'}]), ('number', 1), ('bool', True),
                  ('invalid_json_string', 'not-json'), ('empty_json_string', ''),
                  ('encoded_owned', json.dumps({'structural_policy_version': 'v'})),
                  ('encoded_legacy', json.dumps({'execution_horizon': '1d'})),
                  ('encoded_array', '[]'), ('encoded_number', '1'), ('encoded_null', 'null')]
        for label, payload in cases:
            with self.subTest(shape=label):
                rows = self.rows([payload])
                summary = self.summary(rows)
                self.assert_parity('R56', rows, {r['asset']: r for r in summary}, summary)

    def test_outer_rollback_preserves_all_five_tables_after_real_metadata_writes(self):
        for kind in ('R19', 'R56'):
            with self.subTest(kind=kind):
                payload = self.payload(execution_horizon='1h' if kind == 'R19' else '1d')
                rows = self.rows([payload, self.payload(structural_policy_version='v')])
                summary = self.summary(rows, horizon='1h' if kind == 'R19' else '4h')
                old, new = self.assert_parity(kind, rows, {}, summary, rollback=True)
                self.assertTrue(new[3].writes)
                self.assertNotEqual(new[0], new[4])
                self.assertEqual(new[0], new[1])


if __name__ == '__main__':
    unittest.main()
