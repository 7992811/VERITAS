"""PostgreSQL parity for compact reads feeding unchanged net-stop protection."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
import time
import unittest
import uuid

import veritas_profit_protection as PP
import veritas_protection_read_model as PR


DSN = os.getenv('VERITAS_QUALITY_TEST_DSN', '')
SQL_NULL = object()


def legacy_refresh(c, name=None, now=None, commission=PP.VC.COMMISSION_RATE):
    """Frozen refresh from f8811f13; production evaluation/formulas remain shared."""
    rows = c.execute('SELECT * FROM paper_positions' + (' WHERE portfolio_name=%s' if name else ''),
                     (name,) if name else ()).fetchall()
    if not rows:
        return
    accounts = PP.load_accounts(c, [z['active_trade_id'] for z in rows], include_payload=False)
    for item in rows:
        z = dict(item)
        patch = PP.evaluate(z, accounts.get(z['active_trade_id']), now=now, commission=commission)
        serialized = json.dumps(patch)
        c.execute("UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE active_trade_id=%s",
                  (serialized, z['active_trade_id']))
        c.execute("UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s",
                  (serialized, z['active_trade_id']))


class ReadTrace:
    """Encoded result sizes and update parameters, not protocol/wire traffic."""
    def __init__(self, connection):
        self.connection = connection
        self.reads, self.updates = [], []

    def execute(self, query, params=()):
        started = time.perf_counter()
        cursor = self.connection.execute(query, params)
        if query.startswith('SELECT'):
            owner = self
            class Rows:
                def fetchall(self):
                    rows = cursor.fetchall()
                    elapsed = time.perf_counter()-started
                    owner.reads.append({'encoded_result_bytes': len(json.dumps(rows, default=str).encode()),
                                        'seconds': elapsed, 'rows': len(rows),
                                        'columns': [sorted(row) for row in rows],
                                        'integrity_present': [
                                            'data_integrity_status' in row['payload']
                                            if isinstance(row.get('payload'), dict) else None for row in rows]})
                    return rows
            return Rows()
        if query.startswith('UPDATE'):
            self.updates.append((query, params))
        return cursor

    def logical_updates(self):
        """Compare every saved delta independently of SQL batching/order."""
        out = []
        for query, params in self.updates:
            table = query.split()[1]
            if 'jsonb_to_recordset' in query:
                out.extend((table, row['trade_id'], row['patch'])
                           for row in json.loads(params[0]))
            else:
                out.append((table, params[1], json.loads(params[0])))
        return sorted(out, key=lambda row: (row[0], row[1]))


@unittest.skipUnless(DSN, 'isolated PostgreSQL test database not configured')
class ProfitProtectionProjectionSQLTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        self.driver, self.sql, self.row_factory = psycopg, sql, dict_row
        self.schema = 'profit_projection_test_'+uuid.uuid4().hex
        self.now = datetime(2026, 10, 7, 19, 30, tzinfo=timezone.utc)
        with self.driver.connect(DSN, row_factory=self.row_factory) as c:
            self.verify_database(c)
            c.execute(self.sql.SQL('CREATE SCHEMA {}').format(self.sql.Identifier(self.schema)))
            c.execute(self.sql.SQL('SET search_path TO {}').format(self.sql.Identifier(self.schema)))
            c.execute('''CREATE TABLE paper_positions (
                portfolio_name text, asset text, direction text, units float8,
                avg_entry_price float8, last_price float8, stop_price float8,
                target_fraction float8, opened_at timestamptz, updated_at timestamptz,
                active_trade_id text, payload jsonb, PRIMARY KEY(portfolio_name,asset))''')
            c.execute('''CREATE TABLE paper_trades (
                trade_id text PRIMARY KEY, portfolio_name text, status text, opened_at timestamptz,
                gross_pnl_rub float8, fees_rub float8, funding_rub float8,
                net_pnl_rub float8, payload jsonb)''')
            c.execute('''CREATE TABLE paper_portfolios (
                name text PRIMARY KEY, initial_nav_rub float8, realized_pnl_rub float8,
                fees_rub float8, funding_rub float8, last_mark_at timestamptz,last_ruonia float8)''')
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

    def seed(self, c, rows):
        c.execute('TRUNCATE paper_positions,paper_trades,paper_portfolios,paper_orders,paper_nav_history')
        for row in rows:
            name, payload = row['name'], row['payload']
            direction = row.get('direction', 'LONG')
            encoded = None if payload is SQL_NULL else json.dumps(payload, ensure_ascii=False)
            opened = row.get('opened_at', self.now-timedelta(days=3))
            trade_opened = row.get('trade_opened_at', opened)
            trade_payload = row.get('trade_payload', {'saved_trade_evidence': {'preserved': True}})
            encoded_trade = None if trade_payload is SQL_NULL else json.dumps(trade_payload, ensure_ascii=False)
            c.execute('''INSERT INTO paper_positions VALUES
                (%s,'ETH',%s,10,100,%s,%s,.2,%s,%s,%s,%s::jsonb)''',
                (name, direction, row.get('mark', 110. if direction == 'LONG' else 90.),
                 row.get('stop', 105. if direction == 'LONG' else 95.), opened,
                 self.now-timedelta(minutes=1), name+'-trade', encoded))
            c.execute('INSERT INTO paper_trades VALUES (%s,%s,%s,%s,%s,2,3,7,%s::jsonb)',
                      (name+'-trade', name, row.get('status', 'OPEN'), trade_opened,
                       row.get('gross', 12.), encoded_trade))
            c.execute('INSERT INTO paper_portfolios VALUES (%s,10000,12,2,3,%s,16)',
                      (name, row.get('last_mark_at', self.now)))
            c.execute('INSERT INTO paper_orders VALUES (%s,1000)', (name+'-order',))
            c.execute('INSERT INTO paper_nav_history VALUES (%s,10007)', (name,))

    def snapshot(self, c):
        return {table: c.execute('SELECT * FROM '+table+' ORDER BY 1,2').fetchall()
                for table in ('paper_positions', 'paper_trades', 'paper_portfolios',
                              'paper_orders', 'paper_nav_history')}

    def run_refresh(self, helper, rows, name):
        with self.connect() as c:
            self.seed(c, rows)
            before, trace, error = self.snapshot(c), ReadTrace(c), None
            try:
                with c.transaction():
                    helper(trace, name=name, now=self.now)
            except (TypeError, ValueError, AttributeError) as exc:
                error = (type(exc).__name__, str(exc))
            after = self.snapshot(c)
            for table in before:
                old = [{k: v for k, v in row.items() if k != 'payload'} for row in before[table]]
                new = [{k: v for k, v in row.items() if k != 'payload'} for row in after[table]]
                self.assertEqual(new, old, table)
            return before, after, error, trace

    def assert_parity(self, rows, name=None):
        old = self.run_refresh(legacy_refresh, rows, name)
        new = self.run_refresh(PP.refresh, rows, name)
        self.assertEqual(new[:3], old[:3])
        self.assertEqual(new[3].logical_updates(), old[3].logical_updates())
        self.assertEqual(len(new[3].reads), len(old[3].reads))
        return old, new

    def test_large_immutable_evidence_both_directions_and_named_scope(self):
        proof = {'canonical': [{'id': n, 'context': 'proof-'+str(n)*128} for n in range(1500)]}
        rows = [{'name': d, 'direction': d, 'payload': {'entry_event_snapshot': proof,
                 'preserve': [None, {'text': 'первоначальный журнал'}],
                 'trailing_stop': 106. if d == 'LONG' else 94.},
                 'trade_payload': {'entry_event_snapshot': proof}} for d in ('LONG', 'SHORT')]
        for name in (None, 'LONG', 'missing', ''):
            with self.subTest(scope=name):
                old, new = self.assert_parity(rows, name=name)
                selected = 0 if name == 'missing' else 1 if name == 'LONG' else 2
                self.assertIsNone(new[2])
                self.assertEqual(len(new[3].updates), 2*((selected+PR.BATCH_SIZE-1)//PR.BATCH_SIZE))
                for table in ('paper_positions', 'paper_trades'):
                    for row in new[1][table]:
                        self.assertEqual(row['payload']['entry_event_snapshot'], proof)
                        if selected and (not name or row['portfolio_name'] == name):
                            self.assertEqual(row['payload']['net_profit_protection']['state'], 'PROTECTED')
                if selected:
                    self.assertGreater(old[3].reads[0]['encoded_result_bytes'], 500000)
                    self.assertLess(new[3].reads[0]['encoded_result_bytes'], 2048)
                    self.assertEqual(new[3].reads[0]['columns'], [sorted((
                        'asset', 'direction', 'units', 'avg_entry_price', 'last_price',
                        'stop_price', 'opened_at', 'active_trade_id', 'payload'))]*selected)
                if name is None:
                    print('profit protection fixture encoding '+json.dumps({label: {
                        'encoded_position_result_bytes': result[3].reads[0]['encoded_result_bytes'],
                        'position_query_seconds': round(result[3].reads[0]['seconds'], 6)}
                        for label, result in (('legacy', old), ('projected', new))}))

    def test_integrity_absence_and_explicit_null_keep_distinct_results(self):
        cases = [({}, 'PROTECTED', False), ({'data_integrity_status': None}, 'UNAVAILABLE', True),
                 ({'data_integrity_status': ''}, 'PROTECTED', True),
                 ({'data_integrity_status': 'OK'}, 'PROTECTED', True),
                 ({'data_integrity_status': 'DAMAGED'}, 'UNAVAILABLE', True)]
        for payload, expected, present in cases:
            with self.subTest(payload=payload):
                _, new = self.assert_parity([{'name': 'status', 'payload': payload}])
                self.assertIsNone(new[2])
                self.assertEqual(new[3].reads[0]['integrity_present'], [present])
                for table in ('paper_positions', 'paper_trades'):
                    self.assertEqual(new[1][table][0]['payload']['net_profit_protection']['state'], expected)

    def test_trailing_entry_time_and_accounting_gates_match(self):
        cases = [
            ({'payload': {'trailing_stop': 106.}}, 'PROTECTED', 106.),
            ({'payload': {'trailing_stop': None}}, 'PROTECTED', 105.),
            ({'payload': {}, 'mark': 104.}, 'STOP_REACHED', 105.),
            ({'payload': {}, 'gross': -100.}, 'COSTS_NOT_COVERED', 105.),
            ({'payload': {}, 'status': 'CLOSED'}, 'UNAVAILABLE', 105.),
            ({'payload': {}, 'last_mark_at': None}, 'UNAVAILABLE', 105.),
            ({'payload': {'entry_time': (self.now-timedelta(days=2)).isoformat()},
              'opened_at': None, 'trade_opened_at': None}, 'PROTECTED', 105.),
            ({'payload': {}, 'opened_at': None, 'trade_opened_at': None}, 'UNAVAILABLE', 105.),
        ]
        for row, expected, stop in cases:
            with self.subTest(case=row):
                _, new = self.assert_parity([dict(row, name='gates')])
                self.assertIsNone(new[2])
                result = new[1]['paper_positions'][0]['payload']['net_profit_protection']
                self.assertEqual(result['state'], expected)
                self.assertEqual(result['stop_price'], stop)

    def test_nonobject_and_malformed_shapes_preserve_errors_and_merge_semantics(self):
        encoded = json.dumps({'trailing_stop': 106., 'original': {'keep': [None, 'text']}})
        shapes = [SQL_NULL, None, False, True, 0, 7, [], [['trailing_stop', 106.]],
                  '', 'not-json', 'null', '[]', '[1]', 'false', '3', encoded,
                  {'trailing_stop': {'invalid': True}}, {'entry_time': 'not-a-time'},
                  {'data_integrity_status': {'invalid': True}}]
        trade_shapes = [SQL_NULL, None, 'trade-string', ['prior'], {'prior': True}]
        errors = 0
        for n, payload in enumerate(shapes):
            with self.subTest(shape=n):
                _, new = self.assert_parity([{'name': 'shape', 'payload': payload,
                                             'trade_payload': trade_shapes[n % len(trade_shapes)]}])
                if new[2] is not None:
                    errors += 1
                    self.assertEqual(new[1], new[0])
                else:
                    self.assertEqual(len(new[3].updates), 2)
        self.assertGreater(errors, 5, 'invalid shapes must retain the legacy fail-closed exceptions')


if __name__ == '__main__':
    unittest.main()
