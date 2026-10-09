import unittest
from datetime import datetime, timedelta, timezone

import veritas_canonical_constitution as CTC
import veritas_stop_risk as VSR
import veritas_structural_lifecycle as VSL
import veritas_position_guard as VPG
import veritas_price_source as VPS
import veritas_paper_entry as VPE
import veritas_trend_entry as VTE
import veritas_user_teaching as VUT


def economics():
    return {
        "eligible": True,
        "blockers": [],
        "net_risk_pct": 0.012,
        "net_reward_pct": 0.024,
        "net_reward_risk": 2.0,
        "modeled_entry_fill": 100.0,
        "modeled_stop_fill": 99.0,
        "modeled_target_fill": 102.0,
        "minimum_reward_risk": 1.15,
    }


def trend_row(*, state="CONFIRMED_TREND", horizon="5m", tier="SUPER_LONG",
              evidence=4, expected=0.01, supporting=None, mid=False):
    context = {"event": {"target_progress": 0.20}}
    if mid:
        context["confirmation_15m_trend"] = {
            "confirmed": True, "direction": "LONG", "timeframe": "15m"
        }
    return {
        "research_decision": "LONG",
        "horizon": horizon,
        "signal_tier": tier,
        "independent_evidence_families": evidence,
        "_supporting_horizons": list(supporting or []),
        "horizon_structure": {"state": state},
        "trade_plan": {
            "expected_move_pct": expected,
            "trend_entry_context": context,
            "trade_integrity": {},
        },
        "trend_entry_context": context,
        "institutional_signal": {
            "evidence_independence": {"independent_count": evidence}
        },
    }


class StopRiskDiagnosticsTests(unittest.TestCase):
    def test_gross_cap_is_not_mislabeled_as_stop_risk(self):
        policy = {
            "position_step": 0.05, "max_fraction": 0.75, "max_gross": 0.50
        }
        result = VSR.cap_fraction_from_economics(
            economics(), 0.25, policy,
            current_fraction=0.05,
            existing_stop_risk_nav=0.0005,
            gross_excluding_position=0.45,
        )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "PORTFOLIO_GROSS_CAP_EXCEEDED")
        self.assertEqual(result["binding_constraint"], "GROSS")

    def test_real_stop_risk_cap_keeps_its_own_reason(self):
        policy = {
            "position_step": 0.05, "max_fraction": 0.75, "max_gross": 2.0
        }
        result = VSR.cap_fraction_from_economics(
            economics(), 0.25, policy,
            current_fraction=0.05,
            existing_stop_risk_nav=0.1495,
            gross_excluding_position=0.0,
        )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "STOP_RISK_CAP_EXCEEDED")
        self.assertEqual(result["binding_constraint"], "STOP_RISK")


class AccelerationGrossHeadroomTests(unittest.TestCase):
    def test_impulse_normal_state_earns_temporary_gross_headroom(self):
        policy = dict(CTC.runtime_portfolio_policy("Impulse"))
        row = {
            "_trend_acceleration": {"active": True, "stage": "SENIOR_CONFIRMED"},
            "_canonical_admission": {"risk_governor": {
                "state": "NORMAL", "new_risk": True, "max_gross": 0.50
            }},
        }
        adjusted, acceleration, governor = VPE._policy_with_acceleration_caps(policy, row)
        self.assertTrue(acceleration["active"])
        self.assertEqual(governor["state"], "NORMAL")
        self.assertAlmostEqual(adjusted["max_fraction"], 0.75)
        self.assertAlmostEqual(adjusted["max_gross"], 1.00)

    def test_impulse_caution_state_keeps_drawdown_governor_authority(self):
        policy = dict(CTC.runtime_portfolio_policy("Impulse"))
        row = {
            "_trend_acceleration": {"active": True, "stage": "SENIOR_CONFIRMED"},
            "_canonical_admission": {"risk_governor": {
                "state": "CAUTION", "new_risk": True, "max_gross": 0.45
            }},
        }
        adjusted, _, _ = VPE._policy_with_acceleration_caps(policy, row)
        self.assertAlmostEqual(adjusted["max_fraction"], 0.75)
        self.assertAlmostEqual(adjusted["max_gross"], 0.45)

    def test_currency_never_receives_acceleration_caps(self):
        policy = dict(CTC.runtime_portfolio_policy("Currency"))
        row = {
            "_trend_acceleration": {"active": True, "stage": "SENIOR_CONFIRMED"},
            "_canonical_admission": {"risk_governor": {
                "state": "NORMAL", "new_risk": True, "max_gross": 10.0
            }},
        }
        adjusted, _, _ = VPE._policy_with_acceleration_caps(policy, row)
        self.assertAlmostEqual(adjusted["max_fraction"], policy["max_fraction"])
        self.assertAlmostEqual(adjusted["max_gross"], policy["max_gross"])


class IntermediateTimeframeTests(unittest.TestCase):
    def test_closed_bar_15m_confirmation_is_causal(self):
        bars = [
            {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
             "available_at": 900, "volume": 10},
            {"open": 100.5, "high": 102.0, "low": 100.4, "close": 101.5,
             "available_at": 1800, "volume": 12},
        ]
        result = VTE.trend_confirmation(bars, "15m")
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["direction"], "LONG")
        self.assertEqual(result["closed_at"], 1800)

    def test_no_confirmation_inside_previous_range(self):
        bars = [
            {"open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0,
             "available_at": 1800, "volume": 10},
            {"open": 101.0, "high": 101.8, "low": 100.2, "close": 101.4,
             "available_at": 3600, "volume": 11},
        ]
        result = VTE.trend_confirmation(bars, "30m")
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["direction"], "NO_TRADE")


