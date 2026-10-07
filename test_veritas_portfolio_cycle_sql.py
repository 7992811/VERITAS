"""Real PostgreSQL commits, rollback and protection handoff between paper books.

Only explicitly named CI databases are writable. Accounting writes below are
fixture markers, not another financial engine: NAV and protection use the
production functions, and the real advisory/transaction locks are not mocked.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
import threading
import unittest
from unittest.mock import patch
import uuid

from veritas_book_lock import PriorityRLock
import veritas_portfolio as VP
import veritas_portfolio_cycle as PC
import veritas_position_guard as G
import veritas_price_source as S


DSN = os.getenv('VERITAS_QUALITY_TEST_DSN', '')


@unittest.skipUnless(DSN, 'isolated PostgreSQL test database not configured')
class PortfolioCycleSQLTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        self.driver, self.sql, self.row_factory = psycopg, sql, dict_row
        self.schema = 'portfolio_cycle_test_'+uuid.uuid4().hex
        self.now = datetime(2026, 10, 7, 17, 0, tzinfo=timezone.utc)
        self.identity = S.identity('ETH', {'source_names': {'primary': 'Binance spot'}})
        self.mutex = PriorityRLock()
        self.enterContext(patch.object(G, '_mutex', self.mutex))
        self.enterContext(patch.dict(G._quotes, {}, clear=True))
        self.enterContext(patch.dict(G._source_quotes, {}, clear=True))
        with self.driver.connect(DSN, row_factory=self.row_factory) as c:
            self.verify_database(c)
            c.execute(self.sql.SQL('CREATE SCHEMA {}').format(self.sql.Identifier(self.schema)))
            c.execute(self.sql.SQL('SET search_path TO {}').format(self.sql.Identifier(self.schema)))
            c.execute('''CREATE TABLE paper_portfolios (
                name text PRIMARY KEY, initial_nav_rub float8 NOT NULL,
                realized_pnl_rub float8 NOT NULL DEFAULT 0,
                fees_rub float8 NOT NULL DEFAULT 0, funding_rub float8 NOT NULL DEFAULT 0,
                last_mark_at timestamptz NOT NULL, last_ruonia float8 NOT NULL DEFAULT 16)''')
            c.execute('''CREATE TABLE paper_positions (
                portfolio_name text,asset text,direction text,units float8,avg_entry_price float8,
                last_price float8,stop_price float8,opened_at timestamptz,active_trade_id text,
                payload jsonb NOT NULL DEFAULT '{}',PRIMARY KEY(portfolio_name,asset))''')
            c.execute('''CREATE TABLE paper_trades (
                trade_id text PRIMARY KEY,portfolio_name text,status text,opened_at timestamptz,
                gross_pnl_rub float8,fees_rub float8,funding_rub float8,
                payload jsonb NOT NULL DEFAULT '{}')''')
            c.execute('''CREATE TABLE paper_orders (
                client_order_id text PRIMARY KEY,portfolio_name text,trade_id text,
                side text,notional_rub float8)''')
            c.execute('''CREATE TABLE paper_nav_history (
                portfolio_name text,observed_at timestamptz,nav_rub float8,
                PRIMARY KEY(portfolio_name,observed_at))''')
        self.addCleanup(self.drop_schema)

    def verify_database(self, c):
        if c.execute('SELECT current_database() AS name').fetchone()['name'] != 'veritas_quality_test':
            raise RuntimeError('Refusing integration writes outside veritas_quality_test')

    @contextmanager
    def connect(self):
        # Match production: book_transaction, not implicit connection scope,
        # owns the accounting commit. SET/verification must not open a parent
        # transaction that would turn the production transaction into a savepoint.
        with self.driver.connect(DSN, row_factory=self.row_factory,
                                 autocommit=True, connect_timeout=5) as c:
            self.verify_database(c)
            c.execute(self.sql.SQL('SET search_path TO {}').format(self.sql.Identifier(self.schema)))
            c.execute("SET statement_timeout = '10s'")
            yield c

    def drop_schema(self):
        with self.driver.connect(DSN, row_factory=self.row_factory) as c:
            self.verify_database(c)
            c.execute(self.sql.SQL('DROP SCHEMA {} CASCADE').format(self.sql.Identifier(self.schema)))

    def seed(self, names):
        with self.connect() as c:
            for name in names:
                c.execute('INSERT INTO paper_portfolios(name,initial_nav_rub,last_mark_at) VALUES (%s,1000,%s)',
                          (name, self.now-timedelta(minutes=1)))

    def write_book(self, c, name, policy, book, prices, ruonia, usdrub, ts, commission, summary):
        self.assertTrue(c.autocommit)
        trade_id = name+'-trade'
        payload = {'price_source_lock': self.identity,
                   'source_locked_mark': {'identity': self.identity, 'price': 100., 'observed_at': ts}}
        c.execute('''UPDATE paper_portfolios SET realized_pnl_rub=realized_pnl_rub+7,
            fees_rub=fees_rub+2,funding_rub=funding_rub+3,last_mark_at=%s WHERE name=%s''', (ts, name))
        c.execute('''INSERT INTO paper_trades VALUES
            (%s,%s,'OPEN',%s,7,2,3,%s::jsonb)''', (trade_id, name, self.now-timedelta(hours=1), json.dumps(payload)))
        c.execute('''INSERT INTO paper_positions VALUES
            (%s,'ETH','LONG',1,100,100,90,%s,%s,%s::jsonb)''',
            (name, self.now-timedelta(hours=1), trade_id, json.dumps(payload)))
        c.execute("INSERT INTO paper_orders VALUES (%s,%s,%s,'BUY',100)", (name+'-order', name, trade_id))
        portfolio, positions = VP._portfolio_rows(c, name)
        nav, _, _, _ = VP._mark_nav(portfolio, positions, {})
        c.execute('INSERT INTO paper_nav_history VALUES (%s,%s,%s)', (name, ts, nav))
        return {'name': name, 'nav_rub': nav}

    def run_books(self, names, step=None, emit=None, execution_clock=None):
        return PC.run_books(pg_connect=self.connect, policies={name: {} for name in names},
            make_book=lambda name, policy: {}, step_one=step or self.write_book,
            prices={}, ruonia=16., usdrub=80., observed_at=self.now.isoformat(),
            commission_rate=.0005, summary=[], emit=emit, execution_clock=execution_clock)

    def test_failed_protection_rolls_back_every_write_and_preserves_neighbor_commits(self):
        names = ('Impulse', 'Aggressive', 'Champion')
        self.seed(names)
        with self.connect() as c:
            # Fail after real VPP.refresh already wrote the position's flags.
            # PostgreSQL must undo that update and all preceding accounting.
            c.execute('''CREATE FUNCTION fail_middle_protection() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.portfolio_name='Aggressive' AND NEW.payload ? 'net_profit_protection' THEN
                        RAISE EXCEPTION 'fixture protection write failure';
                    END IF;
                    RETURN NEW;
                END $$''')
            c.execute('''CREATE TRIGGER fail_middle_protection BEFORE UPDATE ON paper_trades
                         FOR EACH ROW EXECUTE FUNCTION fail_middle_protection()''')
        result = self.run_books(names)
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['committed_portfolios'], ['Impulse', 'Champion'])
        self.assertEqual(result['uncertain_portfolios'], [])
        self.assertEqual(result['errors']['Aggressive']['stage'], 'protection')
        self.assertIs(result['errors']['Aggressive']['accounting_committed'], False)
        self.assertEqual([x['status'] for x in result['timing']['portfolios']],
                         ['COMMITTED', 'ROLLED_BACK', 'COMMITTED'])
        rejected = result['portfolios'][1]
        self.assertEqual(rejected['status'], 'ERROR')
        self.assertNotIn('nav_rub', rejected)
        with self.connect() as c:
            accounts = {row['name']: dict(row) for row in c.execute('SELECT * FROM paper_portfolios').fetchall()}
            for name in ('Impulse', 'Champion'):
                self.assertEqual((accounts[name]['realized_pnl_rub'], accounts[name]['fees_rub'],
                                  accounts[name]['funding_rub']), (7., 2., 3.))
                self.assertEqual(accounts[name]['last_mark_at'], self.now)
            self.assertEqual((accounts['Aggressive']['realized_pnl_rub'], accounts['Aggressive']['fees_rub'],
                              accounts['Aggressive']['funding_rub']), (0., 0., 0.))
            self.assertEqual(accounts['Aggressive']['last_mark_at'], self.now-timedelta(minutes=1))
            for table in ('paper_positions', 'paper_trades', 'paper_orders', 'paper_nav_history'):
                rows = c.execute(self.sql.SQL('SELECT * FROM {} ORDER BY portfolio_name').format(
                    self.sql.Identifier(table))).fetchall()
                self.assertEqual([row['portfolio_name'] for row in rows], ['Champion', 'Impulse'], table)
                if table in ('paper_positions', 'paper_trades'):
                    self.assertTrue(all('net_profit_protection' in row['payload'] for row in rows), table)
            self.assertEqual([row['nav_rub'] for row in c.execute(
                'SELECT nav_rub FROM paper_nav_history ORDER BY portfolio_name').fetchall()], [1002., 1002.])

    def test_accounting_io_preserves_native_savepoints_outer_rollback_and_all_financial_tables(self):
        # Both runs use production run_books/book_transaction/VPP.refresh. The
        # baseline calls the original connection from its bound transaction
        # method; only the measured run sends accounting SQL through the proxy.
        table_order = {
            'paper_portfolios': ('name',),
            'paper_positions': ('portfolio_name', 'asset'),
            'paper_trades': ('trade_id',),
            'paper_orders': ('client_order_id',),
            'paper_nav_history': ('portfolio_name', 'observed_at'),
        }

        def all_financial_tables():
            with self.connect() as c:
                return {table: [dict(row) for row in c.execute(
                    self.sql.SQL('SELECT * FROM {} ORDER BY {}').format(
                        self.sql.Identifier(table),
                        self.sql.SQL(',').join(map(self.sql.Identifier, columns)))).fetchall()]
                    for table, columns in table_order.items()}

        names = ('Champion', 'Aggressive')
        outcomes = []
        for measured in (False, True):
            with self.subTest(measured=measured):
                with self.connect() as c:
                    c.execute(self.sql.SQL('TRUNCATE {}').format(
                        self.sql.SQL(',').join(map(self.sql.Identifier, table_order))))
                self.seed(names)
                before = all_financial_tables()
                recovered = []

                def step(wrapped, name, *args):
                    self.assertIsInstance(wrapped, PC.AIO.AccountingConnection)
                    raw = wrapped.transaction.__self__
                    self.assertIsInstance(raw, self.driver.Connection)
                    self.assertIs(raw.row_factory, self.row_factory)
                    c = wrapped if measured else raw
                    result = self.write_book(c, name, *args)
                    try:
                        # A real driver exception occurs after two writes in a
                        # native nested transaction. Its rollback must leave the
                        # outer transaction usable and both tables unchanged.
                        with c.transaction():
                            c.execute('''UPDATE paper_positions SET units=99,
                                payload=payload || '{"discarded_savepoint":true}'::jsonb
                                WHERE portfolio_name=%s''', (name,))
                            c.execute('UPDATE paper_trades SET fees_rub=999 WHERE portfolio_name=%s', (name,))
                            c.execute("INSERT INTO paper_orders VALUES (%s,%s,%s,'DUPLICATE_FIXTURE',0)",
                                      (name+'-order', name, name+'-trade'))
                    except self.driver.errors.UniqueViolation:
                        recovered.append(name)
                    else:
                        self.fail('native duplicate order did not fail the savepoint')

                    with c.cursor() as cursor:
                        self.assertIs(cursor.connection, raw)
                        cursor.execute('SELECT units,payload FROM paper_positions WHERE portfolio_name=%s', (name,))
                        positions = list(cursor)
                    self.assertEqual(len(positions), 1)
                    self.assertEqual(positions[0]['units'], 1.)
                    self.assertNotIn('discarded_savepoint', positions[0]['payload'])
                    self.assertEqual(c.execute(
                        'SELECT fees_rub FROM paper_trades WHERE portfolio_name=%s', (name,))
                        .fetchone()['fees_rub'], 2.)

                    # Commit a second native savepoint after the caught error;
                    # the next failure will roll this and the full book back.
                    with c.transaction():
                        c.execute('UPDATE paper_portfolios SET fees_rub=fees_rub+.5 WHERE name=%s', (name,))
                        c.execute('UPDATE paper_trades SET fees_rub=fees_rub+.5 WHERE portfolio_name=%s', (name,))
                        c.execute("INSERT INTO paper_orders VALUES (%s,%s,%s,'RECOVERY_FIXTURE',0)",
                                  (name+'-recovered', name, name+'-trade'))
                    portfolio, positions = VP._portfolio_rows(c, name)
                    nav = VP._mark_nav(portfolio, positions, {})[0]
                    c.execute('UPDATE paper_nav_history SET nav_rub=%s WHERE portfolio_name=%s', (nav, name))
                    result['nav_rub'] = nav
                    if name == 'Aggressive':
                        raise ValueError('fixture outer accounting failure')
                    return result

                result = self.run_books(names, step)
                after = all_financial_tables()
                self.assertEqual(recovered, list(names))
                self.assertEqual(result['status'], 'PARTIAL')
                self.assertEqual(result['committed_portfolios'], ['Champion'])
                self.assertEqual(result['uncertain_portfolios'], [])
                self.assertEqual(result['errors']['Aggressive']['stage'], 'accounting')
                self.assertIs(result['errors']['Aggressive']['accounting_committed'], False)
                self.assertEqual(result['portfolios'][0]['nav_rub'], 1001.5)
                failed_before = next(row for row in before['paper_portfolios'] if row['name'] == 'Aggressive')
                failed_after = next(row for row in after['paper_portfolios'] if row['name'] == 'Aggressive')
                self.assertEqual(failed_after, failed_before)
                for table in ('paper_positions', 'paper_trades', 'paper_orders', 'paper_nav_history'):
                    self.assertTrue(after[table], table)
                    self.assertTrue(all(row['portfolio_name'] == 'Champion' for row in after[table]), table)
                self.assertEqual(after['paper_trades'][0]['fees_rub'], 2.5)
                self.assertIn('net_profit_protection', after['paper_positions'][0]['payload'])
                self.assertIn('net_profit_protection', after['paper_trades'][0]['payload'])
                for timing in result['timing']['portfolios']:
                    io = timing['accounting_io']
                    if measured:
                        self.assertGreater(io['execute_calls'], 0)
                        self.assertGreater(io['rows_returned'], 0)
                        self.assertEqual(io['execute_errors'], 1)
                        self.assertEqual(io['fetch_errors'], 0)
                    else:
                        self.assertEqual(io['execute_calls'], 0)
                        self.assertEqual(io['fetch_calls'], 0)
                outcomes.append(({key: value for key, value in result.items() if key != 'timing'}, after))
        # Compare every column and complete JSON payload, including VPP output,
        # without deleting timestamps, amounts, evidence or accounting fields.
        self.assertEqual(outcomes[1], outcomes[0])

    def test_waiting_protection_commits_during_cleanup_and_next_book_reads_new_nav_clock(self):
        names = ('Impulse', 'Champion')
        self.seed(names)
        cleanup_active, protection_finished = threading.Event(), threading.Event()
        errors, observations, order = [], {}, []
        clock = [self.now]
        guard_time = self.now+timedelta(seconds=30)
        guard_timing = {}

        def protective():
            try:
                with self.connect() as c, G.book_transaction(c, lane='PROTECTIVE', timing=guard_timing):
                    if not cleanup_active.wait(5):
                        raise AssertionError('protection acquired before cleanup but cleanup never began')
                    order.append('protective_acquired')
                    observations['first_committed_fees'] = c.execute(
                        "SELECT fees_rub FROM paper_portfolios WHERE name='Impulse'").fetchone()['fees_rub']
                    c.execute("UPDATE paper_portfolios SET fees_rub=9,last_mark_at=%s WHERE name='Champion'",
                              (guard_time,))
                    c.execute("INSERT INTO paper_orders VALUES ('guard-order','Champion',NULL,'GUARD_FIXTURE',0)")
                clock[0] = guard_time+timedelta(seconds=1)
                order.append('protective_committed')
            except BaseException as error:
                errors.append(error)
            finally:
                protection_finished.set()

        worker = threading.Thread(target=protective, daemon=True, name='sql-test-protection')

        def step(c, name, policy, book, prices, ruonia, usdrub, ts, commission, summary):
            if name == 'Impulse':
                worker.start()
                with self.mutex._condition:
                    self.assertTrue(self.mutex._condition.wait_for(
                        lambda: self.mutex._priority_waiters == 1, timeout=5),
                        'protective transaction must queue while first book owns the lock')
            else:
                order.append('next_book')
                self.assertTrue(protection_finished.is_set())
                account, positions = VP._portfolio_rows(c, name)
                observations['next_nav'] = VP._mark_nav(account, positions, {})[0]
                observations['next_last_mark'] = account['last_mark_at']
                observations['next_execution_at'] = datetime.fromisoformat(ts)
            return self.write_book(c, name, policy, book, prices, ruonia, usdrub, ts, commission, summary)

        def emit(event, **fields):
            if event != 'paper_portfolio_phase' or fields.get('phase') != 'book_done' or fields.get('portfolio') != 'Impulse':
                return
            try:
                order.append('cleanup_started')
                cleanup_active.set()
                if not protection_finished.wait(5):
                    raise AssertionError('protection could not complete inside the cleanup callback')
                # A different connection can own the same database lock while
                # the cleanup callback is still active: no hidden outer commit.
                with self.connect() as c, c.transaction():
                    observations['cleanup_db_lock_free'] = c.execute(
                        'SELECT pg_try_advisory_xact_lock(%s) AS acquired', (G.BOOK_LOCK_ID,)).fetchone()['acquired']
                order.append('cleanup_finished')
            except BaseException as error:
                # emit_diagnostic intentionally swallows callback exceptions;
                # collect failures so the test cannot pass through that policy.
                errors.append(error)

        try:
            result = self.run_books(names, step, emit, lambda: clock[0].isoformat())
        finally:
            cleanup_active.set()
            if worker.ident is not None:
                worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(result['committed_portfolios'], list(names))
        self.assertEqual(guard_timing['status'], 'COMMITTED')
        self.assertEqual(observations['first_committed_fees'], 2.)
        self.assertIs(observations['cleanup_db_lock_free'], True)
        self.assertEqual(observations['next_nav'], 991.)
        self.assertEqual(observations['next_last_mark'], guard_time)
        self.assertGreater(observations['next_execution_at'], observations['next_last_mark'])
        self.assertEqual(order, ['cleanup_started', 'protective_acquired', 'protective_committed',
                                 'cleanup_finished', 'next_book'])
        with self.connect() as c:
            account, positions = VP._portfolio_rows(c, 'Champion')
            self.assertEqual(account['last_mark_at'], observations['next_execution_at'])
            self.assertEqual(VP._mark_nav(account, positions, {})[0], 993.)
            self.assertEqual(c.execute("SELECT count(*) AS n FROM paper_orders WHERE client_order_id='guard-order'")
                             .fetchone()['n'], 1)


if __name__ == '__main__':
    unittest.main()
