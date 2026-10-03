import unittest
from unittest.mock import MagicMock

from veritas_trade_view import enrich_positions, trade_result


class TradeResultTests(unittest.TestCase):
    def test_partial_profit_and_remaining_loss_are_combined_once(self):
        trade = dict(gross_pnl_rub=120, fees_rub=40, funding_rub=5, entry_notional_rub=250,
                     payload={'entry_nav_rub': 1000, 'r17_tp1_done': True,
                              'r17_tp1_at': '2026-09-29T06:47:17Z'})
        result = trade_result(trade, {'unrealized_pnl_rub': -70})
        self.assertEqual(result['total_trade_pnl_rub'], 5)
        self.assertEqual(result['total_trade_return_pct'], 2)
        self.assertEqual(result['trade_return_basis'], 'ENTRY_NOTIONAL')
        self.assertEqual(result['realized_gross_pnl_rub'], 120)
        self.assertTrue(result['tp1_done'])
        self.assertEqual(result['tp1_at'], '2026-09-29T06:47:17Z')

    def test_closed_trade_uses_net_result_not_price_return(self):
        result = trade_result(dict(status='CLOSED', gross_pnl_rub=121.91,
            fees_rub=193.45, funding_rub=3.25, net_pnl_rub=-74.79,
            entry_notional_rub=1000, return_on_entry_nav=-.000077344, exit_reason='TAKE_PROFIT_FULL_MIN_POSITION_R17',
            closed_at='2026-09-29T08:35:21Z'))
        self.assertEqual(result['total_trade_pnl_rub'], -74.79)
        self.assertAlmostEqual(result['total_trade_return_pct'], -7.479)
        self.assertTrue(result['tp1_done'])

    def test_partial_exit_never_shrinks_whole_trade_return_base(self):
        trade = dict(gross_pnl_rub=20, fees_rub=2, funding_rub=1,
                     entry_notional_rub=200, payload={'entry_nav_rub': 1_000_000})
        result = trade_result(trade, dict(unrealized_pnl_rub=3, units=.5, avg_entry_price=100))
        self.assertEqual(result['total_trade_return_pct'], 10)
        self.assertEqual(result['trade_return_basis_rub'], 200)

    def test_missing_fill_basis_never_falls_back_to_portfolio_return(self):
        for basis in (None, 0, -1, float('nan')):
            result = trade_result(dict(status='CLOSED', net_pnl_rub=100,
                entry_notional_rub=basis,return_on_entry_nav=.01,payload={'entry_nav_rub':10000}))
            self.assertIsNone(result['total_trade_return_pct'])
            self.assertEqual(result['trade_return_basis'],'UNAVAILABLE')

    def test_missing_accounting_never_becomes_zero_cost(self):
        result = trade_result({'gross_pnl_rub': 100, 'fees_rub': None, 'funding_rub': 0},
                              {'unrealized_pnl_rub': 20})
        self.assertIsNone(result['total_trade_pnl_rub'])
        self.assertEqual(result['trade_result_status'], 'INCOMPLETE')

    def test_exact_trade_join_survives_reload_without_changing_source_report(self):
        report = {'portfolios': [{'name': 'Impulse', 'positions': [
            {'active_trade_id': 'new', 'asset': 'MOEX', 'unrealized_pnl_rub': -10}]}]}
        connection = MagicMock()
        connection.__enter__.return_value.execute.return_value.fetchall.return_value = [
            dict(trade_id='old', gross_pnl_rub=9999, fees_rub=0, funding_rub=0, payload={}),
            dict(trade_id='new', gross_pnl_rub=50, fees_rub=5, funding_rub=2,
                 payload={'r17_tp1_done': True})]
        result = enrich_positions(report, lambda: connection)
        position = result['portfolios'][0]['positions'][0]
        self.assertEqual(position['total_trade_pnl_rub'], 33)
        self.assertTrue(position['tp1_done'])
        self.assertNotIn('total_trade_pnl_rub', report['portfolios'][0]['positions'][0])
        self.assertEqual(connection.__enter__.return_value.execute.call_count, 1)

    def test_failed_accounting_read_keeps_position_visible(self):
        report = {'portfolios': [{'name': 'Impulse', 'positions': [
            {'active_trade_id': 'new', 'unrealized_pnl_rub': 10}]}]}
        def unavailable():
            raise RuntimeError('temporarily unavailable')
        result = enrich_positions(report, unavailable)
        self.assertEqual(len(result['portfolios'][0]['positions']), 1)
        self.assertIsNone(result['portfolios'][0]['positions'][0]['total_trade_pnl_rub'])
