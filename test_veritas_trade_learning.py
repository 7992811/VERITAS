"""Real cash-flow normalization, prospective proof and bounded job regressions."""
from copy import deepcopy
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import inspect
import json
import os
import re
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import veritas_trade_learning as T
import veritas_learning_integrity as LI
import veritas_learning_exports as E
import veritas_learning_state as S
import veritas_autonomous_learning as A
from test_veritas_quote_learning import quote_trade
import test_veritas_learning_state as fixtures


def stamped_trade(*,after=.10,step=.01,net=30.):
    t=quote_trade(net);p=t['payload'];event=p['entry_event_snapshot']
    at=t['opened_at'];prob=.55
    context=dict(asset=t['asset'],horizon=t['horizon'],direction=t['direction'],regime='TREND',
                 policy_hash='immutable-policy-hash',source_identity=p['price_source_lock'],
                 base_probability=prob,predicted_probability=prob,
                 fraction=.10,position_step=step,max_fraction=.50)
    candidate=A.register_candidate(A.scope_for(context),{'n':32,'residual_sum':-.5,'evidence_hash':'training'},
                                   'SIZE_DOWN_WEAK_SIGNAL',now=at-timedelta(seconds=2))
    candidates=A.freeze_candidates({'candidates':[candidate]},context,now=at)
    stamp=dict(base_probability=prob,source_identity=deepcopy(p['price_source_lock']),
               policy_hash=context['policy_hash'],regime='TREND',decision_at=at.isoformat(),
               position_step=step,fraction_before=.10,fraction_after=after,prospective_candidates=candidates)
    p.update(entry_canonical_admission={'autonomous_learning':stamp},entry_nav_rub=100000.,
             normalized_units=after*100000./p['entry_execution_model']['fill_price'],
             normalized_paper_notional=True,quantity_semantics='NORMALIZED_PAPER_RETURN_UNITS',
             entry_stop_risk_budget=dict(version='NET_STOP_RISK_BUDGET_V1',eligible=True,current_fraction=0,
                 marginal_net_risk_pct=.05,risk_cap_nav=.02),
             setup_memory=dict(setup_family='TREND',regime_bucket='TREND',entry_state='BREAKOUT'))
    t.update(episode_eligible=True,learning_evidence_hash='current-hash',
             learning_integrity=dict(version=LI.VERSION,status='VERIFIED',evidence_hash='current-hash',event_id=event['event_id']),
             portfolio_name='Champion',setup='TREND',max_fraction=after,avg_entry_price=101.,avg_exit_price=101.2)
    return t,candidate


