"""Parent management of genuine quote-event fixtures, with symmetric sides."""
from copy import deepcopy
from datetime import timedelta
import json
import unittest

import veritas_position_thesis as PT
import veritas_price_source as VPS
import veritas_structural_breakout as SB
import veritas_timeframe_management as TM

from test_veritas_structural_breakout import START, CLOSES, POLICY, advance, bars, raw_at
from test_veritas_timeframe_management import FakeDB


def mirror(raw):
    result = deepcopy(raw)
    result.update(price=200-raw["price"], best_bid=200-raw["best_ask"], best_ask=200-raw["best_bid"])
    for rows in result["structure_bars_by_timeframe"].values():
        for bar in rows:
            bar.update(open=200-bar["open"], high=200-bar["low"],
                       low=200-bar["high"], close=200-bar["close"])
    return result


def initial(held="LONG"):
    raw, now = raw_at(asset="TEST_FUT")
    if held == "SHORT":
        raw = mirror(raw)
    context = SB.build_context(raw, "1m", now, config=POLICY)
    event = context["event"]
    assert event["direction"] == held and context["entry_gate"]["eligible"]
    position = {"portfolio_name": "Currency", "asset": raw["asset"], "direction": held,
                "active_trade_id": "held-quote-event", "opened_at": now.isoformat(),
                "avg_entry_price": raw["price"], "units": 10., "stop_price": event["stop_price"],
                "payload": {"structural_policy_version": event["policy"]["version"],
                            "execution_horizon": "1m", "management_horizon": "1h",
                            "entry_event_snapshot": deepcopy(event),
                            "timeframe_entry_context": deepcopy(context),
                            "price_source_lock": deepcopy(event["source_identity"]),
                            "target_price": event["target_price"]}}
    return position, raw, context, now


def row_for(raw, context):
    return dict(raw, horizon="1m", timeframe_entry_context=context,
                research_decision=(context.get("event") or {}).get("direction", "NO_TRADE"), trade_plan={})


def parent_reversal(held="LONG"):
    position, raw, context, now = initial(held)
    sign = 1 if held == "LONG" else -1
    price = context["event"]["stop_anchor"] - sign*.05
    changed, later = advance(raw, now, price, 30)
    new = SB.build_context(changed, "1m", later, state=context["quote_state"], config=POLICY)
    assert new["event"]["trigger_timeframe"] == "1h"
    return position, row_for(changed, new), later


def parent_pullback(held="LONG", confirmed=True):
    position, raw, context, now = initial(held)
    values = CLOSES + [104, 108, 106, 103, 106, 109, 110][:7 if confirmed else 5]
    later = START + timedelta(hours=len(values), milliseconds=100)
    changed, _ = raw_at(values[-1], later, asset=raw["asset"], hourly=bars(values, "1h"))
    if held == "SHORT":
        changed = mirror(changed)
    new = SB.build_context(changed, "1m", later, state=context["quote_state"], config=POLICY)
    return position, row_for(changed, new), changed, later


