"""Risk regression: actual fills/costs, held exposure and reduced-trade P&L."""
import copy
import math
import unittest

import veritas_canonical_constitution as CTC
import veritas_costs as VC
import veritas_execution as VX
import veritas_stop_risk as R


def economics(direction="LONG", entry=100.0, stop=98.0, target=110.0, hold=300.0):
    """Real gate output, not a synthetic PASS with missing accounting fields."""
    return VX.economics_gate("ETH", dict(
        direction=direction, entry_price=entry, stop_price=stop, target_price=target,
        expected_move_pct=abs(target-entry)/entry,
        expected_to_stop_ratio=abs(target-entry)/abs(entry-stop),
        initial_position_fraction=1.0, horizon="5m", expected_hold_seconds=hold))


class FullStopCostTests(unittest.TestCase):
    def test_long_stop_risk_contains_both_fees_and_each_fill_slippage_once(self):
        entry, stop = 100.04, 98.0
        stop_fill = 97.9608
        result = R.stop_risk_components("LONG", entry, stop, stop_fill)
        self.assertTrue(result["eligible"])
        expected = (100.04-97.9608) + .0004*(100.04+97.9608)
        self.assertAlmostEqual(result["net_risk_per_unit"], expected)
        self.assertAlmostEqual(result["net_risk_pct"], expected/100.04)
        self.assertEqual(result["expected_funding_per_unit"], 0)

    def test_short_stop_risk_contains_adverse_cover_and_both_fees(self):
        result = R.stop_risk_components("SHORT", 99.96, 102, 102.0408)
        self.assertTrue(result["eligible"])
        expected = (102.0408-99.96) + .0004*(99.96+102.0408)
        self.assertAlmostEqual(result["net_risk_per_unit"], expected)

    def test_components_agree_with_real_gate_for_long_short_and_carry(self):
        for direction, stop, target in (("LONG", 98, 110), ("SHORT", 102, 90)):
            for hold in (300, 86400, 3*86400):
                with self.subTest(direction=direction, hold=hold):
                    gate = economics(direction, stop=stop, target=target, hold=hold)
                    self.assertTrue(gate["eligible"])
                    cost = R.stop_risk_components(direction, gate["modeled_entry_fill"],
                        stop, gate["modeled_stop_fill"], expected_hold_seconds=hold)
                    self.assertAlmostEqual(cost["net_risk_pct"], gate["net_risk_pct"])

    def test_funding_free_period_and_original_position_age_for_adds(self):
        new = R.stop_risk_components("LONG", 100.04, 98, 97.9608,
                                     expected_hold_seconds=86400)
        add = R.stop_risk_components("LONG", 100.04, 98, 97.9608,
            expected_hold_seconds=86400, position_age_seconds=2*86400)
        crossing = R.stop_risk_components("LONG", 100.04, 98, 97.9608,
            expected_hold_seconds=7200, position_age_seconds=23*3600)
        self.assertEqual(new["expected_funding_per_unit"], 0)
        self.assertAlmostEqual(add["expected_funding_per_unit"],100.04*.16*86400/VC.YEAR_SECONDS)
        self.assertAlmostEqual(crossing["expected_funding_per_unit"],100.04*.16*3600/VC.YEAR_SECONDS)

    def test_zero_reversed_and_nonfinite_geometry_never_get_a_budget(self):
        cases = [("LONG", 100, 100, 99.96), ("LONG", 100, 101, 100.95),
                 ("SHORT", 100, 99, 99.04), ("LONG", 0, 98, 97.96),
                 ("LONG", 100, -1, 97.96), ("LONG", math.nan, 98, 97.96),
                 ("LONG", 100, 98, math.inf), ("UNKNOWN", 100, 98, 97.96)]
        for args in cases:
            with self.subTest(args=args):
                self.assertFalse(R.stop_risk_components(*args)["eligible"])
        self.assertEqual(R.stop_risk_components("LONG",100,98,98.01)["reason"],
                         "STOP_FILL_NOT_ADVERSE")
        self.assertEqual(R.stop_risk_components("LONG",100,98,97.96,
            expected_hold_seconds=-1)["reason"], "EXPECTED_FUNDING_HORIZON_INVALID")


