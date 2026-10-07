"""Bounded portfolio initialization: current, fresh, legacy, and busy schemas.

SQL writes are restricted to the explicitly named disposable CI database.
These fixtures never call portfolio execution, a broker, or the application main.
"""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
import time
import unittest
from unittest.mock import MagicMock, patch
import uuid

import veritas_portfolio as P
import veritas_startup_guard as G


DSN = os.getenv('VERITAS_QUALITY_TEST_DSN', '')


def catalog_rows():
    return [{'table_name': name, 'columns': sorted(columns),
             'indexes': sorted(P.PORTFOLIO_SCHEMA_INDEXES.get(name, ()))}
            for name, columns in P.PORTFOLIO_SCHEMA_COLUMNS.items()]


class PortfolioSchemaContractTests(unittest.TestCase):
    def connection(self, rows=None, marker=True):
        c = MagicMock()
        c.__enter__.return_value = c
        def execute(sql, params=None):
            cur = MagicMock()
            if 'pg_catalog.pg_class t' in sql:
                cur.fetchall.return_value = catalog_rows() if rows is None else rows
            elif 'SELECT 1 AS ok FROM v90_migration_state' in sql:
                cur.fetchone.return_value = {'ok': 1} if marker else None
            return cur
        c.execute.side_effect = execute
        return c

    def test_current_schema_uses_no_ddl_or_migration_and_bounds_its_transaction(self):
        c = self.connection()
        with patch.object(P, '_create_portfolio_schema') as create, \
                patch.object(P, '_v90_migrate_portfolio_data') as migrate:
            P.ensure_schema(lambda: c)
        create.assert_not_called()
        migrate.assert_not_called()
        c.transaction.assert_called_once_with()
        sql = [call.args[0] for call in c.execute.call_args_list]
        self.assertEqual(c.execute.call_args_list[0].args[1], ('2000ms', '8000ms'))
        self.assertFalse(any('CREATE ' in q or 'ALTER ' in q for q in sql))
        policy_queries = [q for q in sql if 'INSERT INTO paper_portfolios' in q]
        self.assertEqual(len(policy_queries), 5)
        self.assertTrue(all('IS DISTINCT FROM' in q for q in policy_queries))

    def test_missing_column_table_or_valid_index_cannot_be_declared_current(self):
        original = catalog_rows()
        variants = [original[:-1]]
        for field, value in [('columns', 'client_order_id'),
                             ('indexes', 'idx_paper_orders_client_order_id')]:
            rows = deepcopy(original)
            next(r for r in rows if r['table_name'] == 'paper_orders')[field].remove(value)
            variants.append(rows)
        for rows in variants:
            with self.subTest(rows=rows):
                self.assertFalse(G.schema_matches(self.connection(rows),
                    P.PORTFOLIO_SCHEMA_COLUMNS, P.PORTFOLIO_SCHEMA_INDEXES))
        sql = self.connection()
        G.schema_matches(sql, P.PORTFOLIO_SCHEMA_COLUMNS, P.PORTFOLIO_SCHEMA_INDEXES)
        self.assertIn('x.indisvalid AND x.indisready', sql.execute.call_args.args[0])

    def test_missing_marker_runs_legacy_migration_without_repeating_order_ddl(self):
        c = self.connection(marker=False)
        with patch.object(P, '_create_portfolio_schema') as create, \
                patch.object(P, '_v90_migrate_portfolio_data') as migrate:
            P.ensure_schema(lambda: c)
        create.assert_not_called()
        migrate.assert_called_once_with(c)

    def test_busy_or_incomplete_initialization_raises_and_cannot_seed_policies(self):
        for error in (TimeoutError('busy schema'), None):
            c = self.connection(rows=[])
            with self.subTest(error=type(error).__name__), \
                    patch.object(P, '_create_portfolio_schema', side_effect=error):
                with self.assertRaises(TimeoutError if error else RuntimeError):
                    P.ensure_schema(lambda: c)
            self.assertFalse(any('INSERT INTO paper_portfolios' in call.args[0]
                                 for call in c.execute.call_args_list))
            self.assertIsNotNone(c.transaction.return_value.__exit__.call_args.args[0])


