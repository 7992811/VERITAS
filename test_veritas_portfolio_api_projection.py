"""Portfolio read projection preserves accounting, source locks and display.

Unit tests run the actual refresh and VTV/VPP/VPS consumers without app startup.
Optional PostgreSQL tests execute the projection on supplied JSON values only;
they never read or change production tables.
"""
import ast
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
import math
import os
from pathlib import Path
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import patch

import veritas_currency_portfolio as Currency
import veritas_portfolio_api_projection as Projection
import veritas_price_source as Source
import veritas_trade_view as View


ROOT = Path(__file__).resolve().parent
DSN = os.getenv('VERITAS_QUALITY_TEST_DSN', '')
NAMES = ('Impulse', 'Aggressive', 'Champion', 'Challenger', 'Currency')


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


NOW = FixedDatetime(2026, 10, 7, 14, tzinfo=timezone.utc)


@lru_cache(maxsize=1)
def reader_node():
    return next(node for node in ast.parse((ROOT / 'veritas_intelligence.py').read_text()).body
                if isinstance(node, ast.FunctionDef) and node.name == '_v90r25_portfolios_refresh')


def project_reference(value):
    """Independent description of the .get/truthiness-preserving view."""
    if not isinstance(value, dict) or not value:
        return deepcopy(value)
    result = {key: deepcopy(value.get(key)) for key in (
        'horizon', 'calibrated_probability', 'confidence', 'signal_tier',
        'setup_grade', 'setup_grade_score', 'entry_quality', 'decision_stage',
        'expected_move_pct', 'expected_to_stop_ratio', 'target_price')}
    plan = value.get('trade_plan')
    if isinstance(plan, dict) and plan:
        plan = {key: deepcopy(plan.get(key)) for key in (
            'setup_grade', 'setup_grade_score', 'entry_quality',
            'expected_move_pct', 'expected_to_stop_ratio', 'target_price')}
    result['trade_plan'] = deepcopy(plan)
    return result


def fixture(kind='brent'):
    if kind == 'currency':
        name, asset, direction, entry, mark, stop = 'Currency', 'CNYRUBF', 'SHORT', 12.76, 12.74, 12.75
        identity = Source.identity(asset, {'primary_source': 'TBANK_GRPC CNYRUBF',
            'contract': {'instrument_uid': 'test-cny-instrument'}})
    elif kind == 'moex':
        name, asset, direction, entry, mark, stop = 'Challenger', 'BRENT', 'SHORT', 100., 99., 99.5
        identity = Source.identity(asset, {'primary_source': 'MOEX ISS BRX6', 'contract_id': 'BRX6'})
    else:
        name, asset, direction, entry, mark, stop = 'Aggressive', 'BRENT', 'LONG', 100., 102., 101.
        identity = Source.brent_feed_pin_identity()
    proof = {'retained_execution_evidence': ['original-observation'] * 100}
    trade_payload = {'price_source_lock': identity,
        'entry_execution_source_identity': deepcopy(identity),
        'source_locked_mark': {'identity': deepcopy(identity), 'price': mark, 'observed_at': NOW.isoformat()},
        'r17_tp1_done': True, 'r17_tp1_at': (NOW-timedelta(minutes=15)).isoformat(),
        'r66_event_id': 'test-entry-event', 'data_integrity_status': 'OK',
        'entry_event_snapshot': {'target_ladder': [{'price': 103., 'kind': 'TP1'},
                                                 {'price': 104., 'kind': 'FINAL'}], 'proof': proof},
        'proof': proof}
    position = {'portfolio_name': name, 'asset': asset, 'direction': direction,
        'units': 50., 'avg_entry_price': entry, 'last_price': mark,
        'opened_at': NOW-timedelta(hours=1), 'updated_at': NOW-timedelta(minutes=2),
        'stop_price': stop, 'target_fraction': .5, 'active_trade_id': 'test-' + kind,
        'trade_horizon': None, 'trade_setup': 'TEST_STRUCTURE', 'max_fraction': 1.,
        'payload': {'r17_tp1_done': False, 'position_evidence': proof},
        'trade_payload': trade_payload,
        'entry_decision_payload': {'horizon': '1h', 'calibrated_probability': .72,
            'signal_tier': direction, 'decision_stage': 'ENTRY', 'confidence': .64,
            'trade_plan': {'setup_grade': 'A', 'setup_grade_score': 8.,
                'entry_quality': 'VALID', 'expected_move_pct': 2.,
                'expected_to_stop_ratio': 2.5, 'target_price': 104., 'proof': proof},
            'unused_decision_graph': proof}}
    account = {'trade_id': position['active_trade_id'], 'status': 'OPEN',
        'opened_at': position['opened_at'], 'gross_pnl_rub': 27., 'fees_rub': 2.,
        'funding_rub': 1., 'last_mark_at': NOW-timedelta(minutes=1), 'last_ruonia': 16.,
        'portfolio_nav_rub': 10024. if kind == 'currency' else 1000024.,
        'entry_notional_rub': 6000., 'payload': deepcopy(trade_payload)}
    return position, account


