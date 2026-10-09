"""Integration test against an isolated, explicitly named CI database only."""
import json
import copy
import hashlib
import re
from datetime import datetime,timedelta
import os
import unittest
import uuid
from contextlib import contextmanager
from itertools import groupby
from unittest.mock import patch
import veritas_strategy_quality as Q
import veritas_canonical_constitution as CTC
import veritas_launch_readiness as L
from test_veritas_readiness_fixtures import add_observed_path, structural_evidence

def observed_evidence(asset,direction,event_id,entered,timeframe='1m'):
    return structural_evidence(asset,direction,event_id,entered,timeframe)

DSN=os.getenv('VERITAS_QUALITY_TEST_DSN','')

# Frozen pre-record query. LI's default SQL is fingerprinted below so both
# sides cannot silently adopt a changed proof/presence projection together.
# The native quote-event revision preserves its sealed proof up to 128 KiB and
# adds structural/trigger timeframes. Legacy proof/presence parity remains below.
LEGACY_SELECT_TRADES='''SELECT t.trade_id,t.portfolio_name,t.asset,t.direction,t.status,t.horizon,
 t.opened_at,t.closed_at,t.avg_entry_price,t.avg_exit_price,t.max_fraction,
 t.gross_pnl_rub,t.fees_rub,t.funding_rub,t.net_pnl_rub,
 ('''+Q.LI.payload_sql('t')+''' || jsonb_build_object(
 'strategy_epoch',t.payload->'strategy_epoch','strategy_entry_sha',t.payload->'strategy_entry_sha',
 'strategy_policy_hash',t.payload->'strategy_policy_hash',
 'strategy_policy_hash_version',t.payload->'strategy_policy_hash_version',
 'strategy_role',t.payload->'strategy_role','idea_id',t.payload->'idea_id',
 'idea_id_verified',t.payload->'idea_id_verified',
 'posttrade_review',jsonb_build_object('input_hash',t.payload#>'{posttrade_review,input_hash}'))) AS payload,
 o.entry_notional_rub,o.entry_order_count
 FROM paper_trades t LEFT JOIN (
 SELECT trade_id,SUM(notional_rub) AS entry_notional_rub,COUNT(*) AS entry_order_count
 FROM paper_orders WHERE side IN ('BUY','SELL_SHORT') GROUP BY trade_id
 ) o ON o.trade_id=t.trade_id'''


