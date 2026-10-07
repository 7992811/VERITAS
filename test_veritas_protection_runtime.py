"""Synthetic protective reads, bounded writes and transaction-boundary checks.

Prices, quantities, trade names and clocks below are invented fixtures. No
provider, broker, production database or main-service import is used.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import veritas_canonical_constitution as CTC
import veritas_observation_path as OP
import veritas_position_guard as G
import veritas_price_source as S
import veritas_profit_protection as PP
import veritas_protection_read_model as PR
import veritas_structural_breakout as SB
import veritas_structural_lifecycle as SL
from test_veritas_structural_breakout import POLICY, raw_at


NOW = datetime(2001, 2, 3, 9, 0, tzinfo=timezone.utc)


def position(index=0, *, structural=False):
    q = dict(price=101., best_bid=101., best_ask=101.01,
             observed_at=NOW.isoformat(), source_gate_pass=True,
             source_names={'primary': 'Binance spot'}, market_open=True)
    identity = S.identity('ETH', q)
    z = dict(portfolio_name='Synthetic', asset='ETH', direction='LONG',
             units=3., avg_entry_price=100., last_price=101., stop_price=90.,
             opened_at=(NOW-timedelta(hours=2)).isoformat(),
             active_trade_id='synthetic-trade-'+str(index), payload={
                 'price_source_lock': identity, 'execution_horizon': '1h',
                 'entry_market_observed_at': (NOW-timedelta(hours=2)).isoformat(),
                 'entry_execution_model': {'fill_price': 100.},
                 'initial_stop_price': 90., 'entry_atr': 2.,
                 'target_price': 120., 'r55_lifetime_mfe_pct': 2.,
                 'mfe_pct': 1.5, 'mae_pct': -.75,
                 'immutable_history': {'sentinel': 'retained-in-ledger', 'padding': 'proof-'*50000}})
    if structural:
        # A structural marker is enough only for the legacy profit-lock no-op.
        # Valid sealed events for target-authority parity are built separately.
        z['payload']['structural_policy_version'] = CTC.BREAKOUT_LIFECYCLE_POLICY['version']
    return z, q


class ProjectionDatabase:
    """Execute the shipped SELECT expressions using SQLite JSON operators.

    PostgreSQL's JSON iterator/aggregate are mapped to SQLite's equivalents.
    The integration suite executes the unchanged SQL and locks on PostgreSQL.
