"""Closed-journal SQL/UI parity with large durable evidence, without startup."""
import ast
import base64
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
import hashlib
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

# Frozen v9 generator: independent constants and builders preserve the exact SQL
# baseline, including absent-vs-null and malformed target/source semantics.
_V9_JOURNAL_SOURCE = r'''"""Bounded SQL view of closed trades; full execution evidence stays in storage.

The journal displays accounting columns plus the fields below. Its take-profit
notice only distinguishes zero/one target from two or more, so two small target
steps preserve that display without fetching historical event/proof graphs.
Source identities remain available to inspect the recorded price basis.
"""

SCALAR_FIELDS = (
    'exit_reason', 'close_reason', 'stop_price', 'last_stop_price', 'take_price',
    'target_price', 'learning_label', 'opening_fraction', 'entry_signal_tier',
    'signal_tier', 'structural_stop', 'initial_stop_price', 'r17_tp1_done',
    'r17_tp1_at', 'last_target_kind', 'mfe_pct', 'mae_pct', 'capture_ratio',
    'entry_primary_source', 'entry_secondary_source', 'entry_contract_secid',
    'entry_contract_unit', 'entry_verification_mode', 'entry_data_latency_class',
    'entry_source_divergence', 'entry_execution_observed_at',
    'entry_market_observed_at', 'last_exit_market_observed_at',
    'price_source_status', 'data_integrity_status',
)
IDENTITY_FIELDS = ('version', 'asset', 'key', 'primary_source', 'contract_id',
                   'legacy_fixed_adapter')
CONTRACT_FIELDS = ('secid', 'symbol', 'instrument_uid', 'ticker', 'figi', 'lot',
                   'price_tick', 'tick_value_rub', 'price_unit',
                   'broker_price_unit', 'normalization_factor', 'continuous')
VALUATION_FIELDS = (
    'version', 'asset', 'primary_source', 'source_key', 'contract_id',
    'contract_identity_status', 'provider_label', 'provider_ticker',
    'provider_instrument_id', 'provider_ticker_verified', 'price_field',
    'quote_observed_at', 'exact_contract_verified', 'price_series_type',
    'valuation_mode',
)


def _scalar(expression):
    # Unexpected containers must not smuggle evidence graphs into a UI field.
    return ("CASE WHEN jsonb_typeof(" + expression + ") IN ('array','object') "
            "THEN 'null'::jsonb ELSE " + expression + " END")


def _object(expression, fields=(), extra=None):
    values = [(key, _scalar(expression + "->'" + key + "'")) for key in fields]
    values.extend((extra or {}).items())
    selected = ','.join("('" + key + "'," + value + ")" for key, value in values)
    # Keep absence distinct from a recorded JSON null, notably unpinned Brent.
    return ("(CASE WHEN jsonb_typeof(" + expression + ")='object' THEN "
            "(SELECT COALESCE(jsonb_object_agg(journal_key,journal_value),'{}'::jsonb) "
            "FROM (VALUES " + selected + ") AS journal_fields(journal_key,journal_value) "
            "WHERE " + expression + " ? journal_key) ELSE 'null'::jsonb END)")


def _ladder(expression):
    step = _object('journal_step.value', ('price', 'kind'))
    # Index two elements directly: never expand a potentially large JSON array.
    return ("(CASE WHEN jsonb_typeof(" + expression + ")='array' THEN "
            "(SELECT COALESCE(jsonb_agg(" + step + " ORDER BY journal_step.ordinality),'[]'::jsonb) "
            "FROM (VALUES (1," + expression + "->0),(2," + expression + "->1)) "
            "AS journal_step(ordinality,value) WHERE journal_step.ordinality<="
            "jsonb_array_length(" + expression + ")) ELSE 'null'::jsonb END)")


def _payload_sql():
    nested = {key: _object("(payload->'" + key + "')", IDENTITY_FIELDS)
              for key in ('price_source_lock', 'entry_execution_source_identity',
                          'last_exit_source_identity')}
    nested['contract_identity'] = _object("(payload->'contract_identity')", (
        'asset', 'contract_id', 'price_unit', 'primary_source',
        'verification_mode', 'continuous_series'))
    nested['entry_contract'] = _object("(payload->'entry_contract')", CONTRACT_FIELDS)
    nested['entry_source_names'] = _object("(payload->'entry_source_names')", ('primary', 'secondary'))
    nested['entry_valuation_basis'] = _object("(payload->'entry_valuation_basis')", VALUATION_FIELDS)
    nested['source_locked_mark'] = _object("(payload->'source_locked_mark')", ('price', 'observed_at'), {
        'identity': _object("(payload#>'{source_locked_mark,identity}')", IDENTITY_FIELDS)})
    for key in ('active_target_ladder', 'initial_target_ladder'):
        nested[key] = _ladder("(payload->'" + key + "')")
    for key in ('active_target_event_snapshot', 'entry_event_snapshot'):
        nested[key] = _object("(payload->'" + key + "')", extra={
            'target_ladder': _ladder("(payload#>'{" + key + ",target_ladder}')")})
    return _object('payload', SCALAR_FIELDS, nested)


JOURNAL_PAYLOAD_SQL = _payload_sql()
'''
_V9_JOURNAL_NAMESPACE = {}
exec(compile(_V9_JOURNAL_SOURCE, '<frozen-v9-journal>', 'exec'), _V9_JOURNAL_NAMESPACE)
V9_JOURNAL_PAYLOAD_SQL = _V9_JOURNAL_NAMESPACE['JOURNAL_PAYLOAD_SQL']




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
    def test_frozen_projection_matches_the_deployed_v9_sql(self):
        self.assertEqual(hashlib.sha256(V9_JOURNAL_PAYLOAD_SQL.encode()).hexdigest(),
                         '11262df029bda262e535f7b61d3d43a4fbec0d6a02dabf4620f06bd91f388971')

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
    def connect(self, *, legacy=False, projection=None):
        with self.driver.connect(DSN, row_factory=self.row_factory) as connection:
            connection.execute(f'SET search_path TO {self.schema}')
            if legacy or projection is not None:
                class OriginalPayload:
                    def execute(self, query, parameters):
                        replacement = 'payload' if legacy else projection + ' AS payload'
                        query = query.replace(JOURNAL_PAYLOAD_SQL + ' AS payload', replacement)
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
        previous = read_journal(lambda: self.connect(projection=V9_JOURNAL_PAYLOAD_SQL))
        self.assertEqual(original['status'], 'OK', original)
        self.assertEqual(projected['status'], 'OK', projected)
        self.assertEqual(projected, previous)
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

    def test_frozen_projection_preserves_absent_null_and_malformed_fields(self):
        values = (None, False, 0, '', [], {}, [None, {}, {'price':None, 'kind':False}],
                  {'history': ['UNUSED_HEAVY_FIELD']})
        shapes = [None, False, 0, '', [], {}, {'unrequested':True}]
        names = (*_V9_JOURNAL_NAMESPACE['SCALAR_FIELDS'], 'price_source_lock',
                 'entry_execution_source_identity', 'last_exit_source_identity',
                 'contract_identity', 'entry_contract', 'entry_source_names',
                 'entry_valuation_basis', 'source_locked_mark', 'active_target_ladder',
                 'initial_target_ladder', 'active_target_event_snapshot', 'entry_event_snapshot')
        for name in names:
            shapes.extend({name:value} for value in values)
        for value in values:
            shapes.extend(({'source_locked_mark':{'identity':value}},
                           {'price_source_lock':{'contract_id':value}},
                           {'entry_event_snapshot':{'target_ladder':value}},
                           {'active_target_event_snapshot':{'target_ladder':value}}))
        with self.connect() as c:
            rows = c.execute('SELECT sample.ordinality, '+V9_JOURNAL_PAYLOAD_SQL+' AS previous, '
                             +JOURNAL_PAYLOAD_SQL+' AS current FROM '
                             'jsonb_array_elements(%s::jsonb) WITH ORDINALITY AS sample(payload,ordinality) '
                             'ORDER BY sample.ordinality', (json.dumps(shapes),)).fetchall()
            self.assertEqual(len(rows), len(shapes))
            for row in rows:
                self.assertEqual(row['current'], row['previous'], row['ordinality'])
            for sql in (V9_JOURNAL_PAYLOAD_SQL, JOURNAL_PAYLOAD_SQL):
                row = c.execute('SELECT '+sql+' AS projected FROM '
                                '(VALUES (NULL::jsonb)) AS sample(payload)').fetchone()
                self.assertIsNone(row['projected'])

    def test_record_and_presence_execute_once_per_toasted_selected_trade(self):
        count, selected = 104, 100
        with self.connect() as c:
            # A connection-local fixture shadows the shared table. Its genuine
            # low-compression TOAST totals about 5 MiB, not a production scan.
            c.execute('CREATE TEMP TABLE paper_trades (LIKE '+self.schema+'.paper_trades)')
            rows = []
            for index in range(count):
                row = trade_fixture(index)
                def heavy(kind):
                    blob = hashlib.shake_256(f'{kind}:{index}'.encode()).digest(12288)
                    return 'UNUSED_HEAVY_FIELD:'+base64.b64encode(blob).decode('ascii')
                p = row['payload']
                p['diagnostic_history'] = heavy('root')
                p['entry_event_snapshot']['diagnostic_history'] = heavy('event')
                p['source_locked_mark']['diagnostic_history'] = heavy('mark')
                rows.append((*list(row.values())[:-1], json.dumps(p)))
            with c.cursor() as cursor:
                cursor.executemany('INSERT INTO paper_trades VALUES('
                                   +','.join(['%s']*18)+',%s::jsonb)', rows)
            storage = dict(c.execute('''SELECT COUNT(*) AS n,
                MIN(pg_column_size(payload)) AS min_stored_bytes,
                MIN(octet_length(payload::text)) AS min_text_bytes,
                MIN(pg_column_size(payload)::float8 / octet_length(payload::text)) AS min_ratio
                FROM paper_trades''').fetchone())
            storage['toast_bytes'] = c.execute("SELECT pg_relation_size(reltoastrelid) AS n "
                "FROM pg_class WHERE oid='pg_temp.paper_trades'::regclass").fetchone()['n']
            captured = []
            class Capture:
                def execute(self, query, parameters):
                    captured.append((query, parameters))
                    return c.execute(query, parameters)
            @contextmanager
            def connect():
                yield Capture()
            current = read_journal(connect, selected)
            self.assertEqual(current['status'], 'OK', current)
            self.assertEqual(current['returned_count'], selected)
            query, parameters = captured[0]
            previous_rows = c.execute(query.replace(JOURNAL_PAYLOAD_SQL,
                                      V9_JOURNAL_PAYLOAD_SQL), parameters).fetchall()
            self.assertEqual([r['trade_id'] for r in previous_rows],
                             [r['trade_id'] for r in current['trades']])
            for previous, actual in zip(previous_rows, current['trades']):
                self.assertEqual(actual['trade_id'], previous['trade_id'])
                self.assertEqual(actual['payload'], previous['payload'])
            plan = c.execute('EXPLAIN (ANALYZE,VERBOSE,FORMAT JSON) '+query,
                             parameters).fetchone()['QUERY PLAN'][0]['Plan']
        self.assertEqual(storage['n'], count)
        self.assertGreater(storage['min_text_bytes'], 49000)
        self.assertGreater(storage['min_stored_bytes'], 24000)
        self.assertGreater(storage['min_ratio'], .5)
        self.assertGreater(storage['toast_bytes'], 0)
        nodes, pending = [], [plan]
        while pending:
            node = pending.pop(); nodes.append(node); pending.extend(node.get('Plans', []))
        scans = [n for n in nodes if n.get('Node Type') == 'Function Scan'
                 and n.get('Function Name') in ('jsonb_to_record','jsonb_object_keys')]
        sorts = [n for n in nodes if n.get('Node Type') in ('Sort','Incremental Sort')]
        print('journal_record_plan '+json.dumps({'fixture':storage,
            'scans':[{k:n.get(k) for k in ('Function Name','Actual Loops','Actual Rows')} for n in scans],
            'sorts':[{'rows':n.get('Actual Rows'), 'width':n.get('Plan Width'),
                      'output':[v if len(v)<100 else '<projected expression>' for v in n.get('Output', [])]}
                     for n in sorts]}, sort_keys=True), flush=True)
        self.assertEqual(sorted(n['Function Name'] for n in scans),
                         ['jsonb_object_keys','jsonb_to_record'])
        for node in scans:
            self.assertEqual(node['Actual Loops'], selected)
        record = next(n for n in scans if n['Function Name']=='jsonb_to_record')
        self.assertEqual(record['Actual Rows'], 1)
        self.assertTrue(sorts)
        for node in sorts:
            for value in node.get('Output', []):
                # Raw toasted table payload is allowed. Root record columns
                # must remain inside their per-trade scalar subquery.
                self.assertFalse(value.replace('"','').strip().startswith('journal_root.'), value)


if __name__ == '__main__':
    unittest.main()
