"""Integration test against an isolated, explicitly named CI database only."""
import json
import copy
from datetime import datetime,timedelta
import os
import unittest
import uuid
from contextlib import contextmanager
import veritas_strategy_quality as Q
import veritas_canonical_constitution as CTC

def observed_evidence(asset,direction,event_id,entered,timeframe='1m'):
    identity={'asset':asset,'key':'TEST_NATIVE:'+asset,'primary_source':'TEST_NATIVE',
              'contract_id':asset+'-EXACT','version':'R80_SOURCE_LOCK'}
    seconds={'1m':60,'5m':300,'1h':3600}[timeframe]
    opening=entered-timedelta(seconds=2*seconds)
    known=opening-timedelta(seconds=seconds)
    confirmed=opening+timedelta(seconds=seconds)
    event={'event_id':event_id,'event_type':'SAME_TIMEFRAME_STRUCTURAL_BREAKOUT',
           'asset':asset,'direction':direction,'timeframe':timeframe,
           'confirmation':'CLOSED_'+timeframe+'_BAR',
           'atr_timeframe':timeframe,'stop_timeframe':timeframe,'target_timeframe':timeframe,
           'source_identity':copy.deepcopy(identity),
           'breakout_bar_at':opening.isoformat(),'signal_at':confirmed.timestamp(),
           'confirmed_at':confirmed.isoformat(),'level_available_at':known.isoformat(),
           'stop_level_available_at':known.isoformat(),'atr_observed_until':known.isoformat()}
    return {'data_integrity_status':'OK','price_source_lock':copy.deepcopy(identity),
            'entry_execution_source_identity':copy.deepcopy(identity),
            'last_exit_source_identity':copy.deepcopy(identity),
            'r66_event_id':event_id,'entry_event_snapshot':event}

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
                opened='2026-10-06T10:00:00Z' if not epoch else '2026-10-06T21:00:00Z'
                p={**observed_evidence('ETH','LONG','STF_'+key,datetime.fromisoformat(opened.replace('Z','+00:00')),'1h'),
                   'mfe_pct':2.,'mae_pct':-.5,'idea_id':key,'idea_id_verified':True}
                if epoch:p.update(strategy_epoch=epoch,strategy_entry_sha='immutable-entry-sha')
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


    def test_sql_missing_integrity_stays_unknown_and_preserves_ledger(self):
        with self.connect() as c:
            c.execute("UPDATE paper_trades SET payload=payload-'data_integrity_status' WHERE trade_id='legacy'")
        before=self.financials();events=[]
        def emit(event,**values):events.append((event,values))
        first=Q.refresh(self.connect,emit)
        with self.connect() as c:
            stored=c.execute("SELECT payload FROM paper_trades WHERE trade_id='legacy'").fetchone()['payload']
        self.assertNotIn('data_integrity_status',stored)
        self.assertNotIn('strategy_epoch',stored)
        review=stored['posttrade_review']
        self.assertEqual(review['source_integrity_status'],'UNKNOWN')
        self.assertEqual(review['evidence_exclusion'],'SOURCE_UNVERIFIED')
        self.assertEqual(review['evidence_status'],'INCOMPLETE_OR_SOURCE_UNVERIFIED')
        self.assertEqual(review['component'],'UNVERIFIED')
        self.assertIsNone(review['capture_ratio'])
        self.assertEqual(review['net_pnl_rub'],-150)
        self.assertFalse(review['parameter_changes_applied'])
        self.assertEqual(self.financials(),before)
        p=next(p for p in first['portfolios'] if p['name']=='Champion')
        self.assertEqual(p['cohorts']['all']['all']['net_pnl_rub'],-120)
        self.assertEqual(p['cohorts']['all']['all']['learning_evidence']['event_groups'],1)
        self.assertEqual(p['cohorts']['all']['all']['learning_evidence']['net_pnl_rub'],30)
        Q.refresh(self.connect,emit)
        self.assertEqual(events[-1][1]['reviewed'],0)
        self.assertEqual(self.financials(),before)


    def test_sql_keeps_proxy_lock_and_observed_event_exclusions_in_projection(self):
        with self.connect() as c:
            c.execute("""UPDATE paper_trades SET
                payload=jsonb_set(payload,'{price_source_lock}',
                '{"asset":"ETH","key":"PROXY:GLD PROXY BRIDGE","primary_source":"GLD proxy bridge"}'::jsonb)
                WHERE trade_id='legacy'""")
            c.execute("""UPDATE paper_trades SET payload=payload||
                '{"r66_event_id":"R79_SIG_e5007c48f57ece74d1"}'::jsonb WHERE trade_id='current'""")
        before=self.financials()
        result=Q.refresh(self.connect)
        with self.connect() as c:
            reviews={r['trade_id']:r['payload']['posttrade_review'] for r in
                     c.execute('SELECT trade_id,payload FROM paper_trades').fetchall()}
        self.assertEqual(reviews['legacy']['evidence_exclusion'],'PROXY_PRICE')
        self.assertEqual(reviews['current']['evidence_exclusion'],'UNVERIFIED_EVENT')
        self.assertFalse(reviews['current']['idea_id_verified'])
        p=next(p for p in result['portfolios'] if p['name']=='Champion')
        self.assertEqual(p['cohorts']['all']['all']['learning_evidence']['trades'],0)
        self.assertEqual(p['cohorts']['all']['all']['net_pnl_rub'],-120)
        self.assertEqual(self.financials(),before)

if __name__=='__main__':unittest.main()
