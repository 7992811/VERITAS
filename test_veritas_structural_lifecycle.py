"""Structural paper lifecycle through real admission, fills and accounting.

The CNY entry observations replay recorded minute closes; the later target/stop
quotes are explicitly synthetic lifecycle scenarios, not recovered fills. SQL
table semantics are in memory. No provider, broker, notification sender, full
research loop or external database is contacted by these tests.
"""
from contextlib import ExitStack, contextmanager, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import re
import threading
import time
import unittest
from unittest.mock import patch

import veritas_canonical_constitution as CTC
import veritas_canonical_runtime as VCR
import veritas_costs as VC
import veritas_direct_cny as VDC
import veritas_portfolio as VP
import veritas_portfolio_runtime as VPR
import veritas_position_guard as VPG
import veritas_price_source as VPS
import veritas_structural_breakout as SB
import veritas_structural_lifecycle as VSL
import veritas_timeframe_policy as TFP
from test_veritas_structural_cny_episodes import episode_raw


class Result:
    def __init__(self, rows=()):
        self.rows = deepcopy(list(rows))

    def fetchone(self):
        return deepcopy(self.rows[0]) if self.rows else None

    def fetchall(self):
        return deepcopy(self.rows)


class PaperBook:
    """Strict table/JSON-merge semantics, with rollback of nested transactions.

    Unexpected SQL is recorded as well as raised: legacy optional telemetry
    catches exceptions, and must not make an incomplete test silently pass.
    This deliberately makes no claim to emulate PostgreSQL concurrency.
    """
    def __init__(self):
        self.portfolios = {"Currency": {
            "name": "Currency", "initial_nav_rub": 10000., "realized_pnl_rub": 0.,
            "fees_rub": 0., "funding_rub": 0., "high_water_nav_rub": 10000.,
            "last_mark_at": None, "last_ruonia": None, "last_usdrub": None,
            "benchmark_nav_rub": 10000., "payload": {}}}
        self.positions, self.trades, self.orders, self.nav_history = {}, {}, [], []
        self.queries, self.unsupported = [], []
        self.depth, self.book_locks, self.commits = 0, 0, 0
        self.fail_order_insert = False
        self.try_lock_granted = True

    def data(self):
        return deepcopy((self.portfolios, self.positions, self.trades, self.orders, self.nav_history))

    @contextmanager
    def transaction(self):
        before = self.data()
        self.depth += 1
        try:
            yield self
        except Exception:
            self.portfolios, self.positions, self.trades, self.orders, self.nav_history = before
            raise
        else:
            if self.depth == 1:
                self.commits += 1
        finally:
            self.depth -= 1

    @contextmanager
    def connect(self):
        yield self

    def position(self):
        return deepcopy(self.positions.get(("Currency", "CNYRUBF")))

    @staticmethod
    def _merge(row, value, merge):
        value = json.loads(value)
        row["payload"] = dict(row.get("payload") or {}, **value) if merge else value

    def execute(self, sql, args=()):
        q = " ".join(sql.split())
        self.queries.append((q, args))
        if q.startswith("SET LOCAL lock_timeout"):
            assert self.depth > 0
            return Result()
        if q.startswith("SELECT pg_advisory_xact_lock"):
            assert self.depth > 0 and args == (VPG.BOOK_LOCK_ID,)
            self.book_locks += 1
            return Result()
        if q.startswith("SELECT pg_try_advisory_xact_lock"):
            assert self.depth > 0 and args == (VPG.BOOK_LOCK_ID,)
            self.book_locks += int(self.try_lock_granted)
            return Result([{'acquired':self.try_lock_granted}])
        if q.startswith("SELECT 1 AS ok FROM paper_orders"):
            if "client_order_id=%s" in q:
                found = any(order["client_order_id"] == args[0] for order in self.orders)
            else:
                found = any((order["portfolio_name"], order["asset"], order["side"],
                             order["payload"].get("entry_event_id")) == args for order in self.orders)
            return Result([{"ok": 1}] if found else [])
        if q.startswith("SELECT 1 AS ok FROM paper_trades"):
            found = any((t["portfolio_name"], t["asset"], t["direction"],
                         t["payload"].get("r66_event_id")) == args and t["status"] == "CLOSED"
                        for t in self.trades.values())
            return Result([{"ok": 1}] if found else [])
        if q.startswith("SELECT") and " FROM paper_positions" in q:
            rows = list(self.positions.values())
            if "portfolio_name=%s AND asset=%s" in q:
                rows = [r for r in rows if (r["portfolio_name"], r["asset"]) == args]
            elif "portfolio_name=%s" in q:
                rows = [r for r in rows if r["portfolio_name"] == args[0]]
            elif "active_trade_id=%s" in q:
                rows = [r for r in rows if r["active_trade_id"] == args[0]]
            return Result(rows)
        if q.startswith("SELECT") and " FROM paper_portfolios" in q:
            row = self.portfolios.get(args[0])
            return Result([row] if row else [])
        if q.startswith("SELECT") and " FROM paper_trades" in q:
            row = self.trades.get(args[0])
            return Result([row] if row else [])
        if q.startswith(('UPDATE paper_positions AS target', 'UPDATE paper_trades AS target')) and 'jsonb_to_recordset' in q:
            rows = self.positions.values() if q.startswith('UPDATE paper_positions') else self.trades.values()
            key = 'active_trade_id' if q.startswith('UPDATE paper_positions') else 'trade_id'
            deltas = {item['trade_id']: item['patch'] for item in json.loads(args[0])}
            for row in rows:
                if row.get(key) in deltas:
                    row['payload'] = dict(row.get('payload') or {}, **deltas[row[key]])
            return Result()
        if q.startswith("INSERT INTO paper_nav_history"):
            self.nav_history.append(deepcopy(args))
            return Result()
        if q.startswith("INSERT INTO paper_"):
            match = re.match(r"INSERT INTO (paper_\w+)\(([^)]+)\)", q)
            if match:
                table, columns = match.groups()
                row = dict(zip(columns.split(","), args))
                if "payload" in row:
                    row["payload"] = json.loads(row["payload"])
                if table == "paper_orders":
                    if self.fail_order_insert:
                        raise RuntimeError("simulated order insert failure")
                    self.orders.append(row)
                elif table == "paper_positions":
                    self.positions[(row["portfolio_name"], row["asset"])] = row
                elif table == "paper_trades":
                    row.update(gross_pnl_rub=0., funding_rub=0., net_pnl_rub=0., closed_at=None)
                    self.trades[row["trade_id"]] = row
                else:
                    raise AssertionError(table)
                return Result()
        if q.startswith("DELETE FROM paper_positions"):
            self.positions.pop(tuple(args), None)
            return Result()
        if q.startswith("UPDATE paper_portfolios"):
            row = self.portfolios[args[-1]]
            if "SET realized_pnl_rub=" in q:
                row["realized_pnl_rub"] += args[0]
                row["fees_rub"] += args[1]
                row["updated_at"] = args[2]
            elif "SET fees_rub=" in q:
                row["fees_rub"] += args[0]
                row["updated_at"] = args[1]
            elif "SET last_mark_at=" in q:
                row["last_mark_at"] = args[0]
            elif "SET high_water_nav_rub=" in q:
                row.update(high_water_nav_rub=args[0], updated_at=args[1])
            elif "SET funding_rub=" in q:
                row['funding_rub'] += args[0]
            else:
                self.unsupported.append(q)
                raise AssertionError(q)
            return Result()
        if q.startswith("UPDATE paper_positions SET units="):
            row = self.positions[tuple(args[-2:])]
            if "avg_entry_price=%s" in q:
                row.update(units=args[0], avg_entry_price=args[1], last_price=args[2],
                           target_fraction=args[3], updated_at=args[4], payload=json.loads(args[5]))
            else:
                row.update(units=args[0], last_price=args[1], target_fraction=args[2], updated_at=args[3])
                self._merge(row, args[4], True)
            return Result()
        if q.startswith("UPDATE paper_positions SET payload="):
            rows = ([r for r in self.positions.values() if r["active_trade_id"] == args[-1]]
                    if "active_trade_id=%s" in q else [self.positions[tuple(args[-2:])]])
            for row in rows:
                self._merge(row, args[0], "||" in q)
            return Result()
        if q.startswith("UPDATE paper_trades"):
            row = self.trades[args[-1]]
            if "SET payload=" in q:
                self._merge(row, args[0], "||" in q)
            elif "SET fees_rub=" in q:
                row["fees_rub"] += args[0]
                row["max_fraction"] = max(row["max_fraction"], args[1])
                self._merge(row, args[2], True)
            elif "SET gross_pnl_rub=" in q:
                row["gross_pnl_rub"] += args[0]
                row["fees_rub"] += args[1]
            elif "SET funding_rub=" in q:
                row['funding_rub'] += args[0]
            elif "SET closed_at=" in q:
                fields = ("closed_at", "avg_exit_price", "net_pnl_rub", "return_on_entry_nav",
                          "profitable", "meaningful_win", "status")
                row.update(zip(fields, args[:7]))
                self._merge(row, args[7], True)
            else:
                self.unsupported.append(q)
                raise AssertionError(q)
            return Result()
        self.unsupported.append(q)
        raise AssertionError("Unexpected paper accounting SQL: " + q)


