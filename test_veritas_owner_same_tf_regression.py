"""Regression-first proof for the owner's exact entry-timeframe geometry.

This test intentionally exercises the quote-breakout path that regressed after
the original same-timeframe policy. It does not change thresholds, costs,
portfolio sizing, sources, contracts or live execution.
"""
import unittest

import veritas_structural_breakout as SB
from test_veritas_structural_breakout import POLICY, raw_at


class OwnerSameTimeframeRegressionTests(unittest.TestCase):
    def test_fast_breakout_uses_entry_timeframe_for_all_execution_geometry(self):
        raw, at = raw_at()
        for timeframe in ("1m", "5m"):
            with self.subTest(timeframe=timeframe):
                context = SB.build_context(raw, timeframe, at, config=POLICY)
                event = context.get("event")
                self.assertIsNotNone(event, context)
                self.assertEqual(event.get("timeframe"), timeframe)
                self.assertEqual(event.get("trigger_timeframe"), timeframe)
                self.assertEqual(event.get("structural_timeframe"), timeframe)
                self.assertEqual(event.get("stop_timeframe"), timeframe)
                self.assertEqual(event.get("atr_timeframe"), timeframe)
                self.assertEqual(event.get("target_timeframe"), timeframe)
                self.assertEqual((event.get("protected_swing") or {}).get("timeframe"), timeframe)
                self.assertTrue(event.get("target_ladder"))
                self.assertTrue(
                    all(timeframe in (step.get("timeframes") or [])
                        for step in event["target_ladder"]),
                    event["target_ladder"],
                )


if __name__ == "__main__":
    unittest.main()
