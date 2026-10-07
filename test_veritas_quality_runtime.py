"""Frozen quality-report parity and isolation of the background history reader."""
import copy
import gc
import threading
import time
import unittest
import weakref
from collections import Counter
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import veritas_strategy_quality as Q
import veritas_quality_history as H
from veritas_maintenance import MaintenanceLane, MaintenanceDeferred
import test_veritas_strategy_quality_sql as FIXTURE


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026,10,7,19,tzinfo=timezone.utc)


def rows_fixture():
    factory=FIXTURE.EntryVersionTests().trade
    rows=[]
    for index in range(72):
        row=factory('fixture-'+str(index),net=(-30. if index%3==0 else 10.+index))
        row['portfolio_name']=Q.CTC.PORTFOLIO_ORDER[index%5]
        row['closed_at']=(FixedDatetime.now()-timedelta(minutes=index)).isoformat()
        if index%7==0:row['payload']['strategy_entry_sha']='b'*40
        if index%11==0:row['payload']['strategy_policy_hash']=None
        if index%13==0:row['payload']['strategy_epoch']='OLD'
        if index%17==0:row['payload'].pop('observation_path',None)
        rows.append(row)
    # Same event in the two current books exercises paired evidence reuse.
    paired=factory('paired')
    paired['portfolio_name']='Champion'
    challenger=copy.deepcopy(paired)
    challenger.update(trade_id='paired-challenger',portfolio_name='Challenger',net_pnl_rub=25.)
    rows.extend((paired,challenger))
    return rows


class QualityAnalysisReuseTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(Q.RELEASE,'deployment_sha',return_value='a'*40))
        self.enterContext(patch.object(Q,'datetime',FixedDatetime))

    def test_full_report_equals_frozen_reducers_with_one_review_per_trade(self):
        rows=rows_fixture();before=copy.deepcopy(rows)
        namespace=dict(vars(Q));exec(LEGACY_REDUCERS,namespace)
        original_review=Q.review
        namespace['review']=Mock(wraps=original_review)
        namespace['idea_key']=Mock(wraps=Q.idea_key)
        with patch.object(Q,'digest',wraps=Q.digest) as old_digest:
            legacy=namespace['build_report'](rows)
        with patch.object(Q,'review',wraps=original_review) as reviewer, \
                patch.object(Q,'digest',wraps=Q.digest) as new_digest:
            actual=Q.build_report(rows)
        self.assertEqual(actual,legacy)
        self.assertEqual(reviewer.call_count,len(rows))
        self.assertGreater(namespace['review'].call_count,len(rows)*5)
        self.assertLess(new_digest.call_count,old_digest.call_count)
        self.assertEqual(Counter(id(c.args[0]) for c in reviewer.call_args_list),
                         Counter(id(row) for row in rows))
        self.assertEqual(rows,before)
        self.assertEqual(actual['champion_challenger']['matched_events'],1)

    def test_memo_does_not_survive_mutated_evidence_or_a_new_version(self):
        rows=rows_fixture()
        first=Q.build_report(rows)
        for row in rows:
            row['payload'].pop('observation_path',None)
        with patch.object(Q.RELEASE,'deployment_sha',return_value='c'*40), \
                patch.object(Q,'review',wraps=Q.review) as reviewer:
            second=Q.build_report(rows)
        self.assertEqual(reviewer.call_count,len(rows))
        self.assertNotEqual(first,second)
        self.assertTrue(all(p['cohorts']['current']['all']['closed_trades']==0 for p in second['portfolios']))
        self.assertTrue(all(p['cohorts']['all']['all']['learning_evidence']['trades']==0 for p in second['portfolios']))

    def test_dirty_collector_preserves_sql_order_and_archived_portfolios(self):
        rows=rows_fixture()
        for index,row in enumerate(rows):
            if index%4==0:row['portfolio_name']='ARCHIVED_BOOK'
            if index%3==0:row['payload']['posttrade_review']={'input_hash':Q.review(row)['input_hash']}
        expected=[row['trade_id'] for row in rows
                  if (row['payload'].get('posttrade_review') or {}).get('input_hash')!=Q.review(row)['input_hash']][:10]
        with patch.object(Q,'review',wraps=Q.review) as reviewer:
            collector=H.ReviewCollector(rows,reviewer,Q.payload)
            Q.build_report(rows,review_sink=collector.accept)
            pending=collector.pending(rows)
        self.assertEqual([row['trade_id'] for row,_ in pending],expected)
        self.assertEqual(reviewer.call_count,len(rows))
        self.assertEqual(len(collector._dirty),10)
        self.assertTrue(any(row['portfolio_name']=='ARCHIVED_BOOK' for row,_ in pending))

    def test_unchanged_reviews_need_one_review_per_row_and_no_dirty_copy(self):
        rows=rows_fixture()
        for row in rows:row['payload']['posttrade_review']={'input_hash':Q.review(row)['input_hash']}
        with patch.object(Q,'review',wraps=Q.review) as reviewer:
            collector=H.ReviewCollector(rows,reviewer,Q.payload)
            Q.build_report(rows,review_sink=collector.accept)
            self.assertEqual(collector.pending(rows),[])
        self.assertEqual(reviewer.call_count,len(rows))
        self.assertEqual(collector._dirty,[])


