"""A structural event funds one entry/add per portfolio, including after reload."""
import copy
import io
import json
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import veritas_canonical_constitution as CTC
import veritas_execution as VX
import veritas_portfolio as VP
import veritas_portfolio_runtime as VPR
import veritas_price_source as VPS
import veritas_timeframe_policy as TFP
import veritas_user_teaching as UT
from test_veritas_timeframe_policy import valid_row


class Result:
    def __init__(self, row=None): self.row = copy.deepcopy(row)
    def fetchone(self): return self.row
    def fetchall(self): return [copy.deepcopy(self.row)] if self.row is not None else []


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
            if '||' in q:
                self.trade_payload.update(json.loads(args[0]))
            else:
                self.trade_payload = json.loads(args[0]); self.payload_replacements += 1
        elif q.startswith("UPDATE paper_positions SET payload="):
            if '||' in q:
                self.position['payload'].update(json.loads(args[0]))
            else:
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
        original_event = self.original_context["event"]
        p = {"r66_event_id": "STF_ORIGINAL", "execution_horizon": "4h",
             "pwin": .65, "pwin_source": "ORIGINAL_ENTRY",
             "structural_policy_version": CTC.STRUCTURAL_ENTRY_POLICY["version"],
             "price_source_lock": self.identity, "user_teaching_trace": self.original_trace,
             "timeframe_entry_context": self.original_context,
             "entry_event_snapshot": copy.deepcopy(self.original_context["event"]),
             "initial_stop_price": original_event["stop_price"],
             "target_price": original_event["target_price"], "take_price": original_event["target_price"]}
        self.db = AccountingDB({"portfolio_name": "Aggressive", "asset": "NQ", "direction": "LONG",
                                "units": 10., "avg_entry_price": 100., "last_price": 101.,
                                "stop_price": original_event["stop_price"], "opened_at": "2026-10-06T12:00:00Z",
                                "active_trade_id": "Aggressive:NQ:original", "payload": p})
        self.original_payload = copy.deepcopy(p)
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        # Isolate event deduplication and immutable journal fields from candidate
        # selection. Structural preparation, fill economics, costs and snapshot
        # validation below all run through their real implementations.
        self.stack.enter_context(patch.object(VPR.VCR, "evaluate", side_effect=self.admission))
        self.stack.enter_context(patch.object(VP.VPG, "publish_quote"))
        self.stack.enter_context(patch.object(VP, "_v90j_entry_patch", return_value={"wrapper_checked": True}))
        self.accounting = self.stack.enter_context(patch.object(
            VP, "CANONICAL_ACCOUNTING_OPEN_OR_ADD", wraps=VP.CANONICAL_ACCOUNTING_OPEN_OR_ADD))

    def context(self, event_id):
        clock = self.clock-timedelta(hours=8) if event_id == "STF_ORIGINAL" else self.clock
        context = valid_row(asset="NQ", horizon="4h", price=101., now=clock)["timeframe_entry_context"]
        # Named identities make reuse assertions readable; the builder supplies
        # the causal candles, source, ATR, stop, target and confirmation times.
        context["event"]["event_id"] = event_id
        return context

    def row(self, event_id):
        ctx = self.context(event_id)
        quote = {**self.source, "asset": "NQ", "price": 101., "source_gate_pass": True,
                 "market_open": True, "observed_at": self.clock.isoformat()}
        row = valid_row(asset="NQ", horizon="4h", price=101., now=self.clock)
        row.update(**quote, _execution_quote=quote, _pwin=.8, _pwin_source="TEST",
                   trend_entry_context=ctx, timeframe_entry_context=ctx)
        return TFP.prepare_row(row, 101., self.clock)

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


