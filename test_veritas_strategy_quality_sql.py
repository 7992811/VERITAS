"""Integration test against an isolated, explicitly named CI database only."""
import json
import copy
from datetime import datetime,timedelta
import os
import unittest
import uuid
from contextlib import contextmanager
from unittest.mock import patch
import veritas_strategy_quality as Q
import veritas_canonical_constitution as CTC
import veritas_launch_readiness as L
from test_veritas_readiness_fixtures import add_observed_path, structural_evidence

def observed_evidence(asset,direction,event_id,entered,timeframe='1m'):
    return structural_evidence(asset,direction,event_id,entered,timeframe)

DSN=os.getenv('VERITAS_QUALITY_TEST_DSN','')


class EntryVersionTests(unittest.TestCase):
    def setUp(self):
        stamp=patch.object(Q.RELEASE,'deployment_sha',return_value='a'*40)
        stamp.start();self.addCleanup(stamp.stop)

    def trade(self,key,net=30,**stamp):
        entered=datetime.fromisoformat('2026-10-06T21:00:00+00:00')
        p={**observed_evidence('ETH','LONG','STF_'+key,entered,'1h'),
           'strategy_epoch':CTC.STRATEGY_EPOCH,'strategy_entry_sha':'a'*40,
           'strategy_policy_hash':Q.policy_hash(),'mfe_pct':2,'mae_pct':-.5,
           'idea_id':key,'idea_id_verified':True,'exit_reason':'TAKE_PROFIT'}
        p.update(stamp)
        return add_observed_path({'trade_id':key,'portfolio_name':'Champion','asset':'ETH',
            'direction':'LONG','horizon':'1h','status':'CLOSED','opened_at':entered.isoformat(),
            'closed_at':'2026-10-06T21:01:00+00:00','avg_entry_price':100,
            'gross_pnl_rub':net+10,'fees_rub':8,'funding_rub':2,'net_pnl_rub':net,
            'entry_notional_rub':10000,'entry_order_count':1,'payload':p})

    def test_exact_entry_sha_policy_epoch_groups_keep_ledger_and_missing_history(self):
        rows=[self.trade('current'),self.trade('old-sha',-15,strategy_entry_sha='b'*40),
              self.trade('old-policy',40,strategy_policy_hash='old-policy'),
              self.trade('old-epoch',-10,strategy_epoch='OLD'),
              self.trade('missing',-100,strategy_policy_hash=None)]
        before=copy.deepcopy(rows)
        report=Q.build_report(rows,[{'portfolio_name':'Champion','payload':rows[1]['payload']}])
        portfolio=next(p for p in report['portfolios'] if p['name']=='Champion')
        self.assertEqual(portfolio['cohorts']['current']['all']['net_pnl_rub'],30)
        self.assertEqual(portfolio['cohorts']['current']['all']['closed_trades'],1)
        self.assertEqual(portfolio['cohorts']['current_epoch']['all']['net_pnl_rub'],-45)
        self.assertEqual(portfolio['cohorts']['all']['all']['net_pnl_rub'],-55)
        self.assertEqual(portfolio['prior_version_closed_trades'],3)
        self.assertEqual(portfolio['missing_version_closed_trades'],1)
        self.assertEqual(portfolio['inherited_open_positions'],1)
        self.assertEqual(portfolio['version_group_count'],5)
        self.assertEqual(sum(g['metrics']['closed_trades'] for g in portfolio['version_groups']),5)
        self.assertIn('MISSING_VERSION_STAMP',{g['assignment'] for g in portfolio['version_groups']})
        self.assertEqual(rows,before)
        json.dumps(report,allow_nan=False)

    def test_structural_and_daily_ma_policy_changes_change_new_entry_hash(self):
        original=Q.policy_hash()
        for policy,key,value in ((CTC.STRUCTURAL_ENTRY_POLICY,'stop_buffer_atr',.25),
                                 (CTC.MA_REBOUND_POLICY,'zone_atr_daily',.15)):
            with patch.dict(policy,{key:value}):
                self.assertNotEqual(Q.policy_hash(),original)
                self.assertEqual(Q.entry_metadata({},'2026-10-06T21:00:00Z')['strategy_policy_hash'],Q.policy_hash())
        self.assertEqual(Q.policy_hash(),original)
        self.assertEqual(CTC.COST_POLICY['cost_buffer_multiple'],1.1)

    def test_redeploy_does_not_relabel_prior_rows_as_current_or_clear_losses(self):
        row=self.trade('loss',-50)
        before=copy.deepcopy(row)
        with patch.object(Q.RELEASE,'deployment_sha',return_value='b'*40):
            report=Q.build_report([row])
        portfolio=next(p for p in report['portfolios'] if p['name']=='Champion')
        self.assertEqual(portfolio['cohorts']['current']['all']['closed_trades'],0)
        self.assertEqual(portfolio['cohorts']['current_epoch']['all']['net_pnl_rub'],-50)
        self.assertEqual(portfolio['cohorts']['all']['all']['net_pnl_rub'],-50)
        self.assertEqual(portfolio['version_groups'][0]['entry_version']['strategy_entry_sha'],'a'*40)
        self.assertEqual(row,before)

    def test_unknown_runtime_version_and_missing_entry_stamps_cannot_match_each_other(self):
        row=self.trade('unknown',strategy_entry_sha=None)
        with patch.object(Q.RELEASE,'deployment_sha',return_value=None):
            report=Q.build_report([row])
        portfolio=next(p for p in report['portfolios'] if p['name']=='Champion')
        self.assertFalse(report['current_entry_version']['complete'])
        self.assertEqual(portfolio['cohorts']['current']['all']['closed_trades'],0)
        self.assertEqual(portfolio['cohorts']['all']['all']['closed_trades'],1)

    def test_missing_quote_path_keeps_money_but_is_not_learning_evidence(self):
        row=self.trade('no-path',-70);row['payload'].pop('observation_path')
        review=Q.review(row);stats=Q.statistics([row])
        self.assertEqual(review['evidence_status'],'INCOMPLETE_OR_SOURCE_UNVERIFIED')
        self.assertEqual(review['path_coverage']['reason'],'MISSING_OBSERVATION_PATH')
        self.assertEqual(stats['net_pnl_rub'],-70)
        self.assertEqual(stats['learning_evidence']['trades'],0)
        self.assertIsNone(stats['capture_ratio_mean'])

    def test_valid_structural_stop_loss_is_not_an_entry_error(self):
        row=self.trade('valid-stop',-105)
        row['payload']['exit_reason']='STOP'
        review=Q.review(row)
        self.assertEqual(review['evidence_status'],'OBSERVED_PAPER_PATH',review)
        self.assertEqual(review['component'],'VALID_RULE_LOSS')
        self.assertEqual(review['rule_diagnosis']['primary_attribution'],'VALID_STRUCTURAL_STOP_LOSS')
        self.assertFalse(review['directional_error'])
        self.assertFalse(review['parameter_changes_applied'])
        self.assertEqual(Q.statistics([row])['learning_evidence']['net_pnl_rub'],-105)

    def test_observed_original_fill_excursions_override_legacy_missing_or_rebased_values(self):
        row=self.trade('valid-path')
        baseline=Q.review(row)
        for changes in ({'mfe_pct':None,'mae_pct':None},
                        {'mfe_pct':777.,'mae_pct':-999.,'r55_lifetime_mfe_pct':888.}):
            current=copy.deepcopy(row);current['payload'].update(changes)
            result=Q.review(current)
            self.assertEqual(result['evidence_status'],'OBSERVED_PAPER_PATH')
            self.assertEqual(result['mfe_pct'],baseline['mfe_pct'])
            self.assertEqual(result['mae_pct'],baseline['mae_pct'])
            self.assertEqual(result['capture_ratio'],baseline['capture_ratio'])
            self.assertEqual(result['excursion_basis'],'IMMUTABLE_FIRST_FILL_OBSERVED_PATH')

    def test_version_group_output_is_bounded_and_reports_omitted_groups(self):
        rows=[self.trade(str(i),strategy_entry_sha=str(i)) for i in range(4)]
        with patch.object(Q,'MAX_VERSION_GROUPS',2):
            report=Q.build_report(rows)
        portfolio=next(p for p in report['portfolios'] if p['name']=='Champion')
        self.assertTrue(portfolio['version_groups_truncated'])
        self.assertEqual(portfolio['version_groups_omitted_trades'],2)
        self.assertEqual(len(portfolio['version_groups']),2)
        self.assertEqual(portfolio['cohorts']['all']['all']['closed_trades'],4)


