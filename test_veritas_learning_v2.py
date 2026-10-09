import unittest
import veritas_learning_v2 as L

class LearningV2Tests(unittest.TestCase):
    def row(self, decision="NO_TRADE", fr=.01, blockers=None, **kw):
        x={"asset":"NQ","horizon":"5m","regime":"TREND","policy_hash":"p",
           "decision":decision,"forward_return":fr,"candidate_direction":"LONG",
           "final_gate_blockers":blockers or ["IMPULSE_ALREADY_PASSED"]}
        x.update(kw); return x

    def test_blocked_long_signal_is_a_missed_directional_episode(self):
        x=self.row(decision="LONG",candidate_direction="LONG",admission_eligible=False,fr=.01)
        c=L.classify_decision_episode(x)
        self.assertEqual(c["kind"],"MISSED_DIRECTIONAL_MOVE")
        self.assertFalse(c["counterfactual_fill_proven"])

    def test_admitted_long_signal_is_not_a_false_block(self):
        x=self.row(decision="LONG",candidate_direction="LONG",admission_eligible=True,
                   final_gate_blockers=[],blockers=[],fr=.01)
        c=L.classify_decision_episode(x)
        self.assertEqual(c["kind"],"DIRECTIONAL_DECISION")

    def test_declared_reason_field_contributes_known_blocker_only(self):
        row=self.row(blockers=[],trade_entry_reason="ENTRY_BLOCKED: IMPULSE_ALREADY_PASSED; wait for retest")
        row["final_gate_blockers"]=[]
        row["admission_eligible"]=False
        c=L.classify_decision_episode(row)
        self.assertEqual(c["kind"],"MISSED_DIRECTIONAL_MOVE")
        self.assertIn("IMPULSE_ALREADY_PASSED",c["blockers"])

    def test_reason_token_alone_cannot_manufacture_a_block(self):
        row=self.row(blockers=[],trade_entry_reason="IMPULSE_ALREADY_PASSED")
        row["final_gate_blockers"]=[]
        row["admission_eligible"]=True
        row["final_gate_status"]="PASS"
        self.assertIn("IMPULSE_ALREADY_PASSED",L.row_blockers(row))
        self.assertFalse(L.has_block_evidence(row))
        self.assertNotEqual(L.classify_decision_episode(row)["kind"],"MISSED_DIRECTIONAL_MOVE")

    def test_arbitrary_reason_text_never_becomes_a_blocker(self):
        row=self.row(blockers=[],trade_entry_reason="analyst thinks this looks late")
        row["final_gate_blockers"]=[]
        row["admission_eligible"]=False
        self.assertEqual(L.row_blockers(row),())
        d=L.research_diagnostics([row],[])
        self.assertEqual(d["unparsed_blocked_directional"],1)

    def test_hard_veto_reason_is_recognized_but_never_relaxed(self):
        veto=next(iter(L.FORBIDDEN_ENTRY_BLOCKERS))
        rows=[]
        for _ in range(max(L.MIN_CONTEXT_N,L.MIN_FALSE_BLOCK_N)):
            x=self.row(blockers=[],trade_entry_reason=veto)
            x["final_gate_blockers"]=[]
            rows.append(x)
        self.assertIn(veto,L.row_blockers(rows[0]))
        h=L.generate_hypotheses(rows,[])
        self.assertFalse(any(x["kind"]=="ENTRY_BLOCKER_RELAXATION" and x["proposal"]["blocker"]==veto for x in h))

    def test_false_block_is_observed_movement_not_counterfactual_profit(self):
        s=L.false_block_summary([self.row() for _ in range(10)])
        self.assertEqual(s["missed_directional_episodes"],10)
        self.assertEqual(s["blockers"][0]["blocker"],"IMPULSE_ALREADY_PASSED")
        self.assertFalse(s["blockers"][0]["causal_false_block_proven"])

    def test_no_trade_without_frozen_direction_is_not_false_block(self):
        rows=[self.row(candidate_direction="") for _ in range(10)]
        s=L.false_block_summary(rows)
        self.assertEqual(s["missed_directional_episodes"],0)

    def test_move_against_candidate_direction_is_not_false_block(self):
        rows=[self.row(candidate_direction="LONG",fr=-.01) for _ in range(10)]
        s=L.false_block_summary(rows)
        self.assertEqual(s["missed_directional_episodes"],0)

    def test_discovery_threshold_is_lower_than_prospective_validation(self):
        rows=[self.row() for _ in range(L.MIN_CONTEXT_N)]
        h=L.generate_hypotheses(rows,[])
        self.assertTrue(any(x["kind"]=="ENTRY_BLOCKER_RELAXATION" for x in h))
        self.assertLess(L.MIN_CONTEXT_N,64)

    def test_entry_hypothesis_requires_recurrence_and_is_shadow_only(self):
        rows=[self.row() for _ in range(24)]
        h=L.generate_hypotheses(rows,[])
        entry=[x for x in h if x["kind"]=="ENTRY_BLOCKER_RELAXATION"]
        self.assertTrue(entry)
        self.assertTrue(all(x["mode"]=="SHADOW_ONLY" and not x["production_mutation"] for x in entry))

    def test_stop_exit_and_router_hypotheses_are_bounded(self):
        decisions=[]
        for i in range(30):
            decisions.append(self.row(decision="LONG",fr=.01 if i<21 else -.01,
                setup_family="BREAKOUT",final_gate_blockers=[]))
            decisions.append(self.row(decision="LONG",fr=.01 if i<15 else -.01,
                setup_family="TREND",final_gate_blockers=[]))
        trades=[{"asset":"NQ","horizon":"5m","regime":"TREND","policy_hash":"p",
                 "mae":-.004,"mfe":.014,"capture_ratio":.2} for _ in range(20)]
        h=L.generate_hypotheses(decisions,trades)
        kinds={x["kind"] for x in h}
        self.assertTrue({"STOP_GEOMETRY","EXIT_CAPTURE","STRATEGY_ROUTER"}<=kinds)
        self.assertLessEqual(len(h),L.MAX_HYPOTHESES)

    def test_hypotheses_do_not_mix_price_sources(self):
        rows=[]
        for source in ("A","B"):
            rows.extend([self.row(source_key=source) for _ in range(24)])
        h=L.generate_hypotheses(rows,[])
        entries=[x for x in h if x["kind"]=="ENTRY_BLOCKER_RELAXATION"]
        self.assertEqual({x["scope"]["source_key"] for x in entries},{"A","B"})
        self.assertTrue(all(x["evidence"]["n"]==24 for x in entries))

    def test_hypotheses_do_not_mix_contracts(self):
        rows=[]
        for contract in ("C1","C2"):
            rows.extend([self.row(source_key="S",contract_id=contract) for _ in range(L.MIN_CONTEXT_N)])
        h=L.generate_hypotheses(rows,[])
        entries=[x for x in h if x["kind"]=="ENTRY_BLOCKER_RELAXATION"]
        self.assertEqual({x["scope"]["contract_id"] for x in entries},{"C1","C2"})
        self.assertTrue(all(x["evidence"]["n"]==L.MIN_CONTEXT_N for x in entries))

    def test_hard_veto_can_be_diagnosed_but_never_relaxed(self):
        rows=[self.row(blockers=["EXECUTION_QUOTE_STALE"]) for _ in range(L.MIN_CONTEXT_N)]
        summary=L.false_block_summary(rows)
        self.assertEqual(summary["blockers"][0]["blocker"],"EXECUTION_QUOTE_STALE")
        hypotheses=L.generate_hypotheses(rows,[])
        self.assertFalse(any(x["kind"]=="ENTRY_BLOCKER_RELAXATION" for x in hypotheses))
        self.assertIn("EXECUTION_QUOTE_STALE",L.FORBIDDEN_ENTRY_BLOCKERS)

    def test_timing_blocker_can_generate_shadow_entry_candidate(self):
        rows=[self.row(blockers=["IMPULSE_ALREADY_PASSED"]) for _ in range(L.MIN_CONTEXT_N)]
        hypotheses=L.generate_hypotheses(rows,[])
        entry=[x for x in hypotheses if x["kind"]=="ENTRY_BLOCKER_RELAXATION"]
        self.assertTrue(entry)
        self.assertEqual(entry[0]["proposal"]["blocker"],"IMPULSE_ALREADY_PASSED")
        self.assertEqual(entry[0]["mode"],"SHADOW_ONLY")
        self.assertFalse(entry[0]["production_mutation"])

    def test_diagnostics_explain_zero_candidate_reason(self):
        rows=[]
        for _ in range(L.MIN_CONTEXT_N):
            x=self.row(blockers=[],trade_entry_reason="UNDECLARED_TEXT")
            x["final_gate_blockers"]=[]
            x["admission_eligible"]=False
            rows.append(x)
        d=L.research_diagnostics(rows,[])
        self.assertEqual(d["blocked_directional"],L.MIN_CONTEXT_N)
        self.assertEqual(d["unparsed_blocked_directional"],L.MIN_CONTEXT_N)
        self.assertEqual(d["zero_entry_candidate_reason"],"NO_LEARNABLE_BLOCKER_MATCH")

    def test_closed_trade_outcome_tier_is_visible_without_granting_path_authority(self):
        outcome=[
            {"asset":"NQ","horizon":"5m","regime":"TREND","policy_hash":"p",
             "net_pnl_rub":100.0,"opening_fraction":.25,"path_evidence_eligible":False,
             "mfe":9.0,"mae":-9.0,"capture_ratio":1.0},
            {"asset":"NQ","horizon":"5m","regime":"TREND","policy_hash":"p",
             "net_pnl_rub":-40.0,"opening_fraction":.50,"path_evidence_eligible":True,
             "mfe":.6,"mae":-.3,"capture_ratio":.25},
        ]
        paths=[outcome[1]]
        s=L.closed_trade_summary(outcome,paths)
        self.assertEqual(s["outcome_evidence_trades"],2)
        self.assertEqual(s["path_evidence_trades"],1)
        self.assertEqual(s["outcome_only_trades"],1)
        self.assertAlmostEqual(s["win_rate"],.5)
        self.assertAlmostEqual(s["net_pnl_rub"],60.)
        self.assertAlmostEqual(s["avg_mfe_pct"],.6)
        self.assertAlmostEqual(s["avg_capture_ratio"],.25)
        snap=L.research_snapshot([],paths,outcome)
        self.assertEqual(snap["diagnostics"]["outcome_trade_rows"],2)
        self.assertEqual(snap["diagnostics"]["trade_rows"],1)
        self.assertEqual(snap["diagnostics"]["outcome_only_trade_rows"],1)
        self.assertFalse(any(h["kind"] in ("STOP_GEOMETRY","EXIT_CAPTURE") for h in snap["hypotheses"]))
        self.assertFalse(snap["closed_trade_summary"]["production_mutation"])

    def test_short_direction_is_signed_correctly(self):
        c=L.classify_decision_episode(self.row(decision="SHORT",fr=-.01,final_gate_blockers=[]))
        self.assertGreater(c["signed_forward_return"],0)

if __name__=="__main__":
    unittest.main()
