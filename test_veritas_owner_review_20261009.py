import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import veritas_canonical_constitution as CTC
import veritas_canonical_runtime as VCR
import veritas_owner_policy as VOP
import veritas_self_learning as VSELF
import veritas_profit_maturity as VPM
import veritas_peer_invalidation as VPI
import veritas_strategy_roles as VROLE
import veritas_timeframe_management as VTM
import veritas_user_teaching as VUT
import veritas_release as VR


class OwnerReviewPolicyTests(unittest.TestCase):
    def test_constitution_and_owner_policy(self):
        self.assertTrue(CTC.validate_constitution())
        p=VOP.PROFIT_MATURITY
        self.assertEqual(p["required_profit_excursions"],3)
        self.assertEqual(p["qualifying_excursion_positive_windows"],3)
        self.assertTrue(p["first_two_profit_excursions_observe_only"])
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

    def test_verified_1m_trigger_with_1h_structure_is_shared_across_three_books(self):
        row={"asset":"BRENT","horizon":"1m","research_decision":"LONG",
             "timeframe_entry_context":{"source_identity":{"key":"PROFINANCE:Brent oil"},
                "event":{"event_id":"E1","trigger_timeframe":"1m","structural_timeframe":"1h",
                         "stop_timeframe":"1h","atr_timeframe":"1h"}}}
        with patch("veritas_structural_breakout.applies",return_value=True), \
             patch("veritas_structural_breakout.validate_event",return_value={"eligible":True}):
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
            def execute(self,*args,**kwargs):
                return Result()
        position={"active_trade_id":"Champion:BRENT:2","asset":"BRENT","direction":"LONG",
                  "opened_at":"2026-10-08T20:31:00Z",
                  "payload":{"canonical_setup_id":"SETUP_BRENT_X"}}
        result=VPI.find(DB(),position)
        self.assertTrue(result["active"],result)
        self.assertEqual(result["reason"],"HARD_THESIS_INVALIDATION_SHARED_CANONICAL_SETUP")
        self.assertEqual(result["source_portfolio"],"Impulse")

    def test_profit_maturity_uses_structural_risk_timeframe(self):
        p={"timeframe_entry_context":{"event":{"trigger_timeframe":"1m",
                                               "structural_timeframe":"1h",
                                               "stop_timeframe":"1h"}}}
        self.assertEqual(VPM._management_tf({},p),"1h")

    def test_trailing_waits_for_profit_maturity(self):
        position={"direction":"LONG","payload":{"structural_policy_version":"X",
                                                "profit_maturity_excursion_count":2}}
        x=VTM.apply_trailing(None,"Champion",position,[],{},None)
        self.assertFalse(x["eligible"])
        self.assertEqual(x["reason"],"PROFIT_MATURITY_NOT_CONFIRMED")

    def test_first_two_profit_excursions_are_observation_only(self):
        p=VOP.PROFIT_MATURITY
        self.assertEqual(p["required_profit_excursions"],3)
        self.assertTrue(p["first_two_profit_excursions_observe_only"])

    def test_loss_inside_profit_window_resets_streak_and_same_window_rebound_cannot_restore_it(self):
        class DB:
            def execute(self,*args,**kwargs):
                return self
        opened="2026-10-09T00:00:00+00:00"
        observed="2026-10-09T02:20:00+00:00"
        bucket=int(datetime.fromisoformat(observed).timestamp()//3600)
        position={"direction":"LONG","avg_entry_price":100.0,"opened_at":opened,
                  "asset":"GOLD","active_trade_id":"T1",
                  "payload":{"timeframe_entry_context":{"event":{"structural_timeframe":"1h"}},
                             "profit_maturity_last_window":bucket,
                             "profit_maturity_excursion_count":2,
                             "profit_maturity_excursion_positive_windows":2,
                             "profit_maturity_in_profit":True,
                             "profit_maturity_current_window_positive":True,
                             "profit_maturity_excursion_started_at":datetime.fromisoformat("2026-10-09T01:05:00+00:00").timestamp()}}
        def assess(_c,_z,**kw):
            px=float(kw.get("price") or 0)
            return {"net_profit_protection":{"net_at_stop_rub":1.0 if px>100 else -1.0,
                                             "break_even_stop_price":100.2}}
        now=datetime.fromisoformat("2026-10-09T02:25:00+00:00")
        with patch("veritas_profit_maturity.VPP.assess",side_effect=assess):
            down=VPM.observe(DB(),"Champion",position,{"price":99.9,"observed_at":observed},now)
            self.assertEqual(down["positive_windows_in_excursion"],0)
            self.assertFalse(down["patch"]["profit_maturity_in_profit"])
            rebound=VPM.observe(DB(),"Champion",down["position"],
                                {"price":101.0,"observed_at":"2026-10-09T02:40:00+00:00"},
                                datetime.fromisoformat("2026-10-09T02:45:00+00:00"))
            self.assertEqual(rebound["excursion_count"],3)
            self.assertEqual(rebound["positive_windows_in_excursion"],1)
            self.assertFalse(rebound["mature"])

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
        self.assertEqual(s["owner_review_policy"]["profit_maturity"]["required_profit_excursions"],3)
        self.assertTrue(s["owner_review_policy"]["self_learning"]["closed_trade_postmortem_required"])


if __name__=="__main__":
    unittest.main()