class QueryTrace:
    def __init__(self, connection, queries):
        self.connection, self.queries = connection, queries

    def execute(self, sql, params=None):
        self.queries.append(str(sql))
        return self.connection.execute(sql, params)

    def transaction(self):
        return self.connection.transaction()


@unittest.skipUnless(DSN, 'isolated PostgreSQL test database not configured')
class PortfolioSchemaSQLTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        self.driver, self.sql, self.row_factory = psycopg, sql, dict_row
        self.schema = 'portfolio_schema_test_' + uuid.uuid4().hex
        self.queries = []
        with self.driver.connect(DSN, autocommit=True, row_factory=self.row_factory) as c:
            self.verify_database(c)
            c.execute(self.sql.SQL('CREATE SCHEMA {}').format(self.sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)

    def verify_database(self, c):
        if c.execute('SELECT current_database() AS name').fetchone()['name'] != 'veritas_quality_test':
            raise RuntimeError('Refusing integration writes outside veritas_quality_test')

    @contextmanager
    def connect_schema(self, schema):
        with self.driver.connect(DSN, autocommit=True, row_factory=self.row_factory) as c:
            self.verify_database(c)
            c.execute(self.sql.SQL('SET search_path TO {}').format(self.sql.Identifier(schema)))
            yield QueryTrace(c, self.queries)

    def connect(self):
        return self.connect_schema(self.schema)

    def drop_schema(self):
        with self.driver.connect(DSN, autocommit=True, row_factory=self.row_factory) as c:
            self.verify_database(c)
            c.execute(self.sql.SQL('DROP SCHEMA {} CASCADE').format(self.sql.Identifier(self.schema)))

    def financials(self, connect=None):
        with (connect or self.connect)() as c:
            return {
                'books': c.execute('''SELECT name,initial_nav_rub,realized_pnl_rub,fees_rub,funding_rub,
                    benchmark_nav_rub,high_water_nav_rub,last_mark_at FROM paper_portfolios ORDER BY name''').fetchall(),
                'positions': c.execute('SELECT * FROM paper_positions ORDER BY portfolio_name,asset').fetchall(),
                'trades': c.execute('SELECT * FROM paper_trades ORDER BY trade_id').fetchall(),
                'orders': c.execute('SELECT order_id,portfolio_name,trade_id,price,notional_rub,fee_rub,payload FROM paper_orders ORDER BY order_id').fetchall(),
            }

    def seed_active_book(self, name='Currency'):
        with self.connect() as c:
            c.execute('''UPDATE paper_portfolios SET initial_nav_rub=12345,realized_pnl_rub=150,
                fees_rub=5,funding_rub=2,benchmark_nav_rub=12350,high_water_nav_rub=12500,
                policy='{}',model_version='OLD',last_mark_at='2026-10-07T10:00:00Z' WHERE name=%s''', (name,))
            c.execute('''INSERT INTO paper_positions VALUES
                (%s,'CNYRUBF','SHORT',783.9,12.756,'2026-10-07T09:40:00Z','2026-10-07T10:00:00Z',
                 'original-trade',12.9,1.,12.74,'{"original":"position evidence"}')''', (name,))
            c.execute('''INSERT INTO paper_trades(trade_id,portfolio_name,asset,direction,opened_at,
                avg_entry_price,status,payload) VALUES ('original-trade',%s,'CNYRUBF','SHORT',
                '2026-10-07T09:40:00Z',12.756,'OPEN','{"original":"trade evidence"}')''', (name,))
            c.execute('''INSERT INTO paper_orders(portfolio_name,trade_id,created_at,asset,side,price,
                notional_rub,fee_rub,fraction_nav,payload) VALUES (%s,'original-trade',
                '2026-10-07T09:40:00Z','CNYRUBF','SELL_SHORT',12.756,9999.4,4.,1.,'{"original":"order evidence"}')''', (name,))

    def test_fresh_database_creates_complete_schema_all_five_books_and_migration_marker(self):
        @contextmanager
        def checked_connect():
            with self.connect() as c:
                yield c
                self.assertEqual(c.execute('SHOW lock_timeout').fetchone()['lock_timeout'], '0')
                self.assertEqual(c.execute('SHOW statement_timeout').fetchone()['statement_timeout'], '0')
        P.ensure_schema(checked_connect)
        with self.connect() as c:
            self.assertTrue(G.schema_matches(c, P.PORTFOLIO_SCHEMA_COLUMNS, P.PORTFOLIO_SCHEMA_INDEXES))
            books = c.execute('SELECT * FROM paper_portfolios ORDER BY name').fetchall()
            self.assertEqual({b['name'] for b in books}, set(P.POLICIES))
            currency = next(b for b in books if b['name'] == 'Currency')
            self.assertEqual(currency['initial_nav_rub'], 10000)
            self.assertEqual(currency['realized_pnl_rub'], 0)
            marker = c.execute('SELECT * FROM v90_migration_state WHERE key=%s',
                               (P.PORTFOLIO_MIGRATION_MARKER,)).fetchone()
            self.assertIsNotNone(marker)
            self.assertTrue(all(n == 0 for n in marker['details']['copied'].values()))

    def test_current_schema_survives_order_reader_and_preserves_all_financials(self):
        P.ensure_schema(self.connect)
        self.seed_active_book()
        before = self.financials()
        with self.connect() as reader, reader.transaction():
            reader.execute('LOCK TABLE paper_orders IN ACCESS SHARE MODE')
            self.queries.clear()
            P.ensure_schema(self.connect)
            self.assertFalse(any('CREATE ' in q or 'ALTER ' in q for q in self.queries))
        self.assertEqual(self.financials(), before)
        with self.connect() as c:
            first = c.execute('SELECT name,updated_at,policy,model_version FROM paper_portfolios ORDER BY name').fetchall()
        P.ensure_schema(self.connect)
        with self.connect() as c:
            second = c.execute('SELECT name,updated_at,policy,model_version FROM paper_portfolios ORDER BY name').fetchall()
        self.assertEqual(first, second)
        self.assertEqual(next(b for b in first if b['name'] == 'Currency')['policy'],
                         json.loads(json.dumps(P.POLICIES['Currency'])))

    def test_missing_client_id_is_migrated_without_changing_existing_book_or_fills(self):
        P.ensure_schema(self.connect)
        self.seed_active_book()
        before = self.financials()
        with self.connect() as c:
            c.execute('ALTER TABLE paper_orders DROP COLUMN client_order_id')
        P.ensure_schema(self.connect)
        self.assertEqual(self.financials(), before)
        with self.connect() as c:
            self.assertTrue(G.schema_matches(c, P.PORTFOLIO_SCHEMA_COLUMNS, P.PORTFOLIO_SCHEMA_INDEXES))

    def test_busy_migration_times_out_rolls_back_and_does_not_cancel_the_reader(self):
        P.ensure_schema(self.connect)
        with self.connect() as c:
            c.execute('ALTER TABLE paper_orders DROP COLUMN client_order_id')
        before = self.financials()
        with self.connect() as reader, reader.transaction():
            reader.execute('LOCK TABLE paper_orders IN ACCESS SHARE MODE')
            start = time.monotonic()
            with patch.object(P, 'SCHEMA_LOCK_TIMEOUT_MS', 150), \
                    patch.object(P, 'SCHEMA_STATEMENT_TIMEOUT_MS', 1000):
                with self.assertRaises(self.driver.errors.LockNotAvailable):
                    P.ensure_schema(self.connect)
            self.assertLess(time.monotonic() - start, 3)
            self.assertEqual(reader.execute('SELECT 1 AS alive').fetchone()['alive'], 1)
        self.assertEqual(self.financials(), before)
        with self.connect() as c:
            self.assertFalse(G.schema_matches(c, P.PORTFOLIO_SCHEMA_COLUMNS, P.PORTFOLIO_SCHEMA_INDEXES))
        P.ensure_schema(self.connect)
        with self.connect() as c:
            self.assertTrue(G.schema_matches(c, P.PORTFOLIO_SCHEMA_COLUMNS, P.PORTFOLIO_SCHEMA_INDEXES))

    def test_only_an_untouched_zero_capital_placeholder_can_be_rebased(self):
        P.ensure_schema(self.connect)
        with self.connect() as c:
            c.execute("UPDATE paper_portfolios SET initial_nav_rub=0,benchmark_nav_rub=0,high_water_nav_rub=0 WHERE name='Currency'")
        P.ensure_schema(self.connect)
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT initial_nav_rub FROM paper_portfolios WHERE name='Currency'").fetchone()['initial_nav_rub'], 10000)
        self.seed_active_book()
        with self.connect() as c:
            c.execute("UPDATE paper_portfolios SET initial_nav_rub=0,realized_pnl_rub=0,fees_rub=0,funding_rub=0 WHERE name='Currency'")
            c.execute('DELETE FROM paper_trades')
            c.execute('DELETE FROM paper_orders')
        before = self.financials()
        P.ensure_schema(self.connect)
        self.assertEqual(self.financials(), before)

    def test_public_legacy_migration_preserves_evidence_and_does_not_recopy_on_restart(self):
        P.ensure_schema(self.connect)
        self.seed_active_book('Champion')
        tables = ('paper_portfolios', 'paper_positions', 'paper_trades', 'paper_orders', 'paper_nav_history')
        with self.driver.connect(DSN, row_factory=self.row_factory) as c:
            self.verify_database(c)
            if c.execute("SELECT to_regnamespace('veritas_v90') AS schema").fetchone()['schema']:
                raise RuntimeError('Refusing to replace an existing migration fixture schema')
            for table in tables:
                if c.execute('SELECT to_regclass(%s) AS table_name', ('public.'+table,)).fetchone()['table_name']:
                    raise RuntimeError('Refusing to replace an existing legacy fixture table')
            c.execute('CREATE SCHEMA veritas_v90')
            for table in tables:
                c.execute(self.sql.SQL('CREATE TABLE public.{} (LIKE {}.{} INCLUDING ALL)').format(
                    self.sql.Identifier(table), self.sql.Identifier(self.schema), self.sql.Identifier(table)))
                c.execute(self.sql.SQL('INSERT INTO public.{} SELECT * FROM {}.{}').format(
                    self.sql.Identifier(table), self.sql.Identifier(self.schema), self.sql.Identifier(table)))
            c.execute("UPDATE public.paper_portfolios SET initial_nav_rub=777 WHERE name='Currency'")
        def cleanup():
            with self.driver.connect(DSN, row_factory=self.row_factory) as c:
                self.verify_database(c)
                c.execute('DROP SCHEMA veritas_v90 CASCADE')
                for table in reversed(tables):
                    c.execute(self.sql.SQL('DROP TABLE public.{}').format(self.sql.Identifier(table)))
        self.addCleanup(cleanup)
        connect_target = lambda: self.connect_schema('veritas_v90')
        P.ensure_schema(connect_target)
        copied = self.financials(connect_target)
        expected = self.financials()
        self.assertEqual(copied, expected)
        with connect_target() as c:
            details = c.execute('SELECT details FROM v90_migration_state WHERE key=%s',
                                (P.PORTFOLIO_MIGRATION_MARKER,)).fetchone()['details']
            self.assertEqual(details['copied']['paper_positions'], 1)
            self.assertEqual(details['copied']['paper_orders'], 1)
        with self.driver.connect(DSN) as c:
            c.execute("UPDATE public.paper_portfolios SET initial_nav_rub=999999 WHERE name='Champion'")
        P.ensure_schema(connect_target)
        self.assertEqual(self.financials(connect_target), copied)


if __name__ == '__main__':
    unittest.main()