class ProtectedParentManagementTests(unittest.TestCase):
    def test_fast_entry_binds_management_to_immutable_parent(self):
        for held in ("LONG", "SHORT"):
            position, raw, context, now = initial(held)
            saved = deepcopy(position)
            scope = PT.position_scope(position)
            self.assertTrue(scope["binding_valid"], scope)
            self.assertEqual(scope["execution_timeframe"], "1m")
            self.assertEqual(scope["management_timeframe"], "1h")
            self.assertEqual(scope["invalidation_level"], context["event"]["stop_anchor"])
            self.assertEqual(position, saved)

    def test_fast_opposite_break_does_not_invalidate_intact_parent(self):
        position, raw, context, now = initial()
        changed, later = advance(raw, now, 99., 360)
        minutes = bars([101, 103, 100, 101, 102, 101], "1m", now)
        for bar in minutes:
            bar["source_identity"] = deepcopy(raw["structure_source_identity"])
        changed["structure_bars_by_timeframe"]["1m"].extend(minutes)
        new = SB.build_context(changed, "1m", later, state=context["quote_state"], config=POLICY)
        self.assertEqual(new["event"]["direction"], "SHORT")
        self.assertEqual(new["event"]["trigger_timeframe"], "1m")
        row = row_for(changed, new)
        row["trade_plan"]["trade_integrity"] = {"hard_reasons": ["THESIS_INVALIDATION"], "hard_invalidation": True}
        decision = PT.evaluate_exit(position, row, later)
        self.assertFalse(decision["exit_authorized"], decision)
        self.assertEqual(decision["reason"], "HELD_PARENT_BREAK_PROVENANCE_MISMATCH")
        book, rows = TM.filter_lower_context(position, {raw["asset"]: row}, [row])
        self.assertEqual((book, rows), ({}, []))

    def test_entry_expiry_and_candidate_veto_do_not_end_a_hold(self):
        position, raw, context, now = initial()
        changed, later = advance(raw, now, 101.1, 121)
        new = SB.build_context(changed, "1m", later, state=context["quote_state"], config=POLICY)
        self.assertEqual(new["entry_gate"]["reason"], "STRUCTURAL_EVENT_EXPIRED")
        row = row_for(changed, new)
        row["trade_plan"]["trade_integrity"] = {"hard_reasons": ["ENTRY_TOO_LATE", "THESIS_INVALIDATION"]}
        self.assertFalse(PT.evaluate_exit(position, row, later)["exit_authorized"])

    def test_new_parent_quote_break_can_close_before_independent_stop_without_scores(self):
        for held in ("LONG", "SHORT"):
            with self.subTest(held=held):
                position, row, now = parent_reversal(held)
                sign = 1 if held == "LONG" else -1
                self.assertGreater(sign*(row["price"]-position["stop_price"]), 0.)
                decision = PT.evaluate_exit(position, row, now)
                self.assertTrue(decision["exit_authorized"], decision)
                self.assertEqual(decision["reason"], "HELD_PROTECTED_PARENT_INVALIDATED")
                self.assertEqual(decision["proof"]["timeframe"], "1h")
                self.assertNotIn("confidence", row)
                self.assertNotIn("ctc_open_position_thesis_guard", row["trade_plan"])
                selected = PT.management_row([row], position, now)
                self.assertTrue(PT.hard_thesis_exit(selected))
                book, rows = TM.filter_lower_context(position, {row["asset"]: row}, [row])
                self.assertEqual((book, rows), ({row["asset"]: row}, [row]))

    def test_opposite_entry_expiry_does_not_restore_broken_parent(self):
        position, row, now = parent_reversal()
        later = now+timedelta(seconds=121)
        changed = deepcopy(row)
        changed["observed_at"] = later.isoformat()
        context = SB.rebind_quote(row["timeframe_entry_context"], changed, later)
        changed["timeframe_entry_context"] = context
        self.assertEqual(context["entry_gate"]["reason"], "STRUCTURAL_EVENT_EXPIRED")
        self.assertTrue(PT.evaluate_exit(position, changed, later)["exit_authorized"])

    def test_foreign_stale_future_unverified_quote_cannot_close(self):
        position, row, now = parent_reversal()
        variants = [deepcopy(row) for _ in range(6)]
        variants[0]["contract"]["instrument_uid"] = "FOREIGN"
        variants[1]["observed_at"] = (now-timedelta(seconds=121)).isoformat()
        variants[2]["observed_at"] = (now+timedelta(seconds=1)).isoformat()
        variants[3]["source_gate_pass"] = False
        variants[4]["market_open"] = False
        variants[5]["timeframe_entry_context"]["event"]["signal_at"] += 1
        for changed in variants:
            self.assertFalse(PT.evaluate_exit(position, changed, now)["exit_authorized"])

    def test_changed_binding_or_preentry_opposite_event_cannot_close(self):
        position, row, now = parent_reversal()
        for field, value in (("management_horizon", "5m"), ("execution_horizon", "5m")):
            changed = deepcopy(position)
            changed["payload"][field] = value
            self.assertFalse(PT.evaluate_exit(changed, row, now)["exit_authorized"])
        changed = deepcopy(position)
        changed["opened_at"] = now.isoformat()
        self.assertEqual(PT.evaluate_exit(changed, row, now)["reason"], "HELD_PARENT_BREAK_NOT_AFTER_ENTRY")

    def test_only_new_confirmed_parent_pullback_can_tighten_stop_on_both_sides(self):
        for held in ("LONG", "SHORT"):
            early, row, quote, now = parent_pullback(held, confirmed=False)
            self.assertFalse(TM.trailing_candidate(early, [row], quote, now)["eligible"])
            position, row, quote, now = parent_pullback(held)
            result = TM.trailing_candidate(position, [row], quote, now)
            self.assertTrue(result["eligible"], result)
            self.assertEqual(result["timeframe"], "1h")
            self.assertEqual(result["reference_level"], 102.5 if held == "LONG" else 97.5)
            self.assertGreater(result["reference_pivot_at"], PT.position_scope(position)["opened_at"])
            sign = 1 if held == "LONG" else -1
            self.assertAlmostEqual(result["stop_price"], result["reference_level"]-sign*.15*result["atr"])
            self.assertGreater(sign*(result["stop_price"]-position["stop_price"]), 0.)

    def test_micro_levels_and_mutated_or_future_atr_cannot_control_parent_stop(self):
        position, row, quote, now = parent_pullback()
        expected = TM.trailing_candidate(position, [row], quote, now)
        changed = deepcopy(row)
        changed["timeframe_entry_context"]["levels"] = [{"kind": "support", "price": 109.9, "timeframe": "1m"}]
        self.assertEqual(TM.trailing_candidate(position, [changed], quote, now)["stop_price"], expected["stop_price"])
        for corruption in ("atr", "future", "source"):
            changed = deepcopy(row)
            ctx = changed["timeframe_entry_context"]
            if corruption == "atr":
                ctx["atr"] = .00001
            elif corruption == "future":
                ctx["structure_atr_proof"]["bars"][-1]["available_at"] = now.timestamp()+1
            else:
                changed["contract"]["instrument_uid"] = "FOREIGN"
            self.assertFalse(TM.trailing_candidate(position, [changed], quote, now)["eligible"])

    def test_no_widening_or_trailing_after_existing_stop_is_breached(self):
        for held in ("LONG", "SHORT"):
            position, row, quote, now = parent_pullback(held)
            sign = 1 if held == "LONG" else -1
            result = TM.trailing_candidate(position, [row], quote, now)
            position["stop_price"] = result["stop_price"]+sign*.1
            self.assertFalse(TM.trailing_candidate(position, [row], quote, now)["eligible"])
            quote["price"] = position["stop_price"]
            self.assertEqual(TM.trailing_candidate(position, [row], quote, now)["reason"],
                             "STRUCTURAL_PARENT_EXISTING_STOP_BREACHED")

    def test_atomic_revision_preserves_entry_and_rebinds_only_protected_parent(self):
        position, row, quote, now = parent_pullback()
        saved = deepcopy(position)
        db = FakeDB(position, now, accounting=False)
        result = TM.apply_trailing(db, "Currency", position, [row], quote, now)
        self.assertTrue(result["eligible"], result)
        patch = json.loads(db.writes[0][1][1])
        self.assertEqual(patch["trailing_rule"], "CTC_PROTECTED_PARENT_SWING")
        self.assertNotIn("entry_event_snapshot", patch)
        self.assertNotIn("target_price", patch)
        self.assertEqual(position, saved)
        revised = deepcopy(position)
        revised["stop_price"] = result["stop_price"]
        revised["payload"].update(patch)
        scope = PT.position_scope(revised)
        self.assertTrue(scope["binding_valid"], scope)
        self.assertEqual(scope["invalidation_level"], result["reference_level"])
        self.assertEqual(revised["payload"]["entry_event_snapshot"], saved["payload"]["entry_event_snapshot"])
        revised["payload"]["same_tf_trailing"]["reference_level"] += .1
        self.assertFalse(PT.position_scope(revised)["binding_valid"])


if __name__ == "__main__":
    unittest.main()
