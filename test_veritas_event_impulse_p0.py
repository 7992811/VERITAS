import unittest
from datetime import datetime, timezone

import veritas_canonical_constitution as CTC
import veritas_canonical_runtime as VCR
import veritas_event_impulse as VEI
import veritas_structural_lifecycle as VSL
import veritas_trend_day_efficiency as VTDE


SIGNAL_AT = datetime(2026, 10, 9, 18, 46, tzinfo=timezone.utc).timestamp()


def game_row(asset="MOEX", direction="LONG", event_score=0.0, news=None):
    event = {
        "event_id": asset + "_GAME_CHANGER",
        "event_type": "VERIFIED_QUOTE_STRUCTURAL_BREAKOUT",
        "direction": direction,
        "timeframe": "1m",
        "trigger_timeframe": "1h",
        "structural_timeframe": "1h",
        "signal_at": SIGNAL_AT,
        "activity_confirmed": True,
        "senior_level_break": True,
        "game_changer_extreme": True,
        "intrabar_volatility_shock": {
            "version": VEI.VERSION,
            "eligible": True,
            "severity": "GAME_CHANGER_EXTREME",
            "exceeds": {"1m": True, "5m": True, "1h": True},
            "ratios": {"1m": 8.0, "5m": 3.0, "1h": 1.15},
        },
    }
    context = {"status": "OK", "event": event}
    row = {
        "asset": asset,
        "horizon": "1m",
        "research_decision": direction,
        "decision": direction,
        "signal_tier": direction,
        # Slow state is intentionally stale. The sealed quote event owns timing.
        "regime": "RANGE_LOW_VOL",
        "independent_evidence_families": 0,
        "horizon_structure": {"direction": "NO_TRADE", "score": 0.0, "state": "NEUTRAL"},
        "event_shadow_score": event_score,
        "timeframe_entry_context": context,
        "trend_entry_context": context,
        "trade_plan": {"timeframe_entry_context": context, "trade_integrity": {}},
    }
    if news is not None:
        row["event_news_verification"] = news
    return row


class IntrabarGameChangerTests(unittest.TestCase):
    def test_forming_move_requires_real_volatility_coverage(self):
        missing = VEI.quote_shock(100.0, 103.0, {"1h": 2.0})
        self.assertFalse(missing["eligible"], missing)
        full = VEI.quote_shock(100.0, 103.0, {"1m": .20, "5m": .70, "1h": 2.0})
        self.assertTrue(full["eligible"], full)
        self.assertEqual(full["severity"], "GAME_CHANGER_EXTREME")

    def test_game_changer_bypasses_lagging_regime_tier_evidence_but_not_news_conflict(self):
        row = game_row()
        impulse = VTDE.event_impulse_assess(row, "LONG")
        self.assertTrue(impulse["eligible"], impulse)
        self.assertEqual(impulse["reason"], "GAME_CHANGER_EXTREME_CONFIRMED")
        self.assertTrue(impulse["immediate_max"])
        self.assertFalse(impulse["forming_bar_close_required"])
        self.assertEqual(impulse["news_check"]["status"], "PENDING")
        self.assertTrue(impulse["news_check"]["requested"])
        self.assertFalse(impulse["entry_requires_news"])

        conflict = game_row(news={"status": "CONFIRMED", "direction": "SHORT"})
        blocked_max = VTDE.event_impulse_assess(conflict, "LONG")
        self.assertTrue(blocked_max["eligible"])
        self.assertTrue(blocked_max["news_conflict"])
        self.assertFalse(blocked_max["immediate_max"])

    def test_signed_news_score_cannot_turn_opposite_news_into_confirmation(self):
        self.assertTrue(VEI.news_check(game_row(event_score=.80), "LONG", SIGNAL_AT)["confirmed"])
        self.assertTrue(VEI.news_check(game_row(event_score=-.80), "LONG", SIGNAL_AT)["conflict"])
        short = game_row(direction="SHORT", event_score=-.80)
        self.assertTrue(VEI.news_check(short, "SHORT", SIGNAL_AT)["confirmed"])

    def test_same_game_changer_rule_scales_each_portfolio_to_its_allowed_event_cap(self):
        expected = {
            "Impulse": 1.0,
            "Champion": 1.0,
            "Challenger": 1.0,
            "Aggressive": 5.0,
            "Currency": 10.0,
        }
        for name, target in expected.items():
            with self.subTest(portfolio=name):
                asset = "CNYRUBF" if name == "Currency" else "MOEX"
                row = game_row(asset=asset)
                policy = CTC.runtime_portfolio_policy(name)
                state = VSL._trend_acceleration_state(row, "LONG", policy)
                self.assertEqual(state["stage"], "GAME_CHANGER_MAX_IMMEDIATE", state)
                self.assertAlmostEqual(state["target_fraction"], target)
                fraction, governor = VCR._fraction(dict(policy, _row=row), 0.0, soft=False)
                self.assertEqual(governor["state"], "NORMAL")
                self.assertAlmostEqual(fraction, target)

    def test_currency_ordinary_trend_acceleration_remains_disabled(self):
        row = game_row(asset="CNYRUBF")
        row["timeframe_entry_context"]["event"]["game_changer_extreme"] = False
        row["timeframe_entry_context"]["event"]["intrabar_volatility_shock"] = {}
        row.update(regime="RANGE_LOW_VOL", independent_evidence_families=10)
        state = VSL._trend_acceleration_state(
            row, "LONG", CTC.runtime_portfolio_policy("Currency"))
        self.assertFalse(state["active"])
        self.assertEqual(state["reason"], "CURRENCY_EVENT_IMPULSE_ONLY")


if __name__ == "__main__":
    unittest.main()