class QualityProjectionContractTests(unittest.TestCase):
    def test_default_evidence_and_legacy_query_match_frozen_contract(self):
        self.assertEqual(hashlib.sha256(Q.LI.payload_sql().encode()).hexdigest(),
                         '79b5f422521d923d0705c9089da4c8ee150252e64b5500c5f4561bc0d09797f3')
        self.assertEqual(hashlib.sha256(LEGACY_SELECT_TRADES.encode()).hexdigest(),
                         '4d1932c3b38fab63b75c4645fe672b907f0bfe1edf329c86166551d3e2a87d0f')
        self.assertEqual(Q.SELECT_TRADES.count('t.payload'),2)
        self.assertEqual(Q.SELECT_TRADES.count('jsonb_to_record('),1)
        self.assertIn('CROSS JOIN LATERAL jsonb_to_record(',Q.SELECT_TRADES)
        self.assertNotIn("t.payload->",Q.SELECT_TRADES)
        self.assertNotIn("t.payload#>",Q.SELECT_TRADES)

    def test_record_declares_every_generated_evidence_field_as_jsonb(self):
        # LI adds fields independently of this one-decode SQL optimization.
        # Catch an undefined record column without needing a running database.
        referenced=set(re.findall(r'\bquality_payload\.([A-Za-z_]\w*)',Q.SELECT_TRADES))
        record=Q.SELECT_TRADES.split('AS quality_payload(',1)[1].split(') LEFT JOIN',1)[0]
        declared=set(re.findall(r'\b([A-Za-z_]\w*)\s+jsonb\b',record))
        self.assertEqual(referenced,declared)
        self.assertTrue({'structural_timeframe','trigger_timeframe','entry_event_snapshot'}<=declared)


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
        self.assertEqual(projected['observation_path']['version'],'OBSERVED_EXECUTION_PATH_V2_SOURCE_CADENCE')
        self.assertEqual(Q.review(dict(rows[0]))['evidence_status'],'OBSERVED_PAPER_PATH')
        self.assertLess(len(json.dumps(projected)),15000)

    def assert_projection_parity(self,c):
        suffix=" WHERE t.status='CLOSED' ORDER BY t.closed_at DESC,t.trade_id LIMIT 5001"
        old=[dict(row) for row in c.execute(LEGACY_SELECT_TRADES+suffix).fetchall()]
        new=[dict(row) for row in c.execute(Q.SELECT_TRADES+suffix).fetchall()]
        self.assertEqual(new,old)
        bounded=[dict(row) for row in c.execute(Q.history_query(Q.SELECT_TRADES)).fetchall()]
        self.assertEqual(self.history_peers(bounded),self.history_peers(new))
        for before,after in zip(old,new):
            self.assertEqual(Q.review(after),Q.review(before))
            self.assertEqual(Q.LI.trade_exclusion(after),Q.LI.trade_exclusion(before))
        class FixedDateTime(datetime):
            @classmethod
            def now(cls,tz=None):
                return datetime.fromisoformat('2026-10-07T14:00:00+00:00')
        with patch.object(Q,'datetime',FixedDateTime):
            self.assertEqual(Q.build_report(new),Q.build_report(old))
        return next(row for row in new if row['trade_id']=='current')

    def test_record_projection_exact_parity_for_large_null_missing_and_malformed_payloads(self):
        with self.connect() as c:
            original=c.execute("SELECT payload FROM paper_trades WHERE trade_id='current'").fetchone()['payload']
            root_fields=set(re.findall(r"t\.payload->'([^']+)'",LEGACY_SELECT_TRADES))
            root_fields.update(('source_locked_mark','posttrade_review'))
            large=copy.deepcopy(original)
            graph=[{'unused':hashlib.sha256(str(i).encode()).hexdigest()} for i in range(1500)]
            large['unused_history']=graph
            large['entry_event_snapshot']['debug_history']=graph
            large['observation_path']['debug_history']=graph
            large['posttrade_review']={'input_hash':'recorded-hash','unused_history':graph}
            cases=[('large',large),('missing',{}),('all_null',{key:None for key in root_fields})]
            cases.extend(('nonobject_'+str(i),value) for i,value in enumerate((None,[],[{}],'bad',False,0)))
            for value in (None,[],False,'bad'):
                malformed=copy.deepcopy(original)
                for key in ('entry_event_snapshot','observation_path','entry_execution_model',
                            'last_exit_execution_model','price_source_lock','entry_source_names',
                            'source_locked_mark','posttrade_review'):
                    malformed[key]=value
                cases.append(('shape_'+str(value),malformed))
            scalars=copy.deepcopy(original)
            scalars.update(mfe_pct={'bad':1},mae_pct=[1],entry_atr=False,
                           recovered=False,learning_eligible=False,idea_id_verified=False)
            cases.append(('compound_scalars',scalars))
            for name,value in cases:
                with self.subTest(case=name):
                    c.execute("UPDATE paper_trades SET payload=%s::jsonb WHERE trade_id='current'",
                              (json.dumps(value),))
                    projected=self.assert_projection_parity(c)
                    if name=='large':
                        self.assertEqual(Q.review(projected)['evidence_status'],'OBSERVED_PAPER_PATH')
                        self.assertLess(len(json.dumps(projected['payload'])),15000)
                        self.assertEqual(projected['payload']['posttrade_review']['input_hash'],'recorded-hash')
                        plan=c.execute('EXPLAIN (ANALYZE,FORMAT JSON) '+Q.SELECT_TRADES+
                                       " WHERE t.trade_id='current'").fetchone()['QUERY PLAN'][0]['Plan']
                        pending=[plan];record_scans=[]
                        while pending:
                            node=pending.pop();pending.extend(node.get('Plans',[]))
                            if (node.get('Node Type')=='Function Scan' and
                                    node.get('Function Name')=='jsonb_to_record'):
                                record_scans.append(node)
                        self.assertEqual(len(record_scans),1)
                        self.assertEqual(record_scans[0]['Actual Loops'],1)
                        self.assertEqual(record_scans[0]['Actual Rows'],1)
            # SQL NULL is distinct from the JSON null cases above. Read-only
            # projection must retain the same unknown-evidence classification.
            c.execute('ALTER TABLE paper_trades ALTER COLUMN payload DROP NOT NULL')
            c.execute("UPDATE paper_trades SET payload=NULL WHERE trade_id='current'")
            self.assert_projection_parity(c)
            # Missing optional proof and recorded JSON null remain distinct
            # within the extracted event, including their review fingerprints.
            hashes=[]
            for present in (False,True):
                value=copy.deepcopy(original)
                value['entry_event_snapshot'].pop('ma_proof',None)
                if present:value['entry_event_snapshot']['ma_proof']=None
                c.execute("UPDATE paper_trades SET payload=%s::jsonb WHERE trade_id='current'",
                          (json.dumps(value),))
                projected=self.assert_projection_parity(c)
                self.assertEqual('ma_proof' in projected['payload']['entry_event_snapshot'],present)
                self.assertIsNone(Q.LI.trade_exclusion(projected))
                hashes.append(Q.review(projected)['input_hash'])
            self.assertNotEqual(*hashes)

    def test_record_projection_preserves_native_quote_seal_timeframes_and_size_bound(self):
        from test_veritas_quote_learning import quote_trade
        trade=quote_trade();original=copy.deepcopy(trade['payload'])
        event=original['entry_event_snapshot']
        original.update(structural_timeframe=event['structural_timeframe'],
                        trigger_timeframe=event['trigger_timeframe'])
        fields=('asset','direction','horizon','opened_at','closed_at',
                'gross_pnl_rub','fees_rub','funding_rub','net_pnl_rub')
        variants=[('native',original)]
        missing=copy.deepcopy(original)
        for key in ('structural_timeframe','trigger_timeframe'):missing.pop(key)
        variants.append(('missing',missing))
        for name,value in (('null',None),('array',[]),('object',{}),('scalar',False)):
            malformed=copy.deepcopy(original)
            malformed.update(structural_timeframe=value,trigger_timeframe=value)
            variants.append((name,malformed))
        tampered=copy.deepcopy(original);tampered['entry_event_snapshot']['proof_hash']='invalid'
        variants.append(('tampered',tampered))
        oversized=copy.deepcopy(original);oversized['entry_event_snapshot']['unused']='x'*140000
        variants.append(('oversized',oversized))
        with self.connect() as c:
            c.execute('UPDATE paper_trades SET '+','.join(k+'=%s' for k in fields)+
                      " WHERE trade_id='current'",tuple(trade[k] for k in fields))
            for name,p in variants:
                with self.subTest(case=name):
                    c.execute("UPDATE paper_trades SET payload=%s::jsonb WHERE trade_id='current'",(json.dumps(p),))
                    projected=self.assert_projection_parity(c)
                    if name=='native':
                        self.assertEqual(projected['payload']['entry_event_snapshot'],event)
                        self.assertEqual(projected['payload']['structural_timeframe'],event['structural_timeframe'])
                        self.assertEqual(projected['payload']['trigger_timeframe'],event['trigger_timeframe'])
                        self.assertIsNone(Q.LI.trade_exclusion(projected))
                    elif name=='oversized':
                        self.assertEqual(projected['payload']['entry_event_snapshot'],'OVERSIZED_QUOTE_EVENT_PROOF')
                        self.assertLess(len(json.dumps(projected['payload'])),15000)
                        self.assertIsNotNone(Q.LI.trade_exclusion(projected))

    def test_record_projection_exact_parity_for_native_ma50_ma200_long_and_short(self):
        from test_veritas_ma_learning_projection_sql import NativeMAProjectionSQLTests
        fixture=NativeMAProjectionSQLTests().fixture
        fields=('asset','direction','horizon','opened_at','closed_at',
                'gross_pnl_rub','fees_rub','funding_rub','net_pnl_rub')
        with self.connect() as c:
            for period in (50,200):
                for short in (False,True):
                    with self.subTest(period=period,short=short):
                        row=fixture('current',period,short)
                        proof=row['payload']['entry_event_snapshot']['ma_proof']
                        proof['daily_bars']=[{'unused':i} for i in range(1000)]
                        proof['daily_provenance']['bars']=[{'unused':i} for i in range(1000)]
                        c.execute('UPDATE paper_trades SET '+','.join(key+'=%s' for key in fields)+
                                  ",payload=%s::jsonb WHERE trade_id='current'",
                                  tuple(row[key] for key in fields)+(json.dumps(row['payload']),))
                        projected=self.assert_projection_parity(c)
                        self.assertIsNone(Q.LI.trade_exclusion(projected))
                        self.assertEqual(Q.review(projected)['evidence_status'],'OBSERVED_PAPER_PATH')
                        saved=projected['payload']['entry_event_snapshot']['ma_proof']
                        self.assertEqual(saved['daily_provenance']['sha256'],proof['daily_provenance']['sha256'])
                        self.assertNotIn('daily_bars',saved)
                        self.assertNotIn('bars',saved['daily_provenance'])

    @staticmethod
    def history_peers(rows):
        # Original ORDER BY closed_at DESC does not define ordering among ties.
        # Normalize only each adjacent peer group, not the surrounding sequence.
        return [(clock,sorted(group,key=lambda row:row['trade_id']))
                for clock,group in groupby(rows,key=lambda row:row['closed_at'])]

    def seed_bounded_history(self,c,count=5003,*,null_clocks=False):
        c.execute('TRUNCATE paper_trades,paper_orders,paper_positions,paper_nav_history')
        c.execute('''INSERT INTO paper_trades
            (trade_id,portfolio_name,asset,direction,status,horizon,opened_at,closed_at,
             avg_entry_price,avg_exit_price,max_fraction,gross_pnl_rub,fees_rub,funding_rub,net_pnl_rub,payload)
            SELECT 'history-'||n,'Champion','ETH','LONG','CLOSED','1h',
                '2020-01-01T00:00:00Z'::timestamptz,
                '2026-10-06T00:00:00Z'::timestamptz+n*interval '1 second',
                100,101,.1,3,1,1,1,jsonb_build_object('idea_id','history-'||n)
            FROM generate_series(1,%s) AS n''',(count,))
        if null_clocks:
            c.execute('''INSERT INTO paper_trades
                (trade_id,portfolio_name,asset,direction,status,horizon,opened_at,closed_at,payload)
                VALUES ('null-a','Champion','ETH','LONG','CLOSED','1h','2020-01-01',NULL,'null'),
                       ('null-b','Champion','ETH','LONG','CLOSED','1h','2020-01-01',NULL,'[]')''')
        c.execute('''INSERT INTO paper_trades
            (trade_id,portfolio_name,asset,direction,status,horizon,opened_at,closed_at,payload)
            VALUES ('open-future','Champion','ETH','LONG','OPEN','1h','2020-01-01',
                    '2030-01-01','{}')''')

    def test_bounded_history_keeps_original_cohort_all_entry_adds_and_limits_projection(self):
        with self.connect() as c:
            self.seed_bounded_history(c,null_clocks=True)
            c.execute('ALTER TABLE paper_orders ADD COLUMN created_at timestamptz')
            c.execute('''INSERT INTO paper_orders VALUES
                ('history-5003','BUY',100,'2019-01-01'),
                ('history-5003','BUY',50,'2020-01-01'),
                ('history-5003','SELL_SHORT',25,'2026-01-01'),
                ('history-5003','BUY',NULL,'2026-10-06'),
                ('history-5003','SELL',99000,'2026-10-06'),
                ('history-5003','BUY_TO_COVER',88000,'2026-10-06'),
                ('history-5001','BUY',NULL,'2019-01-01'),
                ('history-5001','SELL_SHORT',NULL,'2026-10-06')''')
            c.execute('''INSERT INTO paper_orders(trade_id,side,notional_rub,created_at)
                SELECT CASE WHEN n%3=0 THEN 'history-1' WHEN n%3=1 THEN 'open-future'
                       ELSE 'unrelated-order-only' END,'BUY',99999,'2018-01-01'::timestamptz
                FROM generate_series(1,12000) AS n''')
            suffix=" WHERE t.status='CLOSED' ORDER BY t.closed_at DESC LIMIT 5001"
            baseline=[dict(row) for row in c.execute(Q.SELECT_TRADES+suffix).fetchall()]
            legacy=[dict(row) for row in c.execute(LEGACY_SELECT_TRADES+suffix).fetchall()]
            bounded=[dict(row) for row in c.execute(Q.history_query(Q.SELECT_TRADES)).fetchall()]
            self.assertEqual(self.history_peers(bounded),self.history_peers(baseline))
            self.assertEqual(self.history_peers(bounded),self.history_peers(legacy))
            self.assertEqual(len(bounded),5001)
            self.assertEqual({row['trade_id'] for row in bounded[:2]},{'null-a','null-b'})
            self.assertTrue(all(row['closed_at'] is None for row in bounded[:2]))
            self.assertEqual([row['trade_id'] for row in bounded[2:]],
                             ['history-'+str(n) for n in range(5003,4,-1)])
            by_id={row['trade_id']:row for row in bounded}
            self.assertEqual((by_id['history-5003']['entry_notional_rub'],
                              by_id['history-5003']['entry_order_count']),(175.,4))
            self.assertEqual((by_id['history-5001']['entry_notional_rub'],
                              by_id['history-5001']['entry_order_count']),(None,2))
            self.assertEqual((by_id['history-5002']['entry_notional_rub'],
                              by_id['history-5002']['entry_order_count']),(None,None))
            plan=c.execute('EXPLAIN (ANALYZE,FORMAT JSON) '+Q.history_query(Q.SELECT_TRADES))\
                  .fetchone()['QUERY PLAN'][0]['Plan']
            pending=[plan];selected=[];record_scans=[]
            while pending:
                node=pending.pop();pending.extend(node.get('Plans',[]))
                if node.get('Subplan Name')=='CTE quality_selected':selected.append(node)
                if node.get('Node Type')=='Function Scan' and node.get('Function Name')=='jsonb_to_record':
                    record_scans.append(node)
            self.assertEqual(len(selected),1)
            self.assertEqual(selected[0]['Node Type'],'Limit')
            self.assertEqual(selected[0]['Actual Rows'],5001)
            self.assertEqual(selected[0]['Actual Loops'],1)
            self.assertEqual(len(record_scans),1)
            # PostgreSQL may memoize identical jsonb_to_record inputs. Measure
            # executed projection cardinality, without asserting wall time.
            scans=record_scans[0]
            self.assertGreater(scans['Actual Loops'],0)
            self.assertLessEqual(scans['Actual Loops']*scans['Actual Rows'],5001)

    def test_refresh_5001_sentinel_marks_partial_but_never_enters_5000_trade_report(self):
        with self.connect() as c:
            self.seed_bounded_history(c,count=5000)
        events=[]
        emit=lambda event,**values:events.append((event,values))
        with patch.object(Q,'build_report',wraps=Q.build_report) as build:
            complete=Q.refresh(self.connect,emit)
            self.assertEqual(len(build.call_args.args[0]),5000)
        self.assertEqual(complete['status'],'OK')
        self.assertIs(complete['history_truncated'],False)
        self.assertEqual(events[-1][1]['closed_rows'],5000)
        with self.connect() as c:
            c.execute('''INSERT INTO paper_trades
                SELECT 'history-5001',portfolio_name,asset,direction,status,horizon,opened_at,
                       closed_at+interval '1 second',avg_entry_price,avg_exit_price,max_fraction,
                       gross_pnl_rub,fees_rub,funding_rub,net_pnl_rub,payload-'posttrade_review'
                FROM paper_trades WHERE trade_id='history-5000' ''')
            selected=[row['trade_id'] for row in c.execute(Q.history_query(Q.SELECT_TRADES)).fetchall()]
        before=self.financials()
        with patch.object(Q,'build_report',wraps=Q.build_report) as build:
            partial=Q.refresh(self.connect,emit)
            reported=[row['trade_id'] for row in build.call_args.args[0]]
        self.assertEqual(partial['status'],'PARTIAL_HISTORY')
        self.assertIs(partial['history_truncated'],True)
        self.assertEqual(events[-1][1]['closed_rows'],5001)
        self.assertEqual(selected,['history-'+str(n) for n in range(5001,0,-1)])
        self.assertEqual(reported,selected[:5000])
        self.assertNotIn('history-1',reported)
        champion=next(item for item in partial['portfolios'] if item['name']=='Champion')
        self.assertEqual(champion['cohorts']['all']['all']['closed_trades'],5000)
        self.assertEqual(self.financials(),before)

    def test_autocommit_refresh_scopes_timeout_and_closes_transaction_after_cancel(self):
        before=self.financials();states=[];timeouts=[];write_timeouts=[];connections=[]
        owner=self
        @contextmanager
        def autocommit_connect():
            with owner.driver.connect(DSN,row_factory=owner.row_factory,autocommit=True) as c:
                connections.append(c)
                c.execute(f'SET search_path TO {owner.schema}')
                c.execute("SET statement_timeout = '15s'")
                class ObservedConnection:
                    def transaction(self):return c.transaction()
                    def execute(self,sql,*args):
                        if sql.lstrip().startswith(('SELECT','WITH')):
                            timeouts.append(c.execute('SHOW statement_timeout').fetchone()['statement_timeout'])
                        elif sql.lstrip().startswith('UPDATE'):
                            write_timeouts.append(c.execute('SHOW statement_timeout').fetchone()['statement_timeout'])
                        return c.execute(sql,*args)
                try:
                    yield ObservedConnection()
                finally:
                    states.append(c.info.transaction_status)
                    states.append(c.execute('SHOW statement_timeout').fetchone()['statement_timeout'])
        Q.refresh(autocommit_connect)
        self.assertEqual(timeouts,['4s','4s'])
        self.assertEqual(write_timeouts,['3s','3s'])
        self.assertEqual(states,[self.driver.pq.TransactionStatus.IDLE,'15s']*2)
        self.assertTrue(all(c.closed for c in connections))
        timeouts.clear();write_timeouts.clear();states.clear()
        # The already reviewed second pass has no write transaction.
        Q.refresh(autocommit_connect)
        self.assertEqual(timeouts,['4s','4s'])
        self.assertEqual(write_timeouts,[])
        self.assertEqual(states,[self.driver.pq.TransactionStatus.IDLE,'15s'])
        self.assertTrue(all(c.closed for c in connections))
        cached=copy.deepcopy(Q._CACHE['value']);cache_at=Q._CACHE['at']
        timeouts.clear();states.clear()
        with patch.object(Q,'SELECT_TRADES','SELECT pg_sleep(10) FROM paper_trades t'):
            with self.assertRaises(self.driver.errors.QueryCanceled):
                Q.refresh(autocommit_connect)
        self.assertEqual(timeouts,['4s'])
        self.assertEqual(states,[self.driver.pq.TransactionStatus.IDLE,'15s'])
        self.assertTrue(all(c.closed for c in connections))
        self.assertEqual(Q._CACHE['value'],cached)
        self.assertEqual(Q._CACHE['at'],cache_at)
        self.assertEqual(self.financials(),before)

if __name__=='__main__':unittest.main()
