"""An oil position's saved expiry owns both its quote request and protection."""
from copy import deepcopy
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import MagicMock, Mock, patch

import veritas_market_runtime as M
import veritas_position_guard as G
import veritas_price_source as S


class BrentContractGuardTests(TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.enterContext(patch.dict(G._quotes, {}, clear=True))
        self.enterContext(patch.dict(G._source_quotes, {}, clear=True))
        self.enterContext(patch.dict(G._market_state, {}, clear=True))
        self.moex = Mock(side_effect=self.quote)
        self.pf = Mock(return_value={})
        self.ns = {'_moex_futures_current_quote': self.moex,
                   '_v90r61_profinance_quote': self.pf}

    def quote(self, contract, price=101.28):
        return {'price': price, 'observed_at': self.now.isoformat(),
                'price_field': 'LAST', 'row': {'SECID': contract}}

    def position(self, contract='BRX6'):
        identity = S.identity('BRENT', {'source': 'MOEX ISS '+str(contract or ''),
                                       'contract_id': contract})
        return {'asset': 'BRENT', 'direction': 'LONG', 'avg_entry_price': 100.5,
                'last_price': 100.5, 'stop_price': 99.,
                'payload': {'price_source_lock': identity,
                            'entry_execution_source_identity': deepcopy(identity)}}

    def test_canonical_identity_fetches_exact_expiry_without_legacy_alias(self):
        result = G.fetch_guard_quote(self.ns, 'BRENT', [self.position()])
        self.moex.assert_called_once_with('BRX6')
        self.pf.assert_not_called()
        self.assertEqual(result['contract']['secid'], 'BRX6')
        self.assertEqual(result['price'], 101.28)
        self.assertEqual(result['price_field'], 'LAST')

    def test_foreign_asset_cache_cannot_select_a_held_contract(self):
        G.publish_quote('BRENT', dict(self.quote('BRZ6', price=98.),
            source_gate_pass=True, contract={'secid': 'BRZ6'},
            source_names={'primary': 'MOEX ISS BRZ6'}))
        result = G.fetch_guard_quote(self.ns, 'BRENT', [self.position()])
        self.moex.assert_called_once_with('BRX6')
        self.assertEqual(result['contract']['secid'], 'BRX6')
        self.assertEqual(result['price'], 101.28)

    def test_canonical_identity_supersedes_obsolete_alias_at_protective_boundary(self):
        position = self.position()
        position['payload'].update(entry_contract_secid='BRZ6',
            contract_identity={'primary_source': 'MOEX ISS BRZ6', 'contract_id': 'BRZ6'})
        original = deepcopy(position)
        self.moex.side_effect = lambda cid: self.quote(cid, price=98.5)
        result = G.fetch_guard_quote(self.ns, 'BRENT', [position])
        self.moex.assert_called_once_with('BRX6')
        self.assertTrue(G.quote_matches_position(position, result))
        self.assertEqual(G.protective_reason(position, result, self.now), 'STOP')
        self.assertEqual(G.exit_execution_quote(dict(position, _execution_quote=result),
                                               self.now)['contract']['secid'], 'BRX6')
        self.assertEqual(position, original)

    def test_legacy_contract_identity_remains_resolvable(self):
        position = self.position()
        position['payload'] = {'contract_identity': {
            'primary_source': 'MOEX ISS BRX6', 'contract_id': 'BRX6'}}
        result = G.fetch_guard_quote(self.ns, 'BRENT', [position])
        self.moex.assert_called_once_with('BRX6')
        self.assertEqual(result['contract']['secid'], 'BRX6')

    def test_unresolved_moex_expiry_cannot_borrow_any_cached_contract(self):
        G.publish_quote('BRENT', dict(self.quote('BRZ6'), source_gate_pass=True,
            contract={'secid': 'BRZ6'}, source_names={'primary': 'MOEX ISS BRZ6'}))
        position = self.position(None)
        self.assertEqual(G.fetch_guard_quote(self.ns, 'BRENT', [position]), {})
        self.assertEqual(G.quote_for_position(position, now=self.now), {})
        self.assertFalse(G.quote_matches_position(position, G._quotes['BRENT']))
        self.moex.assert_not_called()

    def test_response_for_foreign_contract_is_not_published_or_relabelled(self):
        self.moex.side_effect = lambda cid: self.quote('BRZ6')
        self.assertEqual(G.fetch_guard_quote(self.ns, 'BRENT', [self.position()]), {})
        self.assertEqual(G._source_quotes, {})
        self.assertEqual(G._quotes, {})

    def test_two_held_expiries_are_refreshed_separately(self):
        positions = [self.position('BRX6'), self.position('BRZ6')]
        G.refresh_position_quotes(self.ns, positions)
        self.assertEqual({call.args[0] for call in self.moex.call_args_list}, {'BRX6', 'BRZ6'})
        self.assertEqual(self.moex.call_count, 2)
        for position in positions:
            result = G.quote_for_position(position, now=self.now)
            self.assertEqual(result['contract']['secid'], S.position_identity(position)['contract_id'])

    def test_mixed_expiries_in_one_group_are_rejected(self):
        result = G.fetch_guard_quote(self.ns, 'BRENT',
                                    [self.position('BRX6'), self.position('BRZ6')])
        self.assertEqual(result, {})
        self.moex.assert_not_called()

    def test_explicit_unheld_candidate_is_used_instead_of_cache_contract(self):
        G._quotes['BRENT'] = {'contract': {'secid': 'BRZ6'}}
        result = G.fetch_guard_quote(self.ns, 'BRENT', [], candidate_contract='BRX6')
        self.moex.assert_called_once_with('BRX6')
        self.assertEqual(result['contract']['secid'], 'BRX6')

    def test_profinance_position_keeps_its_existing_provider(self):
        position = self.position()
        position['payload'] = {'price_source_lock': S.identity('BRENT', {
            'source': 'ProFinance', 'raw_label': 'Brent oil'})}
        self.pf.return_value = {'price': 102.03, 'observed_at': self.now.isoformat(),
                               'source': 'ProFinance', 'raw_label': 'Brent oil'}
        result = G.fetch_guard_quote(self.ns, 'BRENT', [position])
        self.moex.assert_not_called()
        self.pf.assert_called_once_with('BRENT')
        self.assertEqual(S.identity('BRENT', result)['key'], 'PROFINANCE:Brent oil')
        self.assertIsNone(S.identity('BRENT', result)['contract_id'])


class BrentExchangeResponseTests(TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        ns = {'_moex_block': lambda doc, block: doc[block],
              '_moex_parse_dt': lambda value: None,
              '_fetch_asset_bundle': Mock(), 'ASSETS': {}}
        M.install_market_runtime_guard(ns)
        self.fetch = ns['_moex_futures_current_quote']
        self.client = MagicMock()
        self.client.__enter__.return_value = self.client
        self.enterContext(patch.object(M.httpx, 'Client', return_value=self.client))

    def response(self, contract='BRX6', **fields):
        row = {'SECID': contract, 'LAST': 101.28, 'TIME': self.now.isoformat(), **fields}
        self.client.get.return_value.json.return_value = {'marketdata': [row]}
        return row

    def test_exact_returned_secid_is_verified_and_quote_field_preserved(self):
        row = self.response()
        result = self.fetch('BRX6')
        self.assertIn('/securities/BRX6.json', self.client.get.call_args.args[0])
        self.assertEqual(result['row'], row)
        self.assertEqual(result['price'], 101.28)
        self.assertEqual(result['price_field'], 'LAST')
        self.assertEqual(result['observed_at'], self.now.isoformat())

    def test_foreign_missing_or_ambiguous_exchange_identity_is_rejected(self):
        for contract in ('BRZ6', None, ''):
            with self.subTest(contract=contract):
                self.response(contract)
                with self.assertRaisesRegex(RuntimeError, 'MOEX_FORTS_CONTRACT_MISMATCH BRX6'):
                    self.fetch('BRX6')
        row = self.response()
        self.client.get.return_value.json.return_value = {'marketdata': [row, deepcopy(row)]}
        with self.assertRaisesRegex(RuntimeError, 'MOEX_FORTS_CONTRACT_MISMATCH BRX6'):
            self.fetch('BRX6')

    def test_existing_market_and_settlement_fallbacks_are_explicit_and_unchanged(self):
        for field in ('MARKETPRICE', 'SETTLEPRICE'):
            with self.subTest(field=field):
                fields = {'LAST': None, 'MARKETPRICE': None, 'SETTLEPRICE': None, field: 100.75}
                self.response(**fields)
                result = self.fetch('BRX6')
                self.assertEqual(result['price'], 100.75)
                self.assertEqual(result['price_field'], field)

    def test_cny_current_quote_behavior_is_unchanged(self):
        self.response('CNYRUBF', LAST=12.755)
        result = self.fetch('CNYRUBF')
        self.assertEqual(result['price'], 12.755)
        self.assertEqual(result['row']['SECID'], 'CNYRUBF')
