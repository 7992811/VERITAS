"""The observed ProFinance row identifies a feed, never an exchange month."""
from copy import deepcopy
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import MagicMock, patch

import veritas_brent_market as B
import veritas_price_source as S
import veritas_profinance as PF
import veritas_trade_view as V
from test_veritas_brent_market import history, NOW as MARKET_NOW


NOW = datetime(2026, 10, 7, 10, 54, 2, tzinfo=timezone.utc)
OBSERVED = '1;I=27;S=Brent oil;TICK=brent;LP=+102.08;T=13:54:02'


def position():
    lock = S.identity('BRENT', {'source': 'ProFinance', 'raw_label': 'Brent oil'})
    return {'asset': 'BRENT', 'direction': 'LONG', 'avg_entry_price': 101.220472,
            'last_price': 102.03, 'units': 3132.252321200961,
            'payload': {'price_source_lock': lock}}


class BrentValuationBasisTests(TestCase):
    def test_observed_row_id_is_not_a_futures_contract(self):
        quote = PF.parse_quotes(OBSERVED, NOW)['BRENT']
        basis = S.valuation_basis(position(), quote)
        self.assertEqual(basis['provider_instrument_id'], '27')
        self.assertEqual(basis['provider_ticker'], 'brent')
        self.assertTrue(basis['provider_ticker_verified'])
        self.assertEqual(basis['source_pin_status'], 'PINNED_PROVIDER_FEED')
        self.assertTrue(basis['provider_series_verified'])
        self.assertEqual(basis['price_field'], 'LP')
        self.assertIsNone(basis['contract_id'])
        self.assertFalse(basis['exact_contract_verified'])
        self.assertEqual(basis['contract_identity_status'], 'UNVERIFIED_PROVIDER_SERIES')
        self.assertEqual(basis['price_series_type'], 'UNVERIFIED')

    def test_same_label_with_another_ticker_is_rejected_at_both_boundaries(self):
        self.assertNotIn('BRENT', PF.parse_quotes(OBSERVED.replace('TICK=brent', 'TICK=WTI'), NOW))
        quote = PF.parse_quotes(OBSERVED, NOW)['BRENT']
        quote['raw_ticker'] = 'WTI'
        self.assertIsNone(S.identity('BRENT', quote))
        self.assertFalse(S.matches(position(), quote))

    def test_legacy_label_only_quote_cannot_verify_or_value_the_held_feed(self):
        self.assertNotIn('BRENT', PF.parse_quotes(OBSERVED.replace(';TICK=brent', ''), NOW))
        quote = {'source': 'ProFinance', 'raw_label': 'Brent oil', 'price': 102.08}
        basis = S.valuation_basis(position(), quote)
        self.assertFalse(S.matches(position(), quote))
        self.assertFalse(basis['provider_series_verified'])
        self.assertEqual(basis['source_pin_status'], 'AWAITING_PROVIDER_VERIFICATION')
        self.assertIsNone(basis['contract_id'])

    def test_market_preserves_provider_identity_but_cannot_infer_month(self):
        quote = PF.parse_quotes(OBSERVED, NOW)['BRENT']
        quote['observed_at'] = MARKET_NOW.isoformat()
        market = B.build_market(quote, history(), MARKET_NOW)
        self.assertTrue(market['paper_eligible'])
        self.assertEqual(market['instrument_id'], '27')
        self.assertEqual(market['raw_ticker'], 'brent')
        self.assertEqual(market['source_pin_status'], 'PINNED_PROVIDER_FEED')
        self.assertEqual(S.identity('BRENT', market), S.brent_feed_pin_identity())
        self.assertFalse(market['exact_contract_verified'])
        self.assertIsNone(S.identity('BRENT', market)['contract_id'])
        quote['raw_ticker'] = 'WTI'
        rejected = B.build_market(quote, history(), MARKET_NOW)
        self.assertFalse(rejected['paper_eligible'])
        self.assertIsNone(rejected['structure_quote']['price'])

    def test_execution_refresh_replaces_instrument_metadata_with_the_quote(self):
        old = PF.parse_quotes(OBSERVED, NOW)['BRENT']
        replacement = dict(old, instrument_id='99')
        replacement.pop('raw_ticker')
        replacement.pop('provider_ticker_verified')
        row = S.execution_row(dict(old, _execution_quote=replacement))
        self.assertEqual(row['instrument_id'], '99')
        self.assertNotIn('raw_ticker', row)
        self.assertNotIn('provider_ticker_verified', row)

    def test_foreign_contract_cannot_become_the_displayed_basis_or_mark(self):
        held = position()
        held['payload']['source_locked_mark'] = {
            'identity': held['payload']['price_source_lock'], 'price': 102.03,
            'observed_at': '2026-10-07T10:47:19+00:00'}
        foreign = {'source': 'MOEX ISS BRX6', 'contract': {'secid': 'BRX6'},
                   'price': 99.0, 'instrument_id': 'other', 'exact_contract_verified': True}
        basis = S.valuation_basis(held, foreign)
        self.assertIsNone(basis['contract_id'])
        self.assertIsNone(basis['provider_instrument_id'])
        self.assertFalse(basis['exact_contract_verified'])
        self.assertEqual(S.frozen_price(held), 102.03)

    def test_report_discloses_unknown_month_without_rewriting_position_or_pnl(self):
        held = position()
        report = {'portfolios': [{'name': 'Aggressive', 'positions': [held]}]}
        original = deepcopy(report)
        quote = PF.parse_quotes(OBSERVED, NOW)['BRENT']
        with patch.object(V.VPG, 'quote_for_position', return_value=quote), \
             patch.object(V.VPP, 'evaluate', return_value={}):
            enriched = V.enrich_positions(report, MagicMock())
        shown = enriched['portfolios'][0]['positions'][0]
        self.assertEqual(report, original)
        self.assertEqual(shown['avg_entry_price'], 101.220472)
        self.assertEqual(shown['last_price'], 102.08)
        self.assertAlmostEqual(shown['unrealized_pnl_rub'], held['units']*(102.08-101.220472))
        self.assertEqual(shown['valuation_basis']['contract_identity_status'], 'UNVERIFIED_PROVIDER_SERIES')

    def test_saved_contract_has_priority_over_provider_level_description(self):
        held = position()
        held['payload']['price_source_lock'] = S.identity('BRENT', {
            'source': 'MOEX ISS BRX6', 'contract': {'secid': 'BRX6'}})
        basis = S.valuation_basis(held)
        self.assertEqual(basis['contract_id'], 'BRX6')
        self.assertEqual(basis['contract_identity_status'], 'PINNED_CONTRACT')
        self.assertFalse(basis['exact_contract_verified'])  # No current quote proof.