"""
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.create_function('jsonb_typeof', 1, lambda value:
            'object' if isinstance(json.loads(value), dict) else 'other' if value else None)
        class ObjectAggregate:
            def __init__(self): self.result = {}
            def step(self, key, value): self.result[key] = json.loads(value)
            def finalize(self): return json.dumps(self.result)
        self.db.create_aggregate('jsonb_object_agg', 2, ObjectAggregate)
        self.db.execute('CREATE TABLE paper_positions ('+
                        ','.join(name+' TEXT' for name in PR.POSITION_COLUMNS)+',payload TEXT)')

    def project(self, z, sql=PR.PROTECTION_SQL):
        self.db.execute('DELETE FROM paper_positions')
        self.db.execute('INSERT INTO paper_positions VALUES ('+
                        ','.join('?' for _ in range(len(PR.POSITION_COLUMNS)+1))+')',
                        tuple(z.get(name) for name in PR.POSITION_COLUMNS)+(json.dumps(z.get('payload')),))
        # SQLite json_each exposes native scalar strings, so the arrow keeps
        # their JSON type intact for the PostgreSQL-style aggregate adapter.
        sql = (sql.replace('jsonb_each(', 'json_each(')
               .replace('item.key,item.value', 'item.key,payload->item.key')
               .replace("'{}'::jsonb", "'{}'"))
        result = dict(self.db.execute(sql).fetchone())
        for key in ('units', 'avg_entry_price', 'last_price', 'stop_price'):
            if result[key] is not None:
                result[key] = float(result[key])
        result['payload'] = json.loads(result['payload']) if result['payload'] is not None else None
        return result


class ProjectionParityTests(unittest.TestCase):
    def setUp(self):
        self.projection = ProjectionDatabase()
        self.addCleanup(self.projection.db.close)
        for cache in (G._quotes, G._source_quotes, G._market_state):
            self.enterContext(patch.dict(cache, {}, clear=True))

    def test_large_irrelevant_history_does_not_cross_protective_read(self):
        z, q = position()
        projected = self.projection.project(z)
        self.assertNotIn('immutable_history', projected['payload'])
        self.assertLess(len(json.dumps(projected)), len(json.dumps(z))/20)
        self.assertEqual(G.exit_execution_quote(dict(z, _execution_quote=q), NOW),
                         G.exit_execution_quote(dict(projected, _execution_quote=q), NOW))
        self.assertEqual(OP.observe(z, q, NOW), OP.observe(projected, q, NOW))
        z['payload']['immutable_history']['padding'] *= 5
        self.assertEqual(projected, self.projection.project(z))

    def test_long_short_stops_targets_and_quote_refusals_match_full_row(self):
        for direction, px, stop, target, expected in (
                ('LONG', 89., 90., 120., 'STOP'), ('SHORT', 111., 110., 80., 'STOP'),
                ('LONG', 102., 90., 102., 'TAKE_PROFIT'),
                ('SHORT', 98., 110., 98., 'TAKE_PROFIT'), ('LONG', 101., 90., 120., None)):
            z, q = position()
            z.update(direction=direction, stop_price=stop)
            z['payload']['target_price'] = target
            q.update(price=px, best_bid=px, best_ask=px+.01)
            projected = self.projection.project(z)
            self.assertEqual(G.protective_reason(z, q, NOW), expected)
            self.assertEqual(G.protective_reason(projected, q, NOW), expected)
            for bad in (dict(q, observed_at=(NOW-timedelta(hours=1)).isoformat()),
                        dict(q, source_names={'primary': 'Unrelated venue'}),
                        dict(q, source_gate_pass=False)):
                self.assertEqual(G.exit_execution_quote(dict(z, _execution_quote=bad), NOW), {})
                self.assertEqual(G.exit_execution_quote(dict(projected, _execution_quote=bad), NOW), {})

    def test_complete_sealed_events_are_kept_and_tampering_is_still_rejected(self):
        raw, clock = raw_at()
        event = SB.build_context(raw, '1h', clock, config=POLICY)['event']
        z, unused = position()
        z.update(asset='CNYRUBF', stop_price=event['stop_price'])
        z['payload'].update(price_source_lock=S.identity('CNYRUBF', raw),
            structural_policy_version=CTC.BREAKOUT_LIFECYCLE_POLICY['version'],
            entry_event_snapshot=event, active_target_event_snapshot=deepcopy(event),
            active_target_ladder=deepcopy(event['target_ladder']), active_target_stage=0)
        projected = self.projection.project(z)
        self.assertTrue(SL.owns_position(z))
        self.assertTrue(SL.owns_position(projected))
        self.assertEqual(SL.active_target_price(z), SL.active_target_price(projected))
        self.assertEqual(projected['payload']['entry_event_snapshot'], event)
        z['payload'].pop('active_target_stage')
        self.assertNotIn('active_target_stage', self.projection.project(z)['payload'])
        self.assertEqual(SL.active_target_price(z), SL.active_target_price(self.projection.project(z)))
        z['payload']['active_target_stage'] = None
        self.assertIsNone(SL.active_target_price(self.projection.project(z)))
        z['payload']['entry_event_snapshot']['atr_proof']['bars'][0]['low'] -= 3
        self.assertFalse(SL.owns_position(z))
        self.assertFalse(SL.owns_position(self.projection.project(z)))

    def test_non_object_payload_is_not_laundered_into_a_valid_object(self):
        z, unused = position()
        for value in (None, [], 'invalid-shape', 7):
            z['payload'] = value
            self.assertEqual(self.projection.project(z)['payload'], value)


class MemoryConnection:
    """A strict synthetic metadata store, not a substitute financial engine."""
    def __init__(self, positions):
        self.positions = {z['active_trade_id']: deepcopy(z) for z in positions}
        self.trades = {tid: {'payload': deepcopy(z['payload'])} for tid, z in self.positions.items()}
        self.sql, self.depth, self.commits, self.orders = [], 0, 0, []
        self.fail_trade_batch = False
        self.projection = ProjectionDatabase()

    @contextmanager
    def transaction(self):
        before = deepcopy((self.positions, self.trades, self.orders))
        self.depth += 1
        try:
            yield self
        except BaseException:
            self.positions, self.trades, self.orders = before
            raise
        else:
            self.commits += int(self.depth == 1)
        finally:
            self.depth -= 1

    @contextmanager
    def connect(self):
        yield self

    @staticmethod
    def merge_payload(left, right):
        # PostgreSQL JSONB || merges objects; other shapes concatenate arrays.
        if isinstance(left, dict) and isinstance(right, dict):
            return dict(left, **right)
        return (left if isinstance(left, list) else [left]) + (right if isinstance(right, list) else [right])

    def execute(self, sql, args=()):
        self.sql.append((sql, args))
        if sql.startswith('SET LOCAL') or 'pg_advisory_xact_lock' in sql:
            if self.depth < 1:
                raise AssertionError('accounting lock outside transaction')
            return SimpleNamespace(fetchone=lambda: {'acquired': True})
        if sql.startswith(PR.PROTECTION_SQL):
            return SimpleNamespace(fetchall=lambda: [self.projection.project(z)
                for z in self.positions.values()])
        if 'jsonb_to_recordset' in sql:
            if 'paper_trades' in sql and self.fail_trade_batch:
                raise RuntimeError('synthetic telemetry write failure')
            table = self.positions if 'paper_positions' in sql else self.trades
            for row in json.loads(args[0]):
                if row['trade_id'] in table:
                    table[row['trade_id']]['payload'] = self.merge_payload(
                        table[row['trade_id']]['payload'], row['patch'])
            return SimpleNamespace()
        if sql.startswith('SELECT fees_rub'):
            return SimpleNamespace(fetchone=lambda: {'fees_rub': 3., 'funding_rub': 2., 'gross_pnl_rub': 0.})
        if sql.startswith('SELECT gross_pnl_rub,fees_rub,funding_rub FROM paper_trades'):
            return SimpleNamespace(fetchone=lambda: {'fees_rub': 3., 'funding_rub': 2., 'gross_pnl_rub': 0.})
        if sql.startswith('SELECT * FROM paper_trades'):
            return SimpleNamespace(fetchone=lambda: self.trades.get(args[0]))
        if sql.startswith('UPDATE paper_portfolios') or sql.startswith('INSERT INTO paper_nav_history'):
            return SimpleNamespace()
        if (sql.startswith('UPDATE paper_positions SET payload=payload||') or
                sql.startswith('UPDATE paper_trades SET payload=payload||') or
                sql.startswith('UPDATE paper_positions SET payload=COALESCE') or
                sql.startswith('UPDATE paper_trades SET payload=COALESCE')):
            if (sql.startswith('UPDATE paper_trades SET payload=COALESCE')
                    and self.fail_trade_batch):
                raise RuntimeError('synthetic telemetry write failure')
            table = self.positions if sql.startswith('UPDATE paper_positions') else self.trades
            if args[1] in table:
                table[args[1]]['payload'] = self.merge_payload(
                    table[args[1]]['payload'], json.loads(args[0]))
            return SimpleNamespace()
        raise AssertionError('unexpected SQL: '+sql[:100])


class BoundedProtectiveTests(unittest.TestCase):
    def setUp(self):
        for cache in (G._quotes, G._source_quotes, G._market_state):
            self.enterContext(patch.dict(cache, {}, clear=True))

    def connection(self, rows):
        c = MemoryConnection(rows)
        self.addCleanup(c.projection.db.close)
        return c

    def test_structural_no_exit_pass_reads_once_and_batches_without_legacy_work(self):
        rows = [position(i, structural=True)[0] for i in range(33)]
        q = position()[1]
        c = self.connection(rows)
        with patch.object(G, 'exit_fill', side_effect=AssertionError('irrelevant legacy fill')):
            self.assertEqual(G.run_protective_pass(None, c.connect, {'ETH': q}, NOW), [])
        selects = [sql for sql, args in c.sql if sql.startswith('SELECT') and 'advisory' not in sql]
        writes = [args for sql, args in c.sql if 'jsonb_to_recordset' in sql]
        self.assertEqual(selects, [PR.PROTECTION_SQL+' ORDER BY portfolio_name,asset FOR UPDATE'])
        self.assertEqual(len(writes), 6)
        self.assertTrue(all(len(json.loads(args[0])) <= 16 for args in writes))
        self.assertEqual(c.commits, 1)
        for tid, saved in c.positions.items():
            self.assertEqual(saved['payload']['immutable_history'], rows[0]['payload']['immutable_history'])
            self.assertEqual(saved['payload']['observation_path']['observation_count'], 1)
            self.assertEqual(saved['payload']['mfe_pct'], 1.5)
            self.assertEqual(saved['payload']['source_locked_mark']['price'], 101.)
            self.assertEqual(saved['payload'], c.trades[tid]['payload'])
        self.assertEqual(c.orders, [])

    def test_required_batch_failure_propagates_and_optional_savepoint_rolls_back_both_tables(self):
        z, unused = position()
        c = self.connection([z])
        tid = z['active_trade_id']
        c.fail_trade_batch = True
        before = deepcopy((c.positions, c.trades))
        with G.book_transaction(c):
            PR.write_patches(c, [(tid, {'synthetic_patch': 1})], optional=True)
            self.assertEqual((c.positions, c.trades), before)
        with self.assertRaisesRegex(RuntimeError, 'synthetic telemetry'):
            with G.book_transaction(c):
                PR.write_patches(c, [(tid, {'synthetic_patch': 2})])
        self.assertEqual((c.positions, c.trades), before)

    def test_repeated_trade_deltas_preserve_sequential_jsonb_merge(self):
        for previous in ({'prior': True}, ['prior'], 'prior'):
            with self.subTest(payload=previous):
                z, unused = position()
                z['payload'] = deepcopy(previous)
                c = self.connection([z])
                tid = z['active_trade_id']
                first, second = {'a': 1, 'b': 2}, {'a': 3}
                with G.book_transaction(c):
                    PR.write_patches(c, [(tid, first), (tid, second)])
                batches = [json.loads(args[0]) for sql, args in c.sql if 'jsonb_to_recordset' in sql]
                self.assertEqual(batches, [
                    [{'trade_id': tid, 'patch': first}], [{'trade_id': tid, 'patch': first}],
                    [{'trade_id': tid, 'patch': second}], [{'trade_id': tid, 'patch': second}]])
                expected = (dict(previous, a=3, b=2) if isinstance(previous, dict)
                            else (previous if isinstance(previous, list) else [previous]) + [first, second])
                self.assertEqual(c.positions[tid]['payload'], expected)
                self.assertEqual(c.trades[tid]['payload'], expected)

    def test_one_nonfinite_optional_witness_does_not_discard_healthy_neighbours(self):
        rows = [position(i, structural=True)[0] for i in range(3)]
        rows[1]['payload']['mfe_pct'] = 'NaN'
        c = self.connection(rows)
        self.assertEqual(G.run_protective_pass(None, c.connect, {'ETH': position()[1]}, NOW), [])
        for index in (0, 2):
            self.assertIn('observation_path', c.positions[rows[index]['active_trade_id']]['payload'])
        self.assertNotIn('observation_path', c.positions[rows[1]['active_trade_id']]['payload'])
        self.assertEqual(c.positions[rows[1]['active_trade_id']]['payload']['mfe_pct'], 'NaN')
        batches = [json.loads(args[0]) for sql, args in c.sql if 'jsonb_to_recordset' in sql]
        self.assertEqual([len(rows) for rows in batches], [1, 1, 1, 1])
        self.assertEqual([batch[0]['trade_id'] for batch in batches],
                         [rows[0]['active_trade_id']]*2 + [rows[2]['active_trade_id']]*2)

    def test_exit_rehydrates_full_row_and_optional_metadata_failure_cannot_block_stop(self):
        for fail_metadata in (False, True):
            with self.subTest(fail_metadata=fail_metadata):
                z, q = position(structural=True)
                q.update(price=89., best_bid=89., best_ask=89.01)
                c = self.connection([z])
                c.fail_trade_batch = fail_metadata
                seen = []
                account = dict(initial_nav_rub=500., high_water_nav_rub=500.,
                               benchmark_nav_rub=500., last_ruonia=16.)
                def close(connection, book, name, full, price, fraction, nav, ts, reason):
                    self.assertIn('immutable_history', full['payload'])
                    self.assertEqual(full['_execution_quote']['price'], 89.)
                    self.assertEqual(reason, 'STOP')
                    if not fail_metadata:
                        self.assertEqual(full['payload']['observation_path']['observation_count'], 1)
                    seen.append(full['active_trade_id'])
                    connection.orders.append('synthetic-stop')
                    connection.positions.pop(full['active_trade_id'])
                    return 1.
                vp = SimpleNamespace(_portfolio_rows=lambda connection, name:
                    (account, list(connection.positions.values())),
                    _mark_nav=lambda *args: (500., 0., 0., 0.),
                    _apply_funding=lambda *args: None, _v90j_update_excursions=lambda *args: None,
                    _close_or_reduce=close)
                with patch.object(PP, 'refresh'):
                    changes = G.run_protective_pass(vp, c.connect, {'ETH': q}, NOW)
                self.assertEqual(seen, [z['active_trade_id']])
                self.assertEqual([row['reason'] for row in changes], ['STOP'])
                self.assertEqual(c.orders, ['synthetic-stop'])
                self.assertEqual(c.commits, 1)

    def test_stale_and_foreign_quotes_do_not_write_any_metadata(self):
        z, q = position(structural=True)
        for rejected in (dict(q, observed_at=(NOW-timedelta(hours=1)).isoformat()),
                         dict(q, source_names={'primary': 'Unrelated venue'})):
            c = self.connection([z])
            before = deepcopy((c.positions, c.trades))
            self.assertEqual(G.run_protective_pass(None, c.connect, {'ETH': rejected}, NOW), [])
            self.assertEqual((c.positions, c.trades), before)
            self.assertFalse(any(sql.startswith('UPDATE') for sql, args in c.sql))

    def test_live_pass_rechecks_clock_before_each_position(self):
        rows = [position(i, structural=True)[0] for i in range(2)]
        q = position()[1]
        c = self.connection(rows)
        readings = iter((NOW, NOW, NOW+timedelta(hours=1)))
        class Clock:
            @classmethod
            def now(cls, zone): return next(readings)
        with patch.object(G, 'datetime', Clock):
            self.assertEqual(G.run_protective_pass(None, c.connect, {'ETH': q}), [])
        self.assertIn('observation_path', c.positions[rows[0]['active_trade_id']]['payload'])
        self.assertNotIn('observation_path', c.positions[rows[1]['active_trade_id']]['payload'])


if __name__ == '__main__':
    unittest.main()
