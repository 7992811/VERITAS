"""Quote evidence never invents a prefix, cadence, contract or successful write."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
import unittest

import veritas_observation_path as PATH
import veritas_price_source as SOURCE


OPEN=datetime(2026,10,7,9,0,tzinfo=timezone.utc)
FEED={'primary_source':'TEST_NATIVE','contract_id':'ETH-EXACT','source_gate_pass':True}


def stamp(seconds):return (OPEN+timedelta(seconds=seconds)).isoformat()


def trade(short=False,asset='ETH'):
    return {'trade_id':'test-trade','active_trade_id':'test-trade','asset':asset,
        'direction':'SHORT' if short else 'LONG','horizon':'5m','opened_at':stamp(0),
        'status':'OPEN','avg_entry_price':100.,'units':-1. if short else 1.,
        'payload':{'price_source_lock':SOURCE.identity(asset,FEED),
                   'initial_stop_price':102. if short else 98.,'entry_atr':1.,
                   'entry_execution_model':{'asset':'ETH','side':'SELL_SHORT' if short else 'BUY',
                                            'fill_price':100.}}}


def quote(seconds,price=100.,**changes):
    result=dict(FEED,price=price,observed_at=stamp(seconds));result.update(changes)
    return result


def add(row,seconds,price=100.,*,observed=None,at_entry=False,**changes):
    q=quote(seconds if observed is None else observed,price,**changes)
    row['payload']['observation_path']=PATH.observe(row,q,stamp(seconds),at_entry=at_entry)
    return row['payload']['observation_path']


def complete(short=False):
    row=trade(short)
    add(row,0,at_entry=True);add(row,15,98. if short else 102.);add(row,30,101. if short else 99.)
    row.update(status='CLOSED',closed_at=stamp(30))
    return row


class TransactionCursor:
    def __init__(self,fail=False):
        self.writes=[];self.fail=fail;self.aborted=False;self.rollbacks=0

    @contextmanager
    def transaction(self):
        before=list(self.writes)
        try:yield self
        except Exception:
            self.writes=before;self.aborted=False;self.rollbacks+=1
            raise

    def execute(self,sql,params=()):
        if self.aborted:raise RuntimeError('transaction is aborted')
        if self.fail and 'UPDATE paper_trades' in sql:
            self.aborted=True;raise RuntimeError('metadata storage unavailable')
        self.writes.append((sql,params));return self


class ObservationPathTests(unittest.TestCase):
    def test_sampled_source_locked_path_is_explicitly_not_every_market_tick(self):
        row=complete();before=deepcopy(row)
        result=PATH.assessment(row)
        self.assertTrue(result['eligible'],result)
        self.assertEqual(result['observation_count'],3)
        self.assertEqual(result['max_gap_seconds'],15)
        self.assertTrue(result['not_continuous_market_path'])
        self.assertEqual(row,before)

    def test_short_excursions_use_original_price_linear_basis(self):
        row=complete(short=True);w=row['payload']['observation_path']
        self.assertAlmostEqual(w['mfe_pct'],2.)
        self.assertAlmostEqual(w['mae_pct'],-1.)
        self.assertTrue(PATH.assessment(row)['eligible'])

    def test_add_and_trailing_stop_do_not_replace_original_entry_geometry(self):
        row=trade();add(row,0,at_entry=True);add(row,15,101.)
        row['avg_entry_price']=110.;row['stop_price']=109.
        row['payload'].update(last_entry_execution_model={'fill_price':120.},stop_price=109.)
        w=add(row,30,120.)
        row.update(status='CLOSED',closed_at=stamp(30))
        self.assertTrue(PATH.assessment(row)['eligible'])
        self.assertEqual(w['original_entry_price'],100.)
        self.assertEqual(w['initial_stop_price'],98.)
        self.assertEqual(w['entry_atr'],1.)
        self.assertAlmostEqual(w['mfe_pct'],20.)

    def test_changed_immutable_first_fill_cannot_rebase_a_recorded_path(self):
        row=complete();row['payload']['entry_execution_model']['fill_price']=110.
        self.assertEqual(PATH.assessment(row)['reason'],'OBSERVATION_PATH_ENTRY_MISMATCH')

    def test_carried_position_cannot_gain_a_missing_entry_prefix_after_restart(self):
        row=trade();add(row,60);add(row,75,101.,at_entry=True);add(row,90,102.)
        row.update(status='CLOSED',closed_at=stamp(90))
        self.assertFalse(row['payload']['observation_path']['started_at_entry'])
        self.assertEqual(PATH.assessment(row)['reason'],'UNOBSERVED_ENTRY_PREFIX')

    def test_late_at_entry_flag_does_not_backfill_history(self):
        row=trade();add(row,60,at_entry=True);add(row,75,101.)
        row.update(status='CLOSED',closed_at=stamp(75))
        self.assertEqual(PATH.assessment(row)['reason'],'UNOBSERVED_ENTRY_PREFIX')

    def test_observation_before_actual_entry_does_not_certify_the_entry_prefix(self):
        row=trade();add(row,-1,at_entry=True);add(row,15,101.)
        row.update(status='CLOSED',closed_at=stamp(15))
        self.assertFalse(row['payload']['observation_path']['started_at_entry'])
        self.assertEqual(PATH.assessment(row)['reason'],'UNOBSERVED_ENTRY_PREFIX')

    def test_source_contract_asset_and_timeframe_mismatches_are_not_counted(self):
        for changes in ({'primary_source':'OTHER'},{'contract_id':'DIFFERENT_CONTRACT'}):
            row=trade();add(row,0,at_entry=True)
            w=add(row,15,1000.,**changes)
            self.assertEqual(w['observation_count'],1)
            self.assertEqual(w['max_price'],100.)
            self.assertEqual(w['invalid_observation_count'],1)
            self.assertFalse(PATH.assessment(row)['eligible'])
        row=complete();row['horizon']='1h'
        self.assertEqual(PATH.assessment(row)['reason'],'OBSERVATION_PATH_CONTEXT_MISMATCH')
        row=complete();row['payload']['price_source_lock']['asset']='BTC'
        self.assertEqual(PATH.assessment(row)['reason'],'OBSERVATION_PATH_CONTEXT_MISMATCH')

    def test_internal_source_gap_remains_excluded_after_later_regular_quotes(self):
        row=trade();add(row,0,at_entry=True)
        for checked in (15,30,45):
            add(row,checked,100.,observed=0)
        add(row,60,101.,observed=60)
        row.update(status='CLOSED',closed_at=stamp(60))
        result=PATH.assessment(row)
        self.assertEqual(result['reason'],'OBSERVATION_SOURCE_GAP')
        self.assertEqual(result['max_gap_seconds'],60)
        self.assertEqual(result['max_check_gap_seconds'],15)

    def test_slow_processing_is_separate_from_provider_cadence(self):
        row=trade();add(row,0,at_entry=True);add(row,15,101.)
        for checked in (30,45,60,75,90):
            add(row,checked,101.,observed=15)
        row.update(status='CLOSED',closed_at=stamp(90))
        result=PATH.assessment(row)
        self.assertEqual(result['max_gap_seconds'],15)
        self.assertEqual(result['max_check_gap_seconds'],15)
        self.assertEqual(result['reason'],'OBSERVATION_PROCESSING_DELAY')

    def test_minute_provider_cadence_is_valid_when_protective_checks_are_continuous(self):
        row=trade(asset='NQ')
        add(row,0,at_entry=True)
        for checked in (15,30,45):
            add(row,checked,100.,observed=0)
        add(row,60,101.,observed=60)
        add(row,75,101.,observed=60)
        row.update(status='CLOSED',closed_at=stamp(75))
        result=PATH.assessment(row)
        self.assertTrue(result['eligible'],result)
        self.assertEqual(result['max_gap_seconds'],60)
        self.assertEqual(result['max_check_gap_seconds'],15)
        self.assertEqual(result['source_max_age_seconds'],120)
        self.assertEqual(result['source_gap_allowance_seconds'],135)

    def test_missing_protective_checks_still_invalidate_slow_source_path(self):
        row=trade(asset='NQ');add(row,0,at_entry=True);add(row,60,101.,observed=60)
        row.update(status='CLOSED',closed_at=stamp(60))
        result=PATH.assessment(row)
        self.assertEqual(result['reason'],'OBSERVATION_CHECK_GAP')
        self.assertEqual(result['max_check_gap_seconds'],60)

    def test_repeated_provider_timestamp_is_not_a_new_observation(self):
        row=trade();add(row,0,at_entry=True);add(row,15,101.)
        w=add(row,30,101.,observed=15)
        row.update(status='CLOSED',closed_at=stamp(30))
        self.assertEqual(w['observation_count'],2)
        self.assertEqual(w['duplicate_observation_count'],1)
        self.assertEqual(PATH.assessment(row)['tail_gap_seconds'],15)

    def test_revised_price_at_same_timestamp_and_out_of_order_quote_stay_invalid(self):
        for observed,price in ((15,500.),(10,500.)):
            row=trade();add(row,0,at_entry=True);add(row,15,101.)
            w=add(row,30,price,observed=observed)
            add(row,45,100.)
            row.update(status='CLOSED',closed_at=stamp(45))
            self.assertEqual(w['observation_count'],2)
            self.assertEqual(w['max_price'],101.)
            self.assertEqual(PATH.assessment(row)['reason'],'INVALID_PATH_OBSERVATION')

    def test_reversed_processing_clock_is_not_a_fresh_final_check(self):
        row=trade();add(row,0,at_entry=True);add(row,30,101.,observed=15)
        before=row['payload']['observation_path']['last_checked_at']
        w=add(row,20,101.,observed=15)
        self.assertEqual(w['last_checked_at'],before)
        self.assertGreater(w['invalid_observation_count'],0)
        self.assertFalse(PATH.assessment(row)['eligible'])

    def test_unobserved_exit_tail_and_post_exit_quote_cannot_certify_close(self):
        row=complete();row['closed_at']=stamp(90)
        self.assertEqual(PATH.assessment(row)['reason'],'UNOBSERVED_EXIT_CHECK_TAIL')
        row=complete();add(row,45,103.)
        self.assertEqual(PATH.assessment(row)['reason'],'POST_EXIT_PATH_OBSERVATION')

    def test_final_source_quote_can_close_a_regular_observed_tail(self):
        row=trade();add(row,0,at_entry=True);add(row,15,101.)
        row.update(status='CLOSED',closed_at=stamp(45))
        add(row,45,99.)
        self.assertTrue(PATH.assessment(row)['eligible'])
        self.assertEqual(PATH.assessment(row)['tail_gap_seconds'],0.)

    def test_future_naive_or_rejected_quotes_never_become_observations(self):
        for q in (quote(30),quote(0,source_gate_pass=False),quote(0,source_gate_pass='false'),
                  quote(0,observed_at='2026-10-07T09:00:00'),quote(0,price=float('nan'))):
            row=trade();w=PATH.observe(row,q,stamp(0),at_entry=True)
            row['payload']['observation_path']=w
            self.assertEqual(w['observation_count'],0)
            self.assertFalse(PATH.assessment(row)['eligible'])

    def test_impossible_check_count_is_rejected(self):
        row=complete()
        row['payload']['observation_path']['check_count']=1
        self.assertEqual(PATH.assessment(row)['reason'],'INCONSISTENT_CHECK_COVERAGE')

    def test_cadence_metadata_tampering_is_rejected(self):
        row=complete()
        row['payload']['observation_path']['source_max_age_seconds']=999
        self.assertEqual(PATH.assessment(row)['reason'],'OBSERVATION_PATH_CADENCE_MISMATCH')

    def test_impossible_coverage_counts_and_ranges_are_rejected(self):
        for changes in ({'observation_count':2,'max_gap_seconds':1},
                        {'observation_count':2.5}, {'min_price':200.}, {'last_price':500.}):
            row=complete();row['payload']['observation_path'].update(changes)
            self.assertFalse(PATH.assessment(row)['eligible'])

    def test_malformed_legacy_witness_never_raises_or_becomes_usable(self):
        changes=({'observation_count':'broken'},{'gap_count':[1]},
                 {'invalid_observation_count':{'n':0}},{'max_gap_seconds':'bad'},
                 {'source_identity':'NOT_AN_IDENTITY'},{'max_price':float('nan')})
        for values in changes:
            with self.subTest(values=values):
                row=complete();row.pop('closed_at');row['status']='OPEN'
                row['payload']['observation_path'].update(values)
                add(row,45,100.)
                row.update(status='CLOSED',closed_at=stamp(45))
                result=PATH.assessment(row)
                self.assertFalse(result['eligible'],result)
                json.dumps(row['payload']['observation_path'],allow_nan=False)

    def test_malformed_external_shapes_are_safe_for_noncritical_observation(self):
        for row in (None,[],{'payload':'[]'},{'payload':'not JSON'}):
            for q in (None,[],{'source_names':['bad']}):
                witness=PATH.observe(row,q,stamp(0))
                self.assertFalse(PATH.assessment({'payload':{'observation_path':witness}})['eligible'])
                json.dumps(witness,allow_nan=False)

    def test_witness_size_does_not_retain_arbitrary_embedded_diagnostics(self):
        row=complete();w=row['payload']['observation_path']
        w['source_identity']['debug_history']=[{'x':i} for i in range(10000)]
        w['ignored_history']=[1]*10000
        before=deepcopy(row)
        actual=PATH.observe(row,quote(45),stamp(45))
        self.assertLess(len(json.dumps(actual)),3000)
        self.assertNotIn('debug_history',actual['source_identity'])
        self.assertNotIn('ignored_history',actual)
        self.assertEqual(row,before)

    def test_accounting_copy_keeps_failure_marker_and_cannot_create_missing_evidence(self):
        self.assertIsNone(PATH.bounded_witness(trade()))
        row=complete();row['payload']['observation_path']['invalid_observation_count']=1
        before=deepcopy(row)
        copy=PATH.bounded_witness(row)
        self.assertEqual(copy['invalid_observation_count'],1)
        row['payload']['observation_path']=copy
        self.assertFalse(PATH.assessment(row)['eligible'])
        self.assertEqual(row,before)

    def test_extreme_finite_prices_do_not_serialize_infinite_derived_excursions(self):
        row=trade();row['payload']['entry_execution_model']['fill_price']=1e-320
        add(row,0,1e-320,at_entry=True)
        w=add(row,15,1e308)
        row.update(status='CLOSED',closed_at=stamp(15))
        self.assertIsNone(w['mfe_pct'])
        self.assertIsNone(w['mae_pct'])
        self.assertFalse(PATH.assessment(row)['eligible'])
        json.dumps(w,allow_nan=False)
        c=TransactionCursor()
        result=PATH.record(c,row,quote(15,1e308),stamp(15))
        json.dumps(result,allow_nan=False)

    def test_metadata_savepoint_failure_does_not_abort_protective_accounting(self):
        row=trade();add(row,0,at_entry=True);add(row,15,101.)
        row.update(status='CLOSED',closed_at=stamp(30))
        before=deepcopy(row);c=TransactionCursor(fail=True)
        result=PATH.record(c,row,quote(30,99.),stamp(30),lane='CANONICAL_EXIT')
        self.assertEqual(c.rollbacks,1)
        self.assertEqual(c.writes,[])
        self.assertFalse(c.aborted)
        self.assertEqual(PATH.assessment(result)['reason'],'INVALID_PATH_OBSERVATION')
        c.execute('PROTECTIVE_ACCOUNTING_EXIT')
        self.assertEqual(c.writes[0][0],'PROTECTIVE_ACCOUNTING_EXIT')
        self.assertEqual(row,before)

    def test_record_updates_only_payload_and_supports_minimal_cursors(self):
        class Cursor:
            def __init__(self):self.writes=[]
            def execute(self,sql,params):self.writes.append((sql,params))
        c=Cursor();row=trade();add(row,0,at_entry=True);add(row,15,101.)
        row.update(status='CLOSED',closed_at=stamp(30))
        result=PATH.record(c,row,quote(30,99.),stamp(30))
        self.assertTrue(PATH.assessment(result)['eligible'])
        self.assertEqual(len(c.writes),2)
        for sql,params in c.writes:
            self.assertIn('SET payload=',sql)
            self.assertEqual(set(json.loads(params[0])),{'observation_path'})
        self.assertEqual(result['avg_entry_price'],row['avg_entry_price'])
        self.assertEqual(result['units'],row['units'])
        empty=Cursor();PATH.record(empty,{},None,stamp(0))
        self.assertEqual(empty.writes,[])


if __name__=='__main__':unittest.main()