class QualityResourceGuardTests(unittest.TestCase):
    def setUp(self):
        self.previous={'status':'OK','version':Q.VERSION,'portfolios':[{'name':'Champion'}]}
        self.stamp=time.monotonic()-25.
        self.enterContext(patch.dict(Q._CACHE,{'value':self.previous,'at':self.stamp,
            'worker':None,'last_error':'QueryCanceled'},clear=True))
        self.events=[]

    def emit(self,event,**values):
        self.events.append((event,values))

    def run_once(self,refresh,guard):
        H.guarded_refresh(Q._CACHE,Q._LOCK,refresh,self.emit,guard)

    def test_memory_and_unknown_rss_defer_before_sql_and_recover_without_fake_age(self):
        for bad_rss,reason in ((400.,'DEFERRED_MEMORY'),(None,'DEFERRED_MEMORY_UNKNOWN')):
            with self.subTest(reason=reason):
                rss=[bad_rss]
                lane=MaintenanceLane({'rss_mb':lambda:rss[0],'emit':lambda *a,**k:None})
                refresh=Mock(side_effect=AssertionError('SQL ran before resource admission'))
                self.run_once(refresh,lane.permit)
                refresh.assert_not_called()
                view=Q.snapshot()
                self.assertEqual(view['refresh_status'],reason)
                self.assertEqual(view['refresh']['reason'],reason)
                self.assertEqual(Q._CACHE['at'],self.stamp)
                self.assertIs(Q._CACHE['value'],self.previous)
                self.assertEqual(view['last_refresh_error_code'],'QueryCanceled')
                self.assertGreaterEqual(view['snapshot_age_seconds'],25.)
                if bad_rss is not None:self.assertEqual(view['refresh']['rss_mb'],bad_rss)
                rss[0]=200.
                def complete():
                    Q._CACHE.update(value=self.previous,at=time.monotonic(),last_error=None)
                    return self.previous
                self.run_once(complete,lane.permit)
                self.assertEqual(Q.snapshot()['refresh_status'],'OK')
                self.assertIsNone(Q._CACHE['last_error'])
                self.assertGreater(Q._CACHE['at'],self.stamp)
                Q._CACHE.update(at=self.stamp,last_error='QueryCanceled')

    def test_busy_history_permit_does_not_wait_or_touch_sql(self):
        lane=MaintenanceLane({'rss_mb':lambda:200.,'emit':lambda *a,**k:None})
        entered=threading.Event();release=threading.Event()
        def hold():
            with lane.permit('snapshot'):
                entered.set();release.wait(2.)
        worker=threading.Thread(target=hold);worker.start()
        self.assertTrue(entered.wait(1.))
        try:
            refresh=Mock(side_effect=AssertionError('SQL ran while history permit busy'))
            self.run_once(refresh,lane.permit)
            refresh.assert_not_called()
            self.assertEqual(Q.snapshot()['refresh_status'],'DEFERRED_BUSY')
            self.assertEqual(Q._CACHE['at'],self.stamp)
            self.assertEqual(Q._CACHE['last_error'],'QueryCanceled')
        finally:
            release.set();worker.join(1.)

    def test_quality_holds_the_shared_permit_for_its_entire_refresh(self):
        lane=MaintenanceLane({'rss_mb':lambda:200.,'emit':lambda *a,**k:None})
        entered=threading.Event();release=threading.Event();observed=[]
        def refreshing():
            entered.set();release.wait(2.)
            return self.previous
        worker=threading.Thread(target=lambda:self.run_once(refreshing,lane.permit))
        worker.start();self.assertTrue(entered.wait(1.))
        try:
            try:
                with lane.permit('snapshot'):
                    observed.append('overlapped')
            except MaintenanceDeferred as error:
                observed.append(error.status)
            self.assertEqual(observed,['DEFERRED_BUSY'])
        finally:
            release.set();worker.join(1.)
        with lane.permit('snapshot'):
            observed.append('released')
        self.assertEqual(observed,['DEFERRED_BUSY','released'])

    def test_logging_failure_keeps_completed_refresh_and_future_attempts_alive(self):
        events=[]
        def broken_emit(event,**values):
            events.append((event,values))
            raise RuntimeError('logger offline')
        connection=Mock()
        connection.__enter__=Mock(return_value=connection)
        connection.__exit__=Mock(return_value=False)
        connection.transaction.side_effect=lambda:nullcontext()
        connection.execute.return_value.fetchall.return_value=[]
        H.guarded_refresh(Q._CACHE,Q._LOCK,lambda:Q.refresh(lambda:connection,broken_emit),broken_emit)
        self.assertEqual([event for event,_ in events],['strategy_quality_review'])
        self.assertEqual(Q.snapshot()['refresh_status'],'OK')
        self.assertIsNone(Q._CACHE['last_error'])
        completed=Q._CACHE['value'];stamp=Q._CACHE['at']
        def fail():raise ValueError('read failed')
        H.guarded_refresh(Q._CACHE,Q._LOCK,fail,broken_emit)
        self.assertEqual(Q.snapshot()['refresh_status'],'ERROR')
        self.assertIs(Q._CACHE['value'],completed)
        self.assertEqual(Q._CACHE['at'],stamp)
        lane=MaintenanceLane({'rss_mb':lambda:None,'emit':lambda *a,**k:None})
        H.guarded_refresh(Q._CACHE,Q._LOCK,Mock(),broken_emit,lane.permit)
        self.assertEqual(Q.snapshot()['refresh_status'],'DEFERRED_MEMORY_UNKNOWN')
        self.assertEqual(Q._CACHE['last_error'],'ValueError')
        self.assertEqual([event for event,_ in events],['strategy_quality_review',
            'strategy_quality_review_error','strategy_quality_review_deferred'])

    def test_failure_traceback_releases_temporary_rows_before_permit_exit(self):
        class LargeRows:pass
        references=[];observed=[]
        @contextmanager
        def guard(owner):
            self.assertEqual(owner,'strategy_quality_review')
            try:yield
            finally:
                gc.collect()
                observed.append(references[0]() is None)
        def failing():
            rows=LargeRows();references.append(weakref.ref(rows))
            raise RuntimeError('read failed')
        self.run_once(failing,guard)
        self.assertEqual(observed,[True])
        self.assertEqual(Q.snapshot()['refresh_status'],'ERROR')
        self.assertEqual(Q._CACHE['last_error'],'RuntimeError')
        self.assertEqual(Q._CACHE['at'],self.stamp)
        self.assertIs(Q._CACHE['value'],self.previous)


