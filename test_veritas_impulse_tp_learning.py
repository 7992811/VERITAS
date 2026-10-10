import unittest
from unittest.mock import MagicMock

import veritas_portfolio_runtime as VPR
import veritas_structural_lifecycle as VSL
import veritas_trend_day_efficiency as VTDE
import veritas_user_teaching as VUT


def impulse_row(direction="LONG", *, super_signal=True, contraction=False, price=2400.0):
    tier=("SUPER_LONG" if direction=="LONG" else "SUPER_SHORT") if super_signal else direction
    event={
        "event_id":"IMPULSE_CASE_1",
        "event_type":"VERIFIED_QUOTE_STRUCTURAL_BREAKOUT",
        "direction":direction,
        "timeframe":"1m","trigger_timeframe":"1h","structural_timeframe":"1h",
        "stop_timeframe":"1h","atr_timeframe":"1h",
        "trigger_level":2325.0,"signal_price":2325.0,
        "stop_anchor":2301.4,"stop_price":2294.0,
        "activity_confirmed":True,"senior_level_break":True,
        "game_changer_extreme":True,
        "intrabar_volatility_shock":{
            "eligible":True,"severity":"GAME_CHANGER_EXTREME",
            "exceeds":{"1m":True,"5m":True,"1h":True},
            "ratios":{"1m":6.0,"5m":2.5,"1h":1.1},
        },
    }
    ctx={"status":"OK","event":event}
    return {
        "asset":"MOEX","horizon":"1m","price":price,
        "research_decision":direction,"decision":direction,"signal_tier":tier,
        "regime":"UPTREND_HIGH_VOL" if direction=="LONG" else "DOWNTREND_HIGH_VOL",
        "timeframe_entry_context":ctx,"trend_entry_context":ctx,
        "trend_impulse":{
            "phase":"IMPULSE_TREND","direction":direction,
            "volatility_expansion":False if contraction else True,
        },
        "intraday_structure":{
            "direction":direction,
            "volatility_expansion":False if contraction else True,
        },
        "trade_plan":{
            "timeframe_entry_context":ctx,
            "trade_integrity":{},
            "multi_tf_level_context":{
                "horizon":"1h","target_noise_floor_pct":0.001,
                "target_ladder":[
                    {"timeframe":"5m","price":2410.0 if direction=="LONG" else 2390.0},
                    {"timeframe":"1h","price":2422.0 if direction=="LONG" else 2378.0},
                    {"timeframe":"4h","price":2440.0 if direction=="LONG" else 2360.0},
                ],
            },
        },
    }


class ImpulseDynamicTargetTrainingTests(unittest.TestCase):
    def position(self, direction="LONG"):
        row=impulse_row(direction)
        impulse=VTDE.event_impulse_assess(row,direction)
        return {
            "portfolio_name":"Champion","direction":direction,"units":1.0,
            "avg_entry_price":2325.0,
            "payload":{
                "entry_event_impulse":impulse,
                "r46_trend_hold_active":False,
                "active_target_stage":0,
                # Deliberately stale entry-time targets. They must never reactivate.
                "active_target_ladder":[
                    {"price":2395.0 if direction=="LONG" else 2405.0,
                     "fraction":0.5,"kind":"TP1"},
                    {"price":2400.0 if direction=="LONG" else 2400.0,
                     "fraction":0.5,"kind":"TP2"},
                ],
            },
        }

    def test_exhaustion_alone_does_not_reactivate_old_tp(self):
        z=self.position("LONG")
        out=VSL.target_reduction(z,2400.0,2400.0,"2026-10-10T07:20:00Z")
        self.assertFalse(out["eligible"],out)
        self.assertTrue(out["deferred"])
        self.assertEqual(out["reason"],"EVENT_IMPULSE_EXHAUSTED_FRESH_TARGETS_REQUIRED")

    def test_fresh_tp_requires_both_exhaustion_and_lower_volatility(self):
        z=self.position("LONG"); row=impulse_row("LONG")
        no_exhaust=VSL.rebuild_targets_after_impulse(
            z,row,2400.0,"2026-10-10T07:21:00Z",
            exhaustion_confirmed=False,volatility_contracted=True)
        self.assertFalse(no_exhaust["ready"])
        no_contraction=VSL.rebuild_targets_after_impulse(
            z,row,2400.0,"2026-10-10T07:22:00Z",
            exhaustion_confirmed=True,volatility_contracted=False)
        self.assertFalse(no_contraction["ready"])
        ready=VSL.rebuild_targets_after_impulse(
            z,row,2400.0,"2026-10-10T07:23:00Z",
            exhaustion_confirmed=True,volatility_contracted=True)
        self.assertTrue(ready["ready"],ready)
        self.assertEqual([x["price"] for x in ready["ladder"]],[2410.0,2422.0])
        self.assertNotIn(2395.0,[x["price"] for x in ready["ladder"]])

    def test_reacceleration_switches_fixed_tp_off_again(self):
        z=self.position("LONG"); row=impulse_row("LONG")
        ready=VSL.rebuild_targets_after_impulse(
            z,row,2400.0,"2026-10-10T07:23:00Z",
            exhaustion_confirmed=True,volatility_contracted=True)
        z["payload"].update(ready["patch"])
        invalid=VSL.invalidate_post_impulse_targets_on_reacceleration(
            z,"2026-10-10T07:24:00Z")
        self.assertTrue(invalid["invalidated"])
        self.assertEqual(invalid["patch"]["active_target_ladder"],[])
        self.assertEqual(invalid["patch"]["post_impulse_target_rebuild_status"],
                         "STALE_REACCELERATION")


