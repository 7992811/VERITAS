import unittest
from pathlib import Path

import veritas_execution as VX


class ExecutionSafetyTests(unittest.TestCase):
    def test_bad_rr_is_blocked_even_for_setup(self):
        gate = VX.economics_gate("BTC", {
            "eligible": True,
            "entry_price": 100.0,
            "stop_price": 99.0,
            "expected_move_pct": 0.01,
            "expected_to_stop_ratio": 0.80,
        })
        self.assertFalse(gate["eligible"])
        self.assertIn("RR_BELOW_FINAL_FLOOR", gate["blockers"])

    def test_move_below_cost_buffer_is_blocked(self):
        gate = VX.economics_gate("BTC", {
            "eligible": True,
            "entry_price": 100.0,
            "stop_price": 99.0,
            "expected_move_pct": 0.001,
            "expected_to_stop_ratio": 2.0,
        })
        self.assertFalse(gate["eligible"])
        self.assertIn("EXPECTED_MOVE_BELOW_COST_BUFFER", gate["blockers"])

    def test_good_economics_pass(self):
        gate = VX.economics_gate("BTC", {
            "eligible": True,
            "entry_price": 100.0,
            "stop_price": 99.0,
            "expected_move_pct": 0.012,
            "expected_to_stop_ratio": 1.50,
        })
        self.assertTrue(gate["eligible"])

    def test_crypto_production_needs_two_direct_quotes(self):
        good = VX.production_source_gate("BTC", {
            "source_gate_pass": True,
            "market_open": True,
            "secondary_price": 100.01,
            "source_divergence": 0.0001,
        }, {"ok": True})
        self.assertTrue(good["eligible"])
        bad = VX.production_source_gate("BTC", {
            "source_gate_pass": True,
            "market_open": True,
            "secondary_price": None,
            "source_divergence": 0.0,
        }, {"ok": True})
        self.assertFalse(bad["eligible"])

    def test_research_futures_feed_is_not_production_ready(self):
        gate = VX.production_source_gate("NQ", {
            "source_gate_pass": True,
            "market_open": True,
            "production_direct_feed": False,
        })
        self.assertFalse(gate["eligible"])
        self.assertIn("PRODUCTION_DIRECT_FEED_NOT_CONFIGURED", gate["blockers"])

    def test_client_order_id_is_deterministic(self):
        a = VX.make_client_order_id("Champion","BTC","LONG",0.25,"1h","2026-09-28T00:00:00Z","ENTRY")
        b = VX.make_client_order_id("Champion","BTC","LONG",0.25,"1h","2026-09-28T00:00:00Z","ENTRY")
        c = VX.make_client_order_id("Champion","BTC","LONG",0.30,"1h","2026-09-28T00:00:00Z","ENTRY")
        self.assertEqual(a,b)
        self.assertNotEqual(a,c)

    def test_paper_fill_is_adverse(self):
        buy = VX.simulated_fill("BTC","BUY",100.0,0.25)
        sell = VX.simulated_fill("BTC","SELL",100.0,0.25)
        self.assertGreater(buy["fill_price"],100.0)
        self.assertLess(sell["fill_price"],100.0)

    def test_live_risk_profile_is_conservative(self):
        self.assertLessEqual(VX.LIVE_RISK_PROFILE["max_stop_risk_nav"],0.005)
        self.assertLessEqual(VX.LIVE_RISK_PROFILE["max_gross"],1.25)
        self.assertFalse(VX.LIVE_RISK_PROFILE["allow_new_risk_without_durable_storage"])

    def test_nq_outcomes_use_nq_futures(self):
        src = Path("veritas_intelligence.py").read_text(encoding="utf-8")
        self.assertIn("return _yahoo_between('NQ%3DF'", src)
        self.assertIn("VERITAS V90 EXECUTION SAFETY R40", src)


if __name__ == "__main__":
    unittest.main()
