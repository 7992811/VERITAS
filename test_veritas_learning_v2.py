import unittest
import veritas_learning_v2 as L

class LearningV2Tests(unittest.TestCase):
    def row(self, decision="NO_TRADE", fr=.01, blockers=None, **kw):
        x={"asset":"NQ","horizon":"5m","regime":"TREND","policy_hash":"p",
           "decision":decision,"forward_return":fr,"candidate_direction":"LONG",
           "final_gate_blockers":blockers or ["IMPULSE_ALREADY_PASSED"]}
        x.update(kw); return x

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

    def test_short_direction_is_signed_correctly(self):
        c=L.classify_decision_episode(self.row(decision="SHORT",fr=-.01,final_gate_blockers=[]))
        self.assertGreater(c["signed_forward_return"],0)

if __name__=="__main__":
    unittest.main()
