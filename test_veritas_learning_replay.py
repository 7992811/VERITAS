import os
import unittest
from unittest.mock import patch

import veritas_learning_replay as R
import veritas_promotion as P


class LearningReplayTests(unittest.TestCase):
    def bars(self):
        return [
            {"start": 1.0, "end": 2.0, "open": 100.0, "high": 102.0, "low": 99.5, "close": 101.0},
            {"start": 2.0, "end": 3.0, "open": 101.0, "high": 104.0, "low": 100.5, "close": 103.0},
            {"start": 3.0, "end": 4.0, "open": 103.0, "high": 106.0, "low": 102.0, "close": 105.0},
        ]

    def test_baseline_target_replay(self):
        out = R.replay_full(100.0, 98.0, 104.0, "LONG", self.bars(), cost=.0016)
        self.assertTrue(out["eligible"])
        self.assertEqual(out["reason"], "TARGET")
        self.assertAlmostEqual(out["gross"], .04)
        self.assertAlmostEqual(out["net"], .0384)

    def test_ambiguous_stop_target_bar_is_excluded(self):
        bars = [{"start": 1.0, "end": 2.0, "open": 100.0,
                 "high": 105.0, "low": 97.0, "close": 101.0}]
        out = R.replay_full(100.0, 98.0, 104.0, "LONG", bars)
        self.assertFalse(out["eligible"])
        self.assertEqual(out["reason"], "INTRABAR_ORDER_AMBIGUOUS")

    def test_gap_through_stop_uses_adverse_open(self):
        bars = [{"start": 1.0, "end": 2.0, "open": 96.0,
                 "high": 97.0, "low": 95.0, "close": 96.5}]
        out = R.replay_full(100.0, 98.0, 104.0, "LONG", bars, cost=0)
        self.assertTrue(out["eligible"])
        self.assertEqual(out["reason"], "STOP")
        self.assertEqual(out["exit_price"], 96.0)
        self.assertAlmostEqual(out["gross"], -.04)

    def test_wider_stop_reduces_size_to_hold_risk_constant(self):
        bars = [
            {"start": 1.0, "end": 2.0, "open": 100.0, "high": 101.0, "low": 97.95, "close": 99.0},
            {"start": 2.0, "end": 3.0, "open": 99.0, "high": 105.0, "low": 98.5, "close": 104.0},
        ]
        base = R.replay_full(100.0, 98.0, 104.0, "LONG", bars, cost=0)
        wider = R.replay_wider_stop(100.0, 98.0, 104.0, "LONG", bars,
                                    atr=10.0, delta_atr=.05, cost=0)
        self.assertTrue(base["eligible"])
        self.assertEqual(base["reason"], "STOP")
        self.assertTrue(wider["eligible"])
        self.assertEqual(wider["reason"], "TARGET")
        self.assertLess(wider["risk_neutral_size"], 1.0)
        self.assertGreater(wider["net"], base["net"])

    def test_partial_1r_preserves_runner_and_is_replayable(self):
        bars = [
            {"start": 1.0, "end": 2.0, "open": 100.0, "high": 102.1, "low": 99.5, "close": 102.0},
            {"start": 2.0, "end": 3.0, "open": 102.0, "high": 104.1, "low": 101.5, "close": 104.0},
        ]
        out = R.replay_partial_1r(100.0, 98.0, 104.0, "LONG", bars,
                                  partial=.25, trigger_r=1.0, cost=0)
        self.assertTrue(out["eligible"])
        self.assertTrue(out["partial_hit"])
        self.assertEqual(out["reason"], "TARGET")
        self.assertAlmostEqual(out["gross"], .035)

    def test_partial_trigger_must_precede_structural_target(self):
        out = R.replay_partial_1r(100.0, 95.0, 104.0, "LONG", self.bars(),
                                  partial=.25, trigger_r=1.0)
        self.assertFalse(out["eligible"])
        self.assertEqual(out["reason"], "PARTIAL_TRIGGER_NOT_BEFORE_TARGET")

    def evidence(self, **overrides):
        base = dict(
            model_version="candidate",
            oos_n=120, oos_expectancy=.020, oos_profit_factor=1.40,
            vault_n=60, vault_expectancy=.015, vault_profit_factor=1.30,
            high_cost_expectancy=.010,
            calibration_n=0, ece=None,
            shadow_trades=60, shadow_expectancy=.012, shadow_max_drawdown=.04,
            code_ci_pass=True, data_parity_pass=True,
            calibration_applicable=False,
            requires_baseline_outperformance=True,
            baseline_oos_expectancy=.010, baseline_oos_profit_factor=1.20,
            baseline_vault_expectancy=.008, baseline_vault_profit_factor=1.15,
            baseline_shadow_expectancy=.006,
        )
        base.update(overrides)
        return P.PromotionEvidence(**base)

    def test_execution_policy_does_not_require_fake_calibration(self):
        env = {
            "VERITAS_PROMOTION_MIN_OOS_N": "100",
            "VERITAS_PROMOTION_MIN_VAULT_N": "50",
            "VERITAS_PROMOTION_MIN_CALIBRATION_N": "100",
            "VERITAS_PROMOTION_MIN_SHADOW_TRADES": "50",
        }
        with patch.dict(os.environ, env, clear=False):
            gate = P.promotion_gate(self.evidence())
        self.assertTrue(gate["eligible_for_production"])
        self.assertNotIn("CALIBRATION_SAMPLE_TOO_SMALL", gate["blockers"])
        self.assertNotIn("CALIBRATION_ECE_TOO_HIGH_OR_MISSING", gate["blockers"])

    def test_positive_but_worse_challenger_is_blocked(self):
        with patch.dict(os.environ, {
            "VERITAS_PROMOTION_MIN_OOS_N": "1",
            "VERITAS_PROMOTION_MIN_VAULT_N": "1",
            "VERITAS_PROMOTION_MIN_CALIBRATION_N": "1",
            "VERITAS_PROMOTION_MIN_SHADOW_TRADES": "1",
        }, clear=False):
            gate = P.promotion_gate(self.evidence(
                oos_expectancy=.005,
                vault_expectancy=.004,
                shadow_expectancy=.003,
            ))
        self.assertFalse(gate["eligible_for_production"])
        self.assertIn("OOS_NOT_BETTER_THAN_BASELINE", gate["blockers"])
        self.assertIn("VAULT_NOT_BETTER_THAN_BASELINE", gate["blockers"])
        self.assertIn("SHADOW_NOT_BETTER_THAN_BASELINE", gate["blockers"])

    def test_baseline_profit_factor_regression_is_blocked(self):
        with patch.dict(os.environ, {
            "VERITAS_PROMOTION_MIN_OOS_N": "1",
            "VERITAS_PROMOTION_MIN_VAULT_N": "1",
            "VERITAS_PROMOTION_MIN_CALIBRATION_N": "1",
            "VERITAS_PROMOTION_MIN_SHADOW_TRADES": "1",
        }, clear=False):
            gate = P.promotion_gate(self.evidence(
                oos_profit_factor=1.11, baseline_oos_profit_factor=1.30,
                vault_profit_factor=1.06, baseline_vault_profit_factor=1.20,
            ))
        self.assertFalse(gate["eligible_for_production"])
        self.assertIn("OOS_PROFIT_FACTOR_WORSE_THAN_BASELINE", gate["blockers"])
        self.assertIn("VAULT_PROFIT_FACTOR_WORSE_THAN_BASELINE", gate["blockers"])

    def test_probability_models_still_require_calibration(self):
        with patch.dict(os.environ, {
            "VERITAS_PROMOTION_MIN_OOS_N": "1",
            "VERITAS_PROMOTION_MIN_VAULT_N": "1",
            "VERITAS_PROMOTION_MIN_CALIBRATION_N": "100",
            "VERITAS_PROMOTION_MIN_SHADOW_TRADES": "1",
        }, clear=False):
            e = self.evidence(
                calibration_applicable=True,
                calibration_n=0,
                ece=None,
                requires_baseline_outperformance=False,
                baseline_oos_expectancy=None,
                baseline_oos_profit_factor=None,
                baseline_vault_expectancy=None,
                baseline_vault_profit_factor=None,
                baseline_shadow_expectancy=None,
            )
            gate = P.promotion_gate(e)
        self.assertFalse(gate["eligible_for_production"])
        self.assertIn("CALIBRATION_SAMPLE_TOO_SMALL", gate["blockers"])
        self.assertIn("CALIBRATION_ECE_TOO_HIGH_OR_MISSING", gate["blockers"])


if __name__ == "__main__":
    unittest.main()
