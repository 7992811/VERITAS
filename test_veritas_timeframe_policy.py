"""Canonical integration regressions using causal OHLC and real fill economics.

The prices are deterministic test geometry, not market replay evidence. Only
upstream feed/session flags are supplied; signal, structural, cost, sizing and
candidate-routing functions run without replacing their implementations.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

import veritas_canonical_constitution as CTC
import veritas_canonical_runtime as VCR
import veritas_execution as VX
import veritas_price_source as VPS
import veritas_timeframe_policy as TFP
import veritas_timeframe_structure as TS
import veritas_trend_entry as VTE


def structural_row(clock, *, timeframe="5m", asset="NQ", direction="LONG",
                   signal_age=10, width=1., source=None, contract_id=None, price=None):
    """Two known prior pivots, then one confirming close; no later new breakout."""
    step = TS.timeframe_seconds(timeframe)
    signal_at = clock.timestamp() - signal_age
    begin = signal_at - 33*step
    rows = [dict(ts=begin+i*step, open=100., high=100.5, low=99.5,
                 close=100., timeframe=timeframe, volume=100.) for i in range(32)]
    rows[24]["high"] = 101.
    rows[27]["low"] = 99.
    rows.append(dict(ts=begin+32*step, open=100., high=101.3, low=100.,
                     close=101.2, timeframe=timeframe, volume=150.))
    next_open = signal_at
    while next_open+step <= clock.timestamp():
        rows.append(dict(ts=next_open, open=101.2, high=101.4, low=100.8,
                         close=101.2, timeframe=timeframe, volume=100.))
        next_open += step
    if direction == "SHORT":
        for bar in rows:
            bar.update(open=200-bar["open"], close=200-bar["close"],
                       high=200-bar["low"], low=200-bar["high"])
    for bar in rows:
        for key in ("open", "high", "low", "close"):
            bar[key] = 100. + width*(bar[key]-100.)
    if price is not None:
        factor = float(price)/rows[32]["close"]
        for bar in rows:
            for key in ("open", "high", "low", "close"):
                bar[key] *= factor
    provider = source or ("MOEX ISS CNYRUBF" if asset == "CNYRUBF" else "ProFinance NASD100_FUT")
    identity = VPS.identity(asset, {"source":provider, "contract_id":contract_id})
    ctx = TS.build_context(rows, timeframe, clock, asset=asset,
                           source_identity=identity, config=CTC.STRUCTURAL_ENTRY_POLICY)
    event = ctx["event"]
    price = event["signal_price"]
    quote_at = clock.isoformat()
    row = {"asset":asset, "horizon":timeframe, "research_decision":direction,
           "decision":direction, "price":price, "confidence":.90,
           "signal_tier":"SUPER_"+direction, "source":provider,
           "contract_id":contract_id, "source_names":{"primary":provider},
           "source_gate_pass":True, "market_open":True, "direct_sources":1,
           "market_observed_at":quote_at, "paper_eligible":True,
           "horizon_structure":{"direction":direction,"state":"CONFIRMED_TREND","score":.90},
           "independent_evidence_families":5,
           "_local_execution_context":{"same_direction_count":1,"opposite_direction_count":0},
           "timeframe_entry_context":ctx,
           "trade_plan":{"horizon":timeframe, "market_observed_at":quote_at,
                         "entry_price":price, "stop_price":price*(.999 if direction == "LONG" else 1.001),
                         "target_price":price*(3. if direction == "LONG" else .3),
                         "expected_to_stop_ratio":999., "expected_move_pct":1.,
                         "entry_quality":"FRESH_BREAKOUT", "entry_permission":"ENTER_NOW"}}
    return row


def valid_row(asset="NQ", horizon="5m", direction="LONG", price=None, now=None, **kwargs):
    """Reusable real-context fixture for source, portfolio and market tests.

    Optional ``signal_age`` must precede the quote observation being tested.
    Scaling the requested price scales every OHLC/ATR/anchor consistently.
    """
    stamp = TS.timestamp(datetime.now(timezone.utc) if now is None else now)
    if stamp is None:
        raise ValueError("fixture decision time must be an epoch or timezone-aware time")
    clock = datetime.fromtimestamp(stamp, timezone.utc)
    return structural_row(clock, asset=asset, timeframe=horizon, direction=direction,
                          price=price, **kwargs)


class CanonicalSameTimeframeTests(unittest.TestCase):
    def setUp(self):
        # VX's final boundary intentionally uses the actual clock. Anchoring
        # the fixture just behind that clock keeps real freshness checks active.
        self.clock = datetime.now(timezone.utc).replace(microsecond=0)

    def row_for(self, name, **kwargs):
        kwargs.setdefault("timeframe","1h" if name=="Champion" else "5m")
        return structural_row(self.clock, asset="CNYRUBF" if name == "Currency" else "NQ", **kwargs)

    def admit(self, row, name):
        return VCR.evaluate(row, CTC.runtime_portfolio_policy(name), 0., self.clock)

    def test_all_five_portfolios_fail_closed_without_same_tf_context(self):
        for name in CTC.PORTFOLIO_ORDER:
            with self.subTest(portfolio=name):
                row = self.row_for(name)
                row.pop("timeframe_entry_context")
                # A fresh legacy displayed signal is deliberately insufficient.
                row["trend_entry_context"] = {
                    "status":"OK", "closed_at":self.clock.timestamp(),
                    "event":{"event_id":"R79_SIG_SYNTHETIC_FRESH", "direction":"LONG",
                             "signal_at":self.clock.timestamp(), "activity_confirmed":True}}
                result = self.admit(row, name)
                self.assertFalse(result["open"])
                self.assertTrue(result["hard_veto"])
                self.assertEqual(result["reason"], "SAME_TF_CONTEXT_REQUIRED")

    def test_all_five_admit_real_closed_breakout_using_actual_cost_gate(self):
        for name in CTC.PORTFOLIO_ORDER:
            for direction in ("LONG", "SHORT"):
                with self.subTest(portfolio=name, direction=direction):
                    row = self.row_for(name, direction=direction)
                    result = self.admit(row, name)
                    self.assertTrue(result["open"], result)
                    self.assertFalse(result["hard_veto"])
                    self.assertEqual(result["reason"], "CANONICAL_SIGNAL_ENTRY")
                    self.assertGreater(result["fraction"], 0)
                    self.assertGreaterEqual(result["economics"]["expected_to_stop_ratio"], VX.MIN_REWARD_RISK)
                    self.assertGreater(result["economics"]["modeled_commission_pct"], 0)
                    self.assertGreater(result["economics"]["modeled_execution_cost_pct"], 0)

    def test_fresh_quote_and_r79_label_cannot_refresh_an_expired_breakout(self):
        for name in CTC.PORTFOLIO_ORDER:
            with self.subTest(portfolio=name):
                row = self.row_for(name, signal_age=2*TS.timeframe_seconds("1h" if name=="Champion" else "5m")+50)
                original = deepcopy(row["timeframe_entry_context"]["event"])
                self.assertLess(self.clock.timestamp()-row["timeframe_entry_context"]["closed_at"], TS.timeframe_seconds(row["horizon"]))
                row["trend_entry_context"] = {
                    "status":"OK", "closed_at":self.clock.timestamp(),
                    "event":{"event_id":"R79_SIG_NOW", "signal_at":self.clock.timestamp(),
                             "direction":"LONG", "signal_authoritative":True}}
                result = self.admit(row, name)
                self.assertEqual(result["reason"], "SAME_TF_EVENT_EXPIRED")
                self.assertFalse(result["open"])
                self.assertTrue(result["hard_veto"])
                self.assertEqual(row["timeframe_entry_context"]["event"], original)

    def test_structural_stop_and_target_survive_conflicting_legacy_plan_geometry(self):
        for name in CTC.PORTFOLIO_ORDER:
            with self.subTest(portfolio=name):
                row = self.row_for(name, timeframe="1h" if name=="Impulse" else "4h")
                event = deepcopy(row["timeframe_entry_context"]["event"])
                row["trade_plan"].update(management_horizon="1m", execution_timeframe="1m",
                    stop_timeframe="1m", target_timeframe="7d", atr_timeframe="1m",
                    r66_geometry={"eligible":True,"stop_price":row["price"]-.01,"target_price":1000.})
                prepared = VTE.prepare_row(row, row["price"], self.clock)
                result = self.admit(row, name)
                self.assertTrue(result["open"], result)
                for plan in (prepared["trade_plan"], result["prepared_plan"]):
                    self.assertEqual(plan["stop_price"], event["stop_price"])
                    self.assertEqual(plan["target_price"], event["target_price"])
                    self.assertEqual(plan["signal_at"], event["signal_at"])
                    self.assertEqual(plan["entry_event_snapshot"], event)
                    self.assertEqual({plan[k] for k in ("horizon", "execution_timeframe", "management_horizon",
                                                       "atr_timeframe", "stop_timeframe", "target_timeframe")}, {row["horizon"]})
                self.assertEqual(result["economics"]["entry_geometry"]["stop_price"], event["stop_price"])
                self.assertEqual(result["economics"]["target_price"], event["target_price"])

    def test_net_rr_below_floor_is_hard_for_every_portfolio_even_with_large_legacy_rr(self):
        for name in CTC.PORTFOLIO_ORDER:
            with self.subTest(portfolio=name):
                row = self.row_for(name, width=.1)
                prepared = TFP.prepare_row(row, now=self.clock)
                raw_economics = VX.economics_gate(row["asset"], dict(prepared["trade_plan"], direction="LONG"))
                self.assertGreater(raw_economics["forecast_reward_risk"], VX.MIN_REWARD_RISK)
                self.assertGreater(raw_economics["net_reward_pct"], 0)
                self.assertGreater(raw_economics["expected_move_pct"], raw_economics["minimum_expected_move_pct"])
                self.assertLess(raw_economics["expected_to_stop_ratio"], VX.MIN_REWARD_RISK)
                result = self.admit(row, name)
                self.assertFalse(result["open"])
                self.assertTrue(result["hard_veto"])
                self.assertEqual(result["reason"], "NET_REWARD_RISK_BELOW_FLOOR")
                self.assertEqual(result["canonical_stage"], "ECONOMICS")

    def test_fresh_5m_candidate_routes_ahead_of_stale_high_confidence_4h(self):
        for asset, route in (("NQ", VCR.candidate_book), ("CNYRUBF", VCR.currency_candidate_book)):
            with self.subTest(asset=asset):
                fresh = structural_row(self.clock, timeframe="5m", asset=asset)
                fresh.update(confidence=.65, signal_tier="LONG")
                old = structural_row(self.clock, timeframe="4h", asset=asset, signal_age=14420)
                old.update(confidence=.999, signal_tier="SUPER_LONG")
                selected = route([old, fresh])[asset]
                self.assertEqual(selected["horizon"], "5m")
                self.assertEqual(selected["timeframe_entry_context"]["event"]["event_id"],
                                 fresh["timeframe_entry_context"]["event"]["event_id"])

    def test_candle_and_execution_source_conflict_and_contract_roll_fail_closed(self):
        row = structural_row(self.clock)
        row["_execution_quote"] = {"source":"Yahoo Finance", "price":row["price"],
                                   "observed_at":self.clock.isoformat(), "source_gate_pass":True,
                                   "market_open":True}
        self.assertEqual(self.admit(row, "Aggressive")["reason"], "SAME_TF_SOURCE_MISMATCH")
        pinned = structural_row(self.clock, contract_id="NQZ6")
        pinned["_execution_quote"] = {"source":"ProFinance NASD100_FUT", "price":pinned["price"],
            "contract_id":"NQH7", "observed_at":self.clock.isoformat(), "source_gate_pass":True,"market_open":True}
        self.assertEqual(self.admit(pinned, "Aggressive")["reason"], "SAME_TF_SOURCE_MISMATCH")

    def _tbank_row(self, *, signal_age=10):
        uid = "c300543d-aa18-4249-b110-615409dde036"
        row = structural_row(self.clock, asset="CNYRUBF", source="TBANK_GRPC CNYRUBF",
                             signal_age=signal_age)
        source = VPS.identity("CNYRUBF", {
            "source":"TBANK_GRPC CNYRUBF",
            "contract":{"instrument_uid":uid},
        })
        row.update(contract={"instrument_uid":uid}, contract_id=uid)
        row["timeframe_entry_context"]["source_identity"] = source
        row["timeframe_entry_context"]["event"]["source_identity"] = source
        return row

    def test_tbank_execution_quote_still_requires_native_exact_contract_uid(self):
        row = self._tbank_row()
        self.assertTrue(TFP.entry_gate(row, row["price"], "LONG", self.clock)["eligible"])

        top_level_only = deepcopy(row)
        top_level_only.pop("contract")
        gate = TFP.entry_gate(top_level_only, row["price"], "LONG", self.clock)
        self.assertFalse(gate["eligible"])
        self.assertEqual(gate["reason"], "SAME_TF_SOURCE_MISMATCH")

        wrong = deepcopy(row)
        wrong["contract"] = {"instrument_uid":"different-contract"}
        gate = TFP.entry_gate(wrong, row["price"], "LONG", self.clock)
        self.assertFalse(gate["eligible"])
        self.assertEqual(gate["reason"], "SAME_TF_SOURCE_MISMATCH")

    def test_final_plan_preserves_tbank_uid_and_canonical_expiry_reason(self):
        row = self._tbank_row(signal_age=2*TS.timeframe_seconds("5m")+50)
        plan = TFP.prepare_row(row, now=self.clock)["trade_plan"]
        self.assertEqual(plan["entry_timing_gate"]["reason"], "SAME_TF_EVENT_EXPIRED")

        final = TFP.final_plan("CNYRUBF", "LONG", plan, now=self.clock)
        self.assertNotIn("SAME_TF_SOURCE_MISMATCH",
                         final["final_economics_gate"]["blockers"])
        self.assertIn("SAME_TF_EVENT_EXPIRED",
                      final["final_economics_gate"]["blockers"])

        missing_uid = deepcopy(plan)
        missing_uid["timeframe_entry_context"]["source_identity"]["contract_id"] = None
        missing_uid["timeframe_entry_context"]["event"]["source_identity"]["contract_id"] = None
        blocked = TFP.final_plan("CNYRUBF", "LONG", missing_uid, now=self.clock)
        self.assertIn("SAME_TF_SOURCE_MISMATCH",
                      blocked["final_economics_gate"]["blockers"])

    def test_fresh_quote_before_confirmation_cannot_execute_a_later_breakout(self):
        row = structural_row(self.clock,timeframe="1h")
        event = row["timeframe_entry_context"]["event"]
        row["market_observed_at"] = datetime.fromtimestamp(event["signal_at"]-1, timezone.utc).isoformat()
        result = self.admit(row, "Champion")
        self.assertFalse(result["open"])
        self.assertTrue(result["hard_veto"])
        self.assertEqual(result["reason"], "SAME_TF_QUOTE_PREDATES_BREAKOUT")
        row["market_observed_at"] = self.clock.isoformat()
        self.assertTrue(self.admit(row, "Champion")["open"])

    def test_stale_delayed_research_quote_is_not_a_same_tf_execution_override(self):
        row = structural_row(self.clock)
        row["data_latency_class"] = "DELAYED_RESEARCH"
        row["market_observed_at"] = datetime.fromtimestamp(self.clock.timestamp()-180, timezone.utc).isoformat()
        self.assertTrue(VX.paper_quote_time_gate(dict(row, observed_at=row["market_observed_at"]),
                                                "5m", now=self.clock)["eligible"])
        result = self.admit(row, "Aggressive")
        self.assertFalse(result["open"])
        self.assertEqual(result["reason"], "EXECUTION_QUOTE_STALE")

    def test_missing_or_wrong_timeframe_context_is_a_hard_constraint(self):
        row = structural_row(self.clock)
        row["timeframe_entry_context"]["source_identity"] = None
        self.assertEqual(self.admit(row, "Aggressive")["reason"], "SAME_TF_SOURCE_IDENTITY_REQUIRED")
        row = structural_row(self.clock)
        row["horizon"] = "4h"
        self.assertEqual(self.admit(row, "Aggressive")["reason"], "SAME_TF_HORIZON_MISMATCH")

    def test_epoch_and_aware_clock_forms_preserve_historical_replay_time(self):
        historical = datetime(2020, 1, 2, 12, tzinfo=timezone.utc)
        row = valid_row(now=historical)
        forms = (historical, historical.timestamp(), int(historical.timestamp()),
                 historical.isoformat(), historical.isoformat().replace("+00:00", "Z"),
                 historical.astimezone(timezone(timedelta(hours=3))).isoformat())
        for supplied in forms:
            with self.subTest(now=supplied):
                context = TFP.context_gate(row, supplied)
                gate = TFP.entry_gate(row, row["price"], "LONG", supplied)
                self.assertTrue(context["eligible"], context)
                self.assertTrue(gate["eligible"], gate)
                self.assertEqual(context["age_seconds"], 10.)
                self.assertEqual(gate["age_seconds"], 10.)
                self.assertEqual(gate["signal_at"], historical.timestamp()-10)

    def test_explicit_invalid_or_naive_clock_fails_closed_at_each_plan_boundary(self):
        row = valid_row(now=self.clock)
        plan = TFP.prepare_row(row, now=self.clock)["trade_plan"]
        invalid = ("", "not-a-time", self.clock.replace(tzinfo=None),
                   self.clock.replace(tzinfo=None).isoformat(), False, True,
                   float("nan"), float("inf"), float("-inf"), {}, 1e300)
        for supplied in invalid:
            with self.subTest(now=repr(supplied)):
                for gate in (TFP.context_gate(row, supplied),
                             TFP.entry_gate(row, row["price"], "LONG", supplied)):
                    self.assertFalse(gate["eligible"], gate)
                    self.assertEqual(gate["reason"], "SAME_TF_DECISION_TIME_REQUIRED")
                prepared = TFP.prepare_row(row, now=supplied)["trade_plan"]
                self.assertFalse(prepared["eligible"])
                self.assertEqual(prepared["entry_timing_gate"]["reason"], "SAME_TF_DECISION_TIME_REQUIRED")
                final = TFP.final_plan("NQ", "LONG", plan, now=supplied)
                self.assertFalse(final["eligible"])
                self.assertIn("SAME_TF_DECISION_TIME_REQUIRED", final["final_economics_gate"]["blockers"])

    def test_epoch_zero_is_a_supplied_time_and_none_uses_the_actual_clock(self):
        epoch = valid_row(now=0)
        self.assertTrue(TFP.context_gate(epoch, 0)["eligible"])
        self.assertTrue(TFP.entry_gate(epoch, epoch["price"], "LONG", 0)["eligible"])
        current = valid_row()
        self.assertTrue(TFP.entry_gate(current, current["price"], "LONG", None)["eligible"])

    def test_prepare_and_final_fill_use_the_same_supplied_epoch(self):
        historical = datetime(2020, 1, 2, 12, tzinfo=timezone.utc)
        row = valid_row(now=historical)
        plan = TFP.prepare_row(row, now=historical.timestamp())["trade_plan"]
        self.assertTrue(plan["eligible"], plan["entry_timing_gate"])
        final = TFP.final_plan("NQ", "LONG", plan, now=historical.timestamp())
        self.assertTrue(final["eligible"], final["final_economics_gate"])
        gate = final["final_economics_gate"]
        self.assertEqual(gate["trend_event"]["age_seconds"], 10.)
        self.assertEqual(gate["fill_timing_gate"]["age_seconds"], 10.)
        self.assertEqual(gate["context_freshness"]["age_seconds"], 10.)

    def test_quote_observation_uses_the_same_epoch_and_timezone_contract(self):
        row = valid_row(now=self.clock)
        for observed in (self.clock.timestamp(), self.clock, self.clock.isoformat()):
            with self.subTest(observed=observed):
                row["market_observed_at"] = observed
                gate = TFP.entry_gate(row, row["price"], "LONG", self.clock.timestamp())
                self.assertTrue(gate["eligible"], gate)
        row["market_observed_at"] = self.clock.replace(tzinfo=None).isoformat()
        gate = TFP.entry_gate(row, row["price"], "LONG", self.clock.timestamp())
        self.assertFalse(gate["eligible"])
        self.assertEqual(gate["reason"], "EXECUTION_QUOTE_STALE")


if __name__ == "__main__":
    unittest.main()
