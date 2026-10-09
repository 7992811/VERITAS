import unittest

import veritas_episode_profit_floor as VEF


class EpisodeProfitFloorTests(unittest.TestCase):
    def base_position(self):
        return {"direction":"LONG","units":10.0,"avg_entry_price":100.0,
                "stop_price":99.0,"payload":{"r17_tp1_done":True}}

    def test_inactive_before_profit_is_noop(self):
        z=self.base_position(); z["payload"]={}
        r=VEF.assess_add(z,{"gross_pnl_rub":0.0,"fees_rub":1.0,"funding_rub":0.0},
                         105.0,0.50,10000.0)
        self.assertFalse(r["active"])
        self.assertTrue(r["eligible"])

    def test_realized_profit_caps_add_so_episode_stays_nonnegative_at_stop(self):
        z=self.base_position()
        trade={"gross_pnl_rub":80.0,"fees_rub":8.0,"funding_rub":0.0}
        r=VEF.assess_add(z,trade,105.0,0.50,10000.0)
        self.assertTrue(r["active"])
        self.assertFalse(r["eligible"])
        self.assertGreater(r["cap_fraction"],r["current_fraction"])
        self.assertLess(r["cap_fraction"],r["requested_fraction"])
        capped=VEF.assess_add(z,trade,105.0,r["cap_fraction"],10000.0)
        self.assertGreaterEqual(capped["projected_requested_cycle_net_at_stop_rub"],-1e-6)

    def test_no_add_if_existing_active_stop_already_loses_earned_episode(self):
        z=self.base_position()
        trade={"gross_pnl_rub":5.0,"fees_rub":20.0,"funding_rub":0.0}
        r=VEF.assess_add(z,trade,105.0,0.50,10000.0)
        self.assertTrue(r["active"])
        self.assertFalse(r["eligible"])
        self.assertAlmostEqual(r["cap_fraction"],r["current_fraction"])

    def test_missing_accounting_fails_closed_only_after_profit_protection_active(self):
        r=VEF.assess_add(self.base_position(),{},105.0,0.50,10000.0)
        self.assertTrue(r["active"])
        self.assertFalse(r["eligible"])
        self.assertEqual(r["reason"],"EPISODE_PROFIT_FLOOR_ACCOUNTING_REQUIRED")

    def test_native_acceleration_profit_lock_activates_floor(self):
        z=self.base_position(); z["payload"]={"r_accel_mfe_profit_lock_active":True}
        trade={"gross_pnl_rub":80.0,"fees_rub":8.0,"funding_rub":0.0}
        r=VEF.assess_add(z,trade,105.0,0.50,10000.0)
        self.assertTrue(r["active"])

    def test_short_is_symmetric(self):
        z={"direction":"SHORT","units":10.0,"avg_entry_price":100.0,
           "stop_price":101.0,"payload":{"profit_maturity_armed":True}}
        trade={"gross_pnl_rub":100.0,"fees_rub":8.0,"funding_rub":0.0}
        r=VEF.assess_add(z,trade,95.0,0.50,10000.0)
        self.assertTrue(r["active"])
        self.assertLess(r["cap_fraction"],r["requested_fraction"])
        self.assertGreater(r["cap_fraction"],r["current_fraction"])


if __name__=="__main__":
    unittest.main()
