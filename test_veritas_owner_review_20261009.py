import unittest
from unittest.mock import patch

import veritas_canonical_constitution as CTC
import veritas_canonical_runtime as VCR
import veritas_owner_policy as VOP
import veritas_peer_invalidation as VPI
import veritas_self_learning as VSELF
import veritas_strategy_roles as VROLE
import veritas_user_teaching as VUT
import veritas_release as VR


class OwnerReviewPolicyTests(unittest.TestCase):
    def test_native_profit_protection_authority_is_015_and_owner_policy_does_not_duplicate_it(self):
        pp=CTC.TREND_ACCELERATION_POLICY["profit_protection"]
        self.assertEqual(pp["mfe_activation_pct_points"],0.15)
        self.assertEqual(VOP.PROFIT_PROTECTION_AUTHORITY,
                         "CTC.TREND_ACCELERATION_POLICY.profit_protection")
        self.assertTrue(VOP.SELF_LEARNING["owner_verification_required_for_rule_promotion"])

    def test_borrowed_parent_risk_failure_blocks_fast_trade(self):
        row={"asset":"NQ","horizon":"5m","timeframe_entry_context":{"event":{
            "trigger_timeframe":"5m","structural_timeframe":"1h","stop_timeframe":"1h","atr_timeframe":"1h"}}}
        summary=[row,{"asset":"NQ","horizon":"1h","research_decision":"SHORT",
                     "trade_entry_reason":"STRUCTURAL_BREAKOUT_LEVEL_NOT_HELD",
                     "final_gate_blockers":["STRUCTURAL_BREAKOUT_LEVEL_NOT_HELD"]}]
        x=VCR._borrowed_parent_risk_context(row,summary)
        self.assertTrue(x["borrowed"]); self.assertFalse(x["eligible"])
        self.assertEqual(x["reason"],"BORROWED_PARENT_RISK_CONTEXT_INVALID")

    def test_senior_direction_alone_does_not_block_fast_trade(self):
        row={"asset":"NQ","horizon":"5m","timeframe_entry_context":{"event":{
            "trigger_timeframe":"5m","structural_timeframe":"1h","stop_timeframe":"1h","atr_timeframe":"1h"}}}
        summary=[row,{"asset":"NQ","horizon":"1h","research_decision":"LONG",
                      "trade_entry_reason":"NO_DIRECTION","final_gate_blockers":[]}]
        self.assertTrue(VCR._borrowed_parent_risk_context(row,summary)["eligible"])

    def test_verified_structural_event_permission_is_shared_across_three_books(self):
        row={"asset":"BRENT","horizon":"1m","research_decision":"LONG",
             "timeframe_entry_context":{"source_identity":{"key":"PROFINANCE:Brent oil"},
                "event":{"event_id":"E1","trigger_timeframe":"1m","structural_timeframe":"1h",
                         "stop_timeframe":"1h","atr_timeframe":"1h"}}}
        with patch("veritas_structural_breakout.applies",return_value=True),              patch("veritas_structural_breakout.validate_event",return_value={"eligible":True}):
            for mode in ("IMPULSE_ONLY","CORE","CHALLENGER"):
                result=VROLE.gate(row,mode)
                self.assertTrue(result["eligible"],(mode,result))
                self.assertTrue(result["shared_event_entry_permission"],(mode,result))

    def test_hard_thesis_invalidation_is_shared_by_setup_key(self):
        class Result:
            def fetchall(self):
                return [{"trade_id":"Impulse:BRENT:1","portfolio_name":"Impulse",
                         "closed_at":"2026-10-09T03:50:41Z",
                         "exit_reason":"HARD_THESIS_INVALIDATION"}]
        class DB:
            def execute(self,*args,**kwargs): return Result()
        z={"active_trade_id":"Champion:BRENT:2","asset":"BRENT","direction":"LONG",
           "opened_at":"2026-10-08T20:31:00Z",
           "payload":{"canonical_setup_id":"SETUP_BRENT_X"}}
        r=VPI.find(DB(),z)
        self.assertTrue(r["active"]); self.assertEqual(r["source_portfolio"],"Impulse")

    def test_postmortem_uses_immutable_entry_geometry_and_015_threshold(self):
        snap={"atr":10.0,"stop_anchor":95.0,"initial_stop":94.0,"initial_target":112.0,
              "trigger_timeframe":"5m","structural_timeframe":"1h",
              "stop_timeframe":"1h","atr_timeframe":"1h"}
        trade={"trade_id":"T","direction":"LONG","avg_entry_price":100.0,"avg_exit_price":99.0,
               "stop_price":101.0,"take_price":105.0,"mfe_pct":0.20,"mae_pct":-0.5,
               "net_pnl_rub":-100.0,"learning_eligible":True}
        r=VSELF.review(trade,snap)
        self.assertEqual(r["levels_volatility"]["stop"],94.0)
        self.assertEqual(r["levels_volatility"]["target"],112.0)
        self.assertEqual(r["path"]["material_profit_threshold_pct"],0.15)
        self.assertTrue(r["path"]["profit_protection_candidate"])

    def test_tiny_favorable_noise_does_not_trigger_profit_management_review(self):
        snap={"atr":10.0,"initial_stop":95.0,"initial_target":105.0}
        trade={"trade_id":"NQ","direction":"SHORT","avg_entry_price":100.0,"avg_exit_price":100.2,
               "mfe_pct":0.01,"mae_pct":-0.3,"net_pnl_rub":-100.0,"learning_eligible":True}
        r=VSELF.review(trade,snap)
        self.assertFalse(r["path"]["profit_protection_candidate"])
        self.assertEqual(r["primary_review_class"],"ENTRY_OR_DIRECTION_REVIEW")

    def test_partial_profit_then_replan_loss_creates_episode_floor_proposal(self):
        payload={"target_lifecycle_history":[
            {"action":"TARGET_PARTIAL"},{"action":"CONFIRMED_ADD_REPLANS_REMAINING_TARGETS"}]}
        trade={"trade_id":"BRENT","direction":"LONG","avg_entry_price":100.0,"avg_exit_price":101.0,
               "gross_pnl_rub":50.0,"fees_rub":70.0,"funding_rub":0.0,"net_pnl_rub":-20.0,
               "mfe_pct":1.0,"mae_pct":-0.2,"learning_eligible":True,"payload":payload}
        r=VSELF.review(trade,{"atr":1.0,"initial_stop":98.0,"initial_target":102.0})
        self.assertIn("EPISODE_PROFIT_GIVEBACK",r["secondary_findings"])
        self.assertTrue(any(p["kind"]=="EPISODE_PROFIT_FLOOR" for p in r["proposals"]))

    def test_owner_teaching_and_release_include_review_without_replacing_acceleration(self):
        x=VUT.trade_review_policy_snapshot()
        self.assertEqual(x["teaching_id"],VOP.TEACHING_ID)
        self.assertEqual(x["execution_policy"]["material_mfe_threshold_pct"],0.15)
        s=VR.snapshot()
        self.assertEqual(s["active_user_teaching_id"],CTC.TREND_ACCELERATION_POLICY["teaching_id"])
        self.assertIn(VOP.TEACHING_ID,s["active_user_teaching_ids"])
        self.assertEqual(s["owner_review_policy"]["teaching_id"],VOP.TEACHING_ID)


if __name__=="__main__":
    unittest.main()
