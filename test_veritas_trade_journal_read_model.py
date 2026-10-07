"""Closed-journal SQL/UI parity with large durable evidence, without startup."""
import ast
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
import os
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import Mock
import uuid

import veritas_trade_view as VTV
from veritas_trade_journal_read_model import JOURNAL_PAYLOAD_SQL


ROOT = Path(__file__).resolve().parent
DSN = os.getenv('VERITAS_QUALITY_TEST_DSN', '')


@lru_cache(maxsize=1)
def reader_code():
    path = ROOT / 'veritas_intelligence.py'
    node = next(n for n in ast.parse(path.read_text()).body
                if isinstance(n, ast.FunctionDef) and n.name == '_v90r25_trades_fast')
    return compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec')


def read_journal(connect, limit=80, enabled=True):
    ns = {'pg_connect': connect, 'pg_enabled': lambda: enabled, 'json': json,
          'VTV': VTV, '_v90r23_trade_lock': threading.Lock(),
          '_v90r23_trade_cache': {'value': None}}
    exec(reader_code(), ns)
    return ns['_v90r25_trades_fast'](limit)


def trade_fixture(index=0, *, heavy=False):
    source_case = index % 3
    asset = 'CNYRUBF' if source_case == 0 else 'BRENT'
    primary = ('TBANK_GRPC CNYRUBF', 'ProFinance', 'MOEX ISS BRX6')[source_case]
    key = ('TBANK_GRPC:CNYRUBF', 'PROFINANCE:Brent oil', 'MOEX:BRENT')[source_case]
    cid = ('c300543d-aa18-4249-b110-615409dde036', None, 'BRX6')[source_case]
    identity = {'version': 'R80_SOURCE_LOCK', 'asset': asset, 'key': key,
                'primary_source': primary, 'contract_id': cid}
    at = datetime(2026, 10, 7, 8, tzinfo=timezone.utc) + timedelta(seconds=index * 61)
    evidence = 'UNUSED_HEAVY_FIELD:' + 'sealed ATR and target evidence;' * (3500 if heavy else 1)
    ladder = [{'price': 12.9 + i / 10, 'kind': 'FINAL' if i else 'TP1',
               'source_zone': {'proof': evidence}} for i in range(3)]
    p = {
        'exit_reason': 'TAKE_PROFIT_FULL_MIN_POSITION_R17' if index % 2 else None,
        'close_reason': 'STRUCTURAL_STOP', 'stop_price': 12.4 if index % 2 else None,
        'last_stop_price': 12.3, 'structural_stop': 12.2, 'initial_stop_price': 12.1,
        'take_price': 12.9, 'target_price': 13.0, 'learning_label': 'COST_ERASED_WIN',
        'opening_fraction': .5, 'entry_signal_tier': 'SUPER_SHORT', 'signal_tier': 'SHORT',
        'r17_tp1_done': bool(index % 3), 'r17_tp1_at': at.isoformat(),
        'last_target_kind': 'FINAL' if index % 4 else None,
        'mfe_pct': 1.5, 'mae_pct': -.25, 'capture_ratio': .3 if index % 2 else None,
        'price_source_lock': deepcopy(identity),
        'entry_execution_source_identity': deepcopy(identity),
        'last_exit_source_identity': deepcopy(identity),
        'entry_source_names': {'primary': primary, 'secondary': 'VERIFICATION_ONLY'},
        'entry_primary_source': primary, 'entry_secondary_source': 'VERIFICATION_ONLY',
        'entry_contract_secid': 'CNYRUBF' if source_case == 0 else cid,
        'entry_contract_unit': 'RUB_PER_CNY' if source_case == 0 else None,
        'entry_verification_mode': 'EXACT_CONTRACT' if cid else None,
        'entry_data_latency_class': 'LIVE', 'entry_source_divergence': 0.0,
        'entry_execution_observed_at': at.isoformat(),
        'entry_market_observed_at': at.isoformat(),
        'last_exit_market_observed_at': (at + timedelta(minutes=20)).isoformat(),
        'price_source_status': 'OK', 'data_integrity_status': 'OK',
        'entry_contract': {'secid': 'CNYRUBF', 'instrument_uid': cid, 'lot': 1000,
                           'price_tick': .001, 'tick_value_rub': 1.,
                           'price_unit': 'RUB_PER_CNY', 'broker_price_unit': 'POINTS',
                           'normalization_factor': 1.} if source_case == 0 else
                          ({'secid': cid, 'price_unit': 'USD_PER_BARREL'} if cid else None),
        'contract_identity': {'asset': asset, 'contract_id': cid, 'primary_source': primary,
                              'price_unit': None, 'verification_mode': None,
                              'continuous_series': False},
        'entry_valuation_basis': {
            'version': 'VALUATION_SOURCE_BASIS_V1', 'asset': asset,
            'primary_source': primary, 'source_key': key, 'contract_id': cid,
            'contract_identity_status': 'PINNED_CONTRACT' if cid else 'UNVERIFIED_PROVIDER_SERIES',
            'provider_label': 'Brent oil' if source_case == 1 else None,
            'provider_ticker': 'brent' if source_case == 1 else None,
            'provider_instrument_id': 199, 'provider_ticker_verified': source_case == 1,
            'price_field': 'LAST', 'quote_observed_at': at.isoformat(),
            'exact_contract_verified': bool(cid), 'price_series_type': None if cid else 'UNVERIFIED',
            'valuation_mode': 'NORMALIZED_PAPER'},
        'source_locked_mark': {'identity': deepcopy(identity), 'price': 12.75,
                               'observed_at': at.isoformat()},
        'entry_event_snapshot': {'atr_proof': evidence, 'target_history': [evidence]},
        'fill_economics_gate': {'historical_proof': evidence},
    }
    case = index % 6
    if case in (0, 1):
        p['active_target_ladder'] = ladder[:case + 1]
    elif case == 2:
        p['initial_target_ladder'] = ladder
    elif case == 3:
        p['active_target_event_snapshot'] = {'target_ladder': ladder[:2], 'history': evidence}
    elif case == 4:
        p['entry_event_snapshot']['target_ladder'] = ladder[:2]
    else:
        p['active_target_ladder'] = []  # Empty arrays are truthy in the actual UI.
        p['initial_target_ladder'] = ladder[:2]
    return {
        'trade_id': 'journal-' + str(index), 'portfolio_name': ('Currency', 'Impulse', 'Aggressive')[source_case],
        'asset': asset, 'direction': 'LONG' if index % 2 else 'SHORT',
        'opened_at': at, 'closed_at': at + timedelta(minutes=20) if index % 11 else None,
        'avg_entry_price': 12.71, 'avg_exit_price': 12.8 if index % 5 else None,
        'gross_pnl_rub': 121.91, 'fees_rub': None if index % 13 == 0 else 193.45,
        'funding_rub': 0.0 if index % 7 == 0 else 3.25,
        'net_pnl_rub': None if index % 17 == 0 else -74.79,
        'return_on_entry_nav': -.000077344, 'profitable': False,
        'meaningful_win': False, 'status': 'CLOSED', 'setup': 'STRUCTURAL_BREAKOUT',
        'horizon': ('1m', '5m', '1h')[index % 3], 'payload': p,
    }


