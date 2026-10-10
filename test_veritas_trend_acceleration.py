import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import veritas_canonical_constitution as CTC
import veritas_stop_risk as VSR
import veritas_structural_lifecycle as VSL
import veritas_position_guard as VPG
import veritas_price_source as VPS
import veritas_paper_entry as VPE
import veritas_trend_entry as VTE
import veritas_trend_day_efficiency as VTDE
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


def extreme_trend_row(*, cross_regime=None):
    row=trend_row(state="CONFIRMED_TREND",horizon="5m",tier="SUPER_LONG",
                  evidence=5,expected=0.012,supporting=["1h","4h"],mid=True)
    row["trend_entry_context"]["confirmation_30m_trend"]={
        "confirmed":True,"direction":"LONG","timeframe":"30m"
    }
    row["trade_plan"]["trend_entry_context"]=row["trend_entry_context"]
    row["trend_impulse"]={
        "phase":"IMPULSE_TREND","direction":"LONG","impulse_score":0.90,
        "session_efficiency":0.80,"session_persistence":0.82,
        "horizon_consensus_count":4,"horizon_consensus_score":0.88,
        "structure_score":0.86,"entry_quality":"FRESH_BREAKOUT",
    }
    row["intraday_structure"]={
        "direction":"LONG","score":0.88,"session_efficiency":0.80,
        "session_persistence":0.82,"relative_volume":1.45,
    }
    row["horizon_structure"]["score"]=0.90
    if cross_regime:
        row["cross_asset_shadow"]={"regime":cross_regime}
    return row


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
        self.assertAlmostEqual(adjusted["max_fraction"], 1.00)
        self.assertAlmostEqual(adjusted["max_gross"], 1.00)

    def test_extreme_impulse_can_earn_full_nav_in_normal_state(self):
        policy=dict(CTC.runtime_portfolio_policy("Impulse"))
        row={
            "_trend_acceleration":{
                "active":True,"stage":"EXTREME_CONFIRMED",
                "temporary_max_fraction":1.00,"temporary_max_gross":1.00,
            },
            "_canonical_admission":{"risk_governor":{
                "state":"NORMAL","new_risk":True,"max_gross":0.50,
            }},
        }
        adjusted,_,_=VPE._policy_with_acceleration_caps(policy,row)
        self.assertAlmostEqual(adjusted["max_fraction"],1.00)
        self.assertAlmostEqual(adjusted["max_gross"],1.00)

    def test_impulse_caution_state_keeps_drawdown_governor_authority(self):
        policy = dict(CTC.runtime_portfolio_policy("Impulse"))
        row = {
            "_trend_acceleration": {"active": True, "stage": "SENIOR_CONFIRMED"},
            "_canonical_admission": {"risk_governor": {
                "state": "CAUTION", "new_risk": True, "max_gross": 0.45
            }},
        }
        adjusted, _, _ = VPE._policy_with_acceleration_caps(policy, row)
        self.assertAlmostEqual(adjusted["max_fraction"], 1.00)
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

    def test_sustained_015pct_lane_can_lock_before_legacy_021_floor(self):
        position,quote,now=self.structural_position_and_quote(100.16)
        first=VPG._structural_mfe_profit_lock(None,object(),position,quote,
                                              now.isoformat(),now)
        position["payload"].update(first["patch"])
        later=now+timedelta(seconds=100)
        confirmed=dict(quote,observed_at=later.isoformat())
        result=VPG._structural_mfe_profit_lock(None,object(),position,confirmed,
                                               later.isoformat(),later)
        self.assertEqual(result["state"],"PROTECTED")
        self.assertIsNotNone(result["lock"])
        self.assertLess(result["lock"]["activation_profit_pct"],0.21)
        self.assertGreater(result["lock"]["projected_net_profit_at_stop_rub"],0)

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


