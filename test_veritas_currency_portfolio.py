"""Isolation and no-execution checks for the new unconfigured currency book."""
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import MagicMock, patch
import veritas_currency_portfolio as C
import veritas_portfolio as P
import veritas_portfolio_runtime as R


class CurrencyPortfolioTests(TestCase):
    def test_existing_challenger_policy_is_unchanged(self):
        self.assertEqual(P.POLICIES['Challenger'], {'threshold':.75,'strong_threshold':.85,
            'min_independent':4,'mode':'CHALLENGER','max_fraction':2.0})
        self.assertEqual(len(P.POLICIES),5)
        self.assertEqual(P.POLICIES['Currency']['allowed_assets'],['CNYRUBF'])
        self.assertEqual(P.POLICIES['Currency']['initial_nav_rub'],0)

    def test_no_currency_entry_or_add_can_reach_database(self):
        c=MagicMock()
        for asset in ('CNYRUBF','GOLD','MOEX','NQ','BTC','ETH','BRENT'):
            for direction in ('LONG','SHORT'):
                row={'asset':asset,'research_decision':direction,'signal_tier':'SUPER_'+direction}
                result=R._open_or_add(c,{},'Currency',asset,direction,12.7,1.,1e6,
                                     datetime.now(timezone.utc).isoformat(),row,'TEST')
                self.assertEqual(result,0.)
                self.assertEqual(row['_execution_audit']['reason'],C.BLOCK_REASON)
        c.execute.assert_not_called()

    def test_no_candidate_router_can_activate_unconfigured_book(self):
        c=MagicMock(); summary=[{'asset':'CNYRUBF','signal_tier':'SUPER_LONG'}]
        d=R._step_one(c,'Currency',P.POLICIES['Currency'],{'CNYRUBF':summary[0]},
                     {'CNYRUBF':12.7},16,84,datetime.now(timezone.utc).isoformat(),.0004,summary)
        self.assertFalse(d['risk_governor']['new_risk'])
        self.assertIsNone(d['nav_rub'])
        c.execute.assert_not_called()
        for asset in ('CNYRUBF','GOLD'):
            a=R._signal_first_admission({'asset':asset},P.POLICIES['Currency'],0)
            self.assertFalse(a['open'])
            self.assertEqual(a['reason'],C.BLOCK_REASON)

    def test_challenger_and_other_books_keep_original_entry_route(self):
        for name in ('Challenger','Champion','Aggressive','Impulse'):
            with patch.object(R,'_v90r79_signal_state',return_value=({},None,None,False)), \
                 patch.object(R,'_v90r79_base_open_or_add',return_value=123) as original:
                result=R._open_or_add(None,{},name,'GOLD','SHORT',4000,.1,1e6,
                                     datetime.now(timezone.utc).isoformat(),{'asset':'GOLD'},'TEST')
                self.assertEqual(result,123)
                original.assert_called_once()

    def test_reporting_keeps_ledgers_separate_and_unfunded(self):
        challenger={'name':'Challenger','positions':[{'asset':'GOLD'}],'closed_trades':90,'nav_rub':950000}
        currency={'name':'Currency','positions':[],'closed_trades':0,'nav_rub':0}
        original={'portfolios':[challenger,currency]}
        out=C.decorate_report(original)
        self.assertIs(out['portfolios'][0],challenger)
        self.assertEqual(out['portfolios'][1]['closed_trades'],0)
        self.assertIsNone(out['portfolios'][1]['nav_rub'])
        self.assertEqual(out['portfolios'][1]['display_name'],'Валютный портфель')
        self.assertEqual(original['portfolios'][1]['nav_rub'],0)
        self.assertFalse(out['portfolios'][1]['live_trading_enabled'])