class TradeObservationTests(unittest.TestCase):
    def test_native_trade_without_probability_trains_and_preserves_its_rr_trial(self):
        t,_=stamped_trade();stamp=t['payload']['entry_canonical_admission']['autonomous_learning']
        stamp.update(base_probability=None,net_reward_risk=1.2,max_fraction=.5,prospective_candidates=[])
        initial,reason=T.observation(t,now=t['closed_at']+timedelta(seconds=1))
        self.assertIsNone(reason);self.assertIsNone(initial['predicted_probability'])
        candidate=A.register_candidate(A.scope_for(initial),{'n':32,'evidence_hash':'prior'},
                                       'SIZE_DOWN_UNCALIBRATED',now=t['opened_at']-timedelta(seconds=2))
        context=dict(initial,base_probability=None,fraction=.10,position_step=.01,max_fraction=.50)
        stamp['prospective_candidates']=A.freeze_candidates({'candidates':[candidate]},context,now=t['opened_at'])
        row,reason=T.observation(t,now=t['closed_at']+timedelta(seconds=1))
        self.assertIsNone(reason);self.assertEqual(row['candidate_id'],candidate['candidate_id'])
        normalized,reason=A.normalize_observation(row,t['closed_at']+timedelta(seconds=1))
        self.assertIsNone(reason);self.assertIsNone(A._candidate_values(candidate,normalized)[1])
        stamp['net_reward_risk']=2.0
        self.assertEqual(T.observation(t,now=t['closed_at']+timedelta(seconds=1))[1],
                         'PROSPECTIVE_CANDIDATE_SCOPE_MISMATCH')

    def test_net_cashflows_use_common_original_risk_and_prospective_size(self):
        t,candidate=stamped_trade();before=deepcopy(t)
        row,error=T.observation(t,now=t['closed_at']+timedelta(seconds=1))
        self.assertIsNone(error);self.assertEqual(t,before)
        self.assertAlmostEqual(row['baseline_net_r'],30./500.)
        self.assertAlmostEqual(row['candidate_net_r'],.9*30./500.)
        self.assertEqual(row['baseline_risk_r'],1.)
        self.assertAlmostEqual(row['candidate_risk_r'],.9)
        normalized,error=A.normalize_observation(row,t['closed_at']+timedelta(seconds=1))
        self.assertIsNone(error)
        values,error=A._candidate_values(candidate,normalized)
        self.assertIsNone(error);self.assertAlmostEqual(values[2],-.006)

    def test_active_reduction_and_unchanged_quantized_opportunity_are_not_lost(self):
        reduced,_=stamped_trade(after=.09,net=27.)
        row,error=T.observation(reduced,now=reduced['closed_at']+timedelta(seconds=1))
        self.assertIsNone(error);self.assertAlmostEqual(row['baseline_net_r'],.06)
        same,candidate=stamped_trade(step=.05)
        row,error=T.observation(same,now=same['closed_at']+timedelta(seconds=1))
        self.assertIsNone(error);self.assertEqual(row['candidate_risk_r'],1.)
        normalized,error=A.normalize_observation(row,same['closed_at']+timedelta(seconds=1))
        self.assertIsNone(A._candidate_values(candidate,normalized)[1])

    def test_no_backfilled_policy_probability_cashflow_or_add_evidence(self):
        mutations={
            'stamp':lambda t:t['payload'].pop('entry_canonical_admission'),
            'risk':lambda t:t['payload'].pop('entry_stop_risk_budget'),
            'units':lambda t:t['payload'].pop('normalized_units'),
            'cashflow':lambda t:t.update(net_pnl_rub=999.),
            'add':lambda t:t['payload'].update(last_add_event_id='a-new-leg'),
            'fraction':lambda t:t['payload']['entry_canonical_admission']['autonomous_learning'].update(fraction_after=.5),
        }
        for name,mutate in mutations.items():
            with self.subTest(name=name):
                t,_=stamped_trade();mutate(t)
                row,reason=T.observation(t,now=t['closed_at']+timedelta(seconds=1))
                self.assertIsNone(row);self.assertIsNotNone(reason)

    def test_evidence_revocation_emits_same_idea_instead_of_disappearing(self):
        t,_=stamped_trade();good,_=T.observation(t,now=t['closed_at']+timedelta(seconds=1))
        t['episode_eligible']=False;t['learning_integrity']['status']='EXCLUDED'
        t['payload'].pop('observation_path')
        bad,reason=T.observation(t,now=t['closed_at']+timedelta(seconds=2))
        self.assertIsNotNone(reason);self.assertFalse(bad['evidence_valid'])
        self.assertEqual(A.observation_key(good),A.observation_key(bad))
        t['learning_integrity']['exclusion_reason']='DUPLICATE_OBSERVED_EVENT'
        self.assertEqual(T.observation(t)[1],'DUPLICATE_MARKET_IDEA')

    def test_outcome_only_receipt_trains_size_without_path_authority(self):
        t,_=stamped_trade()
        t["payload"].pop("observation_path",None)
        t["episode_eligible"]=False
        t["learning_integrity"]["status"]="EXCLUDED"
        t["episode_outcome_eligible"]=True
        t["episode_outcome_evidence_hash"]=t["learning_evidence_hash"]
        row,reason=T.observation(t,now=t["closed_at"]+timedelta(seconds=1))
        self.assertIsNone(reason)
        self.assertTrue(row["evidence_valid"])
        self.assertEqual(row["proof_kind"],"SIMULATED_SIZE_ON_VERIFIED_NET_OUTCOME")
        self.assertNotIn("mfe_pct",row)
        self.assertNotIn("mae_pct",row)

    def test_outcome_only_proof_is_accepted_by_size_candidate(self):
        t,candidate=stamped_trade()
        t["payload"].pop("observation_path",None)
        t["episode_eligible"]=False
        t["learning_integrity"]["status"]="EXCLUDED"
        t["episode_outcome_eligible"]=True
        t["episode_outcome_evidence_hash"]=t["learning_evidence_hash"]
        row,reason=T.observation(t,now=t["closed_at"]+timedelta(seconds=1))
        self.assertIsNone(reason)
        normalized,reason=A.normalize_observation(row,t["closed_at"]+timedelta(seconds=1))
        self.assertIsNone(reason)
        values,reason=A._candidate_values(candidate,normalized)
        self.assertIsNone(reason)
        self.assertIsNotNone(values)
        self.assertEqual(normalized["proof_kind"],"SIMULATED_SIZE_ON_VERIFIED_NET_OUTCOME")
        self.assertNotIn("mfe_pct",normalized)
        self.assertNotIn("mae_pct",normalized)

    def test_changed_outcome_hash_revokes_outcome_only_receipt(self):
        t,_=stamped_trade()
        t["payload"].pop("observation_path",None)
        t["episode_eligible"]=False
        t["learning_integrity"]["status"]="EXCLUDED"
        t["episode_outcome_eligible"]=True
        t["episode_outcome_evidence_hash"]="frozen-hash"
        t["learning_evidence_hash"]="changed-hash"
        row,reason=T.observation(t,now=t["closed_at"]+timedelta(seconds=1))
        self.assertIsNotNone(reason)
        self.assertFalse(row["evidence_valid"])

    def test_execution_receipt_is_frozen_but_observation_poll_time_is_not_hashed(self):
        import veritas_learning_bridge as bridge
        t,_=stamped_trade();p=t['payload']
        p['entry_canonical_admission']=bridge.execution_receipt(p['entry_canonical_admission'],
            p['normalized_units'],p['entry_execution_model']['fill_price'],p['entry_nav_rub'])
        first,reason=T.observation(t,now=t['closed_at']+timedelta(seconds=1))
        self.assertIsNone(reason)
        second,reason=T.observation(t,now=t['closed_at']+timedelta(seconds=2))
        self.assertIsNone(reason)
        self.assertEqual(first['evidence_hash'],second['evidence_hash'])
        self.assertNotEqual(first['observed_at'],second['observed_at'])
        p['entry_canonical_admission']['autonomous_learning']['execution_audit']['actual_fraction']=.4
        self.assertEqual(T.observation(t,now=t['closed_at']+timedelta(seconds=3))[1],'ACCOUNTED_SIZE_AUDIT_MISMATCH')

    def test_memory_requires_original_entry_context_and_never_duplicates_trade(self):
        t,_=stamped_trade();at=t['closed_at']
        raw=dict(trade_id='one',asset='CNYRUBF',horizon='5m',direction='LONG',family='TREND',
                 regime='TREND',entry_state='BREAKOUT',net_pnl_rub=-30.,entry_nav_rub=100000.,closed_at=at)
        board=T.memory_board([raw,raw,dict(raw,trade_id='unknown',family=None)],LI.generation(),now=at)
        self.assertEqual(board['verified_episodes'],1);self.assertEqual(len(board['items']),4)
        exact=board['items'][0]
        self.assertEqual(exact['n'],1);self.assertEqual(exact['effective_n'],.2)
        self.assertAlmostEqual(exact['posterior_win_rate'],round(5/10.2,4))
        self.assertLess(exact['weighted_avg_pnl'],0)
        self.assertEqual(exact['stop_error_rate'],0.)


