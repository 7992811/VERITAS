"""Real cash-flow normalization, prospective proof and bounded job regressions."""
from copy import deepcopy
from contextlib import contextmanager
from datetime import timedelta
import json
import os
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
