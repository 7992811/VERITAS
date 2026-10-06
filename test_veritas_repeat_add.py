"""A structural event funds one entry/add per portfolio, including after reload."""
import copy
import json
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import patch

import veritas_canonical_constitution as CTC
import veritas_portfolio as VP
import veritas_portfolio_runtime as VPR
import veritas_price_source as VPS
import veritas_user_teaching as UT


class Result:
    def __init__(self, row=None): self.row = copy.deepcopy(row)
    def fetchone(self): return self.row


class AccountingDB:
    """Minimal table semantics, including the outer trade-to-position refresh."""
    def __init__(self, position):
        self.position = copy.deepcopy(position)
        self.trade_payload = copy.deepcopy(position["payload"])
        self.orders, self.queries = [], []
        self.book_fees, self.payload_replacements = 0., 0

    def execute(self, sql, args=()):
        q = " ".join(sql.split())
        self.queries.append((q, args))
        if q.startswith("SELECT * FROM paper_positions"):
            return Result(self.position)
        if q.startswith("SELECT payload FROM paper_trades"):
            return Result({"payload": self.trade_payload})
        if q.startswith("SELECT 1 AS ok FROM paper_trades"):
            return Result()
        if q.startswith("SELECT 1 AS ok FROM paper_orders"):
            if "client_order_id=%s" in q:
                found = any(o["client_order_id"] == args[0] for o in self.orders)
            else:
                portfolio, asset, side, event_id = args
                def matches(order):
                    payload = order["payload"]
                    context = (payload.get("user_teaching_trace") or {}).get("timeframe_entry_context") or {}
                    recorded = (payload.get("entry_event_id") or (context.get("event") or {}).get("event_id")
                                or (payload.get("entry_timing") or {}).get("event_id"))
                    return (order["portfolio_name"], order["asset"], order["side"], recorded) == args
                found = any(matches(o) for o in self.orders)
            return Result({"ok": 1} if found else None)
        if q.startswith("UPDATE paper_portfolios SET fees_rub"):
            self.book_fees += args[0]
        elif q.startswith("UPDATE paper_positions SET units="):
            self.position.update(units=args[0], avg_entry_price=args[1], last_price=args[2],
                                 target_fraction=args[3], updated_at=args[4], payload=json.loads(args[5]))
        elif q.startswith("UPDATE paper_trades SET fees_rub="):
            self.trade_payload.update(json.loads(args[2]))
        elif q.startswith("UPDATE paper_trades SET payload="):
            self.trade_payload = json.loads(args[0]); self.payload_replacements += 1
        elif q.startswith("UPDATE paper_positions SET payload="):
            self.position["payload"] = json.loads(args[0]); self.payload_replacements += 1
        elif q.startswith("INSERT INTO paper_orders"):
            self.orders.append({"portfolio_name": args[0], "asset": args[3], "side": args[4],
                                "payload": json.loads(args[10]), "client_order_id": args[11]})
        else:
            raise AssertionError("Unexpected accounting query: " + q)
        return Result()


