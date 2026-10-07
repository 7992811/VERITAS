"""Quote-source projection exercised on real PostgreSQL JSONB/LATERAL SQL.

Moved unchanged source, expiry, identity and observation-order cases from the
former SQLite adapter. SQL writes use a unique schema only in the explicitly
configured veritas_quality_test database; no market or broker is contacted.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
import unittest
from unittest.mock import patch
import uuid

import veritas_position_guard as G
import veritas_price_source as S


DSN = os.getenv('VERITAS_QUALITY_TEST_DSN', '')


@unittest.skipUnless(DSN, 'isolated PostgreSQL test database not configured')
class QuoteProjectionTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        self.sql = sql
        self.now = datetime.now(timezone.utc)
        self.enterContext(patch.dict(G._quotes, {}, clear=True))
        self.enterContext(patch.dict(G._source_quotes, {}, clear=True))
        self.enterContext(patch.dict(G._market_state, {}, clear=True))
        self.schema = 'quote_projection_test_' + uuid.uuid4().hex
        self.db = psycopg.connect(DSN, autocommit=True, row_factory=dict_row, connect_timeout=5)
        self.addCleanup(self.db.close)
        self.verify_database()
        with self.db.transaction():
            self.db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
            self.db.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
            self.db.execute('CREATE TABLE paper_positions(asset text,active_trade_id text,payload jsonb)')
        self.addCleanup(self.drop_schema)
        self.db.execute("SET statement_timeout = '10s'")

    def verify_database(self):
        if self.db.execute('SELECT current_database() AS name').fetchone()['name'] != 'veritas_quality_test':
            raise RuntimeError('Refusing integration writes outside veritas_quality_test')

    def drop_schema(self):
        self.verify_database()
        self.db.execute(self.sql.SQL('DROP SCHEMA {} CASCADE').format(self.sql.Identifier(self.schema)))

    def project(self, position):
        self.db.execute('DELETE FROM paper_positions')
        self.db.execute('INSERT INTO paper_positions VALUES(%s,%s,%s::jsonb)',
            (position['asset'], position['active_trade_id'], json.dumps(position['payload'])))
        return dict(self.db.execute(G.QUOTE_POSITION_SQL).fetchone())

    def test_real_projection_preserves_source_contract_and_observation_order(self):
        cases = [('ETH', 'Binance spot', None), ('MOEX', 'MOEX ISS IMOEX', None),
            ('CNYRUBF', 'MOEX ISS CNYRUBF', 'CRZ6'), ('BRENT', 'MOEX ISS BRX6', 'BRX6'),
            ('BRENT', 'MOEX ISS BRX6', None), ('NQ', 'ProFinance NASD100_FUT', None),
            ('GOLD', 'ProFinance', None), ('NQ', 'Yahoo NQ futures', None),
            ('GOLD', 'GC GLD proxy bridge', 'GCZ6'), ('NQ', 'Stooq futures', None)]
        for asset, source, contract in cases:
            for legacy in (False, True):
                with self.subTest(asset=asset, source=source, legacy=legacy):
                    quote = dict(price=101., observed_at=self.now.isoformat(), source_gate_pass=True,
                        market_open=True, source_names={'primary': source}, contract={'secid': contract})
                    identity = S.identity(asset, quote)
                    payload = dict(contract_identity={'primary_source': source, 'contract_id': contract},
                        entry_primary_source=source, entry_contract_secid=contract,
                        entry_execution_observed_at=(self.now-timedelta(seconds=2)).isoformat(),
                        entry_market_observed_at=(self.now-timedelta(seconds=3)).isoformat(),
                        source_locked_mark={'price': 100., 'identity': identity,
                            'observed_at': (self.now-timedelta(seconds=1)).isoformat()},
                        entry_decision_snapshot={'proof': 'x'*50000})
                    if not legacy:
                        payload['price_source_lock'] = identity
                    full = dict(asset=asset, active_trade_id='fixture', units=5., payload=payload)
                    original = deepcopy(full)
                    projected = self.project(full)
                    self.assertEqual(S.position_identity(projected), S.position_identity(full))
                    for candidate in (quote, dict(quote, observed_at=(self.now-timedelta(seconds=5)).isoformat()),
                                      dict(quote, contract={'secid': 'WRONG'}),
                                      dict(quote, source_names={'primary': 'Foreign'})):
                        self.assertEqual(G.quote_for_position(projected, candidate, self.now),
                                         G.quote_for_position(full, candidate, self.now))
                    self.assertLess(len(json.dumps(projected)), 1300)
                    self.assertNotIn('entry_decision_snapshot', projected['payload'])
                    self.assertEqual(full, original)

    def test_projected_both_brent_expiries_fetch_exact_contracts(self):
        for contract in ('BRX6', 'BRZ6'):
            payload = {'price_source_lock': S.identity('BRENT', {
                'source_names': {'primary': 'MOEX ISS '+contract}, 'contract': {'secid': contract}}),
                'entry_contract_secid': 'WRONG',
                'entry_decision_snapshot': {'proof': 'x'*50000}}
            full = {'asset': 'BRENT', 'active_trade_id': contract, 'payload': payload}
            projected = self.project(full)
            calls = []
            def fetch(cid):
                calls.append(cid)
                return {'price': 100., 'observed_at': self.now.isoformat(),
                        'market_open': True, 'row': {'SECID': cid}}
            ns = {'_moex_futures_current_quote': fetch}
            expected = G.fetch_guard_quote(ns, 'BRENT', [full])
            actual = G.fetch_guard_quote(ns, 'BRENT', [projected])
            self.assertEqual(actual, expected)
            self.assertEqual(calls, [contract, contract])

    def test_legacy_fixed_venues_and_direct_broker_identity_are_preserved(self):
        for asset in ('BTC', 'ETH', 'MOEX', 'CNYRUBF'):
            full = {'asset': asset, 'active_trade_id': 'legacy', 'payload': {}}
            self.assertEqual(S.position_identity(self.project(full)), S.position_identity(full))
        full = {'asset': 'CNYRUBF', 'active_trade_id': 'direct', 'payload': {
            'price_source_lock': S.identity('CNYRUBF', {
                'primary_source': 'TBANK_GRPC CNYRUBF', 'contract': {'instrument_uid': 'exact-uid'}})}}
        projected = self.project(full)
        self.assertEqual(S.position_identity(projected), S.position_identity(full))
        candidate = {'price': 12.5, 'observed_at': self.now.isoformat(), 'source_gate_pass': True,
            'source_names': {'primary': 'TBANK_GRPC CNYRUBF'}, 'contract': {'instrument_uid': 'exact-uid'}}
        with patch('veritas_direct_cny.quote', return_value=candidate):
            self.assertEqual(G.quote_for_position(projected, now=self.now), G.quote_for_position(full, now=self.now))


if __name__ == '__main__':
    unittest.main()
