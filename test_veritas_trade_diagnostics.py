"""Outcome-neutral diagnosis using real causal event and quote-witness builders."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock, patch

import veritas_observation_path as PATH
import veritas_price_source as SOURCE
import veritas_timeframe_structure as STRUCTURE
import veritas_trade_diagnostics as DIAG
from test_veritas_timeframe_structure import example


FEED = {"source": "TEST_NATIVE", "contract_id": "NQZ6", "source_gate_pass": True}
IDENTITY = SOURCE.identity("NQ", FEED)


def closed_trade(timeframe="5m", *, short=False, price_scale=1., volatility_scale=1.,
                 favorable_r=.02, final_r=-1.):
    rows, now = example(timeframe, short=short)
    for row in rows:
        for field in ("open", "high", "low", "close"):
            row[field] = (100. + (row[field]-100.) * volatility_scale) * price_scale
    context = STRUCTURE.build_context(rows, timeframe, now, asset="NQ", source_identity=IDENTITY)
    event = deepcopy(context["event"])
    if not event:
        raise AssertionError(context)
    sign = -1. if short else 1.
    entered = now+10
    entry, stop = event["signal_price"], event["stop_price"]
    risk = sign * (entry-stop)
    last = entry + sign * final_r * risk
    stamp = lambda at: datetime.fromtimestamp(at, timezone.utc).isoformat()
    trade = {"trade_id":"DIAG_FIXTURE", "portfolio_name":"Champion", "asset":"NQ",
             "direction":event["direction"], "horizon":timeframe, "status":"OPEN",
             "opened_at":stamp(entered), "avg_entry_price":entry, "avg_exit_price":last,
             "gross_pnl_rub":-100., "fees_rub":4., "funding_rub":1., "net_pnl_rub":-105.,
             "max_fraction":.5, "payload":{
                 "entry_event_snapshot":event, "idea_event_id":event["event_id"],
                 "r66_event_id":event["event_id"], "canonical_setup_id":event["event_id"],
                 "data_integrity_status":"OK", "learning_eligible":True,
                 "price_source_lock":deepcopy(IDENTITY),
                 "entry_execution_source_identity":deepcopy(IDENTITY),
                 "last_exit_source_identity":deepcopy(IDENTITY),
                 "execution_timeframe":timeframe, "execution_horizon":timeframe,
                 "atr_timeframe":timeframe, "stop_timeframe":timeframe, "target_timeframe":timeframe,
                 "initial_stop_price":stop, "entry_atr":event["atr"],
                 "entry_execution_model":{"fill_price":entry, "reference_price":entry,
                     "asset":"NQ", "side":"SELL_SHORT" if short else "BUY"},
                 "last_exit_execution_model":{"fill_price":last},
                 "expected_move_pct":sign*(event["target_price"]-entry)/entry,
                 "exit_reason":"STOP", "mfe_pct":.01, "mae_pct":-1.,
                 "entry_nav_rub":1000000., "opening_fraction":.5, "telemetry_completeness":1.,
             }}
    points = ((entered, entry), (entered+15, entry + sign*favorable_r*risk), (entered+30, last))
    for i, (at, price) in enumerate(points):
        quote = dict(FEED, price=price, observed_at=stamp(at))
        trade["payload"]["observation_path"] = PATH.observe(trade, quote, stamp(at), at_entry=i == 0)
    trade.update(status="CLOSED", closed_at=stamp(entered+30))
    if not PATH.assessment(trade)["eligible"]:
        raise AssertionError(PATH.assessment(trade))
    return trade


class TradeDiagnosticsTests(unittest.TestCase):
    def test_equivalent_m5_h1_price_and_volatility_scales_share_r_and_atr_diagnosis(self):
        for timeframe in ("5m", "1h"):
            for short in (False, True):
                for scale, volatility in ((1., .1), (1., 2.), (300., 1.)):
                    with self.subTest(timeframe=timeframe, short=short, scale=scale, volatility=volatility):
                        trade = closed_trade(timeframe, short=short, price_scale=scale, volatility_scale=volatility)
                        before = deepcopy(trade)
                        result = DIAG.diagnose(trade)
                        self.assertEqual(result["status"], "VERIFIED_RULE_OUTCOME", result)
                        self.assertEqual(result["primary_attribution"], "VALID_STRUCTURAL_STOP_LOSS")
                        self.assertTrue(result["strategy_quality_eligible"])
                        self.assertFalse(result["directional_error"])
                        self.assertAlmostEqual(result["normalization"]["mfe_r"], .02)
                        self.assertAlmostEqual(result["normalization"]["mae_r"], -1.)
                        self.assertAlmostEqual(result["normalization"]["initial_risk_atr"], 2.3575/1.05)
                        timing = result["normalization"]["timing"]
                        self.assertEqual(timing["delay_seconds"], 10.)
                        self.assertAlmostEqual(timing["delay_bars"], 10./STRUCTURE.TIMEFRAMES[timeframe])
                        self.assertFalse(result["subsequent_management_rules_verified"])
                        self.assertEqual(trade, before)

    def test_valid_stop_after_one_r_profit_is_only_a_management_hypothesis(self):
        result = DIAG.diagnose(closed_trade(favorable_r=1.2))
        self.assertEqual(result["primary_attribution"], "VALID_STRUCTURAL_STOP_LOSS")
        self.assertEqual(result["violations"], [])
        self.assertIn("PROFIT_CAPTURE_HYPOTHESIS", result["attributions"])
        self.assertFalse(result["hypotheses"][0]["automatic_action"])
        self.assertTrue(result["learning_eligible"])
        self.assertFalse(result["directional_error"])
        self.assertFalse(result["parameter_changes_applied"])

    def test_original_stop_and_first_fill_survive_trailing_adds_and_risk_budget_caps(self):
        trade = closed_trade(favorable_r=1.2)
        before = DIAG.diagnose(trade)["normalization"]
        trade["avg_entry_price"] *= 1.2
        trade["stop_price"] = trade["payload"]["entry_execution_model"]["fill_price"]-.001
        trade["payload"].update(stop_price=trade["stop_price"], last_stop_price=trade["stop_price"],
            last_entry_execution_model={"fill_price":99999.}, initial_risk_budget_rub=1.,
            r55_lifetime_mfe_pct=777., r55_lifetime_mae_pct=-999.)
        after = DIAG.diagnose(trade)
        self.assertEqual(after["normalization"], before)
        self.assertIsNone(after["normalization"]["monetary_net_r"])
        self.assertEqual(after["primary_attribution"], "VALID_STRUCTURAL_STOP_LOSS")

    def test_wrong_timeframe_and_premature_entry_have_explicit_rule_evidence(self):
        wrong = closed_trade()
        wrong["payload"]["entry_event_snapshot"]["atr_timeframe"] = "1h"
        result = DIAG.diagnose(wrong)
        self.assertEqual(result["status"], "RULE_VIOLATION")
        self.assertIn("ATR_TIMEFRAME_MISMATCH", result["attributions"])
        self.assertIsNone(result["normalization"]["entry_atr"])
        self.assertFalse(result["strategy_quality_eligible"])
        early = closed_trade()
        early["opened_at"] = datetime.fromtimestamp(
            early["payload"]["entry_event_snapshot"]["confirmed_at"]-1, timezone.utc).isoformat()
        result = DIAG.diagnose(early)
        self.assertEqual(result["status"], "RULE_VIOLATION")
        self.assertTrue(result["rule_evidence_eligible"])
        self.assertIn("ENTRY_BEFORE_EVENT_CONFIRMATION", result["attributions"])
        self.assertEqual(result["normalization"]["timing"]["delay_seconds"], -1.)
        self.assertFalse(result["directional_error"])

    def test_stop_discrepancy_is_proved_by_anchor_and_immutable_buffer(self):
        trade = closed_trade()
        trade["payload"]["initial_stop_price"] += .5
        result = DIAG.diagnose(trade)
        self.assertEqual(result["status"], "RULE_VIOLATION")
        self.assertIn("INITIAL_STOP_OUTSIDE_RECORDED_RULE", result["attributions"])
        required = next(v["required"] for v in result["violations"]
                        if v["code"] == "INITIAL_STOP_OUTSIDE_RECORDED_RULE")
        self.assertEqual(required, trade["payload"]["entry_event_snapshot"]["stop_price"])

    def test_missing_or_unverified_proof_never_becomes_normal_loss(self):
        changes = (
            lambda p:p.pop("entry_event_snapshot"),
            lambda p:p["entry_event_snapshot"].pop("confirmed_at"),
            lambda p:p["entry_event_snapshot"]["policy"].pop("stop_buffer_atr"),
            lambda p:p.pop("observation_path"),
            lambda p:p["observation_path"].update(started_at_entry=False),
            lambda p:p["observation_path"].update(gap_count=1, max_gap_seconds=100),
            lambda p:p.update(recovered=True),
            lambda p:p.update(data_integrity_status="UNKNOWN"),
            lambda p:p["last_exit_source_identity"].update(contract_id="FOREIGN_CONTRACT"),
        )
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                trade = closed_trade(favorable_r=1.2)
                change(trade["payload"])
                result = DIAG.diagnose(trade)
                self.assertEqual(result["status"], "UNVERIFIED", result)
                self.assertFalse(result["learning_eligible"])
                self.assertEqual(result["hypotheses"], [])

    def test_missing_original_risk_is_not_recovered_from_later_stop_or_nav_budget(self):
        trade = closed_trade()
        p = trade["payload"]
        p.pop("initial_stop_price")
        p.update(stop_price=100., last_stop_price=100., initial_risk_budget_rub=500., entry_nav_rub=1000000.)
        result = DIAG.diagnose(trade)
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertIsNone(result["normalization"]["initial_risk_price"])
        self.assertIsNone(result["normalization"]["mfe_r"])
        trade = closed_trade()
        trade["payload"]["entry_execution_model"].pop("fill_price")
        result = DIAG.diagnose(trade)
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertIsNone(result["normalization"]["original_entry_price"])

    def test_nonfinite_or_boolean_numbers_do_not_certify_risk_or_atr(self):
        for value in (True, False, float("nan"), float("inf"), -1., 0.):
            for field in ("initial_stop_price", "entry_atr"):
                with self.subTest(value=value, field=field):
                    trade = closed_trade()
                    trade["payload"][field] = value
                    result = DIAG.diagnose(trade)
                    self.assertEqual(result["status"], "UNVERIFIED")
                    self.assertFalse(result["learning_eligible"])

    def test_contamination_or_missing_execution_cannot_prove_a_stop_violation(self):
        changes = (
            lambda t:t["payload"].update(data_integrity_status="CONTRACT_IDENTITY_CHANGED"),
            lambda t:t["payload"].update(data_integrity_status="UNKNOWN"),
            lambda t:t["payload"].update(recovered=True),
            lambda t:t["payload"]["entry_execution_model"].pop("fill_price"),
            lambda t:t.pop("opened_at"),
        )
        for i, change in enumerate(changes):
            with self.subTest(i=i):
                trade = closed_trade()
                trade["payload"]["initial_stop_price"] += .5
                change(trade)
                result = DIAG.diagnose(trade)
                self.assertEqual(result["status"], "UNVERIFIED", result)
                self.assertFalse(result["rule_evidence_eligible"])
                self.assertEqual(result["violations"], [])

    def test_unknown_spent_state_and_invalid_policy_do_not_certify_normal_trade(self):
        changes = (
            lambda e:e.pop("spent"),
            lambda e:e.update(spent="false"),
            lambda e:e.update(spent=True, spent_at=None),
            lambda e:e.update(spent=True, spent_at=e["confirmed_at"]-1),
            lambda e:e.update(spent=False, spent_at=e["confirmed_at"]),
            lambda e:e["policy"].update(stop_buffer_atr=True),
            lambda e:e["policy"].update(stop_buffer_atr=float("nan")),
            lambda e:e["policy"].update(pivot_left=1.2),
            lambda e:e["policy"].update(atr_period=0),
        )
        for i, change in enumerate(changes):
            with self.subTest(i=i):
                trade = closed_trade()
                change(trade["payload"]["entry_event_snapshot"])
                result = DIAG.diagnose(trade)
                self.assertEqual(result["status"], "UNVERIFIED", result)
                self.assertFalse(result["strategy_quality_eligible"])
                self.assertNotIn("ENTRY_AFTER_EVENT_SPENT", result["attributions"])

    def test_spent_timestamp_must_be_known_before_actual_entry(self):
        trade = closed_trade()
        e = trade["payload"]["entry_event_snapshot"]
        e.update(spent=True, spent_at=e["confirmed_at"]+1)
        result = DIAG.diagnose(trade)
        self.assertEqual(result["status"], "RULE_VIOLATION")
        self.assertIn("ENTRY_AFTER_EVENT_SPENT", result["attributions"])
        e["spent_at"] = e["confirmed_at"]+1000
        result = DIAG.diagnose(trade)
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertNotIn("ENTRY_AFTER_EVENT_SPENT", result["attributions"])

    def test_booked_cost_drag_is_observed_without_changing_cost_policy(self):
        trade = closed_trade(final_r=.2)
        trade.update(gross_pnl_rub=2., fees_rub=4., funding_rub=1., net_pnl_rub=-3.)
        result = DIAG.diagnose(trade)
        self.assertIn("COST_DRAG", result["attributions"])
        self.assertFalse(result["directional_error"])
        self.assertEqual(result["outcome"], "LOSS")

    def test_unknown_finances_and_partial_exit_price_are_not_fabricated_pnl_r(self):
        trade = closed_trade()
        trade["net_pnl_rub"] = None
        result = DIAG.diagnose(trade)
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertEqual(result["outcome"], "UNKNOWN")
        trade = closed_trade()
        trade["avg_exit_price"] = 99999.
        result = DIAG.diagnose(trade)
        self.assertAlmostEqual(result["normalization"]["final_exit_r"], -1.)
        self.assertEqual(result["normalization"]["final_exit_basis"], "LAST_EXIT_FILL_ONLY_NOT_TOTAL_PNL")
        self.assertIsNone(result["normalization"]["monetary_net_r"])


class PortfolioDiagnosticsIntegrationTests(unittest.TestCase):
    def test_real_episode_wrapper_preserves_losses_and_does_not_penalize_direction(self):
        import veritas_portfolio as PORTFOLIO
        trade = closed_trade()
        before = deepcopy(trade)
        result = PORTFOLIO._v90r29_episode_from_trade(trade)
        self.assertTrue(result["learning_eligible"])
        self.assertEqual(result["primary_attribution"], "VALID_STRUCTURAL_STOP_LOSS")
        self.assertEqual(result["net_pnl_rub"], -105.)
        self.assertEqual(result["payload"]["diagnostics_version"], DIAG.VERSION)
        self.assertAlmostEqual(result["payload"]["trade_diagnostics"]["normalization"]["mae_r"], -1.)
        rows = [dict(result, trade_id="T"+str(i)) for i in range(8)]
        profile = PORTFOLIO._v90r29_build_profiles(rows)[("HORIZON", "LONG", "5m")]
        self.assertEqual(profile["n"], 8)
        self.assertEqual(profile["wins"], 0)
        self.assertEqual(profile["avg_net_pnl_rub"], -105.)
        self.assertEqual(profile["entry_error_rate"], 0.)
        self.assertEqual(profile["stop_error_rate"], 0.)
        self.assertEqual(profile["valid_structural_stop_loss_rate"], 1.)
        self.assertEqual(profile["entry_size_multiplier"], 1.)
        self.assertEqual(trade, before)

    def test_closed_trade_view_replaces_old_outcome_based_labels_using_original_proof(self):
        import veritas_portfolio as PORTFOLIO
        valid, legacy = closed_trade(), closed_trade()
        valid["payload"].update(learning_label="RIGHT_DIRECTION_STOP_ERROR", learning_conclusion="OLD_STOP_VERDICT")
        legacy["trade_id"] = "LEGACY"
        legacy["payload"].pop("observation_path")
        legacy["payload"].update(learning_label="GOOD_EXECUTION", learning_conclusion="OLD_GOOD_VERDICT")
        for row in (valid, legacy):
            row["learning_evidence_hash"] = "original-row-evidence-hash"
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.execute.return_value.fetchall.return_value = [valid, legacy]
        with patch.object(PORTFOLIO, "ensure_schema"):
            rows = PORTFOLIO._v90j_load_closed(lambda:connection, limit=50)
        self.assertEqual(rows[0]["learning_label"], "VALID_STRUCTURAL_STOP_LOSS")
        self.assertNotIn("OLD_STOP_VERDICT", rows[0]["learning_conclusion"])
        self.assertTrue(rows[0]["learning_eligible"])
        self.assertEqual(rows[1]["learning_label"], "UNVERIFIED_TRADE_EVIDENCE")
        self.assertFalse(rows[1]["learning_eligible"])
        self.assertEqual(rows[0]["net_pnl_rub"], valid["net_pnl_rub"])
        self.assertEqual(valid["payload"]["learning_label"], "RIGHT_DIRECTION_STOP_ERROR")

    def test_legacy_label_helper_no_longer_declares_stop_or_direction_errors(self):
        import veritas_portfolio as PORTFOLIO
        for mfe in (0., .01, .20, 10.):
            label = PORTFOLIO._v90j_learning_label(-100., -1., mfe, -1., mfe, "STOP")
            self.assertEqual(label, "LOSS_OBSERVED_RULES_UNVERIFIED")
            self.assertNotIn("ошиб", PORTFOLIO._v90j_learning_conclusion(label, {}))


if __name__ == "__main__":
    unittest.main()
