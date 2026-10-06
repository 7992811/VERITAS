"""Exercise the bound canonical reverse -> accounting -> outbox call chain.

The local SQLite adapter executes the actual ledger and outbox SQL, translating
PostgreSQL types, parameters and JSON concatenation. It does not test PG locks.
Market admission and quotes are deterministic; no external account is used.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import veritas_currency_notifications as N
import veritas_portfolio as VP
import veritas_portfolio_runtime as R
from test_veritas_currency_notifications import Connection as OutboxConnection


NOW = datetime(2026, 10, 6, 20, 15, tzinfo=timezone.utc)


class JsonCursor:
    def __init__(self, cursor):
        self.cursor = cursor

    @staticmethod
    def decode(row):
        if row:
            for key, value in row.items():
                if key.endswith("payload") and isinstance(value, str):
                    row[key] = json.loads(value)
        return row

    def fetchone(self):
        return self.decode(self.cursor.fetchone())

    def fetchall(self):
        return [self.decode(row) for row in self.cursor.fetchall()]


class LedgerConnection(OutboxConnection):
    fail_new_position = False

    def execute(self, sql, params=()):
        if self.fail_new_position and sql.startswith("INSERT INTO paper_positions"):
            raise RuntimeError("injected reverse entry failure")
        sql = sql.replace("payload=payload || %s::jsonb", "payload=json_patch(payload,%s)")
        return JsonCursor(super().execute(sql, params))


class CurrencyFlipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "currency-flip.sqlite")
        self.quote = {"price": 12.75, "observed_at": NOW.isoformat(),
                      "source_gate_pass": True, "market_open": True,
                      "source_names": {"primary": "MOEX ISS"},
                      "contract": {"secid": "CNYRUBF"}}
        self.source = VP.VPS.identity("CNYRUBF", self.quote)
        self.initial_fee = 5000 * VP.COMMISSION
        self.nav = 10000 + 100 - self.initial_fee
        self.row = {"asset": "CNYRUBF", "horizon": "5m", "research_decision": "SHORT",
                    "decision": "SHORT", "price": 12.75, "_execution_quote": self.quote,
                    "_flip_confirmed": True, "_execution_audit": {},
                    "trade_plan": {"stop_price": 12.9, "target_price": 12.4}}
        payload = {"entry_nav_rub": 10000, "price_source_lock": self.source,
                   "execution_horizon": "5m", "stop_price": 12.4, "target_price": 12.9,
                   "pwin": .75, "pwin_source": "MODEL_QUALITY_SCORE_UNCALIBRATED"}
        with self.connect() as c, c.transaction():
            c.execute("""
                CREATE TABLE paper_portfolios (
                  name TEXT PRIMARY KEY,initial_nav_rub REAL,realized_pnl_rub REAL,
                  fees_rub REAL,funding_rub REAL,updated_at TEXT);
                CREATE TABLE paper_trades (
                  trade_id TEXT PRIMARY KEY,portfolio_name TEXT,asset TEXT,direction TEXT,
                  opened_at TEXT,closed_at TEXT,avg_entry_price REAL,avg_exit_price REAL,
                  max_fraction REAL,gross_pnl_rub REAL DEFAULT 0,fees_rub REAL DEFAULT 0,
                  funding_rub REAL DEFAULT 0,net_pnl_rub REAL,return_on_entry_nav REAL,
                  profitable INTEGER,meaningful_win INTEGER,status TEXT,setup TEXT,
                  horizon TEXT,payload TEXT);
                CREATE TABLE paper_positions (
                  portfolio_name TEXT,asset TEXT,direction TEXT,units REAL,avg_entry_price REAL,
                  opened_at TEXT,updated_at TEXT,active_trade_id TEXT,stop_price REAL,
                  target_fraction REAL,last_price REAL,payload TEXT,
                  PRIMARY KEY(portfolio_name,asset));
                CREATE TABLE paper_orders (
                  order_id INTEGER PRIMARY KEY AUTOINCREMENT,client_order_id TEXT UNIQUE,
                  trade_id TEXT,portfolio_name TEXT,asset TEXT,created_at TEXT,side TEXT,
                  price REAL,notional_rub REAL,fee_rub REAL,fraction_nav REAL,reason TEXT,payload TEXT);
            """)
            N.ensure_schema(c)
            c.execute("INSERT INTO paper_portfolios VALUES ('Currency',10000,0,%s,0,%s)",
                      (self.initial_fee, NOW))
            c.execute("""INSERT INTO paper_trades
                (trade_id,portfolio_name,asset,direction,opened_at,avg_entry_price,
                 max_fraction,fees_rub,status,horizon,payload)
                VALUES ('existing','Currency','CNYRUBF','LONG',%s,12.5,.5,%s,'OPEN','5m',%s)""",
                (NOW, self.initial_fee, json.dumps(payload)))
            c.execute("""INSERT INTO paper_positions VALUES
                ('Currency','CNYRUBF','LONG',400,12.5,%s,%s,'existing',12.4,.5,12.75,%s)""",
                (NOW, NOW, json.dumps(payload)))
            c.execute("""INSERT INTO paper_orders
                (client_order_id,trade_id,portfolio_name,asset,created_at,side,price,
                 notional_rub,fee_rub,fraction_nav,reason,payload)
                VALUES ('old-entry','existing','Currency','CNYRUBF',%s,'BUY',12.5,5000,%s,.5,'ENTRY','{}')""",
                (NOW, self.initial_fee))

    def tearDown(self):
        self.tmp.cleanup()

    def connect(self):
        return LedgerConnection(self.path)

    def market_context(self):
        stack = ExitStack()
        stack.enter_context(patch.dict(os.environ, {
            "VERITAS_CURRENCY_NOTIFICATIONS_ENABLED": "1",
            "VERITAS_CURRENCY_NOTIFICATIONS_CHAT_ID": "@veritas_test",
            "VERITAS_CURRENCY_NOTIFICATIONS_KEY": "local-test-key",
        }))
        stack.enter_context(patch.object(R.VCR, "evaluate", return_value={
            "open": True, "fraction": .5, "probability": .75,
            "probability_source": "MODEL_QUALITY_SCORE_UNCALIBRATED"}))
        stack.enter_context(patch.object(R.VTE, "prepare_row", side_effect=lambda row, *a, **k: row))
        stack.enter_context(patch.object(VP.VX, "paper_quote_time_gate", return_value={"eligible": True}))
        stack.enter_context(patch.object(VP.VX, "entry_gate", return_value={"eligible": True, "blockers": []}))
        stack.enter_context(patch.object(VP.VX, "simulated_fill", return_value={
            "fill_price": 12.75, "reference_price": 12.75}))
        stack.enter_context(patch.object(VP.VPG, "quote_for_position", return_value=self.quote))
        stack.enter_context(patch.object(VP.VPG, "exit_fill", return_value={
            "fill_price": 12.75, "reference_price": 12.75}))
        stack.enter_context(patch.object(VP.VPG, "publish_quote"))
        return stack

    def flip(self, c):
        self.assertIs(VP._open_or_add, R.FINAL_OPEN_OR_ADD)
        self.assertIs(VP._close_or_reduce, R.FINAL_CLOSE_OR_REDUCE)
        return VP._open_or_add(c, {"high_water_nav_rub": self.nav}, "Currency", "CNYRUBF",
                              "SHORT", 12.75, .5, self.nav, NOW.isoformat(), self.row, "ENTRY")

    def test_confirmed_flip_commits_close_then_open_and_exact_event_snapshots(self):
        with self.market_context(), self.connect() as c, c.transaction():
            self.flip(c)
        with self.connect() as c:
            position = c.execute("SELECT * FROM paper_positions").fetchone()
            old = c.execute("SELECT * FROM paper_trades WHERE trade_id='existing'").fetchone()
            orders = c.execute("SELECT side FROM paper_orders ORDER BY order_id").fetchall()
            events = c.execute(f"SELECT * FROM {N.TABLE} ORDER BY event_id").fetchall()
        self.assertEqual(position["direction"], "SHORT")
        self.assertEqual(old["status"], "CLOSED")
        self.assertAlmostEqual(old["net_pnl_rub"], 100 - self.initial_fee - 5100 * VP.COMMISSION)
        self.assertEqual([row["side"] for row in orders], ["BUY", "SELL", "SELL_SHORT"])
        self.assertEqual([row["kind"] for row in events], ["CLOSE", "OPEN"])
        closed, opened = [json.loads(row["snapshot"]) for row in events]
        self.assertEqual(closed["position_notional_rub_after"], 0)
        self.assertAlmostEqual(closed["net_pnl_rub"], old["net_pnl_rub"])
        self.assertEqual(opened["direction"], "SHORT")
        self.assertEqual(opened["stop_price"], position["stop_price"])
        self.assertEqual(opened["target_price"], position["payload"]["target_price"])
        self.assertEqual(opened["execution_horizon"], "5m")
        self.assertEqual(opened["source_identity"], self.source)
        self.assertEqual(opened["quote_observed_at"], NOW.isoformat())

    def test_unconfirmed_close_retains_old_side_and_does_not_enqueue_reverse(self):
        with self.market_context(), patch.object(VP.VPG, "quote_for_position", return_value=None):
            with self.connect() as c, c.transaction():
                self.flip(c)
        self.assertEqual(self.row["_execution_audit"]["reason"], "DIRECTION_FLIP_CLOSE_NOT_CONFIRMED")
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT direction FROM paper_positions").fetchone()["direction"], "LONG")
            self.assertEqual(c.execute("SELECT COUNT(*) AS n FROM paper_orders").fetchone()["n"], 1)
            self.assertEqual(c.execute(f"SELECT COUNT(*) AS n FROM {N.TABLE}").fetchone()["n"], 0)

    def test_reverse_entry_failure_rolls_back_old_close_and_both_event_states(self):
        with self.market_context(), self.assertRaisesRegex(RuntimeError, "injected reverse entry failure"):
            with self.connect() as c, c.transaction():
                c.fail_new_position = True
                self.flip(c)
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT direction FROM paper_positions").fetchone()["direction"], "LONG")
            self.assertEqual(c.execute("SELECT status FROM paper_trades").fetchone()["status"], "OPEN")
            self.assertEqual(c.execute("SELECT COUNT(*) AS n FROM paper_orders").fetchone()["n"], 1)
            self.assertEqual(c.execute(f"SELECT COUNT(*) AS n FROM {N.TABLE}").fetchone()["n"], 0)

    def test_canonical_and_legacy_confirmed_flip_reasons_reach_accounting(self):
        position = {"asset": "CNYRUBF", "units": 400, "direction": "LONG", "payload": {}}
        for reason in ("V84_CONFIRMED_DIRECTION_FLIP", "V842_CONFIRMED_DIRECTION_FLIP"):
            with self.subTest(reason=reason), self.market_context(), patch.object(
                    VP, "CANONICAL_ACCOUNTING_CLOSE_OR_REDUCE", return_value=1.0) as close:
                result = VP._close_or_reduce(None, {}, "Currency", position, 12.75, 0,
                                             self.nav, NOW.isoformat(), reason)
                self.assertEqual(result, 1.0)
                close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