@unittest.skipUnless(DSN,'isolated PostgreSQL test database not configured')
class QualitySQLTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        self.driver=psycopg; self.row_factory=dict_row
        self.schema='quality_test_'+uuid.uuid4().hex
        self.sha=patch.object(Q.RELEASE,'deployment_sha',return_value='a'*40)
        self.sha.start();self.addCleanup(self.sha.stop)
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
            c.execute('''CREATE TABLE paper_nav_history (
                portfolio_name text,observed_at timestamptz,nav_rub float8)''')
            for key,epoch,net in [('legacy',None,-150),('current',CTC.STRATEGY_EPOCH,30)]:
                opened='2026-10-06T10:00:00Z' if not epoch else '2026-10-06T21:00:00Z'
                p={**observed_evidence('ETH','LONG','STF_'+key,datetime.fromisoformat(opened.replace('Z','+00:00')),'1h'),
                   'mfe_pct':2.,'mae_pct':-.5,'idea_id':key,'idea_id_verified':True}
                if epoch:p.update(strategy_epoch=epoch,strategy_entry_sha='a'*40,
                                  strategy_policy_hash=Q.policy_hash(),entry_rule_revision=L.COHORT)
                add_observed_path({'asset':'ETH','direction':'LONG','horizon':'1h','opened_at':opened,
                    'closed_at':'2026-10-06T23:00:00Z','avg_entry_price':100,'payload':p})
                c.execute('''INSERT INTO paper_trades VALUES
                    (%s,'Champion','ETH','LONG','CLOSED','1h',%s,'2026-10-06T23:00:00Z',100,101,.1,%s,60,10,%s,%s::jsonb)''',
                    (key,opened,net+70,net,json.dumps(p)))
                c.execute("INSERT INTO paper_orders VALUES (%s,'BUY',10000)",(key,))
            c.execute("INSERT INTO paper_positions VALUES ('Champion','NQ',1,'{}')")
            c.execute("""INSERT INTO paper_nav_history
                SELECT 'Champion',moment,1000000 FROM generate_series(
                    '2026-10-06T20:59:00Z'::timestamptz,'2026-10-06T23:01:00Z'::timestamptz,
                    interval '1 minute') AS moment""")

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
              payload->'strategy_epoch' AS epoch,payload->'strategy_entry_sha' AS sha,
              payload->'strategy_policy_hash' AS policy_hash
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

    def test_readiness_sql_retains_horizon_and_separates_current_from_full_ledger(self):
        before=self.financials()
        with self.connect() as c:
            metrics=L.candidate_metrics(c,'Champion')
        self.assertEqual(metrics['closed_trades'],1)
        self.assertEqual(metrics['net_pnl_rub'],30)
        self.assertEqual(metrics['ledger_totals']['closed_trades'],2)
        self.assertEqual(metrics['ledger_totals']['net_pnl_rub'],-120)
        self.assertTrue(metrics['history_complete'],metrics)
        self.assertEqual(metrics['nav_history_coverage']['observed_cadence_seconds'],60)
        self.assertTrue(metrics['path_coverage']['complete'])
        self.assertEqual(self.financials(),before)

    def test_nav_sql_detects_internal_gap_even_when_both_boundaries_exist(self):
        before=self.financials()
        with self.connect() as c:
            c.execute("""DELETE FROM paper_nav_history WHERE observed_at BETWEEN
                '2026-10-06T21:15:00Z' AND '2026-10-06T21:25:00Z'""")
            metrics=L.candidate_metrics(c,'Champion')
        nav=metrics['nav_history_coverage']
        self.assertTrue(nav['boundaries_covered'])
        self.assertEqual(nav['observed_cadence_seconds'],60)
        self.assertEqual(nav['max_gap_seconds'],720)
        self.assertEqual(nav['gap_count'],1)
        self.assertFalse(nav['complete'])
        self.assertTrue(metrics['path_coverage']['complete'])
        self.assertFalse(metrics['history_complete'])
        self.assertIsNone(metrics['max_drawdown'])
        self.assertEqual(metrics['net_pnl_rub'],30)
        self.assertEqual(self.financials(),before)

    def test_compact_quality_sql_drops_bulky_event_diagnostics_but_keeps_version_and_path(self):
        with self.connect() as c:
            c.execute("""UPDATE paper_trades SET payload=jsonb_set(payload,
                '{entry_event_snapshot,debug_history}',%s::jsonb) WHERE trade_id='current'""",
                (json.dumps([{'unused':i} for i in range(10000)]),))
            rows=c.execute(Q.SELECT_TRADES+" WHERE t.trade_id='current'").fetchall()
        projected=rows[0]['payload']
        self.assertNotIn('debug_history',projected['entry_event_snapshot'])
        self.assertEqual(projected['strategy_policy_hash'],Q.policy_hash())
        self.assertEqual(projected['observation_path']['version'],'OBSERVED_EXECUTION_PATH_V1')
        self.assertEqual(Q.review(dict(rows[0]))['evidence_status'],'OBSERVED_PAPER_PATH')
        self.assertLess(len(json.dumps(projected)),15000)

if __name__=='__main__':unittest.main()
