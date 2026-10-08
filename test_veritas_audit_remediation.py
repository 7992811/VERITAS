"""Audit regressions: bounded reads, unchanged evidence and honest public state."""
import ast
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import veritas_dashboard_projection as P
import veritas_operational_status as O
import veritas_quality_delivery as D
import veritas_regime_delivery as R
import veritas_strategy_quality as Q
import veritas_learning_state as STORE
from veritas_maintenance import MaintenanceDeferred, MaintenanceLane
from test_veritas_maintenance import namespace
from test_veritas_quality_runtime import rows_fixture, FixedDatetime
import test_veritas_strategy_quality_sql as SQL


class DisplayTests(unittest.TestCase):
    def test_durable_signal_restore_retains_original_provider_and_does_not_forge_a_pin(self):
        tree=ast.parse(Path('veritas_intelligence.py').read_text())
        nodes=[node for node in tree.body if
               isinstance(node,ast.FunctionDef) and node.name=='latest_signal_summary_pg' or
               isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='_V90_QUOTE_IDENTITY_FIELDS' for t in node.targets)]
        source={'primary_source':'ProFinance','raw_label':'NASD100_FUT','market_contract':{'symbol':'NQ CONTINUOUS'},
                'market_source_names':{'primary':'ProFinance'},'provider_ticker_verified':False,'price':24000}
        rows=[dict(asset='NQ',horizon='5m',event_ts='2026-10-08',payload={'features':source}),
              dict(asset='BRENT',horizon='5m',event_ts='2026-10-08',payload={})]
        before=deepcopy(rows);c=Mock();c.execute.return_value.fetchall.return_value=rows
        @contextmanager
        def connect():yield c
        ns=dict(pg_enabled=lambda:True,pg_connect=connect,json=json,emit=Mock())
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'<actual-summary>','exec'),ns)
        result=ns['latest_signal_summary_pg']()
        self.assertEqual(len(result),2);self.assertEqual(rows,before)
        self.assertEqual(result[0]['primary_source'],'ProFinance')
        self.assertEqual(result[0]['contract'],{'symbol':'NQ CONTINUOUS'})
        self.assertEqual(result[0]['source_names'],{'primary':'ProFinance'})
        self.assertFalse(result[0]['provider_ticker_verified'])
        self.assertNotIn('exact_contract_verified',result[0])
        self.assertNotIn('primary_source',result[1])
        self.assertEqual(O.asset_catalog(['NQ','BRENT'],result)['NQ']['primary'],'ProFinance')

    def test_signal_projection_preserves_actions_levels_and_event_identity_without_mutating_evidence(self):
        event={'event_id':'QSB_verified','event_type':'CROSS','stop_price':90,'target_price':115,
               'source_bars':[{'close':100}]*2000,'sealed_proof':{'hash':'immutable'}}
        context={'status':'READY','reason':'VERIFIED','event':event,'levels':{'proofs':[event]*20}}
        row={'asset':'BRENT','trade_entry_eligible':False,'trade_entry_reason':'TARGET_NOT_PROFITABLE_AFTER_COSTS',
             'market_observed_at':'2026-10-08T04:00:00Z','snapshot_stale':True,
             'timeframe_entry_context':context,'trade_plan':{'entry_event_snapshot':event,
              'timeframe_entry_context':context,'stop_price':90,'final_economics_gate':{'eligible':False,'net_reward_risk':-.1}}}
        before=deepcopy(row);view=P.signal_display(row)
        self.assertEqual(row,before)
        self.assertLess(len(json.dumps(view)),len(json.dumps(row))//20)
        self.assertFalse(view['trade_entry_eligible'])
        self.assertEqual(view['trade_plan']['final_economics_gate'],row['trade_plan']['final_economics_gate'])
        self.assertEqual(view['trade_plan']['entry_event_snapshot']['event_id'],'QSB_verified')
        view['trade_plan']['entry_event_snapshot']['stop_price']=0
        self.assertEqual(row,before)

    def test_readiness_does_not_confuse_liveness_bootstrap_or_stale_storage(self):
        for boot,configured,health,expected in (
            (False,True,{'ok':True,'checked_at':950},False),
            (True,False,{'ok':True,'checked_at':950},False),
            (True,True,{'ok':False,'checked_at':950},False),
            (True,True,{'ok':True,'checked_at':800},False),
            (True,True,{'ok':True,'checked_at':950},True)):
            self.assertEqual(O.readiness(boot,configured,health,now=1000)['ok'],expected)

    def test_successful_job_cannot_hide_a_different_job_failure(self):
        jobs={'ingest':{'status':'OK','last_success_at':1000},'score':{'status':'ERROR','last_success_at':900}}
        result=O.learning_operation(jobs,True)
        self.assertEqual(result['status'],'DEGRADED');self.assertEqual(result['failed_jobs'],['score'])
        jobs['score']['status']='DEFERRED_MEMORY';jobs['score']['capacity_review_required']=True
        result=O.learning_operation(jobs,True)
        self.assertEqual(result['status'],'WAITING_RESOURCES');self.assertTrue(result['capacity_review_required'])

    def test_guard_does_not_report_a_slow_or_stuck_pass_as_within_target(self):
        state={'checked_at':'2026-10-08T00:00:00Z','duration_seconds':31.,'interval_seconds':15}
        self.assertEqual(O.protection_operation(state,datetime(2026,10,8,tzinfo=timezone.utc))['status'],'DEADLINE_EXCEEDED')
        state['duration_seconds']=5
        self.assertEqual(O.protection_operation(state,datetime(2026,10,8,0,1,tzinfo=timezone.utc))['status'],'STALE')

    def test_catalog_follows_observed_provider_without_inventing_exchange_contract(self):
        rows=[{'asset':'NQ','primary_source':'ProFinance','raw_label':'NASD100_FUT','source_gate_pass':True},
              {'asset':'GOLD','primary_source':'Yahoo GC=F','source_gate_pass':False}]
        result=O.asset_catalog(['NQ','GOLD','BRENT'],rows)
        self.assertEqual(result['NQ']['primary'],'ProFinance')
        self.assertIsNone(result['NQ']['source_identities'][0]['contract_id'])
        self.assertEqual(result['GOLD']['primary'],'Yahoo GC=F')
        self.assertEqual(result['BRENT']['status'],'NOT_OBSERVED')


class ReadinessHTTPTests(unittest.TestCase):
    def test_actual_handler_keeps_readyz_closed_until_bootstrap_and_fresh_storage(self):
        tree=ast.parse(Path('veritas_intelligence.py').read_text())
        handler=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='H')
        for boot,health,code in ((False,{'ok':True,'checked_at':1000},503),
                                (True,{'ok':True,'checked_at':1000},200),
                                (True,{'ok':False,'checked_at':1000},503)):
            ns=dict(BaseHTTPRequestHandler=object,_BOOTSTRAP_READY=boot,VERSION='test',DATABASE_URL='configured',
                    _v90_pg_health_snapshot=lambda:health,VR=SimpleNamespace(snapshot=lambda:{}),
                    VCTC=SimpleNamespace(dispatch=lambda *args:False),pg_connect=None,lock=None,last_cycle={})
            exec(compile(ast.Module(body=[handler],type_ignores=[]),'<actual-readyz>','exec'),ns)
            request=ns['H'].__new__(ns['H']);request.path='/readyz';request.reply=Mock()
            with patch.object(O.time,'time',return_value=1000):request.do_GET()
            self.assertEqual(request.reply.call_args.args[1],code)
            self.assertEqual(request.reply.call_args.args[0]['ok'],code==200)


class QualityDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(Q.RELEASE,'deployment_sha',return_value='a'*40))
        self.enterContext(patch.object(Q,'datetime',FixedDatetime))

    def test_compaction_preserves_every_financial_cohort_and_evidence_result(self):
        original=rows_fixture();before=deepcopy(original)
        compact=[D.compact_review(row) for row in original]
        recent={}
        for row in compact:D.retain_recent_details(recent,row)
        self.assertTrue(any(len(row['_quality_review'])==4 for row in compact))
        actual=D.completed_report(compact,[],cutoff='2026-10-08T04:00:00Z')
        expected=Q.build_report(original,max_version_groups=5)
        self.assertEqual({k:actual[k] for k in expected},expected)
        self.assertEqual(original,before)
        self.assertTrue(all('observation_path' not in row['payload'] for row in compact))
        STORE._good(actual);STORE._json(actual,STORE.MAX_SNAPSHOT_BYTES)
        # Malformed proof remains excluded after compaction, despite a saved verified flag.
        altered=deepcopy(original[0]);altered['payload']['idea_id_verified']=True
        altered['payload'].pop('entry_event_snapshot',None)
        self.assertEqual(D.compact_review(altered)['_quality_idea'],Q.idea_key(altered))

    def test_deferred_batch_keeps_last_good_report_and_resumes_after_reviewed_row(self):
        rows=rows_fixture()[:3];lane=Mock();lane.current_budget.return_value={}
        ns={'_v90_background_maintenance':lane,'_continuous_learning':SimpleNamespace(ready=True),'pg_connect':Mock()}
        delivery=D.QualityDelivery(ns);delivery.restore_pending=False
        c=Mock();c.execute.return_value.fetchall.return_value=rows
        @contextmanager
        def transaction(*args):yield c
        old={'status':'OK','portfolios':[{'name':'Champion'}]}
        self.enterContext(patch.dict(Q._CACHE,{'value':old,'at':1.,'last_error':None}))
        original=D.compact_review;counter=[0]
        def interrupted(row):
            counter[0]+=1
            if counter[0]==2:raise MaintenanceDeferred('DEFERRED_BUDGET',reason='fixture deadline')
            return original(row)
        with patch.object(D,'transaction',transaction),patch.object(D,'compact_review',interrupted):
            with self.assertRaises(MaintenanceDeferred):delivery.tick()
        self.assertIs(Q._CACHE['value'],old)
        self.assertEqual(len(delivery.rows),1);self.assertEqual(delivery.cursor[1],rows[0]['trade_id'])
        c.execute.return_value.fetchall.return_value=[]
        with patch.object(D,'transaction',transaction),patch.object(STORE,'publish_snapshot_in_transaction',return_value=True) as publish:
            self.assertEqual(delivery.tick()['stage'],'REPORT_READY')
            self.assertIs(Q._CACHE['value'],old)
            result=delivery.tick()
        self.assertEqual(result['status'],'OK');self.assertEqual(result['processed'],1)
        self.assertEqual(publish.call_count,1);self.assertEqual(delivery.rows,[])
        self.assertEqual(Q.snapshot()['refresh']['processed'],1)

    def test_restore_rejects_previous_release_cohort(self):
        lane=Mock();lane.current_budget.return_value={}
        delivery=D.QualityDelivery({'_v90_background_maintenance':lane,
            '_continuous_learning':SimpleNamespace(ready=True),'pg_connect':Mock()})
        @contextmanager
        def transaction(*args):yield Mock()
        saved={'payload':{'status':'OK','current_entry_version':{'strategy_entry_sha':'old'},'portfolios':[{}]}}
        with patch.object(D,'transaction',transaction),patch.object(STORE,'load_snapshot_in_transaction',return_value=saved),patch.object(delivery,'_publish') as publish:
            self.assertEqual(delivery.step()['status'],'PROGRESS');publish.assert_not_called()

    def test_timeout_shrinks_batch_without_skipping_evidence_or_replacing_last_good(self):
        class QueryCanceled(Exception):sqlstate='57014'
        lane=Mock();lane.current_budget.return_value={}
        delivery=D.QualityDelivery({'_v90_background_maintenance':lane,
            '_continuous_learning':SimpleNamespace(ready=True),'pg_connect':Mock()})
        delivery.restore_pending=False;delivery.cursor=('2026-10-07','kept')
        delivery.rows=[{'prior':'review'}];old={'status':'OK','portfolios':[{}]}
        self.enterContext(patch.dict(Q._CACHE,{'value':old,'last_error':None}))
        c=Mock();c.execute.side_effect=QueryCanceled()
        @contextmanager
        def transaction(*args):yield c
        with patch.object(D,'transaction',transaction):
            with self.assertRaises(MaintenanceDeferred) as caught:delivery.tick()
            self.assertEqual(caught.exception.status,'DEFERRED_SQL_TIMEOUT')
            self.assertEqual(delivery.batch_size,D.BATCH//2)
            self.assertEqual(delivery.cursor,('2026-10-07','kept'))
            self.assertEqual(delivery.rows,[{'prior':'review'}]);self.assertIs(Q._CACHE['value'],old)
            c.execute.side_effect=None;c.execute.return_value.fetchall.return_value=rows_fixture()[:1]
            result=delivery.tick()
        self.assertEqual(c.execute.call_args.args[1][1],'kept')
        self.assertEqual(c.execute.call_args.args[1][-1],D.BATCH//2)
        self.assertEqual(result['processed'],2)


class BootstrapPriorityTests(unittest.TestCase):
    def test_legacy_history_waits_for_durable_learning_bootstrap_then_resumes(self):
        ns=namespace();ns['_continuous_learning']=SimpleNamespace(ready=False)
        lane=MaintenanceLane(ns);self.addCleanup(lane.close)
        with self.assertRaises(MaintenanceDeferred) as caught:
            with lane.permit('legacy_history'):self.fail('history ran before bootstrap')
        self.assertEqual(caught.exception.status,'DEFERRED_LEARNING_BOOTSTRAP')
        with lane.permit('periodic:learning_bootstrap'):
            with lane.permit('nested_bootstrap_read'):pass
        ns['_continuous_learning'].ready=True
        with lane.permit('legacy_history'):pass


class RegimeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(R._CACHE,{'value':None,'at':0.,'retry_at':0.,'error':None},clear=True))

    def test_ordered_transitions_and_truncated_coverage(self):
        rows=[dict(id=i,event_ts=i,asset='ETH',horizon='1m',regime=r)
              for i,r in enumerate(['UP','UP','DOWN','UP'])]
        value=R.reduce_rows(list(reversed(rows)),2,True)['items'][0]
        self.assertEqual(value['transition_observations'],3)
        self.assertEqual(value['persistence_probability'],.5)
        self.assertTrue(R.reduce_rows(rows,2,True)['history_truncated'])

    def test_reader_failure_is_rate_limited_and_keeps_real_snapshot_age(self):
        connect=Mock(side_effect=RuntimeError('offline'))
        R._CACHE.update(value={'status':'ok','items':[{'asset':'ETH'}]},at=1.)
        with patch.object(R.time,'monotonic',return_value=1000.):
            first=R.snapshot(connect,2);second=R.snapshot(connect,2)
        self.assertEqual(connect.call_count,1)
        self.assertEqual(first,second);self.assertEqual(second['status'],'STALE')
        self.assertEqual(second['snapshot_age_seconds'],999.)

    def test_timeout_retries_a_smaller_window_and_reports_actual_coverage(self):
        class QueryCanceled(Exception):sqlstate='57014'
        c=Mock();c.transaction.return_value.__enter__=Mock(return_value=c)
        c.transaction.return_value.__exit__=Mock(return_value=False)
        windows=[]
        def execute(query,params=None):
            if query==R.QUERY:
                windows.append(params[0])
                if len(windows)==1:raise QueryCanceled()
                return SimpleNamespace(fetchall=lambda:[dict(id=i,event_ts=i,asset='BTC',horizon='5m',regime='UP') for i in range(params[0])])
        c.execute.side_effect=execute
        @contextmanager
        def connect():yield c
        with patch.object(R.time,'monotonic',return_value=1000):
            self.assertEqual(R.snapshot(connect,25)['status'],'UNAVAILABLE')
            R.snapshot(connect,25)
        self.assertEqual(windows,[R.LIMIT+1])
        with patch.object(R.time,'monotonic',return_value=1011):value=R.snapshot(connect,25)
        self.assertEqual(windows,[R.LIMIT+1,R.LIMIT//4+1])
        self.assertEqual(value['history_limit'],R.LIMIT//4)
        self.assertTrue(value['history_truncated'])
        self.assertEqual(value['history_start_at'],'0')
        self.assertEqual(value['history_end_at'],str(R.LIMIT//4-1))


@unittest.skipUnless(SQL.DSN,'isolated PostgreSQL test database not configured')
class BoundedQualitySQLTests(unittest.TestCase):
    setUp=SQL.QualitySQLTests.setUp
    tearDown=SQL.QualitySQLTests.tearDown
    connect=SQL.QualitySQLTests.connect
    financials=SQL.QualitySQLTests.financials

    def test_native_keyset_handles_ties_null_clock_future_rows_and_keeps_accounts_unchanged(self):
        with self.connect() as c:
            c.execute("INSERT INTO paper_trades(trade_id,portfolio_name,status,closed_at) VALUES ('missing','Champion','CLOSED',NULL),('future','Champion','CLOSED','2026-10-10T00:00:00Z')")
        before=self.financials();rows=[];cursor=(None,None)
        for _ in range(8):
            with self.connect() as c:
                batch=c.execute(D.batch_query(),('2026-10-08T00:00:00Z',cursor[1],cursor[0],cursor[1],1)).fetchall()
            if not batch:break
            rows.extend(batch);cursor=(batch[-1]['closed_at'],batch[-1]['trade_id'])
        self.assertEqual([r['trade_id'] for r in rows],['missing','legacy','current'])
        result=D.completed_report([D.compact_review(dict(r)) for r in rows],[])
        self.assertEqual(next(p for p in result['portfolios'] if p['name']=='Champion')['cohorts']['all']['all']['closed_trades'],3)
        STORE.ensure_schema(self.connect)
        self.assertTrue(STORE.publish_snapshot(self.connect,'strategy_quality',D.VERSION,result))
        self.assertEqual(STORE.load_snapshot(self.connect,'strategy_quality',D.VERSION)['payload'],result)
        self.assertEqual(self.financials(),before)


if __name__=='__main__':unittest.main()