class TrendDayRunnerTests(unittest.TestCase):
    def test_protected_impulse_trend_keeps_85pct_runner(self):
        position={"portfolio_name":"Aggressive"}
        payload={
            "r_accel_mfe_profit_lock_active":True,
            "last_trend_day_efficiency":{
                "eligible":True,"phase":"IMPULSE_TREND","runner_ratio":0.85
            },
        }
        self.assertAlmostEqual(VTDE.protected_runner_ratio(position,payload),0.85)

    def test_structural_tp1_uses_protected_85pct_runner(self):
        position={
            "portfolio_name":"Aggressive","direction":"LONG","units":0.5,
            "payload":{
                "active_target_stage":0,
                "r_accel_mfe_profit_lock_active":True,
                "last_trend_day_efficiency":{
                    "eligible":True,"phase":"IMPULSE_TREND","runner_ratio":0.85,
                },
            },
        }
        ladder=[
            {"price":110.0,"fraction":0.5,"kind":"TP1"},
            {"price":120.0,"fraction":0.5,"kind":"TP2"},
        ]
        with patch.object(VSL,"active_ladder",return_value=ladder):
            result=VSL.target_reduction(position,110.0,100.0,"2026-10-09T11:31:00Z")
        self.assertTrue(result["eligible"])
        self.assertAlmostEqual(result["target_fraction"],0.5*110.0/100.0*0.85)
        self.assertEqual(result["patch"]["profit_exit_policy"],"ADAPTIVE_PROTECTED_TREND_RUNNER")
        self.assertAlmostEqual(result["patch"]["adaptive_runner_ratio"],0.85)

    def test_unprotected_or_currency_keeps_default_half_runner(self):
        td={"eligible":True,"phase":"IMPULSE_TREND","runner_ratio":0.85}
        self.assertAlmostEqual(VTDE.protected_runner_ratio(
            {"portfolio_name":"Aggressive"},{"last_trend_day_efficiency":td}),0.50)
        self.assertAlmostEqual(VTDE.protected_runner_ratio(
            {"portfolio_name":"Currency"},
            {"r_accel_mfe_profit_lock_active":True,"last_trend_day_efficiency":td}),0.50)


def moex_event_impulse_row(*, mid=False, supporting=None, news=False, tier="SUPER_LONG"):
    row=trend_row(state="BUILDING_TREND",horizon="5m",tier=tier,
                  evidence=5,expected=0.00189,supporting=supporting,mid=mid)
    row.update({
        "asset":"MOEX","price":2341.20,"regime":"UPTREND_HIGH_VOL",
        "horizon_structure":{
            "direction":"LONG","score":0.955085,"state":"BUILDING_TREND",
        },
        "intraday_structure":{
            "direction":"LONG","score":0.955085,
            "volume_confirmed":True,"activity_confirmed":True,
            "relative_volume":1.80,"volatility_expansion":True,
        },
        "event_shadow_score":0.80 if news else 0.0,
    })
    event={
        "event_id":"STF_8aefee60345acc7c0ea3d4a3",
        "event_type":"SAME_TIMEFRAME_STRUCTURAL_BREAKOUT",
        "direction":"LONG","timeframe":"5m","trigger_timeframe":"5m",
        "structural_timeframe":"5m","stop_timeframe":"5m","atr_timeframe":"5m",
        "trigger_level":2323.95,"signal_price":2330.12,
        "stop_anchor":2322.58,"stop_price":2322.48295,
        # Deliberately stale/spent target from the actual 2026-10-09 incident.
        "target_price":2326.8841,"spent":True,
        "spent_reason":"SAME_TF_TARGET_ALREADY_REACHED",
        "target_progress":1.0,
    }
    row["timeframe_entry_context"]={"status":"OK","event":event}
    row["trade_plan"]["timeframe_entry_context"]=row["timeframe_entry_context"]
    row["trade_plan"]["trade_integrity"]={}
    return row


