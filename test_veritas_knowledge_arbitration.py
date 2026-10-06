import unittest

import veritas_knowledge_arbitration as VKA
import veritas_signal_core as VSC


class KnowledgeArbitrationTests(unittest.TestCase):
    def test_pack6_is_loaded(self):
        s = VKA.catalog_summary()
        self.assertGreaterEqual(s["source_count"], 15)
        self.assertGreaterEqual(s["rule_count"], 20)
        self.assertGreaterEqual(s["anti_rule_count"], 8)
        self.assertIn("veritas_knowledge_seed_pack6.json", s["files"])

    def test_trend_rule_matches_without_forcing_direction(self):
        out = VKA.arbitrate({
            "asset": "NQ", "horizon": "1h", "research_decision": "LONG",
            "confidence": 0.72, "effective_evidence": 4,
            "horizon_structure": {"direction": "LONG", "score": 0.78},
            "trade_plan": {"expected_move_pct": 0.012, "expected_to_stop_ratio": 1.8},
            "source_gate_pass": True, "market_open": True,
        })
        ids = {x["rule_id"] for x in out["matched_rules"]}
        self.assertIn("KP6_TREND_NATIVE_CONTINUATION", ids)
        self.assertFalse(out["automatic_veto"])
        self.assertGreaterEqual(out["size_multiplier"], 0.80)
        self.assertLessEqual(out["size_multiplier"], 1.08)

    def test_anti_rule_blocks_false_late_impulse_logic_only_as_policy(self):
        out = VKA.arbitrate({
            "asset": "BRENT", "horizon": "1h", "research_decision": "SHORT",
            "horizon_structure": {"direction": "SHORT", "score": 0.70},
        })
        ids = {x["rule_id"] for x in out["anti_rules"]}
        self.assertIn("KP6_AR_IMPULSE_PASSED_NOT_VETO", ids)
        self.assertFalse(out["automatic_veto"])

    def test_hard_data_gate_cannot_be_bypassed_by_knowledge(self):
        gate = VSC.pretrade_gate({
            "asset": "BTC", "horizon": "1h", "research_decision": "LONG",
            "confidence": 0.85, "effective_evidence": 5,
            "source_gate": False, "time_gate": True, "market_open": True,
            "horizon_structure": {"direction": "LONG", "score": 0.90},
            "agents": [{"agent": "A", "direction": "LONG", "confidence": 0.9}],
        })
        self.assertFalse(gate["allow"])
        self.assertEqual(gate["gate_class"], "DATA_VETO")
        self.assertIn("knowledge_arbitration", gate)

    def test_governance_rules_do_not_add_directional_score(self):
        out = VKA.arbitrate({
            "asset": "MOEX", "horizon": "3d", "research_decision": "NO_TRADE",
            "horizon_structure": {"direction": "NO_TRADE", "score": 0.2},
        })
        self.assertEqual(out["long_score"], 0.0)
        self.assertEqual(out["short_score"], 0.0)


if __name__ == "__main__":
    unittest.main()