class NetBudgetSizingTests(unittest.TestCase):
    def setUp(self):
        self.policy = CTC.runtime_portfolio_policy("Aggressive")

    def test_default_automatic_budget_sizes_to_fifteen_percent_with_costs(self):
        gate = economics('LONG', stop=95, target=115)
        result = R.cap_fraction_from_economics(gate, 5.0, self.policy)
        self.assertTrue(result['eligible'])
        self.assertEqual(result['risk_cap_nav'], .15)
        self.assertGreater(result['total_stop_risk_nav_after'], .14)
        self.assertLessEqual(result['total_stop_risk_nav_after'], .15)
        self.assertGreater((result['fraction']+.05)*gate['net_risk_pct'], .15)

    def test_price_only_one_x_would_breach_budget_but_net_cap_reduces_to_point_nine(self):
        for direction, stop, target in (("LONG",98,110),("SHORT",102,90)):
            gate=economics(direction, stop=stop, target=target)
            before=copy.deepcopy(gate)
            result=R.cap_fraction_from_economics(gate,1.0,self.policy,risk_cap_nav=.02)
            self.assertTrue(result["eligible"])
            self.assertAlmostEqual(result["fraction"],.9)
            self.assertLessEqual(result["total_stop_risk_nav_after"],.02)
            self.assertGreater(gate["net_risk_pct"],.02)
            self.assertEqual(gate,before)  # No stop/target repair to make the size fit.

    def test_missing_final_costs_fail_closed_without_price_distance_fallback(self):
        for field in ("net_risk_pct","net_reward_pct","net_reward_risk",
                      "modeled_entry_fill","modeled_stop_fill","modeled_target_fill","eligible"):
            gate=economics();gate.pop(field)
            with self.subTest(field=field):
                result=R.cap_fraction_from_economics(gate,.5,self.policy)
                self.assertFalse(result["eligible"])
                self.assertEqual(result["add_fraction"],0)

    def test_eth_bad_target_economics_cannot_become_a_small_probe(self):
        gate=economics("SHORT",2685.34,2696.7828,2679.63)
        self.assertFalse(gate["eligible"])
        self.assertIn("NET_REWARD_RISK_BELOW_FLOOR",gate["blockers"])
        for requested in (.05,.10,.50,1.0):
            result=R.cap_fraction_from_economics(gate,requested,self.policy)
            self.assertFalse(result["eligible"])
            self.assertEqual(result["fraction"],0)

    def test_forged_gross_risk_cannot_omit_commission(self):
        gate=economics()
        gate["net_risk_pct"]=abs(gate["modeled_entry_fill"]-gate["modeled_stop_fill"])/gate["modeled_entry_fill"]
        gate["net_reward_risk"]=gate["net_reward_pct"]/gate["net_risk_pct"]
        result=R.cap_fraction_from_economics(gate,.5,self.policy)
        self.assertEqual(result["reason"],"NET_STOP_RISK_COSTS_INCOMPLETE")

    def test_add_reserves_held_risk_and_rounds_only_increment(self):
        # A different new signal target/stop must already have been replaced by
        # the held executable stop/target before this final economics boundary.
        for direction,old_entry,stop,target in (("LONG",100,101,115),("SHORT",110,109,95)):
            with self.subTest(direction=direction):
                position=dict(direction=direction,units=10,avg_entry_price=old_entry,
                              stop_price=stop,payload={"take_price":target})
                gate=economics(direction,105,position["stop_price"],position["payload"]["take_price"])
                held=R.existing_stop_risk_nav(position,gate["modeled_stop_fill"],10000,
                                              "2026-10-07T10:00:00Z",mark_price=105)
                current=10*105/10000
                result=R.cap_fraction_from_economics(gate,.9,self.policy,current_fraction=current,
                                                    existing_stop_risk_nav=held["net_stop_risk_nav"],risk_cap_nav=.02)
                self.assertTrue(result["eligible"])
                self.assertAlmostEqual(result["add_fraction"],.40)
                self.assertAlmostEqual(result["fraction"],.505)
                self.assertLessEqual(result["total_stop_risk_nav_after"],.02)
                self.assertEqual(gate["target_price"],target)
                self.assertEqual(position["stop_price"],stop)

    def test_add_cannot_omit_existing_exposure_risk(self):
        for missing in (None,0):
            result=R.cap_fraction_from_economics(economics(),.9,self.policy,
                current_fraction=.4,existing_stop_risk_nav=missing)
            self.assertEqual(result["reason"],"EXISTING_STOP_RISK_REQUIRED")
            self.assertEqual(result["fraction"],.4)

    def test_exhausted_add_budget_holds_instead_of_reducing_existing_position(self):
        result=R.cap_fraction_from_economics(economics(),.9,self.policy,
            current_fraction=.413,existing_stop_risk_nav=.15)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["status"],"HOLD")
        self.assertEqual(result["action"],"HOLD")
        self.assertEqual(result["fraction"],.413)
        self.assertEqual(result["add_fraction"],0)

    def test_existing_portfolio_maxima_and_gross_cannot_be_increased(self):
        gate=economics("LONG",100,99.5,105)
        for name in CTC.PORTFOLIO_ORDER:
            policy=CTC.runtime_portfolio_policy(name)
            result=R.cap_fraction_from_economics(gate,20,policy,risk_cap_nav=.50)
            self.assertEqual(result["risk_cap_nav"],.15)
            self.assertLessEqual(result["fraction"],policy["max_fraction"])
            self.assertLessEqual(result["fraction"],policy["max_gross"])
        policy=CTC.runtime_portfolio_policy("Champion")
        result=R.cap_fraction_from_economics(gate,1,policy,gross_excluding_position=1.87)
        self.assertAlmostEqual(result["fraction"],.10)


