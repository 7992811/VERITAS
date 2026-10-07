"""Optional guard I/O parity and fault isolation; no broker or real account."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
import unittest
from unittest.mock import patch

import veritas_position_guard as G
from veritas_guard_telemetry import PathBuffer, PAIR_PATCH_SQL
from test_veritas_structural_lifecycle import PaperBook


class Memory:
    def __init__(self, n=3):
        self.positions = {str(i):{'cold':{'keep':[1,2,3]}} for i in range(n)}
        self.trades = deepcopy(self.positions)
        self.fail_ids = set()
        self.calls = self.savepoints = self.rollbacks = 0
        self.depth = 0

    @contextmanager
    def transaction(self):
        before = deepcopy((self.positions,self.trades))
        self.savepoints += 1
        self.depth += 1
        try:
            yield self
        except Exception:
            self.positions,self.trades = before
            self.rollbacks += 1
            raise
        finally:
            self.depth -= 1

    def execute(self, sql, args):
        assert sql == PAIR_PATCH_SQL and self.depth
        self.calls += 1
        rows=json.loads(args[0])
        for x in rows:
            tid=x['trade_id']
            if tid in self.positions:self.positions[tid].update(x['patch'])
        # Simulate failure in the second table after position writes.
        for x in rows:
            tid=x['trade_id']
            if tid in self.fail_ids:raise RuntimeError('synthetic journal failure')
            if tid and tid in self.trades:self.trades[tid].update(x['patch'])


class BufferTests(unittest.TestCase):
    def test_many_rows_one_atomic_command_and_cold_evidence_preserved(self):
        c=Memory(23);m={};b=PathBuffer(c,m)
        rows=[{'active_trade_id':str(i),'payload':deepcopy(c.positions[str(i)])} for i in range(23)]
        for i,z in enumerate(rows):b.add(z,{'mfe_pct':i,'observation_path':{'count':i}})
        self.assertEqual(c.calls,0)
        self.assertNotIn('mfe_pct',rows[0]['payload'])
        b.flush();b.flush()
        self.assertEqual((c.calls,c.savepoints),(1,1))
        self.assertEqual((m['path_rows'],m['path_batches']),(23,1))
        for i,z in enumerate(rows):
            self.assertEqual(z['payload'],c.positions[str(i)])
            self.assertEqual(z['payload'],c.trades[str(i)])
            self.assertEqual(z['payload']['cold'],{'keep':[1,2,3]})

    def test_failed_batch_retries_rows_and_does_not_poison_next_stop(self):
        c=Memory();c.fail_ids={'1'};b=PathBuffer(c)
        rows=[{'active_trade_id':str(i),'payload':deepcopy(c.positions[str(i)])} for i in range(3)]
        for z in rows:b.add(z,{'last_price':12.7})
        b.flush()
        self.assertEqual(c.rollbacks,2)
        self.assertEqual(b.timing['path_failed_rows'],1)
        for i,z in enumerate(rows):
            self.assertEqual('last_price' in c.positions[str(i)],i!=1)
            self.assertEqual(z['payload'],c.positions[str(i)])
            self.assertEqual(c.positions[str(i)],c.trades[str(i)])
        c.fail_ids.clear();b.add(rows[1],{'stop_checked':True});b.flush()
        self.assertTrue(c.positions['1']['stop_checked'])

    def test_single_row_failure_is_optional_and_does_not_change_memory(self):
        c=Memory(1);c.fail_ids={'0'};b=PathBuffer(c);z={'active_trade_id':'0','payload':{'old':1}}
        b.add(z,{'new':2});b.flush()
        self.assertEqual(z['payload'],{'old':1})
        self.assertEqual(c.positions['0'],c.trades['0'])
        self.assertEqual(b.timing['path_failed_rows'],1)

    def test_duplicate_ids_flush_sequentially_instead_of_ambiguous_update_from(self):
        c=Memory(1);b=PathBuffer(c);z={'active_trade_id':'0','payload':{}}
        b.add(z,{'v':1});b.add(z,{'v':2});b.flush()
        self.assertEqual(c.calls,2)
        self.assertEqual(c.trades['0']['v'],2)

    def test_detached_patch_and_invalid_json(self):
        c=Memory(1);b=PathBuffer(c);z={'active_trade_id':'0','payload':{}}
        delta={'nested':{'a':1}};b.add(z,delta);delta['nested']['a']=9
        with self.assertRaises(ValueError):b.add(z,{'bad':float('nan')})
        b.flush();self.assertEqual(z['payload']['nested']['a'],1)

    def test_missing_journal_still_updates_position(self):
        c=Memory(1);c.trades.clear();b=PathBuffer(c);z={'active_trade_id':'0','payload':{}}
        b.add(z,{'v':2});b.flush()
        self.assertEqual(c.positions['0']['v'],2);self.assertEqual(c.trades,{})

    def test_null_id_and_empty_id_preserve_original_where_semantics(self):
        c=Memory(0);c.positions['']={};c.trades['']={};b=PathBuffer(c)
        null={'active_trade_id':None,'payload':'{"old":1}'}
        empty={'active_trade_id':'','payload':{}}
        b.add(null,{'v':1});b.add(empty,{'v':2});b.flush()
        self.assertEqual(null['payload'],{'old':1,'v':1})
        self.assertEqual(c.positions[''],{'v':2});self.assertEqual(c.trades[''],{})


class GuardBatchTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,10,7,10,tzinfo=timezone.utc)
        self.book=PaperBook()
        self.quote={'price':100.,'observed_at':self.now.isoformat(),'source_gate_pass':True,
                    'market_open':True,'source_names':{'primary':'Binance spot'}}
        for i in range(23):
            name=f'Paper{i//5}'
            z=dict(portfolio_name=name,asset='ETH',direction='LONG',units=1.,avg_entry_price=100.,
                   last_price=100.,stop_price=90.,opened_at=(self.now-timedelta(minutes=5)).isoformat(),
                   active_trade_id=f'T{i}',payload={'structural_policy_version':'held-structural-policy',
                   'entry_market_observed_at':(self.now-timedelta(minutes=5)).isoformat()})
            self.book.positions[(name,str(i))]=z
            self.book.trades[z['active_trade_id']]=dict(trade_id=z['active_trade_id'],
                gross_pnl_rub=0.,fees_rub=.04,funding_rub=0.,payload={})

    def test_23_no_action_structural_positions_one_batch_no_unused_cost_reads(self):
        timing={}
        with patch.object(G,'quote_for_position',return_value=self.quote):
            changes=G.run_protective_pass(None,self.book.connect,{'ETH':self.quote},self.now,timing=timing)
        self.assertEqual(changes,[])
        self.assertEqual(self.book.unsupported,[])
        self.assertEqual(timing['path_rows'],23)
        self.assertEqual(timing['path_batches'],1)
        self.assertFalse(any(q.startswith('SELECT fees_rub') for q,_ in self.book.queries))
        self.assertEqual(self.book.commits,1)
        for z in self.book.positions.values():
            self.assertEqual(z['payload']['r55_last_path_mark_price'],100.)
            self.assertEqual(z['payload']['observation_path'],self.book.trades[z['active_trade_id']]['payload']['observation_path'])

    def test_stale_or_foreign_quote_never_enters_the_metadata_batch(self):
        for q in (dict(self.quote,observed_at=(self.now-timedelta(hours=1)).isoformat()),
                  dict(self.quote,source_names={'primary':'Coinbase'})):
            with self.subTest(q=q),patch.object(G,'quote_for_position',return_value=q):
                timing={};G.run_protective_pass(None,self.book.connect,{'ETH':q},self.now,timing=timing)
                self.assertEqual(timing['path_rows'],0)
        self.assertTrue(all('observation_path' not in z['payload'] for z in self.book.positions.values()))

    def test_observations_flush_before_any_action_and_before_refresh(self):
        # A sentinel action observes the complete preceding path prefix. Abort
        # deliberately, proving that the outer book transaction rolls it back.
        z=list(self.book.positions.values())[3];z['stop_price']=101.
        initial=self.book.data()
        class VP:
            COMMISSION=.0004
            @staticmethod
            def _portfolio_rows(c,name):
                items=list(c.positions.values())
                assert all('observation_path' in item['payload'] for item in items[:4])
                assert all('observation_path' not in item['payload'] for item in items[4:])
                raise RuntimeError('sentinel protective mutation')
        with patch.object(G,'quote_for_position',return_value=self.quote):
            with self.assertRaisesRegex(RuntimeError,'sentinel'):
                G.run_protective_pass(VP,self.book.connect,{'ETH':self.quote},self.now)
        self.assertEqual(self.book.data(),initial)


try:
    import psycopg
except ImportError:
    psycopg=None
DSN=os.getenv('VERITAS_GUARD_TEST_DSN')
if os.getenv('VERITAS_GUARD_TEST_REQUIRED')=='1' and (not DSN or psycopg is None):
    raise RuntimeError('Required isolated PostgreSQL guard validation is unavailable')


@unittest.skipUnless(DSN and psycopg,'isolated PostgreSQL is not configured')
class PostgreSQLBufferTests(unittest.TestCase):
    def setUp(self):
        info=psycopg.conninfo.conninfo_to_dict(DSN)
        self.assertIn(info.get('host'),('localhost','127.0.0.1','::1'))
        self.c=psycopg.connect(DSN,autocommit=True)
        self.c.execute('CREATE TEMP TABLE paper_positions(active_trade_id text PRIMARY KEY,payload jsonb)')
        self.c.execute('CREATE TEMP TABLE paper_trades(trade_id text PRIMARY KEY,payload jsonb)')
        for i in range(3):
            for table,col in (('paper_positions','active_trade_id'),('paper_trades','trade_id')):
                self.c.execute(f'INSERT INTO {table}({col},payload) VALUES(%s,%s::jsonb)',
                               (str(i),json.dumps({'cold':'x'*1000000,'keep':i})))

    def tearDown(self):self.c.close()

    def test_real_cte_preserves_large_evidence_and_outer_rollback(self):
        with self.c.transaction():
            b=PathBuffer(self.c)
            for i in range(3):b.add({'active_trade_id':str(i),'payload':{}},{'new':i})
            b.flush()
            for table in ('paper_positions','paper_trades'):
                got=self.c.execute(f"SELECT count(*),sum(length(payload->>'cold')),sum((payload->>'new')::int) FROM {table}").fetchone()
                self.assertEqual(got,(3,3000000,3))
        with self.assertRaisesRegex(RuntimeError,'rollback'),self.c.transaction():
            b=PathBuffer(self.c);b.add({'active_trade_id':'0','payload':{}},{'should_rollback':True});b.flush()
            raise RuntimeError('rollback')
        self.assertEqual(self.c.execute("SELECT count(*) FROM paper_positions WHERE payload ? 'should_rollback'").fetchone()[0],0)

    def test_real_savepoint_failure_isolates_one_bad_journal_row(self):
        self.c.execute("ALTER TABLE paper_trades ADD CHECK (NOT (trade_id='1' AND payload ? 'new'))")
        with self.c.transaction():
            b=PathBuffer(self.c)
            for i in range(3):b.add({'active_trade_id':str(i),'payload':{}},{'new':i})
            b.flush()
            self.assertEqual(b.timing['path_failed_rows'],1)
            self.assertEqual(self.c.execute('SELECT 1').fetchone()[0],1)
        for table,col in (('paper_positions','active_trade_id'),('paper_trades','trade_id')):
            self.assertEqual(self.c.execute(f"SELECT {col} FROM {table} WHERE payload ? 'new' ORDER BY {col}").fetchall(),[('0',),('2',)])