class JournalReaderTests(unittest.TestCase):
    def test_fast_reader_uses_projection_and_keeps_closed_trade_accounting(self):
        row = trade_fixture(1)
        row['entry_notional_rub'] = 1500.0
        connection = Mock()
        connection.execute.return_value.fetchall.return_value = [row]

        @contextmanager
        def connect():
            yield connection

        result = read_journal(connect, limit=80)
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(result['api_source'], 'fast_sql')
        self.assertEqual(result['returned_count'], 1)
        query, parameters = connection.execute.call_args.args
        self.assertIn(JOURNAL_PAYLOAD_SQL + ' AS payload', query)
        self.assertEqual(parameters, (80,))
        self.assertEqual(connection.execute.call_count, 1)
        z = result['trades'][0]
        self.assertEqual(z['avg_entry_price'], row['avg_entry_price'])
        self.assertEqual(z['avg_exit_price'], row['avg_exit_price'])
        self.assertEqual(z['total_trade_pnl_rub'], -74.79)
        self.assertAlmostEqual(z['total_trade_return_pct'], -4.986)
        self.assertEqual(z['trade_return_basis_rub'], 1500.)
        self.assertTrue(z['tp1_done'])
        self.assertEqual(z['trade_fees_rub'], 193.45)

    def test_unavailable_database_does_not_open_any_connection(self):
        connect = Mock(side_effect=AssertionError('must not open a connection'))
        self.assertEqual(read_journal(connect, enabled=False), {'status': 'UNAVAILABLE', 'trades': []})
        connect.assert_not_called()


