"""Real PostgreSQL parity for bounded protective I/O and full exit accounting.

Only an isolated veritas_quality_test database is writable. Synthetic structural
quotes exercise production source gates, stops, targets, fees, funding and NAV;
they are lifecycle fixtures, not historical trade or profitability evidence.
"""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from veritas_book_lock import PriorityRLock
import veritas_observation_path as PATH
import veritas_portfolio as P
import veritas_position_guard as G
import veritas_price_source as S
import veritas_profit_protection as PP
import veritas_structural_breakout as SB
import veritas_structural_lifecycle as SL
from test_veritas_structural_breakout import POLICY, raw_at
from test_veritas_structural_management import mirror


DSN = os.getenv('VERITAS_QUALITY_TEST_DSN', '')
SQL_NULL = object()
BASELINE = Path(__file__).parent / 'tests/fixtures/guard_pre_batch_0e4ace8.py'
TABLE_KEYS = (('paper_portfolios', 'name'), ('paper_positions', 'active_trade_id'),
              ('paper_trades', 'trade_id'), ('paper_orders', 'order_id'),
              ('paper_nav_history', 'portfolio_name'))


def legacy_refresh(c, name=None, now=None, commission=PP.VC.COMMISSION_RATE):
    """Pre-batch #95 projected reads, unchanged production calculation."""
    rows = c.execute(PP.REFRESH_POSITIONS_SQL + (' WHERE portfolio_name=%s' if name else ''),
                     (name,) if name else ()).fetchall()
    if not rows:
        return
    accounts = PP.load_accounts(c, [z['active_trade_id'] for z in rows], include_payload=False)
    for row in rows:
        encoded = json.dumps(PP.evaluate(dict(row), accounts.get(row['active_trade_id']),
                                         now=now, commission=commission))
        c.execute("UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE active_trade_id=%s",
                  (encoded, row['active_trade_id']))
        c.execute("UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s",
                  (encoded, row['active_trade_id']))


class GuardTrace:
    """Trace statement count/result encoding; no claim about wire bytes."""
    def __init__(self, connection):
        self.connection = connection
        self.statements, self.position_reads, self.nested_transactions = [], [], 0

    def __getattr__(self, name):
        return getattr(self.connection, name)

    @contextmanager
    def transaction(self):
        self.nested_transactions += 1
        with self.connection.transaction():
            yield self

    def execute(self, query, params=None):
        normalized = ' '.join(query.split())
        self.statements.append(normalized)
        cursor = self.connection.execute(query, params)
        if normalized.startswith('SELECT') and 'FOR UPDATE' in normalized:
            owner = self
            class Rows:
                def fetchall(self):
                    rows = cursor.fetchall()
                    owner.position_reads.append(len(json.dumps(rows, default=str).encode()))
                    return rows
            return Rows()
        return cursor


