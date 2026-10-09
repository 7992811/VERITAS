"""Native transaction-local book compression, never a production database.

Fresh UUID schemas contain fabricated, ordinary LOGGED financial tables. The
production book_transaction and PIO writer execute on the original autocommit
psycopg connection. PostgreSQL must actually store LZ4; an unavailable build is
a failing prerequisite, not a skipped or simulated success.
"""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
import unittest
from unittest.mock import patch
import uuid

from veritas_book_lock import PriorityRLock
import veritas_book_storage as BS
import veritas_position_guard as G
import veritas_protective_io as PIO
from test_veritas_toast_compression_sql import fixture_payload


DSN = os.getenv('VERITAS_QUALITY_TEST_DSN', '')
TABLE_ORDER = {
    'paper_positions': 'active_trade_id',
    'paper_trades': 'trade_id',
    'paper_portfolios': 'name',
    'paper_orders': 'client_order_id',
    'paper_nav_history': 'portfolio_name',
}
PAYLOAD_KEYS = (('paper_positions', 'active_trade_id'), ('paper_trades', 'trade_id'))


class FixtureRollback(RuntimeError):
    pass


@unittest.skipUnless(DSN, 'isolated PostgreSQL test database not configured')
class BookStorageSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        cls.driver, cls.sql, cls.row_factory = psycopg, sql, dict_row
        cls.payloads = [fixture_payload(index) for index in range(3)]

    def setUp(self):
        self.schema = 'book_storage_test_'+uuid.uuid4().hex
        self.enterContext(patch.object(G, '_mutex', PriorityRLock()))
        self.enterContext(patch.dict(os.environ, {'VERITAS_BOOK_TOAST_COMPRESSION': 'lz4'}))
        with self.connect() as c:
            original = self.setting(c)
            try:
                with c.transaction():
                    c.execute("SET LOCAL default_toast_compression = 'lz4'")
                    self.assertEqual(self.setting(c), 'lz4')
            except self.driver.Error as exc:
                self.fail('Native book-storage tests require compiled LZ4 support; '
                          'SET LOCAL failed with SQLSTATE '+str(exc.sqlstate))
            self.assertEqual(self.setting(c), original)
            with c.transaction():
                c.execute(self.sql.SQL('CREATE SCHEMA {}').format(self.sql.Identifier(self.schema)))
                c.execute('''CREATE TABLE paper_positions (
                    portfolio_name text, asset text, direction text, units float8,
                    avg_entry_price float8, last_price float8, stop_price float8,
                    opened_at timestamptz, active_trade_id text PRIMARY KEY, payload jsonb)''')
                c.execute('''CREATE TABLE paper_trades (
                    trade_id text PRIMARY KEY, portfolio_name text, status text,
                    gross_pnl_rub float8, fees_rub float8, funding_rub float8, payload jsonb)''')
                c.execute('''CREATE TABLE paper_portfolios (
                    name text PRIMARY KEY, realized_pnl_rub float8, fees_rub float8,
                    funding_rub float8, last_mark_at timestamptz, payload jsonb)''')
                c.execute('''CREATE TABLE paper_orders (
                    client_order_id text PRIMARY KEY, trade_id text, notional_rub float8,
                    fee_rub float8, payload jsonb)''')
                c.execute('''CREATE TABLE paper_nav_history (
                    portfolio_name text PRIMARY KEY, observed_at timestamptz,
                    nav_rub float8, drawdown float8, payload jsonb)''')
        self.addCleanup(self.drop_schema)
        with self.connect() as c:
            self.seed(c)

    @contextmanager
    def connect(self):
        with self.driver.connect(DSN, autocommit=True, row_factory=type(self).row_factory,
                                 connect_timeout=5) as c:
            # Every connection is checked before any setting or schema write.
            if c.execute('SELECT current_database() AS name').fetchone()['name'] != 'veritas_quality_test':
                raise RuntimeError('Refusing book-storage test writes outside veritas_quality_test')
            c.execute(self.sql.SQL('SET search_path TO {}').format(self.sql.Identifier(self.schema)))
            c.execute("SET statement_timeout = '10s'")
            yield c

    def drop_schema(self):
        with self.connect() as c:
            c.execute(self.sql.SQL('DROP SCHEMA {} CASCADE').format(self.sql.Identifier(self.schema)))

    @staticmethod
    def setting(c):
        return c.execute('SHOW default_toast_compression').fetchone()['default_toast_compression']

    def seed(self, c):
        with c.transaction():
            c.execute("SET LOCAL default_toast_compression = 'pglz'")
            c.execute('TRUNCATE '+','.join(TABLE_ORDER))
            for index, payload in enumerate(self.payloads):
                encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
                tid, name = 'trade-'+str(index).zfill(2), 'Synthetic-'+str(index)
                c.execute('''INSERT INTO paper_positions VALUES
                    (%s,%s,%s,%s,%s,%s,%s,'2026-10-07T18:00:00Z',%s,%s::jsonb)''',
                    (name, 'SYNTH-'+str(index), 'LONG' if index % 2 else 'SHORT',
                     7.+index, 100.+index, 101.+index, 95.+index, tid, encoded))
                c.execute("INSERT INTO paper_trades VALUES (%s,%s,'OPEN',%s,%s,%s,%s::jsonb)",
                          (tid, name, 17.+index, 3.+index, 2.+index, encoded))
                c.execute('''INSERT INTO paper_portfolios VALUES
                    (%s,17,3,2,'2026-10-07T18:00:00Z','{"accounting_witness":[1,null,false]}'::jsonb)''',
                    (name,))
                c.execute('''INSERT INTO paper_orders VALUES
                    (%s,%s,700,3,'{"source_audit":{"provider":"synthetic"}}'::jsonb)''',
                    ('order-'+tid, tid))
                c.execute('''INSERT INTO paper_nav_history VALUES
                    (%s,'2026-10-07T18:00:00Z',100012,0.001,'{"risk_governor":{"allow":true}}'::jsonb)''',
                    (name,))
        self.assert_codecs(c, 'pglz', 'pglz')

    def contents(self, c):
        return {table: c.execute('SELECT * FROM '+table+' ORDER BY '+key).fetchall()
                for table, key in TABLE_ORDER.items()}

    def attributes(self, c):
        # Resolve the same search_path as the application, not a production schema.
        rows = c.execute('''SELECT r.relname,a.attcompression::text AS compression,
                a.attstorage::text AS storage,r.relpersistence::text AS persistence
            FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class r ON r.oid=a.attrelid
            WHERE a.attrelid IN (pg_catalog.to_regclass('paper_positions'),
                                pg_catalog.to_regclass('paper_trades'))
              AND a.attname='payload' AND a.attnum>0 AND NOT a.attisdropped
            ORDER BY r.relname''').fetchall()
        return rows

    def codecs(self, c):
        return {table: {row['tid']: row['codec'] for row in c.execute(
            'SELECT '+key+' AS tid,pg_column_compression(payload) AS codec FROM '+table).fetchall()}
            for table, key in PAYLOAD_KEYS}

    def assert_codecs(self, c, positions, trades):
        expected = {table: {'trade-'+str(index).zfill(2): method for index in range(3)}
                    for (table, unused), method in zip(PAYLOAD_KEYS, (positions, trades))}
        self.assertEqual(self.codecs(c), expected)

    @staticmethod
    def patches():
        # Two changed rows and one retained row prove that enabling the setting
        # does not rewrite stored history. Expected values below are independent
        # of the production JSONB writer and its codec selection.
        return [('trade-'+str(index).zfill(2), {
            'source_locked_mark': {'identity': {'provider': 'synthetic', 'contract_uid': 'SYNTH-'+str(index)},
                                   'price': 105.+index, 'observed_at': '2026-10-07T18:01:00Z'},
            'observation_path': {'mfe_pct': .5+index, 'observation_count': 2,
                                 'eligible': True, 'nullable': None},
        }) for index in range(2)]

    @staticmethod
    def expected_contents(before, patches):
        expected = deepcopy(before)
        for table, key in PAYLOAD_KEYS:
            by_id = {row[key]: row for row in expected[table]}
            for tid, delta in patches:
                by_id[tid]['payload'] = dict(by_id[tid]['payload'], **deepcopy(delta))
        return expected

    @contextmanager
    def book(self, c, *, requested='lz4', lane='PORTFOLIO', blocking=True, timing=None):
        original, outcomes = BS.configure, []
        def observe(raw, **kwargs):
            self.assertIs(raw, c)
            outcome = original(raw, requested=requested, prepared=kwargs.get('prepared'))
            outcomes.append(outcome)
            return outcome
        with patch.object(BS, 'configure', side_effect=observe) as probe:
            with G.book_transaction(c, lane=lane, blocking=blocking, timing=timing) as acquired:
                yield acquired, outcomes, probe

    def test_book_hook_preserves_every_value_and_only_updated_payloads_become_lz4(self):
        outcomes = {}
        with self.connect() as c:
            c.execute("SET default_toast_compression = 'pglz'")
            for requested in ('default', 'lz4'):
                with self.subTest(requested=requested):
                    self.seed(c)
                    before, attributes = self.contents(c), self.attributes(c)
                    expected = self.expected_contents(before, self.patches())
                    timing = {}
                    with self.book(c, requested=requested, timing=timing) as (acquired, configured, probe):
                        self.assertIsNone(acquired)
                        self.assertEqual(c.info.transaction_status, self.driver.pq.TransactionStatus.INTRANS)
                        self.assertEqual(self.setting(c), 'lz4' if requested == 'lz4' else 'pglz')
                        self.assertEqual(PIO.write_patches(c, self.patches(), optional=True), {'trade-00', 'trade-01'})
                        self.assertEqual(self.contents(c), expected)
                    probe.assert_called_once()
                    self.assertEqual(configured[0]['status'] if configured[0] else None,
                                     'APPLIED' if requested == 'lz4' else None)
                    if requested == 'lz4':
                        self.assertEqual(timing['payload_compression'], configured[0])
                        self.assertEqual(timing['storage_setup_seconds'], configured[0]['elapsed_seconds'])
                        self.assertGreaterEqual(timing['storage_setup_seconds'], 0.)
                    else:
                        self.assertNotIn('payload_compression', timing)
                        self.assertNotIn('storage_setup_seconds', timing)
                    self.assertEqual(timing['status'], 'COMMITTED')
                    self.assertEqual(c.info.transaction_status, self.driver.pq.TransactionStatus.IDLE)
                    self.assertEqual(self.setting(c), 'pglz')
                    self.assertEqual(self.attributes(c), attributes)
                    actual = self.contents(c)
                    self.assertEqual(actual, expected)
                    outcomes[requested] = actual
                    expected_codec = 'lz4' if requested == 'lz4' else 'pglz'
                    self.assertEqual(self.codecs(c), {table: {
                        'trade-00': expected_codec, 'trade-01': expected_codec, 'trade-02': 'pglz'}
                        for table, unused in PAYLOAD_KEYS})
            self.assertEqual(outcomes['default'], outcomes['lz4'])

    def test_prior_session_setting_returns_after_commit_and_outer_rollback(self):
        with self.connect() as c, self.connect() as observer:
            observer_setting = self.setting(observer)
            for prior in ('pglz', 'lz4'):
                for rollback in (False, True):
                    with self.subTest(prior=prior, rollback=rollback):
                        self.seed(c)
                        c.execute("SET default_toast_compression = '"+prior+"'")
                        before, timing = self.contents(c), {}
                        try:
                            with self.book(c, lane='PROTECTIVE', timing=timing) as (acquired, configured, probe):
                                self.assertEqual(self.setting(c), 'lz4')
                                self.assertEqual(self.setting(observer), observer_setting)
                                self.assertEqual(configured[0]['status'],
                                                 'ALREADY_ACTIVE' if prior == 'lz4' else 'APPLIED')
                                PIO.write_patches(c, self.patches(), optional=True)
                                c.execute("UPDATE paper_portfolios SET funding_rub=funding_rub+7 WHERE name='Synthetic-0'")
                                if rollback:
                                    raise FixtureRollback('roll back the entire book')
                        except FixtureRollback:
                            self.assertTrue(rollback)
                        else:
                            self.assertFalse(rollback)
                        self.assertEqual(timing['status'], 'ROLLED_BACK' if rollback else 'COMMITTED')
                        self.assertEqual(c.info.transaction_status, self.driver.pq.TransactionStatus.IDLE)
                        self.assertEqual(self.setting(c), prior)
                        self.assertEqual(self.setting(observer), observer_setting)
                        expected = before if rollback else self.expected_contents(before, self.patches())
                        if not rollback:
                            expected['paper_portfolios'][0]['funding_rub'] += 7
                        self.assertEqual(self.contents(c), expected)

    def test_explicit_column_policy_is_preserved_without_rewriting_existing_rows(self):
        with self.connect() as c:
            c.execute("SET default_toast_compression = 'pglz'")
            for policy in ('pglz', 'lz4'):
                with self.subTest(policy=policy):
                    c.execute('ALTER TABLE paper_positions ALTER COLUMN payload SET COMPRESSION default')
                    self.seed(c)
                    before = self.contents(c)
                    c.execute('ALTER TABLE paper_positions ALTER COLUMN payload SET COMPRESSION '+policy)
                    attributes = self.attributes(c)
                    self.assert_codecs(c, 'pglz', 'pglz')
                    patches = self.patches()[:1]
                    with self.book(c) as (acquired, configured, probe):
                        self.assertEqual(configured[0]['status'], 'COLUMN_POLICY')
                        self.assertEqual(self.setting(c), 'pglz')
                        PIO.write_patches(c, patches)
                    self.assertEqual(self.attributes(c), attributes)
                    self.assertEqual(self.contents(c), self.expected_contents(before, patches))
                    self.assertEqual(self.codecs(c), {
                        'paper_positions': {'trade-00': policy, 'trade-01': 'pglz', 'trade-02': 'pglz'},
                        'paper_trades': {'trade-00': 'pglz', 'trade-01': 'pglz', 'trade-02': 'pglz'},
                    })

    def test_real_22023_rolls_back_probe_then_the_same_book_commits_successfully(self):
        with self.connect() as c:
            c.execute("SET default_toast_compression = 'pglz'")
            before, timing = self.contents(c), {}
            # An actual PostgreSQL enum error leaves its savepoint INERROR until
            # the production context manager rolls back. No Python-only fake.
            with patch.object(BS, 'SET_LZ4_SQL',
                              "SET LOCAL default_toast_compression = '__unsupported_fixture_enum__'"):
                with self.book(c, timing=timing) as (acquired, configured, probe):
                    self.assertEqual(configured[0]['status'], 'UNSUPPORTED')
                    self.assertEqual(c.info.transaction_status, self.driver.pq.TransactionStatus.INTRANS)
                    self.assertEqual(self.setting(c), 'pglz')
                    self.assertEqual(PIO.write_patches(c, self.patches(), optional=True), {'trade-00', 'trade-01'})
                    c.execute("UPDATE paper_portfolios SET fees_rub=fees_rub+.5 WHERE name='Synthetic-0'")
            self.assertEqual(timing['status'], 'COMMITTED')
            self.assertEqual(c.info.transaction_status, self.driver.pq.TransactionStatus.IDLE)
            self.assertEqual(self.setting(c), 'pglz')
            expected = self.expected_contents(before, self.patches())
            expected['paper_portfolios'][0]['fees_rub'] += .5
            self.assertEqual(self.contents(c), expected)
            self.assert_codecs(c, 'pglz', 'pglz')

    def test_unexpected_probe_errors_propagate_and_restore_the_outer_financial_transaction(self):
        cases = (
            ('METADATA_SQL', 'SELECT 1/0', self.driver.errors.DivisionByZero),
            ('SET_LZ4_SQL', 'SELECT 1/0', self.driver.errors.DivisionByZero),
            ('SET_LZ4_SQL', 'SELECT pg_catalog.pg_sleep(1)', self.driver.errors.QueryCanceled),
        )
        with self.connect() as c:
            c.execute("SET default_toast_compression = 'pglz'")
            for constant, statement, error in cases:
                with self.subTest(constant=constant, error=error.__name__):
                    before, timing = self.contents(c), {}
                    with patch.object(BS, constant, statement):
                        with self.assertRaises(error):
                            with c.transaction():
                                c.execute("UPDATE paper_portfolios SET fees_rub=999 WHERE name='Synthetic-0'")
                                c.execute("UPDATE paper_trades SET funding_rub=999 WHERE trade_id='trade-00'")
                                if error is self.driver.errors.QueryCanceled:
                                    c.execute("SET LOCAL statement_timeout = '50ms'")
                                with self.book(c, timing=timing):
                                    self.fail('a failed probe must not enter the accounting body')
                    self.assertEqual(timing['status'], 'ROLLED_BACK')
                    self.assertEqual(c.info.transaction_status, self.driver.pq.TransactionStatus.IDLE)
                    self.assertEqual(self.setting(c), 'pglz')
                    self.assertEqual(self.contents(c), before)
                    self.assertIsNone(G._mutex._owner)

    def test_busy_other_and_structural_lanes_do_not_call_probe(self):
        with self.connect() as c:
            c.execute("SET default_toast_compression = 'pglz'")
            for lane in ('OTHER', 'STRUCTURAL_ENTRY'):
                with self.subTest(lane=lane):
                    with self.book(c, lane=lane) as (acquired, configured, probe):
                        self.assertIsNone(acquired)
                        probe.assert_not_called()
                        self.assertEqual(configured, [])
                        self.assertEqual(self.setting(c), 'pglz')
            with self.connect() as holder, holder.transaction():
                holder.execute('SELECT pg_advisory_xact_lock(%s)', (G.BOOK_LOCK_ID,))
                timing = {}
                with self.book(c, lane='PROTECTIVE', blocking=False, timing=timing) as (acquired, configured, probe):
                    self.assertIs(acquired, False)
                    probe.assert_not_called()
                    self.assertEqual(configured, [])
                self.assertEqual(timing['status'], 'BUSY')
                self.assertEqual(c.info.transaction_status, self.driver.pq.TransactionStatus.IDLE)
            # The failed nonblocking attempt must leave both lock owners usable.
            with self.book(c, lane='PROTECTIVE') as (acquired, configured, probe):
                self.assertEqual(configured[0]['status'], 'APPLIED')
                self.assertEqual(self.setting(c), 'lz4')


if __name__ == '__main__':
    unittest.main()