class TradeProjectionContractTests(unittest.TestCase):
    def test_generated_record_declares_all_proof_and_admission_fields_once(self):
        worker=T.TradeLearning({})
        query=worker._rows_sql('SELECT trade_id FROM paper_trades ORDER BY closed_at,trade_id LIMIT 4',
            'FROM selected JOIN paper_trades t ON t.trade_id=selected.trade_id',evidence_hash=True)
        self.assertEqual(query.count('jsonb_to_record('),1)
        self.assertEqual(query.count('t.payload'),2)
        self.assertNotIn('t.payload->',query)
        self.assertIn('md5(source_evidence::text)',query)
        self.assertIn('selected AS MATERIALIZED',query)
        self.assertIn('projected AS MATERIALIZED',query)
        names=set(re.findall(r'\btrade_payload\.([A-Za-z_]\w*)',query))
        declaration=query.split('AS trade_payload(',1)[1].split(')',1)[0]
        declared=set(re.findall(r'([A-Za-z_]\w*) jsonb',declaration))
        self.assertEqual(names,declared)
        self.assertTrue(set(T.EXTRA_SCALARS+T.EXTRA_OBJECTS).issubset(declared))

    def test_validation_keeps_full_quarantine_and_conditional_source_hash_after_bounded_read(self):
        queries=[]
        class Connection:
            def execute(self,sql,args=()):
                queries.append(sql)
                if 'AS revoked FROM staged' in sql:
                    return SimpleNamespace(fetchone=lambda:{'n':0,'revoked':0})
                if 'WITH retry AS' in sql:
                    return SimpleNamespace(rowcount=0)
                if 'WITH selected AS MATERIALIZED' in sql:
                    return SimpleNamespace(fetchall=lambda:[dict(episode_trade_id='trade',prior={},
                        trade={'asset':'ETH','direction':'LONG'},evidence_hash='immutable')])
                if 'SELECT 1 FROM v90_learning_episodes' in sql:
                    return SimpleNamespace(fetchone=lambda:None)
                if 'UPDATE v90_learning_episodes e SET learning_eligible=' in sql:
                    return SimpleNamespace(rowcount=1)
                if 'SELECT count(*) AS n FROM v90_learning_episodes' in sql:
                    return SimpleNamespace(fetchone=lambda:{'n':0})
                raise AssertionError(sql[:100])
        diagnosis=dict(learning_eligible=True,primary_attribution='GOOD_EXECUTION',
                       attributions=['GOOD_EXECUTION'],learning_action='RETAIN_RULE')
        with patch.object(LI,'trade_exclusion',return_value=None), \
                patch.object(LI.AUDIT,'observed_event',return_value=('event',None)), \
                patch.object(LI.DIAGNOSTICS,'diagnose',return_value=diagnosis):
            self.assertEqual(LI._revalidate_eligible(Connection(),batch_size=4)['verified'],1)
        self.assertNotIn('LIMIT',queries[0])
        for query in (queries[0],queries[2],queries[4]):
            self.assertEqual(query.count('jsonb_to_record('),1)
            self.assertEqual(query.count('t.payload'),2)
        self.assertIn('FOR UPDATE OF e SKIP LOCKED',queries[2])
        self.assertIn('md5(trade::text)',queries[2])
        self.assertIn('AND (NOT %s OR EXISTS',queries[4])


