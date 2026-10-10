from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import MagicMock, patch

import veritas_execution as VX
import veritas_portfolio as VP
import veritas_position_guard as VPG
import veritas_profit_protection as PP
from veritas_trade_view import enrich_positions


class NetProtectionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.z = dict(asset='CNYRUBF', direction='LONG', units=1000, avg_entry_price=100,
                      last_price=104, stop_price=101, active_trade_id='t1', opened_at=self.now-timedelta(days=3), payload={})
        self.a = dict(trade_id='t1', status='OPEN', gross_pnl_rub=0, fees_rub=50, funding_rub=0,
                      last_mark_at=self.now, last_ruonia=14.1, portfolio_nav_rub=1e6)

    def check(self, **kwargs):
        return PP.evaluate(self.z, self.a, now=self.now, **kwargs)

    def test_stop_above_entry_is_not_enough_to_cover_costs(self):
        self.z['stop_price'] = 100.10
        result = self.check()
        self.assertFalse(result['profit_protection_active'])
        self.assertEqual(result['net_profit_protection']['state'], 'COSTS_NOT_COVERED')
        self.assertLess(result['net_profit_protection']['net_at_stop_rub'], 0)

    def test_partial_pnl_all_booked_costs_and_exit_fee_count_exactly_once(self):
        self.a.update(gross_pnl_rub=-150, fees_rub=125, funding_rub=17)
        self.z['units'] = 500
        result = self.check()['net_profit_protection']
        fill = VX.simulated_fill('CNYRUBF', 'SELL', 101, .0505)['fill_price']
        expected = -150 + 500*(fill-100) - 125 - 17 - 500*fill*.0004
        self.assertAlmostEqual(result['net_at_stop_rub'], expected)
        self.assertAlmostEqual(result['estimated_exit_commission_rub'], 500*fill*.0004)

    def test_realized_profit_can_cover_remainder_below_entry(self):
        self.z['stop_price'] = 99.90
        self.a['gross_pnl_rub'] = 1000
        self.assertTrue(self.check()['profit_protection_active'])

    def test_realized_losses_can_cancel_apparent_stop_profit(self):
        self.a['gross_pnl_rub'] = -2000
        self.assertFalse(self.check()['profit_protection_active'])

    def test_short_uses_adverse_buy_to_cover_and_short_funding(self):
        self.z.update(direction='SHORT', last_price=97, stop_price=99)
        self.a['last_mark_at'] = self.now-timedelta(hours=2)
        result = self.check()['net_profit_protection']
        fill = VX.simulated_fill('CNYRUBF', 'BUY_TO_COVER', 99, .099)['fill_price']
        due = 1000*97*.16*7200/(365.25*86400)
        self.assertAlmostEqual(result['modeled_stop_fill'], fill)
        self.assertAlmostEqual(result['unbooked_funding_rub'], due)
        self.assertAlmostEqual(result['net_at_stop_rub'], 1000*(100-fill)-50-due-1000*fill*.0004)

    def test_funding_can_remove_protection_without_a_stop_change(self):
        self.z['stop_price'] = 100.17
        self.assertTrue(self.check()['profit_protection_active'])
        self.a['last_mark_at'] = self.now-timedelta(days=1)
        self.assertFalse(self.check()['profit_protection_active'])

    def test_break_even_inverts_the_fill_model_for_both_directions_and_large_size(self):
        self.z['units'] = 40000
        self.a.update(gross_pnl_rub=230, fees_rub=2550, funding_rub=187)
        for direction, mark, stop in [('LONG', 106, 102), ('SHORT', 94, 98)]:
            with self.subTest(direction=direction):
                self.z.update(direction=direction, last_price=mark, stop_price=stop)
                be = self.check()['net_profit_protection']['break_even_stop_price']
                self.assertAlmostEqual(self.check(stop=be)['net_profit_protection']['net_at_stop_rub'], 0, places=6)

    def test_missing_funding_or_commission_never_becomes_zero(self):
        for key in ('fees_rub', 'funding_rub', 'last_mark_at', 'portfolio_nav_rub'):
            a = dict(self.a); a.pop(key)
            with self.subTest(key=key):
                result = PP.evaluate(self.z, a, now=self.now)
                self.assertFalse(result['profit_protection_active'])
                self.assertIsNone(result['net_profit_protection']['net_at_stop_rub'])

    def test_crossed_stop_does_not_claim_protected_execution(self):
        self.z['last_price'] = 100.5
        self.assertEqual(self.check()['net_profit_protection']['state'], 'STOP_REACHED')
        self.assertFalse(self.check()['profit_protection_active'])

    def test_cost_check_does_not_move_structural_stop(self):
        z = dict(self.z); self.z['stop_price'] = 100.1
        self.check()
        self.assertEqual(self.z['stop_price'], 100.1)
        self.assertEqual(self.z['payload'], z['payload'])

    def test_previous_cny_entry_and_structural_stop_do_not_guarantee_net_profit(self):
        self.z.update(avg_entry_price=12.60445302, units=15343.083713636657,
                      stop_price=12.613374, last_price=12.652)
        self.a.update(fees_rub=96.6955889252302, portfolio_nav_rub=966955.8892523019)
        self.assertFalse(self.check()['profit_protection_active'])

    def test_old_flag_stale_projection_or_changed_units_cannot_authorize_scaling(self):
        self.z['payload'] = {'profit_protection_active': True, 'trailing_stop': 101}
        self.assertFalse(PP.is_protected(self.z))
        self.z['payload'].update(self.check())
        self.assertTrue(PP.is_protected(self.z))
        self.z['units'] += 10
        self.assertFalse(PP.is_protected(self.z))
        self.z['units'] -= 10
        self.z['payload']['net_profit_protection']['checked_at'] = (self.now-timedelta(minutes=2)).isoformat()
        self.assertFalse(PP.is_protected(self.z))

    def test_projected_add_floor_blocks_add_that_erases_existing_net_protection(self):
        self.z['payload'].update(self.check())
        self.assertTrue(self.check()['profit_protection_active'])
        c = MagicMock()
        cursor = MagicMock(); cursor.fetchall.return_value = [self.a]
        c.execute.return_value = cursor
        # Large expensive add raises the weighted entry enough that the old
        # protected stop no longer covers the whole episode after costs.
        result = PP.projected_add_floor(
            c, self.z, add_fill_price=104, add_units=4000, add_fee_rub=166.4,
            mark_price=104, now=self.now)
        self.assertTrue(result['applied'])
        self.assertFalse(result['eligible'])
        self.assertEqual(result['reason'],'PROTECTED_EPISODE_FLOOR_WOULD_BE_LOST')
        self.assertFalse(result['projected']['profit_protection_active'])

    def test_projected_add_floor_allows_small_add_when_same_stop_remains_net_positive(self):
        self.z['payload'].update(self.check())
        c = MagicMock()
        cursor = MagicMock(); cursor.fetchall.return_value = [self.a]
        c.execute.return_value = cursor
        result = PP.projected_add_floor(
            c, self.z, add_fill_price=104, add_units=50, add_fee_rub=2.08,
            mark_price=104, now=self.now)
        self.assertTrue(result['applied'])
        self.assertTrue(result['eligible'])
        self.assertEqual(result['reason'],'PROTECTED_EPISODE_FLOOR_PRESERVED')
        self.assertTrue(result['projected']['profit_protection_active'])

    def test_aggressive_scale_does_not_bypass_net_check_with_trailing_stop(self):
        self.z['payload'] = {'profit_protection_active': True, 'trailing_stop': 101}
        quality = dict(confirmed=True, super=True, independent=6, rr=2, alignment=4)
        with patch.object(VP, '_v90r24_aggressive_quality', return_value=quality):
            target, meta = VP._v90r24_protected_scale_target(self.z, {'research_decision': 'LONG'}, 104, 1e6, .35)
            self.assertLessEqual(target, 1)
            self.assertFalse(meta['protected'])
            self.z['payload'].update(self.check())
            target, meta = VP._v90r24_protected_scale_target(self.z, {'research_decision': 'LONG'}, 104, 1e6, .35)
            self.assertEqual(target, 5)
            self.assertTrue(meta['protected'])

    def test_dashboard_recomputes_after_reload_instead_of_trusting_legacy_flag(self):
        self.z.update(stop_price=100.1, unrealized_pnl_rub=4000)
        self.z['payload'] = {'profit_protection_active': True}
        report = {'portfolios': [{'name': 'Aggressive', 'positions': [self.z]}]}
        c = MagicMock(); c.__enter__.return_value = c
        c.execute.return_value.fetchall.return_value = [self.a]
        out = enrich_positions(report, lambda: c)['portfolios'][0]['positions'][0]
        self.assertFalse(out['profit_protection_active'])
        self.assertLess(out['net_profit_protection']['net_at_stop_rub'], 0)
        self.assertTrue(self.z['payload']['profit_protection_active'])

    def test_refresh_updates_both_position_and_trade_after_partial_fill(self):
        c = MagicMock()
        def execute(sql, args=None):
            cursor = MagicMock()
            cursor.fetchall.return_value = [self.z] if sql.startswith(PP.REFRESH_POSITIONS_SQL) else [self.a]
            return cursor
        c.execute.side_effect = execute
        PP.refresh(c, now=self.now)
        updates = [x for x in c.execute.call_args_list if x.args[0].startswith('UPDATE')]
        self.assertEqual(len(updates), 2)
        for update in updates:
            rows = json.loads(update.args[1][0])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['trade_id'], self.z['active_trade_id'])
            self.assertEqual(rows[0]['patch'], self.check())
            self.assertTrue(rows[0]['patch']['profit_protection_active'])
            self.assertNotIn('SET stop_price', update.args[0])

    def test_structural_trailing_persists_net_assessment_without_tightening_for_costs(self):
        self.z['stop_price'] = 99
        c = MagicMock()
        def execute(sql, args=None):
            cursor = MagicMock()
            cursor.fetchall.return_value = [self.z] if sql.startswith('SELECT * FROM paper_positions') else [self.a]
            return cursor
        c.execute.side_effect = execute
        with patch.object(VP, '_v90r17_exact_structural_stop', return_value=(100.1, '5m', 100.2)):
            changes = VP._v90tr_apply(c, 'Aggressive', {}, {'CNYRUBF': 104}, self.now.isoformat())
        self.assertEqual(changes[0]['stage'], 'RISK_REDUCTION_STRUCTURAL')
        update = next(x for x in c.execute.call_args_list if 'SET stop_price=%s,payload=' in x.args[0])
        self.assertEqual(update.args[1][0], 100.1)
        p = json.loads(update.args[1][1])
        self.assertFalse(p['profit_protection_active'])
        self.assertLess(p['net_profit_protection']['net_at_stop_rub'], 0)



    def test_missing_stop_repair_uses_only_immutable_initial_stop(self):
        z = dict(self.z)
        z['stop_price'] = None
        z['payload'] = {'initial_stop_price': 99}
        self.assertEqual(VPG.recover_missing_hard_stop(z), 99)

        z['payload']['trailing_stop'] = 100.5
        self.assertIsNone(VPG.recover_missing_hard_stop(z))

        z['payload'] = {'initial_stop_price': 101}
        self.assertIsNone(VPG.recover_missing_hard_stop(z))

        z.update(direction='SHORT', avg_entry_price=100)
        z['payload'] = {'initial_stop_price': 103}
        self.assertEqual(VPG.recover_missing_hard_stop(z), 103)


if __name__ == '__main__':
    unittest.main()
