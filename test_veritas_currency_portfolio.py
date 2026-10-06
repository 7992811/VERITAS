"""Regressions for the configured independent CNYRUBf currency book."""
from unittest import TestCase
from unittest.mock import patch

import veritas_currency_portfolio as C
import veritas_portfolio as P
import veritas_portfolio_runtime as R


class CurrencyPortfolioTests(TestCase):
    def test_core_caps_and_currency_policy_are_canonical(self):
        self.assertEqual(P.POLICIES['Champion']['max_fraction'],1.0)
        self.assertEqual(P.POLICIES['Challenger']['max_fraction'],1.0)
        self.assertEqual(len(P.POLICIES),5)
        pol=P.POLICIES['Currency']
        self.assertEqual(pol['allowed_assets'],['CNYRUBF'])
        self.assertEqual(pol['initial_nav_rub'],10_000.0)
        self.assertEqual(pol['max_gross'],10.0)
        self.assertEqual(pol['leverage_limit'],10.0)
        self.assertEqual(pol['hard_drawdown'],0.35)
        self.assertTrue(pol['weekend_carry_allowed'])
        self.assertTrue(pol['paper_trading_enabled'])
        self.assertFalse(pol['live_trading_enabled'])
        self.assertEqual(pol['initial_normal'],.50)
        self.assertEqual(pol['initial_super'],1.00)
        self.assertEqual(pol['probe_normal'],.05)
        self.assertEqual(pol['probe_super'],.10)

    def test_currency_rejects_every_non_cnyrubf_asset(self):
        for asset in ('GOLD','MOEX','NQ','BTC','ETH','BRENT'):
            out=P._signal_first_admission({'asset':asset},P.POLICIES['Currency'],0)
            self.assertFalse(out['open'])
            self.assertEqual(out['reason'],C.BLOCK_REASON)

    def test_currency_uses_canonical_admission_not_setup_pending_block(self):
        with patch.object(R.VCR,'evaluate',
                          return_value={'open':True,'fraction':.50,'reason':'TEST_PASS'}):
            out=P._signal_first_admission({'asset':'CNYRUBF'},P.POLICIES['Currency'],0)
        self.assertTrue(out['open'])
        self.assertEqual(out['fraction'],.50)
        self.assertEqual(out['canonical_policy_version'],R.CTC.VERSION)

    def test_currency_timing_blocks_are_soft_but_cost_blocks_stay_hard(self):
        self.assertIn('R69_WAIT_LOCAL_BREAKOUT',R._R79_SOFT_ECON_BLOCKERS)
        self.assertIn('R69_BREAKOUT_ACTIVITY_REQUIRED',R._R79_SOFT_ECON_BLOCKERS)
        self.assertNotIn('EXPECTED_MOVE_BELOW_COST_BUFFER',R._R79_SOFT_ECON_BLOCKERS)
        self.assertNotIn('TARGET_NOT_PROFITABLE_AFTER_COSTS',R._R79_SOFT_ECON_BLOCKERS)

    def test_currency_drawdown_profile_matches_owner_limit(self):
        p=P._v90r35_profile('CURRENCY','Currency')
        self.assertEqual(p['name'],'CURRENCY')
        self.assertEqual(p['hard_drawdown'],0.35)
        self.assertEqual(p['normal_max_gross'],10.0)

    def test_reporting_adds_configured_currency_when_missing(self):
        original={'portfolios':[{'name':'Champion','positions':[],'gross_leverage':0.0}]}
        out=C.decorate_report(original)
        cur=next(p for p in out['portfolios'] if p['name']=='Currency')
        self.assertEqual(cur['nav_rub'],10_000.0)
        self.assertEqual(cur['configuration_status'],'CONFIGURED')
        self.assertEqual(cur['max_gross_limit'],10.0)
        self.assertFalse(cur['live_trading_enabled'])

    def test_reporting_repairs_currency_return_base(self):
        original={'portfolios':[{'name':'Currency','nav_rub':10_000.0,
                                 'total_return_pct':-99.0,'positions':[]}]}
        cur=C.decorate_report(original)['portfolios'][0]
        self.assertEqual(cur['initial_nav_rub'],10_000.0)
        self.assertEqual(cur['total_return_pct'],0.0)


if __name__ == '__main__':
    import unittest
    unittest.main()