class EventImpulseP0Tests(unittest.TestCase):
    def test_first_directional_long_can_enter_before_super_confirmation(self):
        row=moex_event_impulse_row(tier="LONG")
        impulse=VTDE.event_impulse_assess(row,"LONG")
        self.assertTrue(impulse["eligible"],impulse)
        self.assertEqual(impulse["signal_tier"],"LONG")

    def test_actual_moex_shape_is_event_impulse_even_with_spent_old_target(self):
        row=moex_event_impulse_row()
        impulse=VTDE.event_impulse_assess(row,"LONG")
        self.assertTrue(impulse["eligible"],impulse)
        self.assertEqual(impulse["owner_priority"],"P0_HIGHEST")
        self.assertTrue(impulse["activity_confirmed"])
        self.assertTrue(impulse["volatility_expansion"])
        self.assertFalse(impulse["entry_requires_news"])
        result=VSL._trend_acceleration_state(row,"LONG",{"mode":"CORE"})
        self.assertTrue(result["active"],result)
        self.assertEqual(result["stage"],"EVENT_FAST_CONFIRMED")
        self.assertAlmostEqual(result["target_fraction"],0.50)
        self.assertEqual(result["reason"],"OWNER_P0_EVENT_IMPULSE_ACCELERATION")

    def test_event_impulse_scales_to_max_on_senior_confirmation(self):
        row=moex_event_impulse_row(supporting=["1h","4h"])
        core=VSL._trend_acceleration_state(row,"LONG",{"mode":"CORE"})
        aggressive=VSL._trend_acceleration_state(row,"LONG",{"mode":"AGGRESSIVE"})
        self.assertEqual(core["stage"],"EVENT_MAX_CONFIRMED")
        self.assertAlmostEqual(core["target_fraction"],1.00)
        self.assertAlmostEqual(aggressive["target_fraction"],5.00)
        self.assertAlmostEqual(aggressive["temporary_max_gross"],5.00)

    def test_news_confirms_hold_and_can_complete_scale_but_is_not_entry_gate(self):
        first=VTDE.event_impulse_assess(moex_event_impulse_row(news=False),"LONG")
        self.assertTrue(first["eligible"])
        self.assertFalse(first["news_confirmed"])
        row=moex_event_impulse_row(mid=True,news=True)
        scaled=VSL._trend_acceleration_state(row,"LONG",{"mode":"CORE"})
        self.assertEqual(scaled["stage"],"EVENT_MAX_CONFIRMED")
        self.assertAlmostEqual(scaled["target_fraction"],1.00)
        self.assertTrue(scaled["event_impulse"]["news_confirmed"])

    def test_fixed_tp_is_deferred_until_impulse_exhaustion(self):
        impulse=VTDE.event_impulse_assess(moex_event_impulse_row(),"LONG")
        position={
            "portfolio_name":"Champion","direction":"LONG","units":1.0,
            "payload":{
                "entry_event_impulse":impulse,
                "active_target_stage":0,
            },
        }
        blocked=VSL.target_reduction(position,2400.0,2400.0,"2026-10-09T20:36:00Z")
        self.assertFalse(blocked["eligible"],blocked)
        self.assertTrue(blocked["deferred"])
        self.assertEqual(blocked["reason"],"OWNER_P0_EVENT_IMPULSE_FIXED_TP_DEFERRED")
        self.assertEqual(blocked["target_reference_mode"],"HIGHER_TIMEFRAME_HIGHS_AND_ZONES")
        position["payload"]["r46_trend_hold_active"]=False
        ladder=[
            {"price":2395.0,"fraction":0.5,"kind":"TP1"},
            {"price":2400.0,"fraction":0.5,"kind":"TP2"},
        ]
        with patch.object(VSL,"active_ladder",return_value=ladder):
            released=VSL.target_reduction(position,2400.0,2400.0,"2026-10-09T20:47:00Z")
        self.assertTrue(released["eligible"],released)

    def test_owner_p0_teaching_requires_no_additional_proof(self):
        snap=VUT.event_impulse_policy_snapshot()
        self.assertEqual(snap["priority"],"P0_HIGHEST")
        self.assertEqual(snap["status"],"ACTIVE_OWNER_P0_CANONICAL")
        self.assertFalse(snap["parameter_validation"]["additional_proof_required"])
        self.assertEqual(snap["case_anchor"]["asset"],"MOEX")
        self.assertEqual(snap["case_anchor"]["owner_expected_entry_time"],"21:46")


