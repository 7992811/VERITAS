"""Currency economics cannot drift from CTC through legacy startup settings."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from datetime import datetime, timezone

import veritas_canonical_runtime as VCR
import veritas_canonical_constitution as CTC
import veritas_execution as VX
from test_veritas_timeframe_policy import structural_row


class CurrencyCostConsistencyTests(unittest.TestCase):
    @staticmethod
    def plan(direction, move, spread_bps=0.0, horizon="5m"):
        sign = 1 if direction == "LONG" else -1
        return {
            "direction": direction,
            "entry_price": 100.0,
            "stop_price": 100.0 * (1.0 - sign * 0.0005),
            "target_price": 100.0 * (1.0 + sign * move),
            "expected_move_pct": move,
            "expected_to_stop_ratio": move / 0.0005,
            "spread_bps": spread_bps,
            "horizon": horizon,
        }

    def test_stale_or_malformed_environment_cannot_raise_currency_move_floor(self):
        script = """
import json
import veritas_execution as X
g = X.economics_gate('CNYRUBF', {
    'direction': 'LONG', 'entry_price': 100., 'stop_price': 99.95,
    'target_price': 100.21, 'expected_move_pct': .0021,
    'expected_to_stop_ratio': 4.2, 'horizon': '5m',
})
print(json.dumps({
    'floor': X.MIN_EXPECTED_MOVE_PCT,
    'multiple': X.MIN_MOVE_COST_MULTIPLE,
    'required': g['minimum_expected_move_pct'],
    'modeled_cost': g['modeled_round_trip_cost_pct'],
    'accounting_buffer': X.VC.COST_BUFFER_MULTIPLE,
    'move_blocked': 'EXPECTED_MOVE_BELOW_COST_BUFFER' in g['blockers'],
    'commission': g['cost_policy']['commission_rate_per_side'],
}))
"""
        for stale_value in ("0.004", "0.006", "not-a-number"):
            with self.subTest(stale_value=stale_value):
                env = dict(os.environ, VERITAS_FINAL_MIN_EXPECTED_MOVE=stale_value)
                result = subprocess.run(
                    [sys.executable, "-c", script], cwd=Path(__file__).resolve().parent,
                    env=env, check=True, capture_output=True, text=True,
                )
                state = json.loads(result.stdout)
                self.assertAlmostEqual(state["floor"], 0.0019)
                self.assertAlmostEqual(state["multiple"], 1.1)
                self.assertAlmostEqual(state["accounting_buffer"], 1.1)
                self.assertAlmostEqual(state["required"], max(.0019, 1.1*state["modeled_cost"]))
                self.assertFalse(state["move_blocked"])
                self.assertAlmostEqual(state["commission"], 0.0004)

    def test_low_cost_currency_signals_still_respect_absolute_move_floor(self):
        for direction in ("LONG", "SHORT"):
            for horizon in ("5m", "1h", "4h"):
                with self.subTest(direction=direction, horizon=horizon):
                    below = VX.economics_gate("CNYRUBF", self.plan(direction, 0.00185, horizon=horizon))
                    above = VX.economics_gate("CNYRUBF", self.plan(direction, 0.00195, horizon=horizon))
                    self.assertIn("EXPECTED_MOVE_BELOW_COST_BUFFER", below["blockers"])
                    self.assertNotIn("EXPECTED_MOVE_BELOW_COST_BUFFER", above["blockers"])
                    self.assertAlmostEqual(above["minimum_expected_move_pct"],
                                           max(.0019,1.1*above["modeled_round_trip_cost_pct"]))
                    self.assertEqual(VX.minimum_expected_move_pct(.0001),.0019)

    def test_wider_currency_spread_requires_owner_buffer(self):
        for direction in ("LONG", "SHORT"):
            with self.subTest(direction=direction):
                below = VX.economics_gate("CNYRUBF", self.plan(direction, 0.0032, spread_bps=30))
                above = VX.economics_gate("CNYRUBF", self.plan(direction, 0.0034, spread_bps=30))
                self.assertAlmostEqual(above["modeled_round_trip_cost_pct"], 0.003)
                self.assertAlmostEqual(above["minimum_expected_move_pct"], 0.0033)
                self.assertIn("EXPECTED_MOVE_BELOW_COST_BUFFER", below["blockers"])
                self.assertNotIn("EXPECTED_MOVE_BELOW_COST_BUFFER", above["blockers"])


class CurrencyRoutingConsistencyTests(unittest.TestCase):
    @staticmethod
    def row(horizon, direction="LONG", **overrides):
        clock = datetime.now(timezone.utc)
        sign = 1 if direction == "LONG" else -1
        # Exercise the owner's real native-timeframe breakout detector using
        # causal OHLC, rather than manufacturing a current displayed signal.
        row = structural_row(clock, timeframe=horizon, asset="CNYRUBF",
                             direction=direction, width=overrides.pop("width", 1.0))
        row.update(signal_tier=direction, confidence=0.80,
                   realized_vol=0.002, horizon_return=sign * 0.001,
                   independent_evidence_families=4, entry_quality="FRESH_BREAKOUT")
        row.update(overrides)
        return row

    def test_currency_uses_one_hour_when_five_minute_quote_is_stale(self):
        five = self.row("5m", market_observed_at="2000-01-01T00:00:00+00:00")
        hour = self.row("1h")
        chosen = VCR.currency_candidate_book([five, hour])["CNYRUBF"]
        self.assertEqual(chosen["horizon"], "1h")
        attempts = chosen["_currency_route_trace"]
        self.assertEqual(attempts[0]["reason"], "EXECUTION_QUOTE_STALE")
        self.assertFalse(attempts[0]["open"])
        self.assertTrue(attempts[-1]["open"])

    def test_currency_uses_four_hours_when_faster_setups_fail_cost_gate(self):
        # The faster OHLC structures have too little native target room to
        # cover costs. The wider 4h structure retains its own anchors and ATR.
        five, hour, four = self.row("5m", width=.025), self.row("1h", width=.025), self.row("4h")
        chosen = VCR.currency_candidate_book([five, hour, four])["CNYRUBF"]
        self.assertEqual(chosen["horizon"], "4h")
        self.assertEqual([x["horizon"] for x in chosen["_currency_route_trace"]], ["5m", "1h", "4h"])
        self.assertTrue(all(not x["open"] for x in chosen["_currency_route_trace"][:2]))
        self.assertTrue(all(x["canonical_stage"] == "ECONOMICS" for x in chosen["_currency_route_trace"][:2]))
        self.assertTrue(chosen["_currency_route_trace"][-1]["open"])

    def test_currency_keeps_five_minute_priority_when_multiple_setups_pass(self):
        chosen = VCR.currency_candidate_book([
            self.row("4h", confidence=0.99), self.row("1h", confidence=0.95),
            self.row("5m", confidence=0.62),
        ])["CNYRUBF"]
        self.assertEqual(chosen["horizon"], "5m")
        self.assertTrue(chosen["_currency_route_trace"][0]["open"])

    def test_no_admissible_currency_setup_preserves_priority_and_real_blocker(self):
        chosen = VCR.currency_candidate_book([
            self.row("5m", paper_eligible=False),
            self.row("1h", source_gate_pass=False),
            self.row("4h", market_open=False),
        ])["CNYRUBF"]
        self.assertEqual(chosen["horizon"], "5m")
        self.assertEqual(chosen["_currency_route_trace"][0]["reason"], "PAPER_EXPLICIT_DENIAL")
        self.assertTrue(all(not x["open"] for x in chosen["_currency_route_trace"]))
        admission = VCR.evaluate(chosen, CTC.runtime_portfolio_policy("Currency"), 0.0)
        self.assertEqual(admission["reason"], "PAPER_EXPLICIT_DENIAL")

    def test_fallback_never_bypasses_confirmed_four_hour_conflict(self):
        four = self.row("4h", direction="SHORT", research_decision="NO_TRADE", decision="NO_TRADE")
        daily = self.row("1d")
        chosen = VCR.currency_candidate_book([daily, four])["CNYRUBF"]
        self.assertTrue(chosen["_currency_mtf_conflict"])
        self.assertFalse(chosen["_currency_route_trace"][0]["open"])
        self.assertEqual(chosen["_currency_route_trace"][0]["reason"], "CURRENCY_MTF_DIRECTION_CONFLICT")


if __name__ == "__main__":
    unittest.main()