class Result:
    def __init__(self, rows=()):
        self.rows = rows

    def fetchall(self):
        return self.rows


class SnapshotDB:
    def __init__(self, position, account, *, project=project_reference):
        self.position, self.account, self.project = deepcopy(position), deepcopy(account), project
        self.queries, self.returned_positions = [], []
        self.transactions, self.connections = 0, 0

    @contextmanager
    def connect(self):
        self.connections += 1
        yield self

    @contextmanager
    def transaction(self):
        self.transactions += 1
        try:
            yield self
        finally:
            self.transactions -= 1

    def execute(self, sql, parameters=()):
        if self.transactions != 1:
            raise AssertionError('Every account and quantity must use the same read snapshot')
        self.queries.append(' '.join(sql.split()))
        if sql.startswith(('SET TRANSACTION', 'SET LOCAL')):
            return Result()
        if sql.startswith('SELECT name,initial_nav'):
            return Result([dict(name=name, initial_nav_rub=10000. if name == 'Currency' else 1000000.,
                realized_pnl_rub=27., fees_rub=2., funding_rub=1.,
                benchmark_nav_rub=10000. if name == 'Currency' else 1000000.,
                high_water_nav_rub=10000. if name == 'Currency' else 1000000.,
                last_ruonia=16., last_usdrub=100., last_mark_at=NOW) for name in NAMES])
        if sql.startswith('SELECT DISTINCT ON'):
            return Result([])
        if sql.startswith('SELECT pp.portfolio_name'):
            row = deepcopy(self.position)
            row['entry_decision_payload'] = self.project(row['entry_decision_payload'])
            self.returned_positions = [row]
            return Result(self.returned_positions)
        if sql.startswith('SELECT portfolio_name,COUNT'):
            return Result([])
        if sql.startswith('SELECT t.trade_id,t.status'):
            if self.account is None:
                return Result([])
            row = deepcopy(self.account)
            if 't.payload' not in sql:
                row.pop('payload', None)
            return Result([row])
        raise AssertionError('Unexpected or non-read-only query: ' + sql[:100])


def read_api(db, *, legacy_accounts=False):
    captured = {}
    original_evaluate = View.VPP.evaluate

    def load_accounts(connection, ids, include_entry_notional=False, *, include_payload=True):
        captured['include_payload'] = include_payload
        return View.VPP.load_accounts(connection, ids, include_entry_notional,
                                      include_payload=True if legacy_accounts else include_payload)

    def enrich(report, connect, *, preloaded_accounts):
        if db.transactions:
            raise AssertionError('Enrichment must occur after the bounded read transaction')
        captured['accounts'] = deepcopy(preloaded_accounts)
        for row in db.returned_positions:
            account = preloaded_accounts.get(row['active_trade_id'])
            if account is not None and 'trade_payload' in row:
                if account['payload'] is not row['trade_payload']:
                    raise AssertionError('Trade payload was decoded or copied again')
        captured['before_enrichment'] = deepcopy(report)
        return View.enrich_positions(report, connect, preloaded_accounts=preloaded_accounts)

    ns = dict(_v90r25_pf_cache={'at': 0., 'value': None}, _v90r25_pf_lock=threading.Lock(),
        lock=threading.Lock(), last_cycle={'summary': [dict(asset=db.position['asset'],
            research_decision=db.position['direction'], horizon='1h', calibrated_probability=.61)]},
        time=time, datetime=FixedDatetime, timezone=timezone, math=math,
        V90_CANONICAL_PORTFOLIOS=NAMES, pg_enabled=lambda: True, pg_connect=db.connect,
        VTV=SimpleNamespace(enrich_positions=enrich, VPP=SimpleNamespace(load_accounts=load_accounts)),
        VP=SimpleNamespace(VCP=Currency, POLICIES={}),
        VX=SimpleNamespace(VC=SimpleNamespace(COMMISSION_RATE=.0004)),
        CLOSED_METRICS_SQL='0 AS diagnostic', closed_trade_metrics=lambda *_: {})
    exec(compile(ast.Module(body=[reader_node()], type_ignores=[]), '<portfolio-read>', 'exec'), ns)
    with patch.object(View.VPG, 'quote_for_position', return_value={}), \
            patch.object(View.VPP, 'evaluate', side_effect=lambda z, a: original_evaluate(z, a, now=NOW)):
        output = ns['_v90r25_portfolios_refresh']()
    captured['cache'] = ns['_v90r25_pf_cache']
    return output, captured