def observed_rows(horizon="1m"):
    state, result = None, []
    for episode, stamp, price in ((0, "2026-10-07T04:00:00Z", 12.722),
                                  (0, "2026-10-07T04:01:00Z", 12.736),
                                  (1, "2026-10-07T07:14:00Z", 12.752),
                                  (1, "2026-10-07T07:15:00Z", 12.766)):
        raw = episode_raw(episode, stamp, price)
        context = SB.build_context(raw, horizon, stamp, state=state, config=CTC.BREAKOUT_LIFECYCLE_POLICY)
        state = context["quote_state"]
        event = context.get("event") or {}
        quote = {key: deepcopy(raw[key]) for key in (*VPS.QUOTE_FIELDS, "asset", "observed_at") if key in raw}
        row = TFP.prepare_row(dict(raw, horizon=horizon, research_decision=event.get("direction", "NO_TRADE"),
                                   market_observed_at=stamp, timeframe_entry_context=context,
                                   trade_plan={}, _execution_quote=quote, _execution_audit={},
                                   _runtime_quote_refresh=False), now=stamp)
        result.append(row)
    return result


def synthetic_higher_break(trigger, at=None):
    """A proved new parent level above the held CNY target, with higher zones.

    These bars are constructed lifecycle input, explicitly separate from the
    downloaded CNY episodes. The real builder, source checks, proof seal and
    economics create/validate the event; no seal or PASS result is hand-written.
    """
    from test_veritas_structural_breakout import raw_at, bars, CLOSES
    at=at or datetime(2026,10,7,7,16,0,100000,tzinfo=timezone.utc)
    factor=(trigger-12.693)/5.
    hourly=bars(CLOSES,'1h',at-timedelta(hours=len(CLOSES),milliseconds=100))
    raw,_=raw_at(at=at,hourly=hourly)
    for sequence in raw['structure_bars_by_timeframe'].values():
        for bar in sequence:
            for key in ('open','high','low','close'):
                bar[key]=12.693+(bar[key]-95.5)*factor
    raw.update(price=12.693+5.5*factor,source='TBANK_GRPC CNYRUBF',paper_eligible=True,
               contract={'instrument_uid':'c300543d-aa18-4249-b110-615409dde036','normalization_factor':1.})
    raw.pop('best_bid'); raw.pop('best_ask')
    source=VPS.identity('CNYRUBF',raw)
    raw['structure_source_identity']=source
    for sequence in raw['structure_bars_by_timeframe'].values():
        for bar in sequence:
            bar['source_identity']=source
    context=SB.build_context(raw,'1m',at,config=CTC.BREAKOUT_LIFECYCLE_POLICY)
    quote={key:deepcopy(raw[key]) for key in (*VPS.QUOTE_FIELDS,'asset','observed_at') if key in raw}
    return TFP.prepare_row(dict(raw,horizon='1m',research_decision='LONG',timeframe_entry_context=context,
                                _execution_quote=quote,_execution_audit={},_runtime_quote_refresh=False),now=at)