class ReceiptSweepTests(unittest.TestCase):
    def setUp(self):
        self.saved=(LI._GENERATION,LI._REVOCATION_GENERATION,LI._UNCONFIRMED)
        LI._UNCONFIRMED=False
    def tearDown(self):
        LI._GENERATION,LI._REVOCATION_GENERATION,LI._UNCONFIRMED=self.saved

    def test_twenty_receipts_finish_despite_continuous_additions_and_restart_on_revocation(self):
        state={'phase':'recheck'};page_starts=[]
        rows=[dict(receipt_trade_id=f'trade-{i:02}',original_evidence_hash=f'hash-{i}') for i in range(20)]
        class Connection:
            def execute(self,query,params):
                after,limit=params;page_starts.append(after)
                return SimpleNamespace(fetchall=lambda:[r for r in rows if r['receipt_trade_id']>after][:limit])
        @contextmanager
        def transaction(context):yield Connection()
        def checkpoint(pg,lease,**kw):state.update(kw['cursor']);return True
        def validate_addition():
            LI.invalidate('new_closed_trade',new_evidence_only=True)
            with patch.object(LI,'_revalidate_eligible',return_value=dict(fixtures.NO_CHANGE,processed=4)):
                with LI.revalidation_transaction(fixtures.FakeConnection()) as c:LI.revalidate_eligible(c)
        worker=T.TradeLearning({'pg_connect':lambda:None})
        with patch.object(worker,'_transaction',transaction), \
                patch.object(S,'claim_job',side_effect=lambda *a,**k:dict(cursor=deepcopy(state))), \
                patch.object(S,'checkpoint_job',side_effect=checkpoint), \
                patch.object(T,'observation',side_effect=lambda row,**kw:(dict(evidence_valid=True,evidence_hash=row['original_evidence_hash']),None)), \
                patch.object(A,'snapshot',return_value={'status':'OK','profiles':[]}):
            for index in range(5):
                validate_addition()
                result=worker.process()
                self.assertEqual(result['checked'],4)
                self.assertEqual(state['phase'],'recheck')
            validate_addition()
            result=worker.process()
            self.assertEqual(page_starts,['','trade-03','trade-07','trade-11','trade-15','trade-19'])
            self.assertFalse(result['snapshot']['evidence_revalidation_pending'])
            self.assertEqual(result['snapshot']['verified_revocation_generation'],LI.memory_state()['revocation_generation'])
            validate_addition()
            self.assertFalse(worker.validation_status()['evidence_revalidation_pending'])
            LI.invalidate('old_trade_rewritten')
            self.assertTrue(worker.validation_status()['evidence_revalidation_pending'])
            validate_addition()  # Commit a no-revocation validation; old revocation remains.
            state['phase']='recheck';state['recheck_after']='trade-15'
            worker.process()
            self.assertEqual(page_starts[-1],'')
            self.assertTrue(worker.validation_status()['evidence_revalidation_pending'])


