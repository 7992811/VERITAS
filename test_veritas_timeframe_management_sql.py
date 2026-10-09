"""Real trailing CAS SQL, restricted to the explicitly named CI database.

The structural candidate, cost assessment and both journal updates run without
mocking SQL or protection. A separate committed connection changes the position
after its snapshot was read, reproducing the stale-manager race deterministically.
"""
import copy
import json
import os
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import veritas_canonical_constitution as CTC
import veritas_price_source as VPS
import veritas_profit_protection as VPP
import veritas_timeframe_management as TM
import veritas_timeframe_structure as TFS


DSN = os.getenv("VERITAS_QUALITY_TEST_DSN", "")


@unittest.skipUnless(DSN, "isolated PostgreSQL test database not configured")
class SameTimeframeManagementSQLTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        self.driver, self.sql, self.row_factory = psycopg, sql, dict_row
        self.schema = "tf_management_test_"+uuid.uuid4().hex
        self.now = datetime(2026, 10, 6, 20, 0, 10, tzinfo=timezone.utc)
        self.source = {"source_names": {"primary": "Binance spot"}}
        self.identity = VPS.identity("ETH", self.source)
        with self.driver.connect(DSN, row_factory=self.row_factory) as c:
            self.verify_database(c)
            c.execute(self.sql.SQL("CREATE SCHEMA {}").format(self.sql.Identifier(self.schema)))
            c.execute(self.sql.SQL("SET search_path TO {}").format(self.sql.Identifier(self.schema)))
            c.execute("""CREATE TABLE paper_portfolios (
                name text PRIMARY KEY,last_mark_at timestamptz,last_ruonia float8,
                initial_nav_rub float8,realized_pnl_rub float8,fees_rub float8,funding_rub float8)""")
            c.execute("""CREATE TABLE paper_positions (
                portfolio_name text,asset text,direction text,units float8,avg_entry_price float8,
                last_price float8,stop_price float8,opened_at timestamptz,active_trade_id text,
                payload jsonb NOT NULL DEFAULT '{}',PRIMARY KEY(portfolio_name,asset))""")
            c.execute("""CREATE TABLE paper_trades (
                trade_id text PRIMARY KEY,portfolio_name text,status text,opened_at timestamptz,
                gross_pnl_rub float8,fees_rub float8,funding_rub float8,
                payload jsonb NOT NULL DEFAULT '{}')""")
        self.addCleanup(self.drop_schema)

    def verify_database(self, c):
        if c.execute("SELECT current_database() AS name").fetchone()["name"] != "veritas_quality_test":
            raise RuntimeError("Refusing integration writes outside veritas_quality_test")

    @contextmanager
    def connect(self):
        with self.driver.connect(DSN, row_factory=self.row_factory) as c:
            self.verify_database(c)
            c.execute(self.sql.SQL("SET search_path TO {}").format(self.sql.Identifier(self.schema)))
            yield c

    def drop_schema(self):
        with self.driver.connect(DSN, row_factory=self.row_factory) as c:
            self.verify_database(c)
            c.execute(self.sql.SQL("DROP SCHEMA {} CASCADE").format(self.sql.Identifier(self.schema)))

    def seed(self, direction, label, trailing="missing"):
        name, trade_id = direction+"_"+label, direction+"_"+label+"_trade"
        stop = 90. if direction == "LONG" else 110.
        opened = self.now.replace(hour=19, minute=0, second=0)
        prior = {"reason": "PREVIOUS_CONFIRMED_SWING",
                 "reference_pivot_at": (opened+timedelta(minutes=10)).timestamp()}
        payload = {
            "execution_horizon": "5m", "structural_policy_version": CTC.STRUCTURAL_ENTRY_POLICY["version"],
            "price_source_lock": copy.deepcopy(self.identity),
            "entry_event_snapshot": {"event_id": "STF_ORIGINAL_"+trade_id, "timeframe": "5m",
                "source_identity": copy.deepcopy(self.identity), "stop_price": stop},
            "initial_stop_price": stop, "initial_take_price": 130. if direction == "LONG" else 70.,
            "same_tf_trailing": prior, "same_tf_trailing_history": [prior],
            # These SQL tests target atomic CAS/journaling after the lifecycle
            # gate has already passed; they are not tests of profit maturity.
            "profit_maturity_armed": True, "profit_maturity_floor_satisfied": True,
            "original_note": "keep immutable entry and prior journal"}
        if trailing == "null":
            payload["trailing_stop"] = None
        elif trailing == "number":
            payload["trailing_stop"] = 93. if direction == "LONG" else 107.
        with self.connect() as c:
            c.execute("INSERT INTO paper_portfolios VALUES (%s,%s,16.,10000.,0.,.4,0.)",
                      (name, self.now))
            c.execute("""INSERT INTO paper_positions VALUES
                (%s,'ETH',%s,10.,100.,100.,%s,%s,%s,%s::jsonb)""",
                (name, direction, stop, opened, trade_id, json.dumps(payload)))
            c.execute("""INSERT INTO paper_trades VALUES
                (%s,%s,'OPEN',%s,0.,.4,0.,%s::jsonb)""",
                (trade_id, name, opened, json.dumps(payload)))
        position, trade = self.read(name)
        return position, copy.deepcopy(trade["payload"])

    def read(self, name):
        with self.connect() as c:
            position = dict(c.execute("SELECT * FROM paper_positions WHERE portfolio_name=%s", (name,)).fetchone())
            trade = dict(c.execute("SELECT * FROM paper_trades WHERE trade_id=%s",
                                   (position["active_trade_id"],)).fetchone())
        return position, trade

    def context_and_quote(self, direction):
        closed = self.now.replace(second=0)
        pivot = closed-timedelta(minutes=15)
        context = {"version": TFS.VERSION, "status": "OK", "timeframe": "5m", "atr_timeframe": "5m",
                   "source_identity": copy.deepcopy(self.identity), "atr": 4.,
                   "closed_at": closed.timestamp(),
                   "levels": [{"kind": "support" if direction == "LONG" else "resistance",
                       "price": 96. if direction == "LONG" else 104., "timeframe": "5m",
                       "pivot_at": pivot.timestamp(), "available_at": closed.timestamp()}]}
        row = {**self.source, "asset": "ETH", "horizon": "5m", "timeframe_entry_context": context}
        quote = {**self.source, "asset": "ETH", "price": 110. if direction == "LONG" else 90.,
                 "observed_at": self.now.isoformat(), "source_gate_pass": True, "market_open": True}
        return row, quote

    def apply(self, snapshot):
        row, quote = self.context_and_quote(snapshot["direction"])
        with self.connect() as c:
            return TM.apply_trailing(c, snapshot["portfolio_name"], snapshot, [row], quote, self.now)

    def test_real_update_returns_row_and_appends_identical_event_to_both_journals(self):
        for direction in ("LONG", "SHORT"):
            for trailing in ("missing", "null", "number"):
                with self.subTest(direction=direction, trailing=trailing):
                    snapshot, original = self.seed(direction, "success_"+trailing, trailing)
                    expected = 95.4 if direction == "LONG" else 104.6
                    event = self.apply(snapshot)
                    self.assertTrue(event["eligible"], event)
                    current, trade = self.read(snapshot["portfolio_name"])
                    self.assertAlmostEqual(current["stop_price"], expected)
                    self.assertAlmostEqual(VPP.effective_stop(current), expected)
                    for payload in (current["payload"], trade["payload"]):
                        self.assertEqual(payload["same_tf_trailing"], event)
                        self.assertEqual(payload["same_tf_trailing_history"],
                                         original["same_tf_trailing_history"]+[event])
                        self.assertEqual(payload["trailing_stop"], expected)
                        self.assertEqual(payload["entry_event_snapshot"], original["entry_event_snapshot"])
                        self.assertEqual(payload["initial_stop_price"], original["initial_stop_price"])
                        self.assertEqual(payload["initial_take_price"], original["initial_take_price"])
                        self.assertEqual(payload["original_note"], original["original_note"])
                        # This is a loss-reducing structural move. Real cost
                        # accounting must run, rather than falling back silently.
                        self.assertEqual(payload["net_profit_protection"]["state"], "COSTS_NOT_COVERED")
                        self.assertLess(payload["net_profit_protection"]["net_at_stop_rub"], 0.)
                        self.assertFalse(payload["profit_protection_active"])
                    self.assertEqual((trade["gross_pnl_rub"], trade["fees_rub"], trade["funding_rub"]), (0., .4, 0.))
                    before_retry = copy.deepcopy((current, trade))
                    rejected = self.apply(snapshot)
                    self.assertFalse(rejected["eligible"])
                    self.assertEqual(rejected["reason"], "SAME_TF_POSITION_CHANGED")
                    self.assertEqual(self.read(snapshot["portfolio_name"]), before_retry)

    def test_committed_tighter_column_or_json_stop_cannot_be_widened_by_stale_snapshot(self):
        for direction in ("LONG", "SHORT"):
            for field in ("column", "payload"):
                with self.subTest(direction=direction, field=field):
                    snapshot, _ = self.seed(direction, "race_"+field)
                    name = snapshot["portfolio_name"]
                    marker = {"concurrent_manager": field,
                              "same_tf_trailing": {"reason": "NEWER_STOP_ALREADY_SAVED"},
                              "same_tf_trailing_history": [{"reason": "NEWER_STOP_ALREADY_SAVED"}]}
                    with self.connect() as other:
                        if field == "column":
                            other.execute("UPDATE paper_positions SET stop_price=100. WHERE portfolio_name=%s", (name,))
                        else:
                            other.execute("UPDATE paper_positions SET payload=payload||%s::jsonb WHERE portfolio_name=%s",
                                          (json.dumps({"trailing_stop": 100.}), name))
                        other.execute("UPDATE paper_positions SET payload=payload||%s::jsonb WHERE portfolio_name=%s",
                                      (json.dumps(marker), name))
                        other.execute("UPDATE paper_trades SET payload=payload||%s::jsonb WHERE trade_id=%s",
                                      (json.dumps(marker), snapshot["active_trade_id"]))
                    before = self.read(name)
                    rejected = self.apply(snapshot)
                    self.assertFalse(rejected["eligible"])
                    self.assertEqual(rejected["reason"], "SAME_TF_POSITION_CHANGED")
                    after = self.read(name)
                    self.assertEqual(after, before)
                    self.assertEqual(VPP.effective_stop(after[0]), 100.)

    def test_changed_units_or_average_price_rejects_stale_cost_event_without_journal_write(self):
        for direction in ("LONG", "SHORT"):
            for field, value in (("units", 11.), ("avg_entry_price", 100.5)):
                with self.subTest(direction=direction, field=field):
                    snapshot, _ = self.seed(direction, "resize_"+field)
                    name = snapshot["portfolio_name"]
                    with self.connect() as other:
                        other.execute(self.sql.SQL("UPDATE paper_positions SET {}=%s WHERE portfolio_name=%s").format(
                            self.sql.Identifier(field)), (value, name))
                        other.execute("UPDATE paper_trades SET payload=payload||%s::jsonb WHERE trade_id=%s",
                                      (json.dumps({"concurrent_resize": field}), snapshot["active_trade_id"]))
                    before = self.read(name)
                    rejected = self.apply(snapshot)
                    self.assertFalse(rejected["eligible"])
                    self.assertEqual(rejected["reason"], "SAME_TF_POSITION_CHANGED")
                    after = self.read(name)
                    self.assertEqual(after, before)
                    self.assertEqual(after[0][field], value)
                    self.assertEqual(after[0]["stop_price"], snapshot["stop_price"])


if __name__ == "__main__":
    unittest.main()