class PrematureTakeProfitContinuationTests(unittest.TestCase):
    def prior(self, reason):
        c=MagicMock()
        c.execute.return_value.fetchone.return_value={
            "trade_id":"prior-trade",
            "payload":{"r66_event_id":"IMPULSE_CASE_1","exit_reason":reason},
        }
        return c

    def test_long_old_tp_then_super_long_active_impulse_reopens_same_event(self):
        row=impulse_row("LONG")
        out=VPR._r72_event_reentry_gate(
            self.prior("TAKE_PROFIT_STRUCTURAL_FINAL"),
            "Champion","MOEX","LONG",
            row["timeframe_entry_context"]["event"],row=row)
        self.assertTrue(out["eligible"],out)
        self.assertEqual(out["reason"],
                         "R72_PREMATURE_TP_IMPULSE_CONTINUATION_REENTRY")
        self.assertTrue(out["same_event_reuse"])

    def test_short_is_symmetric(self):
        row=impulse_row("SHORT")
        out=VPR._r72_event_reentry_gate(
            self.prior("TAKE_PROFIT_STRUCTURAL_FINAL"),
            "Champion","MOEX","SHORT",
            row["timeframe_entry_context"]["event"],row=row)
        self.assertTrue(out["eligible"],out)

    def test_old_tp_does_not_reopen_without_super_signal(self):
        row=impulse_row("LONG",super_signal=False)
        out=VPR._r72_event_reentry_gate(
            self.prior("TAKE_PROFIT_STRUCTURAL_FINAL"),
            "Champion","MOEX","LONG",
            row["timeframe_entry_context"]["event"],row=row)
        self.assertFalse(out["eligible"],out)

    def test_old_tp_does_not_reopen_after_volatility_contraction(self):
        row=impulse_row("LONG",contraction=True)
        out=VPR._r72_event_reentry_gate(
            self.prior("TAKE_PROFIT_STRUCTURAL_FINAL"),
            "Champion","MOEX","LONG",
            row["timeframe_entry_context"]["event"],row=row)
        self.assertFalse(out["eligible"],out)
        self.assertEqual(out["impulse_continuation"]["reason"],
                         "R72_VOLATILITY_ALREADY_CONTRACTED")

    def test_stop_never_gets_same_event_tp_reentry_exception(self):
        row=impulse_row("LONG")
        out=VPR._r72_event_reentry_gate(
            self.prior("STOP"),"Champion","MOEX","LONG",
            row["timeframe_entry_context"]["event"],row=row)
        self.assertFalse(out["eligible"],out)
        self.assertEqual(out["reason"],"R72_EVENT_ALREADY_TRADED")


class OwnerTeachingTests(unittest.TestCase):
    def test_dynamic_tp_training_is_p0_and_needs_no_confirmation(self):
        snap=VUT.dynamic_tp_policy_snapshot()
        self.assertEqual(snap["priority"],"P0_HIGHEST")
        self.assertFalse(snap["parameter_validation"]["additional_proof_required"])
        self.assertIn("premature_tp",snap["requirements"])
        self.assertTrue(snap["execution_policy"]["premature_tp_impulse_reentry"])
        self.assertTrue(snap["execution_policy"]["old_target_reactivation_forbidden"])


if __name__=="__main__":
    unittest.main()