class StoredAddGeometryRegressionTests(unittest.TestCase):
    """Real canonical admission and accounting; only the database is in memory."""
    def setUp(self):
        self.clock = datetime.now(timezone.utc)
        # Avoid leaving synthetic prices in the process-wide quote cache.
        publisher = patch.object(VP.VPG, "publish_quote")
        publisher.start(); self.addCleanup(publisher.stop)
        output = redirect_stdout(io.StringIO())
        output.__enter__(); self.addCleanup(output.__exit__, None, None, None)

    def case(self, direction, near=False, held_room=None):
        row = TFP.prepare_row(valid_row(horizon="4h", direction=direction, now=self.clock),
                              now=self.clock)
        event = row["timeframe_entry_context"]["event"]
        sign = 1 if direction == "LONG" else -1
        held_target = row["price"]+sign*.3 if near else event["target_price"]-sign*.2
        if held_room is not None:
            held_target = row["price"]+sign*held_room
        original = copy.deepcopy(row["timeframe_entry_context"])
        original["event"].update(event_id="STF_ORIGINAL_"+direction, target_price=held_target,
                                 signal_at=(self.clock-timedelta(hours=8)).timestamp())
        payload = {"execution_horizon":"4h", "target_price":held_target, "take_price":held_target,
                   "initial_take_price":held_target, "initial_stop_price":event["stop_price"],
                   "structural_policy_version":TFP.VERSION,
                   "price_source_lock":row["timeframe_entry_context"]["source_identity"],
                   "r66_event_id":original["event"]["event_id"],
                   "pwin":.65, "pwin_source":"ORIGINAL_ENTRY",
                   "timeframe_entry_context":original,
                   "entry_event_snapshot":copy.deepcopy(original["event"]),
                   "user_teaching_trace":UT.entry_trace(original, "Aggressive")}
        position = {"portfolio_name":"Aggressive", "asset":"NQ", "direction":direction,
                    "units":10., "avg_entry_price":100., "last_price":row["price"],
                    "stop_price":event["stop_price"], "payload":payload,
                    "opened_at":(self.clock-timedelta(hours=8)).isoformat(),
                    "active_trade_id":"TEST_ORIGINAL_"+direction}
        row["_execution_audit"] = {}
        return row, AccountingDB(position), copy.deepcopy(payload)

    def add(self, row, db):
        return VPR.canonical_open_or_add(db, {"high_water_nav_rub":10000.}, "Aggressive", "NQ",
            row["research_decision"], row["price"], .5, 10000., self.clock, row, "TEST_ADD_GEOMETRY")

    def initial_admission(self, row):
        admission = VPR.VCR.evaluate(row, CTC.runtime_portfolio_policy("Aggressive"), 0., self.clock)
        self.assertTrue(admission["open"], admission)
        return admission

    def test_fresh_farther_target_funds_add_near_tp1_without_widening_stop(self):
        for direction in ("LONG", "SHORT"):
            with self.subTest(direction=direction):
                row, db, original = self.case(direction, near=True)
                new_event = copy.deepcopy(row["timeframe_entry_context"]["event"])
                original_stop = db.position["stop_price"]
                self.initial_admission(row)
                with patch.object(VP, "CANONICAL_ACCOUNTING_OPEN_OR_ADD",
                                  wraps=VP.CANONICAL_ACCOUNTING_OPEN_OR_ADD) as accounting:
                    self.add(row, db)
                    self.assertEqual(accounting.call_count, 1)
                self.assertEqual(len(db.orders), 1)
                gate = db.orders[0]["payload"]["fill_economics_gate"]
                self.assertTrue(gate["eligible"], gate)
                self.assertEqual(gate["add_geometry_basis"],
                                 "STORED_POSITION_STOP_FRESH_CONTINUATION_TARGET")
                self.assertEqual(gate["target_price"], new_event["target_price"])
                self.assertEqual(gate["entry_geometry"]["stop_price"], original_stop)
                self.assertEqual(db.position["stop_price"], original_stop)
                self.assertEqual(db.position["payload"]["take_price"], original["take_price"])
                self.assertEqual(db.position["payload"]["target_price"], original["target_price"])
                self.assertEqual(db.position["payload"]["runner_target_price"],
                                 new_event["target_price"])
                self.assertEqual(db.position["payload"]["continuation_target_ladder"][0],
                                 original["take_price"])
                self.assertEqual(db.position["payload"]["continuation_target_ladder"][-1],
                                 new_event["target_price"])

    def test_valid_held_target_funds_add_and_keeps_original_and_new_traces(self):
        for direction in ("LONG", "SHORT"):
            with self.subTest(direction=direction):
                row, db, original = self.case(direction)
                new_event = copy.deepcopy(row["timeframe_entry_context"]["event"])
                original_stop = db.position["stop_price"]
                with patch.object(VP, "CANONICAL_ACCOUNTING_OPEN_OR_ADD",
                                  wraps=VP.CANONICAL_ACCOUNTING_OPEN_OR_ADD) as accounting:
                    self.add(row, db)
                    self.assertEqual(accounting.call_count, 1)
                self.assertEqual(len(db.orders), 1)
                self.assertGreater(db.book_fees, 0.)
                order = db.orders[0]["payload"]
                gate = order["fill_economics_gate"]
                self.assertTrue(gate["eligible"], gate)
                self.assertEqual(gate["add_geometry_basis"],
                                 "STORED_POSITION_STOP_FRESH_CONTINUATION_TARGET")
                self.assertEqual(order["target_price"], original["take_price"])
                self.assertEqual(order["runner_target_price"], new_event["target_price"])
                self.assertEqual(gate["target_price"], new_event["target_price"])
                self.assertEqual(gate["entry_geometry"]["stop_price"], original_stop)
                self.assertGreaterEqual(gate["net_reward_risk"], VX.MIN_REWARD_RISK)
                self.assertEqual(row["timeframe_entry_context"]["event"], new_event)
                for stored in (db.position["payload"], db.trade_payload):
                    for key in ("user_teaching_trace", "entry_event_snapshot", "timeframe_entry_context",
                                "r66_event_id", "initial_stop_price", "target_price", "take_price",
                                "execution_horizon", "price_source_lock", "pwin", "pwin_source"):
                        self.assertEqual(stored[key], original[key], key)
                    self.assertEqual(stored["last_add_event_id"], new_event["event_id"])
                    self.assertEqual(stored["last_continuation_event_id"], new_event["event_id"])
                    self.assertEqual(stored["runner_target_price"], new_event["target_price"])
                    self.assertTrue(UT.verify_entry_trace(stored["last_add_teaching_trace"]))
                    self.assertEqual(stored["last_add_teaching_trace"]["timeframe_entry_context"]["event"], new_event)

    def test_missing_or_invalid_stored_geometry_never_uses_the_new_event_as_fallback(self):
        for direction in ("LONG", "SHORT"):
            for field in ("stop", "target"):
                for invalid in (None, 0., "invalid", float("nan")):
                    with self.subTest(direction=direction, field=field, invalid=invalid):
                        row, db, _ = self.case(direction)
                        if field == "stop":
                            db.position["stop_price"] = invalid
                        else:
                            db.position["payload"].update(take_price=invalid, target_price=invalid,
                                last_target_price=row["timeframe_entry_context"]["event"]["target_price"])
                        with patch.object(VP, "CANONICAL_ACCOUNTING_OPEN_OR_ADD",
                                          wraps=VP.CANONICAL_ACCOUNTING_OPEN_OR_ADD) as accounting:
                            self.add(row, db)
                            accounting.assert_not_called()
                        self.assertIn("ADD_STORED_"+field.upper()+"_REQUIRED", row["_execution_audit"]["blockers"])
                        self.assertEqual(db.orders, [])
                        self.assertEqual(db.book_fees, 0.)

    def test_executable_take_alias_remains_immutable_tp1_while_fresh_event_sets_runner(self):
        for direction in ("LONG", "SHORT"):
            with self.subTest(direction=direction):
                row, db, original = self.case(direction, near=True)
                db.position["payload"]["target_price"] = row["timeframe_entry_context"]["event"]["target_price"]
                self.add(row, db)
                self.assertEqual(len(db.orders), 1)
                self.assertEqual(db.orders[0]["payload"]["target_price"], original["take_price"])
                self.assertEqual(db.position["payload"]["take_price"], original["take_price"])
                self.assertEqual(db.position["payload"]["runner_target_price"],
                                 row["timeframe_entry_context"]["event"]["target_price"])

    def test_harvested_tp1_allows_only_a_fresh_farther_breakout_continuation(self):
        for direction in ("LONG", "SHORT"):
            with self.subTest(direction=direction):
                row, db, original = self.case(direction)
                db.position["payload"]["r17_tp1_done"] = True
                db.trade_payload["r17_tp1_done"] = True
                new_event = copy.deepcopy(row["timeframe_entry_context"]["event"])
                original_stop = db.position["stop_price"]
                self.initial_admission(row)
                with patch.object(VP, "CANONICAL_ACCOUNTING_OPEN_OR_ADD",
                                  wraps=VP.CANONICAL_ACCOUNTING_OPEN_OR_ADD) as accounting:
                    self.add(row, db)
                    self.assertEqual(accounting.call_count, 1)
                self.assertEqual(len(db.orders), 1)
                self.assertGreater(db.book_fees, 0.)
                self.assertTrue(db.position["payload"]["r17_tp1_done"])
                self.assertEqual(db.position["payload"]["take_price"], original["take_price"])
                self.assertEqual(db.position["payload"]["runner_target_price"],
                                 new_event["target_price"])
                self.assertEqual(db.position["stop_price"], original_stop)

    def test_explicit_target_argument_cannot_replace_the_actual_stored_target(self):
        row, db, _ = self.case("LONG", near=True)
        gate = VX.entry_gate(row, row["price"], "LONG", .5, db.position,
                             existing_target_price=row["timeframe_entry_context"]["event"]["target_price"])
        self.assertFalse(gate["eligible"])
        self.assertIn("ADD_STORED_TARGET_MISMATCH", gate["blockers"])

    def test_final_accounting_accepts_fresh_continuation_and_preserves_original_plan(self):
        for direction in ("LONG", "SHORT"):
            with self.subTest(direction=direction):
                row, db, original = self.case(direction, near=True)
                original_stop = db.position["stop_price"]
                row["_canonical_admission"] = self.initial_admission(row)
                row.update(_pwin=.65, _pwin_source="TEST_FINAL_BOUNDARY")
                VP.CANONICAL_ACCOUNTING_OPEN_OR_ADD(db, {}, "Aggressive", "NQ", direction,
                    row["price"], .5, 10000., self.clock, row, "TEST_FINAL_ADD_BOUNDARY")
                self.assertEqual(len(db.orders), 1)
                self.assertGreater(db.book_fees, 0.)
                self.assertEqual(db.position["stop_price"], original_stop)
                self.assertEqual(db.position["payload"]["take_price"], original["take_price"])
                self.assertEqual(db.position["payload"]["user_teaching_trace"],
                                 original["user_teaching_trace"])
                self.assertEqual(db.position["payload"]["runner_target_price"],
                                 row["timeframe_entry_context"]["event"]["target_price"])

    def test_refreshed_add_fill_cannot_chase_after_a_timely_initial_admission(self):
        for direction in ("LONG", "SHORT"):
            for extension_atr in (.49, .7):
                with self.subTest(direction=direction, extension_atr=extension_atr):
                    row, db, original = self.case(direction, held_room=8.)
                    row["_canonical_admission"] = self.initial_admission(row)
                    event = copy.deepcopy(row["timeframe_entry_context"]["event"])
                    sign = 1 if direction == "LONG" else -1
                    refreshed = event["trigger_level"]+sign*extension_atr*event["atr"]
                    # .49 ATR is still inside the quote limit; the actual adverse
                    # modeled fill crosses .5 ATR and must be checked as well.
                    row["_execution_quote"] = {"asset":"NQ", "price":refreshed,
                        "source_names":row["source_names"], "observed_at":self.clock.isoformat(),
                        "source_gate_pass":True, "market_open":True}
                    VP.CANONICAL_ACCOUNTING_OPEN_OR_ADD(db, {}, "Aggressive", "NQ", direction,
                        refreshed, .5, 10000., self.clock, row, "TEST_REFRESHED_ADD_FILL")
                    self.assertEqual(db.orders, [])
                    self.assertEqual(db.book_fees, 0.)
                    self.assertEqual(row["_execution_audit"]["hard_blockers"], ["SAME_TF_ENTRY_EXTENDED"])
                    self.assertEqual(row["timeframe_entry_context"]["event"], event)
                    self.assertEqual(db.position["payload"]["user_teaching_trace"], original["user_teaching_trace"])


if __name__ == "__main__":
    unittest.main()
