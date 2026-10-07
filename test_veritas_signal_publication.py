"""Partial signal publication never fabricates freshness or execution facts."""
import ast
from copy import deepcopy
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest

import veritas_signal_publication as P

NOW = "2026-10-07T19:45:00Z"
EARLIER = "2026-10-07T19:44:00Z"
LATER = "2026-10-07T19:45:05Z"
RUNTIME = Path(__file__).with_name("veritas_intelligence.py")


def row(asset="ETH", tf="5m", observed=NOW, event_id="event-1", checked=NOW):
    return {"asset": asset, "horizon": tf, "price": 2650.,
            "research_decision": "LONG", "market_observed_at": observed,
            "source_names": {"primary": "Binance"}, "trade_entry_checked_at": checked,
            "trade_plan": {"eligible": True, "entry_event_id": event_id,
                "timeframe_entry_context": {"event": {"event_id": event_id, "direction": "LONG"}}}}


def executed(value):
    value = deepcopy(value)
    value.update(_breakout_runtime="STRUCTURAL_QUOTE_RUNTIME_V1", snapshot_stale=False)
    event_id = value["trade_plan"]["entry_event_id"]
    value["_execution_audit"] = {"lane": value["_breakout_runtime"], "checked_at": NOW,
        "result": {"status": "OK", "portfolios": [{"name": "Impulse",
            "asset": value["asset"], "horizon": value["horizon"], "event_id": event_id,
            "status": "EXECUTED", "checked_at": NOW, "fill_price": 2650.5,
            "order_id": "actual-order"}]}}
    return value