@unittest.skipUnless(DSN, 'isolated PostgreSQL test database not configured')
class GuardObservationBatchSQLTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        self.driver, self.sql, self.row_factory = psycopg, sql, dict_row
        self.schema = 'guard_batch_test_' + uuid.uuid4().hex
        self.clock = datetime(2026, 1, 2, 2, 0, 30, 100000, tzinfo=timezone.utc)
        self.proof = {'retained_original_decision': 'original-proof|' * 10000,
                      'nested': [None, True, 0, {'text': 'первоначальное решение'}]}
        self.enterContext(patch.object(G, '_mutex', PriorityRLock()))
        for cache in (G._quotes, G._source_quotes, G._market_state):
            self.enterContext(patch.dict(cache, {}, clear=True))
        owner = self
        class ReplayClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return owner.clock if tz else owner.clock.replace(tzinfo=None)
        self.enterContext(patch.object(G, 'datetime', ReplayClock))
        self.enterContext(patch.object(PP, 'datetime', ReplayClock))
        # The notification outbox is outside the accounting scope of this
        # fixture. No provider, broker, transport or sender is called.
        self.enterContext(patch.object(P.VCN, 'enqueue_order'))
        with self.driver.connect(DSN, row_factory=self.row_factory) as c:
            self.verify_database(c)
            c.execute(self.sql.SQL('CREATE SCHEMA {}').format(self.sql.Identifier(self.schema)))
            c.execute(self.sql.SQL('SET search_path TO {}').format(self.sql.Identifier(self.schema)))
            c.execute('''CREATE TABLE paper_portfolios (
                name text PRIMARY KEY,created_at timestamptz,updated_at timestamptz,
                initial_nav_rub float8,realized_pnl_rub float8 DEFAULT 0,
                fees_rub float8 DEFAULT 0,funding_rub float8 DEFAULT 0,
                benchmark_nav_rub float8,high_water_nav_rub float8,last_ruonia float8,
                last_usdrub float8,last_mark_at timestamptz,policy jsonb,model_version text)''')
            c.execute('''CREATE TABLE paper_positions (
                portfolio_name text,asset text,direction text,units float8,avg_entry_price float8,
                opened_at timestamptz,updated_at timestamptz,active_trade_id text,
                stop_price float8,target_fraction float8,last_price float8,payload jsonb,
                PRIMARY KEY(portfolio_name,asset))''')
            c.execute('''CREATE TABLE paper_trades (
                trade_id text PRIMARY KEY,portfolio_name text,asset text,direction text,
                opened_at timestamptz,closed_at timestamptz,avg_entry_price float8,
                avg_exit_price float8,max_fraction float8,gross_pnl_rub float8 DEFAULT 0,
                fees_rub float8 DEFAULT 0,funding_rub float8 DEFAULT 0,net_pnl_rub float8,
                return_on_entry_nav float8,profitable boolean,meaningful_win boolean,
                status text,setup text,horizon text,payload jsonb)''')
            c.execute('''CREATE TABLE paper_orders (
                order_id bigserial PRIMARY KEY,portfolio_name text,trade_id text,
                created_at timestamptz,asset text,side text,price float8,notional_rub float8,
                fee_rub float8,fraction_nav float8,reason text,payload jsonb,
                client_order_id text UNIQUE)''')
            c.execute('''CREATE TABLE paper_nav_history (
                portfolio_name text,observed_at timestamptz,nav_rub float8,nav_usd float8,
                benchmark_nav_rub float8,gross_leverage float8,net_exposure float8,
                drawdown float8,ruonia float8,usdrub float8,payload jsonb,
                PRIMARY KEY(portfolio_name,observed_at))''')
        self.addCleanup(self.drop_schema)

    @staticmethod
    def verify_database(c):
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

    def fixture(self, direction='LONG', count=23, asset='ETH', prefix='Book'):
        raw, opened = raw_at(asset=asset)
        # Preserve the real swing/event geometry while keeping both synthetic
        # target legs within the existing per-quote jump gate.
        for key in ('price', 'best_bid', 'best_ask'):
            raw[key] = 100. + (raw[key]-100.)*.5
        for bars in raw['structure_bars_by_timeframe'].values():
            for bar in bars:
                for key in ('open', 'high', 'low', 'close'):
                    bar[key] = 100. + (bar[key]-100.)*.5
        if direction == 'SHORT':
            raw = mirror(raw)
        context = SB.build_context(raw, '1m', opened, config=POLICY)
        event = context['event']
        self.assertTrue(context['entry_gate']['eligible'], context)
        self.assertEqual(event['direction'], direction)
        quote = S.quote_from_row(raw)
        payload = {'structural_policy_version': event['policy']['version'],
                   'execution_horizon': '1m', 'management_horizon': '1h',
                   'entry_event_snapshot': deepcopy(event),
                   'entry_decision_snapshot': deepcopy(self.proof),
                   'timeframe_entry_context': deepcopy(context),
                   'price_source_lock': deepcopy(event['source_identity']),
                   'entry_execution_observed_at': opened.isoformat(),
                   'entry_execution_model': {'fill_price': raw['price']},
                   'entry_nav_rub': 1000000., 'entry_atr': event['atr'],
                   'initial_stop_price': event['stop_price'],
                   'target_price': event['target_price'],
                   'preserved_position': {'token': 'position-original'}}
        payload.update(SL.entry_metadata(dict(raw, horizon='1m', timeframe_entry_context=context), 1000.))
        rows = []
        for index in range(count):
            name = f'{prefix}{index:03}'
            row = {'portfolio_name': name, 'asset': asset, 'direction': direction,
                   'units': 1000., 'avg_entry_price': raw['price'], 'last_price': raw['price'],
                   'stop_price': event['stop_price'], 'target_fraction': .1,
                   'opened_at': opened, 'updated_at': opened,
                   'active_trade_id': name+'-'+asset, 'payload': deepcopy(payload)}
            for second in (0, 15):
                mark = dict(quote, observed_at=(opened+timedelta(seconds=second)).isoformat())
                row['payload']['observation_path'] = PATH.observe(
                    row, mark, mark['observed_at'], at_entry=second == 0)
            self.assertTrue(SL.owns_position(row))
            rows.append(row)
        self.clock = opened+timedelta(seconds=30)
        quote.update(price=raw['price']+(.1 if direction == 'LONG' else -.1),
                     observed_at=self.clock.isoformat())
        quote.update(best_bid=quote['price']-.005, best_ask=quote['price']+.005)
        return rows, quote

    def insert(self, c, table, row):
        values, placeholders = [], []
        for key, value in row.items():
            if key in ('payload', 'policy'):
                values.append(None if value is SQL_NULL else json.dumps(value, ensure_ascii=False))
                placeholders.append(self.sql.SQL('%s::jsonb'))
            else:
                values.append(value)
                placeholders.append(self.sql.Placeholder())
        c.execute(self.sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(
            self.sql.Identifier(table), self.sql.SQL(',').join(map(self.sql.Identifier, row)),
            self.sql.SQL(',').join(placeholders)), values)

    def seed(self, rows):
        with self.connect() as c:
            c.execute('TRUNCATE paper_portfolios,paper_positions,paper_trades,paper_orders,paper_nav_history RESTART IDENTITY')
            c.execute('ALTER TABLE paper_trades DROP CONSTRAINT IF EXISTS reject_guard_metadata')
            names = sorted({row['portfolio_name'] for row in rows})
            for name in names:
                members = [row for row in rows if row['portfolio_name'] == name]
                opened = min(row['opened_at'] for row in members)
                self.insert(c, 'paper_portfolios', dict(name=name, created_at=opened, updated_at=opened,
                    initial_nav_rub=1000000., realized_pnl_rub=0., fees_rub=float(len(members)),
                    funding_rub=0., benchmark_nav_rub=1000000., high_water_nav_rub=1000000.,
                    last_ruonia=16., last_usdrub=80., last_mark_at=opened+timedelta(seconds=15),
                    policy={'preserved': True}, model_version='synthetic-test'))
                self.insert(c, 'paper_nav_history', dict(portfolio_name=name, observed_at=opened,
                    nav_rub=1000000.-len(members), nav_usd=None, benchmark_nav_rub=1000000.,
                    gross_leverage=.1*len(members), net_exposure=.1*len(members),
                    drawdown=0., ruonia=16., usdrub=80., payload={'preserved_nav': True}))
            for row in rows:
                self.insert(c, 'paper_positions', row)
                saved = deepcopy(row['payload']) if row['payload'] is not SQL_NULL else SQL_NULL
                if isinstance(saved, dict):
                    saved['preserved_trade'] = {'token': 'trade-original'}
                self.insert(c, 'paper_trades', dict(trade_id=row['active_trade_id'],
                    portfolio_name=row['portfolio_name'], asset=row['asset'], direction=row['direction'],
                    opened_at=row['opened_at'], avg_entry_price=row['avg_entry_price'],
                    max_fraction=.1, gross_pnl_rub=0., fees_rub=1., funding_rub=0., net_pnl_rub=-1.,
                    status='OPEN', horizon='1m', setup='STRUCTURAL_FIXTURE', payload=saved))
                self.insert(c, 'paper_orders', dict(portfolio_name=row['portfolio_name'],
                    trade_id=row['active_trade_id'], created_at=row['opened_at'], asset=row['asset'],
                    side='BUY' if row['direction'] == 'LONG' else 'SELL_SHORT',
                    price=row['avg_entry_price'], notional_rub=row['units']*row['avg_entry_price'],
                    fee_rub=1., fraction_nav=.1, reason='SYNTHETIC_ENTRY_FIXTURE',
                    payload={'preserved_order': True}, client_order_id='initial-'+row['active_trade_id']))

    def snapshot(self, c=None):
        if c is None:
            with self.connect() as connection:
                return self.snapshot(connection)
        return {table: c.execute(self.sql.SQL('SELECT * FROM {} ORDER BY {}, 2').format(
            self.sql.Identifier(table), self.sql.Identifier(key))).fetchall()
            for table, key in TABLE_KEYS}

    @staticmethod
    def baseline_guard():
        namespace = dict(vars(G))
        exec(compile(BASELINE.read_text(), str(BASELINE), 'exec'), namespace)
        return namespace['run_protective_pass']

    def pass_once(self, quotes, *, legacy=False, connection=None):
        for cache in (G._quotes, G._source_quotes, G._market_state):
            cache.clear()
        for asset, quote in quotes.items():
            G.publish_quote(asset, quote)
        traces = []
        @contextmanager
        def traced_connect():
            with self.connect() if connection is None else nullcontext(connection) as c:
                traced = GuardTrace(c)
                traces.append(traced)
                yield traced
        guard = self.baseline_guard() if legacy else G.run_protective_pass
        with patch.object(PP, 'refresh', legacy_refresh) if legacy else nullcontext():
            changes = guard(P, traced_connect, quotes, self.clock)
        return changes, traces[0]

    def run_case(self, rows, quotes, *, legacy=False):
        self.seed(rows)
        before = self.snapshot()
        try:
            changes, trace = self.pass_once(quotes, legacy=legacy)
            error = None
        except (TypeError, ValueError, AttributeError) as exc:
            changes, trace, error = None, None, (type(exc).__name__, str(exc))
        return before, self.snapshot(), changes, error, trace

    def assert_parity(self, rows, quotes):
        old = self.run_case(rows, quotes, legacy=True)
        new = self.run_case(rows, quotes)
        self.assertEqual(new[:4], old[:4])
        return old, new

    def test_twenty_three_no_action_positions_preserve_full_payload_and_reduce_io(self):
        for direction in ('LONG', 'SHORT'):
            with self.subTest(direction=direction):
                rows, quote = self.fixture(direction)
                old, new = self.assert_parity(rows, {'ETH': quote})
                self.assertIsNone(new[3])
                self.assertEqual(new[2], [])
                for table, _ in TABLE_KEYS:
                    for before, after in zip(new[0][table], new[1][table]):
                        self.assertEqual({k: v for k, v in before.items() if k != 'payload'},
                                         {k: v for k, v in after.items() if k != 'payload'}, table)
                        if table in ('paper_positions', 'paper_trades'):
                            for key, value in before['payload'].items():
                                if key != 'observation_path':
                                    self.assertEqual(after['payload'][key], value, key)
                            self.assertEqual(after['payload']['observation_path']['observation_count'], 3)
                            self.assertTrue(PATH.assessment(dict(after, horizon='1m'))['eligible'])
                old_updates = sum(q.startswith('UPDATE') for q in old[4].statements)
                new_updates = sum(q.startswith('UPDATE') for q in new[4].statements)
                self.assertEqual(old_updates, 46)
                self.assertLessEqual(new_updates, 4)
                self.assertLessEqual(new[4].nested_transactions, 3)
                self.assertLess(new[4].position_reads[0], old[4].position_reads[0]/4)
                print('protective I/O fixture '+json.dumps({'direction': direction,
                    'legacy_updates': old_updates, 'batch_updates': new_updates,
                    'legacy_encoded_read_bytes': old[4].position_reads[0],
                    'projected_encoded_read_bytes': new[4].position_reads[0]}))

    def test_stale_future_and_foreign_contract_quotes_cannot_write_metadata(self):
        rows, quote = self.fixture(count=3)
        variants = [dict(quote, observed_at=(self.clock-timedelta(seconds=121)).isoformat()),
                    dict(quote, observed_at=(self.clock+timedelta(seconds=6)).isoformat()),
                    dict(quote, contract={'instrument_uid': 'FOREIGN'})]
        for candidate in variants:
            with self.subTest(quote=candidate):
                _, new = self.assert_parity(rows, {'ETH': candidate})
                self.assertEqual(new[1], new[0])
                self.assertEqual(new[2], [])

    def test_absent_explicit_null_and_malformed_payloads_retain_legacy_semantics(self):
        rows, quote = self.fixture(count=1)
        source = deepcopy(rows[0]['payload'])
        shapes = [source, dict(source, trailing_stop=None, data_integrity_status=None),
                  dict(source, structural_policy_version=None),
                  dict(source, active_target_stage=None), dict(source, trailing_stop={'bad': True}),
                  dict(source, observation_path={'version': PATH.VERSION, 'observation_count': 'bad'}),
                  SQL_NULL, None, [], [source], False, 'not-json', json.dumps(source)]
        for index, payload in enumerate(shapes):
            with self.subTest(shape=index):
                changed = deepcopy(rows)
                changed[0]['payload'] = payload
                self.assert_parity(changed, {'ETH': quote})

    def test_quote_prepass_preserves_always_present_null_keys_and_unusual_json_shapes(self):
        rows, _ = self.fixture(count=1)
        original = rows[0]['payload']
        shapes = [original, {}, dict(original, price_source_lock=None, source_locked_mark=None),
                  dict(original, source_locked_mark={'observed_at': None, 'ignored': self.proof}),
                  dict(original, source_locked_mark=[]), SQL_NULL, None,
                  0, False, [], [original], 'not-json', json.dumps(original)]
        legacy = self.baseline_guard().__globals__['QUOTE_POSITION_SQL']
        for index, payload in enumerate(shapes):
            with self.subTest(shape=index):
                changed = deepcopy(rows)
                changed[0]['payload'] = payload
                self.seed(changed)
                with self.connect() as c:
                    before = self.snapshot(c)
                    expected = c.execute(legacy).fetchall()
                    actual = c.execute(G.QUOTE_POSITION_SQL).fetchall()
                    self.assertEqual(actual, expected)
                    self.assertEqual(self.snapshot(c), before)
                    self.assertEqual(set(actual[0]['payload']), {
                        'price_source_lock', 'contract_identity', 'entry_primary_source',
                        'entry_contract_secid', 'source_locked_mark',
                        'entry_execution_observed_at', 'entry_market_observed_at'})

    def reject_metadata(self, c, trade_id):
        c.execute(self.sql.SQL('''ALTER TABLE paper_trades ADD CONSTRAINT reject_guard_metadata
            CHECK (trade_id <> {} OR
                   (payload #>> '{{observation_path,last_lane}}') IS DISTINCT FROM 'PROTECTIVE_GUARD' OR
                   COALESCE((payload #>> '{{observation_path,observation_count}}')::int, 0) < 3)''')
            .format(self.sql.Literal(trade_id)))

    def test_one_optional_failure_preserves_other_twenty_two_and_outer_accounting(self):
        rows, quote = self.fixture()
        failed_id = rows[7]['active_trade_id']
        outcomes = []
        for legacy in (True, False):
            self.seed(rows)
            before = self.snapshot()
            with self.connect() as c, c.transaction():
                self.reject_metadata(c, failed_id)
                c.execute('UPDATE paper_trades SET fees_rub=0.04 WHERE trade_id=%s', (failed_id,))
                changes, _ = self.pass_once({'ETH': quote}, legacy=legacy, connection=c)
                self.assertEqual(changes, [])
                self.assertEqual(c.info.transaction_status, self.driver.pq.TransactionStatus.INTRANS)
                c.execute('UPDATE paper_trades SET net_pnl_rub=-10 WHERE trade_id=%s', (failed_id,))
            after = self.snapshot()
            for table in ('paper_positions', 'paper_trades'):
                for old, new in zip(before[table], after[table]):
                    tid = new.get('active_trade_id') or new.get('trade_id')
                    self.assertEqual(new['payload']['observation_path']['observation_count'],
                                     2 if tid == failed_id else 3)
                    if tid == failed_id:
                        self.assertEqual(new['payload'], old['payload'])
            rejected = next(r for r in after['paper_trades'] if r['trade_id'] == failed_id)
            self.assertEqual((rejected['fees_rub'], rejected['net_pnl_rub']), (.04, -10.))
            outcomes.append(after)
        self.assertEqual(outcomes[0], outcomes[1])

    def test_outer_rollback_removes_successful_metadata_batch_and_accounting(self):
        rows, quote = self.fixture()
        self.seed(rows)
        before = self.snapshot()
        with self.connect() as c:
            with self.assertRaisesRegex(RuntimeError, 'outer accounting failure'):
                with c.transaction():
                    self.pass_once({'ETH': quote}, connection=c)
                    self.assertNotEqual(self.snapshot(c), before)
                    c.execute('UPDATE paper_trades SET net_pnl_rub=-10')
                    raise RuntimeError('outer accounting failure')
        self.assertEqual(self.snapshot(), before)

    def test_required_second_table_failure_rolls_back_prior_accounting_and_first_table(self):
        import veritas_protective_io as PIO
        rows, _ = self.fixture(count=3)
        self.seed(rows)
        before = self.snapshot()
        with self.connect() as c:
            c.execute('''ALTER TABLE paper_trades ADD CONSTRAINT reject_guard_metadata
                         CHECK (payload->>'required_batch_marker' IS DISTINCT FROM 'reject')''')
            with self.assertRaises(self.driver.errors.CheckViolation):
                with c.transaction():
                    c.execute('UPDATE paper_trades SET fees_rub=0.04')
                    PIO.write_patches(c, [(row['active_trade_id'],
                        {'required_batch_marker': 'reject'}) for row in rows])
        self.assertEqual(self.snapshot(), before)

    def test_optional_guard_metadata_failure_does_not_suppress_a_real_stop(self):
        rows, quote = self.fixture(count=1)
        self.clock = rows[0]['opened_at']+timedelta(days=4, seconds=30)
        price = rows[0]['stop_price']-.01
        quote.update(price=price, best_bid=price-.005, best_ask=price+.005,
                     observed_at=self.clock.isoformat())
        outcomes = []
        for legacy in (True, False):
            self.seed(rows)
            with self.connect() as c:
                self.reject_metadata(c, rows[0]['active_trade_id'])
            changes, _ = self.pass_once({'ETH': quote}, legacy=legacy)
            after = self.snapshot()
            self.assertEqual([row['reason'] for row in changes], ['STOP'])
            self.assertEqual(after['paper_positions'], [])
            self.assertEqual(after['paper_trades'][0]['status'], 'CLOSED')
            self.assertEqual(after['paper_trades'][0]['payload']['entry_decision_snapshot'], self.proof)
            self.assertEqual(after['paper_trades'][0]['payload']['observation_path']['last_lane'],
                             'CANONICAL_EXIT')
            outcomes.append((changes, after))
        self.assertEqual(outcomes[0], outcomes[1])

    def test_real_stop_closes_both_assets_with_identical_funding_and_ledger(self):
        for direction in ('LONG', 'SHORT'):
            with self.subTest(direction=direction):
                rows, _ = self.fixture(direction, count=1, asset='ETH', prefix='Shared')
                other, _ = self.fixture(direction, count=1, asset='BTC', prefix='Shared')
                rows += other
                self.clock = rows[0]['opened_at']+timedelta(days=4, seconds=30)
                quotes = {}
                for row in rows:
                    mark = row['stop_price']+(-.01 if direction == 'LONG' else .01)
                    raw, _ = raw_at(asset=row['asset'])
                    quotes[row['asset']] = dict(S.quote_from_row(raw), price=mark,
                        best_bid=mark-.005, best_ask=mark+.005, observed_at=self.clock.isoformat())
                _, new = self.assert_parity(rows, quotes)
                self.assertEqual(new[1]['paper_positions'], [])
                self.assertEqual([r['reason'] for r in new[2]], ['STOP', 'STOP'])
                self.assertEqual(len(new[1]['paper_orders']), 4)
                for trade in new[1]['paper_trades']:
                    self.assertEqual(trade['status'], 'CLOSED')
                    self.assertGreater(trade['funding_rub'], 0.)
                    self.assertAlmostEqual(trade['net_pnl_rub'],
                        trade['gross_pnl_rub']-trade['fees_rub']-trade['funding_rub'])
                    self.assertEqual(trade['payload']['entry_decision_snapshot'], self.proof)

    def test_real_partial_then_final_target_retains_frozen_evidence_and_exact_ledger(self):
        for direction in ('LONG', 'SHORT'):
            with self.subTest(direction=direction):
                rows, quote = self.fixture(direction, count=1)
                opened = rows[0]['opened_at']
                ladder = SL.active_ladder(rows[0])
                outcomes = []
                for legacy in (True, False):
                    self.seed(rows)
                    stages = []
                    for index, target in enumerate(ladder):
                        self.clock = opened+timedelta(days=4, seconds=30+15*index)
                        price = target['price']
                        mark = dict(quote, price=price, best_bid=price-.005, best_ask=price+.005,
                                    observed_at=self.clock.isoformat())
                        changes, _ = self.pass_once({'ETH': mark}, legacy=legacy)
                        self.assertTrue(any(r['reason'] == 'TAKE_PROFIT' for r in changes), changes)
                        snapshot = self.snapshot()
                        if index == 0:
                            self.assertEqual(len(snapshot['paper_positions']), 1)
                            residual = snapshot['paper_positions'][0]
                            self.assertAlmostEqual(residual['units'], 500.)
                            self.assertEqual(residual['payload']['active_target_stage'], 1)
                            self.assertEqual(residual['payload']['entry_decision_snapshot'], self.proof)
                            self.assertEqual(residual['payload']['preserved_position'],
                                             {'token': 'position-original'})
                        else:
                            self.assertEqual(snapshot['paper_positions'], [])
                        stages.append((changes, snapshot))
                    outcomes.append(stages)
                self.assertEqual(outcomes[0], outcomes[1])
                trade = outcomes[1][-1][1]['paper_trades'][0]
                self.assertEqual(trade['status'], 'CLOSED')
                self.assertGreater(trade['funding_rub'], 0.)
                self.assertAlmostEqual(trade['net_pnl_rub'],
                    trade['gross_pnl_rub']-trade['fees_rub']-trade['funding_rub'])


if __name__ == '__main__':
    unittest.main()
