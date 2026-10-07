"""Real PostgreSQL parity and bounded transport for held-position excursions.

The frozen helper is the pre-projection implementation. No financial formula,
quote admission rule, or execution boundary is replaced in these tests.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
import time
import unittest
from unittest.mock import patch
import uuid

import veritas_portfolio as P
import veritas_position_guard as G
import veritas_price_source as S


DSN = os.getenv('VERITAS_QUALITY_TEST_DSN', '')
SQL_NULL = object()


def legacy_update_excursions(c, name, prices, ts):
    """Frozen from 63f3a7f, including its whole-pass exception boundary."""
    try:
        rows = c.execute("SELECT * FROM paper_positions WHERE portfolio_name=%s", (name,)).fetchall()
        for z0 in rows:
            z = dict(z0); asset = z.get('asset'); quote = G.quote_for_position(z, now=ts)
            if not quote: continue
            px = float(quote['price'])
            entry = P._v90j_float(z.get('avg_entry_price'))
            if px is None or entry is None or entry <= 0:
                continue
            sign = 1.0 if z.get('direction') == 'LONG' else -1.0
            signed = 100.0*sign*(px/entry-1.0)
            payload = P._v90j_json(z.get('payload'))
            old_mfe = P._v90j_float(payload.get('mfe_pct'), 0.0)
            old_mae = P._v90j_float(payload.get('mae_pct'), 0.0)
            payload['mfe_pct'] = max(0.0, old_mfe, signed)
            payload['mae_pct'] = min(0.0, old_mae, signed)
            payload['last_mark_price'] = px
            payload['last_mark_at'] = P._v90j_iso(ts)
            tid = z.get('active_trade_id')
            c.execute("UPDATE paper_positions SET payload=%s::jsonb WHERE portfolio_name=%s AND asset=%s",
                      (json.dumps(payload, ensure_ascii=False, default=str), name, asset))
            c.execute("UPDATE paper_trades SET payload=payload || %s::jsonb WHERE trade_id=%s",
                      (json.dumps({'mfe_pct': payload['mfe_pct'], 'mae_pct': payload['mae_pct'],
                                   'last_mark_price': px, 'last_mark_at': P._v90j_iso(ts)},
                                  ensure_ascii=False, default=str), tid))
    except Exception:
        pass


class TraceConnection:
    """Measure encoded fetched-row / serialized parameter size, not wire bytes."""
    def __init__(self, connection):
        self.connection = connection
        self.reads, self.position_writes, self.trade_writes = [], [], []

    def execute(self, query, params=None):
        started = time.perf_counter()
        cursor = self.connection.execute(query, params)
        if query.lstrip().upper().startswith('SELECT'):
            owner = self
            class ReadCursor:
                def fetchall(self):
                    rows = cursor.fetchall()
                    elapsed = time.perf_counter()-started
                    owner.reads.append({'bytes': len(json.dumps(rows, default=str).encode()),
                                        'seconds': elapsed, 'rows': len(rows)})
                    return rows
            return ReadCursor()
        if query.startswith('UPDATE paper_positions'):
            self.position_writes.append(len(params[0].encode()))
        if query.startswith('UPDATE paper_trades'):
            self.trade_writes.append(len(params[0].encode()))
        return cursor


@unittest.skipUnless(DSN, 'isolated PostgreSQL test database not configured')
class ExcursionProjectionSQLTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        self.driver, self.sql, self.row_factory = psycopg, sql, dict_row
        self.schema = 'excursion_projection_test_'+uuid.uuid4().hex
        self.now = datetime.now(timezone.utc)
        for cache in (G._quotes, G._source_quotes, G._market_state):
            self.enterContext(patch.dict(cache, {}, clear=True))
        with self.driver.connect(DSN, row_factory=self.row_factory) as c:
            self.verify_database(c)
            c.execute(self.sql.SQL('CREATE SCHEMA {}').format(self.sql.Identifier(self.schema)))
            c.execute(self.sql.SQL('SET search_path TO {}').format(self.sql.Identifier(self.schema)))
            # Nullable payloads deliberately extend the production schema to
            # verify legacy imported/corrupt shapes without normalizing them.
            c.execute('''CREATE TABLE paper_positions (
                portfolio_name text, asset text, direction text, units float8,
                avg_entry_price float8, last_price float8, stop_price float8,
                target_fraction float8, opened_at timestamptz, updated_at timestamptz,
                active_trade_id text, payload jsonb, PRIMARY KEY(portfolio_name,asset))''')
            c.execute('''CREATE TABLE paper_trades (
                trade_id text PRIMARY KEY, portfolio_name text, status text,
                gross_pnl_rub float8, fees_rub float8, funding_rub float8,
                net_pnl_rub float8, payload jsonb)''')
            c.execute('''CREATE TABLE paper_portfolios (
                name text PRIMARY KEY, initial_nav_rub float8, realized_pnl_rub float8,
                fees_rub float8, funding_rub float8)''')
            c.execute('CREATE TABLE paper_orders (order_id text PRIMARY KEY, notional_rub float8)')
            c.execute('CREATE TABLE paper_nav_history (portfolio_name text PRIMARY KEY, nav_rub float8)')
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

    def quote(self, price=110., source='Binance spot', age=0, **extra):
        return {'price': price, 'source_names': {'primary': source},
                'observed_at': (self.now-timedelta(seconds=age)).isoformat(),
                'source_gate_pass': True, 'market_open': True, **extra}

    def payload(self, **extra):
        identity = S.identity('ETH', self.quote())
        return {'price_source_lock': identity,
                'entry_execution_observed_at': (self.now-timedelta(hours=1)).isoformat(),
                'source_locked_mark': {'identity': identity, 'price': 100.,
                                       'observed_at': (self.now-timedelta(minutes=1)).isoformat()},
                'mfe_pct': 5., 'mae_pct': -2., **extra}

    def seed(self, c, rows):
        c.execute('TRUNCATE paper_positions,paper_trades,paper_portfolios,paper_orders,paper_nav_history')
        for row in rows:
            name, payload = row['name'], row['payload']
            encoded = None if payload is SQL_NULL else json.dumps(payload, ensure_ascii=False)
            trade_payload = row.get('trade_payload', {'journal_evidence': {'kept': [1, None, 'данные']}})
            encoded_trade = None if trade_payload is SQL_NULL else json.dumps(trade_payload)
            c.execute('''INSERT INTO paper_positions VALUES
                (%s,%s,%s,3,%s,101,95,.2,%s,%s,%s,%s::jsonb)''',
                (name, row.get('asset', 'ETH'), row.get('direction', 'LONG'), row.get('entry', 100.),
                 self.now-timedelta(hours=1), self.now-timedelta(minutes=1), name+'-trade', encoded))
            c.execute("INSERT INTO paper_trades VALUES (%s,%s,'OPEN',12,2,3,7,%s::jsonb)",
                      (name+'-trade', name, encoded_trade))
            c.execute('INSERT INTO paper_portfolios VALUES (%s,10000,12,2,3)', (name,))
            c.execute('INSERT INTO paper_orders VALUES (%s,300)', (name+'-order',))
            c.execute('INSERT INTO paper_nav_history VALUES (%s,10007)', (name,))

    def snapshot(self, c):
        return {table: c.execute('SELECT * FROM '+table+' ORDER BY 1,2').fetchall()
                for table in ('paper_positions', 'paper_trades', 'paper_portfolios',
                              'paper_orders', 'paper_nav_history')}

    def assert_financial_unchanged(self, before, after):
        for table in before:
            old = [{k: v for k, v in row.items() if k != 'payload'} for row in before[table]]
            new = [{k: v for k, v in row.items() if k != 'payload'} for row in after[table]]
            self.assertEqual(new, old, table)

    def run_helper(self, helper, rows, marks):
        with self.connect() as c:
            self.seed(c, rows)
            before = self.snapshot(c)
            trace, snapshots = TraceConnection(c), []
            for ts, quotes in marks:
                for cache in (G._quotes, G._source_quotes, G._market_state):
                    cache.clear()
                for asset, quote in quotes:
                    G.publish_quote(asset, quote)
                with c.transaction():
                    for row in rows:
                        # This deliberately foreign scalar must remain ignored.
                        helper(trace, row['name'], {row.get('asset', 'ETH'): 999999.}, ts)
                snapshots.append(self.snapshot(c))
                self.assert_financial_unchanged(before, snapshots[-1])
            return before, snapshots, trace

    def assert_parity(self, rows, marks):
        old = self.run_helper(legacy_update_excursions, rows, marks)
        new = self.run_helper(P._v90j_update_excursions, rows, marks)
        self.assertEqual(new[:2], old[:2])
        self.assertEqual(len(new[2].position_writes), len(old[2].position_writes))
        self.assertEqual(len(new[2].trade_writes), len(old[2].trade_writes))
        return old, new

    def test_large_proof_long_short_extrema_last_mark_and_bounded_transfer(self):
        # The full sealed journal remains authoritative in PostgreSQL, but none
        # of this unrelated proof is an input to excursion/quote evaluation.
        proof = {'canonical': [{'id': n, 'context': 'evidence-'+str(n)*128} for n in range(1500)]}
        payload = self.payload(timeframe_entry_context=proof, unrelated=[None, {'x': 'сохранить'}])
        rows = [{'name': direction, 'direction': direction, 'payload': payload}
                for direction in ('LONG', 'SHORT')]
        marks = [(self.now+timedelta(seconds=n), [('ETH', self.quote(price=price, age=-n))])
                 for n, price in enumerate((110., 90., 105.))]
        old, new = self.assert_parity(rows, marks)
        self.assertEqual(len(new[2].reads), 6)
        self.assertEqual(len(new[2].position_writes), 6)
        for row in new[1][-1]['paper_positions']:
            p = row['payload']
            self.assertAlmostEqual(p['mfe_pct'], 10.)
            self.assertAlmostEqual(p['mae_pct'], -10.)
            self.assertEqual(p['last_mark_price'], 105.)
            self.assertEqual(p['last_mark_at'], marks[-1][0].isoformat())
            self.assertEqual(p['timeframe_entry_context'], proof)
        self.assertGreater(min(r['bytes'] for r in old[2].reads), 500000)
        self.assertLess(max(r['bytes'] for r in new[2].reads), 4096)
        self.assertGreater(min(old[2].position_writes), 500000)
        self.assertLess(max(new[2].position_writes), 256)
        self.assertEqual(new[2].position_writes, new[2].trade_writes)
        print('excursion fixture encoding '+json.dumps({label: {
            'encoded_result_bytes': sum(r['bytes'] for r in trace.reads),
            'serialized_position_update_bytes': sum(trace.position_writes),
            'query_seconds': round(sum(r['seconds'] for r in trace.reads), 6)}
            for label, trace in (('legacy', old[2]), ('projected', new[2]))}))

    def test_unavailable_foreign_stale_and_older_quotes_never_change_rows(self):
        cases = [
            ('unavailable', self.payload(), []),
            ('foreign', self.payload(), [('ETH', self.quote(source='Coinbase spot'))]),
            ('stale', self.payload(), [('ETH', self.quote(age=1800))]),
            ('future', self.payload(), [('ETH', self.quote(age=-600))]),
            ('denied', self.payload(), [('ETH', self.quote(source_gate_pass=False))]),
            ('entry_newer', self.payload(entry_execution_observed_at=self.now.isoformat()),
             [('ETH', self.quote(age=10))]),
            ('mark_newer', self.payload(source_locked_mark={'observed_at': self.now.isoformat()}),
             [('ETH', self.quote(age=10))]),
            ('market_entry_newer', self.payload(entry_execution_observed_at=None,
                                               entry_market_observed_at=self.now.isoformat()),
             [('ETH', self.quote(age=10))]),
        ]
        for label, payload, quotes in cases:
            with self.subTest(case=label):
                _, new = self.assert_parity([{'name': label, 'payload': payload}], [(self.now, quotes)])
                self.assertEqual(new[1][0], new[0])
                self.assertEqual(new[2].position_writes, [])

    def test_legacy_identity_fields_and_invalid_entries_match(self):
        q = self.quote(source='MOEX ISS GOLD', contract={'secid': 'GDU6'})
        cases = [
            ({'contract_identity': {'primary_source': 'MOEX ISS GOLD', 'contract_id': 'GDU6'}}, 100., True),
            ({'entry_primary_source': 'MOEX ISS GOLD', 'entry_contract_secid': 'GDU6'}, 100., True),
            ({'entry_primary_source': 'MOEX ISS GOLD', 'entry_contract_secid': 'GDZ6'}, 100., False),
            ({'entry_primary_source': 'MOEX ISS GOLD'}, 0., False),
            ({'entry_primary_source': 'MOEX ISS GOLD'}, None, False),
        ]
        for payload, entry, changed in cases:
            with self.subTest(payload=payload, entry=entry):
                _, new = self.assert_parity([{'name': 'legacy', 'asset': 'GOLD', 'payload': payload,
                                             'entry': entry}], [(self.now, [('GOLD', q)])])
                self.assertEqual(bool(new[2].position_writes), changed)

    def test_nonobject_and_malformed_nested_payloads_preserve_legacy_behavior(self):
        encoded = json.dumps(self.payload(unrelated={'preserved': [1, 'текст', None]}))
        shapes = [SQL_NULL, None, False, True, 0, 7, [], [['mfe_pct', 9]],
                  '', 'not-json', 'null', '[]', '[1]', 'false', '3', encoded,
                  self.payload(price_source_lock='broken'),
                  self.payload(contract_identity='broken', price_source_lock=None),
                  self.payload(source_locked_mark='broken'),
                  self.payload(mfe_pct='not-a-number', mae_pct={'invalid': True})]
        trade_shapes = [SQL_NULL, None, 'journal-string', ['prior'], {'prior': {'keep': True}}]
        for n, payload in enumerate(shapes):
            with self.subTest(shape=n):
                rows = [{'name': 'shape', 'payload': payload,
                         'trade_payload': trade_shapes[n % len(trade_shapes)]}]
                old, new = self.assert_parity(rows, [(self.now, [('ETH', self.quote())])])
                if payload == encoded:
                    self.assertEqual(new[1][0]['paper_positions'][0]['payload']['unrelated'],
                                     {'preserved': [1, 'текст', None]})
                    self.assertEqual(len(new[2].position_writes), 1)


if __name__ == '__main__':
    unittest.main()
