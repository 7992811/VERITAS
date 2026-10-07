"""Native quote-breakout evidence must be learned without relaxing old guards."""
from copy import deepcopy
from datetime import timedelta
import json
import os
import unittest

import veritas_observation_path as O
import veritas_structural_breakout as B
import veritas_trade_audit as A
import veritas_trade_diagnostics as D
import veritas_learning_integrity as LI
from test_veritas_structural_breakout import raw_at, POLICY
import test_veritas_learning_state as state_tests


def quote_trade(net=30):
    raw, at = raw_at()
    event=B.build_context(raw,'5m',at,config=dict(POLICY,atr_period=20))["event"]
    source=deepcopy(event['source_identity'])
    p=dict(data_integrity_status='OK',price_source_lock=source,
           entry_execution_source_identity=deepcopy(source),last_exit_source_identity=deepcopy(source),
           r66_event_id=event['event_id'],entry_event_snapshot=event,
           entry_execution_model=dict(fill_price=raw['price'],asset=raw['asset'],side='BUY'),
           initial_stop_price=event['stop_price'],entry_atr=event['atr'],
           atr_timeframe=event['atr_timeframe'],stop_timeframe=event['stop_timeframe'],
           target_timeframe=event['target_timeframe'])
    t=dict(trade_id='QUOTE',asset=event['asset'],direction='LONG',horizon='5m',
           opened_at=at,closed_at=at+timedelta(seconds=60),status='CLOSED',
           gross_pnl_rub=net+2.,fees_rub=1.,funding_rub=1.,net_pnl_rub=net,payload=p)
    for index in range(3):
        clock=at+timedelta(seconds=index*30)
        quote=dict(raw,observed_at=clock.isoformat(),price=raw['price']+index*.1)
        p['observation_path']=O.observe(t,quote,clock,at_entry=index==0)
    return t


class QuoteLearningTests(unittest.TestCase):
    def test_native_quote_event_preserves_parent_atr_and_historical_targets(self):
        for net in (30.,-30.):
            with self.subTest(net=net):
                t=quote_trade(net); before=deepcopy(t)
                result=D.diagnose(t)
                self.assertTrue(A.observed_event(t)[1]); self.assertIsNone(LI.trade_exclusion(t))
                self.assertTrue(result['learning_eligible'],result)
                self.assertEqual(result['normalization']['timeframe'],'5m')
                self.assertEqual(result['normalization']['atr_timeframe'],'1h')
                self.assertEqual(result['normalization']['target_timeframe'],'HISTORICAL_ZONES')
                self.assertEqual(t,before)

    def test_missing_or_tampered_proofs_never_become_learning(self):
        def event(t): return t['payload']['entry_event_snapshot']
        cases={
            'seal':lambda t:event(t).update(proof_hash='bad'),
            'bar':lambda t:event(t)['atr_proof']['bars'][0].update(close=1.),
            'source':lambda t:t['payload']['price_source_lock'].update(contract_id='OTHER'),
            'confirmation':lambda t:event(t).update(confirmation='CLOSED_5m_BAR'),
            'target':lambda t:event(t)['target_ladder'][0].update(price=1.),
            'path':lambda t:t['payload'].pop('observation_path'),
            'gap':lambda t:t['payload']['observation_path'].update(max_gap_seconds=99.,gap_count=1),
            'fill':lambda t:t['payload'].pop('entry_execution_model'),
            'atr':lambda t:t['payload'].pop('entry_atr'),
            'accounting':lambda t:t.update(fees_rub=None),
            'admin':lambda t:t['payload'].update(exit_reason='ADMINISTRATIVE_REBASE'),
        }
        for name,mutate in cases.items():
            with self.subTest(name=name):
                t=quote_trade(); mutate(t)
                self.assertFalse(D.diagnose(t)['learning_eligible'])
                self.assertIsNotNone(LI.trade_exclusion(t))

    def test_wrong_fill_stop_timeframe_and_late_entry_remain_rule_violations(self):
        for name in ('stop','timeframe','late','extension'):
            with self.subTest(name=name):
                t=quote_trade()
                if name=='stop': t['payload']['initial_stop_price']-=2.
                if name=='timeframe': t['horizon']='1m'
                if name=='late': t['opened_at']+=timedelta(hours=1)
                if name=='extension': t['payload']['entry_execution_model']['fill_price']=106.
                result=D.diagnose(t)
                self.assertFalse(result['learning_eligible']); self.assertTrue(result['rule_evidence_eligible'],result)


