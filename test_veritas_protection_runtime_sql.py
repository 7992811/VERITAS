"""Protective projections and batched deltas on isolated PostgreSQL only.

All rows are fabricated; native advisory locks, rollback, JSONB and accounting
projection execute as shipped. No test is permitted to write a production DB.
"""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
import unittest
from unittest.mock import patch
import uuid

import veritas_observation_path as OP
import veritas_position_guard as G
import veritas_profit_protection as PP
import veritas_protection_read_model as PR
from test_veritas_protection_runtime import NOW, position


DSN = os.getenv('VERITAS_QUALITY_TEST_DSN', '')


class TracedConnection:
    def __init__(self, connection):
        self.connection, self.statements = connection, []

    def transaction(self):
        return self.connection.transaction()

    def execute(self, sql, args=()):
        self.statements.append((sql, args))
        return self.connection.execute(sql, args)


@unittest.skipUnless(DSN, 'isolated PostgreSQL test database not configured')
class ProtectiveRuntimeSQLTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        self.driver, self.sql, self.row_factory = psycopg, sql, dict_row
        self.schema = 'protective_runtime_'+uuid.uuid4().hex
        for cache in (G._quotes, G._source_quotes, G._market_state):
            self.enterContext(patch.dict(cache, {}, clear=True))
        with self.driver.connect(DSN, row_factory=dict_row) as c:
            self.verify_database(c)
            c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
            c.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
            c.execute('''CREATE TABLE paper_positions (
                portfolio_name text, asset text, direction text, units float8,
                avg_entry_price float8, last_price float8, stop_price float8,
                opened_at timestamptz, active_trade_id text, payload jsonb,
                PRIMARY KEY(portfolio_name,asset))''')
            c.execute('''CREATE TABLE paper_trades (
                trade_id text PRIMARY KEY, portfolio_name text, status text,
                opened_at timestamptz, gross_pnl_rub float8, fees_rub float8,
                funding_rub float8, payload jsonb)''')
            c.execute('''CREATE TABLE paper_portfolios (
                name text PRIMARY KEY, initial_nav_rub float8,
                realized_pnl_rub float8, fees_rub float8, funding_rub float8,
                last_mark_at timestamptz, last_ruonia float8)''')
        self.addCleanup(self.drop_schema)

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
            c.execute(self.sql.SQL('DROP SCHEMA {} CASCADE').format(self.sql.Identifier(self.schema)))

    def seed(self, count=1):
        rows = []
        with self.connect() as c:
            for index in range(count):
                z, unused = position(index, structural=True)
                z['portfolio_name'] = 'Synthetic-'+str(index)
                z['payload']['immutable_history']['padding'] = 'diagnostic-'*5000
                rows.append(z)
                c.execute('INSERT INTO paper_positions VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)',
                          tuple(z[k] for k in PR.POSITION_COLUMNS)+(json.dumps(z['payload']),))
                c.execute("INSERT INTO paper_trades VALUES (%s,%s,'OPEN',%s,11,3,2,%s::jsonb)",
                          (z['active_trade_id'], z['portfolio_name'], z['opened_at'], json.dumps(z['payload'])))
                c.execute('INSERT INTO paper_portfolios VALUES (%s,500,11,3,2,%s,16)',
                          (z['portfolio_name'], NOW))
        return rows

    def contents(self, c):
        return [c.execute('SELECT * FROM '+table+' ORDER BY 1').fetchall()
                for table in ('paper_positions', 'paper_trades', 'paper_portfolios')]

    def test_native_no_exit_pass_is_bounded_and_preserves_ledger_and_witness(self):
        rows = self.seed(PR.BATCH_SIZE+1)
        q = position()[1]
        with self.connect() as c:
            trace = TracedConnection(c)
            @contextmanager
            def connect():
                yield trace
            before_accounts = c.execute('SELECT * FROM paper_portfolios ORDER BY name').fetchall()
            with patch.object(G, 'exit_fill', side_effect=AssertionError('irrelevant structural legacy fill')):
                self.assertEqual(G.run_protective_pass(None, connect, {'ETH': q}, NOW), [])
            reads = [sql for sql, args in trace.statements if sql.startswith('SELECT') and 'advisory' not in sql]
            updates = [(sql, args) for sql, args in trace.statements if sql.startswith('UPDATE')]
            self.assertEqual(reads, [PR.PROTECTION_SQL+' ORDER BY portfolio_name,asset FOR UPDATE'])
            self.assertEqual(len(updates), 4)
            self.assertEqual([len(json.loads(args[0])) for sql, args in updates],
                             [PR.BATCH_SIZE, PR.BATCH_SIZE, 1, 1])
            self.assertTrue(all(len(args[0]) < 60000 for sql, args in updates))
            saved = {z['active_trade_id']: z for z in c.execute('SELECT * FROM paper_positions').fetchall()}
            trades = {z['trade_id']: z for z in c.execute('SELECT * FROM paper_trades').fetchall()}
            for original in rows:
                tid = original['active_trade_id']
                result = saved[tid]
                self.assertEqual(result['units'], original['units'])
                self.assertEqual(result['stop_price'], original['stop_price'])
                self.assertEqual(result['payload']['immutable_history'], original['payload']['immutable_history'])
                self.assertEqual(result['payload']['observation_path'], OP.observe(original, q, NOW))
                self.assertEqual(result['payload'], trades[tid]['payload'])
                self.assertEqual((trades[tid]['gross_pnl_rub'], trades[tid]['fees_rub'], trades[tid]['funding_rub']), (11, 3, 2))
            self.assertEqual(c.execute('SELECT * FROM paper_portfolios ORDER BY name').fetchall(), before_accounts)

    def test_native_optional_pair_failure_rolls_back_savepoint_required_pair_rolls_back_book(self):
        z = self.seed()[0]
        with self.connect() as c:
            c.execute("ALTER TABLE paper_trades ADD CONSTRAINT synthetic_metadata CHECK (NOT (payload ? 'poison'))")
            before = self.contents(c)
            with G.book_transaction(c):
                PR.write_patches(c, [(z['active_trade_id'], {'poison': True})], optional=True)
                self.assertEqual(self.contents(c), before)
            with self.assertRaises(self.driver.errors.CheckViolation):
                with G.book_transaction(c):
                    PR.write_patches(c, [(z['active_trade_id'], {'poison': True})])
            self.assertEqual(self.contents(c), before)

    def test_native_bad_optional_chunk_recovers_every_healthy_row_and_next_chunk_commits(self):
        rows = self.seed(PR.BATCH_SIZE+1)
        patches = [(z['active_trade_id'], {'synthetic_mark': index}) for index, z in enumerate(rows)]
        patches[0][1]['poison'] = True
        with self.connect() as c:
            c.execute("ALTER TABLE paper_trades ADD CONSTRAINT synthetic_metadata CHECK (NOT (payload ? 'poison'))")
            trace = TracedConnection(c)
            with G.book_transaction(trace):
                PR.write_patches(trace, patches, optional=True)
            for table, key in (('paper_positions', 'active_trade_id'), ('paper_trades', 'trade_id')):
                saved = {row[key]: row['payload'] for row in c.execute('SELECT * FROM '+table).fetchall()}
                self.assertEqual(saved[rows[0]['active_trade_id']], rows[0]['payload'])
                for index, z in enumerate(rows[1:], 1):
                    self.assertEqual(saved[z['active_trade_id']], dict(z['payload'], synthetic_mark=index))
            # Roll back the failed pair, then recover each healthy member in a
            # bounded independent savepoint before processing the final chunk.
            updates = [(sql, args) for sql, args in trace.statements if sql.startswith('UPDATE')]
            self.assertEqual(len(updates), 2+2*PR.BATCH_SIZE+2)
            self.assertEqual([len(json.loads(args[0])) for sql, args in updates],
                             [PR.BATCH_SIZE, PR.BATCH_SIZE]+[1, 1]*PR.BATCH_SIZE+[1, 1])

    def test_native_repeated_ids_preserve_each_jsonb_merge_for_objects_arrays_and_scalars(self):
        z = self.seed()[0]
        tid = z['active_trade_id']
        first, second = {'a': 1, 'b': 2}, {'a': 3}
        with self.connect() as c:
            for previous in ({'prior': True}, ['prior'], 'prior'):
                with self.subTest(payload=previous):
                    for table in ('paper_positions', 'paper_trades'):
                        c.execute('UPDATE '+table+' SET payload=%s::jsonb', (json.dumps(previous),))
                    trace = TracedConnection(c)
                    with G.book_transaction(trace):
                        PR.write_patches(trace, [(tid, first), (tid, second)])
                    expected = (dict(previous, a=3, b=2) if isinstance(previous, dict)
                                else (previous if isinstance(previous, list) else [previous])+[first, second])
                    for table in ('paper_positions', 'paper_trades'):
                        self.assertEqual(c.execute('SELECT payload FROM '+table).fetchone()['payload'], expected)
                    batches = [json.loads(args[0]) for sql, args in trace.statements if sql.startswith('UPDATE')]
                    self.assertEqual(batches, [
                        [{'trade_id': tid, 'patch': first}], [{'trade_id': tid, 'patch': first}],
                        [{'trade_id': tid, 'patch': second}], [{'trade_id': tid, 'patch': second}]])

    def test_native_profit_refresh_keeps_exact_accounting_and_uses_only_current_fields(self):
        self.seed(3)
        with self.connect() as c:
            originals = c.execute('SELECT * FROM paper_positions').fetchall()
            accounts = PP.load_accounts(c, [z['active_trade_id'] for z in originals], include_payload=False)
            expected = {z['active_trade_id']: PP.evaluate(z, accounts[z['active_trade_id']], now=NOW)
                        for z in originals}
            trace = TracedConnection(c)
            with G.book_transaction(trace):
                PP.refresh(trace, now=NOW)
            self.assertTrue(any(sql == PP.REFRESH_POSITIONS_SQL for sql, args in trace.statements))
            self.assertEqual(len([sql for sql, args in trace.statements if sql.startswith('UPDATE')]), 2)
            for z in c.execute('SELECT * FROM paper_positions').fetchall():
                for key, value in expected[z['active_trade_id']].items():
                    self.assertEqual(z['payload'][key], value)
                self.assertEqual(z['units'], 3.)
                self.assertEqual(z['stop_price'], 90.)

    def test_native_projection_preserves_nonobject_nulls_and_unknown_integrity(self):
        z = self.seed()[0]
        with self.connect() as c:
            for value in (None, [], 'corrupt', 7, {'data_integrity_status': 'UNVERIFIED'}):
                c.execute('UPDATE paper_positions SET payload=%s::jsonb', (json.dumps(value),))
                projected = c.execute(PP.REFRESH_POSITIONS_SQL).fetchone()['payload']
                if isinstance(value, dict):
                    self.assertEqual(projected['data_integrity_status'], 'UNVERIFIED')
                else:
                    self.assertEqual(projected, value)
            c.execute('UPDATE paper_positions SET payload=NULL')
            self.assertIsNone(c.execute(PR.PROTECTION_SQL).fetchone()['payload'])


if __name__ == '__main__':
    unittest.main()