class StructuralLifecycleAccountingTests(unittest.TestCase):
    def setUp(self):
        self.db = PaperBook()
        self.seed, self.first, self.expired, self.add = observed_rows()
        self.clock = VPG.utc_datetime(self.first["observed_at"])
        self.ns = {"pg_connect": self.db.connect, "lock": threading.RLock(), "last_cycle": {}}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        # The outbox sender/provider are outside this accounting test. Quotes
        # still pass through the real pinned-source/time/price gates.
        self.stack.enter_context(patch.object(VP.VCN, "enqueue_order"))
        self.stack.enter_context(patch.object(VDC, "quote", return_value={}))
        self.stack.enter_context(patch.object(VPG.VPP, "refresh", return_value={}))
        with VPG._quotes_lock:
            self.prior_quotes = deepcopy((VPG._quotes, VPG._source_quotes, VPG._market_state))
            VPG._quotes.clear(); VPG._source_quotes.clear(); VPG._market_state.clear()
        self.addCleanup(self.restore_quotes)
        original_mark = VPG.position_mark_price
        self.stack.enter_context(patch.object(VPG, "position_mark_price",
            side_effect=lambda position, now=None: original_mark(position, self.clock if now is None else now)))

    def restore_quotes(self):
        with VPG._quotes_lock:
            for target, saved in zip((VPG._quotes, VPG._source_quotes, VPG._market_state), self.prior_quotes):
                target.clear(); target.update(saved)

    def tearDown(self):
        self.assertEqual(self.db.unsupported, [], "A swallowed optional SQL failure invalidates this test")

    def nav(self):
        p, positions = VPR._portfolio_rows(self.db, "Currency")
        return VPR._mark_nav(p, positions, {})[0]

    def run_fast(self, row):
        self.clock = VPG.utc_datetime(row["observed_at"])
        VPG.publish_quote(row["asset"], VPS.quote_from_row(row))
        return VSL.fast_entry_pass(self.ns, [row], self.clock)

    def open(self):
        result = self.run_fast(self.first)
        audit = result["portfolios"][0]["admission_trace"][0]["execution"]
        self.assertEqual(audit["status"], "EXECUTED", audit)
        self.assertEqual(audit["execution_action"], "OPEN")
        self.assertTrue(VSL.owns_position(self.db.position()))
        return self.db.position()

    def synthetic_quote(self, price, stamp):
        quote = VPS.quote_from_row(self.first)
        quote.update(price=price, observed_at=stamp, market_open=True, source_gate_pass=True)
        self.clock = VPG.utc_datetime(stamp)
        VPG.publish_quote("CNYRUBF", quote)
        return quote

    def test_observed_open_then_new_local_high_add_retains_parent_and_entry_evidence(self):
        opened = self.open()
        before = deepcopy(opened["payload"])
        self.assertEqual(before["entry_event_snapshot"]["stop_anchor"], 12.693)
        self.assertAlmostEqual(opened["stop_price"], 12.68784)
        result = self.run_fast(self.add)
        execution = result["portfolios"][0]["admission_trace"][0]["execution"]
        self.assertEqual(execution["status"], "EXECUTED", execution)
        self.assertEqual(execution["execution_action"], "ADD")
        position = self.db.position()
        self.assertGreater(position["units"], opened["units"])
        self.assertEqual(position["active_trade_id"], opened["active_trade_id"])
        self.assertEqual(position["stop_price"], opened["stop_price"])
        event = self.add["timeframe_entry_context"]["event"]
        self.assertEqual(event["trigger_level"], 12.756)
        self.assertEqual(event["parent_event_id"], before["r66_event_id"])
        self.assertEqual(event["stop_anchor"], 12.693)
        immutable = ("entry_event_snapshot", "entry_decision_snapshot", "timeframe_entry_context",
                     "r66_event_id", "initial_stop_price", "initial_take_price", "initial_target_ladder",
                     "user_teaching_trace", "entry_stop_risk_budget", "entry_geometry_audit")
        trade = self.db.trades[position["active_trade_id"]]
        for stored in (position["payload"], trade["payload"]):
            for key in immutable:
                self.assertEqual(stored[key], before[key], key)
            self.assertEqual(stored["last_add_event_id"], event["event_id"])
            self.assertEqual(stored["active_target_event_snapshot"], event)
            self.assertAlmostEqual(stored["active_ladder_units"], position["units"])
        order = self.db.orders[-1]
        self.assertAlmostEqual(order["fraction_nav"], .25)
        self.assertAlmostEqual(order["fee_rub"], order["notional_rub"]*.0004)
        self.assertEqual(len(self.db.orders), 2)
        self.assertEqual(self.db.book_locks, 2)

    def test_requested_large_add_reserves_held_risk_within_owner_fifteen_percent(self):
        self.open()
        self.clock = VPG.utc_datetime(self.add["observed_at"])
        VPG.publish_quote("CNYRUBF", VPS.quote_from_row(self.add))
        row = deepcopy(self.add)
        with VPG.book_transaction(self.db):
            VPR.canonical_open_or_add(self.db, self.db.portfolios["Currency"], "Currency", "CNYRUBF",
                "LONG", row["price"], 10., self.nav(), self.clock.isoformat(), row, "LARGE_ADD_RISK_TEST")
        self.assertEqual(row["_execution_audit"]["status"], "EXECUTED", row["_execution_audit"])
        budget = self.db.orders[-1]["payload"]["stop_risk_budget"]
        self.assertGreater(budget["existing_position_risk"]["net_stop_risk_nav"], 0.)
        self.assertLessEqual(budget["total_stop_risk_nav_after"], .15+1e-12)
        self.assertGreater(budget["total_stop_risk_nav_after"], .019)
        self.assertLess(budget["fraction"], 10.)
        self.assertAlmostEqual(budget["risk_cap_nav"], .15)
        # The cap reduces the increment; it does not move the protected stop.
        self.assertAlmostEqual(self.db.position()["stop_price"], 12.68784)

    def test_order_journal_blocks_reusing_an_add_after_mutable_last_id_is_lost(self):
        self.open()
        self.run_fast(self.add)
        position = self.db.positions[("Currency", "CNYRUBF")]
        position["payload"].pop("last_add_event_id")
        position["payload"].pop("last_add_teaching_trace")
        before = self.db.data()
        row = deepcopy(self.add)
        VPR.canonical_open_or_add(self.db, self.db.portfolios["Currency"], "Currency", "CNYRUBF", "LONG",
            row["price"], 1., self.nav(), self.clock.isoformat(), row, "REUSED_ADD")
        self.assertEqual(row["_execution_audit"]["reason"], "EVENT_REUSE_WITHOUT_NEW_CONFIRMATION")
        self.assertEqual(self.db.data(), before)

    def test_expired_entry_event_holds_existing_structure_and_never_becomes_exit(self):
        opened = self.open()
        self.assertFalse(VCR.evaluate(self.expired, CTC.runtime_portfolio_policy("Currency"),
                                      0., self.expired["observed_at"])["open"])
        result = self.run_fast(self.expired)
        trace = result["portfolios"][0]["admission_trace"][0]
        self.assertEqual(trace["execution"]["status"], "HELD")
        self.assertEqual(trace["execution"]["execution_action"], "HOLD")
        self.assertEqual(trace["execution"]["reason"], "STRUCTURE_INTACT_WAIT_NEW_LEVEL")
        self.assertTrue(trace["admission"]["hard_veto"])
        self.assertFalse(trace["hard_veto"])  # The held position has no exit veto.
        self.assertEqual(self.db.position()["units"], opened["units"])
        self.assertEqual(self.db.position()["stop_price"], opened["stop_price"])
        self.assertEqual(len(self.db.orders), 1)
        self.assertIsNone(VPG.protective_reason(self.db.position(), VPS.quote_from_row(self.expired), self.clock))

    def test_guard_tp1_reduces_half_actual_units_then_tp2_closes_only_the_remainder(self):
        self.open()
        self.run_fast(self.add)
        before = self.db.position()
        original = deepcopy(before["payload"]["entry_event_snapshot"])
        first, second = [item["price"] for item in VSL.active_ladder(before)]
        self.assertEqual((first, second), (12.805, 12.845))
        q1 = self.synthetic_quote(first, "2026-10-07T07:16:00Z")
        result = VPG.run_protective_pass(VP, self.db.connect, {"CNYRUBF": q1}, self.clock)
        self.assertTrue(any(item["reason"] == "TAKE_PROFIT" for item in result), result)
        residual = self.db.position()
        self.assertIsNotNone(residual)
        self.assertAlmostEqual(residual["units"], before["units"]*.5)
        self.assertEqual(residual["payload"]["active_target_stage"], 1)
        self.assertEqual(VSL.active_target_price(residual), second)
        self.assertEqual(residual["payload"]["entry_event_snapshot"], original)
        self.assertEqual(self.db.orders[-1]["reason"], "TAKE_PROFIT_STRUCTURAL_PARTIAL")
        self.assertAlmostEqual(self.db.orders[-1]["payload"]["closed_normalized_units"], before["units"]*.5)
        again = self.synthetic_quote(first+.001, "2026-10-07T07:16:05Z")
        self.assertIsNone(VPG.protective_reason(residual, again, self.clock))
        q2 = self.synthetic_quote(second, "2026-10-07T07:17:00Z")
        VPG.run_protective_pass(VP, self.db.connect, {"CNYRUBF": q2}, self.clock)
        self.assertIsNone(self.db.position())
        self.assertEqual(self.db.orders[-1]["reason"], "TAKE_PROFIT_STRUCTURAL_FINAL")
        self.assertAlmostEqual(self.db.orders[-1]["payload"]["closed_normalized_units"], before["units"]*.5)
        trade = self.db.trades[before["active_trade_id"]]
        self.assertEqual(trade["status"], "CLOSED")
        self.assertEqual(trade["payload"]["entry_event_snapshot"], original)
        self.assertAlmostEqual(trade["fees_rub"], sum(order["fee_rub"] for order in self.db.orders))
        self.assertAlmostEqual(trade["net_pnl_rub"], trade["gross_pnl_rub"]-trade["fees_rub"]-trade["funding_rub"])

    def test_structural_stop_remains_active_after_entry_window_and_rejects_foreign_uid(self):
        opened = self.open()
        quote = self.synthetic_quote(opened["stop_price"]-.001, "2026-10-07T07:16:00Z")
        foreign = deepcopy(quote)
        foreign["contract"]["instrument_uid"] = "different-contract"
        self.assertIsNone(VPG.protective_reason(opened, foreign, self.clock))
        self.assertEqual(VPG.protective_reason(opened, quote, self.clock), "STOP")
        VPG.run_protective_pass(VP, self.db.connect, {"CNYRUBF": quote}, self.clock)
        self.assertIsNone(self.db.position())
        self.assertEqual(self.db.orders[-1]["reason"], "STOP")

    def test_fast_new_break_at_old_tp1_takes_half_before_adding_and_replanning(self):
        opened=self.open()
        old_payload=deepcopy(opened['payload'])
        row=synthetic_higher_break(12.803)
        self.assertTrue(VCR.evaluate(row,CTC.runtime_portfolio_policy('Currency'),0.,row['observed_at'])['open'])
        result=self.run_fast(row)
        self.assertEqual([order['side'] for order in self.db.orders],['BUY','SELL','BUY'])
        self.assertEqual(self.db.orders[1]['reason'],'TAKE_PROFIT_STRUCTURAL_PARTIAL')
        closed=self.db.orders[1]['payload']['closed_normalized_units']
        added=self.db.orders[2]['notional_rub']/self.db.orders[2]['price']
        self.assertAlmostEqual(closed,opened['units']*.5)
        position=self.db.position()
        self.assertEqual(position['active_trade_id'],opened['active_trade_id'])
        self.assertAlmostEqual(position['units'],opened['units']-closed+added)
        self.assertEqual(position['payload']['entry_event_snapshot'],old_payload['entry_event_snapshot'])
        self.assertEqual(position['payload']['initial_target_ladder'],old_payload['initial_target_ladder'])
        history=position['payload']['target_lifecycle_history']
        self.assertEqual([entry['action'] for entry in history],['TARGET_PARTIAL','CONFIRMED_ADD_REPLANS_REMAINING_TARGETS'])
        self.assertGreater(VSL.active_target_price(position),12.845)
        trace=result['portfolios'][0]['admission_trace'][0]
        self.assertEqual(trace['execution']['execution_action'],'ADD')
        self.assertEqual(trace['preceding_protection']['status'],'EXECUTED')
        self.assertAlmostEqual(trace['preceding_protection']['closed_normalized_units'],closed)

    def test_direct_canonical_add_cannot_bypass_an_already_reached_old_target(self):
        opened=self.open()
        row=synthetic_higher_break(12.803)
        self.clock=VPG.utc_datetime(row['observed_at'])
        VPG.publish_quote('CNYRUBF',VPS.quote_from_row(row))
        before=self.db.data()
        with VPG.book_transaction(self.db):
            VPR.canonical_open_or_add(self.db,self.db.portfolios['Currency'],'Currency','CNYRUBF',
                'LONG',row['price'],1.,self.nav(),self.clock.isoformat(),row,'DIRECT_PENDING_TARGET_TEST')
        self.assertEqual(row['_execution_audit']['status'],'HELD')
        self.assertEqual(row['_execution_audit']['reason'],'STRUCTURAL_PROTECTIVE_EXIT_PENDING')
        self.assertEqual(self.db.data(),before)
        self.assertEqual(self.db.position()['payload']['active_target_ladder'],opened['payload']['active_target_ladder'])

    def test_fast_new_break_above_old_tp2_closes_old_trade_before_opening_new_trade(self):
        opened=self.open()
        original=deepcopy(opened['payload']['entry_event_snapshot'])
        row=synthetic_higher_break(12.842)
        result=self.run_fast(row)
        self.assertEqual([order['side'] for order in self.db.orders],['BUY','SELL','BUY'])
        self.assertEqual(self.db.orders[1]['reason'],'TAKE_PROFIT_STRUCTURAL_FINAL')
        self.assertAlmostEqual(self.db.orders[1]['payload']['closed_normalized_units'],opened['units'])
        old=self.db.trades[opened['active_trade_id']]
        self.assertEqual(old['status'],'CLOSED')
        self.assertEqual(old['payload']['entry_event_snapshot'],original)
        position=self.db.position()
        self.assertNotEqual(position['active_trade_id'],opened['active_trade_id'])
        self.assertEqual(position['payload']['entry_event_snapshot'],row['timeframe_entry_context']['event'])
        self.assertEqual(result['portfolios'][0]['admission_trace'][0]['execution']['execution_action'],'OPEN')

    def test_reached_old_target_with_negative_cycle_economics_holds_instead_of_erasing_target(self):
        opened=self.open()
        self.db.portfolios['Currency']['funding_rub']=1000.
        self.db.trades[opened['active_trade_id']]['funding_rub']=1000.
        row=synthetic_higher_break(12.803)
        result=self.run_fast(row)
        self.assertEqual(len(self.db.orders),1)
        self.assertEqual(self.db.position()['units'],opened['units'])
        self.assertEqual(self.db.position()['payload']['active_target_ladder'],opened['payload']['active_target_ladder'])
        self.assertEqual(self.db.position()['payload']['active_target_stage'],0)
        trace=result['portfolios'][0]['admission_trace'][0]
        self.assertEqual(trace['execution']['status'],'HELD')
        self.assertEqual(trace['execution']['reason'],'PROTECTIVE_EXIT_PENDING')
        self.assertEqual(trace['preceding_protection']['status'],'BLOCKED')

    def test_fast_protective_close_accrues_unpaid_funding_once_before_deleting_old_position(self):
        opened=self.open()
        cursor=self.first['observed_at']
        self.db.portfolios['Currency']['last_mark_at']=cursor
        row=synthetic_higher_break(12.842,datetime(2026,10,9,7,16,0,100000,tzinfo=timezone.utc))
        expected=VC.funding_between(opened['units']*row['price'],opened['opened_at'],cursor,row['observed_at'])
        self.assertGreater(expected,0.)
        self.run_fast(row)
        old=self.db.trades[opened['active_trade_id']]
        self.assertEqual(old['status'],'CLOSED')
        self.assertAlmostEqual(old['funding_rub'],expected)
        self.assertAlmostEqual(self.db.portfolios['Currency']['funding_rub'],expected)
        self.assertEqual(self.db.portfolios['Currency']['last_mark_at'],row['observed_at'])
        replacement=self.db.position()
        self.assertNotEqual(replacement['active_trade_id'],opened['active_trade_id'])
        self.assertEqual(self.db.trades[replacement['active_trade_id']]['funding_rub'],0.)

    def test_observed_profit_peak_is_saved_before_admission_and_survives_quote_retreat(self):
        opened=self.open()
        row=synthetic_higher_break(12.803)
        peak=(10000.-self.db.portfolios['Currency']['fees_rub']
              +opened['units']*(row['price']-opened['avg_entry_price']))
        observed_at_admission=[]
        evaluate=VCR.evaluate
        def admission(*args,**kwargs):
            observed_at_admission.append(self.db.portfolios['Currency']['high_water_nav_rub'])
            return evaluate(*args,**kwargs)
        with patch.object(VCR,'evaluate',side_effect=admission):
            result=self.run_fast(row)
        self.assertGreater(peak,10000.)
        self.assertTrue(observed_at_admission)
        self.assertTrue(all(hwm>=peak-1e-9 for hwm in observed_at_admission))
        self.assertAlmostEqual(self.db.portfolios['Currency']['high_water_nav_rub'],peak)
        self.assertAlmostEqual(result['portfolios'][0]['high_water_nav_rub'],peak)
        lower=deepcopy(row)
        stamp=(self.clock+timedelta(seconds=5)).isoformat()
        lower.update(price=12.79,observed_at=stamp,market_observed_at=stamp)
        lower['_execution_quote'].update(price=12.79,observed_at=stamp)
        self.run_fast(lower)
        self.assertLess(self.nav(),peak)
        self.assertAlmostEqual(self.db.portfolios['Currency']['high_water_nav_rub'],peak)

    def test_failed_order_insert_rolls_back_book_and_does_not_publish_success_trace(self):
        prior_report = {"portfolio_autopilot": {"status": "OLD", "portfolios": []}}
        self.ns["last_cycle"] = deepcopy(prior_report)
        before = self.db.data()
        self.db.fail_order_insert = True
        with self.assertRaisesRegex(RuntimeError, "simulated order insert failure"):
            self.run_fast(self.first)
        self.assertEqual(self.db.data(), before)
        self.assertEqual(self.ns["last_cycle"], prior_report)
        self.assertEqual(self.db.commits, 0)

    def test_runtime_locally_busy_book_returns_without_connecting_or_waiting(self):
        acquired,release=threading.Event(),threading.Event()
        def hold():
            with VPG._mutex:
                acquired.set()
                release.wait(5.)
        owner=threading.Thread(target=hold)
        owner.start()
        self.assertTrue(acquired.wait(1.))
        try:
            with patch.dict(self.ns,pg_connect=lambda: self.fail('Busy local book must not open a connection')):
                started=time.monotonic()
                result=VSL.fast_entry_pass(self.ns,[self.first],self.clock,runtime=True)
                elapsed=time.monotonic()-started
            self.assertEqual(result['status'],'BUSY')
            self.assertEqual(result['reason'],'LOCAL_PAPER_BOOK_BUSY')
            self.assertLess(elapsed,.5)
            self.assertEqual(self.db.queries,[])
            self.assertEqual(self.ns['last_cycle'],{})
        finally:
            release.set(); owner.join(1.)

    def test_runtime_database_try_lock_failure_is_busy_without_accounting_or_success_trace(self):
        self.db.try_lock_granted=False
        before=self.db.data()
        result=VSL.fast_entry_pass(self.ns,[self.first],self.clock,runtime=True)
        self.assertEqual(result['status'],'BUSY')
        self.assertEqual(result['reason'],'DATABASE_PAPER_BOOK_BUSY')
        self.assertEqual(self.db.data(),before)
        self.assertEqual(self.ns['last_cycle'],{})
        self.assertEqual(len(self.db.queries),2)
        self.assertTrue(any('pg_try_advisory_xact_lock' in q for q,_ in self.db.queries))

    def test_runtime_uses_actual_clock_after_book_acquisition_and_refuses_old_quote(self):
        late=self.clock+timedelta(seconds=130)
        original_quote=deepcopy(self.first['_execution_quote'])
        with patch.object(VSL,'_wall_clock',return_value=late):
            result=VSL.fast_entry_pass(self.ns,[self.first],self.clock,runtime=True)
        trace=result['portfolios'][0]['admission_trace'][0]
        self.assertEqual(trace['execution']['status'],'BLOCKED')
        self.assertEqual(trace['execution']['reason'],'EXECUTION_QUOTE_STALE')
        self.assertEqual(trace['execution']['checked_at'],late.isoformat())
        self.assertEqual(self.db.orders,[])
        self.assertEqual(self.first['_execution_quote'],original_quote)

    def test_runtime_rechecks_actual_clock_at_mutation_after_a_ready_admission(self):
        initial=self.clock
        late=initial+timedelta(seconds=130)
        ready_seen=False
        evaluate=VCR.evaluate
        def admission(*args,**kwargs):
            nonlocal ready_seen
            result=evaluate(*args,**kwargs)
            if result.get('open'):
                ready_seen=True
            return result
        with patch.object(VCR,'evaluate',side_effect=admission), \
                patch.object(VSL,'_wall_clock',side_effect=lambda:late if ready_seen else initial):
            result=VSL.fast_entry_pass(self.ns,[self.first],initial,runtime=True)
        self.assertTrue(ready_seen)
        trace=result['portfolios'][0]['admission_trace'][0]
        self.assertEqual(trace['execution']['status'],'BLOCKED')
        self.assertEqual(trace['execution']['reason'],'EXECUTION_QUOTE_STALE')
        self.assertEqual(trace['execution']['checked_at'],late.isoformat())
        self.assertEqual(self.db.orders,[])

    def cached_entry_quote(self,row,price,observed,uid=None):
        quote=VPS.quote_from_row(row)
        quote.update(price=price,best_bid=price-.0005,best_ask=price+.0005,
                     observed_at=observed.isoformat(),orderbook_observed_at=observed.isoformat())
        if uid:
            quote['contract']['instrument_uid']=uid
        VPG.publish_quote(row['asset'],quote)
        return quote

    def test_runtime_fresh_cached_quote_replaces_stale_observer_without_redating_original_event(self):
        _,row,_,_=observed_rows('5m')
        original=deepcopy(row)
        late=self.clock+timedelta(seconds=130)
        quote=self.cached_entry_quote(row,12.737,late-timedelta(seconds=1))
        self.assertFalse(VPR.VX.paper_quote_time_gate(VPS.quote_from_row(row),'5m',now=late)['eligible'])
        with patch.object(VSL,'_wall_clock',return_value=late), \
                patch.object(VPG,'fetch_guard_quote',side_effect=AssertionError('No provider fetch under book lock')):
            result=VSL.fast_entry_pass(self.ns,[row],self.clock,runtime=True)
        trace=result['portfolios'][0]['admission_trace'][0]
        self.assertEqual(trace['execution']['status'],'EXECUTED',trace['execution'])
        self.assertEqual(trace['execution']['checked_at'],late.isoformat())
        order=self.db.orders[-1]
        fill=order['payload']['execution_model']
        self.assertEqual(fill['reference_price'],quote['price'])
        self.assertEqual((fill['bid'],fill['ask']),(quote['best_bid'],quote['best_ask']))
        self.assertAlmostEqual(order['price'],quote['best_ask']*1.0004)
        self.assertEqual(order['created_at'],late.isoformat())
        self.assertEqual(order['payload']['market_observed_at'],quote['observed_at'])
        self.assertEqual(order['payload']['price_source_identity'],VPS.identity(row['asset'],quote))
        event=original['timeframe_entry_context']['event']
        self.assertEqual(self.db.position()['payload']['entry_event_snapshot'],event)
        self.assertEqual(order['payload']['entry_event_id'],event['event_id'])
        self.assertEqual(row,original)

    def test_runtime_cached_quote_from_other_contract_cannot_replace_stale_observer(self):
        _,row,_,_=observed_rows('5m')
        original=deepcopy(row)
        late=self.clock+timedelta(seconds=130)
        self.cached_entry_quote(row,12.737,late-timedelta(seconds=1),uid='different-broker-contract')
        with patch.object(VSL,'_wall_clock',return_value=late):
            result=VSL.fast_entry_pass(self.ns,[row],self.clock,runtime=True)
        trace=result['portfolios'][0]['admission_trace'][0]
        self.assertEqual(trace['execution']['status'],'BLOCKED')
        self.assertEqual(trace['execution']['reason'],'EXECUTION_QUOTE_STALE')
        self.assertEqual(self.db.orders,[])
        self.assertEqual(row,original)

    def test_runtime_refreshes_complete_cached_book_again_immediately_before_entry(self):
        _,row,_,_=observed_rows('5m')
        original=deepcopy(row['timeframe_entry_context']['event'])
        initial=self.clock; late=initial+timedelta(seconds=130)
        ready_seen=False; selected={}
        evaluate=VCR.evaluate
        def admission(*args,**kwargs):
            nonlocal ready_seen,selected
            result=evaluate(*args,**kwargs)
            if result.get('open') and not ready_seen:
                ready_seen=True
                selected=self.cached_entry_quote(row,12.738,late-timedelta(seconds=1))
            return result
        with patch.object(VCR,'evaluate',side_effect=admission), \
                patch.object(VSL,'_wall_clock',side_effect=lambda:late if ready_seen else initial):
            result=VSL.fast_entry_pass(self.ns,[row],initial,runtime=True)
        self.assertTrue(ready_seen)
        trace=result['portfolios'][0]['admission_trace'][0]
        self.assertEqual(trace['execution']['status'],'EXECUTED',trace['execution'])
        self.assertEqual(trace['execution']['checked_at'],late.isoformat())
        order=self.db.orders[-1]; fill=order['payload']['execution_model']
        self.assertEqual(fill['reference_price'],selected['price'])
        self.assertEqual((fill['bid'],fill['ask']),(selected['best_bid'],selected['best_ask']))
        self.assertAlmostEqual(order['price'],selected['best_ask']*1.0004)
        self.assertEqual(order['payload']['market_observed_at'],selected['observed_at'])
        self.assertEqual(self.db.position()['payload']['entry_event_snapshot'],original)

    def test_runtime_new_cached_quote_at_old_target_blocks_add_without_erasing_ladder(self):
        opened=self.open()
        row=synthetic_higher_break(12.79)
        initial=VPG.utc_datetime(row['observed_at']); late=initial+timedelta(seconds=20)
        self.assertLess(row['price'],VSL.active_target_price(opened))
        ready_seen=False
        evaluate=VCR.evaluate
        def admission(*args,**kwargs):
            nonlocal ready_seen
            result=evaluate(*args,**kwargs)
            if result.get('open') and not ready_seen:
                ready_seen=True
                self.cached_entry_quote(row,12.813,late-timedelta(seconds=1))
            return result
        with patch.object(VCR,'evaluate',side_effect=admission), \
                patch.object(VSL,'_wall_clock',side_effect=lambda:late if ready_seen else initial):
            result=VSL.fast_entry_pass(self.ns,[row],initial,runtime=True)
        self.assertTrue(ready_seen)
        audit=result['portfolios'][0]['admission_trace'][0]['execution']
        self.assertEqual(audit['status'],'HELD',audit)
        self.assertEqual(audit['reason'],'STRUCTURAL_PROTECTIVE_EXIT_PENDING')
        self.assertEqual(len(self.db.orders),1)
        self.assertEqual(self.db.position()['units'],opened['units'])
        self.assertEqual(self.db.position()['payload']['active_target_ladder'],opened['payload']['active_target_ladder'])
        self.assertEqual(self.db.position()['payload']['entry_event_snapshot'],opened['payload']['entry_event_snapshot'])

    def test_unavailable_entry_does_not_reread_unchanged_portfolio_positions(self):
        result=self.run_fast(self.expired)
        self.assertEqual(result['portfolios'][0]['admission_trace'][0]['execution']['status'],'BLOCKED')
        positions=[q for q,_ in self.db.queries if q.startswith('SELECT * FROM paper_positions')]
        portfolios=[q for q,_ in self.db.queries if q.startswith('SELECT * FROM paper_portfolios')]
        # Each configured name is looked up once. Absent books have one empty
        # positions read; the existing Currency book needs no second read.
        self.assertEqual(len(positions),len(CTC.PORTFOLIO_ORDER))
        self.assertEqual(len(portfolios),len(CTC.PORTFOLIO_ORDER))

    def test_fast_fill_publishes_bounded_position_and_invalidates_api_cache(self):
        self.ns['_v90r25_pf_cache']={'at':1.,'value':{'positions':[]},'revision':3}
        self.ns['_v90r25_pf_lock']=threading.Lock()
        opened=self.open()
        current=self.ns['last_cycle']['portfolio_autopilot']['portfolios'][0]
        self.assertEqual(current['positions_status'],'COMPLETE')
        self.assertEqual(current['positions_checked_at'],self.clock.isoformat())
        self.assertEqual(current['positions'][0]['active_trade_id'],opened['active_trade_id'])
        self.assertEqual(current['positions'][0]['units'],opened['units'])
        self.assertGreater(current['gross_leverage'],0.)
        self.assertEqual(current['accounting_base']['fees_rub'],self.db.portfolios['Currency']['fees_rub'])
        self.assertNotIn('entry_event_snapshot',current['positions'][0]['payload'])
        self.assertLess(len(json.dumps(current['positions'][0],default=str)),12000)
        self.assertIsNone(self.ns['_v90r25_pf_cache']['value'])
        self.assertEqual(self.ns['_v90r25_pf_cache']['revision'],4)

    def test_fast_protective_close_publishes_empty_book_and_current_balance(self):
        opened=self.open()
        row=deepcopy(self.expired)
        quote=VPS.quote_from_row(row)
        quote['price']=opened['stop_price']-.001
        row=VPS.execution_row(dict(row,_execution_quote=quote))
        result=self.run_fast(row)
        self.assertIsNone(self.db.position())
        self.assertEqual(result['portfolios'][0]['admission_trace'][0]['preceding_protection']['status'],'EXECUTED')
        current=self.ns['last_cycle']['portfolio_autopilot']['portfolios'][0]
        self.assertEqual(current['positions'],[])
        self.assertEqual(current['positions_status'],'COMPLETE')
        self.assertEqual(current['gross_leverage'],0.)
        self.assertEqual(current['net_exposure'],0.)
        self.assertAlmostEqual(current['nav_rub'],self.nav())

    def test_merging_older_complete_snapshot_cannot_restore_closed_position(self):
        old={'status':'OK','portfolios':[{'name':'Currency','positions':[{'asset':'CNYRUBF'}],
             'positions_status':'COMPLETE','positions_checked_at':'2026-10-07T07:00:00Z',
             'nav_rub':9998.,'gross_leverage':.5,'net_exposure':.5,'admission_trace':[]}]}
        new={'status':'OK','portfolios':[{'name':'Currency','positions':[],
             'positions_status':'COMPLETE','positions_checked_at':'2026-10-07T07:01:00Z',
             'nav_rub':9980.,'gross_leverage':0.,'net_exposure':0.,'admission_trace':[]}]}
        merged=VSL.merge_reports(old,new)['portfolios'][0]
        self.assertEqual(merged['positions'],[])
        self.assertEqual(merged['nav_rub'],9980.)
        self.assertEqual(merged['gross_leverage'],0.)
        later=VSL.merge_reports(new,old)['portfolios'][0]
        self.assertEqual(later['positions'],[])
        self.assertEqual(later['positions_checked_at'],'2026-10-07T07:01:00Z')
        unobserved={'portfolios':[{'name':'Currency','nav_rub':9980.,'admission_trace':[]}]}
        missing=VSL.merge_reports(unobserved,old)['portfolios'][0]
        self.assertEqual(missing['positions_status'],'UNAVAILABLE')
        self.assertNotIn('positions',missing)

    def test_invalid_target_stage_or_execution_numbers_cannot_produce_reduction(self):
        position = self.open()
        target = VSL.active_target_price(position)
        for stage in (True, False, -1, 2, "0"):
            malformed = deepcopy(position)
            malformed["payload"]["active_target_stage"] = stage
            self.assertIsNone(VSL.active_target_price(malformed))
            self.assertFalse(VSL.target_reduction(malformed, target, 10000., self.clock)["eligible"])
        for price, nav in ((float("nan"), 10000.), (target, float("nan")),
                           (float("inf"), 10000.), (target, 0.)):
            self.assertFalse(VSL.target_reduction(position, price, nav, self.clock)["eligible"])

    def test_fast_commit_and_later_slow_report_keep_per_portfolio_execution_audit(self):
        older = {"asset": "CNYRUBF", "execution": {"status": "BLOCKED", "checked_at": "2026-10-07T03:59:00Z"}}
        unrelated = {"asset": "TEST_OTHER", "execution": {"status": "HELD", "checked_at": "2026-10-07T03:58:00Z"}}
        slow = {"status": "OK", "portfolios": [{"name": "Currency", "closed_trades": 7,
                    "admission_trace": [older, unrelated]}]}
        self.ns["last_cycle"]["portfolio_autopilot"] = deepcopy(slow)
        with patch.object(VCR,"evaluate",wraps=VCR.evaluate) as admission_calls:
            self.open()
            called = admission_calls.call_count
            current = self.ns["last_cycle"]["portfolio_autopilot"]
            self.assertEqual(current["portfolios"][0]["closed_trades"], 7)
            merged = VSL.merge_reports(slow, current)
            self.assertEqual(admission_calls.call_count, called)
        traces = {trace["asset"]: trace for trace in merged["portfolios"][0]["admission_trace"]}
        self.assertEqual(traces["CNYRUBF"]["execution"]["status"], "EXECUTED")
        self.assertEqual(traces["CNYRUBF"]["execution"]["execution_action"], "OPEN")
        self.assertEqual(traces["CNYRUBF"]["admission"]["phase"], "EXECUTION")
        self.assertEqual(traces["CNYRUBF"]["admission"]["source_key"], "TBANK_GRPC:CNYRUBF")
        self.assertEqual(traces["CNYRUBF"]["fill_admission"]["phase"], "FILL")
        self.assertTrue(traces["CNYRUBF"]["fill_admission"]["economics_checked"])
        self.assertIsNotNone(traces["CNYRUBF"]["net_rr"])
        self.assertEqual(traces["TEST_OTHER"], unrelated)
        self.assertEqual(slow["portfolios"][0]["admission_trace"][0], older)
        self.assertEqual(merged["structural_checked_at"], self.first["observed_at"].replace("Z", "+00:00"))

    def test_real_quote_runtime_shared_data_state_and_canonical_callback_execute_one_open_and_add(self):
        import veritas_breakout_runtime as BR
        import veritas_timeframe_data as TFD
        with BR._cache_lock, TFD._STRUCTURAL_LOCK:
            previous = deepcopy((BR._markets, BR._latest_rows, TFD._STRUCTURAL_STATE))
            BR._markets.clear(); BR._latest_rows.clear(); TFD._STRUCTURAL_STATE.clear()

        def restore():
            with BR._cache_lock, TFD._STRUCTURAL_LOCK:
                for current, saved in zip((BR._markets, BR._latest_rows, TFD._STRUCTURAL_STATE), previous):
                    current.clear(); current.update(saved)
        self.addCleanup(restore)
        runtime = BR.BreakoutRuntime(self.ns, lambda rows, clock: VSL.fast_entry_pass(self.ns, rows, clock))
        self.addCleanup(runtime.close)
        for episode, row in ((0, self.seed), (0, self.first), (1, self.expired), (1, self.add)):
            self.clock = VPG.utc_datetime(row["observed_at"])
            self.assertTrue(BR.publish_market(episode_raw(episode, row["observed_at"], row["price"])))
            result = runtime.run_once(self.clock, quotes={"CNYRUBF": VPS.quote_from_row(row)})
            self.assertEqual(result["status"], "OK", result)
        self.assertEqual(len(self.db.orders), 2)
        self.assertEqual(self.db.orders[0]["payload"]["entry_event_id"], self.first["timeframe_entry_context"]["event"]["event_id"])
        position = self.db.position()
        self.assertEqual(position["payload"]["protected_stop_anchor"], 12.693)
        self.assertEqual(position["payload"]["active_target_event_snapshot"]["trigger_level"], 12.760)
        trace = self.ns["last_cycle"]["portfolio_autopilot"]["portfolios"][0]["admission_trace"][0]
        self.assertEqual(trace["execution"]["execution_action"], "ADD")
        self.assertTrue(trace["fill_admission"]["economics_checked"])
        rows = self.ns["last_cycle"]["summary"]
        self.assertTrue(rows)
        selected_key = (trace["asset"], trace["horizon"], trace["event_id"])
        selected, unselected = [], []
        for row in rows:
            callback = row["_execution_audit"]["result"]
            key = (row["asset"], row["horizon"],
                   row["timeframe_entry_context"]["event"]["event_id"])
            for outcome in callback["portfolios"]:
                self.assertEqual((outcome["asset"], outcome["horizon"], outcome["event_id"]), key)
            if row["_execution_audit"]["checked_at"] == self.clock.isoformat():
                if key == selected_key:
                    selected.extend(callback["portfolios"])
                else:
                    unselected.append(row)
                    self.assertEqual(callback["portfolios"], [])
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["status"], "EXECUTED")
        self.assertEqual(selected[0]["execution_action"], "ADD")
        self.assertTrue(unselected, "the other trigger horizons must not inherit the selected fill")



    def test_position_projection_exposes_effective_stop_tp2_and_integrity(self):
        opened = self.open()
        view = VSL._position_view(opened)
        ladder = opened['payload']['active_target_ladder']
        self.assertEqual(view['effective_stop_price'], opened['stop_price'])
        self.assertEqual(view['tp1_price'], ladder[0]['price'])
        self.assertEqual(view['second_take_price'], ladder[1]['price'])
        self.assertEqual(view['second_take_kind'], 'TP2')
        self.assertEqual(view['position_management_status'], 'OK')

        protected = deepcopy(opened)
        protected['payload']['trailing_stop'] = opened['stop_price'] + .001
        view = VSL._position_view(protected)
        self.assertEqual(view['effective_stop_price'], protected['payload']['trailing_stop'])


if __name__ == "__main__":
    unittest.main()