class HeldRiskAndCycleTests(unittest.TestCase):
    def test_protected_profit_does_not_subsidize_forward_nav_risk(self):
        position=dict(direction="LONG",units=10,avg_entry_price=100,stop_price=101)
        result=R.existing_stop_risk_nav(position,100.9596,10000,None,mark_price=110)
        self.assertTrue(result["eligible"])
        expected=10*(110-100.9596) + 10*100.9596*.0004
        self.assertAlmostEqual(result["net_stop_risk_rub"],expected)
        self.assertGreater(result["net_stop_risk_rub"],0)
        self.assertEqual(result["basis"],"FORWARD_MARK_TO_STOP_PLUS_EXIT_COSTS")

    def test_short_held_risk_and_funding_use_original_age(self):
        position=dict(direction="SHORT",units=10,avg_entry_price=110,stop_price=101,
                      opened_at="2026-10-05T10:00:00Z")
        result=R.existing_stop_risk_nav(position,101.0404,10000,"2026-10-07T10:00:00Z",
                                      mark_price=100,expected_hold_seconds=86400)
        expected_funding=10*101.0404*.16*86400/VC.YEAR_SECONDS
        expected=10*(101.0404-100)+10*101.0404*.0004+expected_funding
        self.assertAlmostEqual(result["expected_funding_rub"],expected_funding)
        self.assertAlmostEqual(result["net_stop_risk_rub"],expected)

    def test_unknown_funding_age_or_breached_stored_stop_blocks_add_risk(self):
        position=dict(direction="LONG",units=10,stop_price=98)
        result=R.existing_stop_risk_nav(position,97.9608,10000,"2026-10-07T10:00:00Z",
                                      mark_price=100,expected_hold_seconds=86400)
        self.assertEqual(result["reason"],"EXISTING_FUNDING_AGE_REQUIRED")
        result=R.existing_stop_risk_nav(position,97.9608,10000,None,mark_price=97)
        self.assertEqual(result["reason"],"STOP_DIRECTION_INVALID")

    def test_eth_partial_close_target_can_profit_remaining_leg_but_lose_whole_cycle(self):
        result=R.whole_cycle_projection("SHORT",2684.265864,17.546839872158092,
            2680.701852,-102.70699360842747,37.75644720037285+18.95737668185298,0)
        self.assertTrue(result["complete"])
        self.assertTrue(result["diagnostic_only"])
        self.assertGreater(result["remaining_net_before_paid_cycle_costs_rub"],0)
        self.assertAlmostEqual(result["whole_cycle_net_pnl_rub"],-115.69880808101522)
        self.assertNotIn("eligible",result)

    def test_cycle_preserves_paid_legacy_fees_and_requires_complete_costs(self):
        result=R.whole_cycle_projection("LONG",100,10,102,-5,23,7,projected_funding_rub=2)
        self.assertAlmostEqual(result["whole_cycle_net_pnl_rub"],-5+20-23-7-2-1020*.0004)
        for bad in (None,math.nan,-1):
            result=R.whole_cycle_projection("LONG",100,10,102,-5,bad,7)
            self.assertFalse(result["complete"])


if __name__ == "__main__":
    unittest.main()
