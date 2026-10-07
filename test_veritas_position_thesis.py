"""Held-position exit regressions, including the real portfolio close loop.

Closed-bar fixtures are synthetic. Accounting writes/notifications are mocked;
structural evidence, identity binding and protective predicates are production.
"""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock, patch

import veritas_intelligence as I
import veritas_portfolio as P
import veritas_portfolio_runtime as R
import veritas_position_guard as PG
import veritas_position_thesis as PT
import veritas_price_source as PS
import veritas_thesis_guard as TG
import veritas_timeframe_structure as S


START = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()
SOURCE = PS.identity("ETH", {"source": "Binance spot"})


def scenario(timeframe="5m", held="SHORT", anchor=None):
    step = S.TIMEFRAMES[timeframe]
    bars = [dict(ts=START+i*step, open=100., high=100.5, low=99.5, close=100.,
                 volume=100., timeframe=timeframe) for i in range(32)]
    bars[24]["high"], bars[27]["low"] = 101., 99.
    bars.append(dict(ts=START+32*step, open=100., high=101.3, low=100., close=101.2,
                     volume=150., timeframe=timeframe))
    if held == "LONG":
        for bar in bars:
            bar.update(open=200-bar["open"], close=200-bar["close"],
                       high=200-bar["low"], low=200-bar["high"])
    now = datetime.fromtimestamp(START+33*step+10, timezone.utc)
    context = S.build_context(bars, timeframe, now, asset="ETH", source_identity=SOURCE)
    opposite = "LONG" if held == "SHORT" else "SHORT"
    px = bars[-1]["close"]
    row = {"asset": "ETH", "horizon": timeframe, "research_decision": opposite,
           "confidence": .85, "source": "Binance spot", "source_gate_pass": True,
           "price": px, "market_observed_at": now.isoformat(), "market_open": True,
           "entry_quality": "FRESH_BREAKOUT", "timeframe_entry_context": context,
           "horizon_structure": {"direction": opposite, "state": "BUILDING_TREND", "score": .85},
           "institutional_signal": {"evidence_independence": {"independent_count": 4}},
           "trade_plan": {"eligible": False, "rule_arbitration": {"hard_veto": {
               "decision": "VETO", "rule_id": "NEGATIVE_VALIDATED_SETUP_EDGE"}}}}
    row["trade_plan"] = I.trade_integrity_layer("ETH", timeframe, {}, row["trade_plan"], opposite)
    position = {"asset": "ETH", "direction": held, "active_trade_id": "held-ETH",
                "opened_at": datetime.fromtimestamp(START+31*step, timezone.utc).isoformat(),
                "avg_entry_price": 100., "last_price": 100., "units": 1000.,
                "stop_price": 103. if held == "SHORT" else 97.,
                "payload": {"execution_horizon": timeframe, "execution_timeframe": timeframe,
                            "entry_event_id": "original-entry", "price_source_lock": deepcopy(SOURCE)}}
    if anchor is not None:
        position["payload"]["entry_event_snapshot"] = {
            "event_id": "original-entry", "asset": "ETH", "direction": held,
            "timeframe": timeframe, "source_identity": deepcopy(SOURCE), "stop_anchor": anchor}
    return position, row, now


