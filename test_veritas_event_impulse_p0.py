import unittest
from datetime import datetime, timezone

import veritas_event_impulse as VEI
import veritas_execution as VX
import veritas_execution_snapshot as VES
import veritas_structural_lifecycle as VSL
import veritas_trend_day_efficiency as VTDE


# Verification branch: run the dedicated P0 suite on the exact current main.
class IntrabarGameChangerP0Tests(unittest.TestCase):
    def event_row(self):
        event={
            "event_id":"MOEX_20261009_2146",
            "event_type":"VERIFIED_QUOTE_STRUCTURAL_BREAKOUT",
            "direction":"LONG","timeframe":"1m","trigger_timeframe":"1h",
            "structural_timeframe":"1h","stop_timeframe":"1h","atr_timeframe":"1h",
            "trigger_level":2325.0,"signal_price":2325.0,
            "stop_anchor":2301.4,"stop_price":2294.0,
            "target_price":2416.33,"runner_target_price":2516.38,
            "activity_confirmed":True,"senior_level_break":True,
            "game_changer_extreme":True,
            "intrabar_volatility_shock":{
                "eligible":True,"severity":"GAME_CHANGER_EXTREME",
                "exceeds":{"1m":True,"5m":True,"1h":True},
                "ratios":{"1m":8.0,"5m":3.0,"1h":1.15},
            },
        }
        context={"status":"OK","event":event}
        return {
            "asset":"MOEX","horizon":"1m","price":2325.0,
            "research_decision":"LONG","decision":"LONG","signal_tier":"LONG",
            # Deliberately lagging slow-model state: the quote event must win.
            "regime":"RANGE_LOW_VOL","independent_evidence_families":0,
            "horizon_structure":{"direction":"NO_TRADE","score":0.0,"state":"NEUTRAL"},
            "timeframe_entry_context":context,"trend_entry_context":context,
            "trade_plan":{"timeframe_entry_context":context,"trade_integrity":{}},
        }

    def test_forming_move_can_exceed_completed_multi_tf_volatility(self):
        shock=VEI.quote_shock(2323.5,2325.5,{"1m":0.20,"5m":0.70,"1h":1.70})
        self.assertTrue(shock["eligible"],shock)
        self.assertEqual(shock["severity"],"GAME_CHANGER_EXTREME")
        self.assertTrue(all(shock["exceeds"].values()))

    def test_2146_quote_event_is_confirmation_without_waiting_for_super_or_news(self):
        row=self.event_row()
        impulse=VTDE.event_impulse_assess(row,"LONG")
        self.assertTrue(impulse["eligible"],impulse)
        self.assertEqual(impulse["reason"],"GAME_CHANGER_EXTREME_CONFIRMED")
        self.assertTrue(impulse["immediate_max"])
        self.assertFalse(impulse["forming_bar_close_required"])
        self.assertFalse(impulse["entry_requires_news"])

    def test_game_changer_requests_maximum_allocation_immediately(self):
        row=self.event_row()
        core=VSL._trend_acceleration_state(row,"LONG",{"mode":"CORE"})
        aggressive=VSL._trend_acceleration_state(row,"LONG",{"mode":"AGGRESSIVE"})
        self.assertEqual(core["stage"],"GAME_CHANGER_MAX_IMMEDIATE")
        self.assertAlmostEqual(core["target_fraction"],1.0)
        self.assertEqual(aggressive["stage"],"GAME_CHANGER_MAX_IMMEDIATE")
        self.assertAlmostEqual(aggressive["target_fraction"],5.0)

    def test_nearest_offer_fill_is_frozen_and_changed_quote_requires_requote(self):
        plan={
            "direction":"LONG","entry_price":2325.0,"stop_price":2300.0,
            "target_price":2422.0,"expected_move_pct":0.0417,
            "expected_to_stop_ratio":3.8,"initial_position_fraction":1.0,
            "best_bid":2324.8,"best_ask":2325.0,
            "execution_style":"MARKETABLE_LIMIT_NEAREST_OFFER_SWEEP",
            "fill_confirmation_required":True,
            "partial_fill_policy":"REQUOTE_REMAINDER_WHILE_CANONICAL_ADMISSION_VALID",
            "reprice_policy":"REFRESH_TOP_OF_BOOK_RECHECK_STOP_RISK_AND_ANTI_CHASE",
            "execution_instrument_required":True,
            "horizon":"1m",
        }
        gate=VX.economics_gate("MOEX",plan,now=datetime(2026,10,9,18,46,tzinfo=timezone.utc))
        self.assertTrue(gate["eligible"],gate)
        fill=gate["entry_execution_model"]
        self.assertEqual(fill["execution_style"],"MARKETABLE_LIMIT_NEAREST_OFFER_SWEEP")
        self.assertEqual(fill["orderbook_price_source"],"BEST_ASK")
        self.assertTrue(fill["fill_confirmation_required"])
        quote={"asset":"MOEX","price":2325.0,"best_bid":2324.8,"best_ask":2325.0,
               "observed_at":"2026-10-09T18:46:00+00:00","source":"MOEX ISS IMOEX",
               "source_gate_pass":True,"market_open":True}
        row={"asset":"MOEX","horizon":"1m","price":2325.0,"trade_plan":plan}
        snap=VES.capture(row,quote,"LONG",1.0,datetime(2026,10,9,18,46,tzinfo=timezone.utc),gate)
        self.assertIsNotNone(snap)
        gate=dict(gate,execution_snapshot=snap)
        self.assertIsNotNone(VES.checked_fill(gate,quote,"MOEX","LONG",2325.0,1.0))
        moved=dict(quote,price=2326.0,best_bid=2325.8,best_ask=2326.0,
                   observed_at="2026-10-09T18:46:01+00:00")
        self.assertIsNone(VES.checked_fill(gate,moved,"MOEX","LONG",2326.0,1.0))


if __name__=="__main__":
    unittest.main()