def decision_shapes():
    values = (None, False, 0, '', [], {}, [None], {'unused': True})
    shapes = list(values) + [{'trade_plan': value} for value in values]
    shapes += [{'calibrated_probability': value, 'expected_move_pct': value} for value in values]
    shapes += [{'trade_plan': {'setup_grade': None, 'setup_grade_score': 0,
        'expected_move_pct': '', 'expected_to_stop_ratio': False, 'unused': [123]}}]
    return shapes


class PortfolioAPIProjectionTests(unittest.TestCase):
    def assert_api_parity(self, position, account, projected_decision=None, *, supplied=False):
        raw = SnapshotDB(position, account, project=deepcopy)
        projected = SnapshotDB(position, account,
            project=(lambda _: deepcopy(projected_decision)) if supplied else project_reference)
        before, _ = read_api(raw, legacy_accounts=True)
        after, captured = read_api(projected)
        self.assertEqual(after, before)
        self.assertEqual(captured['cache']['value'], after)
        return before, after, projected, captured

    def test_reuses_one_complete_trade_payload_for_actual_accounting_protection_and_sources(self):
        pairs = []
        for kind in ('currency', 'brent', 'moex'):
            with self.subTest(kind=kind):
                position, account = fixture(kind)
                before, after, db, captured = self.assert_api_parity(position, account)
                self.assertEqual(captured['accounts'][position['active_trade_id']], account)
                self.assertFalse(captured['include_payload'])
                self.assertEqual(db.connections, 1)
                self.assertEqual(len(db.queries), 8)  # Three SETs and five SELECTs.
                position_sql = next(sql for sql in db.queries if sql.startswith('SELECT pp.portfolio_name'))
                self.assertIn(Projection.ENTRY_DECISION_PAYLOAD_SQL, position_sql)
                self.assertIn('pp.payload', position_sql)
                self.assertIn('pt.payload AS trade_payload', position_sql)
                account_sql = next(sql for sql in db.queries if sql.startswith('SELECT t.trade_id'))
                self.assertNotIn('t.payload', account_sql)
                positions = [z for p in after['portfolios'] for z in p['positions']]
                z = positions[0]
                self.assertEqual(z['price_source_lock'], position['trade_payload']['price_source_lock'])
                self.assertEqual(z['last_price'], position['last_price'])
                self.assertFalse(z['tp1_done'])  # Position payload still overrides trade payload.
                self.assertEqual(z['trade_result_status'], 'COMPLETE')
                self.assertNotEqual(z['net_profit_protection']['state'], 'UNAVAILABLE')
                self.assertEqual(z['held_seconds'], 3600.)
                pairs.append({'original': before, 'projected': after})
        # This existing harness renders the actual canonical HTML and source labels.
        from test_veritas_portfolio_read_model import assert_display_ui_parity
        assert_display_ui_parity(pairs)

    def test_every_decision_fallback_consumer_is_present_in_the_sql_contract(self):
        reads = {'entry_dec': set(), 'entry_plan': set()}
        for node in ast.walk(reader_node()):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == 'get' and node.args
                    and isinstance(node.args[0], ast.Constant)):
                receiver = node.func.value
                if isinstance(receiver, ast.BoolOp):
                    receiver = receiver.values[0]
                if isinstance(receiver, ast.Name) and receiver.id in reads:
                    reads[receiver.id].add(node.args[0].value)
        self.assertEqual(reads['entry_dec'], set(Projection.ENTRY_DECISION_FIELDS) | {'trade_plan'})
        self.assertEqual(reads['entry_plan'], set(Projection.ENTRY_PLAN_FIELDS))

    def test_empty_unknown_null_and_malformed_decisions_keep_fallback_behavior(self):
        for decision in decision_shapes():
            with self.subTest(decision=decision):
                position, account = fixture()
                position['entry_decision_payload'] = decision
                plan = decision.get('trade_plan') if isinstance(decision, dict) else None
                if plan and not isinstance(plan, dict):
                    for projector in (deepcopy, project_reference):
                        output, _ = read_api(SnapshotDB(position, account, project=projector))
                        self.assertTrue(output['positions_complete'])
                        self.assertTrue(output['accounting_complete'])
                        self.assertEqual(sum(len(p['positions']) for p in output['portfolios']), 1)
                else:
                    self.assert_api_parity(position, account)

    def test_trade_snapshot_precedence_and_large_proof_inputs_remain_complete(self):
        position, account = fixture()
        huge = {'historical_bars': ['unused-proof-' * 5000] * 5}
        position['entry_decision_payload'] = {'trade_plan': ['malformed-fallback'], 'huge': huge}
        position['trade_payload']['entry_decision_snapshot'] = {'horizon': '4h', 'confidence': .88,
            'trade_plan': {'target_price': 109.}, 'full_proof': huge}
        account['payload'] = deepcopy(position['trade_payload'])
        before, after, _, captured = self.assert_api_parity(position, account)
        z = next(p['positions'][0] for p in after['portfolios'] if p['positions'])
        self.assertEqual(z['signal_probability'], .88)
        self.assertEqual(z['take_price'], 109.)
        self.assertEqual(captured['accounts'][position['active_trade_id']]['payload'], account['payload'])
        self.assertNotIn('entry_decision_payload', z)
        self.assertNotIn('historical_bars', json.dumps(after, default=str))

    def test_null_or_malformed_trade_payload_and_missing_account_do_not_create_balances(self):
        for payload in (None, {}, [], False, '{}'):
            with self.subTest(payload=payload):
                position, account = fixture('currency')
                position['trade_payload'] = payload
                account['payload'] = deepcopy(payload)
                _, _, _, captured = self.assert_api_parity(position, account)
                self.assertEqual(captured['accounts'][position['active_trade_id']]['payload'], payload)
        position, _ = fixture()
        _, after, db, captured = self.assert_api_parity(position, None)
        self.assertEqual(captured['accounts'], {})
        self.assertEqual(db.connections, 1)
        z = next(p['positions'][0] for p in after['portfolios'] if p['positions'])
        self.assertEqual(z['trade_result_status'], 'INCOMPLETE')
        self.assertIsNone(z['total_trade_pnl_rub'])


