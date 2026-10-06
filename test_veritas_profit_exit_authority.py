"""Discretionary harvest authority; protective exits retain their independent lane."""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import veritas_portfolio as VP
import veritas_portfolio_runtime as R
import veritas_position_guard as G


class Result:
    def __init__(self, row=None, rows=None):
        self.row, self.rows = row, rows or []
    def fetchone(self):
        return self.row
    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, trade=None, position=None):
        self.trade, self.position, self.writes = trade, position, []
    def execute(self, sql, args=None):
        if sql.startswith("SELECT * FROM paper_trades"):
            return Result(self.trade)
        if sql.startswith("SELECT * FROM paper_positions"):
            return Result(rows=[self.position] if self.position else [])
        if sql.lstrip().startswith(("UPDATE", "INSERT", "DELETE")):
            self.writes.append((sql, args))
        return Result()


class ProfitExitAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc).isoformat()
        self.z = {"asset":"ETH", "direction":"LONG", "units":1000.,
                  "avg_entry_price":100., "active_trade_id":"T", "stop_price":98.,
                  "payload":{}}
        self.trade = {"trade_id":"T", "gross_pnl_rub":0., "fees_rub":40.,
                      "funding_rub":0., "max_fraction":.10}

    def quote(self, price):
        return {"asset":"ETH", "price":price, "observed_at":self.now,
                "source_gate_pass":True, "source_names":{"primary":"Binance spot"}}

    def invoke(self, reason, trade=None, price=100.2, target=.05, direction="LONG"):
        z = dict(self.z, direction=direction)
        c = Connection(self.trade if trade is None else trade, z)
        with patch.object(G, "quote_for_position", return_value=self.quote(price)), \
             patch.object(VP, "CANONICAL_ACCOUNTING_CLOSE_OR_REDUCE", return_value=.05) as mutate:
            result = R.canonical_close_or_reduce(
                c, {}, "Champion", z, price, target, 1e6, self.now, reason)
        return result, mutate, c

    def test_negative_cycle_blocks_every_discretionary_partial_in_both_directions(self):
        reasons = ("TAKE_PROFIT_PARTIAL_R17", "DYNAMIC_PARTIAL_PROFIT",
                   "PROFIT_HARVEST", "R33_MFE_GIVEBACK_HARVEST",
                   "R46_MFE_GIVEBACK_HARVEST")
        trade = dict(self.trade, funding_rub=200.)
        for direction, price in (("LONG",100.2), ("SHORT",99.8)):
            for reason in reasons:
                with self.subTest(direction=direction, reason=reason):
                    result, mutate, c = self.invoke(reason, trade, price, direction=direction)
                    self.assertEqual(result, 0.)
                    mutate.assert_not_called()
                    self.assertEqual(c.writes, [])

    def test_unknown_paid_costs_do_not_approve_profit_partial(self):
        for missing in ("gross_pnl_rub", "fees_rub", "funding_rub"):
            trade = {k:v for k,v in self.trade.items() if k != missing}
            with self.subTest(missing=missing):
                result, mutate, _ = self.invoke("PROFIT_HARVEST", trade, price=102.)
                self.assertEqual(result, 0.)
                mutate.assert_not_called()

    def test_positive_partial_keeps_requested_exposure_and_original_reason(self):
        for direction, price in (("LONG",102.), ("SHORT",98.)):
            with self.subTest(direction=direction):
                result, mutate, _ = self.invoke("DYNAMIC_PARTIAL_PROFIT",
                    price=price, direction=direction)
                self.assertEqual(result, .05)
                self.assertEqual(mutate.call_count, 1)
                self.assertEqual(mutate.call_args.args[5], .05)
                self.assertEqual(mutate.call_args.args[8], "DYNAMIC_PARTIAL_PROFIT")

    def test_whole_cycle_projection_counts_paid_costs_and_prior_realization_once(self):
        trade = dict(self.trade, gross_pnl_rub=-125., fees_rub=80., funding_rub=15.)
        q = self.quote(101.)
        with patch.object(G, "quote_for_position", return_value=q):
            assessment = G.profit_exit_assessment(self.z, q, trade, 1e6)
        # Entry is already an executed average fill. Charge only adverse EXIT fill
        # plus residual exit commission; never charge entry slippage again.
        fill = 101. * (1. - .0004)
        expected = -125. + 1000. * (fill - 100.) - 80. - 15. - 1000. * fill * .0004
        self.assertTrue(assessment["eligible"], assessment)
        self.assertAlmostEqual(assessment["net_pnl_rub"], expected, places=7)
        self.assertAlmostEqual(assessment["exit_fee_rub"], 1000. * fill * .0004, places=7)

    def test_prior_realized_loss_cannot_be_hidden_by_positive_remaining_price_move(self):
        trade = dict(self.trade, gross_pnl_rub=-200.)
        result, mutate, _ = self.invoke("PROFIT_HARVEST", trade)
        self.assertEqual(result, 0.)
        mutate.assert_not_called()

    def test_hard_exits_and_partial_exposure_reduction_do_not_require_profit(self):
        requests = [(r,0.) for r in ("STOP","HARD_THESIS_INVALIDATION",
                    "STRUCTURE_BREAK","STRUCTURE_EXHAUSTION",
                    "PORTFOLIO_HARD_STOP","RISK_HARD_STOP")]
        requests += [(r,.05) for r in ("SOFT_SIZE_REDUCTION","EDGE_DECAY",
                     "RISK_REDUCTION","TRAILING_REDUCTION")]
        for reason, target in requests:
            with self.subTest(reason=reason), \
                 patch.object(G, "profit_exit_assessment",
                              side_effect=AssertionError("Risk exit entered profit guard")), \
                 patch.object(G, "protective_reason", return_value="STOP"):
                result, mutate, _ = self.invoke(reason, {}, price=99., target=target)
                self.assertEqual(result, .05)
                self.assertEqual(mutate.call_count, 1)

    def test_full_take_profit_still_keeps_runner_after_net_approval(self):
        result, mutate, _ = self.invoke("TAKE_PROFIT", price=102., target=0.)
        self.assertEqual(result, .05)
        self.assertGreater(mutate.call_args.args[5], 0.)
        self.assertEqual(mutate.call_args.args[8], "TAKE_PROFIT_PARTIAL_CTC_V2")

    def test_r46_cannot_bypass_canonical_net_check_or_mark_unexecuted_harvest(self):
        z = dict(self.z, payload={"mfe_pct":.50})
        c = Connection(dict(self.trade, funding_rub=200.), z)
        with patch.object(G, "quote_for_position", return_value=self.quote(100.2)), \
             patch.object(VP, "_v90r46_base_close_or_reduce", return_value=.05) as legacy, \
             patch.object(VP, "CANONICAL_ACCOUNTING_CLOSE_OR_REDUCE", return_value=.05) as mutate:
            result = R._v90r46_giveback_harvest(c, {}, "Champion", {"ETH":100.2}, 1e6, self.now)
        self.assertEqual(result, [])
        legacy.assert_not_called()
        mutate.assert_not_called()
        self.assertEqual(c.writes, [])

    def test_r46_positive_harvest_reaches_canonical_accounting_once(self):
        z = dict(self.z, payload={"mfe_pct":.80})
        c = Connection(self.trade, z)
        with patch.object(G, "quote_for_position", return_value=self.quote(100.4)), \
             patch.object(VP, "_v90r46_base_close_or_reduce", return_value=.05) as legacy, \
             patch.object(VP, "CANONICAL_ACCOUNTING_CLOSE_OR_REDUCE", return_value=.05) as mutate:
            result = R._v90r46_giveback_harvest(c, {}, "Champion", {"ETH":100.4}, 1e6, self.now)
        self.assertEqual(len(result), 1)
        legacy.assert_not_called()
        self.assertEqual(mutate.call_count, 1)
        self.assertEqual(mutate.call_args.args[8], "R46_MFE_GIVEBACK_HARVEST")

    def test_r7_does_not_mark_a_blocked_harvest_as_done(self):
        c = Connection(self.trade, self.z)
        with patch.object(VP, "_portfolio_rows", return_value=({}, [self.z])), \
             patch.object(VP, "_mark_nav", return_value=(1e6,0.,0.,0.)), \
             patch.object(VP, "_v90ph_next_level",
                          return_value={"timeframe":"1h","price":101.1,"distance_pct":.001}), \
             patch.object(VP, "_v90ph_strength", return_value=1), \
             patch.object(VP, "_v90ph_take_fraction", return_value=.50), \
             patch.object(VP, "_close_or_reduce", return_value=0.) as close:
            changes = VP._v90ph_apply(c, "Champion", {"ETH":{}}, {"ETH":101.}, self.now)
        self.assertEqual(close.call_count, 1)
        self.assertEqual(changes, [])
        self.assertEqual(c.writes, [])

    def test_r33_does_not_mark_a_blocked_harvest_as_done(self):
        z = dict(self.z, payload={"mfe_pct":.60})
        c = Connection(self.trade, z)
        with patch.object(VP, "_v90j_update_excursions"), \
             patch.object(VP, "_close_or_reduce", return_value=0.) as close:
            changes = VP._v90r33_harvest(c, {}, "Champion", {"ETH":100.25}, 1e6, self.now)
        self.assertEqual(close.call_count, 1)
        self.assertEqual(changes, [])
        self.assertEqual(c.writes, [])


if __name__ == "__main__":
    unittest.main()
