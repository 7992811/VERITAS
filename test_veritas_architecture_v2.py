import unittest
from unittest.mock import patch
from datetime import datetime, timezone

import veritas_canonical_constitution as CTC
import veritas_canonical_runtime as VCR
import veritas_costs as VC
import veritas_execution as VX
import veritas_portfolio as VP
import veritas_portfolio_runtime as VPR
import veritas_release as VR


class CanonicalArchitectureV2Tests(unittest.TestCase):
    def test_portfolio_runtime_policy_has_one_source(self):
        self.assertEqual(tuple(VP.POLICIES), CTC.PORTFOLIO_ORDER)
        for name in CTC.PORTFOLIO_ORDER:
            self.assertEqual(VP.POLICIES[name], CTC.runtime_portfolio_policy(name))
        self.assertEqual(VP.POLICIES["Impulse"]["max_gross"], 0.50)
        self.assertEqual(CTC.drawdown_profile("Impulse")["normal_max_gross"], 0.50)

    def test_veto_registry_is_fail_closed(self):
        self.assertEqual(CTC.veto_severity("R69_WAIT_LOCAL_BREAKOUT"), "SOFT")
        self.assertEqual(CTC.veto_severity("R69_BREAKOUT_ACTIVITY_REQUIRED"), "SOFT")
        self.assertEqual(CTC.veto_severity("EXPECTED_MOVE_BELOW_COST_BUFFER"), "HARD")
        self.assertEqual(CTC.veto_severity("TARGET_NOT_PROFITABLE_AFTER_COSTS"), "HARD")
        self.assertEqual(CTC.veto_severity("UNKNOWN_NEW_BLOCKER"), "HARD")

    def test_currency_sizing_is_explicit(self):
        p=CTC.PORTFOLIO_POLICIES["Currency"]
        self.assertEqual(p["initial_normal"], .50)
        self.assertEqual(p["initial_super"], 1.00)
        self.assertEqual(p["probe_normal"], .05)
        self.assertEqual(p["probe_super"], .10)
        self.assertEqual(p["max_gross"], 10.0)
        self.assertEqual(CTC.PORTFOLIO_POLICIES["Aggressive"]["probe_normal"], .50)
        self.assertEqual(CTC.PORTFOLIO_POLICIES["Aggressive"]["probe_super"], 1.00)

    def test_release_identity_is_five_portfolio_ctc_v2(self):
        s=VR.snapshot()
        self.assertEqual(s["ctc_version"], CTC.VERSION)
        self.assertEqual(s["runtime_authority"], CTC.BASIS_RUNTIME)
        self.assertEqual(s["portfolio_count"], 5)
        self.assertEqual(tuple(s["portfolios"]), CTC.PORTFOLIO_ORDER)
        self.assertIn("five-portfolio", s["product_version"])

    def test_final_runtime_binds_canonical_routing_and_admission(self):
        snap=VPR.runtime_authority_snapshot()
        self.assertEqual(snap["version"], CTC.BASIS_RUNTIME)
        self.assertFalse(snap["legacy_admission_authoritative"])
        self.assertFalse(snap["legacy_candidate_routing_authoritative"])
        self.assertIs(VP._signal_first_admission, VPR.FINAL_SIGNAL_FIRST_ADMISSION)
        self.assertIs(VP._open_or_add, VPR.FINAL_OPEN_OR_ADD)
        self.assertIs(VP._close_or_reduce, VPR.FINAL_CLOSE_OR_REDUCE)
        self.assertIs(VP._candidate_book_v84, VCR.candidate_book)

    def test_signal_first_candidate_book_keeps_published_direction(self):
        rows=[
            {"asset":"CNYRUBF","horizon":"5m","research_decision":"LONG","confidence":.62,
             "signal_tier":"LONG","horizon_structure":{"score":.4}},
            {"asset":"CNYRUBF","horizon":"1h","research_decision":"LONG","confidence":.78,
             "signal_tier":"LONG","horizon_structure":{"score":.7}},
        ]
        book=VCR.candidate_book(rows)
        self.assertIn("CNYRUBF",book)
        self.assertEqual(book["CNYRUBF"]["research_decision"],"LONG")
        self.assertEqual(book["CNYRUBF"]["_alignment_count"],2)

    def test_soft_timing_block_reduces_to_probe_not_cash(self):
        row={"asset":"CNYRUBF","horizon":"5m","research_decision":"LONG","decision":"LONG",
             "signal_tier":"LONG","price":12.8,"source_gate_pass":True,"market_open":True,
             "paper_eligible":True,"market_observed_at":datetime.now(timezone.utc).isoformat(),
             "trade_plan":{"stop_price":12.7,"expected_move_pct":.01,"expected_to_stop_ratio":2.0}}
        policy=CTC.runtime_portfolio_policy("Currency")
        with patch.object(VCR.VX,"paper_source_gate",return_value={"eligible":True,"blockers":[]}),              patch.object(VCR.VPG,"quote_gate",return_value={"eligible":True}),              patch.object(VCR.VTE,"prepare_row",side_effect=lambda x,*a,**k:x),              patch.object(VCR.VTE,"event_gate",return_value={"eligible":False,"reason":"R69_WAIT_LOCAL_BREAKOUT"}),              patch.object(VCR,"anti_chase_gate",return_value={"eligible":True,"reason":"OK"}),              patch.object(VCR.VX,"entry_gate",return_value={
                 "eligible":False,"blockers":["RR_BELOW_FINAL_FLOOR","R69_WAIT_LOCAL_BREAKOUT"],
                 "net_risk_pct":.01,"net_reward_pct":.002}):
            out=VCR.evaluate(row,policy,0.0)
        self.assertTrue(out["open"],out)
        self.assertEqual(out["fraction"],.05)
        self.assertIn("R69_WAIT_LOCAL_BREAKOUT",out["soft_blockers"])

    def test_weak_senior_signal_waits_for_local_confirmation(self):
        row={"asset":"NQ","horizon":"4h","research_decision":"LONG",
             "entry_quality":"NEW_SETUP_PROVISIONAL",
             "horizon_structure":{"state":"WEAK","score":.46,"direction":"LONG"},
             "independent_evidence_families":4,
             "_local_execution_context":{"same_direction_count":0,"opposite_direction_count":0}}
        gate=VCR.local_confirmation_gate(row,{"reason":"R69_WAIT_LOCAL_BREAKOUT"})
        self.assertFalse(gate["eligible"])
        self.assertEqual(gate["reason"],"LOCAL_EXECUTION_CONFIRMATION_REQUIRED")

    def test_strong_senior_trend_can_trade_without_local_signal(self):
        row={"asset":"NQ","horizon":"4h","research_decision":"LONG",
             "entry_quality":"CONFIRMED_TREND",
             "horizon_structure":{"state":"CONFIRMED_TREND","score":.80,"direction":"LONG"},
             "independent_evidence_families":5,
             "_local_execution_context":{"same_direction_count":0,"opposite_direction_count":0}}
        gate=VCR.local_confirmation_gate(row,{"reason":"R69_WAIT_LOCAL_BREAKOUT"})
        self.assertTrue(gate["eligible"],gate)

    def test_cost_negative_signal_remains_hard_block(self):
        row={"asset":"CNYRUBF","horizon":"1h","research_decision":"LONG","decision":"LONG",
             "signal_tier":"LONG","price":12.8,"source_gate_pass":True,"market_open":True,
             "paper_eligible":True,"market_observed_at":datetime.now(timezone.utc).isoformat(),
             "trade_plan":{"stop_price":12.7}}
        policy=CTC.runtime_portfolio_policy("Currency")
        with patch.object(VCR.VX,"paper_source_gate",return_value={"eligible":True,"blockers":[]}),              patch.object(VCR.VPG,"quote_gate",return_value={"eligible":True}),              patch.object(VCR.VTE,"prepare_row",side_effect=lambda x,*a,**k:x),              patch.object(VCR.VTE,"event_gate",return_value={"eligible":True,"reason":"OK"}),              patch.object(VCR,"anti_chase_gate",return_value={"eligible":True,"reason":"OK"}),              patch.object(VCR.VX,"entry_gate",return_value={
                 "eligible":False,"blockers":["EXPECTED_MOVE_BELOW_COST_BUFFER"],
                 "net_risk_pct":.01,"net_reward_pct":-.001}):
            out=VCR.evaluate(row,policy,0.0)
        self.assertFalse(out["open"])
        self.assertEqual(out["reason"],"EXPECTED_MOVE_BELOW_COST_BUFFER")


if __name__ == "__main__":
    unittest.main()
