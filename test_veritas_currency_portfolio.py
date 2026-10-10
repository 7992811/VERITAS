"""Regressions for the configured independent CNYRUBf currency book."""
from unittest import TestCase
from unittest.mock import patch
from datetime import datetime, timezone, timedelta

import veritas_currency_portfolio as C
import veritas_currency_dashboard as CD
import veritas_portfolio as P
import veritas_portfolio_runtime as R
import veritas_canonical_runtime as VCR
import veritas_canonical_constitution as CTC
from test_veritas_timeframe_policy import structural_row


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
        self.assertTrue(pol['live_trading_capable'])
        self.assertTrue(pol['live_trading_enabled'])
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

    def test_currency_router_prefers_execution_timeframe_over_daily_rank(self):
        rows=[
            {'asset':'CNYRUBF','horizon':'5m','research_decision':'LONG','confidence':.51,
             'horizon_structure':{'state':'CONFIRMED_TREND','score':.60,'direction':'LONG'}},
            {'asset':'CNYRUBF','horizon':'1d','research_decision':'LONG','confidence':.90,
             'horizon_structure':{'state':'NEUTRAL','score':.30,'direction':'NO_TRADE'}},
        ]
        row=R.VCR.currency_candidate_book(rows)['CNYRUBF']
        self.assertEqual(row['horizon'],'5m')

    def test_currency_router_flags_senior_signal_against_confirmed_4h_structure(self):
        rows=[
            {'asset':'CNYRUBF','horizon':'1d','research_decision':'LONG','confidence':.90,
             'horizon_structure':{'state':'NEUTRAL','score':.30,'direction':'NO_TRADE'}},
            {'asset':'CNYRUBF','horizon':'4h','research_decision':'NO_TRADE','confidence':0,
             'horizon_structure':{'state':'CONFIRMED_TREND','score':.75,'direction':'SHORT'}},
        ]
        row=R.VCR.currency_candidate_book(rows)['CNYRUBF']
        self.assertTrue(row['_currency_mtf_conflict'])

    def _currency_structural(self, probability, independent=2):
        now=datetime.now(timezone.utc).replace(microsecond=0)
        row=structural_row(now, asset='CNYRUBF', price=12.345, width=.26)
        row['calibrated_probability']=probability
        row['independent_evidence_families']=independent
        return row,now

    def test_currency_normal_entry_requires_62pct_and_two_independent_confirmations(self):
        row,now=self._currency_structural(.619,2)
        blocked=VCR.evaluate(row,CTC.runtime_portfolio_policy('Currency'),0.,now)
        self.assertFalse(blocked['open'])
        self.assertEqual(blocked['reason'],'CURRENCY_PROBABILITY_BELOW_THRESHOLD')
        row['calibrated_probability']=.62
        row['independent_evidence_families']=1
        blocked=VCR.evaluate(row,CTC.runtime_portfolio_policy('Currency'),0.,now)
        self.assertFalse(blocked['open'])
        self.assertEqual(blocked['reason'],'CURRENCY_INDEPENDENT_EVIDENCE_REQUIRED')
        row['independent_evidence_families']=2
        admitted=VCR.evaluate(row,CTC.runtime_portfolio_policy('Currency'),0.,now)
        self.assertTrue(admitted['open'],admitted)
        self.assertEqual(admitted['role_gate']['entry_mode'],'NORMAL')
        self.assertEqual(admitted['role_gate']['strength'],'NORMAL')

    def test_currency_strong_threshold_uses_super_initial_size(self):
        normal,now=self._currency_structural(.62,2)
        strong,_=self._currency_structural(.74,2)
        a=VCR.evaluate(normal,CTC.runtime_portfolio_policy('Currency'),0.,now)
        b=VCR.evaluate(strong,CTC.runtime_portfolio_policy('Currency'),0.,now)
        self.assertTrue(a['open'],a); self.assertTrue(b['open'],b)
        self.assertEqual(b['role_gate']['strength'],'STRONG')
        self.assertGreaterEqual(b['fraction'],a['fraction'])

    def test_currency_game_changer_bypasses_probability_only_after_structural_assessor(self):
        row,now=self._currency_structural(.10,2)
        with patch.object(VCR.VTDE,'event_impulse_assess',
                          return_value={'eligible':True,'immediate_max':True,'phase':'GAME_CHANGER_EXTREME'}):
            admitted=VCR.evaluate(row,CTC.runtime_portfolio_policy('Currency'),0.,now)
        self.assertTrue(admitted['open'],admitted)
        self.assertEqual(admitted['role_gate']['entry_mode'],'GAME_CHANGER')
        self.assertTrue(admitted['role_gate']['probability_bypass'])

    def test_currency_drawdown_profile_matches_owner_limit(self):
        p=P._v90r35_profile('CURRENCY','Currency')
        self.assertEqual(p['name'],'CURRENCY')
        self.assertEqual(p['hard_drawdown'],0.35)
        self.assertEqual(p['normal_max_gross'],10.0)

    def test_reporting_adds_configured_currency_when_missing(self):
        original={'portfolios':[{'name':'Champion','positions':[],'gross_leverage':0.0}]}
        out=C.decorate_report(original)
        cur=next(p for p in out['portfolios'] if p['name']=='Currency')
        self.assertIsNone(cur['nav_rub'])
        self.assertEqual(cur['initial_nav_rub'],10_000.0)
        self.assertEqual(cur['positions_status'],'UNAVAILABLE')
        self.assertFalse(out['positions_complete'])
        self.assertEqual(cur['configuration_status'],'CONFIGURED')
        self.assertEqual(cur['max_gross_limit'],10.0)
        self.assertTrue(cur['live_trading_capable'])
        self.assertTrue(cur['live_trading_enabled'])

    def test_reporting_filters_currency_trace_and_positions_to_cny_only(self):
        original={'portfolios':[{'name':'Currency','nav_rub':10_000.0,
            'positions':[{'asset':'CNYRUBF'},{'asset':'BTC'}],
            'admission_trace':[{'asset':'BTC','reason':'X'},
                               {'asset':'CNYRUBF','direction':'LONG','reason':'Y'}]}]}
        cur=C.decorate_report(original)['portfolios'][0]
        self.assertEqual([x['asset'] for x in cur['positions']],['CNYRUBF','BTC'])
        self.assertEqual([x['asset'] for x in cur['quarantined_positions']],['BTC'])
        self.assertEqual([x['asset'] for x in cur['admission_trace']],['CNYRUBF'])
        self.assertEqual(cur['current_cny_admission']['reason'],'Y')
        self.assertEqual(cur['portfolio_integrity_warning'],'NON_CNY_POSITION_QUARANTINED')

    def test_reporting_repairs_currency_return_base(self):
        original={'portfolios':[{'name':'Currency','nav_rub':10_000.0,
                                 'total_return_pct':-99.0,'positions':[]}]}
        cur=C.decorate_report(original)['portfolios'][0]
        self.assertEqual(cur['initial_nav_rub'],10_000.0)
        self.assertEqual(cur['total_return_pct'],0.0)

    def test_currency_canonical_display_name_is_currency(self):
        self.assertEqual(C.DISPLAY_NAME,'Currency')

    def _live_account(self, **changes):
        t=datetime(2026,10,9,12,0,tzinfo=timezone.utc)
        row={
            'allocation_rub':'10000','tick_size':'0.001','tick_value_rub':'1','lot_size':1,
            'signed_lots':0,'average_entry_price':None,'realized_pnl_rub':'100',
            'fees_rub':'4','funding_rub':'0','high_water_rub':'10096',
            'costs_reconciled':True,'ledger_revision':2,'reconciled_revision':2,
            'broker_signed_lots':0,'broker_observed_at':t,'last_execution_at':t,
            'last_mark_price':'12.8','last_mark_observed_at':t,
            'excursion_entry_price':None,'excursion_direction':None,
            'mfe_price':None,'mae_price':None,'excursion_started_at':None,
            'excursion_observation_count':0,'excursion_last_observed_at':None,
            'excursion_max_gap_seconds':None,'protection_test_started_at':None,
            'protection_qualified_at':None,
            'held_stop_price':None,'held_target_price':None,'held_horizon':None,
            'held_opened_at':None,
        }
        row.update(changes)
        return row

    def test_live_currency_round_trip_replaces_paper_currency_history(self):
        t=datetime(2026,10,9,11,0,tzinfo=timezone.utc)
        fills=[
            {'trade_id':'fill-open','client_order_id':'order-open','side':'BUY','lots':1,
             'price':'12.700','fee_rub':None,'executed_at':t,'action':'OPEN',
             'meta_direction':'LONG','horizon':'5m','stop_price':'12.650',
             'target_price':'12.800','exit_reason':None,'manual':False,'signal_tier':'LONG'},
            {'trade_id':'fill-close','client_order_id':'order-close','side':'SELL','lots':1,
             'price':'12.800','fee_rub':None,'executed_at':t+timedelta(hours=1),'action':'CLOSE',
             'meta_direction':'LONG','horizon':'5m','stop_price':None,
             'target_price':None,'exit_reason':'TAKE_PROFIT','manual':False,'signal_tier':None,
             'currency_excursion_snapshot':{
                 'version':'CURRENCY_LIVE_EXCURSION_V1','direction':'LONG',
                 'entry_price':'12.700','mfe_price':'12.850','mae_price':'12.680',
                 'mfe_pct':'1.1811023622','mae_pct':'0.1574803150',
                 'observation_count':8,'max_gap_seconds':'15','horizon':'5m',
                 'protection_test_started_at':(t+timedelta(minutes=5)).isoformat(),
                 'protection_qualified_at':(t+timedelta(minutes=7)).isoformat()}},
        ]
        fees=[
            {'client_order_id':'order-open','cumulative_fee_rub':'2','filled_lots':1},
            {'client_order_id':'order-close','cumulative_fee_rub':'2','filled_lots':1},
        ]
        live=CD.project_live_currency(self._live_account(),fills,fees,[],checked_at=t+timedelta(hours=1))
        self.assertEqual(live['portfolio']['display_name'],'Currency')
        self.assertEqual(len(live['trades']),1)
        trade=live['trades'][0]
        self.assertEqual(trade['portfolio_name'],'Currency')
        self.assertEqual(trade['execution_source'],'LIVE_BROKER_LEDGER')
        self.assertAlmostEqual(trade['gross_pnl_rub'],100.0)
        self.assertAlmostEqual(trade['net_pnl_rub'],96.0)
        self.assertAlmostEqual(trade['mfe_pct'],1.1811023622)
        self.assertAlmostEqual(trade['mae_pct'],-0.1574803150)
        self.assertGreater(trade['capture_ratio'],0.0)
        self.assertGreater(trade['live_giveback_pct'],0.0)
        shadow=trade['payload']['currency_live_management_shadow']
        self.assertTrue(shadow['mfe_threshold_reached'])
        self.assertFalse(shadow['automatic_action'])
        merged=CD.merge_trade_history(
            [{'trade_id':'paper-cur','portfolio_name':'Currency','closed_at':t.isoformat()},
             {'trade_id':'paper-champ','portfolio_name':'Champion','closed_at':t.isoformat()}],
            live,limit=80)
        self.assertNotIn('paper-cur',[x['trade_id'] for x in merged])
        self.assertIn('paper-champ',[x['trade_id'] for x in merged])
        self.assertIn(trade['trade_id'],[x['trade_id'] for x in merged])

    def test_live_currency_dashboard_uses_portfolio_native_risk_governor(self):
        t=datetime(2026,10,9,11,0,tzinfo=timezone.utc)
        live=CD.project_live_currency(self._live_account(),[],[],[],checked_at=t)
        risk=live['portfolio']['risk_governor']
        self.assertEqual(risk['profile'],'CURRENCY')
        self.assertEqual(risk['state'],'NORMAL')
        self.assertEqual(risk['max_gross'],10.0)
        self.assertEqual(risk['hard_drawdown_limit'],.35)
        self.assertEqual(risk['max_stop_risk_nav'],.15)

    def test_live_currency_open_position_is_visible_from_live_ledger(self):
        t=datetime(2026,10,9,11,0,tzinfo=timezone.utc)
        account=self._live_account(
            signed_lots=1,average_entry_price='12.700',realized_pnl_rub='0',
            fees_rub='2',high_water_rub='10048',broker_signed_lots=1,
            last_mark_price='12.750',last_mark_observed_at=t+timedelta(minutes=5),
            excursion_entry_price='12.700',excursion_direction='LONG',
            mfe_price='12.780',mae_price='12.690',excursion_started_at=t,
            excursion_observation_count=6,excursion_last_observed_at=t+timedelta(minutes=5),
            excursion_max_gap_seconds='15',
            protection_test_started_at=t+timedelta(minutes=1),
            protection_qualified_at=t+timedelta(minutes=3),
            held_stop_price='12.650',held_target_price='12.800',
            held_horizon='5m',held_opened_at=t.isoformat())
        fills=[{'trade_id':'fill-open','client_order_id':'order-open','side':'BUY','lots':1,
                'price':'12.700','fee_rub':None,'executed_at':t,'action':'OPEN',
                'meta_direction':'LONG','horizon':'5m','stop_price':'12.650',
                'target_price':'12.800','exit_reason':None,'manual':True,'signal_tier':'LONG'}]
        fees=[{'client_order_id':'order-open','cumulative_fee_rub':'2','filled_lots':1}]
        live=CD.project_live_currency(account,fills,fees,[],checked_at=t+timedelta(minutes=5))
        cur=CD.overlay_portfolio(
            {'status':'OK','positions_complete':True,'accounting_complete':True,
             'portfolios':[{'name':'Currency','positions':[],
                            'admission_trace':[{'asset':'CNYRUBF','reason':'KEEP'}],
                            'risk_governor':{'state':'NORMAL'}}]},
            live)['portfolios'][0]
        self.assertEqual(len(cur['positions']),1)
        pos=cur['positions'][0]
        self.assertEqual(pos['position_source'],'LIVE_BROKER_LEDGER')
        self.assertEqual(pos['direction'],'LONG')
        self.assertAlmostEqual(pos['unrealized_pnl_rub'],50.0)
        self.assertAlmostEqual(pos['total_trade_pnl_rub'],48.0)
        self.assertGreater(pos['mfe_pct'],0.15)
        self.assertLess(pos['mae_pct'],0.0)
        self.assertEqual(pos['management_evidence_status'],'DURABLE_LIVE_PATH')
        self.assertTrue(pos['currency_live_management_shadow']['profit_protection_candidate'])
        self.assertFalse(pos['currency_live_management_shadow']['automatic_action'])
        self.assertEqual(cur['admission_trace'][0]['reason'],'KEEP')
        self.assertTrue(cur['live_trading_enabled'])


if __name__ == '__main__':
    import unittest
    unittest.main()
