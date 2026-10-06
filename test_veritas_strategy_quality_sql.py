"""Integration test against an isolated, explicitly named CI database only."""
import json
import os
import unittest
import uuid
from contextlib import contextmanager
import veritas_strategy_quality as Q
import veritas_canonical_constitution as CTC

DSN=os.getenv('VERITAS_QUALITY_TEST_DSN','')

@unittest.skipUnless(DSN,'isolated PostgreSQL test database not configured')
class QualitySQLTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        self.driver=psycopg; self.row_factory=dict_row
        self.schema='quality_test_'+uuid.uuid4().hex
        with psycopg.connect(DSN) as c:
            database=c.execute('select current_database()').fetchone()[0]
            if database!='veritas_quality_test':
                raise RuntimeError('Refusing integration writes outside veritas_quality_test')
            c.execute(f'CREATE SCHEMA {self.schema}')
            c.execute(f'SET search_path TO {self.schema}')
            c.execute('''CREATE TABLE paper_trades (
                trade_id text PRIMARY KEY,portfolio_name text,asset text,direction text,status text,horizon text,
                opened_at timestamptz,closed_at timestamptz,avg_entry_price float8,avg_exit_price float8,
                max_fraction float8,gross_pnl_rub float8,fees_rub float8,funding_rub float8,
                net_pnl_rub float8,payload jsonb NOT NULL DEFAULT '{}')''')
            c.execute('''CREATE TABLE paper_positions (portfolio_name text,asset text,units float8,payload jsonb)''')
            c.execute('''CREATE TABLE paper_orders (trade_id text,side text,notional_rub float8)''')
            for key,epoch,net in [('legacy',None,-150),('current',CTC.STRATEGY_EPOCH,30)]:
                p={'mfe_pct':2.,'mae_pct':-.5,'data_integrity_status':'OK','idea_id':key,'idea_id_verified':True}
                if epoch:p.update(strategy_epoch=epoch,strategy_entry_sha='immutable-entry-sha')
                opened='2026-10-06T10:00:00Z' if not epoch else '2026-10-06T21:00:00Z'
                c.execute('''INSERT INTO paper_trades VALUES
                    (%s,'Champion','ETH','LONG','CLOSED','1h',%s,'2026-10-06T23:00:00Z',100,101,.1,%s,60,10,%s,%s::jsonb)''',
                    (key,opened,net+70,net,json.dumps(p)))
                c.execute("INSERT INTO paper_orders VALUES (%s,'BUY',10000)",(key,))
            c.execute("INSERT INTO paper_positions VALUES ('Champion','NQ',1,'{}')")

    @contextmanager
    def connect(self):
        with self.driver.connect(DSN,row_factory=self.row_factory) as c:
            c.execute(f'SET search_path TO {self.schema}')
            yield c

    def tearDown(self):
        with self.driver.connect(DSN) as c:
            c.execute(f'DROP SCHEMA {self.schema} CASCADE')

    def financials(self):
        with self.connect() as c:
            return [dict(x) for x in c.execute('''SELECT trade_id,opened_at,closed_at,status,
              gross_pnl_rub,fees_rub,funding_rub,net_pnl_rub,
              payload->'strategy_epoch' AS epoch,payload->'strategy_entry_sha' AS sha
              FROM paper_trades ORDER BY trade_id''').fetchall()]

    def test_refresh_json_provenance_financial_integrity_and_idempotency(self):
        before=self.financials();events=[]
        def emit(event,**values):events.append((event,values))
        first=Q.refresh(self.connect,emit)
        self.assertEqual(self.financials(),before)
        p=next(p for p in first['portfolios'] if p['name']=='Champion')
        self.assertEqual(p['cohorts']['all']['all']['net_pnl_rub'],-120)
        self.assertEqual(p['cohorts']['current']['all']['net_pnl_rub'],30)
        self.assertEqual(p['cohorts']['since_73266d9']['all']['net_pnl_rub'],30)
        self.assertEqual(p['inherited_open_positions'],1)
        self.assertEqual(events[-1][1]['reviewed'],2)
        with self.connect() as c:
            snapshot=[dict(x) for x in c.execute('SELECT trade_id,payload FROM paper_trades ORDER BY trade_id').fetchall()]
            self.assertIsNone(c.execute("SELECT payload->>'strategy_epoch' AS epoch FROM paper_trades WHERE trade_id='legacy'").fetchone()['epoch'])
        Q.refresh(self.connect,emit)
        self.assertEqual(events[-1][1]['reviewed'],0)
        with self.connect() as c:
            self.assertEqual([dict(x) for x in c.execute('SELECT trade_id,payload FROM paper_trades ORDER BY trade_id').fetchall()],snapshot)
            self.assertEqual(c.execute('SELECT count(*) AS n FROM paper_positions').fetchone()['n'],1)
            self.assertEqual(c.execute('SELECT count(*) AS n FROM paper_orders').fetchone()['n'],2)
        self.assertEqual(self.financials(),before)
        json.dumps(Q.snapshot(),allow_nan=False)

if __name__=='__main__':unittest.main()
