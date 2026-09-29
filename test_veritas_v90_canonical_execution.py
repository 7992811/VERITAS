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

    def test_research_books_can_probe_but_candidate_books_require_profitability_evidence(self):
        row = self.row()
        for name in ("Impulse","Aggressive"):
            out = VP._signal_first_admission(dict(row), VP.POLICIES[name], 0.0)
            self.assertTrue(out["open"], out)
            self.assertGreater(out["fraction"], 0.0)
            self.assertEqual(out["reason"], "CANONICAL_SIGNAL_ENTRY")
        for name in ("Champion","Challenger"):
            out = VP._signal_first_admission(dict(row), VP.POLICIES[name], 0.0)
            self.assertFalse(out["open"], out)
            self.assertEqual(out["reason"], "PROFITABILITY_GATE")
            self.assertTrue(out["profitability_blockers"])

    def test_confirmed_multi_tf_setup_can_enter_candidate_books(self):
        row=self.row()
        row["horizon_structure"]={"direction":"SHORT","score":0.78,"state":"CONFIRMED_TREND"}
        row["institutional_signal"]={
            "evidence_independence":{"independent_count":5},
            "breakout_quality":{"state":"CONFIRMED_BREAKOUT"}
        }
        row["_supporting_horizons"]=["1h","4h","1d","3d"]
        row["_alignment_count"]=4
        row["trade_plan"]["setup_memory"]={
            "effective_n":0.0,"posterior_win_rate":None,"weighted_avg_pnl":None
        }
        for name in ("Champion","Challenger"):
            out=VP._signal_first_admission(dict(row),VP.POLICIES[name],0.0)
            self.assertTrue(out["open"],out)
            self.assertEqual(out["reason"],"CANONICAL_SIGNAL_ENTRY")
            self.assertGreater(out["fraction"],0.0)

    def test_weak_breakout_is_blocked_by_profitability_history(self):
        row=self.row()
        row["institutional_signal"]={
            "evidence_independence":{"independent_count":5},
            "breakout_quality":{"state":"WEAK_BREAKOUT"}
        }
        row["horizon_structure"]={"direction":"SHORT","score":0.80,"state":"CONFIRMED_TREND"}
        row["_supporting_horizons"]=["1h","4h","1d"]
        row["_alignment_count"]=3
        for name in VP.POLICIES:
            out=VP._signal_first_admission(dict(row),VP.POLICIES[name],0.0)
            self.assertFalse(out["open"],out)
            self.assertEqual(out["reason"],"PROFITABILITY_GATE")
            self.assertIn("WEAK_BREAKOUT_NEGATIVE_HISTORY",out["profitability_blockers"])

    def test_uncalibrated_score_is_not_reported_as_probability(self):
        out = VP._signal_first_admission(self.row(), VP.POLICIES["Aggressive"], 0.0)
        self.assertIsNone(out["probability"])
        self.assertIsNotNone(out["model_quality_score"])
        self.assertIn("UNCALIBRATED", out["probability_source"])

    def test_negative_setup_memory_blocks_candidate_repetition(self):
        row=self.row()
        row["horizon_structure"]={"direction":"SHORT","score":0.80,"state":"CONFIRMED_TREND"}
        row["institutional_signal"]={
            "evidence_independence":{"independent_count":5},
            "breakout_quality":{"state":"CONFIRMED_BREAKOUT"}
        }
        row["_supporting_horizons"]=["1h","4h","1d"]
        row["_alignment_count"]=3
        row["trade_plan"]["setup_memory"]={
            "effective_n":12.0,"posterior_win_rate":0.42,"weighted_avg_pnl":-0.001
        }
        out=VP._signal_first_admission(row,VP.POLICIES["Champion"],0.0)
        self.assertFalse(out["open"],out)
        self.assertIn("NEGATIVE_SETUP_EXPECTANCY_HISTORY",out["profitability_blockers"])
        self.assertIn("SETUP_WIN_RATE_TOO_LOW",out["profitability_blockers"])

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