class MFEProtectionTests(unittest.TestCase):
    def structural_position_and_quote(self, price=100.16):
        now = datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc)
        quote = {
            "price": price, "best_bid": price, "best_ask": price + 0.01,
            "observed_at": now.isoformat(), "source_gate_pass": True,
            "source_names": {"primary": "ProFinance NASD100_FUT"},
            "market_open": True,
        }
        identity = VPS.identity("NQ", quote)
        position = {
            "portfolio_name": "Impulse", "asset": "NQ", "direction": "LONG",
            "units": 1.0, "avg_entry_price": 100.0, "stop_price": 98.0,
            "active_trade_id": "mfe-test",
            "payload": {
                "structural_policy_version": "TEST",
                "price_source_lock": identity,
                "execution_timeframe": "5m",
                "mfe_pct": max(0.0, price - 100.0),
            },
        }
        return position, quote, now

    def test_015pct_starts_persistence_instead_of_immediate_lock(self):
        position, quote, now = self.structural_position_and_quote(100.16)
        result = VPG._structural_mfe_profit_lock(None, object(), position, quote,
                                                 now.isoformat(), now)
        self.assertEqual(result["state"], "PERSISTENCE_PENDING")
        self.assertIn("r_accel_mfe_candidate_at", result["patch"])
        self.assertIsNone(result["lock"])

    def test_lost_015pct_persistence_resets_candidate(self):
        position, quote, now = self.structural_position_and_quote(100.16)
        first = VPG._structural_mfe_profit_lock(None, object(), position, quote,
                                                now.isoformat(), now)
        position["payload"].update(first["patch"])
        lower = dict(quote, price=100.10, best_bid=100.10, best_ask=100.11,
                     observed_at=(now + timedelta(seconds=30)).isoformat())
        result = VPG._structural_mfe_profit_lock(
            None, object(), position, lower, lower["observed_at"],
            now + timedelta(seconds=30))
        self.assertEqual(result["state"], "BELOW_THRESHOLD")
        self.assertIsNone(result["patch"]["r_accel_mfe_candidate_at"])

    def test_currency_portfolio_is_not_modified_by_mfe_lane(self):
        position, quote, now = self.structural_position_and_quote(100.50)
        position["portfolio_name"] = "Currency"
        result = VPG._structural_mfe_profit_lock(None, object(), position, quote,
                                                 now.isoformat(), now)
        self.assertEqual(result["state"], "NOT_APPLICABLE")
        self.assertEqual(result["patch"], {})


class TrendAccelerationTests(unittest.TestCase):
    def test_fast_confirmation_earns_first_standard_scale(self):
        result = VSL._trend_acceleration_state(
            trend_row(), "LONG", {"mode": "CORE"}
        )
        self.assertTrue(result["active"])
        self.assertEqual(result["stage"], "FAST_CONFIRMED")
        self.assertAlmostEqual(result["target_fraction"], 0.25)

    def test_mid_confirmation_earns_half_nav_standard_target(self):
        row = trend_row(state="BUILDING_TREND", tier="LONG", mid=True)
        result = VSL._trend_acceleration_state(row, "LONG", {"mode": "CORE"})
        self.assertTrue(result["active"])
        self.assertEqual(result["stage"], "MID_CONFIRMED")
        self.assertAlmostEqual(result["target_fraction"], 0.50)

    def test_senior_confirmation_earns_75pct_standard_target(self):
        row = trend_row(supporting=["1h"])
        result = VSL._trend_acceleration_state(row, "LONG", {"mode": "CORE"})
        self.assertEqual(result["stage"], "SENIOR_CONFIRMED")
        self.assertAlmostEqual(result["target_fraction"], 0.75)

    def test_aggressive_senior_target_is_250pct(self):
        row = trend_row(supporting=["1h", "4h"])
        result = VSL._trend_acceleration_state(
            row, "LONG", {"mode": "AGGRESSIVE"}
        )
        self.assertEqual(result["stage"], "SENIOR_CONFIRMED")
        self.assertAlmostEqual(result["target_fraction"], 2.50)

    def test_currency_is_explicitly_excluded(self):
        result = VSL._trend_acceleration_state(
            trend_row(), "LONG", {"mode": "CURRENCY"}
        )
        self.assertFalse(result["active"])

    def test_fast_reversal_can_exit_without_authorizing_entry(self):
        position = {"direction": "SHORT"}
        row = trend_row()
        result = VSL.fast_reversal_exit_eligible(
            position, row, {"mode": "CORE"}
        )
        self.assertTrue(result["eligible"])
        self.assertEqual(result["reason"], "FAST_REVERSAL_CONFIRMED_EXIT")

    def test_teaching_snapshot_matches_ctc_policy(self):
        snapshot = VUT.acceleration_policy_snapshot()
        self.assertEqual(snapshot["teaching_id"], CTC.TREND_ACCELERATION_POLICY["teaching_id"])
        self.assertEqual(snapshot["scope"], "PAPER_NON_CURRENCY_PORTFOLIOS")
        self.assertIn("Aggressive", snapshot["portfolios"])
        self.assertNotIn("Currency", snapshot["portfolios"])


if __name__ == "__main__":
    unittest.main()