@unittest.skipUnless(DSN, 'isolated PostgreSQL test database not configured')
class PortfolioAPIProjectionSQLTests(unittest.TestCase):
    @contextmanager
    def connect(self):
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(DSN, row_factory=dict_row) as connection:
            connection.execute('SET TRANSACTION READ ONLY')
            if connection.execute('SELECT current_database() AS name').fetchone()['name'] != 'veritas_quality_test':
                raise RuntimeError('Refusing integration access outside veritas_quality_test')
            yield connection

    def project(self, values):
        with self.connect() as c:
            rows = c.execute('SELECT ed.ordinality, ' + Projection.ENTRY_DECISION_PAYLOAD_SQL
                + ' AS projected FROM jsonb_array_elements(%s::jsonb) WITH ORDINALITY'
                + ' AS ed(payload,ordinality) ORDER BY ed.ordinality', (json.dumps(values),)).fetchall()
        self.assertEqual(len(rows), len(values))
        return [row['projected'] for row in rows]

    def test_postgresql_matches_get_truthiness_and_type_semantics(self):
        values = decision_shapes()
        self.assertEqual(self.project(values), [project_reference(value) for value in values])
        with self.connect() as c:
            result = c.execute('SELECT ' + Projection.ENTRY_DECISION_PAYLOAD_SQL
                + ' AS projected FROM (VALUES (NULL::jsonb)) AS ed(payload)').fetchone()
        self.assertIsNone(result['projected'])

    def test_real_sql_projection_preserves_the_complete_portfolio_api(self):
        fixtures = [fixture(kind) for kind in ('currency', 'brent', 'moex')]
        projected = self.project([position['entry_decision_payload'] for position, _ in fixtures])
        for (position, account), decision in zip(fixtures, projected):
            PortfolioAPIProjectionTests().assert_api_parity(position, account, decision, supplied=True)

    def test_large_decision_proof_is_removed_before_driver_decode(self):
        position, _ = fixture()
        decision = position['entry_decision_payload']
        decision['unused_decision_graph'] = {'raw_bars': ['full-proof-' * 10000] * 10}
        decision['trade_plan']['proof'] = deepcopy(decision['unused_decision_graph'])
        projected = self.project([decision])[0]
        self.assertEqual(projected, project_reference(decision))
        self.assertGreater(len(json.dumps(decision)), 2000000)
        self.assertLess(len(json.dumps(projected)), 1000)
        self.assertNotIn('full-proof', json.dumps(projected))


if __name__ == '__main__':
    unittest.main()