class SignalPublicationTests(unittest.TestCase):
    def ns(self, rows):
        return {"lock": threading.RLock(), "now": lambda: LATER,
                "last_cycle": {"summary": rows, "at": EARLIER, "telemetry": {"seconds": 123},
                               "portfolio_autopilot": {"status": "PREVIOUS_COMPLETED"}}}

    def test_partial_cell_preserves_neighbor_clocks_stale_flag_and_completed_cycle(self):
        neighbor = dict(row("NQ", observed=EARLIER), snapshot_stale=True)
        ns = self.ns([row(observed=EARLIER), neighbor])
        before = deepcopy(ns["last_cycle"])
        candidate = row()
        self.assertEqual(P.publish_completed(ns, [candidate], "cycle-2", "FULL"), 1)
        last = ns["last_cycle"]
        current = next(r for r in last["summary"] if r["asset"] == "ETH")
        self.assertEqual(current["market_observed_at"], NOW)
        self.assertEqual(current["trade_entry_checked_at"], NOW)
        self.assertFalse(current["snapshot_stale"])
        self.assertIs(next(r for r in last["summary"] if r["asset"] == "NQ"), neighbor)
        self.assertEqual(neighbor, before["summary"][1])
        for field in ("at", "telemetry", "portfolio_autopilot"):
            self.assertEqual(last[field], before[field])
        self.assertEqual(last["signals_updated_at"], LATER)
        self.assertEqual(last["cycle_in_progress"], {"cycle_id": "cycle-2", "cycle_mode": "FULL"})
        self.assertNotIn("snapshot_stale", candidate)

    def test_unknown_or_older_quote_never_displaces_known_observation(self):
        current = executed(row())
        for bad in (dict(row(observed=EARLIER), trade_entry_checked_at=LATER),
                    row(observed=None, checked=LATER), row(observed="invalid", checked=LATER),
                    dict(row(observed=LATER), _execution_quote={"price": 999}),
                    dict(row(observed=LATER), _execution_quote=None)):
            with self.subTest(bad=bad):
                ns = self.ns([current])
                self.assertEqual(P.publish_completed(ns, [bad], "cycle-2", "FULL"), 0)
                self.assertIs(ns["last_cycle"]["summary"][0], current)
                self.assertNotIn("signals_updated_at", ns["last_cycle"])

    def test_equal_quote_newer_completed_rejection_wins_and_keeps_exact_fill(self):
        current = executed(row())
        denied = row(checked=LATER)
        denied.update(trade_entry_eligible=False, trade_entry_reason="STRUCTURAL_EVENT_EXPIRED")
        denied["trade_plan"].update(eligible=False, reason="STRUCTURAL_EVENT_EXPIRED")
        result = P.merge_rows([current], [denied])[0]
        self.assertFalse(result["trade_plan"]["eligible"])
        self.assertEqual(result["trade_entry_checked_at"], LATER)
        self.assertEqual(result["_execution_audit"]["result"]["portfolios"][0]["order_id"], "actual-order")
        self.assertEqual(result["_execution_audit"]["checked_at"], NOW)

    def test_delayed_exact_fill_survives_without_replacing_newer_rejection(self):
        current = row(checked=LATER)
        current["trade_plan"].update(eligible=False, reason="STRUCTURAL_EVENT_EXPIRED")
        for observed in (NOW, EARLIER):
            with self.subTest(observed=observed):
                incoming = executed(row(observed=observed))
                result = P.merge_rows([current], [incoming])[0]
                self.assertFalse(result["trade_plan"]["eligible"])
                self.assertEqual(result["trade_entry_checked_at"], LATER)
                self.assertEqual(result["market_observed_at"], NOW)
                self.assertEqual(result["_execution_audit"]["checked_at"], NOW)
                receipt = result["_execution_audit"]["result"]["portfolios"][0]
                self.assertEqual(receipt["order_id"], "actual-order")
                self.assertEqual(receipt["checked_at"], NOW)
                self.assertIs(P.merge_rows([result], [incoming])[0], result,
                              "a duplicate receipt is not a new publication")
                self.assertNotIn("_execution_audit", current)

    def test_delayed_foreign_fill_cannot_be_attached_to_current_rejection(self):
        current = row(checked=LATER)
        current["trade_plan"].update(eligible=False, reason="STRUCTURAL_EVENT_EXPIRED")
        other_event = executed(row(event_id="event-2"))
        other_direction = executed(row())
        other_direction["research_decision"] = "SHORT"
        other_direction["trade_plan"]["timeframe_entry_context"]["event"]["direction"] = "SHORT"
        other_source = executed(row())
        other_source["source_names"] = {"primary": "Coinbase"}
        other_contract = dict(executed(row()), contract_id="OTHER")
        for incoming in (other_event, other_direction, other_source, other_contract):
            with self.subTest(incoming=incoming):
                result = P.merge_rows([current], [incoming])[0]
                self.assertIs(result, current)
                self.assertNotIn("_execution_audit", result)

    def test_delayed_finish_cannot_mark_live_fast_cell_stale_or_erase_execution(self):
        current = executed(row())
        previous = {"summary": [current], "signals_updated_at": LATER,
                    "cycle_in_progress": {"cycle_id": "cycle-2"}}
        for delayed in (dict(current, snapshot_stale=True), row(observed=EARLIER), row(checked=None)):
            with self.subTest(delayed=delayed):
                state = {"summary": [delayed], "at": LATER}
                P.finish_summary(state, previous)
                self.assertIs(state["summary"][0], current)
                self.assertFalse(state["summary"][0]["snapshot_stale"])
                self.assertFalse(state["cycle_in_progress"])
                self.assertEqual(state["signals_updated_at"], LATER)

    def test_actual_execution_is_not_inherited_by_other_event_direction_or_source(self):
        current = executed(row())
        other_event = row(observed=LATER, checked=LATER, event_id="event-2")
        other_direction = row(observed=LATER, checked=LATER)
        other_direction["research_decision"] = "SHORT"
        other_direction["trade_plan"]["timeframe_entry_context"]["event"]["direction"] = "SHORT"
        other_source = row(observed=LATER, checked=LATER)
        other_source["source_names"] = {"primary": "Coinbase"}
        other_contract = dict(row(observed=LATER, checked=LATER), contract_id="OTHER")
        foreign_proof = row(observed=LATER, checked=LATER)
        foreign_proof["trade_plan"]["timeframe_entry_context"]["source_identity"] = {
            "key": "COINBASE:ETH-USD", "contract_id": None}
        for incoming in (other_event, other_direction, other_source, other_contract, foreign_proof):
            with self.subTest(incoming=incoming):
                result = P.merge_rows([current], [incoming])[0]
                self.assertNotIn("_execution_audit", result)
                self.assertEqual(result["market_observed_at"], LATER)

    def test_publication_and_audit_projection_never_copy_candle_or_archive_graphs(self):
        class DoNotCopy:
            def __deepcopy__(self, memo):
                raise AssertionError("a signal publication copied a proof graph")
        current = executed(row())
        current["_execution_audit"]["result"]["positions"] = DoNotCopy()
        current["_execution_audit"]["result"]["portfolios"][0]["archived_proof"] = DoNotCopy()
        incoming = row(observed=LATER, checked=LATER)
        proof = DoNotCopy()
        incoming["trade_plan"]["timeframe_entry_context"]["sealed_proof"] = proof
        ns = self.ns([current])
        P.publish_completed(ns, [incoming], "cycle-2", "FULL")
        result = ns["last_cycle"]["summary"][0]
        self.assertIs(result["trade_plan"]["timeframe_entry_context"]["sealed_proof"], proof)
        self.assertNotIn("positions", result["_execution_audit"]["result"])
        self.assertNotIn("archived_proof", result["_execution_audit"]["result"]["portfolios"][0])

    def test_matrix_is_bounded_and_deterministically_ordered(self):
        supplied = [row(asset, tf) for tf in reversed(P.HORIZONS) for asset in reversed(P.ASSETS)]
        supplied.extend(row("FOREIGN_"+str(i)) for i in range(200))
        supplied.extend(row() for _ in range(20))
        result = P.merge_rows([], supplied)
        self.assertEqual(len(result), 49)
        self.assertEqual([(r["asset"], r["horizon"]) for r in result],
                         [(a, h) for a in P.ASSETS for h in P.HORIZONS])


