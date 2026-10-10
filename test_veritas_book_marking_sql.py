"""Native mark/NAV parity on an explicitly isolated PostgreSQL database.

Every amount, date, identifier and retained proof is synthetic. The actual
engine helpers are loaded by AST in test_veritas_book_marking; no application
service, provider or production database is contacted.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import os
import unittest
from unittest.mock import patch
import uuid

from test_veritas_book_marking import G, H, M, NOW, PORTFOLIO, legacy_mark, position, quote


DSN = os.getenv('VERITAS_QUALITY_TEST_DSN', '')
SQL_NULL = object()


class TracedConnection:
    def __init__(self, connection):
        self.connection, self.statements = connection, []

    def transaction(self):
        return self.connection.transaction()

    def execute(self, sql, args=()):
        self.statements.append((sql, args))
        return self.connection.execute(sql, args)


@unittest.skipUnless(DSN, 'isolated PostgreSQL test database not configured')
class BookMarkingSQLTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        self.driver, self.sql, self.row_factory = psycopg, sql, dict_row
        self.schema = 'book_marking_'+uuid.uuid4().hex
        for cache in (G._quotes, G._source_quotes, G._market_state):
            self.enterContext(patch.dict(cache, {}, clear=True))
        clock = self.enterContext(patch.object(G, 'datetime', wraps=datetime))
        clock.now.return_value = NOW
        with self.driver.connect(DSN, row_factory=dict_row) as c:
            self.verify_database(c)
            c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
            self.addCleanup(self.drop_schema)
            c.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
            c.execute('''CREATE TABLE paper_positions (
                portfolio_name text, asset text, direction text, units float8,
                avg_entry_price float8, last_price float8, stop_price float8,
                opened_at timestamptz, active_trade_id text, payload jsonb,
                updated_at timestamptz, target_fraction float8, immutable_note text,
                PRIMARY KEY(portfolio_name,asset))''')
            c.execute('''CREATE TABLE paper_portfolios (
                name text PRIMARY KEY, initial_nav_rub float8,
                realized_pnl_rub float8, fees_rub float8, funding_rub float8,
                payload jsonb)''')
            c.execute('''CREATE TABLE paper_trades (
                trade_id text PRIMARY KEY, portfolio_name text, asset text,
                gross_pnl_rub float8, fees_rub float8, funding_rub float8,
                net_pnl_rub float8, status text, payload jsonb)''')

    def verify_database(self, c):
        if c.execute('SELECT current_database() AS name').fetchone()['name'] != 'veritas_quality_test':
            raise RuntimeError('Refusing integration writes outside veritas_quality_test')

    @contextmanager
    def connect(self):
        with self.driver.connect(DSN, row_factory=self.row_factory,
                                 autocommit=True, connect_timeout=5) as c:
            self.verify_database(c)
            c.execute(self.sql.SQL('SET search_path TO {}').format(self.sql.Identifier(self.schema)))
            c.execute("SET statement_timeout = '10s'")
            yield c

    def drop_schema(self):
        with self.driver.connect(DSN, row_factory=self.row_factory) as c:
            self.verify_database(c)
            c.execute(self.sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(self.sql.Identifier(self.schema)))

    def seed(self, rows):
        with self.connect() as c:
            for row in rows:
                encoded = json.dumps(row['payload'], ensure_ascii=False, default=str)
                c.execute('''INSERT INTO paper_portfolios VALUES
                    (%s,4103,19,7,3,'{"synthetic":"retained-ledger"}'::jsonb)
                    ON CONFLICT(name) DO NOTHING''', (row['portfolio_name'],))
                keys = ('portfolio_name','asset','direction','units','avg_entry_price',
                        'last_price','stop_price','opened_at','active_trade_id')
                c.execute('''INSERT INTO paper_positions VALUES
                    (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)''',
                    tuple(row[key] for key in keys)+(encoded,row['updated_at'],.25,'retained-synthetic-note'))
                c.execute('''INSERT INTO paper_trades VALUES
                    (%s,%s,%s,19,7,3,9,'OPEN',%s::jsonb)''',
                    (row['active_trade_id'],row['portfolio_name'],row['asset'],encoded))

    def contents(self, c):
        return {
            'positions': c.execute('''SELECT *,payload IS NULL AS payload_sql_null,
                jsonb_typeof(payload) AS payload_json_type
                FROM paper_positions ORDER BY portfolio_name,asset''').fetchall(),
            'portfolios': c.execute('SELECT * FROM paper_portfolios ORDER BY name').fetchall(),
            'trades': c.execute('SELECT * FROM paper_trades ORDER BY trade_id').fetchall(),
        }

    def test_native_delta_matches_full_marker_and_preserves_complete_evidence_and_scope(self):
        rows = [position('BTC'), dict(position('ETH'), direction='SHORT')]
        for row in rows:
            row['payload']['entry_event_snapshot'] = {
                'event_id':'SYNTHETIC-RETAINED-'+row['asset'],
                'atr_proof':{'bars':[{'high':143.,'low':101.,'available_at':NOW.isoformat()}]*40},
                'causal_evidence':{'opaque':'SYNTHETIC-FROZEN-PROOF','nested':[None,{'preserve':True}]}}
        foreign = dict(position('BTC'), portfolio_name=PORTFOLIO+'-FOREIGN',
                       active_trade_id='SYNTHETIC-FOREIGN-BTC')
        self.seed(rows+[foreign])
        G._quotes.update(BTC=quote('BTC',137.), ETH=quote('ETH',149.))
        prices = {'BTC':9999., 'ETH':9999.}
        with self.connect() as c, G.book_transaction(c):
            before = self.contents(c)
            full = c.execute('SELECT * FROM paper_positions WHERE portfolio_name=%s', (PORTFOLIO,)).fetchall()
            projected = c.execute(M.MARK_SQL+' WHERE portfolio_name=%s', (PORTFOLIO,)).fetchall()
            self.assertLess(len(json.dumps(projected,default=str)), len(json.dumps(full,default=str))/20)
            c.execute('SAVEPOINT reference_mark')
            self.assertEqual(legacy_mark(c, PORTFOLIO, prices, NOW), 2)
            expected = self.contents(c)
            c.execute('ROLLBACK TO SAVEPOINT reference_mark')
            trace = TracedConnection(c)
            self.assertEqual(H._v90j_mark_open_positions(trace, PORTFOLIO, prices, NOW), 2)
            actual = self.contents(c)
            self.assertEqual(actual, expected)
            self.assertEqual(actual['portfolios'], before['portfolios'])
            self.assertEqual(actual['trades'], before['trades'])
            changed_payload_keys = {'last_mark_price','last_mark_at','price_source_lock',
                                    'price_source_status','source_locked_mark'}
            for original, saved in zip(before['positions'], actual['positions']):
                if original['portfolio_name'] != PORTFOLIO:
                    self.assertEqual(saved, original)
                    continue
                for key, value in original.items():
                    if key not in ('last_price','updated_at','payload'):
                        self.assertEqual(saved[key], value, key)
                for key, value in original['payload'].items():
                    if key not in changed_payload_keys:
                        self.assertEqual(saved['payload'][key], value, key)
                self.assertEqual(saved['last_price'], G._quotes[saved['asset']]['price'])
            updates = [(sql,args) for sql,args in trace.statements
                       if sql.startswith('WITH delta AS')]
            self.assertEqual(len(updates), 1)
            batch=json.loads(updates[0][1][0])
            self.assertEqual(len(batch),2)
            self.assertTrue(all(not row['replace_payload'] for row in batch))
            self.assertTrue(all(len(json.dumps(row['patch']).encode('utf-8')) < 1000
                                for row in batch))

    def test_native_scalar_json_string_and_null_mark_semantics_match_frozen_baseline(self):
        self.seed([position()])
        G._quotes['BTC'] = quote()
        cases = (SQL_NULL, None, {}, [], [1], 0, 7, False, True, 'null', '[]', '7',
                 '{"entry_primary_source":"Binance spot","opaque":"SYNTHETIC-RETAIN"}',
                 {'price_source_lock':'malformed'}, {'source_locked_mark':None})
        with self.connect() as c, G.book_transaction(c):
            for index, value in enumerate(cases):
                with self.subTest(case=index):
                    encoded = None if value is SQL_NULL else json.dumps(value)
                    c.execute('UPDATE paper_positions SET payload=%s::jsonb', (encoded,))
                    c.execute('SAVEPOINT shape_reference')
                    expected_count = legacy_mark(c, PORTFOLIO, {'BTC':1.}, NOW)
                    expected = self.contents(c)
                    c.execute('ROLLBACK TO SAVEPOINT shape_reference')
                    count = H._v90j_mark_open_positions(c, PORTFOLIO, {'BTC':1.}, NOW)
                    self.assertEqual(count, expected_count)
                    self.assertEqual(self.contents(c), expected)
                    c.execute('ROLLBACK TO SAVEPOINT shape_reference')
                    c.execute('RELEASE SAVEPOINT shape_reference')

    def test_native_mark_only_reads_new_ledger_costs_and_units_after_mutations(self):
        self.seed([position()])
        with self.connect() as c, G.book_transaction(c):
            before = H._portfolio_rows(c, PORTFOLIO, mark_only=True)
            before_nav = H._mark_nav(*before, {'BTC':9999.})
            c.execute('''UPDATE paper_portfolios SET realized_pnl_rub=47,fees_rub=23,funding_rub=17
                         WHERE name=%s''', (PORTFOLIO,))
            c.execute('''UPDATE paper_trades SET gross_pnl_rub=47,fees_rub=23,funding_rub=17,net_pnl_rub=7
                         WHERE portfolio_name=%s''', (PORTFOLIO,))
            c.execute('UPDATE paper_positions SET units=1.25 WHERE portfolio_name=%s', (PORTFOLIO,))
            after = H._portfolio_rows(c, PORTFOLIO, mark_only=True)
            full = H._portfolio_rows(c, PORTFOLIO)
            self.assertEqual(after[0], full[0])
            self.assertEqual((after[0]['realized_pnl_rub'],after[0]['fees_rub'],after[0]['funding_rub']), (47.,23.,17.))
            self.assertEqual(after[1][0]['units'], 1.25)
            self.assertNotIn('retained_history', after[1][0]['payload'])
            self.assertIn('retained_history', full[1][0]['payload'])
            projected_nav = H._mark_nav(*after, {'BTC':9999.})
            self.assertEqual(projected_nav, H._mark_nav(*full, {'BTC':9999.}))
            self.assertEqual(projected_nav[1], 5.)  # 1.25 * (source-locked 131 - entry 127)
            self.assertEqual(projected_nav[0], 4103.+47.-23.-17.+5.)
            self.assertNotEqual(projected_nav, before_nav)

    def test_native_failed_mark_update_rolls_back_portfolio_and_positions_together(self):
        self.seed([position('BTC'), position('ETH')])
        G._quotes.update(BTC=quote('BTC',137.), ETH=quote('ETH',149.))
        with self.connect() as c:
            c.execute("ALTER TABLE paper_positions ADD CONSTRAINT synthetic_mark_failure CHECK (last_price<>149)")
            before = self.contents(c)
            with self.assertRaises(self.driver.errors.CheckViolation):
                with G.book_transaction(c):
                    c.execute('UPDATE paper_portfolios SET fees_rub=fees_rub+13 WHERE name=%s', (PORTFOLIO,))
                    c.execute('UPDATE paper_trades SET fees_rub=fees_rub+13 WHERE portfolio_name=%s', (PORTFOLIO,))
                    H._v90j_mark_open_positions(c, PORTFOLIO, {'BTC':1.,'ETH':1.}, NOW)
            self.assertEqual(self.contents(c), before)

    def test_native_unverified_or_stale_quote_keeps_every_saved_field(self):
        self.seed([position()])
        cases = ({}, dict(quote(),source_gate_pass=False),
                 dict(quote(),observed_at=(NOW-timedelta(hours=1)).isoformat()),
                 dict(quote(),source_names={'primary':'UNRELATED-SYNTHETIC-SOURCE'}))
        with self.connect() as c:
            before = self.contents(c)
            for candidate in cases:
                with self.subTest(candidate=candidate), patch.dict(G._quotes, {'BTC':candidate}, clear=True):
                    trace = TracedConnection(c)
                    with G.book_transaction(trace):
                        self.assertEqual(H._v90j_mark_open_positions(trace, PORTFOLIO, {'BTC':9999.}, NOW), 0)
                    self.assertFalse(any(sql.startswith('UPDATE') or sql.startswith('WITH delta AS')
                                         for sql,args in trace.statements))
                    self.assertEqual(self.contents(c), before)


if __name__ == '__main__':
    unittest.main()
