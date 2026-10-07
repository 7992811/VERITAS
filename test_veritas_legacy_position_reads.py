"""Bound legacy helpers omit only positions they already skip in Python.

Native SQL compares the same live helpers with/without the WHERE predicate,
including actual stop calculations, cost reads, both journals and source proof.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import inspect
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

import veritas_portfolio as VP
import veritas_portfolio_runtime as RT
import veritas_timeframe_management as TM


DSN = os.getenv('VERITAS_QUALITY_TEST_DSN', '')
SQL_NULL = object()
BASE_SQL = 'SELECT * FROM paper_positions WHERE portfolio_name=%s'
FILTERED_SQL = BASE_SQL+' AND '+TM.LEGACY_POSITION_SQL_PREDICATE
# Independent conservative reference for the single-extraction JSONPath.
REFERENCE_PREDICATE = """NOT COALESCE(
    jsonb_typeof(payload) = 'object'
    AND jsonb_typeof(payload->'structural_policy_version') = 'string'
    AND payload->>'structural_policy_version' <> '', FALSE)"""


class LegacyPositionBindingTests(unittest.TestCase):
    def test_both_changed_helpers_are_reachable_from_final_runtime(self):
        seen, pending = set(), [RT.FINAL_STEP_ONE]
        while pending:
            fn = pending.pop()
            if fn in seen:
                continue
            seen.add(fn)
            for name in fn.__code__.co_names:
                target = fn.__globals__.get(name)
                if inspect.isfunction(target) and (
                        'step_one' in name or name in
                        ('_v90tr_apply', '_v90r65_base_trailing_apply')):
                    pending.append(target)
        self.assertIn(RT._v90pi_step_one, seen)
        self.assertIn(RT._v90r65_base_trailing_apply, seen)
        self.assertIs(VP._v90pi_step_one, RT._v90pi_step_one)
        self.assertIs(VP._v90tr_apply, RT._v90tr_apply)
        for fn in (RT._v90pi_step_one, RT._v90r65_base_trailing_apply):
            self.assertEqual(Path(fn.__code__.co_filename).name, 'veritas_portfolio.py')
            self.assertIn('LEGACY_POSITION_SQL_PREDICATE', fn.__code__.co_names)
        self.assertIn('R17 structural trailing', RT._v90r65_base_trailing_apply.__doc__)
        self.assertNotIn('LEGACY_POSITION_SQL_PREDICATE', RT._v90tr_apply.__code__.co_names)


class ReadTrace:
    def __init__(self, connection, legacy, predicate=None):
        self.connection, self.legacy, self.predicate = connection, legacy, predicate
        self.reads, self.writes = [], []

    def execute(self, query, params=()):
        sql = query
        if query == FILTERED_SQL:
            if self.legacy:
                sql = BASE_SQL
            elif self.predicate is not None:
                sql = BASE_SQL+' AND '+self.predicate
        if query.lstrip().startswith('UPDATE'):
            self.writes.append((query, deepcopy(params)))
        cursor = self.connection.execute(sql, params)
        if query not in (BASE_SQL, FILTERED_SQL):
            return cursor
        owner = self
        class Rows:
            def fetchall(self):
                rows = cursor.fetchall()
                owner.reads.append({
                    'filtered': query == FILTERED_SQL,
                    'ids': [z['active_trade_id'] for z in rows],
                    'encoded_bytes': len(json.dumps(rows, default=str).encode()),
                })
                return rows
        return Rows()


@unittest.skipUnless(DSN, 'isolated PostgreSQL test database not configured')
class LegacyPositionReadsSQLTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        self.driver, self.sql, self.row_factory = psycopg, sql, dict_row
        self.schema = 'legacy_position_reads_'+uuid.uuid4().hex
        self.now = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
        with self.driver.connect(DSN, row_factory=self.row_factory) as c:
            self.verify_database(c)
            c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
            c.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
            c.execute('''CREATE TABLE paper_portfolios (
                name text PRIMARY KEY,last_mark_at timestamptz,last_ruonia float8,
                initial_nav_rub float8,realized_pnl_rub float8,fees_rub float8,funding_rub float8)''')
            c.execute('''CREATE TABLE paper_positions (
                portfolio_name text,asset text,direction text,units float8,avg_entry_price float8,
                last_price float8,stop_price float8,opened_at timestamptz,active_trade_id text,
                payload jsonb,updated_at timestamptz,PRIMARY KEY(portfolio_name,asset))''')
            c.execute('''CREATE TABLE paper_trades (
                trade_id text PRIMARY KEY,portfolio_name text,status text,opened_at timestamptz,
                gross_pnl_rub float8,fees_rub float8,funding_rub float8,payload jsonb)''')
        self.addCleanup(self.drop_schema)

    def verify_database(self, c):
        if c.execute('SELECT current_database() AS name').fetchone()['name'] != 'veritas_quality_test':
            raise RuntimeError('Refusing integration writes outside veritas_quality_test')

    @contextmanager
    def connect(self):
        with self.driver.connect(DSN, row_factory=self.row_factory) as c:
            self.verify_database(c)
            c.execute(self.sql.SQL('SET search_path TO {}').format(self.sql.Identifier(self.schema)))
            yield c

    def drop_schema(self):
        with self.driver.connect(DSN, row_factory=self.row_factory) as c:
            self.verify_database(c)
            c.execute(self.sql.SQL('DROP SCHEMA {} CASCADE').format(self.sql.Identifier(self.schema)))

    def seed(self, payloads, direction):
        with self.connect() as c:
            c.execute('TRUNCATE paper_positions,paper_trades,paper_portfolios')
            c.execute("INSERT INTO paper_portfolios VALUES ('Champion',%s,16,1000000,17,13,7)",
                      (self.now-timedelta(hours=1),))
            for i, payload in enumerate(payloads):
                asset = ('ETH', 'BTC', 'CNYRUBF')[i] if i < 3 else 'OWNED_'+str(i)
                raw = None if payload is SQL_NULL else json.dumps(payload)
                opened = self.now-timedelta(days=3)
                c.execute('''INSERT INTO paper_positions VALUES
                    ('Champion',%s,%s,1000,100,%s,%s,%s,%s,%s::jsonb,%s)''',
                    (asset, direction, 104. if direction == 'LONG' else 96.,
                     95. if direction == 'LONG' else 105., opened, 'trade-'+str(i), raw, opened))
                c.execute("""INSERT INTO paper_trades VALUES
                    (%s,'Champion','OPEN',%s,17,13,7,%s::jsonb)""",
                    ('trade-'+str(i), opened, raw))

    def snapshot(self):
        with self.connect() as c:
            return {table: c.execute('SELECT * FROM '+table+' ORDER BY 1,2').fetchall()
                    for table in ('paper_positions', 'paper_trades', 'paper_portfolios')}

    def run_case(self, kind, payloads, direction, legacy, predicate=None):
        self.seed(payloads, direction)
        before = self.snapshot()
        assets = [z['asset'] for z in before['paper_positions']]
        price = 104. if direction == 'LONG' else 96.
        prices = {asset: price for asset in assets}
        candidates = {asset: {'asset': asset, 'horizon': '1h',
            'trade_plan': {'multi_tf_levels': {'timeframes': {'1h': {
                'recent_support': 101., 'recent_resistance': 99.}}}}}
            for asset in assets}
        forwarded = []
        def next_step(c, name, policy, book, marks, *args):
            forwarded.append((deepcopy(book), deepcopy(marks)))
            return 'delegated'
        with self.connect() as c:
            trace = ReadTrace(c, legacy, predicate)
            try:
                if kind == 'PI':
                    with patch.dict(RT._v90pi_step_one.__globals__,
                                    {'_v90pi_base_step_one': next_step}):
                        result = RT._v90pi_step_one(trace, 'Champion', {}, candidates, prices,
                                                   16., 80., self.now, .0004, [])
                else:
                    # Exercise the public R65 wrapper and its actual captured R17.
                    result = RT._v90tr_apply(trace, 'Champion', candidates, prices, self.now)
                error = None
            except (TypeError, ValueError, AttributeError) as exc:
                result, error = None, (type(exc).__name__, str(exc))
        return before, self.snapshot(), result, error, forwarded, trace

    def assert_parity(self, kind, payloads, direction='LONG'):
        old = self.run_case(kind, payloads, direction, True)
        new = self.run_case(kind, payloads, direction, False)
        reference = self.run_case(kind, payloads, direction, False, REFERENCE_PREDICATE)
        self.assertEqual(new[:5], old[:5])
        self.assertEqual(reference[:5], old[:5])
        self.assertEqual(new[5].writes, old[5].writes)
        self.assertEqual(reference[5].writes, old[5].writes)
        self.assertEqual(reference[5].reads, new[5].reads)
        # These helpers may tighten stops; the booked ledger remains untouched.
        self.assertEqual(new[0]['paper_portfolios'], new[1]['paper_portfolios'])
        for before, after in zip(new[0]['paper_trades'], new[1]['paper_trades']):
            self.assertEqual({k: v for k, v in before.items() if k != 'payload'},
                             {k: v for k, v in after.items() if k != 'payload'})
        return old, new

    def payload(self, tag=SQL_NULL):
        data = {'mfe_pct': 1.5, 'data_integrity_status': 'OK',
                'price_source_lock': {'key': 'immutable-provider', 'contract': {'uid': 'original'}},
                'entry_event_snapshot': {'event_id': 'original', 'sealed_witness': 'bar|'*10000},
                'source_locked_mark': {'price': 100, 'observed_at': 'original-clock'},
                'trailing_history': [{'rule': 'previous-confirmed-swing'}]}
        if tag is not SQL_NULL:
            data['structural_policy_version'] = tag
        return data

    def test_sql_omits_only_nonempty_string_tags_in_object_payloads(self):
        cases = [('sql_null', SQL_NULL), ('json_null', None), ('missing', {}),
                 ('empty', {'structural_policy_version': ''}),
                 ('valid', {'structural_policy_version': 'CTC_INTRABAR_STRUCTURE_V1'}),
                 ('unknown', {'structural_policy_version': 'unknown-policy'}),
                 ('space', {'structural_policy_version': ' '}),
                 ('null_tag', {'structural_policy_version': None}),
                 ('false_tag', {'structural_policy_version': False}),
                 ('true_tag', {'structural_policy_version': True}),
                 ('zero_tag', {'structural_policy_version': 0}),
                 ('one_tag', {'structural_policy_version': 1}),
                 ('list_tag', {'structural_policy_version': ['v']}),
                 ('object_tag', {'structural_policy_version': {'version': 'v'}}),
                 ('list', []), ('list_pairs', [['structural_policy_version', 'v']]),
                 ('array_object', [{'structural_policy_version': 'v'}]),
                 ('nested', {'other': {'structural_policy_version': 'v'}}),
                 ('number', 1), ('boolean', True), ('malformed', 'not-json'),
                 ('encoded', json.dumps({'structural_policy_version': 'v'}))]
        params = tuple(value for label, data in cases
                       for value in (label, None if data is SQL_NULL else json.dumps(data)))
        for predicate in (TM.LEGACY_POSITION_SQL_PREDICATE, REFERENCE_PREDICATE):
            sql = ('WITH samples(label,payload) AS (VALUES '+
                   ','.join(['(%s,%s::jsonb)']*len(cases))+') SELECT label FROM samples WHERE '+predicate)
            with self.connect() as c:
                selected = {z['label'] for z in c.execute(sql, params).fetchall()}
            omitted = {label for label, _ in cases}-selected
            self.assertEqual(omitted, {'valid', 'unknown', 'space'})
            for label, data in cases:
                if label in omitted:
                    self.assertTrue(TM.owns_position({'payload': data}))

    def test_owned_book_reduces_transferred_rows_without_changing_any_result(self):
        payloads = [self.payload('CTC_INTRABAR_STRUCTURE_V1') for _ in range(23)]
        for kind in ('PI', 'R65_R17'):
            with self.subTest(kind=kind):
                old, new = self.assert_parity(kind, payloads)
                old_read = next(x for x in old[5].reads if x['filtered'])
                new_read = next(x for x in new[5].reads if x['filtered'])
                self.assertEqual(len(old_read['ids']), 23)
                self.assertEqual(new_read['ids'], [])
                self.assertLess(new_read['encoded_bytes'], old_read['encoded_bytes']/100)
                self.assertEqual(new[0], new[1])
                self.assertFalse(new[5].writes)
                # PI source/discontinuity prepass and R65 candidate removal still read all rows.
                self.assertEqual(len(new[5].reads[0]['ids']), 23)

    def test_mixed_owned_missing_and_null_tags_keep_real_long_short_actions(self):
        payloads = [self.payload('CTC_INTRABAR_STRUCTURE_V1'), self.payload(), self.payload(None)]
        for kind in ('PI', 'R65_R17'):
            for direction in ('LONG', 'SHORT'):
                with self.subTest(kind=kind, direction=direction):
                    old, new = self.assert_parity(kind, payloads, direction)
                    self.assertIsNone(new[3])
                    self.assertTrue(new[5].writes)
                    self.assertEqual(set(next(x for x in new[5].reads if x['filtered'])['ids']),
                                     {'trade-1', 'trade-2'})
                    for before, after in zip(new[0]['paper_positions'], new[1]['paper_positions']):
                        if before['active_trade_id'] == 'trade-0':
                            self.assertEqual(before, after)
                        else:
                            self.assertNotEqual(before['stop_price'], after['stop_price'])
                        for key in ('price_source_lock', 'source_locked_mark', 'entry_event_snapshot'):
                            self.assertEqual(before['payload'][key], after['payload'][key])

    def test_unusual_json_retains_original_action_or_exception_semantics(self):
        payloads = [SQL_NULL, None, [], False, 0, 'not-json', '[]', 'null',
                    json.dumps(self.payload('encoded-policy')),
                    self.payload(''), self.payload(False), self.payload(True),
                    self.payload(0), self.payload(1), self.payload([]), self.payload({})]
        for kind in ('PI', 'R65_R17'):
            for i, data in enumerate(payloads):
                with self.subTest(kind=kind, case=i):
                    old, new = self.assert_parity(kind, [data])
                    self.assertEqual(next(x for x in new[5].reads if x['filtered'])['ids'],
                                     ['trade-0'])


if __name__ == '__main__':
    unittest.main()
