import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import veritas_costs as C
import veritas_execution as X
import veritas_portfolio as P
import veritas_profit_protection as PP


class CostPolicyTests(unittest.TestCase):
    def test_fixed_four_bps_per_side_all_assets_and_sizes(self):
        for asset in X.PAPER_ASSETS:
            for size in (.05, .5, 1., 5.):
                for side, expected in [('BUY', 100.04), ('SELL', 99.96),
                                       ('SELL_SHORT', 99.96), ('BUY_TO_COVER', 100.04)]:
                    with self.subTest(asset=asset, size=size, side=side):
                        f = X.simulated_fill(asset, side, 100, size)
                        self.assertAlmostEqual(f['fill_price'], expected)
                        self.assertEqual(f['size_impact_bps'], 0)
        self.assertAlmostEqual(P.COMMISSION, .0004)

    def test_quote_spread_is_preserved_and_slippage_is_exact(self):
        self.assertAlmostEqual(X.simulated_fill('BTC', 'BUY', 100, bid=99.99, ask=100.01)['fill_price'], 100.01*1.0004)
        self.assertAlmostEqual(X.simulated_fill('BTC', 'SELL', 100, bid=99.99, ask=100.01)['fill_price'], 99.99*.9996)

    def plan(self, hours):
        return dict(direction='LONG', entry_price=100, stop_price=99.8,
                    target_price=101, expected_move_pct=.01, expected_to_stop_ratio=5,
                    expected_hold_seconds=hours*3600, horizon='1h')

    def test_admission_charges_both_legs_and_free_intraday(self):
        g = X.economics_gate('CNYRUBF', self.plan(24))
        self.assertEqual(g['modeled_funding_pct'], 0)
        self.assertAlmostEqual(g['modeled_commission_pct'], .0004*(100.04+101*.9996)/100)
        self.assertAlmostEqual(g['modeled_execution_cost_pct'], (.04+101*.0004)/100)
        self.assertEqual(g['minimum_expected_move_pct'], max(.0019, 1.1*g['modeled_round_trip_cost_pct']))
        self.assertAlmostEqual(X.round_trip_cost_pct(), .0016)
        self.assertAlmostEqual(X.minimum_expected_move_pct(.003), .006)
        self.assertTrue(g['eligible'])

    def test_funding_boundary_and_no_retroactive_first_day(self):
        for seconds in (0, 3600, 86399, 86400):
            self.assertEqual(C.funding_fraction(seconds), 0)
        self.assertAlmostEqual(C.funding_fraction(86401), .16/C.YEAR_SECONDS)
        self.assertAlmostEqual(C.funding_fraction(48*3600), .16/365.25)
        g = X.economics_gate('CNYRUBF', self.plan(36))
        self.assertAlmostEqual(g['modeled_funding_pct'], 1.0004*.16*12/8766)

    def test_accounting_splits_at_24_hours_and_uses_transaction_clock(self):
        opened = datetime(2026, 10, 5, 9, tzinfo=timezone.utc)
        z = dict(asset='CNYRUBF', direction='SHORT', opened_at=opened,
                 units=10000, last_price=100, active_trade_id='t')
        p = dict(name='Champion', last_mark_at=opened+timedelta(hours=23))
        c = MagicMock()
        first = P._apply_funding(c, p, [z], {}, 99, opened+timedelta(hours=25))
        self.assertAlmostEqual(first, 1e6*.16/8766)
        self.assertEqual(c.execute.call_count, 2)
        self.assertAlmostEqual(c.execute.call_args_list[0].args[1][0], first)
        p['last_mark_at'] = opened+timedelta(hours=25)
        c.reset_mock()
        self.assertEqual(P._apply_funding(c, p, [z], {}, 99, p['last_mark_at']), 0)
        c.execute.assert_not_called()
        second = P._apply_funding(c, p, [z], {}, 0, opened+timedelta(hours=26))
        self.assertAlmostEqual(first+second, 1e6*.16*2/8766)

    def test_each_position_has_own_free_period(self):
        now = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
        p = dict(name='Aggressive', last_mark_at=now-timedelta(hours=2))
        positions = [dict(asset=a, direction=d, units=1000, last_price=100,
                          active_trade_id=a, opened_at=now-timedelta(hours=age))
                     for a,d,age in [('BTC','LONG',23), ('ETH','SHORT',25)]]
        c = MagicMock()
        result = P._apply_funding(c,p,positions,{},.1,now)
        self.assertAlmostEqual(result, 100000*.16/8766)
        self.assertEqual(c.execute.call_args_list[0].args[1][1], 'ETH')

    def test_profit_protection_matches_book_funding_without_ruonia(self):
        now = datetime(2026,10,6,12,tzinfo=timezone.utc)
        z = dict(asset='CNYRUBF', direction='LONG', units=1000, avg_entry_price=100,
                 last_price=102, stop_price=101, opened_at=now-timedelta(hours=25), payload={})
        account = dict(status='OPEN', gross_pnl_rub=0, fees_rub=40, funding_rub=7,
                       last_mark_at=now-timedelta(hours=2), portfolio_nav_rub=1e6)
        result = PP.evaluate(z,account,now=now)['net_profit_protection']
        self.assertAlmostEqual(result['unbooked_funding_rub'], 102000*.16/8766)
        self.assertEqual(result['booked_funding_rub'], 7)
        z['opened_at'] = now-timedelta(hours=23)
        self.assertEqual(PP.evaluate(z,account,now=now)['net_profit_protection']['unbooked_funding_rub'], 0)


if __name__ == '__main__':
    unittest.main()