class TransactionBudgetTests(unittest.TestCase):
    def test_budget_deferral_retries_without_cursor_progress_but_sql_errors_remain_errors(self):
        from psycopg.errors import QueryCanceled
        from veritas_maintenance import MaintenanceDeferred
        for failure,expected_status in ((MaintenanceDeferred('DEFERRED_TIME_BUDGET',stage='synthetic_connect'),'RETRY'),
                                        (QueryCanceled('synthetic statement timeout'),'ERROR')):
            with self.subTest(status=expected_status):
                state={'expired':False}
                lease={'cursor':{'phase':'export','after':['2026-01-01T00:00:00+00:00','committed-trade']},
                       'last_good':{'scanned':4,'submitted':1},'fence':7}
                saved=deepcopy(lease)
                class Context:
                    sql_timeout_ms=2000
                    def check(self):
                        if state['expired']: raise failure
                class Connection:
                    @contextmanager
                    def transaction(self): yield self
                    def execute(self,sql,*args): raise failure
                @contextmanager
                def connect():
                    state['expired']=expected_status=='RETRY'
                    yield Connection()
                worker=T.TradeLearning({'pg_connect':connect})
                with patch.object(S,'claim_job',return_value=lease), \
                        patch.object(S,'checkpoint_job',return_value=True) as checkpoint:
                    with self.assertRaises(type(failure)) as raised:
                        worker.process(Context())
                self.assertIs(raised.exception,failure)
                checkpoint.assert_called_once()
                self.assertEqual(checkpoint.call_args.kwargs['status'],expected_status)
                self.assertEqual(checkpoint.call_args.kwargs['retry_after_seconds'],15)
                self.assertNotIn('cursor',checkpoint.call_args.kwargs)
                self.assertEqual(lease,saved)
                expected_result=failure.result() if expected_status=='RETRY' else {'error_type':'QueryCanceled'}
                self.assertEqual(checkpoint.call_args.kwargs['result'],expected_result)

    def test_expired_blocking_boundary_never_starts_the_learning_query(self):
        for expires_at in ('checkout','begin','statement_timeout','lock_timeout'):
            for revalidate in (False,True):
                with self.subTest(expires_at=expires_at,revalidate=revalidate):
                    state={'expired':False};events=[]
                    class Context:
                        @property
                        def sql_timeout_ms(self): return 1 if state['expired'] else 2000
                        def check(self):
                            if state['expired']: raise RuntimeError('synthetic budget expired')
                    class Connection:
                        @contextmanager
                        def transaction(self):
                            events.append('begin')
                            state['expired']=expires_at=='begin'
                            try: yield self
                            except BaseException:
                                events.append('rollback');raise
                        def execute(self,sql,*args):
                            events.append(sql)
                            if expires_at in ('statement_timeout','lock_timeout') and expires_at in sql:
                                state['expired']=True
                    @contextmanager
                    def connect():
                        state['expired']=expires_at=='checkout'
                        try: yield Connection()
                        finally: events.append('closed')
                    worker=T.TradeLearning({'pg_connect':connect})
                    with patch.object(LI,'_GENERATION',LI._GENERATION), \
                            patch.object(LI,'_REVOCATION_GENERATION',LI._REVOCATION_GENERATION), \
                            patch.object(LI,'_UNCONFIRMED',LI._UNCONFIRMED):
                        with self.assertRaisesRegex(RuntimeError,'synthetic budget expired'):
                            with worker._transaction(Context(),revalidate=revalidate):
                                self.fail('expired transaction yielded to the learning query')
                    expected=['closed'] if expires_at=='checkout' else ['begin','rollback','closed']
                    if expires_at in ('statement_timeout','lock_timeout'):
                        expected[1:1]=["SET LOCAL statement_timeout = '2000ms'","SET LOCAL lock_timeout = '250ms'"]
                    self.assertEqual(events,expected)