class RepeatAddRegressionTests(unittest.TestCase):
    def setUp(self):
        self.clock = datetime(2026, 10, 6, 20, 0, tzinfo=timezone.utc)
        self.source = {"source_names": {"primary": "ProFinance NASD100_FUT"}}
        self.identity = VPS.identity("NQ", self.source)
        self.original_context = self.context("STF_ORIGINAL")
        self.original_trace = UT.entry_trace(self.original_context, "Aggressive")
        p = {"r66_event_id": "STF_ORIGINAL", "execution_horizon": "4h",
             "pwin": .65, "pwin_source": "ORIGINAL_ENTRY",
             "structural_policy_version": CTC.STRUCTURAL_ENTRY_POLICY["version"],
             "price_source_lock": self.identity, "user_teaching_trace": self.original_trace,
             "timeframe_entry_context": self.original_context,
             "entry_event_snapshot": copy.deepcopy(self.original_context["event"]),
             "initial_stop_price": 99., "target_price": 110., "take_price": 110.}
        self.db = AccountingDB({"portfolio_name": "Aggressive", "asset": "NQ", "direction": "LONG",
                                "units": 10., "avg_entry_price": 100., "last_price": 101.,
                                "stop_price": 99., "opened_at": "2026-10-06T12:00:00Z",
                                "active_trade_id": "Aggressive:NQ:original", "payload": p})
        self.original_payload = copy.deepcopy(p)
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(VPR.TFP, "prepare_row", side_effect=lambda row, *a, **k: dict(row)))
        self.stack.enter_context(patch.object(VPR.VTE, "prepare_row", side_effect=lambda row, *a, **k: dict(row)))
        self.stack.enter_context(patch.object(VPR.VCR, "evaluate", side_effect=self.admission))
        self.stack.enter_context(patch.object(VP.VX, "entry_gate", return_value={"eligible": True, "blockers": []}))
        self.stack.enter_context(patch.object(VP.VX, "paper_quote_time_gate", return_value={"eligible": True}))
        self.stack.enter_context(patch.object(VP.VPG, "publish_quote"))
        self.stack.enter_context(patch.object(VP, "_v90j_entry_patch", return_value={"wrapper_checked": True}))
        self.accounting = self.stack.enter_context(patch.object(
            VP, "CANONICAL_ACCOUNTING_OPEN_OR_ADD", wraps=VP.CANONICAL_ACCOUNTING_OPEN_OR_ADD))

    def context(self, event_id):
        return {"timeframe": "4h", "source_identity": self.identity,
                "event": {"event_id": event_id, "direction": "LONG", "timeframe": "4h",
                          "signal_at": self.clock.timestamp(), "stop_price": 99., "target_price": 110.}}

    def row(self, event_id):
        ctx = self.context(event_id)
        quote = {**self.source, "asset": "NQ", "price": 101., "source_gate_pass": True,
                 "market_open": True, "observed_at": self.clock.isoformat()}
        return {**quote, "horizon": "4h", "research_decision": "LONG", "_execution_quote": quote,
                "_pwin": .8, "_pwin_source": "TEST", "trend_entry_context": ctx,
                "timeframe_entry_context": ctx,
                "trade_plan": {"horizon": "4h", "stop_price": 99., "target_price": 110.,
                               "structural_policy_version": CTC.STRUCTURAL_ENTRY_POLICY["version"],
                               "timeframe_entry_context": ctx, "trend_entry_context": ctx}}

    def admission(self, row, *args):
        return {"open": True, "fraction": 1., "trend_event": row["timeframe_entry_context"]["event"],
                "prepared_plan": row["trade_plan"]}

    def add(self, event_id, fraction=.5, name="Aggressive"):
        return VPR.canonical_open_or_add(self.db, {"high_water_nav_rub": 10000.}, name, "NQ", "LONG",
                                         101., fraction, 10000., self.clock, self.row(event_id), "TEST_ADD")

    def test_actual_accounting_wrapper_keeps_new_add_trace_in_both_payloads(self):
        self.add("STF_ADD_1")
        self.assertEqual(self.accounting.call_count, 1)
        self.assertEqual(self.db.payload_replacements, 2)
        self.assertEqual(len(self.db.orders), 1)
        self.assertGreater(self.db.book_fees, 0)
        for stored in (self.db.position["payload"], self.db.trade_payload):
            self.assertEqual(stored["last_add_event_id"], "STF_ADD_1")
            self.assertTrue(UT.verify_entry_trace(stored["last_add_teaching_trace"]))
            self.assertEqual(stored["last_add_teaching_trace"]["timeframe_entry_context"]["event"]["event_id"], "STF_ADD_1")
            for key in ("r66_event_id", "user_teaching_trace", "entry_event_snapshot",
                        "timeframe_entry_context", "initial_stop_price", "target_price", "pwin", "pwin_source"):
                self.assertEqual(stored[key], self.original_payload[key])
        self.assertEqual(self.db.orders[0]["payload"]["entry_event_id"], "STF_ADD_1")

    def test_same_event_cannot_reach_accounting_again_after_an_add(self):
        self.add("STF_ADD_1")
        after = copy.deepcopy((self.db.position, self.db.trade_payload, self.db.orders, self.db.book_fees))
        self.assertEqual(self.add("STF_ADD_1", .75), 0.)
        self.assertEqual(self.accounting.call_count, 1)
        self.assertEqual((self.db.position, self.db.trade_payload, self.db.orders, self.db.book_fees), after)

    def test_original_and_last_add_ids_block_even_without_order_history(self):
        for event_id in ("STF_ORIGINAL", "STF_LAST"):
            with self.subTest(event_id=event_id):
                self.db.position["payload"]["last_add_event_id"] = "STF_LAST"
                self.assertEqual(self.add(event_id), 0.)
        self.accounting.assert_not_called()
        self.assertEqual(self.db.book_fees, 0.)

    def test_last_add_trace_also_protects_records_written_before_explicit_id(self):
        self.db.position["payload"]["last_add_teaching_trace"] = UT.entry_trace(self.context("STF_LAST"), "Aggressive")
        self.assertEqual(self.add("STF_LAST"), 0.)
        self.accounting.assert_not_called()

    def test_older_consumed_add_cannot_recur_after_a_different_add(self):
        self.add("STF_ADD_1", .5)
        self.add("STF_ADD_2", .7)
        self.assertEqual(self.accounting.call_count, 2)
        self.assertEqual(self.db.position["payload"]["last_add_event_id"], "STF_ADD_2")
        self.assertEqual(self.add("STF_ADD_1", .8), 0.)
        self.assertEqual(self.accounting.call_count, 2)
        lookups = [q for q, args in self.db.queries if "FROM paper_orders" in q and "portfolio_name=%s" in q]
        self.assertTrue(lookups)
        self.assertTrue(all("asset=%s" in q and "side=%s" in q and "LIMIT 1" in q for q in lookups))

    def test_preexisting_json_trace_orders_are_consumed_without_explicit_id(self):
        self.db.orders.append({"portfolio_name": "Aggressive", "asset": "NQ", "side": "BUY",
                               "client_order_id": "prior", "payload": {
                                   "user_teaching_trace": UT.entry_trace(self.context("STF_OLD"), "Aggressive")}})
        self.assertEqual(self.add("STF_OLD"), 0.)
        self.accounting.assert_not_called()

    def test_flat_reentry_cannot_reuse_an_event_that_previously_funded_an_add(self):
        self.add("STF_ADD_1")
        self.db.position = None
        self.assertEqual(self.add("STF_ADD_1"), 0.)
        self.assertEqual(self.accounting.call_count, 1)

    def test_event_consumed_in_another_portfolio_does_not_block_this_portfolio(self):
        self.db.orders.append({"portfolio_name": "Champion", "asset": "NQ", "side": "BUY",
                               "client_order_id": "other-portfolio", "payload": {"entry_event_id": "STF_SHARED"}})
        self.add("STF_SHARED")
        self.assertEqual(self.accounting.call_count, 1)
        self.assertEqual(len(self.db.orders), 2)


if __name__ == "__main__":
    unittest.main()
