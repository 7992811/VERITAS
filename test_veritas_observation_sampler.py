"""Independent observation sampler stays evidence-only and cache-only."""
from datetime import datetime,timedelta,timezone
import inspect
import unittest
from unittest.mock import Mock

import veritas_observation_path as PATH
import veritas_observation_sampler as SAMPLER
import veritas_position_guard as GUARD
import veritas_price_source as SOURCE

OPEN=datetime(2026,10,9,13,0,tzinfo=timezone.utc)
FEED={"primary_source":"TEST_NATIVE","contract_id":"NQ-EXACT","source_gate_pass":True}

def quote(at,price=100.):
    return dict(FEED,price=price,observed_at=at.isoformat(),market_open=True)

def row_with_entry_witness():
    identity=SOURCE.identity("NQ",FEED)
    row={"portfolio_name":"Aggressive","asset":"NQ","direction":"LONG",
         "avg_entry_price":100.,"opened_at":OPEN.isoformat(),"active_trade_id":"T1",
         "payload":{"price_source_lock":identity,"initial_stop_price":98.,"entry_atr":1.,
                    "execution_horizon":"5m",
                    "entry_execution_model":{"asset":"NQ","side":"BUY","fill_price":100.}}}
    witness=PATH.observe(row,quote(OPEN),OPEN,at_entry=True,lane="CANONICAL_ENTRY")
    row["payload"]["observation_path"]=witness
    return row

class ObservationSamplerTests(unittest.TestCase):
    def test_repeated_provider_timestamp_advances_check_without_inventing_observation(self):
        row=row_with_entry_witness()
        seen={}
        def resolver(position,now=None,cache_only=False):
            seen["cache_only"]=cache_only
            return quote(OPEN)
        patches,stats=SAMPLER.prepare([row],resolver,OPEN+timedelta(seconds=15))
        self.assertTrue(seen["cache_only"])
        self.assertEqual(stats,{"positions":1,"quotes":1,"patches":1})
        witness=patches[0]["witness"]
        self.assertEqual(witness["observation_count"],1)
        self.assertEqual(witness["duplicate_observation_count"],1)
        self.assertEqual(witness["invalid_observation_count"],0)
        self.assertEqual(witness["last_checked_at"],(OPEN+timedelta(seconds=15)).isoformat())

    def test_missing_cached_quote_writes_no_evidence(self):
        row=row_with_entry_witness()
        patches,stats=SAMPLER.prepare([row],lambda *a,**k:{},OPEN+timedelta(seconds=15))
        self.assertEqual(patches,[])
        self.assertEqual(stats["quotes"],0)
        self.assertEqual(stats["patches"],0)

    def test_sampler_surface_has_no_network_or_accounting_authority(self):
        source=inspect.getsource(SAMPLER)
        for forbidden in ("httpx","requests","CANONICAL_ACCOUNTING","paper_orders",
                          "paper_portfolios","stop_price=","target_fraction"):
            self.assertNotIn(forbidden,source)
        self.assertIn("paper_trade_observation_paths",source)

    def test_protective_loop_no_longer_writes_observation_path(self):
        source=inspect.getsource(GUARD.run_protective_pass)
        self.assertNotIn("VOP.observe",source)
        start=inspect.getsource(GUARD.start)
        self.assertIn("VOS.start(ns,quote_for_position)",start)

    def test_cache_only_guard_never_uses_direct_cny_refresh_branch(self):
        source=inspect.getsource(GUARD.quote_for_position)
        self.assertIn("not cache_only",source)

if __name__=="__main__":
    unittest.main()
