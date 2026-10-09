import unittest

import veritas_canonical_constitution as CTC
import veritas_canonical_runtime as VCR
import veritas_owner_policy as VOP
import veritas_self_learning as VSELF
import veritas_profit_maturity as VPM
import veritas_timeframe_management as VTM
import veritas_user_teaching as VUT
import veritas_release as VR


class OwnerReviewPolicyTests(unittest.TestCase):
    def test_constitution_and_owner_policy(self):
        self.assertTrue(CTC.validate_constitution())
        p=VOP.PROFIT_MATURITY
        self.assertEqual(p["required_positive_windows"],3)
        self.assertTrue(p["first_two_positive_windows_observe_only"])
        self.assertFalse(p["structural_trailing_before_maturity"])
        self.assertTrue(VOP.PORTFOLIO_PARITY["canonical_setup_hard_invalidation_shared_across_portfolios"])
        self.assertTrue(VOP.PORTFOLIO_PARITY["verified_structural_event_entry_permission_is_shared"])
        self.assertTrue(VOP.SELF_LEARNING["owner_verification_required_for_rule_promotion"])
        self.assertTrue(VOP.SELF_LEARNING["closed_trade_postmortem_required"])

    def test_borrowed_parent_risk_failure_blocks_fast_trade(self):
        row={
            "asset":"NQ","horizon":"5m",
            "timeframe_entry_context":{"event":{
                "trigger_timeframe":"5m","structural_timeframe":"1h",
                "stop_timeframe":"1h","atr_timeframe":"1h",
            }},
        }
        summary=[row,{
            "asset":"NQ","horizon":"1h","research_decision":"SHORT",
            "trade_entry_reason":"STRUCTURAL_BREAKOUT_LEVEL_NOT_HELD",
            "final_gate_blockers":["STRUCTURAL_BREAKOUT_LEVEL_NOT_HELD"],
        }]
        x=VCR._borrowed_parent_risk_context(row,summary)
        self.assertTrue(x["borrowed"])
        self.assertFalse(x["eligible"])
        self.assertEqual(x["reason"],"BORROWED_PARENT_RISK_CONTEXT_INVALID")

    def test_senior_direction_alone_does_not_block_fast_trade(self):
        row={
            "asset":"NQ","horizon":"5m",
            "timeframe_entry_context":{"event":{
                "trigger_timeframe":"5m","structural_timeframe":"1h",
                "stop_timeframe":"1h","atr_timeframe":"1h",
            }},
        }
        summary=[row,{"asset":"NQ","horizon":"1h","research_decision":"LONG",
                      "trade_entry_reason":"NO_DIRECTION","final_gate_blockers":[]}]
        x=VCR._borrowed_parent_risk_context(row,summary)
        self.assertTrue(x["eligible"])

    def test_profit_maturity_uses_structural_risk_timeframe(self):
        p={"timeframe_entry_context":{"event":{"trigger_timeframe":"1m",
                                               "structural_timeframe":"1h",
                                               "stop_timeframe":"1h"}}}
        self.assertEqual(VPM._management_tf({},p),"1h")

    def test_trailing_waits_for_profit_maturity(self):
        position={"direction":"LONG","payload":{"structural_policy_version":"X",
                                                "profit_maturity_positive_streak":2}}
        x=VTM.apply_trailing(None,"Champion",position,[],{},None)
        self.assertFalse(x["eligible"])
        self.assertEqual(x["reason"],"PROFIT_MATURITY_NOT_CONFIRMED")
        self.assertEqual(x["positive_streak"],2)

    def test_mature_flag_without_economic_floor_still_blocks_trailing(self):
        position={"direction":"LONG","payload":{"structural_policy_version":"X",
                                                "profit_maturity_armed":True}}
        x=VTM.apply_trailing(None,"Champion",position,[],{},None)
        self.assertFalse(x["eligible"])
        self.assertEqual(x["reason"],"PROFIT_MATURITY_ECONOMIC_FLOOR_NOT_SECURED")

    def test_closed_trade_postmortem_is_approval_gated(self):
        snap={"atr":10.0,"stop_anchor":90.0,"regime":"UPTREND",
              "trigger_timeframe":"5m","structural_timeframe":"1h",
              "stop_timeframe":"1h","atr_timeframe":"1h"}
        trade={"trade_id":"T1","asset":"BRENT","direction":"LONG",
               "avg_entry_price":100.0,"avg_exit_price":99.0,
               "stop_price":95.0,"take_price":110.0,
               "mfe_pct":0.7,"mae_pct":-1.2,"giveback_pct":0.7,
               "net_pnl_rub":-100.0,"learning_eligible":True,
               "trade_diagnostics":{"primary_attribution":"EXIT_MANAGEMENT"}}
        r=VSELF.review(trade,snap)
        self.assertEqual(r["status"],"REVIEWED")
        self.assertTrue(r["proposals"])
        self.assertTrue(all(p["status"]=="OWNER_REVIEW_REQUIRED" for p in r["proposals"]))
        self.assertTrue(all(not p["automatic_promotion_allowed"] for p in r["proposals"]))

    def test_owner_teaching_is_durable_snapshot_candidate(self):
        x=VUT.trade_review_policy_snapshot()
        self.assertEqual(x["teaching_id"],"USER_TRADE_REVIEW_2026_10_09")
        self.assertTrue(x["execution_policy"]["canonical_setup_hard_invalidation_shared"])
        self.assertTrue(x["learning_policy"]["owner_verification_required_for_rule_promotion"])

    def test_release_identifies_new_policy(self):
        self.assertIn("v91.8.29",VR.PRODUCT_VERSION)
        s=VR.snapshot()
        self.assertEqual(s["owner_review_policy"]["profit_maturity"]["required_positive_windows"],3)
        self.assertTrue(s["owner_review_policy"]["self_learning"]["closed_trade_postmortem_required"])


if __name__=="__main__":
    unittest.main()
