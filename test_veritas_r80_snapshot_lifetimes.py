"""R80 prepass evidence must be released before the nested portfolio core runs.

Execute the actual AST functions without importing/starting the service. A DB
factory makes fresh full payloads on every SELECT and holds only weakrefs. The
legacy comparison removes only the two lifetime assignments, preserving the
same source/trailing/thesis checks and delegated arguments.
"""
import ast
from copy import deepcopy
import gc
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

import veritas_price_source as VPS
import veritas_position_guard as VPG
import veritas_thesis_guard as VTG
import veritas_costs as VC


RUNTIME = Path(__file__).with_name('veritas_portfolio_runtime.py')
CLOCK = '2026-10-07T12:07:00+00:00'
UID = 'c300543d-aa18-4249-b110-615409dde036'


def load_r80(name, namespace, *, legacy=False):
    delegate = '_r80_base_step_all' if name == 'step_all' else '_r80_base_step_one'
    node = deepcopy(next(n for n in ast.parse(RUNTIME.read_text()).body
        if isinstance(n, ast.FunctionDef) and n.name == name
        and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                and c.func.id == delegate for c in ast.walk(n))))
    if legacy:
        released = {'positions'} if name == 'step_all' else {'rows', 'z'}
        node.body = [n for n in node.body if not (
            isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
            and n.value.value is None and
            {t.id for t in n.targets if isinstance(t, ast.Name)} == released)]
    def decode_payload(value):
        if isinstance(value,dict):
            return dict(value)
        if not value:
            return {}
        try:
            return json.loads(value)
        except Exception:
            return {}
    ns = {'COMMISSION': .0004, 'VC': VC, 'VPS': VPS, 'json': json,
          '_v90j_json': decode_payload, **namespace}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(RUNTIME), 'exec'), ns)
    return ns[name]


class Evidence(dict):
    """Weakref-capable payload; no global collection owns these objects."""


def quote(asset):
    result = {'asset': asset, 'price': 12.8 if asset == 'CNYRUBF' else 102.,
              'observed_at': CLOCK, 'source_gate_pass': True, 'market_open': True}
    if asset == 'CNYRUBF':
        result.update(source_names={'primary': 'TBANK_GRPC CNYRUBF'},
                      contract={'instrument_uid': UID, 'secid': asset})
    else:
        result.update(source_names={'primary': 'MOEX ISS BRX6'}, contract={'secid': 'BRX6'})
    return result


class FactoryDatabase:
    def __init__(self, assets):
        self.assets = assets
        self.references = []
        self.updates = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def connect(self):
        return self

    def rows(self):
        rows = []
        for index, asset in enumerate(self.assets):
            identity = VPS.identity(asset, quote(asset))
            payload = Evidence({
                'price_source_lock': identity, 'structural_policy_version': 'SEALED_TEST_EVENT',
                'entry_event_snapshot': {'event_id': 'sealed-' + str(index),
                                         'atr_proof': 'closed-native-bar;' * 15000},
                'source_locked_mark': {'identity': identity,
                                       'price': 12.7 if asset == 'CNYRUBF' else 101.25,
                                       'observed_at': CLOCK},
            })
            self.references.append(weakref.ref(payload))
            rows.append({'asset': asset, 'direction': 'LONG', 'units': 1.,
                         'avg_entry_price': 12.6 if asset == 'CNYRUBF' else 100.,
                         'last_price': 999., 'stop_price': 10., 'target_fraction': .5,
                         'active_trade_id': asset + '-' + str(index), 'payload': payload})
        return rows

    def execute(self, sql, parameters=()):
        if sql == VPG.QUOTE_POSITION_SQL:
            def quote_rows():
                result = []
                for index, asset in enumerate(self.assets):
                    payload = Evidence(price_source_lock=VPS.identity(asset, quote(asset)),
                                       source_locked_mark={'observed_at': CLOCK})
                    self.references.append(weakref.ref(payload))
                    result.append({'asset': asset, 'active_trade_id': asset + '-' + str(index),
                                   'payload': payload})
                return result
            return SimpleNamespace(fetchall=quote_rows)
        if sql.startswith('SELECT * FROM paper_positions'):
            # The cursor stores a factory, never the list returned by fetchall.
            return SimpleNamespace(fetchall=self.rows)
        if sql.startswith('UPDATE paper_positions') or sql.startswith('UPDATE paper_trades'):
            self.updates.append((sql, parameters))
            return None
        raise AssertionError('unexpected query: ' + sql)