@unittest.skipUnless(os.getenv('VERITAS_QUALITY_TEST_DSN'),'isolated PostgreSQL test database not configured')
class QuoteLearningSQLTests(unittest.TestCase):
    setUp=state_tests.DurableStateSQLTests.setUp
    tearDown=state_tests.DurableStateSQLTests.tearDown
    connect=state_tests.DurableStateSQLTests.connect

    def test_sql_projection_keeps_exact_native_seal_and_bounds_oversized_events(self):
        t=quote_trade(); p=t['payload']
        with self.connect() as c:
            projected=c.execute('SELECT '+LI.payload_sql('t')+' AS payload FROM (SELECT %s::jsonb AS payload) t',
                                (json.dumps(p),)).fetchone()['payload']
            altered=dict(t,payload=projected)
            self.assertEqual(projected['entry_event_snapshot'],p['entry_event_snapshot'])
            self.assertEqual(D.diagnose(altered),D.diagnose(t))
            huge=deepcopy(p); huge['entry_event_snapshot']['unused']='x'*140000
            projected=c.execute('SELECT '+LI.payload_sql('t')+' AS payload FROM (SELECT %s::jsonb AS payload) t',
                                (json.dumps(huge),)).fetchone()['payload']
            self.assertLess(len(json.dumps(projected)),20000)
            self.assertFalse(D.diagnose(dict(t,payload=projected))['learning_eligible'])

    def test_obsolete_quote_diagnosis_retries_once_without_promoting_manual_or_path_exclusions(self):
        with self.connect() as c:
            c.execute('''CREATE TABLE paper_trades(trade_id text PRIMARY KEY,asset text,direction text,horizon text,
                status text,opened_at timestamptz,closed_at timestamptz,gross_pnl_rub float8,fees_rub float8,
                funding_rub float8,net_pnl_rub float8,payload jsonb)''')
            c.execute('''CREATE TABLE v90_learning_episodes(trade_id text PRIMARY KEY,asset text,direction text,
                horizon text,closed_at timestamptz,primary_attribution text,attributions jsonb,
                learning_action text,learning_eligible boolean,payload jsonb)''')
            for kind in ('clean','badpath','manual'):
                t=quote_trade();t['trade_id']=kind
                if kind=='badpath':t['payload'].pop('observation_path')
                c.execute('INSERT INTO paper_trades VALUES('+','.join(['%s']*len(LI.TRADE_FIELDS))+',%s::jsonb)',
                          tuple(t[k] for k in LI.TRADE_FIELDS)+(json.dumps(t['payload']),))
                p=dict(diagnostics_version='SAME_TF_TRADE_DIAGNOSTICS_V1',
                       learning_exclusion_reason='UNVERIFIED_EVENT_PROVENANCE')
                if kind=='manual':p['learning_exclusion_reason']='INCOMPLETE_EVIDENCE'
                c.execute('''INSERT INTO v90_learning_episodes VALUES
                    (%s,%s,%s,%s,%s,'UNVERIFIED_TRADE_EVIDENCE','["UNVERIFIED_TRADE_EVIDENCE"]',
                     'REVIEW_ORIGINAL_EVIDENCE',FALSE,%s::jsonb)''',
                    tuple(t[k] for k in ('trade_id','asset','direction','horizon','closed_at'))+(json.dumps(p),))
            before=c.execute('SELECT * FROM paper_trades ORDER BY trade_id').fetchall()
            with LI.revalidation_transaction(c):
                result=LI.revalidate_eligible(c,batch_size=8)
            self.assertEqual(result['retry_staged'],2)
            rows={r['trade_id']:r for r in c.execute('SELECT * FROM v90_learning_episodes').fetchall()}
            self.assertTrue(rows['clean']['learning_eligible'])
            self.assertFalse(rows['badpath']['learning_eligible']);self.assertFalse(rows['manual']['learning_eligible'])
            self.assertNotIn('learning_integrity',rows['manual']['payload'])
            self.assertEqual(before,c.execute('SELECT * FROM paper_trades ORDER BY trade_id').fetchall())
            token=LI.generation()
            with LI.revalidation_transaction(c):
                again=LI.revalidate_eligible(c,batch_size=8)
            self.assertEqual(again['staged'],0);self.assertEqual(again['processed'],0)
            self.assertEqual(LI.generation(),token)