class PositionThesisTests(unittest.TestCase):
    def test_opposite_long_entry_veto_is_not_short_exit(self):
        z, row, now = scenario()
        row.pop("timeframe_entry_context")
        row["structure_breakout_grid"] = {"5m": {"exit_signal": True, "direction": "SHORT", "state": "EXIT_REVERSAL"}}
        original = deepcopy(row)
        bound = P._v842_management_row([row], z, now)
        self.assertTrue(bound["trade_plan"]["trade_integrity"]["hard_invalidation"])
        self.assertFalse(R._v842_hard_thesis_exit(bound))
        self.assertFalse(P._v90r19_old_structure_broken([row], z, now))
        self.assertEqual(row, original)

    def test_same_side_entry_only_vetoes_remain_admission_hard(self):
        z, row, now = scenario()
        row.pop("timeframe_entry_context")
        row["research_decision"] = "SHORT"
        for veto in ("RULE_ARBITRATION_VETO", "EXECUTION_CONSISTENCY_VETO", "REENTRY_BLOCKED", "THESIS_INVALIDATION"):
            with self.subTest(veto=veto):
                row["trade_plan"]["trade_integrity"].update(hard_invalidation=True, hard_reasons=[veto])
                bound = P._v842_management_row([row], z, now)
                self.assertFalse(R._v842_hard_thesis_exit(bound))
                self.assertTrue(bound["trade_plan"]["trade_integrity"]["hard_invalidation"])

    def test_integrity_distinguishes_candidate_from_held_thesis(self):
        _, row, _ = scenario()
        ti = row["trade_plan"]["trade_integrity"]
        self.assertEqual(ti["entry_permission"], "VETO")
        self.assertEqual(ti["entry_veto_reasons"], ["RULE_ARBITRATION_VETO"])
        self.assertEqual(ti["invalidation_scope"], "ENTRY_CANDIDATE")
        self.assertEqual(ti["exit_authority"], "POSITION_BOUND_STRUCTURE_REQUIRED")

    def test_real_closed_same_tf_reversal_protects_legacy_1m_and_5m(self):
        for tf in ("1m", "5m"):
            for held in ("LONG", "SHORT"):
                with self.subTest(tf=tf, held=held):
                    z, row, now = scenario(tf, held)
                    decision = PT.evaluate_exit(z, row, now)
                    self.assertTrue(decision["exit_authorized"], decision)
                    self.assertEqual(decision["reason"], "LEGACY_SAME_TF_STRUCTURAL_REVERSAL")
                    self.assertNotEqual(decision["proof"]["direction"], held)
                    self.assertNotEqual(decision["proof"]["event_id"], decision["held"]["entry_event_id"])

    def test_new_event_must_break_original_held_anchor(self):
        z, row, now = scenario(anchor=102.)
        self.assertEqual(PT.evaluate_exit(z, row, now)["reason"], "HELD_EVENT_INVALIDATION_LEVEL_INTACT")
        z["payload"]["entry_event_snapshot"]["stop_anchor"] = 101.
        saved = deepcopy(z)
        bound = P._v842_management_row([row], z, now)
        self.assertTrue(R._v842_hard_thesis_exit(bound))
        self.assertEqual(bound[PT.DECISION_KEY]["reason"], "HELD_EVENT_STRUCTURE_INVALIDATED")
        journal = PT.journal_position(z, bound, "HARD_THESIS_INVALIDATION", now)
        decision = journal["payload"]["last_exit_thesis_decision"]
        self.assertEqual(decision["held"]["entry_event_id"], "original-entry")
        self.assertEqual(decision["held"]["execution_timeframe"], "5m")
        self.assertEqual(decision["held"]["source_identity"], SOURCE)
        self.assertEqual(journal["payload"]["entry_event_snapshot"], saved["payload"]["entry_event_snapshot"])
        self.assertEqual(z, saved)

    def test_no_timeframe_or_source_fallback_even_with_higher_confidence_veto(self):
        z, row, now = scenario()
        wrong_tf = deepcopy(row); wrong_tf.update(horizon="1m", confidence=.99)
        wrong_source = deepcopy(row); wrong_source.update(source="Coinbase spot", confidence=.99)
        self.assertIsNone(P._v842_management_row([wrong_tf, wrong_source], z, now))
        bound = P._v842_management_row([wrong_tf, wrong_source, row], z, now)
        self.assertEqual(bound["horizon"], "5m")
        self.assertEqual(bound["source"], "Binance spot")
        self.assertTrue(R._v842_hard_thesis_exit(bound))

    def test_mutated_held_contract_never_adopts_the_new_signal_identity(self):
        z, row, now = scenario(anchor=101.)
        for key, value in (("direction", "LONG"), ("timeframe", "1m"),
                           ("source_identity", PS.identity("ETH", {"source": "Coinbase spot"}))):
            changed = deepcopy(z)
            changed["payload"]["entry_event_snapshot"][key] = value
            with self.subTest(key=key):
                self.assertFalse(PT.evaluate_exit(changed, row, now)["exit_authorized"])

    def test_future_reused_preentry_or_reversed_event_cannot_close(self):
        z, row, now = scenario()
        for defect in ("future", "known_after_open", "before_entry", "same_event", "reversed", "stale_quote", "source"):
            changed = deepcopy(row); position = deepcopy(z)
            event = changed["timeframe_entry_context"]["event"]
            if defect == "future": event["confirmed_at"] = now.timestamp()+1
            if defect == "known_after_open": event["level_available_at"] = event["breakout_bar_at"]+1
            if defect == "before_entry": position["opened_at"] = now.isoformat()
            if defect == "same_event": event["event_id"] = "original-entry"
            if defect == "reversed": event.update(spent=True, spent_reason="SAME_TF_STOP_ALREADY_REACHED")
            if defect == "stale_quote": changed["market_observed_at"] = datetime.fromtimestamp(now.timestamp()-301, timezone.utc).isoformat()
            if defect == "source": event["source_identity"] = PS.identity("ETH", {"source": "Coinbase spot"})
            with self.subTest(defect=defect):
                self.assertFalse(PT.evaluate_exit(position, changed, now)["exit_authorized"])

    def test_senior_structure_still_needs_decisive_senior_confirmation(self):
        z, row, now = scenario("1h")
        self.assertEqual(PT.evaluate_exit(z, row, now)["reason"], "HELD_SENIOR_CONFIRMATION_REQUIRED")
        senior = deepcopy(row); senior["horizon"] = "4h"
        _, guarded, meta = TG.guard_open_position(MagicMock(), z, {}, [row, senior], now)
        self.assertTrue(meta["hard_exit_allowed"], meta)
        bound = P._v842_management_row(guarded, z, now)
        self.assertTrue(R._v842_hard_thesis_exit(bound), bound[PT.DECISION_KEY])
        senior["research_decision"] = z["direction"]
        _, guarded, meta = TG.guard_open_position(MagicMock(), z, {}, [row, senior], now)
        self.assertFalse(meta["hard_exit_allowed"])
        self.assertFalse(R._v842_hard_thesis_exit(P._v842_management_row(guarded, z, now)))

    def test_original_horizon_precedes_a_conflicting_trade_lookup(self):
        z, _, _ = scenario(anchor=101.)
        c = MagicMock(); c.execute.return_value.fetchone.return_value = {"horizon": "1h"}
        self.assertEqual(TG._trade_horizon(c, z), "5m")
        c.execute.assert_not_called()

    def test_legacy_r66_event_id_is_preserved_in_exit_journal(self):
        z, row, now = scenario()
        z["payload"].pop("entry_event_id")
        z["payload"].update(r66_event_id="original-r66-event", signal_event_id="later-display-event")
        bound = P._v842_management_row([row], z, now)
        journal = PT.journal_position(z, bound, "HARD_THESIS_INVALIDATION", now)
        self.assertEqual(journal["payload"]["last_exit_thesis_decision"]["held"]["entry_event_id"], "original-r66-event")

    def test_flip_requires_old_bound_break_and_separate_new_confirmation(self):
        z, row, now = scenario()
        without = deepcopy(row); without.pop("timeframe_entry_context")
        without["horizon_structure"].update(state="EXIT_REVERSAL", score=.99)
        self.assertFalse(P._v90r19_flip_confirmed([without], z, without, now))
        weak = deepcopy(row); weak["institutional_signal"]["evidence_independence"]["independent_count"] = 1
        self.assertTrue(R._v842_hard_thesis_exit(P._v842_management_row([weak], z, now)))
        self.assertFalse(P._v90r19_flip_confirmed([weak], z, weak, now))
        with patch.object(P, "_v90r33_previous_flip_confirmed", return_value=False), \
                patch.object(P, "_v90r33_edge_eval", return_value={"active": False}):
            self.assertTrue(P._v90r19_flip_confirmed([row], z, row, now))
            self.assertTrue(row["_v90_exit_only_flip"])


