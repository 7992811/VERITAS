"""Post-funding guard accounting stays fresh without transferring trade proof.

Both variants run the production guard, funding, quote gates and net assessment
against PostgreSQL. The legacy variant changes only the projected SELECT back
to the original SELECT *, through a connection proxy.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
import unittest
from unittest.mock import patch
import uuid

import veritas_costs as VC
import veritas_portfolio as P
import veritas_position_guard as G
import veritas_price_source as S
from veritas_book_lock import PriorityRLock


DSN = os.getenv('VERITAS_QUALITY_TEST_DSN', '')
PROJECTED_SQL = 'SELECT gross_pnl_rub,fees_rub,funding_rub FROM paper_trades WHERE trade_id=%s'
LEGACY_SQL = 'SELECT * FROM paper_trades WHERE trade_id=%s'
EARLY_SQL = 'SELECT fees_rub,funding_rub,gross_pnl_rub FROM paper_trades WHERE trade_id=%s'
ACCOUNTING_FIELDS = {'gross_pnl_rub', 'fees_rub', 'funding_rub'}


class GuardConnection:
    """Record only the two guard account reads; other real SQL is unchanged."""
    def __init__(self, connection, legacy=False):
        self.connection, self.legacy = connection, legacy
        self.events, self.reads = [], []

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, query, params=None):
        normalized = ' '.join(query.split())
        self.events.append(normalized)
        target = normalized == PROJECTED_SQL
        cursor = self.connection.execute(LEGACY_SQL if target and self.legacy else query, params)
        if target or normalized == EARLY_SQL:
            owner = self
            class AccountCursor:
                def fetchone(self):
                    row = cursor.fetchone()
                    owner.reads.append({'target': target, 'row': deepcopy(row),
                                        'bytes': len(json.dumps(row, default=str).encode())})
                    return row
            return AccountCursor()
        return cursor


@unittest.skipUnless(DSN, 'isolated PostgreSQL test database not configured')
class GuardAccountingProjectionSQLTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        self.driver, self.sql, self.row_factory = psycopg, sql, dict_row
        self.schema = 'guard_account_projection_test_'+uuid.uuid4().hex
        self.now = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
        self.opened, self.cursor = self.now-timedelta(days=4), self.now-timedelta(days=3)
        self.proof = {'retained_decision_evidence': 'original-proof|'*50000,
                      'nested': {'values': [None, True, 0, 'unchanged']}}
        self.enterContext(patch.object(G, '_mutex', PriorityRLock()))
        for cache in (G._quotes, G._source_quotes, G._market_state):
            self.enterContext(patch.dict(cache, {}, clear=True))
        # run_protective_pass supplies its explicit replay clock to its own
        # work. Its final refresh has an independent default wall clock.
        moment = self.now
        class ReplayClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment if tz else moment.replace(tzinfo=None)
        self.enterContext(patch.object(G.VPP, 'datetime', ReplayClock))
        with self.driver.connect(DSN, row_factory=self.row_factory) as c:
            self.verify_database(c)
            c.execute(self.sql.SQL('CREATE SCHEMA {}').format(self.sql.Identifier(self.schema)))
            c.execute(self.sql.SQL('SET search_path TO {}').format(self.sql.Identifier(self.schema)))
            c.execute('''CREATE TABLE paper_portfolios (
                name text PRIMARY KEY,initial_nav_rub float8,realized_pnl_rub float8,
                fees_rub float8,funding_rub float8,last_mark_at timestamptz,last_ruonia float8,
                high_water_nav_rub float8,benchmark_nav_rub float8,last_usdrub float8,
                updated_at timestamptz,payload jsonb)''')
            c.execute('''CREATE TABLE paper_positions (
                portfolio_name text,asset text,direction text,units float8,avg_entry_price float8,
                last_price float8,stop_price float8,target_fraction float8,
                opened_at timestamptz,updated_at timestamptz,active_trade_id text,payload jsonb,
                PRIMARY KEY(portfolio_name,asset))''')
            c.execute('''CREATE TABLE paper_trades (
                trade_id text PRIMARY KEY,portfolio_name text,asset text,direction text,
                status text,opened_at timestamptz,closed_at timestamptz,
                gross_pnl_rub float8,fees_rub float8,funding_rub float8,net_pnl_rub float8,
                avg_entry_price float8,max_fraction float8,payload jsonb)''')
            c.execute('''CREATE TABLE paper_orders (
                order_id text PRIMARY KEY,trade_id text,side text,notional_rub float8,
                fee_rub float8,payload jsonb)''')
            c.execute('''CREATE TABLE paper_nav_history (
                portfolio_name text,observed_at timestamptz,nav_rub float8,payload jsonb,
                PRIMARY KEY(portfolio_name,observed_at))''')
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

    def seed(self, direction):
        price = 100.2 if direction == 'LONG' else 99.8
        quote = {'asset': 'ETH', 'price': price, 'best_bid': price-.01, 'best_ask': price+.01,
                 'observed_at': self.now.isoformat(), 'source_gate_pass': True,
                 'market_open': True, 'source_names': {'primary': 'Binance spot'}}
        payload = {'price_source_lock': S.identity('ETH', quote),
                   'entry_execution_observed_at': self.opened.isoformat(),
                   'entry_nav_rub': 1000000., 'r55_net_profit_lock_active': True,
                   'trailing_stop': 100.3 if direction == 'LONG' else 99.7,
                   'r63_profit_lock_rearm_after_pct': 5., 'preserved_position': {'token': 'original'}}
        with self.connect() as c:
            c.execute('TRUNCATE paper_portfolios,paper_positions,paper_trades,paper_orders,paper_nav_history')
            c.execute('''INSERT INTO paper_portfolios VALUES
                ('Champion',1000000,11,53,7,%s,16,1000000,1000000,80,%s,%s::jsonb)''',
                      (self.cursor, self.cursor, json.dumps({'preserve': 'account'})))
            c.execute('''INSERT INTO paper_positions VALUES
                ('Champion','ETH',%s,1000,100,%s,%s,.1,%s,%s,'guard-trade',%s::jsonb)''',
                      (direction, price, 95. if direction == 'LONG' else 105.,
                       self.opened, self.cursor, json.dumps(payload)))
            c.execute('''INSERT INTO paper_trades VALUES
                ('guard-trade','Champion','ETH',%s,'OPEN',%s,NULL,11,53,7,-49,100,.1,%s::jsonb)''',
                      (direction, self.opened, json.dumps({'proof': self.proof, 'preserve': 'trade'})))
            c.execute('''INSERT INTO paper_trades VALUES
                ('unrelated-trade','Champion','BTC','LONG','CLOSED',%s,%s,29,3,2,24,200,.2,%s::jsonb)''',
                      (self.opened, self.cursor, json.dumps({'preserve': 'unrelated'})))
            c.execute("INSERT INTO paper_orders VALUES ('prior-order','guard-trade','BUY',100000,53,%s::jsonb)",
                      (json.dumps({'preserve': 'order'}),))
            c.execute("INSERT INTO paper_nav_history VALUES ('Champion',%s,999951,%s::jsonb)",
                      (self.cursor, json.dumps({'preserve': 'nav'})))
        return quote

    def snapshot(self):
        with self.connect() as c:
            return {table: c.execute(self.sql.SQL('SELECT * FROM {} ORDER BY {}').format(
                self.sql.Identifier(table), self.sql.Identifier(key))).fetchall()
                for table, key in (('paper_portfolios', 'name'), ('paper_positions', 'asset'),
                                   ('paper_trades', 'trade_id'), ('paper_orders', 'order_id'),
                                   ('paper_nav_history', 'observed_at'))}

    def run_case(self, direction, legacy):
        quote = self.seed(direction)
        before = self.snapshot()
        G.publish_quote('ETH', quote)
        observed, connections = [], []
        real_assessment = G._r63_soft_profit_stop_assessment

        @contextmanager
        def traced_connect():
            with self.connect() as c:
                wrapped = GuardConnection(c, legacy)
                connections.append(wrapped)
                yield wrapped

        def assess(z, q, trade, nav, commission):
            self.assertEqual(len(connections[0].reads), 2)
            self.assertEqual(connections[0].reads[0]['row']['funding_rub'], 7.)
            self.assertGreater(trade['funding_rub'], 7.)
            if not legacy:
                self.assertEqual(set(trade), ACCOUNTING_FIELDS)
            # A stale reuse would approve the soft stop in this fixture.
            stale = real_assessment(z, q, dict(trade, funding_rub=7.), nav, commission)
            fresh = real_assessment(z, q, trade, nav, commission)
            self.assertTrue(stale['valid'])
            self.assertGreater(stale['net_pnl_rub'], 0.)
            self.assertFalse(stale['suppress'])
            self.assertTrue(fresh['valid'])
            self.assertTrue(fresh['soft_only'])
            self.assertTrue(fresh['suppress'])
            self.assertLessEqual(fresh['net_pnl_rub'], 0.)
            observed.append({'assessment': fresh, 'stale': stale, 'quote': deepcopy(q), 'nav': nav})
            return fresh

        with patch.object(G, '_r63_soft_profit_stop_assessment', side_effect=assess), \
             patch.object(P, '_apply_funding', wraps=P._apply_funding) as funding, \
             patch.object(P, '_close_or_reduce', wraps=P._close_or_reduce) as close:
            changes = G.run_protective_pass(P, traced_connect, {'ETH': quote}, self.now)
        funding.assert_called_once()
        close.assert_not_called()
        self.assertEqual(len(observed), 1)
        self.assertEqual([x['reason'] for x in changes], ['PROFIT_LOCK_SOFT_STOP_NET_NEGATIVE_REARMED'])
        trace = connections[0]
        self.assertEqual(len(trace.reads), 2)
        self.assertEqual([x['target'] for x in trace.reads], [False, True])
        funding_query = 'UPDATE paper_trades SET funding_rub=funding_rub+%s WHERE trade_id=%s'
        self.assertLess(trace.events.index(EARLY_SQL), trace.events.index(funding_query))
        self.assertLess(trace.events.index(funding_query), trace.events.index(PROJECTED_SQL))
        after = self.snapshot()
        due = VC.funding_between(1000*quote['price'], self.opened, self.cursor, self.now)
        self.assertGreater(due, 0.)
        for table, ignored in (('paper_portfolios', {'funding_rub', 'last_mark_at'}),
                               ('paper_positions', {'payload'}), ('paper_trades', {'funding_rub', 'payload'})):
            for old, new in zip(before[table], after[table]):
                self.assertEqual({k: v for k, v in old.items() if k not in ignored},
                                 {k: v for k, v in new.items() if k not in ignored}, table)
        self.assertAlmostEqual(after['paper_portfolios'][0]['funding_rub'], 7.+due)
        self.assertEqual(after['paper_portfolios'][0]['last_mark_at'], self.now)
        self.assertAlmostEqual(after['paper_trades'][0]['funding_rub'], 7.+due)
        self.assertEqual(after['paper_trades'][0]['payload']['proof'], self.proof)
        self.assertEqual(after['paper_trades'][0]['payload']['preserve'], 'trade')
        self.assertEqual(after['paper_positions'][0]['payload']['preserved_position'], {'token': 'original'})
        self.assertIsNone(after['paper_positions'][0]['payload']['trailing_stop'])
        self.assertFalse(after['paper_positions'][0]['payload']['r55_net_profit_lock_active'])
        self.assertEqual(after['paper_trades'][1], before['paper_trades'][1])
        for table in ('paper_orders', 'paper_nav_history'):
            self.assertEqual(after[table], before[table])
        return {'after': after, 'changes': changes, 'observed': observed,
                'target_bytes': trace.reads[1]['bytes']}

    def assert_direction_parity(self, direction):
        legacy = self.run_case(direction, legacy=True)
        projected = self.run_case(direction, legacy=False)
        for key in ('after', 'changes', 'observed'):
            self.assertEqual(projected[key], legacy[key], key)
        # Encoded fetched-row sizes, not a claim about PostgreSQL wire traffic.
        self.assertGreater(legacy['target_bytes'], 500000)
        self.assertLess(projected['target_bytes'], 256)

    def test_long_reads_fresh_post_funding_accounts_with_identical_ledger(self):
        self.assert_direction_parity('LONG')

    def test_short_reads_fresh_post_funding_accounts_with_identical_ledger(self):
        self.assert_direction_parity('SHORT')


if __name__ == '__main__':
    unittest.main()
