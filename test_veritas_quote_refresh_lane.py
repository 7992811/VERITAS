"""Independent quote refresh lane has provider authority but no book/accounting authority."""
from contextlib import contextmanager
from datetime import datetime,timezone
import inspect
import unittest

import veritas_position_guard as GUARD
import veritas_quote_refresh_lane as LANE

NOW=datetime(2026,10,9,15,tzinfo=timezone.utc)

class Rows:
    def __init__(self,rows):self.rows=rows
    def fetchall(self):return list(self.rows)

class Connection:
    def __init__(self,rows):self.rows=rows;self.sql=[]
    def execute(self,sql,args=()):
        self.sql.append((sql,args))
        if sql.startswith("SET LOCAL"):return Rows([])
        if "FROM paper_positions" in sql:return Rows(self.rows)
        raise AssertionError(sql)

class PG:
    def __init__(self,rows):self.rows=rows;self.connections=[]
    @contextmanager
    def __call__(self):
        c=Connection(self.rows);self.connections.append(c);yield c

class QuoteRefreshLaneTests(unittest.TestCase):
    def test_run_once_reads_positions_and_delegates_refresh_only(self):
        rows=[{"asset":"BTC","active_trade_id":"T1","payload":{"price_source_lock":{"key":"K"}}}]
        pg=PG(rows);seen=[]
        result=LANE.run_once(pg,"SELECT * FROM paper_positions",lambda values:seen.extend(values) or {"x":1},now=NOW)
        self.assertEqual(seen,rows)
        self.assertEqual(result["positions"],1)
        self.assertEqual(result["refreshed_groups"],1)
        sql=" ".join(q for c in pg.connections for q,_ in c.sql)
        self.assertNotIn("UPDATE ",sql)
        self.assertNotIn("INSERT ",sql)
        self.assertNotIn("DELETE ",sql)

    def test_lane_source_has_no_accounting_or_order_authority(self):
        source=inspect.getsource(LANE)
        for forbidden in ("CANONICAL_ACCOUNTING","paper_orders","paper_portfolios",
                          "UPDATE paper_positions","UPDATE paper_trades","stop_price=",
                          "target_fraction","enqueue_order"):
            self.assertNotIn(forbidden,source)

    def test_protective_pass_is_cache_only(self):
        source=inspect.getsource(GUARD.run_protective_pass)
        self.assertIn("cache_only=True",source)
        self.assertNotIn("fetch_guard_quote(",source)
        self.assertNotIn("refresh_position_quotes(",source)

    def test_start_launches_refresh_before_observation_sampler(self):
        source=inspect.getsource(GUARD.start)
        self.assertIn("VQR.start(ns,QUOTE_POSITION_SQL",source)
        self.assertIn("VOS.start(ns,quote_for_position)",source)
        self.assertLess(source.index("VQR.start"),source.index("VOS.start"))

    def test_refresh_function_serializes_provider_io_without_book_lock(self):
        source=inspect.getsource(GUARD.refresh_position_quotes)
        self.assertIn("with _refresh_mutex:",source)
        self.assertNotIn("book_transaction",source)
        self.assertNotIn("_mutex",source)

if __name__=="__main__":
    unittest.main()