@unittest.skipUnless(DSN, 'isolated PostgreSQL test database not configured')
class JournalProjectionSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg.rows import dict_row
        cls.driver, cls.row_factory = psycopg, staticmethod(dict_row)
        cls.schema = 'trade_journal_test_' + uuid.uuid4().hex
        with psycopg.connect(DSN) as c:
            if c.execute('SELECT current_database()').fetchone()[0] != 'veritas_quality_test':
                raise RuntimeError('Refusing writes outside veritas_quality_test')
            c.execute(f'CREATE SCHEMA {cls.schema}')
            cls.addClassCleanup(cls.drop_schema)
            c.execute(f'SET search_path TO {cls.schema}')
            c.execute('''CREATE TABLE paper_trades(
                trade_id text PRIMARY KEY, portfolio_name text, asset text, direction text,
                opened_at timestamptz, closed_at timestamptz, avg_entry_price double precision,
                avg_exit_price double precision, gross_pnl_rub double precision,
                fees_rub double precision, funding_rub double precision, net_pnl_rub double precision,
                return_on_entry_nav double precision, profitable boolean, meaningful_win boolean,
                status text, setup text, horizon text, payload jsonb)''')
            c.execute('CREATE TABLE paper_orders(trade_id text,side text,notional_rub double precision)')
            with c.cursor() as cursor:
                rows = []
                orders = []
                for i in range(104):
                    row = trade_fixture(i, heavy=True)
                    rows.append((*list(row.values())[:-1], json.dumps(row['payload'])))
                    side = 'BUY' if row['direction'] == 'LONG' else 'SELL_SHORT'
                    if i % 7:
                        orders.extend([(row['trade_id'], side, 1000.), (row['trade_id'], side, 500.)])
                    orders.append((row['trade_id'], 'SELL' if side == 'BUY' else 'BUY_TO_COVER', 99999.))
                row = trade_fixture(1000)
                row.update(status='OPEN', closed_at=None)
                rows.append((*list(row.values())[:-1], json.dumps(row['payload'])))
                cursor.executemany('INSERT INTO paper_trades VALUES(' + ','.join(['%s'] * 18) + ',%s::jsonb)', rows)
                cursor.executemany('INSERT INTO paper_orders VALUES(%s,%s,%s)', orders)

    @classmethod
    def drop_schema(cls):
        with cls.driver.connect(DSN) as c:
            c.execute(f'DROP SCHEMA IF EXISTS {cls.schema} CASCADE')

    @contextmanager
    def connect(self, *, legacy=False):
        with self.driver.connect(DSN, row_factory=self.row_factory) as connection:
            connection.execute(f'SET search_path TO {self.schema}')
            if legacy:
                class OriginalPayload:
                    def execute(self, query, parameters):
                        query = query.replace(JOURNAL_PAYLOAD_SQL + ' AS payload', 'payload')
                        return connection.execute(query, parameters)
                yield OriginalPayload()
            else:
                yield connection

    def payload_hashes(self):
        with self.connect() as c:
            return c.execute('SELECT trade_id,md5(payload::text) AS digest FROM paper_trades ORDER BY trade_id').fetchall()

    def test_large_page_preserves_accounting_sources_and_actual_ui(self):
        before = self.payload_hashes()
        original = read_journal(lambda: self.connect(legacy=True))
        projected = read_journal(self.connect)
        self.assertEqual(original['status'], 'OK', original)
        self.assertEqual(projected['status'], 'OK', projected)
        old, new = original['trades'], projected['trades']
        self.assertEqual(len(old), 80)
        self.assertEqual(len(new), 80)
        source_fields = ('price_source_lock', 'entry_execution_source_identity', 'last_exit_source_identity',
                         'entry_contract', 'contract_identity', 'entry_source_names', 'entry_valuation_basis',
                         'source_locked_mark')
        for a, b in zip(old, new):
            self.assertEqual({k: v for k, v in a.items() if k != 'payload'},
                             {k: v for k, v in b.items() if k != 'payload'})
            for key in source_fields:
                self.assertEqual(b['payload'][key], a['payload'][key], (b['trade_id'], key))
            self.assertEqual(VTV.trade_result(a), VTV.trade_result(b))
            self.assertNotIn('UNUSED_HEAVY_FIELD', json.dumps(b['payload']))
        old_bytes = len(json.dumps(old, default=str).encode())
        new_bytes = len(json.dumps(new, default=str).encode())
        self.assertGreater(old_bytes, 25 * 1024 * 1024)
        self.assertLess(new_bytes, 80 * 7000)
        self.assertLess(new_bytes, old_bytes / 50)
        pairs = [{'original': a, 'projected': b} for a, b in zip(old[:24], new[:24])]
        ui = subprocess.run(['node', str(ROOT / 'tools/test_trade_journal_projection_ui.cjs')],
                            input=json.dumps({'pairs': pairs}, default=str), text=True,
                            capture_output=True, cwd=ROOT, timeout=60)
        self.assertEqual(ui.returncode, 0, ui.stdout + ui.stderr)
        self.assertEqual(self.payload_hashes(), before)

    def test_same_recent_window_and_entry_fill_denominator(self):
        for requested, limit in ((1, 20), (100, 100), (999, 200)):
            with self.subTest(limit=limit):
                result = read_journal(self.connect, requested)
                self.assertEqual(result['status'], 'OK', result)
                with self.connect() as c:
                    expected = c.execute('''SELECT trade_id FROM paper_trades
                        WHERE closed_at IS NOT NULL OR status='CLOSED'
                        ORDER BY COALESCE(closed_at,opened_at) DESC LIMIT %s''', (limit,)).fetchall()
                self.assertEqual([r['trade_id'] for r in result['trades']], [r['trade_id'] for r in expected])
                for row in result['trades']:
                    index = int(row['trade_id'].split('-')[-1])
                    self.assertNotEqual(index, 1000)
                    self.assertEqual(row['entry_notional_rub'], 1500. if index % 7 else None)
                    self.assertEqual(row['trade_return_basis'], 'ENTRY_NOTIONAL' if index % 7 else 'UNAVAILABLE')

    def test_nulls_empty_targets_and_nested_graphs_are_bounded_before_decode(self):
        raw = trade_fixture(5)['payload']
        heavy = {'UNUSED_HEAVY_FIELD': ['historical-proof' * 10000] * 4}
        raw['price_source_lock']['contract_id'] = None
        raw['price_source_lock']['unexpected_history'] = heavy
        raw['learning_label'] = heavy
        raw['active_target_event_snapshot'] = {'target_ladder': [
            {'price': 101., 'kind': 'TP1', 'nested_proof': heavy},
            {'price': 102., 'kind': 'FINAL', 'nested_proof': heavy},
            {'price': 103., 'kind': 'FINAL', 'nested_proof': heavy}], 'events': heavy}
        with self.connect() as c:
            row = c.execute('SELECT ' + JOURNAL_PAYLOAD_SQL + ' AS payload FROM '
                            '(VALUES (%s::jsonb)) AS sample(payload)', (json.dumps(raw),)).fetchone()
        projected = row['payload']
        self.assertIsNone(projected['price_source_lock']['contract_id'])
        self.assertIn('contract_id', projected['price_source_lock'])
        self.assertIsNone(projected['learning_label'])
        self.assertEqual(projected['active_target_ladder'], [])
        self.assertEqual(projected['active_target_event_snapshot']['target_ladder'], [
            {'price': 101., 'kind': 'TP1'}, {'price': 102., 'kind': 'FINAL'}])
        self.assertNotIn('unexpected_history', projected['price_source_lock'])
        self.assertNotIn('UNUSED_HEAVY_FIELD', json.dumps(projected))
        self.assertLess(len(json.dumps(projected)), 7000)


if __name__ == '__main__':
    unittest.main()
