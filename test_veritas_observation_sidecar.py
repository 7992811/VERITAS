"""Independent observation sidecar never invents history or trading authority."""
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import unittest

import veritas_observation_path as PATH
import veritas_observation_sidecar as SIDECAR
import veritas_price_source as SOURCE

OPEN=datetime(2026,10,9,14,0,tzinfo=timezone.utc)
FEED={"primary_source":"TEST_NATIVE","contract_id":"BTC-EXACT","source_gate_pass":True}


def stamp(seconds):
    return (OPEN+timedelta(seconds=seconds)).isoformat()


def quote(seconds,price=100.):
    return dict(FEED,price=price,observed_at=stamp(seconds))


def position():
    return {
        "portfolio_name":"Impulse","asset":"BTC","direction":"LONG","units":1.,
        "avg_entry_price":100.,"last_price":100.,"stop_price":98.,
        "opened_at":stamp(0),"active_trade_id":"sidecar-trade",
        "payload":{
            "price_source_lock":SOURCE.identity("BTC",FEED),
            "initial_stop_price":98.,"entry_atr":1.,
            "entry_execution_model":{"asset":"BTC","side":"BUY","fill_price":100.},
            "execution_horizon":"5m",
        },
    }


class Result:
    def __init__(self,one=None,rows=None):
        self.one=one;self.rows=rows or []
    def fetchone(self): return self.one
    def fetchall(self): return self.rows


class Cursor:
    def __init__(self,row):
        self.row=dict(row);self.witness=None;self.writes=0

    @contextmanager
    def transaction(self):
        yield self

    def execute(self,sql,params=()):
        if "SELECT p.*,s.witness AS sidecar_witness" in sql:
            r=dict(self.row,sidecar_witness=self.witness)
            return Result(rows=[r])
        if "SELECT witness FROM paper_observation_sidecar" in sql:
            return Result(one={"witness":self.witness} if self.witness else None)
        if "WITH incoming AS" in sql and "paper_observation_sidecar" in sql:
            rows=json.loads(params[0])
            if rows:
                self.witness=rows[-1]["witness"];self.writes+=len(rows)
            return Result(one={"n":len(rows)})
        if "CREATE TABLE IF NOT EXISTS paper_observation_sidecar" in sql:
            return Result()
        if "CREATE INDEX IF NOT EXISTS paper_observation_sidecar_updated" in sql:
            return Result()
        raise AssertionError(sql)


class Connect:
    def __init__(self,cursor): self.cursor=cursor
    def __call__(self): return self
    def __enter__(self): return self.cursor
    def __exit__(self,*args): return False


class SidecarTests(unittest.TestCase):
    def test_seed_sample_and_seal_produce_causal_eligible_path(self):
        row=position();c=Cursor(row)
        seeded=SIDECAR.seed(c,row,quote(0),stamp(0))
        self.assertTrue(seeded["started_at_entry"])
        self.assertEqual(seeded["observation_count"],1)

        clock=[10,20]
        prices=[100.,102.]
        calls=[]
        def selector(work,now=None):
            i=len(calls);calls.append(now)
            return quote(clock[i],prices[i])

        for seconds in clock:
            result=SIDECAR.sample_once(Connect(c),selector,now=OPEN+timedelta(seconds=seconds))
            self.assertFalse(result["book_lock_acquired"])
            self.assertEqual(result["network_fetches"],0)

        sealed=SIDECAR.seal(c,row,quote(30,101.),stamp(30))
        closed=dict(row,status="CLOSED",closed_at=stamp(30))
        closed["payload"]=dict(row["payload"],observation_path=sealed)
        assessment=PATH.assessment(closed)
        self.assertTrue(assessment["eligible"],assessment)
        self.assertEqual(sealed["last_lane"],"OBSERVATION_SIDECAR_EXIT")
        self.assertEqual(c.writes,3)

    def test_exit_seal_does_not_write_sidecar_row(self):
        row=position();c=Cursor(row)
        SIDECAR.seed(c,row,quote(0),stamp(0))
        before=c.writes
        sealed=SIDECAR.seal(c,row,quote(10,101.),stamp(10))
        self.assertIsInstance(sealed,dict)
        self.assertEqual(c.writes,before)

    def test_sample_without_entry_seed_cannot_invent_prefix(self):
        row=position();c=Cursor(row)
        result=SIDECAR.sample_once(
            Connect(c),lambda work,now=None:quote(20,101.),
            now=OPEN+timedelta(seconds=20))
        self.assertEqual(result["written"],1)
        self.assertFalse(c.witness["started_at_entry"])
        sealed=SIDECAR.seal(c,row,quote(30,102.),stamp(30))
        closed=dict(row,status="CLOSED",closed_at=stamp(30))
        closed["payload"]=dict(row["payload"],observation_path=sealed)
        self.assertEqual(PATH.assessment(closed)["reason"],"UNOBSERVED_ENTRY_PREFIX")

    def test_canonical_lifecycle_seeds_and_seals_sidecar(self):
        runtime=Path("veritas_portfolio_runtime.py").read_text()
        guard=Path("veritas_position_guard.py").read_text()
        self.assertIn("VOS.seed(c,dict(opened),entry_quote,ts)",runtime)
        self.assertIn("sidecar_witness=VOS.seal(c,z,q,ts)",runtime)
        self.assertIn("allow_direct=False",guard)
        self.assertIn("veritas-observation-sidecar",Path("veritas_observation_sidecar.py").read_text())

    def test_witness_size_is_bounded_and_sidecar_has_no_production_authority(self):
        row=position();c=Cursor(row)
        witness=SIDECAR.seed(c,row,quote(0),stamp(0))
        self.assertLess(len(json.dumps(witness).encode()),SIDECAR.MAX_WITNESS_BYTES)
        result=SIDECAR.sample_once(
            Connect(c),lambda work,now=None:quote(10),
            now=OPEN+timedelta(seconds=10))
        self.assertFalse(result["production_influence"])

if __name__=="__main__":
    unittest.main()