class TrendAccelerationTests(unittest.TestCase):
    def test_fast_confirmation_earns_first_standard_scale(self):
        result = VSL._trend_acceleration_state(
            trend_row(), "LONG", {"mode": "CORE"}
        )
        self.assertTrue(result["active"])
        self.assertEqual(result["stage"], "FAST_CONFIRMED")
        self.assertAlmostEqual(result["target_fraction"], 0.50)

    def test_mid_confirmation_earns_75pct_standard_target(self):
        row = trend_row(state="BUILDING_TREND", tier="LONG", mid=True)
        result = VSL._trend_acceleration_state(row, "LONG", {"mode": "CORE"})
        self.assertTrue(result["active"])
        self.assertEqual(result["stage"], "MID_CONFIRMED")
        self.assertAlmostEqual(result["target_fraction"], 0.75)

    def test_senior_confirmation_earns_full_standard_target(self):
        row = trend_row(supporting=["1h"])
        result = VSL._trend_acceleration_state(row, "LONG", {"mode": "CORE"})
        self.assertEqual(result["stage"], "SENIOR_CONFIRMED")
        self.assertAlmostEqual(result["target_fraction"], 1.00)

    def test_aggressive_senior_target_is_500pct(self):
        row = trend_row(supporting=["1h", "4h"])
        result = VSL._trend_acceleration_state(
            row, "LONG", {"mode": "AGGRESSIVE"}
        )
        self.assertEqual(result["stage"], "SENIOR_CONFIRMED")
        self.assertAlmostEqual(result["target_fraction"], 5.00)

    def test_extreme_trend_day_earns_full_standard_allocation(self):
        row=extreme_trend_row()
        result=VSL._trend_acceleration_state(row,"LONG",{"mode":"CORE"})
        self.assertTrue(result["active"])
        self.assertEqual(result["stage"],"EXTREME_CONFIRMED")
        self.assertAlmostEqual(result["target_fraction"],1.00)
        self.assertTrue(result["trend_day_efficiency"]["eligible"])

    def test_extreme_trend_day_earns_500pct_aggressive_allocation(self):
        row=extreme_trend_row()
        result=VSL._trend_acceleration_state(row,"LONG",{"mode":"AGGRESSIVE"})
        self.assertEqual(result["stage"],"EXTREME_CONFIRMED")
        self.assertAlmostEqual(result["target_fraction"],5.00)
        self.assertAlmostEqual(result["temporary_max_gross"],5.00)

    def test_cross_asset_context_is_telemetry_only(self):
        aligned=VTDE.assess(extreme_trend_row(cross_regime="RISK_ON"),"LONG",{"mode":"CORE"},
                            mid=True,senior=True,evidence=5,expected=.012,progress=.20)
        conflict=VTDE.assess(extreme_trend_row(cross_regime="RISK_OFF"),"LONG",{"mode":"CORE"},
                             mid=True,senior=True,evidence=5,expected=.012,progress=.20)
        self.assertTrue(aligned["eligible"])
        self.assertTrue(conflict["eligible"])
        self.assertAlmostEqual(aligned["score"],conflict["score"])
        self.assertFalse(aligned["cross_asset_size_influence"])
        self.assertEqual(conflict["cross_asset_alignment"],"CONFLICT")

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

    def test_trend_day_refinement_has_separate_immutable_teaching(self):
        snapshot=VUT.trend_day_efficiency_snapshot()
        self.assertEqual(snapshot["teaching_id"],CTC.TREND_DAY_EFFICIENCY_POLICY["teaching_id"])
        self.assertEqual(snapshot["parent_teaching_id"],VUT.ACCELERATION_TEACHING_ID)
        self.assertIn("Aggressive",snapshot["portfolios"])
        self.assertNotIn("Currency",snapshot["portfolios"])

    def test_teaching_snapshot_matches_ctc_policy(self):
        snapshot = VUT.acceleration_policy_snapshot()
        self.assertEqual(snapshot["teaching_id"], CTC.TREND_ACCELERATION_POLICY["teaching_id"])
        self.assertEqual(snapshot["scope"], "PAPER_NON_CURRENCY_PORTFOLIOS")
        self.assertIn("Aggressive", snapshot["portfolios"])
        self.assertNotIn("Currency", snapshot["portfolios"])


if __name__ == "__main__":
    unittest.main()
