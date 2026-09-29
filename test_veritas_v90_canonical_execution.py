import unittest
from datetime import datetime, timezone

import veritas_intelligence as VI
import veritas_portfolio as VP


class CanonicalExecutionKernelTests(unittest.TestCase):
    def row(self):
        plan = VI.final_execution_safety("BTC", "SHORT", {
            "eligible": True,
            "entry_price": 100.0,
            "stop_price": 101.0,
            "target_price": 98.0,
            "expected_move_pct": 0.02,
            "expected_to_stop_ratio": 2.0,
            "stop_distance_pct": 0.01,
            "initial_position_fraction": 0.10,
        })
        return {
            "asset": "BTC",
            "price": 100.0,
            "horizon": "1h",
            "research_decision": "SHORT",
            "source_gate_pass": True,
            "market_open": True,
            "paper_eligible": True,
            "production_eligible": False,
            "market_observed_at": datetime.now(timezone.utc).isoformat(),
            "best_bid": 99.99,
            "best_ask": 100.01,
            "secondary_price": 100.0,
            "source_divergence": 0.0001,
            "confidence": 0.05,
            "horizon_structure": {
                "direction": "SHORT", "score": 0.20, "state": "NEUTRAL"
            },
            "institutional_signal": {
                "evidence_independence": {"independent_count": 1}
            },
            "trade_plan": plan,
            "_pwin": 0.90,
            "_pwin_source": "MODEL_PRIOR_UNCALIBRATED",
            "_rank": 0.90,
        }

    def test_soft_model_quality_no_longer_vetoes_valid_signal(self):
        row = self.row()
        for name, policy in VP.POLICIES.items():
            with self.subTest(portfolio=name):
                out = VP._signal_first_admission(dict(row), policy, 0.0)
                self.assertTrue(out["open"], out)
                self.assertGreater(out["fraction"], 0.0)
                self.assertEqual(out["reason"], "CANONICAL_SIGNAL_ENTRY")
                self.assertNotEqual(out["reason"], "MODEL_SCORE_BELOW_PAPER_ADMISSION")
                self.assertNotEqual(out["reason"], "Q2_MODEL_SCORE_BELOW_FLOOR")

    def test_uncalibrated_score_is_not_reported_as_probability(self):
        out = VP._signal_first_admission(self.row(), VP.POLICIES["Aggressive"], 0.0)
        self.assertIsNone(out["probability"])
        self.assertIsNotNone(out["model_quality_score"])
        self.assertIn("UNCALIBRATED", out["probability_source"])

    def test_negative_trade_economics_remains_hard_block(self):
        row = self.row()
        row["trade_plan"] = VI.final_execution_safety("BTC", "SHORT", {
            "eligible": True,
            "entry_price": 100.0,
            "stop_price": 101.0,
            "target_price": 99.8,
            "expected_move_pct": 0.002,
            "expected_to_stop_ratio": 0.20,
            "stop_distance_pct": 0.01,
            "initial_position_fraction": 0.10,
        })
        out = VP._signal_first_admission(row, VP.POLICIES["Aggressive"], 0.0)
        self.assertFalse(out["open"])
        self.assertEqual(out["reason"], "R41_FINAL_ECONOMICS_GATE")
        self.assertTrue(out["economics_blockers"])

    def test_source_failure_remains_hard_block(self):
        row = self.row()
        row["source_gate_pass"] = False
        out = VP._signal_first_admission(row, VP.POLICIES["Aggressive"], 0.0)
        self.assertFalse(out["open"])
        self.assertEqual(out["reason"], "R42_PAPER_SOURCE_GATE")


if __name__ == "__main__":
    unittest.main()