class PortfolioThesisExitTests(unittest.TestCase):
    def run_cycle(self, z, row, now, price=None, new_risk=True, candidate=False):
        px = price if price is not None else row.get("price", 100.)
        book = {"initial_nav_rub": 1e6, "high_water_nav_rub": 1e6,
                "benchmark_nav_rub": 1e6, "last_mark_at": None}
        with ExitStack() as stack:
            for key, value in {"_portfolio_rows": (book, [z]), "_mark_nav": (1e6, 0, .1, -.1),
                    "_apply_funding": 0, "_risk_governor": {"new_risk": new_risk, "max_gross": 2},
                    "_stats": {}, "_portfolio_admission_trace": [], "_desired_fraction": 0.}.items():
                stack.enter_context(patch.object(P, key, return_value=value))
            close = stack.enter_context(patch.object(P, "_close_or_reduce", return_value=0.))
            opening = stack.enter_context(patch.object(P, "_open_or_add"))
            P._v90j_base_step_one(MagicMock(), "Champion", P.POLICIES["Champion"], {"ETH": row} if candidate else {},
                                  {"ETH": px}, 14., 83., now.isoformat(), .0004, [row] if row else [])
            opening.assert_not_called()
            return close

    def test_veto_and_raw_exit_reversal_do_not_close_current_short(self):
        z, row, now = scenario()
        row.pop("timeframe_entry_context")
        row["structure_breakout_grid"] = {"5m": {"exit_signal": True, "direction": "SHORT", "state": "EXIT_REVERSAL"}}
        self.run_cycle(z, row, now).assert_not_called()

    def test_same_side_entry_veto_does_not_close_current_short(self):
        z, row, now = scenario()
        row.pop("timeframe_entry_context"); row["research_decision"] = "SHORT"
        self.run_cycle(z, row, now).assert_not_called()

    def test_candidate_flip_flag_cannot_bypass_held_structure_binding(self):
        z, row, now = scenario()
        row.pop("timeframe_entry_context"); row["_flip_confirmed"] = True
        self.run_cycle(z, row, now, candidate=True).assert_not_called()

    def test_real_own_tf_break_closes_before_stop_and_records_proof(self):
        z, row, now = scenario(anchor=101.)
        self.assertLess(row["price"], z["stop_price"])
        close = self.run_cycle(z, row, now)
        close.assert_called_once()
        self.assertEqual(close.call_args.args[-1], "HARD_THESIS_INVALIDATION")
        exit_z = close.call_args.args[3]
        proof = exit_z["payload"]["last_exit_thesis_decision"]
        self.assertTrue(proof["exit_authorized"])
        self.assertEqual(proof["held"]["entry_event_id"], "original-entry")
        self.assertEqual(z["payload"]["entry_event_snapshot"]["event_id"], "original-entry")
        self.assertNotIn("last_exit_thesis_decision", z["payload"])

    def test_missing_or_broken_diagnostics_do_not_block_stop_or_portfolio_risk(self):
        z, row, now = scenario()
        quote = dict(PS.quote_from_row(row), price=104.)
        self.assertEqual(PG.protective_reason(z, quote, now), "STOP")
        for broken in (False, True):
            with self.subTest(broken=broken), ExitStack() as stack:
                if broken:
                    stack.enter_context(patch.object(PT, "position_scope", side_effect=ValueError("missing diagnostic")))
                close = self.run_cycle(z, {}, now, price=104.)
                self.assertEqual(close.call_args.args[-1], "STOP")
                close = self.run_cycle(z, {}, now, price=100., new_risk=False)
                self.assertEqual(close.call_args.args[-1], "RISK_HARD_STOP")


if __name__ == "__main__":
    unittest.main()