class PublicationBeforeBookIntegrationTests(unittest.TestCase):
    def test_actual_cycle_publication_is_visible_through_snapshot_while_book_is_blocked(self):
        tree = ast.parse(RUNTIME.read_text())
        functions = {node.name:node for node in tree.body if isinstance(node, ast.FunctionDef)}
        cycle = functions["cycle"]
        horizon = next(node for node in ast.walk(cycle) if isinstance(node, ast.For)
                       and isinstance(node.target, ast.Name) and node.target.id == "horizon"
                       and isinstance(node.iter, ast.Name) and node.iter.id == "processing_horizons")
        append = next(i for i, node in enumerate(horizon.body) if isinstance(node, ast.Expr)
                      and isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == "summary.append")
        publication = horizon.body[append:append+2]
        self.assertIn("VSP.publish_completed", ast.unparse(publication[-1]))
        book = next(node for node in cycle.body if isinstance(node, ast.If)
                    and ast.unparse(node.test) == "VP is not None and pg_enabled()")
        selected = ast.FunctionDef(name="cycle_slice", args=ast.arguments(posonlyargs=[], args=[],
            kwonlyargs=[], kw_defaults=[], defaults=[]), body=[*publication, book], decorator_list=[])
        block_entered, release, done = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        failures = []

        def step_all(**kwargs):
            block_entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release portfolio accounting")
            return {"status": "OK"}

        initial = [dict(row(a, h, observed=EARLIER), snapshot_stale=True)
                   for a in P.ASSETS for h in P.HORIZONS]
        ns = {"VSP": P, "lock": threading.RLock(), "summary": [], "z": row(),
              "last_cycle": {"summary": initial, "at": EARLIER, "telemetry": {"completed": True},
                             "portfolio_autopilot": {"status": "PREVIOUS_COMPLETED"}},
              "_v90_compact_live_row": lambda value:dict(value), "cycle_id": "cycle-2", "cycle_mode": "FULL",
              "now": lambda:LATER, "VP": SimpleNamespace(step_all=step_all), "pg_enabled": lambda:True,
              "pg_connect": lambda:None, "VERSION": "TEST", "VX": SimpleNamespace(VC=SimpleNamespace(COMMISSION_RATE=.0005)),
              "_v90_emit_portfolio": lambda *args, **kwargs:None,
              "DISPLAY_ASSETS": P.ASSETS, "HORIZONS": P.HORIZONS,
              "latest_signal_summary_pg": lambda:self.fail("complete memory must not read PostgreSQL")}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[selected, functions["fresh_cycle_snapshot"]],
                             type_ignores=[])), str(RUNTIME), "exec"), ns)

        def run():
            try:
                ns["cycle_slice"]()
            except BaseException as exc:
                failures.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=run, daemon=True)
        try:
            thread.start()
            self.assertTrue(block_entered.wait(2), failures)
            snapshot = ns["fresh_cycle_snapshot"]()
            target = next(r for r in snapshot["summary"] if r["asset"] == "ETH" and r["horizon"] == "5m")
            self.assertEqual(target["market_observed_at"], NOW)
            self.assertFalse(target["snapshot_stale"])
            self.assertEqual(snapshot["at"], EARLIER)
            self.assertEqual(snapshot["telemetry"], {"completed": True})
            self.assertEqual(snapshot["portfolio_autopilot"], {"status": "PREVIOUS_COMPLETED"})
            self.assertEqual(snapshot["signals_updated_at"], LATER)
            self.assertTrue(snapshot["cycle_in_progress"])
            self.assertFalse(done.is_set(), "fixture book phase should remain blocked")
        finally:
            release.set()
            thread.join(2)
        if failures:
            raise failures[0]


if __name__ == "__main__":
    unittest.main()