def alive(references):
    gc.collect()
    return sum(reference() is not None for reference in references)


class R80SnapshotLifetimeTests(unittest.TestCase):
    def assert_full_evidence(self, row):
        payload = row['payload']
        self.assertIsInstance(payload, Evidence)
        self.assertEqual(len(payload['entry_event_snapshot']['atr_proof']), 270000)
        self.assertTrue(payload['entry_event_snapshot']['event_id'].startswith('sealed-'))
        self.assertEqual(payload['price_source_lock'], VPS.identity(row['asset'], quote(row['asset'])))

    def test_all_book_prepass_releases_18_payloads_before_core_without_losing_core_reads(self):
        for namespace_enabled in (True, False):
            counts = []
            for legacy in (True, False):
                with self.subTest(namespace_enabled=namespace_enabled, legacy=legacy):
                    db = FactoryDatabase(['CNYRUBF', 'BRENT'] * 9)
                    summary = [quote('CNYRUBF'), quote('BRENT')]
                    published, refreshed, quarantined = [], [], []
                    emit = lambda *args, **kwargs: None
                    connect = db.connect
                    entry_ns = {'configured': True} if namespace_enabled else None

                    def refresh(ns, positions):
                        self.assertIs(ns, entry_ns)
                        self.assertEqual(len(positions), 18)
                        self.assertEqual(alive(db.references), 18)
                        for row in positions:
                            self.assertEqual(row['payload']['price_source_lock'],
                                             VPS.identity(row['asset'], quote(row['asset'])))
                            self.assertNotIn('entry_event_snapshot', row['payload'])
                        refreshed.append(len(positions))

                    def base(*args):
                        self.assertIs(args[0], summary)
                        self.assertIs(args[1], connect)
                        self.assertEqual(args[2:5], ('version-test', CLOCK, .0004))
                        self.assertIs(args[5], emit)
                        retained = alive(db.references)
                        counts.append(retained)
                        self.assertEqual(retained, 18 if legacy else 0)
                        # The actual core remains free to read complete execution
                        # evidence. Releasing a prepass does not project the DB.
                        core_rows = db.execute('SELECT * FROM paper_positions').fetchall()
                        for row in core_rows:
                            self.assert_full_evidence(row)
                        return {'status': 'CORE', 'positions_read': len(core_rows)}

                    vpg = SimpleNamespace(_entry_namespace=entry_ns,
                        QUOTE_POSITION_SQL=VPG.QUOTE_POSITION_SQL,
                        publish_quote=lambda asset, q: published.append((asset, q['price'])),
                        refresh_position_quotes=refresh)
                    run = load_r80('step_all', {'VPG': vpg, '_r80_base_step_all': base,
                        '_r80_quarantine_source_incident': lambda c: quarantined.append(c is connect)}, legacy=legacy)
                    self.assertEqual(run(summary, connect, 'version-test', CLOCK, .0004, emit),
                                     {'status': 'CORE', 'positions_read': 18})
                    self.assertEqual(published, [('CNYRUBF', 12.8), ('BRENT', 102.)])
                    self.assertEqual(refreshed, [18] if namespace_enabled else [])
                    self.assertEqual(quarantined, [True])
                    self.assertEqual(alive(db.references), 0)
            self.assertEqual(counts, [18, 0])

    def test_book_prepass_preserves_full_guards_source_filtering_and_all_delegated_values(self):
        observations = []
        for legacy in (True, False):
            db = FactoryDatabase(['CNYRUBF', 'BRENT'])
            valid = dict(quote('CNYRUBF'), horizon='4h', research_decision='LONG',
                         trade_plan={'immutable_core_evidence': 'keep-in-delegated-summary'})
            foreign = dict(valid, horizon='1h', source_names={'primary': 'MOEX ISS CNYRUBF'},
                           contract={'secid': 'CNYRUBF'})
            stale = dict(quote('BRENT'), horizon='4h', research_decision='LONG')
            summary, candidates = [valid, foreign, stale], {'CNYRUBF': valid, 'BRENT': stale}
            original_summary, original_candidates = deepcopy(summary), deepcopy(candidates)
            policy = {'mode': 'CURRENCY'}
            checked = []

            def selected(row, now):
                self.assertEqual(now, CLOCK)
                self.assert_full_evidence(row)
                return quote(row['asset']) if row['asset'] == 'CNYRUBF' else {}

            def trailing(c, name, row, rows, q, at):
                self.assertIs(c, db)
                self.assert_full_evidence(row)
                self.assertEqual((name, at), ('Currency', CLOCK))
                self.assertEqual(bool(q), row['asset'] == 'CNYRUBF')
                checked.append(('trailing', row['asset']))

            def filter_context(row, book, rows):
                self.assert_full_evidence(row)
                checked.append(('timeframe', row['asset']))
                return book, rows

            def thesis(c, row, book, rows, now=None):
                self.assertIs(c, db)
                self.assert_full_evidence(row)
                self.assertEqual(now, CLOCK)
                checked.append(('thesis', row['asset']))
                return book, rows, {'active': True, 'held_horizon': '4h', 'hard_exit_allowed': False}

            def base(*args):
                self.assertIs(args[0], db)
                self.assertEqual(args[1], 'Currency')
                self.assertIs(args[2], policy)
                self.assertEqual(args[3], {'CNYRUBF': valid})
                self.assertEqual(args[4], {'CNYRUBF': 12.8, 'BRENT': 101.25})
                self.assertEqual(args[5:9], (16., 80., CLOCK, .0004))
                self.assertEqual(args[9], [valid])
                self.assertEqual(args[9][0]['trade_plan']['immutable_core_evidence'],
                                 'keep-in-delegated-summary')
                count = alive(db.references)
                self.assertEqual(count, 2 if legacy else 0)
                observations.append((count, deepcopy(args[3:]), deepcopy(db.updates), list(checked)))
                return {'status': 'CORE'}

            vtm = SimpleNamespace(owns_position=lambda row: True,
                                  apply_trailing=trailing, filter_lower_context=filter_context)
            run = load_r80('_step_one', {'VPG': SimpleNamespace(quote_for_position=selected),
                           'VTM': vtm, '_r80_base_step_one': base}, legacy=legacy)
            with patch.object(VTG, 'guard_open_position', thesis):
                self.assertEqual(run(db, 'Currency', policy, candidates,
                    {'CNYRUBF': 999., 'BRENT': 999.}, 16., 80., CLOCK, .0004, summary), {'status': 'CORE'})
            self.assertEqual(summary, original_summary)
            self.assertEqual(candidates, original_candidates)
            self.assertEqual(alive(db.references), 0)
            self.assertEqual(len(db.updates), 8)  # Same source and thesis writes for both positions.
        self.assertEqual([x[0] for x in observations], [2, 0])
        self.assertEqual(observations[0][1:], observations[1][1:])

    def test_empty_book_delegates_without_a_last_loop_position(self):
        db = FactoryDatabase([])
        summary = [quote('CNYRUBF')]
        called = []

        def base(*args):
            called.append(True)
            self.assertEqual(args[3], {})
            self.assertEqual(args[9], summary)
            self.assertEqual(alive(db.references), 0)
            return {'status': 'EMPTY_CORE'}

        run = load_r80('_step_one', {'_r80_base_step_one': base})
        self.assertEqual(run(db, 'Currency', {}, {}, {}, 16., 80., CLOCK, .0004, summary),
                         {'status': 'EMPTY_CORE'})
        self.assertEqual(called, [True])
        self.assertEqual(db.updates, [])


if __name__ == '__main__':
    unittest.main()