@unittest.skipUnless(os.getenv('VERITAS_QUALITY_TEST_DSN'),'isolated PostgreSQL test database not configured')
class TradeLearningSQLTests(unittest.TestCase):
    setUp=fixtures.DurableStateSQLTests.setUp
    tearDown=fixtures.DurableStateSQLTests.tearDown
    connect=fixtures.DurableStateSQLTests.connect

    def create_trade_tables(self):
        import veritas_portfolio as P
        self.P=P
        with self.connect() as c:
            c.execute('''CREATE TABLE paper_trades(trade_id text PRIMARY KEY,asset text,direction text,horizon text,
              status text,opened_at timestamptz,closed_at timestamptz,gross_pnl_rub float8,fees_rub float8,
              funding_rub float8,net_pnl_rub float8,payload jsonb,portfolio_name text,setup text,max_fraction float8,
              avg_entry_price float8,avg_exit_price float8)''')
        A.ensure_schema(self.connect)
        self.context=SimpleNamespace(check=lambda:None,sql_timeout_ms=2000)
        def board(force=False):return None
        self.ns={'pg_connect':self.connect,'VP':P,'setup_memory_board':board}
        self.worker=T.TradeLearning(self.ns);self.worker.ensure_schema(self.context)
    def insert(self,t):
        fields=LI.TRADE_FIELDS+('portfolio_name','setup','max_fraction','avg_entry_price','avg_exit_price')
        with self.connect() as c:
            c.execute('INSERT INTO paper_trades('+','.join(fields)+',payload) VALUES ('+
                      ','.join(['%s']*len(fields))+',%s::jsonb)',tuple(t[k] for k in fields)+(json.dumps(t['payload']),))

    def test_record_and_reused_evidence_equal_original_hash_for_native_malformed_and_large_payloads(self):
        self.create_trade_tables();trade,_=stamped_trade();self.insert(trade)
        original=deepcopy(trade['payload'])
        oversized=deepcopy(original);oversized['entry_event_snapshot']['unused']='x'*140000
        malformed=deepcopy(original)
        malformed.update(entry_event_snapshot=[],entry_canonical_admission=False,
                         normalized_units={'bad':1},price_source_lock='invalid')
        bulky=deepcopy(original);bulky['unused_history']=['x'*8192]*64
        variants=[('native',original),('bulky',bulky),('oversized',oversized),('malformed',malformed),
                  ('empty',{}),('json_null',None),('false',False),('array',[]),('string','legacy'),('sql_null',None)]
        selected='SELECT trade_id FROM paper_trades WHERE trade_id=%s'
        query=self.worker._rows_sql(selected,'FROM selected JOIN paper_trades t ON t.trade_id=selected.trade_id',evidence_hash=True)
        with self.connect() as c:
            for name,payload in variants:
                with self.subTest(shape=name):
                    c.execute('UPDATE paper_trades SET payload=%s::jsonb WHERE trade_id=%s',
                              (None if name=='sql_null' else json.dumps(payload),trade['trade_id']))
                    before=dict(c.execute('SELECT '+self.worker._projection()+','+LI.evidence_hash_sql()+
                        ' AS learning_evidence_hash FROM paper_trades t WHERE trade_id=%s',(trade['trade_id'],)).fetchone())
                    after=dict(c.execute(query,(trade['trade_id'],)).fetchone())
                    self.assertEqual(after,before)
                    record=LI.payload_record_sql()
                    row=c.execute('SELECT '+LI.evidence_hash_sql()+ ' AS original,'+
                        LI.evidence_hash_sql(root_field=lambda key:'evidence_payload.'+key)+
                        ' AS projected FROM paper_trades t '+record+' WHERE trade_id=%s',(trade['trade_id'],)).fetchone()
                    self.assertEqual(row['projected'],row['original'])
                    if name in ('native','bulky'):
                        self.assertEqual(after['payload']['entry_event_snapshot'],original['entry_event_snapshot'])
                        self.assertEqual(after['payload']['entry_canonical_admission'],original['entry_canonical_admission'])
                        self.assertNotIn('unused_history',after['payload'])

    def test_actual_export_large_payload_keeps_two_second_budget_and_reports_jit_cost(self):
        self.create_trade_tables();trade,_=stamped_trade()
        history=[{'bar':i,'close':100+i/97,
                  'synthetic_hash':hashlib.sha256(('export-synthetic-'+str(i)).encode()).hexdigest(),
                  'note':'fabricated closed-trade history'} for i in range(1800)]
        for index in range(T.BATCH_SIZE+1):
            row=deepcopy(trade);row['trade_id']='synthetic_export_'+str(index)
            row['closed_at']+=timedelta(minutes=index)
            row['payload']['synthetic_unrelated_history']=history
            self.insert(row)
        with self.connect() as c:
            before=c.execute('SELECT * FROM paper_trades ORDER BY trade_id').fetchall()
            size=c.execute('SELECT min(octet_length(payload::text)) AS source_bytes,'+
                           'min(pg_column_size(payload)) AS stored_bytes FROM paper_trades').fetchone()
        self.assertGreater(size['source_bytes'],200000)
        self.assertGreater(size['stored_bytes'],8192)
        self.assertLess(size['stored_bytes'],size['source_bytes'])
        calls=[]
        class Trace:
            def __init__(self,c): self.c=c
            def __getattr__(self,key): return getattr(self.c,key)
            def execute(self,sql,args=()):
                if 'WITH selected AS MATERIALIZED' in sql and 'AS source_evidence' in sql:
                    calls.append((sql,args))
                return self.c.execute(sql,args)
        @contextmanager
        def traced_connect():
            with self.connect() as c: yield Trace(c)
        lease=S.claim_job(self.connect,T.JOB_NAME,T.VERSION)
        self.assertTrue(S.checkpoint_job(self.connect,lease,status='OK',cursor={'phase':'export'}))
        with patch.object(T,'EPOCH','2025-01-01T00:00:00+00:00'), \
                patch.dict(self.worker.ns,pg_connect=traced_connect):
            result=self.worker.process(self.context)
        self.assertEqual(result['scanned'],T.BATCH_SIZE)
        self.assertEqual(result['submitted'],0)  # Missing verified episodes stay rejected.
        self.assertEqual(len(calls),1)
        query,args=calls[0]
        self.assertEqual(args[-1],4)
        reports={}
        with self.connect() as c:
            original_jit=c.execute('SHOW jit').fetchone()['jit']
            with c.transaction():
                c.execute("SET LOCAL statement_timeout = '2000ms'")
                expected=c.execute(query,args).fetchall()
                default=c.execute('EXPLAIN (ANALYZE, VERBOSE, FORMAT JSON) '+query,args).fetchone()['QUERY PLAN'][0]
                self.assertEqual(default['Plan']['Actual Rows'],4)
                selected=[p for p in self._plan_nodes(default['Plan']) if p.get('Subplan Name')=='CTE selected']
                self.assertEqual(len(selected),1);self.assertEqual(selected[0]['Actual Rows'],4)
                for item in expected:
                    self.assertEqual(item['payload']['entry_event_snapshot'],trade['payload']['entry_event_snapshot'])
                    self.assertEqual(item['payload']['entry_canonical_admission'],trade['payload']['entry_canonical_admission'])
                    self.assertNotIn('synthetic_unrelated_history',item['payload'])
                    self.assertIsNone(item['episode_eligible'])
                    frozen=c.execute('SELECT '+LI.evidence_hash_sql()+
                        ' AS hash FROM paper_trades t WHERE trade_id=%s',(item['trade_id'],)).fetchone()['hash']
                    self.assertEqual(item['learning_evidence_hash'],frozen)
                self.assertTrue(c.execute('SELECT pg_jit_available() AS available').fetchone()['available'])
                for setting in ('jit = on','jit_above_cost = 0','jit_inline_above_cost = 0','jit_optimize_above_cost = 0'):
                    c.execute('SET LOCAL '+setting)
                try:
                    with c.transaction():
                        forced=c.execute('EXPLAIN (ANALYZE, FORMAT JSON) '+query,args).fetchone()['QUERY PLAN'][0]
                        self.assertEqual(forced['Plan']['Actual Rows'],4)
                        self.assertIn('JIT',forced)
                        reports['forced_jit']={'execution_ms':forced['Execution Time'],'jit':forced['JIT']}
                except self.driver.errors.QueryCanceled:
                    reports['forced_jit']={'timed_out_2000ms':True}
                c.execute('SET LOCAL jit = off')
                self.assertEqual(c.execute(query,args).fetchall(),expected)
                off=c.execute('EXPLAIN (ANALYZE, FORMAT JSON) '+query,args).fetchone()['QUERY PLAN'][0]
                self.assertNotIn('JIT',off)
                reports.update(default_execution_ms=default['Execution Time'],default_jit=default.get('JIT'),
                               default_total_cost=default['Plan']['Total Cost'],jit_off_execution_ms=off['Execution Time'])
            self.assertEqual(c.execute('SHOW jit').fetchone()['jit'],original_jit)
            self.assertEqual(c.execute('SELECT * FROM paper_trades ORDER BY trade_id').fetchall(),before)
            self.assertEqual(c.execute('SELECT count(*) AS n FROM learning_trade_receipts').fetchone()['n'],0)
        print(json.dumps(dict(event='synthetic_trade_export_plan',rows=4,sql_bytes=len(query),
                              statement_timeout_ms=2000,**dict(size),**reports)))

    def _plan_nodes(self,plan):
        yield plan
        for child in plan.get('Plans',[]): yield from self._plan_nodes(child)

    def test_selected_order_and_missing_trade_receipt_survive_record_projection(self):
        self.create_trade_tables();trade,_=stamped_trade()
        for index in (3,1,2):
            row=deepcopy(trade);row['trade_id']=str(index);row['closed_at']+=timedelta(minutes=index)
            self.insert(row)
        with self.connect() as c:
            query=self.worker._rows_sql('SELECT trade_id FROM paper_trades ORDER BY closed_at,trade_id LIMIT 2',
                'FROM selected JOIN paper_trades t ON t.trade_id=selected.trade_id',evidence_hash=True)
            self.assertEqual([r['trade_id'] for r in c.execute(query).fetchall()],['1','2'])
            c.execute("INSERT INTO learning_trade_receipts VALUES('missing','ETH','event','hash','{}'::jsonb,TRUE,now())")
            source='FROM selected JOIN learning_trade_receipts r ON r.trade_id=selected.trade_id LEFT JOIN paper_trades t ON t.trade_id=r.trade_id'
            optimized=self.worker._rows_sql('SELECT trade_id FROM learning_trade_receipts',source,
                extra_columns=(('r.trade_id','receipt_trade_id'),),evidence_hash=True,order='receipt_trade_id')
            after=dict(c.execute(optimized).fetchone())
            before=dict(c.execute('SELECT '+self.worker._projection()+',r.trade_id AS receipt_trade_id,'+
                LI.evidence_hash_sql()+' AS learning_evidence_hash FROM learning_trade_receipts r '+
                'LEFT JOIN paper_trades t ON t.trade_id=r.trade_id').fetchone())
            self.assertEqual(after,before)
            self.assertIsNone(after['trade_id']);self.assertEqual(after['receipt_trade_id'],'missing')

    def test_all_phases_replay_without_duplicate_learning_and_revoke_removed_event(self):
        self.create_trade_tables();t,candidate=stamped_trade();self.insert(t)
        with patch.object(T,'EPOCH','2025-01-01T00:00:00+00:00'),patch.object(self.P,'V90_Q2_STARTED_AT','2025-01-01T00:00:00+00:00'):
            material=self.worker.process(self.context);self.assertEqual(material['materialized'],1)
            validated=self.worker.process(self.context);self.assertEqual(validated['validation']['verified'],1)
            exported=self.worker.process(self.context);self.assertEqual(exported['submitted'],1)
            # first recheck page then another cycle's empty page certifies epoch
            for _ in range(5):self.worker.process(self.context)
            with self.connect() as c:
                self.assertEqual(c.execute('SELECT count(*) AS n FROM autonomous_learning_seen').fetchone()['n'],1)
                self.assertEqual(c.execute('SELECT count(*) AS n FROM learning_trade_receipts').fetchone()['n'],1)
                c.execute("UPDATE paper_trades SET payload=payload-'entry_event_snapshot'")
            for _ in range(8):self.worker.process(self.context)
            with self.connect() as c:
                self.assertFalse(c.execute('SELECT valid FROM autonomous_learning_seen').fetchone()['valid'])
                self.assertFalse(c.execute('SELECT valid FROM learning_trade_receipts').fetchone()['valid'])

    def test_materialize_repairs_missing_outcome_hash(self):
        source=inspect.getsource(T.TradeLearning.process)
        self.assertIn("NULLIF(e.payload->>'outcome_evidence_hash','') IS NULL",source)
        self.assertIn("LI.DIAGNOSTICS.VERSION",source)

    def test_materialization_failure_rolls_back_without_advancing_cursor(self):
        self.create_trade_tables();t,_=stamped_trade();self.insert(t)
        with patch.object(T,'EPOCH','2025-01-01T00:00:00+00:00'),patch.object(self.P,'_v90r29_upsert_episode',side_effect=RuntimeError('write failed')):
            with self.assertRaisesRegex(RuntimeError,'write failed'):self.worker.process(self.context)
        state=S.job_state(self.connect,T.JOB_NAME,T.VERSION)
        self.assertEqual(state['status'],'ERROR');self.assertIsNone(state['cursor'])
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) AS n FROM v90_learning_episodes').fetchone()['n'],0)

    def test_sql_staging_distinguishes_new_labels_from_changed_verified_evidence(self):
        self.create_trade_tables();t,_=stamped_trade();self.insert(t)
        before=LI.memory_state()['revocation_generation']
        with patch.object(T,'EPOCH','2025-01-01T00:00:00+00:00'),patch.object(self.P,'V90_Q2_STARTED_AT','2025-01-01T00:00:00+00:00'):
            self.worker.process(self.context)
            result=self.worker.process(self.context)['validation']
            self.assertEqual(result['verified'],1);self.assertEqual(result['revoked'],0)
            self.assertEqual(LI.memory_state()['revocation_generation'],before)
            with self.connect() as c:
                c.execute("UPDATE paper_trades SET payload=payload-'observation_path'")
                with LI.revalidation_transaction(c):result=LI.revalidate_eligible(c,batch_size=4)
            self.assertEqual(result['staged'],1);self.assertEqual(result['revoked'],1)
            self.assertEqual(result['excluded'],1)
            self.assertGreater(LI.memory_state()['revocation_generation'],before)