# Frozen statistics/build_report from main 888e596, independent of the new memo.
LEGACY_REDUCERS = r'''
def statistics(trades,window=None):
    grouped=defaultdict(list)
    for trade in trades:
        grouped[idea_key(trade)[0]].append(trade)
    ids=sorted(grouped,key=lambda key:max(at(t.get('closed_at')) or datetime.min.replace(tzinfo=timezone.utc)
                                          for t in grouped[key]))
    if window:
        ids=ids[-window:]
    rows=[t for key in ids for t in grouped[key]]
    values=[num(t.get('net_pnl_rub')) for t in rows]
    known=[x for x in values if x is not None]
    pnl=sum(known); pos=[x for x in known if x>0]; neg=[x for x in known if x<0]
    idea_values=[sum(num(t.get('net_pnl_rub')) or 0 for t in grouped[key]) for key in ids
                 if all(num(t.get('net_pnl_rub')) is not None for t in grouped[key])]
    verified=sum(all(idea_key(t)[1] for t in grouped[key]) for key in ids)
    reviewed=[review(t) for t in rows]
    captures=[r['capture_ratio'] for r in reviewed if r['capture_ratio'] is not None]
    gross_values=[num(t.get('gross_pnl_rub')) for t in rows]
    fees_values=[num(t.get('fees_rub')) for t in rows]
    funding_values=[num(t.get('funding_rub')) for t in rows]
    losses=-sum(neg); gains=sum(pos); n=len(known)
    # Financial totals above retain every ledger result. Learning uses complete
    # event groups only; one unknown path cannot become an apparent win/loss label.
    path_status={id(t):r['evidence_status'] for t,r in zip(rows,reviewed)}
    learning_groups=[
        group for key,group in ((key,grouped[key]) for key in ids)
        if all(idea_key(t)[1] and path_status[id(t)]=='OBSERVED_PAPER_PATH'
               for t in group)
    ]
    learning_values=[sum(num(t.get('net_pnl_rub')) for t in group) for group in learning_groups]
    learning_trades=sum(len(group) for group in learning_groups)
    learning_gains=sum(value for value in learning_values if value>0)
    learning_losses=-sum(value for value in learning_values if value<0)
    learning_evidence={
        'requirements':'EXPLICIT_EVENT_AND_OBSERVED_SOURCE_MATCHED_COVERED_PATH',
        'event_groups':len(learning_groups),'trades':learning_trades,
        'excluded_event_groups':len(ids)-len(learning_groups),
        'excluded_trades':len(rows)-learning_trades,
        'net_pnl_rub':sum(learning_values) if learning_values else None,
        'expectancy_rub_per_event':sum(learning_values)/len(learning_values) if learning_values else None,
        'win_rate_per_event':sum(value>0 for value in learning_values)/len(learning_values) if learning_values else None,
        'profit_factor_per_event':learning_gains/learning_losses if learning_losses>0 else None,
        'independence_established':False,'automatic_promotion':False,
    }
    return {'closed_trades':len(rows),'known_results':n,'idea_count':len(ids),'verified_event_count':verified,
            'independence_status':'VERIFIED_EVENT_CLUSTERS' if ids and verified==len(ids) else 'INCLUDES_ESTIMATED_CLUSTERS',
            'wins':len(pos),'win_rate':len(pos)/n if n else None,
            'idea_win_rate':sum(v>0 for v in idea_values)/len(idea_values) if idea_values else None,
            'idea_win_rate_interval_95':_wilson(sum(v>0 for v in idea_values),len(idea_values)) if verified==len(ids) else None,
            'net_pnl_rub':pnl if n==len(rows) else None,'profit_factor':gains/losses if losses>0 else None,
            'profit_factor_state':'NO_LOSSES' if gains>0 and losses==0 else 'NO_TRADES' if not n else 'DEFINED',
            'avg_win_rub':gains/len(pos) if pos else None,'avg_loss_rub':losses/len(neg) if neg else None,
            'payoff_ratio':(gains/len(pos))/(losses/len(neg)) if pos and neg else None,
            'expectancy_rub':pnl/n if n and n==len(rows) else None,
            'capture_ratio_mean':sum(captures)/len(captures) if captures else None,'capture_sample':len(captures),
            'cost_erased_winners':sum(r['component']=='COSTS' for r in reviewed),
            'low_capture_count':sum(r['component']=='EXIT' for r in reviewed),
            'gross_pnl_rub':sum(gross_values) if all(v is not None for v in gross_values) else None,
            'fees_rub':sum(fees_values) if all(v is not None for v in fees_values) else None,
            'funding_rub':sum(funding_values) if all(v is not None for v in funding_values) else None,
            'learning_evidence':learning_evidence,
            'readiness':'NOT_PROVEN' if len(learning_groups)<50 else 'REQUIRES_OUT_OF_SAMPLE_VALIDATION',
            'window_requested':window,'results_are_paper_only':True}

def build_report(trades,positions=()):
    baseline=at(BASELINE_AT); now=datetime.now(timezone.utc).isoformat()
    current=version_identity()
    result={'status':'OK','version':VERSION,'at':now,'strategy_epoch':CTC.STRATEGY_EPOCH,
            'current_sha':RELEASE.deployment_sha(),'baseline_sha':BASELINE_SHA,'baseline_at':BASELINE_AT,
            'current_entry_version':current,'policy_hash_version':POLICY_HASH_VERSION,
            'current_cohort_assignment':'EXACT_IMMUTABLE_ENTRY_SHA_POLICY_AND_EPOCH',
            'baseline_assignment':'OPENED_AT_AFTER_VERIFIED_DEPLOY_WITHOUT_RELABELING_OLD_TRADES',
            'automatic_parameter_promotion':False,'portfolios':[],
            'all_portfolio_independent_ideas':len({idea_key(t)[0] for t in trades}),
            'readiness':'NOT_PROVEN','real_orders_enabled':False}
    for name in CTC.PORTFOLIO_ORDER:
        rows=[t for t in trades if t.get('portfolio_name')==name and t.get('status')=='CLOSED']
        cohorts={'all':rows,
                 'since_73266d9':[t for t in rows if at(t.get('opened_at')) and at(t['opened_at'])>=baseline],
                 'current':[t for t in rows if matches_current_version(t,current)],
                 'current_epoch':[t for t in rows if payload(t.get('payload')).get('strategy_epoch')==CTC.STRATEGY_EPOCH]}
        version_groups,version_count,omitted=partition_versions(rows,current,MAX_VERSION_GROUPS)
        for group in version_groups:
            group['metrics']=statistics(group.pop('trades'))
        opened=[z for z in positions if z.get('portfolio_name')==name]
        result['portfolios'].append({'name':name,
            'cohorts':{key:{'all':statistics(group),'last20':statistics(group,20),'last50':statistics(group,50)}
                       for key,group in cohorts.items()},
            'cohort_definitions':{'current':'EXACT_ENTRY_SHA_POLICY_AND_EPOCH',
                                  'current_epoch':'EPOCH_ONLY_INCLUDES_OTHER_CODE_AND_POLICY_VERSIONS',
                                  'since_73266d9':'ENTRY_TIME_ONLY_NOT_A_VERSION_PROOF','all':'LEDGER_HISTORY'},
            'version_groups':version_groups,'version_group_count':version_count,
            'version_groups_truncated':version_count>MAX_VERSION_GROUPS,
            'version_groups_omitted_trades':omitted,
            'missing_version_closed_trades':sum(not version_identity(t)['complete'] for t in rows),
            'prior_version_closed_trades':sum(version_identity(t)['complete'] and
                                             not matches_current_version(t,current) for t in rows),
            'open_positions':len(opened),
            'inherited_open_positions':sum(not matches_current_version(z,current) for z in opened),
            'recent_reviews':[review(t) for t in sorted(rows,key=lambda t:str(t.get('closed_at')))[-5:][::-1]]})
    # Pair by explicit shared market event only. No claim that two different
    # market windows form a randomized A/B experiment.
    pairs=defaultdict(dict)
    for t in trades:
        p=payload(t.get('payload')); key,verified=idea_key(t)
        if (t.get('portfolio_name') in ('Champion','Challenger') and verified
                and matches_current_version(t,current) and t.get('status')=='CLOSED'):
            pairs[key][t['portfolio_name']]=t
    paired=[]
    for key,values in pairs.items():
        if set(values)=={'Champion','Challenger'}:
            reviews={k:review(v) for k,v in values.items()}
            returns={k:r['net_return_on_entry_notional_pct'] for k,r in reviews.items()}
            if (all(r['evidence_status']=='OBSERVED_PAPER_PATH' for r in reviews.values())
                    and all(v is not None for v in returns.values())):
                paired.append(returns['Challenger']-returns['Champion'])
    result['champion_challenger']={'matched_events':len(paired),
        'mean_return_delta_pp':sum(paired)/len(paired) if paired else None,
        'automatic_promotion':False,'causal_evidence':False}
    return result
'''

if __name__=='__main__':unittest.main()
