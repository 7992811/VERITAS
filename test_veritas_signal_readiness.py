"""Display-only readiness must not promote a source or refresh old decisions."""
import ast
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

from veritas_signal_readiness import project_readiness, readiness_fields


NOW = datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc)


def signal():
    return {'asset': 'ETH', 'horizon': '5m', 'research_decision': 'SHORT',
            'source_gate_pass': True, 'market_open': True, 'paper_eligible': True,
            'execution_eligible': True, 'market_observed_at': '2026-10-07T07:59:55+00:00',
            'trade_plan': {'eligible': True,
                'entry_timing_gate': {'eligible': True, 'reason': 'SAME_TF_ENTRY_READY'},
                'final_economics_gate': {'eligible': True, 'status': 'PASS', 'blockers': [],
                    'net_reward_risk': 1.3, 'context_freshness': {
                        'closed_at': NOW.timestamp()-300, 'age_seconds': 300.}}}}


class SignalReadinessTests(unittest.TestCase):
    def test_source_admission_alone_is_not_trade_readiness(self):
        row=signal();row.pop('trade_plan')
        before=copy.deepcopy(row)
        result=readiness_fields(row)
        self.assertFalse(result['trade_entry_eligible'])
        self.assertEqual(result['trade_entry_reason'],'TRADE_PLAN_NOT_CHECKED')
        self.assertIsNone(result['trade_entry_checked_at'])
        self.assertEqual(row,before)
        self.assertTrue(row['execution_eligible'])

    def test_complete_plan_retains_actual_decision_time_not_market_or_current_time(self):
        row=signal();before=copy.deepcopy(row)
        result=readiness_fields(row)
        self.assertTrue(result['trade_entry_eligible'])
        self.assertEqual(result['trade_entry_reason'],'TRADE_PLAN_READY')
        self.assertEqual(result['trade_entry_checked_at'],NOW.isoformat())
        self.assertEqual(result['trade_entry_basis'],'FINAL_PLAN')
        self.assertEqual(row,before)

    def test_timing_reason_precedes_but_does_not_hide_net_cost_blockers(self):
        for reason in ('SAME_TF_CONTEXT_STALE','SAME_TF_DIRECTION_CONFLICT',
                       'SAME_TF_TARGET_ALREADY_REACHED','SAME_TF_EVENT_EXPIRED'):
            row=signal();p=row['trade_plan'];p['eligible']=False
            p['entry_timing_gate']={'eligible':False,'reason':reason}
            p['final_economics_gate'].update(eligible=False,status='BLOCK',blockers=[
                'TARGET_NOT_PROFITABLE_AFTER_COSTS','NET_REWARD_RISK_BELOW_FLOOR',reason])
            result=readiness_fields(row)
            self.assertFalse(result['trade_entry_eligible'])
            self.assertEqual(result['trade_entry_reason'],reason)
            self.assertEqual(result['trade_entry_blockers'],[reason,
                'TARGET_NOT_PROFITABLE_AFTER_COSTS','NET_REWARD_RISK_BELOW_FLOOR'])

    def test_no_direction_or_source_failure_cannot_claim_ready(self):
        for field,value,reason in (
            ('research_decision','NO_TRADE','NO_DIRECTION'),
            ('source_gate_pass',False,'PRIMARY_SOURCE_GATE_FAILED'),
            ('market_open',False,'MARKET_SESSION_CLOSED'),
            ('paper_eligible',False,'PAPER_SOURCE_NOT_ELIGIBLE')):
            row=signal();row[field]=value
            result=readiness_fields(row)
            self.assertFalse(result['trade_entry_eligible'])
            self.assertEqual(result['trade_entry_reason'],reason)

    def test_production_denial_does_not_change_paper_readiness(self):
        row=signal();row.update(execution_eligible=False,production_eligible=False)
        self.assertTrue(readiness_fields(row)['trade_entry_eligible'])

    def test_missing_or_invalid_decision_time_is_not_replaced_by_quote_time(self):
        for freshness in ({}, {'closed_at':NOW.isoformat(),'age_seconds':float('nan')},
                          {'closed_at':NOW.timestamp(),'age_seconds':True}):
            row=signal();row['trade_plan']['final_economics_gate']['context_freshness']=freshness
            result=readiness_fields(row)
            self.assertFalse(result['trade_entry_eligible'])
            self.assertEqual(result['trade_entry_reason'],'TRADE_ENTRY_TIME_MISSING')
            self.assertIsNone(result['trade_entry_checked_at'])

    def test_explicit_saved_clock_and_canonical_decline_override_an_old_ready_plan(self):
        row=signal()
        canonical={'open':False,'reason':'SAME_TF_CONTEXT_STALE',
                   'checked_at':'2026-10-07T08:00:30+00:00','hard_blockers':['SAME_TF_CONTEXT_STALE']}
        result=readiness_fields(row,canonical=canonical)
        self.assertFalse(result['trade_entry_eligible'])
        self.assertEqual(result['trade_entry_reason'],'SAME_TF_CONTEXT_STALE')
        self.assertEqual(result['trade_entry_checked_at'],canonical['checked_at'])
        self.assertEqual(result['trade_entry_basis'],'CANONICAL_ADMISSION')

    def test_canonical_permission_is_separate_from_an_executed_order(self):
        result=readiness_fields(signal(),canonical={'open':True,'checked_at':NOW.isoformat()})
        self.assertTrue(result['trade_entry_eligible'])
        self.assertEqual(result['trade_entry_reason'],'CANONICAL_ENTRY_ADMITTED')
        self.assertNotIn('execution',result)
        self.assertNotIn('EXECUTED',json.dumps(result))

    def test_canonical_permission_cannot_borrow_a_different_plan_clock(self):
        result=readiness_fields(signal(),canonical={'open':True})
        self.assertFalse(result['trade_entry_eligible'])
        self.assertEqual(result['trade_entry_reason'],'TRADE_ENTRY_TIME_MISSING')
        self.assertIsNone(result['trade_entry_checked_at'])

    def test_incomplete_or_contradictory_final_economics_does_not_become_ready(self):
        for gate in ({'status':'PASS'}, {'eligible':True,'status':'BLOCK'}):
            row=signal();row['trade_plan']['final_economics_gate']=gate
            result=readiness_fields(row,checked_at=NOW)
            self.assertFalse(result['trade_entry_eligible'])
            self.assertEqual(result['trade_entry_reason'],'TRADE_PLAN_NOT_CHECKED')

    def test_complete_fields_are_bounded_and_do_not_copy_market_history(self):
        row=signal();row['native_daily_bars']=[{'close':123}]*1000
        row['trade_plan']['timeframe_entry_context']={'bars':[{'close':456}]*1000}
        row['trade_plan']['final_economics_gate']['blockers']=['FAIL_'+str(i) for i in range(100)]
        row['trade_plan']['eligible']=False
        result=readiness_fields(row)
        self.assertLessEqual(len(result['trade_entry_blockers']),24)
        self.assertLess(len(json.dumps(result)),1500)
        self.assertFalse(any(isinstance(v,dict) for v in result.values()))
        self.assertNotIn('native_daily_bars',result)

    def test_legacy_reason_prefix_preserves_codes_without_free_text(self):
        row=signal();row['trade_plan'].update(eligible=False,
            reason='final_economics_gate:NET_REWARD_RISK_BELOW_FLOOR,SAME_TF_DIRECTION_CONFLICT')
        row['trade_plan']['final_economics_gate']={}
        result=readiness_fields(row,checked_at=NOW)
        self.assertEqual(result['trade_entry_blockers'],[
            'NET_REWARD_RISK_BELOW_FLOOR','SAME_TF_DIRECTION_CONFLICT'])
        self.assertEqual(result['trade_entry_checked_at'],NOW.isoformat())

    def test_stale_snapshot_cannot_reuse_permission_or_a_new_display_clock(self):
        row=signal();row.update(readiness_fields(row),snapshot_stale=True)
        row['market_observed_at']='2026-10-07T19:39:15+00:00'
        result=readiness_fields(row)
        self.assertFalse(result['trade_entry_eligible'])
        self.assertEqual(result['trade_entry_reason'],'SIGNAL_SNAPSHOT_STALE')
        self.assertEqual(result['trade_entry_checked_at'],NOW.isoformat())
        self.assertEqual(row['trade_entry_eligible'],True)

    def test_stale_snapshot_preserves_the_recorded_plan_blockers(self):
        row=signal();row['snapshot_stale']=True
        row['trade_plan']['eligible']=False
        row['trade_plan']['entry_timing_gate']={
            'eligible':False,'reason':'STRUCTURAL_EVENT_EXPIRED'}
        result=readiness_fields(row)
        self.assertFalse(result['trade_entry_eligible'])
        self.assertEqual(result['trade_entry_blockers'],[
            'SIGNAL_SNAPSHOT_STALE','STRUCTURAL_EVENT_EXPIRED'])
        self.assertEqual(result['trade_entry_checked_at'],NOW.isoformat())

    @unittest.skipUnless(shutil.which('node'),'Node required for entry readiness UI regression')
    def test_ui_keeps_research_direction_separate_from_entry_decision(self):
        result=subprocess.run(['node','tools/test_signal_readiness_ui.cjs'],
            cwd=Path(__file__).resolve().parent,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


class PublicSignalReadinessTests(unittest.TestCase):
    """Exercise the real API read boundary without booting services or a DB."""

    @classmethod
    def setUpClass(cls):
        path=Path(__file__).with_name('veritas_intelligence.py')
        tree=ast.parse(path.read_text())
        names={'fresh_cycle_snapshot','latest_signal_summary_pg',
               '_v90_small_dict','_v90_compact_live_row'}
        nodes=[node for node in tree.body if
               isinstance(node,ast.FunctionDef) and node.name in names or
               isinstance(node,ast.Assign) and any(isinstance(target,ast.Name)
                   and target.id=='_V90_QUOTE_IDENTITY_FIELDS' for target in node.targets)]
        cls.module=compile(ast.Module(body=nodes,type_ignores=[]),str(path),'exec')

    def setUp(self):
        self.assets=('BTC','ETH','NQ','BRENT','GOLD','MOEX','CNYRUBF')
        self.horizons=('1m','5m','1h','4h','1d','3d','7d')
        self.ns={'lock':threading.RLock(),'last_cycle':{'status':'warming','summary':[]},
                 'DISPLAY_ASSETS':self.assets,'HORIZONS':self.horizons,
                 'json':json,'emit':Mock(),'pg_enabled':lambda:True,
                 'VERSION':'test','now':lambda:'2026-10-07T19:28:39+00:00'}
        exec(self.module,self.ns)

    def matrix(self):
        return [dict(signal(),asset=asset,horizon=horizon)
                for asset in self.assets for horizon in self.horizons]

    def test_real_postgres_fallback_supplies_all_fields_without_promoting_cold_rows(self):
        row=signal()
        payload={'decision':row['research_decision'],'features':{
                    'price':100,'market_observed_at':row['market_observed_at']},
                 'trade_plan':row['trade_plan'],'gates':{'source':True,'time':True},
                 'execution_eligibility':{'eligible':True,'paper_eligible':True}}
        records=[{'asset':asset,'horizon':horizon,'event_ts':NOW.isoformat(),
                  'payload':copy.deepcopy(payload)}
                 for asset in self.assets for horizon in self.horizons]
        db=Mock()
        db.execute.return_value.fetchall.return_value=records
        manager=Mock(__enter__=Mock(return_value=db),__exit__=Mock(return_value=False))
        self.ns['pg_connect']=lambda:manager
        restored=self.ns['fresh_cycle_snapshot']()
        self.assertEqual(restored['summary_count'],49)
        self.assertEqual(restored['summary_source'],'live_memory+postgres_cold_fallback')
        for row in restored['summary']:
            self.assertEqual(row['trade_entry_reason'],'SIGNAL_SNAPSHOT_STALE')
            self.assertFalse(row['trade_entry_eligible'])
            self.assertEqual(row['trade_entry_checked_at'],NOW.isoformat())
        self.assertNotIn('trade_entry_eligible',records[0]['payload'])
        self.assertEqual(self.ns['last_cycle']['summary'],[])

    def test_actual_structural_publish_does_not_inherit_cold_cycle_block_or_time(self):
        import veritas_breakout_runtime as BR
        cold=self.matrix()
        self.ns['latest_signal_summary_pg']=Mock(return_value=cold)
        with patch.object(BR.TFD,'restore_context_state'):
            BR.restore_snapshot(self.ns)
        runtime=BR.BreakoutRuntime(self.ns,lambda *args,**kwargs:{})
        fresh=signal();fresh['snapshot_stale']=False
        observed='2026-10-07T19:39:15+00:00'
        checked='2026-10-07T19:39:27+00:00'
        fresh.update(market_observed_at=observed,_breakout_checked_at=checked)
        fresh['trade_plan']['final_economics_gate']['checked_at']=checked
        with BR._cache_lock:
            previous=BR._latest_rows.copy()
            BR._latest_rows.clear()
        try:
            self.ns['now']=lambda:checked
            runtime._publish_rows([fresh],{'status':'OK','portfolios':[]},
                                  datetime.fromisoformat(checked))
            snapshot=self.ns['fresh_cycle_snapshot']()
            self.assertEqual(snapshot['status'],'warming')
            self.assertEqual(snapshot['at'],'2026-10-07T19:28:39+00:00')
            current=next(row for row in snapshot['summary']
                         if (row['asset'],row['horizon'])==('ETH','5m'))
            self.assertTrue(current['trade_entry_eligible'])
            self.assertEqual(current['trade_entry_checked_at'],checked)
            self.assertEqual(sum(row['trade_entry_eligible'] for row in snapshot['summary']),1)
            self.assertEqual(sum(row['trade_entry_reason']=='SIGNAL_SNAPSHOT_STALE'
                                 for row in snapshot['summary']),48)
            self.assertNotIn('trade_entry_eligible',fresh)
            self.assertIs(current['_execution_audit'],fresh['_execution_audit'])
            self.ns['latest_signal_summary_pg'].assert_called_once()
        finally:
            runtime.close()
            with BR._cache_lock:
                BR._latest_rows.clear()
                BR._latest_rows.update(previous)

    def test_new_blocked_overlay_cannot_inherit_an_older_ready_boolean(self):
        rows=self.matrix();row=rows[0]
        row.update(readiness_fields(row),snapshot_stale=False)
        row['trade_plan']['eligible']=False
        row['trade_plan']['entry_timing_gate']={
            'eligible':False,'reason':'STRUCTURAL_EVENT_EXPIRED'}
        self.ns['last_cycle']['summary']=rows
        self.ns['latest_signal_summary_pg']=Mock(side_effect=AssertionError('unexpected DB read'))
        projected=self.ns['fresh_cycle_snapshot']()['summary'][0]
        self.assertFalse(projected['trade_entry_eligible'])
        self.assertEqual(projected['trade_entry_reason'],'STRUCTURAL_EVENT_EXPIRED')
        self.assertTrue(row['trade_entry_eligible'])

    def test_compaction_round_trip_retains_exact_decision_clock_and_staleness(self):
        for location in ('gate','plan'):
            row=signal();gate=row['trade_plan']['final_economics_gate']
            gate.pop('context_freshness')
            stamp='2026-10-07T19:39:27.546377+00:00'
            if location=='gate':gate['checked_at']=stamp
            else:row['trade_plan']['trade_entry_checked_at']=stamp
            for stale in (False,True):
                with self.subTest(location=location,stale=stale):
                    row['snapshot_stale']=stale
                    compact=self.ns['_v90_compact_live_row'](row)
                    repeated=self.ns['_v90_compact_live_row'](compact)
                    projected=readiness_fields(repeated)
                    self.assertEqual(projected['trade_entry_checked_at'],stamp)
                    self.assertEqual(projected['trade_entry_eligible'],not stale)
                    self.assertEqual(repeated['snapshot_stale'],stale)

    def test_api_projection_does_not_copy_or_mutate_history_or_execution_graphs(self):
        class RetainedProof:
            def __deepcopy__(self,memo):
                raise AssertionError('readiness copied retained history')
        rows=self.matrix();proof=RetainedProof();audit={'execution':{'status':'EXECUTED'}}
        rows[0]['native_daily_bars']=proof
        rows[0]['_execution_audit']=audit
        self.ns['last_cycle']['summary']=rows
        projected=self.ns['fresh_cycle_snapshot']()['summary'][0]
        self.assertIs(projected['native_daily_bars'],proof)
        self.assertIs(projected['_execution_audit'],audit)
        self.assertNotIn('trade_entry_eligible',rows[0])
        self.assertLess(len(json.dumps({key:value for key,value in projected.items()
                                      if key.startswith('trade_entry_')})),1500)


def evaluated_fast_signal():
    """Production structural/final gates, with synthetic causal native bars."""
    import veritas_price_source as VPS
    import veritas_structural_breakout as SB
    import veritas_timeframe_policy as TFP
    from test_veritas_structural_breakout import POLICY
    from test_veritas_weighted_structural_economics import structural_plan
    raw, clock, _ = structural_plan(direction='SHORT')
    raw.update(asset='ETH', source='Binance', contract={})
    source = VPS.identity('ETH', raw)
    raw['structure_source_identity'] = source
    for bars in raw['structure_bars_by_timeframe'].values():
        for bar in bars:
            bar['source_identity'] = copy.deepcopy(source)
    context = SB.build_context(raw, '5m', clock, config=POLICY)
    row = dict(raw, horizon='5m', research_decision='SHORT', paper_eligible=True,
               execution_eligible=True, market_observed_at=raw['observed_at'],
               timeframe_entry_context=context, _execution_quote=copy.deepcopy(raw),
               trade_plan={'horizon':'5m', 'timeframe_entry_context':context})
    row = TFP.prepare_row(row, now=clock)
    row['trade_plan'] = TFP.final_plan('ETH', 'SHORT', row['trade_plan'], now=clock)
    if not row['trade_plan']['eligible']:
        raise AssertionError(row['trade_plan']['reason'])
    row.update(snapshot_stale=False, _breakout_runtime='STRUCTURAL_QUOTE_RUNTIME_V1',
               _breakout_checked_at=clock.isoformat())
    return row, clock


class ReadSideExpiryTests(unittest.TestCase):
    def test_fresh_deadline_preserves_recorded_clock_plan_and_execution(self):
        row=signal();audit={'result':{'status':'EXECUTED','order_id':'saved'}}
        row['_execution_audit']=audit
        before=copy.deepcopy(row);plan=row['trade_plan']
        projected=project_readiness(row,now=NOW)
        self.assertTrue(projected['trade_entry_eligible'])
        self.assertEqual(projected['trade_entry_valid_until'],(NOW+timedelta(seconds=25)).isoformat())
        self.assertEqual(projected['trade_entry_expiry_reason'],'EXECUTION_QUOTE_STALE')
        self.assertEqual(projected['trade_entry_checked_at'],NOW.isoformat())
        self.assertEqual(row,before)
        self.assertIs(row['_execution_audit'],audit)
        self.assertIs(row['trade_plan'],plan)
        self.assertLess(len(json.dumps(projected)),1500)
        expired=project_readiness(row,now=NOW+timedelta(seconds=600))
        self.assertFalse(expired['trade_entry_eligible'])
        self.assertEqual(expired['trade_entry_reason'],'EXECUTION_QUOTE_STALE')
        self.assertEqual(expired['trade_entry_checked_at'],NOW.isoformat())
        self.assertEqual(row,before)

    def test_invalid_explicit_quote_cannot_borrow_fresh_display_or_decision_time(self):
        for quote in (None, {}, {'observed_at':'invalid'}, {'observed_at':False},
                      {'observed_at':float('nan')}, {'observed_at':None}):
            with self.subTest(quote=quote):
                row=signal();row['_execution_quote']=quote
                projected=project_readiness(row,now=NOW)
                self.assertFalse(projected['trade_entry_eligible'])
                self.assertEqual(projected['trade_entry_reason'],'QUOTE_TIME_MISSING')
                self.assertIsNone(projected['trade_entry_valid_until'])
                self.assertEqual(projected['trade_entry_checked_at'],NOW.isoformat())
        row=signal();row.pop('market_observed_at')
        self.assertEqual(project_readiness(row,now=NOW)['trade_entry_reason'],'QUOTE_TIME_MISSING')
        row['market_observed_at']=(NOW+timedelta(seconds=6)).isoformat()
        self.assertEqual(project_readiness(row,now=NOW)['trade_entry_reason'],'QUOTE_TIME_FUTURE')

    def test_delayed_paper_uses_recorded_limit_and_does_not_widen_structural_quotes(self):
        row=signal();observed=NOW-timedelta(seconds=600)
        row['market_observed_at']=observed.isoformat()
        row['trade_plan']['final_economics_gate']['quote_time_gate']={
            'eligible':True,'paper_delayed_research':True,'observed_at':observed.isoformat(),
            'max_age_seconds':3600.,'age_seconds':600.}
        projected=project_readiness(row,now=NOW)
        self.assertTrue(projected['trade_entry_eligible'])
        self.assertEqual(projected['trade_entry_valid_until'],(observed+timedelta(seconds=3600)).isoformat())
        self.assertEqual(projected['trade_entry_expiry_reason'],'DELAYED_RESEARCH_QUOTE_STALE')
        self.assertFalse(project_readiness(row,now=observed+timedelta(seconds=3601))['trade_entry_eligible'])
        row['_breakout_runtime']='STRUCTURAL_QUOTE_RUNTIME_V1'
        row['trade_plan']['entry_timing_gate'].update(signal_at=observed.timestamp(),max_age_seconds=6000)
        self.assertEqual(project_readiness(row,now=NOW)['trade_entry_reason'],'EXECUTION_QUOTE_STALE')
        row.pop('_breakout_runtime');row['trade_plan']['final_economics_gate'].pop('quote_time_gate')
        row['data_latency_class']='DELAYED_RESEARCH'
        self.assertFalse(project_readiness(row,now=NOW)['trade_entry_eligible'])

    def test_event_expires_with_a_still_fresh_quote_and_preserves_original_event(self):
        import veritas_structural_breakout as SB
        import veritas_timeframe_policy as TFP
        row, first=evaluated_fast_signal()
        later=first+timedelta(seconds=590)
        quote=copy.deepcopy(row['_execution_quote']);quote['observed_at']=later.isoformat()
        context=SB.rebind_quote(row['timeframe_entry_context'],quote,now=later)
        row.update(_execution_quote=quote, market_observed_at=later.isoformat(),
                   timeframe_entry_context=context)
        row['trade_plan']=dict(row['trade_plan'],timeframe_entry_context=context)
        row=TFP.prepare_row(row,now=later)
        row['trade_plan']=TFP.final_plan('ETH','SHORT',row['trade_plan'],now=later)
        self.assertTrue(row['trade_plan']['eligible'],row['trade_plan']['reason'])
        event_before=copy.deepcopy(context['event'])
        ready=project_readiness(row,now=later)
        self.assertTrue(ready['trade_entry_eligible'])
        self.assertEqual(ready['trade_entry_expiry_reason'],'STRUCTURAL_EVENT_EXPIRED')
        self.assertEqual(ready['trade_entry_valid_until'],(first+timedelta(seconds=600)).isoformat())
        self.assertTrue(project_readiness(row,now=first+timedelta(seconds=600))['trade_entry_eligible'])
        expired=project_readiness(row,now=first+timedelta(seconds=601))
        self.assertFalse(expired['trade_entry_eligible'])
        self.assertEqual(expired['trade_entry_reason'],'STRUCTURAL_EVENT_EXPIRED')
        self.assertEqual(expired['trade_entry_checked_at'],later.isoformat())
        self.assertEqual(context['event'],event_before)

    def test_new_row_quote_cannot_refresh_original_context_or_book_clock(self):
        row, clock=evaluated_fast_signal()
        later=clock+timedelta(seconds=600)
        row['market_observed_at']=later.isoformat()
        row['_execution_quote']=dict(row['_execution_quote'],observed_at=later.isoformat())
        projected=project_readiness(row,now=later)
        self.assertEqual(projected['trade_entry_reason'],'EXECUTION_QUOTE_STALE')
        self.assertEqual(projected['trade_entry_valid_until'],(clock+timedelta(seconds=30)).isoformat())
        row=signal();row['orderbook_observed_at']=(NOW-timedelta(seconds=40)).isoformat()
        self.assertEqual(project_readiness(row,now=NOW)['trade_entry_reason'],'EXECUTION_ORDERBOOK_STALE')

    def test_malformed_or_future_saved_clocks_are_unknown_not_extended(self):
        for value in ('invalid',None,(NOW+timedelta(seconds=6)).isoformat()):
            row=signal();row['trade_plan']['final_economics_gate']['quote_time_gate']={
                'eligible':True,'observed_at':value,'max_age_seconds':30}
            projected=project_readiness(row,now=NOW)
            self.assertFalse(projected['trade_entry_eligible'])
            self.assertIsNone(projected['trade_entry_valid_until'])
        for value in ('invalid',None,(NOW+timedelta(seconds=1)).timestamp()):
            row=signal();row['trade_plan']['entry_timing_gate'].update(signal_at=value,max_age_seconds=600)
            projected=project_readiness(row,now=NOW)
            self.assertEqual(projected['trade_entry_reason'],'TRADE_ENTRY_EXPIRY_MISSING')
            self.assertFalse(projected['trade_entry_eligible'])
        self.assertFalse(project_readiness(signal(),now='invalid')['trade_entry_eligible'])
        missing_window=signal();missing_window['_breakout_runtime']='STRUCTURAL_QUOTE_RUNTIME_V1'
        self.assertEqual(project_readiness(missing_window,now=NOW)['trade_entry_reason'],
                         'TRADE_ENTRY_EXPIRY_MISSING')

    def test_legacy_saved_event_window_and_context_expire_without_reinterpreting_policy(self):
        row=signal();p=row['trade_plan'];signal_at=NOW-timedelta(seconds=600)
        p['timeframe_entry_context']={'timeframe':'5m','closed_at':(NOW-timedelta(seconds=5)).isoformat(),
            'event':{'event_id':'legacy','signal_at':signal_at.timestamp(),'confirmed_at':NOW.timestamp(),
                     'timeframe':'5m','policy':{'max_signal_age_bars':20.}}}
        p['entry_timing_gate'].update(event_id='legacy',signal_at=signal_at.timestamp(),max_age_seconds=599.)
        projected=project_readiness(row,now=NOW)
        self.assertEqual(projected['trade_entry_reason'],'SAME_TF_EVENT_EXPIRED')
        self.assertEqual(projected['trade_entry_valid_until'],(NOW-timedelta(seconds=1)).isoformat())
        # Prefer saved window to a different policy; never date it with confirmed_at.
        p['entry_timing_gate']['max_age_seconds']=6000.
        p['timeframe_entry_context']['closed_at']=(NOW-timedelta(seconds=301)).isoformat()
        self.assertEqual(project_readiness(row,now=NOW)['trade_entry_reason'],'SAME_TF_CONTEXT_STALE')
        p['entry_timing_gate']['event_id']='different'
        self.assertEqual(project_readiness(row,now=NOW)['trade_entry_reason'],'TRADE_ENTRY_EXPIRY_MISSING')


class PublicSignalExpiryTests(PublicSignalReadinessTests):
    @unittest.skipUnless(shutil.which('node'),'Node required for actual API-to-UI regression')
    def test_real_fast_delayed_full_and_ten_minute_api_ui_projection(self):
        import veritas_signal_publication as VSP
        row, clock=evaluated_fast_signal()
        audit={'lane':'STRUCTURAL_QUOTE_RUNTIME_V1','checked_at':clock.isoformat(),
               'result':{'status':'OK','portfolios':[{'name':'Impulse','asset':'ETH','horizon':'5m',
                   'event_id':row['trade_plan']['entry_event_id'],'status':'EXECUTED',
                   'checked_at':clock.isoformat(),'order_id':'recorded-order'}]}}
        row['_execution_audit']=audit
        delayed=dict(row,snapshot_stale=True);delayed.pop('_breakout_runtime')
        cold=[dict(asset=asset,horizon=tf,research_decision='NO_TRADE',snapshot_stale=True)
              for asset in self.assets for tf in self.horizons]
        rows=VSP.merge_rows(cold,[row]);key=('ETH','5m')
        fast=next(r for r in rows if (r['asset'],r['horizon'])==key)
        merged=VSP.merge_rows(rows,[delayed])
        self.assertIs(next(r for r in merged if (r['asset'],r['horizon'])==key),fast)
        self.assertFalse(fast['snapshot_stale'])
        self.ns['last_cycle'].update(summary=merged,at=clock.isoformat(),
                                    signals_updated_at=clock.isoformat())
        self.ns['latest_signal_summary_pg']=Mock(side_effect=AssertionError('unexpected DB access'))
        def snapshot(at):
            self.ns['now']=Mock(return_value=at.isoformat())
            result=self.ns['fresh_cycle_snapshot']()
            self.ns['now'].assert_called_once_with()
            self.assertEqual(result['summary_count'],49)
            fields={'trade_entry_eligible','trade_entry_reason','trade_entry_blockers',
                    'trade_entry_checked_at','trade_entry_basis','trade_entry_version',
                    'trade_entry_valid_until','trade_entry_expiry_reason'}
            for record in result['summary']:
                self.assertTrue(fields <= record.keys())
            return next(r for r in result['summary'] if (r['asset'],r['horizon'])==key)
        fresh=snapshot(clock)
        self.assertTrue(fresh['trade_entry_eligible'])
        old=snapshot(clock+timedelta(seconds=600))
        self.assertFalse(old['trade_entry_eligible'])
        self.assertEqual(old['trade_entry_reason'],'EXECUTION_QUOTE_STALE')
        self.assertEqual(old['trade_entry_checked_at'],clock.isoformat())
        self.assertFalse(old['snapshot_stale'])
        self.assertIs(old['trade_plan'],fast['trade_plan'])
        self.assertIs(old['_execution_audit'],fast['_execution_audit'])
        self.assertEqual(old['_execution_audit']['result']['portfolios'][0]['order_id'],'recorded-order')
        self.ns['latest_signal_summary_pg'].assert_not_called()
        # Both a newly read expired row and an already loaded READY row expire
        # through the production UI with a frozen browser clock and no request.
        javascript=r'''
const fs=require('node:fs'),vm=require('node:vm'),input=JSON.parse(fs.readFileSync(0,'utf8'));
const script=fs.readFileSync('veritas_v90_ui.py','utf8').split('<script>')[1].split('</script>')[0];
class Clock extends Date {constructor(...a){super(...(a.length?a:[input.now]));}static now(){return Date.parse(input.now);}}
const context=vm.createContext({Date:Clock,document:{readyState:'loading',addEventListener(){},querySelectorAll:()=>[],getElementById:()=>({})}});
vm.runInContext(script.replace(/\}\)\(\);\s*$/,'globalThis.ui={planStatus};})();'),context);
process.stdout.write(JSON.stringify(input.rows.map(r=>context.ui.planStatus(r))));
'''
        result=subprocess.run(['node','-e',javascript],input=json.dumps({'rows':[fresh,old],
            'now':(clock+timedelta(seconds=600)).isoformat()}),text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        for status in json.loads(result.stdout):
            self.assertFalse(status['ready'])
            self.assertEqual(status['short'],'цена')
            self.assertEqual(status['checked_at'],clock.isoformat())


if __name__=='__main__':
    unittest.main()
