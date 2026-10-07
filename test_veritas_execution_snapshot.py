"""One fresh quote/fill from canonical admission through the paper ledger.

Structural OHLC, source, event timing, economics, risk sizing, snapshot and
accounting run unchanged. Only SQL storage, notifications and quote caches are
isolated; attempted network access is an error. The prices reproduce the stale
ETH book failure mode, not a fitted or counterfactual trading strategy.
"""
import copy
import io
import json
import socket
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import veritas_costs as VC
import veritas_execution as VX
import veritas_execution_snapshot as VES
import veritas_portfolio as VP
import veritas_portfolio_runtime as VPR
import veritas_position_guard as VPG
import veritas_price_source as VPS
import veritas_timeframe_policy as TFP
import veritas_user_teaching as UT
from test_veritas_repeat_add import AccountingDB, Result
from test_veritas_timeframe_policy import valid_row


class EntryAccountingDB(AccountingDB):
    """Extend the real ADD regression's SQL model with the first OPEN inserts."""
    def __init__(self, position=None):
        super().__init__(position or {"payload": {}})
        self.position = copy.deepcopy(position)
        self.inserted_trade = None

    def execute(self, sql, args=()):
        q = " ".join(sql.split())
        if q.startswith("INSERT INTO paper_trades"):
            self.queries.append((q, args))
            self.trade_payload = json.loads(args[11])
            self.inserted_trade = {
                "trade_id": args[0], "avg_entry_price": args[5], "max_fraction": args[6],
                "fees_rub": args[7], "payload": copy.deepcopy(self.trade_payload)}
            return Result()
        if q.startswith("INSERT INTO paper_positions"):
            self.queries.append((q, args))
            self.position = dict(zip(("portfolio_name", "asset", "direction", "units",
                "avg_entry_price", "opened_at", "updated_at", "active_trade_id", "stop_price",
                "target_fraction", "last_price", "payload"), args))
            self.position["payload"] = json.loads(args[11])
            return Result()
        result = super().execute(sql, args)
        if q.startswith("INSERT INTO paper_orders"):
            self.orders[-1].update(price=args[5], notional_rub=args[6], fee_rub=args[7],
                                   fraction_nav=args[8])
        return result


class VerifiedExecutionSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.clock = datetime(2026, 10, 6, 20, 0, tzinfo=timezone.utc)
        self.nav = 1_000_000.
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(VPG._quotes, {}, clear=True))
        self.stack.enter_context(patch.dict(VPG._source_quotes, {}, clear=True))
        self.stack.enter_context(patch.object(VPG, "_entry_namespace", None))
        self.stack.enter_context(patch.object(VPG, "publish_quote"))
        self.stack.enter_context(patch.object(VP.VCN, "enqueue_order"))
        self.stack.enter_context(patch.object(socket, "create_connection",
                                             side_effect=AssertionError("Network disabled in execution test")))
        self.stack.enter_context(patch.object(socket.socket, "connect",
                                             side_effect=AssertionError("Network disabled in execution test")))
        self.stack.enter_context(redirect_stdout(io.StringIO()))

    def case(self, direction="SHORT"):
        row = valid_row("ETH", "1h", direction, price=2700., source="Binance spot", now=self.clock)
        stale = 2680. if direction == "SHORT" else 2720.
        row.update(best_bid=stale-.005, best_ask=stale+.005, bid=stale, ask=stale+.01,
                   _execution_audit={})
        quote = {"asset": "ETH", "price": 2700., "best_bid": 2699.995,
                 "best_ask": 2700.005, "source_names": {"primary": "Binance spot"},
                 "source_gate_pass": True, "market_open": True,
                 "observed_at": self.clock.isoformat()}
        row["_execution_quote"] = copy.deepcopy(quote)
        return row, quote

    def open(self, row, db=None, fraction=.1):
        db = db or EntryAccountingDB()
        result = VPR.canonical_open_or_add(
            db, {"high_water_nav_rub": self.nav}, "Champion", "ETH", row["research_decision"],
            row["price"], fraction, self.nav, self.clock, row, "VERIFIED_ETH_ENTRY_TEST")
        return db, result

    def assert_ledger_fill(self, db, direction):
        self.assertEqual(len(db.orders), 1, db.queries)
        order = db.orders[0]
        payload = order["payload"]
        gate = payload["fill_economics_gate"]
        snapshot = payload["execution_snapshot"]
        expected = 2700.005*(1.+VC.SLIPPAGE_RATE) if direction == "LONG" else 2699.995*(1.-VC.SLIPPAGE_RATE)
        self.assertTrue(gate["eligible"], gate)
        self.assertAlmostEqual(order["price"], expected, places=10)
        self.assertEqual(order["price"], gate["modeled_entry_fill"])
        self.assertEqual(payload["execution_model"], gate["entry_execution_model"])
        self.assertEqual(snapshot, gate["execution_snapshot"])
        self.assertEqual(snapshot["fill"], payload["execution_model"])
        self.assertEqual(snapshot["quote"]["best_bid"], 2699.995)
        self.assertEqual(snapshot["quote"]["best_ask"], 2700.005)
        self.assertEqual(snapshot["source_identity"]["key"], "BINANCE:ETHUSDT")
        self.assertEqual(snapshot["quote_id"], VES._digest(snapshot["quote"]))
        self.assertEqual(snapshot["fraction_nav"], order["fraction_nav"])
        self.assertAlmostEqual(order["fee_rub"], order["notional_rub"]*VC.COMMISSION_RATE)
        return order, gate, snapshot

    def test_fresh_short_and_long_book_produces_the_exact_checked_recorded_fill(self):
        for direction in ("SHORT", "LONG"):
            with self.subTest(direction=direction):
                row, _ = self.case(direction)
                original_event = copy.deepcopy(row["timeframe_entry_context"]["event"])
                db, _ = self.open(row)
                order, gate, snapshot = self.assert_ledger_fill(db, direction)
                self.assertEqual(db.inserted_trade["avg_entry_price"], order["price"])
                self.assertEqual(db.position["avg_entry_price"], order["price"])
                self.assertAlmostEqual(db.position["units"], order["notional_rub"]/order["price"])
                self.assertEqual(db.position["last_price"], 2700.)
                for stored in (db.position["payload"], db.trade_payload):
                    self.assertEqual(stored["execution_snapshot"], snapshot)
                    self.assertEqual(stored["entry_execution_model"], gate["entry_execution_model"])
                    self.assertEqual(stored["entry_event_snapshot"], original_event)
                    self.assertEqual(stored["initial_stop_price"], original_event["stop_price"])
                self.assertEqual(row["timeframe_entry_context"]["event"], original_event)

    def test_original_signal_price_and_event_time_do_not_become_the_execution_price(self):
        row, _ = self.case()
        row["price"] = 2684.26
        original = copy.deepcopy(row["timeframe_entry_context"]["event"])
        db, _ = self.open(row)
        _, _, snapshot = self.assert_ledger_fill(db, "SHORT")
        self.assertEqual(snapshot["signal_reference_price"], 2684.26)
        self.assertEqual(snapshot["fill"]["reference_price"], 2700.)
        self.assertEqual(db.position["payload"]["entry_event_snapshot"], original)

    def test_snapshot_and_recorded_event_are_detached_from_mutable_input(self):
        row, quote = self.case()
        gate = VX.entry_gate(row, row["price"], "SHORT", .1, now=self.clock)
        frozen = copy.deepcopy(gate["execution_snapshot"])
        db, _ = self.open(row)
        recorded = copy.deepcopy(db.position["payload"])
        row["_execution_quote"]["source_names"]["primary"] = "Coinbase spot"
        row["_execution_quote"]["best_bid"] = 1000.
        row["timeframe_entry_context"]["event"]["signal_at"] += 3600
        row["timeframe_entry_context"]["event"]["stop_price"] += 100.
        self.assertEqual(gate["execution_snapshot"], frozen)
        self.assertEqual(db.position["payload"], recorded)
        self.assertIsNotNone(VES.checked_fill(gate, quote, "ETH", "SHORT", 2700., .1))
        self.assertIsNone(VES.checked_fill(gate, row["_execution_quote"], "ETH", "SHORT", 2700., .1))

    def test_execution_price_outside_original_extension_blocks_before_sql_orders(self):
        for direction in ("SHORT", "LONG"):
            row, _ = self.case(direction)
            event = row["timeframe_entry_context"]["event"]
            sign = 1 if direction == "LONG" else -1
            price = event["trigger_level"] + sign*(event["policy"]["max_extension_atr"]+.1)*event["atr"]
            row["_execution_quote"].update(price=price, best_bid=price-.005, best_ask=price+.005)
            db, result = self.open(row)
            self.assertEqual(result, 0.)
            self.assertEqual(row["_execution_audit"]["reason"], "SAME_TF_ENTRY_EXTENDED")
            self.assertFalse(db.orders)
            self.assertEqual(db.book_fees, 0.)

    def test_missing_selected_quote_time_or_book_cannot_fall_back_to_old_row(self):
        for missing in ("empty", "null", "time", "book"):
            with self.subTest(missing=missing):
                row, _ = self.case()
                if missing == "empty":
                    row["_execution_quote"] = {}
                elif missing == "null":
                    row["_execution_quote"] = None
                elif missing == "time":
                    row["_execution_quote"].pop("observed_at")
                else:
                    row["_execution_quote"].pop("best_bid")
                    row["_execution_quote"].pop("best_ask")
                db, result = self.open(row)
                self.assertEqual(result, 0.)
                self.assertFalse(db.orders)
                self.assertEqual(db.book_fees, 0.)

    def test_explicit_stale_book_is_not_refreshed_by_a_fresh_midprice_timestamp(self):
        row, _ = self.case()
        row["_execution_quote"]["orderbook_observed_at"] = (self.clock-timedelta(minutes=20)).isoformat()
        gate = VX.entry_gate(row, row["price"], "SHORT", .1, now=self.clock)
        self.assertFalse(gate["eligible"])
        db, result = self.open(row)
        self.assertEqual(result, 0.)
        self.assertFalse(db.orders)

    def test_invalid_execution_prices_fail_closed_without_conversion_exceptions(self):
        for label, value in (("huge_integer", 10**1000), ("missing", None),
                             ("text", "invalid"), ("nan", float("nan")), ("infinite", float("inf"))):
            with self.subTest(price=label):
                row, _ = self.case()
                row["_execution_quote"]["price"] = value
                gate = VX.entry_gate(row, row["price"], "SHORT", .1, now=self.clock)
                self.assertFalse(gate["eligible"])
                db, result = self.open(row)
                self.assertEqual(result, 0.)
                self.assertFalse(db.orders)
                self.assertEqual(db.book_fees, 0.)

    def test_checked_snapshot_rejects_changed_stop_target_or_net_economics(self):
        row, quote = self.case()
        original = VX.entry_gate(row, row["price"], "SHORT", .1, now=self.clock)
        self.assertTrue(original["eligible"], original)
        for field in ("stop_price", "target_price", "net_risk_pct", "net_reward_pct", "net_reward_risk",
                      "modeled_stop_fill", "modeled_target_fill"):
            with self.subTest(field=field):
                gate = copy.deepcopy(original)
                if field == "stop_price":
                    gate["entry_geometry"][field] += 1.
                else:
                    gate[field] += .01
                self.assertIsNone(VES.checked_fill(gate, quote, "ETH", "SHORT", 2700., .1))

    def test_add_records_only_incremental_units_at_the_checked_fill(self):
        for direction in ("SHORT", "LONG"):
            with self.subTest(direction=direction):
                row, _ = self.case(direction)
                event = row["timeframe_entry_context"]["event"]
                original = copy.deepcopy(row["timeframe_entry_context"])
                original["event"].update(event_id="STF_PRIOR_ETH_"+direction,
                                         signal_at=(self.clock-timedelta(hours=4)).timestamp())
                payload = {"execution_horizon": "1h", "structural_policy_version": TFP.VERSION,
                           "price_source_lock": original["source_identity"],
                           "r66_event_id": original["event"]["event_id"],
                           "entry_event_snapshot": copy.deepcopy(original["event"]),
                           "timeframe_entry_context": original,
                           "initial_stop_price": event["stop_price"], "target_price": event["target_price"],
                           "take_price": event["target_price"], "pwin": .7, "pwin_source": "ORIGINAL",
                           "user_teaching_trace": UT.entry_trace(original, "Champion")}
                average = 2680. if direction == "LONG" else 2720.
                position = {"portfolio_name": "Champion", "asset": "ETH", "direction": direction,
                            "units": 10., "avg_entry_price": average, "last_price": 2700.,
                            "opened_at": (self.clock-timedelta(hours=4)).isoformat(),
                            "stop_price": event["stop_price"], "active_trade_id": "PRIOR_ETH_"+direction,
                            "payload": payload}
                current_fraction = position["units"]*2700./self.nav
                db = EntryAccountingDB(position)
                self.open(row, db, current_fraction+.1)
                order, _, snapshot = self.assert_ledger_fill(db, direction)
                added_units = order["notional_rub"]/order["price"]
                self.assertAlmostEqual(db.position["units"], 10.+added_units)
                self.assertAlmostEqual(db.position["avg_entry_price"],
                                       (10.*average+order["notional_rub"])/(10.+added_units))
                self.assertAlmostEqual(snapshot["fraction_nav"], .1)
                self.assertAlmostEqual(order["notional_rub"], .1*self.nav)
                for stored in (db.position["payload"], db.trade_payload):
                    self.assertEqual(stored["entry_event_snapshot"], payload["entry_event_snapshot"])
                    self.assertEqual(stored["initial_stop_price"], payload["initial_stop_price"])
                    self.assertEqual(stored["target_price"], payload["target_price"])
                    self.assertEqual(stored["last_add_execution_snapshot"], snapshot)


if __name__ == "__main__":
    unittest.main()
