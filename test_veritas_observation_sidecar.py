"""Independent observation sidecar never invents history or trading authority."""
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch

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
        self.row=dict(row);self.witness=None;self.writes=0;self.stale=0

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
        if "DELETE FROM paper_observation_sidecar" in sql:
            n=min(self.stale,int(params[0]) if params else self.stale)
            self.stale-=n
            return Result(rows=[{"trade_id":f"stale-{i}"} for i in range(n)])
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
    def setUp(self):
        self.previous_status=SIDECAR._state.get("status")
        SIDECAR._state["status"]="READY"

    def tearDown(self):
        SIDECAR._state["status"]=self.previous_status

    def test_not_ready_seed_and_seal_are_sql_free_noops(self):
        row=position();c=Cursor(row)
        SIDECAR._state["status"]="NOT_STARTED"
        self.assertIsNone(SIDECAR.seed(c,row,quote(0),stamp(0)))
        self.assertIsNone(SIDECAR.seal(c,row,quote(10),stamp(10)))
        self.assertEqual(c.writes,0)

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

    def test_sample_without_entry_seed_is_skipped_not_rewritten(self):
        row=position();c=Cursor(row)
        result=SIDECAR.sample_once(
            Connect(c),lambda work,now=None:quote(20,101.),
            now=OPEN+timedelta(seconds=20))
        self.assertEqual(result["written"],0)
        self.assertEqual(result["unseeded_positions"],1)
        self.assertEqual(result["sampled_positions"],0)
        self.assertIsNone(c.witness)

    def test_live_cache_quote_published_during_pass_uses_post_selection_clock(self):
        row=position();c=Cursor(row)
        SIDECAR.seed(c,row,quote(0),stamp(0))
        calls={"n":0}
        original_clock=SIDECAR._clock
        def advancing_clock(value=None):
            if value is not None:
                return original_clock(value)
            value=OPEN+timedelta(seconds=10+calls["n"])
            calls["n"]+=1
            return value
        def selector(work,now=None):
            # Simulate the protective cache publishing after this sidecar pass
            # began, but before the observation is actually processed.
            observed=now+timedelta(milliseconds=500)
            return dict(FEED,price=101.,observed_at=observed.isoformat())
        with patch.object(SIDECAR,"_clock",side_effect=advancing_clock):
            result=SIDECAR.sample_once(Connect(c),selector,now=None)
        self.assertEqual(result["written"],1)
        self.assertEqual(result["invalid"],0)
        self.assertEqual(c.witness["invalid_observation_count"],0)
        self.assertEqual(c.witness["coverage_status"],"OBSERVED")
        self.assertEqual(c.witness["observation_count"],2)

    def test_missing_quote_does_not_poison_seeded_witness(self):
        row=position();c=Cursor(row)
        SIDECAR.seed(c,row,quote(0),stamp(0))
        before=dict(c.witness)
        result=SIDECAR.sample_once(
            Connect(c),lambda work,now=None:{},
            now=OPEN+timedelta(seconds=10))
        self.assertEqual(result["missing_quotes"],1)
        self.assertEqual(result["written"],0)
        self.assertEqual(c.witness,before)
        result=SIDECAR.sample_once(
            Connect(c),lambda work,now=None:quote(20,101.),
            now=OPEN+timedelta(seconds=20))
        self.assertEqual(result["invalid"],0)
        self.assertEqual(result["written"],1)
        self.assertEqual(c.witness["observation_count"],2)

    def test_valid_canonical_prefix_can_handoff_without_history_synthesis(self):
        row=position()
        canonical=PATH.observe(row,quote(0),stamp(0),at_entry=True,lane="CANONICAL_ENTRY")
        row["payload"]=dict(row["payload"],observation_path=canonical)
        c=Cursor(row)
        result=SIDECAR.sample_once(
            Connect(c),lambda work,now=None:quote(10,101.),
            now=OPEN+timedelta(seconds=10))
        self.assertEqual(result["handoff_positions"],1)
        self.assertEqual(result["written"],1)
        self.assertTrue(c.witness["started_at_entry"])
        self.assertEqual(c.witness["observation_count"],2)

    def test_irrecoverable_seeded_witness_is_not_resampled(self):
        row=position();c=Cursor(row)
        witness=SIDECAR.seed(c,row,quote(0),stamp(0))
        c.witness=dict(witness,invalid_observation_count=1,coverage_status="INCOMPLETE")
        calls=[]
        result=SIDECAR.sample_once(
            Connect(c),lambda work,now=None:calls.append(now) or quote(10,101.),
            now=OPEN+timedelta(seconds=10))
        self.assertEqual(result["irrecoverable_seeded"],1)
        self.assertEqual(result["written"],0)
        self.assertEqual(calls,[])

    def test_canonical_lifecycle_seeds_and_seals_sidecar(self):
        runtime=Path("veritas_portfolio_runtime.py").read_text()
        guard=Path("veritas_position_guard.py").read_text()
        self.assertIn("VOS.seed(c,dict(opened),entry_quote,ts)",runtime)
        self.assertIn("sidecar_witness=VOS.seal(c,z,q,ts)",runtime)
        self.assertIn("allow_direct=False",guard)
        self.assertIn("veritas-observation-sidecar",Path("veritas_observation_sidecar.py").read_text())

    def test_health_counts_distinguish_seeded_from_carried_positions(self):
        row=position();c=Cursor(row)
        SIDECAR.seed(c,row,quote(0),stamp(0))
        result=SIDECAR.sample_once(
            Connect(c),lambda work,now=None:quote(10,101.),
            now=OPEN+timedelta(seconds=10))
        self.assertEqual(result["seeded_positions"],1)
        self.assertEqual(result["unseeded_positions"],0)
        self.assertEqual(result["observed_positions"],1)

        carried=position();carried["active_trade_id"]="carried"
        c2=Cursor(carried)
        result2=SIDECAR.sample_once(
            Connect(c2),lambda work,now=None:quote(20,101.),
            now=OPEN+timedelta(seconds=20))
        self.assertEqual(result2["seeded_positions"],0)
        self.assertEqual(result2["unseeded_positions"],1)
        self.assertEqual(result2["sampled_positions"],0)
        self.assertEqual(result2["written"],0)

    def test_seed_and_seal_counters_are_aggregate_only(self):
        row=position();c=Cursor(row)
        before_seed=int(SIDECAR._state.get("seeded_events") or 0)
        before_seal=int(SIDECAR._state.get("sealed_events") or 0)
        SIDECAR.seed(c,row,quote(0),stamp(0))
        SIDECAR.seal(c,row,quote(10,101.),stamp(10))
        self.assertEqual(SIDECAR._state["seeded_events"],before_seed+1)
        self.assertEqual(SIDECAR._state["sealed_events"],before_seal+1)

    def test_cleanup_is_bounded_to_closed_sidecar_rows(self):
        c=Cursor(position());c.stale=50
        deleted=SIDECAR.cleanup_once(Connect(c),limit=32)
        self.assertEqual(deleted,32)
        self.assertEqual(c.stale,18)

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
