"""One observed oil provider feed owns quotes, history and held valuation."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from unittest import TestCase, skipUnless
from unittest.mock import patch

import veritas_price_source as S
import veritas_profinance as PF
import veritas_profinance_history as H
import veritas_position_guard as G
import veritas_execution as E


NOW=datetime(2026,10,7,17,25,30,tzinfo=timezone.utc)
OBSERVED='1;I=27;S=Brent oil;TICK=brent;LP=+102.08;T=20:25:30'
REFRESH=('1;publicSession\ns;Brent oil\nt;brent\nn;Last;2\n'
         '6;1Min;1;bar\n6;5Min;3;bar\n6;1Hour;6;bar\n'
         '6;4Hour;8;bar\n6;1Day;9;bar\n')
HISTORY=';Open;High;Low;Close;Время\n;102;102.1;101.9;102.08;07.10.2026 20:24\n'


def quote(**changes):
    return {**PF.parse_quotes(OBSERVED,NOW)['BRENT'],
            'source_gate_pass':True,'market_open':True,**changes}


def held(pinned=False):
    identity=(S.brent_feed_pin_identity() if pinned else
              S.identity('BRENT',{'source':'ProFinance','raw_label':'Brent oil'}))
    return {'asset':'BRENT','direction':'LONG','avg_entry_price':101.18,
            'last_price':102.,'stop_price':100.,
            'payload':{'price_source_lock':identity,
                       'source_locked_mark':{'identity':deepcopy(identity),'price':102.}}}


class BrentObservedPinTests(TestCase):
    def test_exact_observed_tuple_verifies_feed_without_inventing_expiry(self):
        q=quote()
        identity=S.identity('BRENT',q)
        self.assertEqual(identity,S.brent_feed_pin_identity())
        self.assertTrue(q['provider_series_verified'])
        self.assertTrue(S.brent_quote_verified(q))
        self.assertEqual(q['price_field'],'LP')
        self.assertIsNone(identity['contract_id'])
        self.assertFalse(q['exact_contract_verified'])
        self.assertEqual(q['price_series_type'],'UNVERIFIED')

    def test_missing_or_conflicting_provider_fields_never_become_current_oil(self):
        for field,value in (('I','99'),('I',''),('TICK','WTI'),('TICK',''),
                            ('S','WTI'),('S','')):
            parts=[part for part in OBSERVED.split(';') if not part.startswith(field+'=')]
            candidate=';'.join(parts+[field+'='+value])
            with self.subTest(field=field,value=value):
                self.assertNotIn('BRENT',PF.parse_quotes(candidate,NOW))
        for field in ('I','TICK','S'):
            candidate=';'.join(part for part in OBSERVED.split(';') if not part.startswith(field+'='))
            with self.subTest(missing=field):
                self.assertNotIn('BRENT',PF.parse_quotes(candidate,NOW))

    def test_explicit_wrong_ids_are_rejected_again_at_identity_boundary(self):
        for change in ({'instrument_id':'99'}, {'raw_ticker':'WTI'},
                       {'provider_instrument_id':'99'}, {'provider_ticker':'WTI'},
                       {'source_pin_version':'SILENT_ROLL_V2'}):
            with self.subTest(change=change):
                q=quote(**change)
                self.assertIsNone(S.identity('BRENT',q))
                self.assertFalse(S.brent_quote_verified(q))
                self.assertFalse(S.matches(held(),q))

    def test_legacy_source_only_identity_remains_unverified(self):
        identity=S.position_identity(held())
        self.assertFalse(S.is_pinned_brent_identity(identity))
        self.assertNotIn('provider_instrument_id',identity)
        self.assertFalse(S.brent_quote_verified({'source':'ProFinance',
            'raw_label':'Brent oil','provider_series_verified':True}))
        legacy_fields=S.quote_identity_fields(identity)
        self.assertNotIn('instrument_id',legacy_fields)
        self.assertNotIn('provider_series_verified',legacy_fields)

    def test_complete_pin_round_trips_without_price_or_execution_permission(self):
        identity=S.brent_feed_pin_identity()
        fields=S.quote_identity_fields(identity)
        self.assertEqual(S.identity('BRENT',fields),identity)
        self.assertEqual(fields['raw_ticker'],'brent')
        self.assertEqual(fields['instrument_id'],'27')
        self.assertNotIn('price',fields)
        self.assertNotIn('observed_at',fields)
        self.assertNotIn('source_gate_pass',fields)
        for field in S.BRENT_PIN_FIELDS:
            broken=deepcopy(identity)
            broken.pop(field)
            self.assertEqual(S.quote_identity_fields(broken),{})

    def test_no_contract_is_not_a_wildcard_for_any_expiry(self):
        for pinned in (False,True):
            expected=S.position_identity(held(pinned))
            for cid in ('BRX6','BRZ6','27'):
                q=quote(contract_id=cid)
                actual=S.identity('BRENT',q)
                with self.subTest(pinned=pinned,cid=cid):
                    self.assertFalse(S.same(expected,actual))
                    self.assertFalse(S.same(actual,expected))
                    self.assertFalse(S.matches(held(pinned),q))

    def test_abstract_legacy_scope_compatibility_does_not_verify_a_quote(self):
        expected=S.brent_feed_pin_identity()
        old=S.position_identity(held())
        self.assertTrue(S.same(expected,old))
        self.assertTrue(S.same(old,expected))
        unproven=quote()
        unproven.pop('instrument_id')
        self.assertFalse(S.brent_quote_verified(unproven))
        self.assertFalse(S.matches(held(True),unproven))
        for key in S.BRENT_PIN_FIELDS:
            wrong=dict(expected,**{key:'other'})
            self.assertFalse(S.same(expected,wrong))
            self.assertFalse(S.same(old,wrong))

    def test_other_assets_keep_their_existing_protocol(self):
        q=PF.parse_quotes('S=Gold;LP=-4142.88;T=20:25:30',NOW)['GOLD']
        self.assertEqual(q['price'],4142.88)
        self.assertNotIn('provider_series_verified',q)
        self.assertEqual(S.identity('GOLD',q)['key'],'PROFINANCE:Gold')


class BrentHeldPinTests(TestCase):
    def setUp(self):
        self.enterContext(patch.dict(G._quotes,{},clear=True))
        self.enterContext(patch.dict(G._source_quotes,{},clear=True))
        self.enterContext(patch.dict(G._market_state,{},clear=True))

    def test_existing_position_gets_same_feed_quote_without_rebasing(self):
        position=held()
        before=deepcopy(position)
        G.publish_quote('BRENT',quote())
        current=G.quote_for_position(position,now=NOW)
        self.assertEqual(current['price'],102.08)
        self.assertEqual(position,before)
        basis=S.valuation_basis(position,current)
        self.assertEqual(basis['source_pin_status'],'PINNED_PROVIDER_FEED')
        self.assertTrue(basis['provider_series_verified'])
        self.assertIsNone(basis['contract_id'])
        self.assertFalse(basis['exact_contract_verified'])

    def test_wrong_newer_quote_never_replaces_last_matching_provider(self):
        G.publish_quote('BRENT',quote())
        G.publish_quote('BRENT',quote(instrument_id='99',price=99.))
        self.assertEqual(G.quote_for_position(held(),now=NOW)['price'],102.08)

    def test_unproven_label_only_quote_never_drives_a_mark_or_exit(self):
        q=quote()
        q.pop('instrument_id')
        G.publish_quote('BRENT',q)
        position=held()
        self.assertEqual(G.quote_for_position(position,now=NOW),{})
        self.assertEqual(G.position_mark_price(position,NOW),102.)
        self.assertEqual(G.exit_execution_quote(dict(position,_execution_quote=q),NOW),{})

    def test_saved_pin_outage_keeps_metadata_but_does_not_claim_live_proof(self):
        saved=S.valuation_basis(held(True))
        self.assertEqual(saved['source_pin_status'],'PINNED_PROVIDER_FEED')
        self.assertEqual(saved['provider_ticker'],'brent')
        self.assertEqual(saved['provider_instrument_id'],'27')
        self.assertFalse(saved['provider_series_verified'])
        legacy=S.valuation_basis(held())
        self.assertEqual(legacy['source_pin_status'],'AWAITING_PROVIDER_VERIFICATION')
        self.assertIsNone(legacy['provider_ticker'])
        self.assertIsNone(legacy['provider_instrument_id'])
        self.assertFalse(legacy['provider_series_verified'])


class BrentAdmissionPinTests(TestCase):
    def test_new_oil_risk_requires_verified_pin_even_with_generic_gate_pass(self):
        self.assertTrue(E.paper_source_gate('BRENT',quote())['eligible'])
        for change in ({'source':'MOEX ISS BRX6','contract':{'secid':'BRX6'}},
                       {'source':'Yahoo BZ=F'}, {'instrument_id':None},
                       {'instrument_id':'99'}, {'raw_ticker':None},
                       {'contract_id':'BRZ6'}):
            with self.subTest(change=change):
                rejected=E.paper_source_gate('BRENT',quote(**change))
                self.assertFalse(rejected['eligible'])
                self.assertIn('PRIMARY_SOURCE_GATE_FAILED',rejected['blockers'])

    def test_replaced_execution_quote_cannot_inherit_previous_identity_proof(self):
        old=quote()
        replacement={'source':'ProFinance','price':102.09,'observed_at':NOW.isoformat(),
                     'source_gate_pass':True,'market_open':True,'raw_label':'Brent oil'}
        current=S.execution_row(dict(old,_execution_quote=replacement))
        self.assertNotIn('instrument_id',current)
        self.assertNotIn('provider_series_verified',current)
        rejected=E.paper_source_gate('BRENT',current)
        self.assertFalse(rejected['eligible'])
        self.assertEqual(rejected['reason'],'PRIMARY_SOURCE_GATE_FAILED')

    def test_held_exact_moex_contract_keeps_its_protective_exit(self):
        current={'source':'MOEX ISS BRX6','contract':{'secid':'BRX6'},
                 'price':99.5,'observed_at':NOW.isoformat(),
                 'source_gate_pass':True,'market_open':True}
        position={'asset':'BRENT','direction':'LONG','stop_price':100.,
                  'avg_entry_price':101.,'last_price':101.,
                  'payload':{'price_source_lock':S.identity('BRENT',current)}}
        self.assertFalse(E.paper_source_gate('BRENT',current)['eligible'])
        self.assertEqual(G.protective_reason(position,current,NOW),'STOP')
        exit_quote=G.exit_execution_quote(dict(position,_execution_quote=current),NOW)
        self.assertEqual(exit_quote['contract']['secid'],'BRX6')
        self.assertEqual(exit_quote['price'],99.5)

    def test_other_assets_keep_single_source_admission(self):
        for asset in ('GOLD','NQ','MOEX','CNYRUBF'):
            current={'source':'configured existing source','price':100.,
                     'source_gate_pass':True,'market_open':True}
            with self.subTest(asset=asset):
                self.assertTrue(E.paper_source_gate(asset,current)['eligible'])


class BrentLegacyManagementPinTests(TestCase):
    def test_legacy_position_can_trail_from_fully_verified_current_feed(self):
        import veritas_timeframe_management as TM
        from test_veritas_timeframe_management import SameTimeframeManagementTests
        fixture=SameTimeframeManagementTests()
        fixture.setUp()
        position,row=fixture.position(),fixture.row()
        legacy=S.position_identity(held())
        current=quote(price=110.,observed_at=fixture.now.isoformat())
        position.update(asset='BRENT')
        position['payload']['price_source_lock']=legacy
        row.update(asset='BRENT',source_names={'primary':'ProFinance'},
                   raw_label='Brent oil',raw_ticker='brent',instrument_id='27')
        row['timeframe_entry_context']['source_identity']=S.brent_feed_pin_identity()
        original=deepcopy(position)
        candidate=TM.trailing_candidate(position,[row],current,fixture.now)
        self.assertTrue(candidate['eligible'],candidate)
        self.assertGreater(candidate['stop_price'],position['stop_price'])
        self.assertEqual(position,original)
        bad=TM.trailing_candidate(position,[row],dict(current,instrument_id='99'),fixture.now)
        self.assertFalse(bad['eligible'])

    def test_legacy_position_can_use_current_same_feed_reversal_without_rewriting_entry(self):
        import veritas_position_thesis as PT
        import veritas_timeframe_structure as TFS
        from test_veritas_position_thesis import scenario,START
        position,row,now=scenario()
        bars=[dict(ts=START+i*300,open=100.,high=100.5,low=99.5,close=100.,
                   volume=100.,timeframe='5m',source_identity=S.brent_feed_pin_identity())
              for i in range(32)]
        bars[24]['high'],bars[27]['low']=101.,99.
        bars.append(dict(ts=START+32*300,open=100.,high=101.3,low=100.,close=101.2,
                         volume=150.,timeframe='5m',source_identity=S.brent_feed_pin_identity()))
        context=TFS.build_context(bars,'5m',now,asset='BRENT',
                                  source_identity=S.brent_feed_pin_identity())
        position['asset']='BRENT'
        position['payload']['price_source_lock']=S.position_identity(held())
        row.update(asset='BRENT',source='ProFinance',raw_label='Brent oil',
                   raw_ticker='brent',instrument_id='27',timeframe_entry_context=context)
        original=deepcopy(position)
        decision=PT.evaluate_exit(position,row,now)
        self.assertTrue(decision['exit_authorized'],decision)
        self.assertEqual(position,original)
        self.assertFalse(PT.evaluate_exit(position,dict(row,instrument_id=None),now)['exit_authorized'])

    def test_observation_witness_retains_pin_and_rejects_a_conflicting_saved_id(self):
        import veritas_observation_path as PATH
        position=held(True)
        position.update(opened_at=NOW.isoformat(),horizon='5m')
        position['payload'].update(initial_stop_price=100.,entry_atr=1.,
            entry_execution_model={'fill_price':101.18})
        witness=PATH.observe(position,quote(),NOW,at_entry=True)
        self.assertEqual(witness['source_identity'],S.brent_feed_pin_identity())
        self.assertEqual(witness['observation_count'],1)
        witness['source_identity']['provider_instrument_id']='99'
        position['payload']['observation_path']=witness
        later=NOW+timedelta(seconds=15)
        changed=PATH.observe(position,quote(observed_at=later.isoformat()),later)
        self.assertEqual(changed['observation_count'],1)
        self.assertEqual(changed['invalid_observation_count'],1)


@skipUnless(os.getenv('VERITAS_QUALITY_TEST_DSN'),'isolated PostgreSQL test database not configured')
class BrentAuditPinSQLTests(TestCase):
    def test_bounded_sql_projection_keeps_pin_and_absent_legacy_fields(self):
        import psycopg
        import veritas_learning_integrity as LI
        with psycopg.connect(os.environ['VERITAS_QUALITY_TEST_DSN']) as connection:
            self.assertEqual(connection.execute('SELECT current_database()').fetchone()[0],
                             'veritas_quality_test')
            def projected(identity):
                sql='SELECT '+LI._identity('s.source')+' FROM (SELECT %s::jsonb AS source) s'
                return connection.execute(sql,(json.dumps(identity),)).fetchone()[0]
            complete=projected(S.brent_feed_pin_identity())
            self.assertTrue(S.is_pinned_brent_identity(complete))
            legacy=projected(S.position_identity(held()))
            self.assertTrue(S.same(legacy,S.brent_feed_pin_identity()))
            for field in S.BRENT_PIN_FIELDS:
                self.assertNotIn(field,legacy)
            for value in (None,{'unexpected':'nested value'}):
                invalid=projected(dict(S.brent_feed_pin_identity(),source_pin_version=value))
                self.assertIn('source_pin_version',invalid)
                self.assertFalse(S.same(S.brent_feed_pin_identity(),invalid))


class BrentChartPinTests(TestCase):
    def test_refresh_requires_matching_label_and_ticker_without_ambiguity(self):
        verified=H.parse_refresh(REFRESH,'BRENT')
        self.assertTrue(verified['provider_chart_identity_verified'])
        self.assertEqual(verified['raw_ticker'],'brent')
        self.assertNotIn('instrument_id',verified)
        bad=(REFRESH.replace('s;Brent oil\n',''),
             REFRESH.replace('s;Brent oil','s;WTI'),
             REFRESH.replace('t;brent','t;WTI'),
             's;WTI\n'+REFRESH,'t;WTI\n'+REFRESH)
        for text in bad:
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError,'INSTRUMENT_MISMATCH'):
                    H.parse_refresh(text,'BRENT')

    def test_native_history_carries_verified_chart_and_configured_source_pin(self):
        calls=[]
        def fetch(url,params,remaining):
            calls.append((url,params))
            return REFRESH if url.endswith('refresh') else HISTORY
        cache=H.HistoryCache(fetch_text=fetch,clock=NOW.timestamp,monotonic=lambda:0.)
        result=cache.fetch_bundle('BRENT',('1m',))
        self.assertEqual(calls[0][1]['s'],'Brent oil')
        self.assertEqual(calls[1][1]['s'],'brent')
        self.assertEqual(calls[1][1]['ba'],2)
        self.assertEqual(result['source_identity'],S.brent_feed_pin_identity())
        self.assertTrue(result['provider_chart_identity_verified'])
        self.assertEqual(result['raw_ticker'],'brent')
        bar=result['bars_by_timeframe']['1m'][0]
        self.assertEqual(bar['source_identity'],S.brent_feed_pin_identity())
        self.assertTrue(bar['provider_chart_identity_verified'])
        self.assertEqual(bar['raw_ticker'],'brent')
        self.assertNotIn('instrument_id',bar)
        again=cache.fetch_bundle('BRENT',('1m',))
        self.assertEqual(len(calls),2)
        self.assertTrue(again['provider_chart_identity_verified'])

    def test_wrong_refresh_never_fetches_or_labels_history_verified(self):
        calls=[]
        def fetch(url,params,remaining):
            calls.append(url)
            return REFRESH.replace('s;Brent oil','s;WTI')
        cache=H.HistoryCache(fetch_text=fetch,clock=NOW.timestamp,monotonic=lambda:0.)
        result=cache.fetch_bundle('BRENT',('1m',))
        self.assertEqual(len(calls),1)
        self.assertEqual(result['bars_by_timeframe']['1m'],[])
        self.assertFalse(result['provider_chart_identity_verified'])
        self.assertIsNone(result['raw_ticker'])

    def test_failed_poll_preserves_old_proof_and_original_quote_clock(self):
        clock=[NOW.timestamp()]
        failed=[False]
        def fetch(url,params,remaining):
            if failed[0]:
                raise RuntimeError('provider unavailable')
            return REFRESH if url.endswith('refresh') else HISTORY
        cache=H.HistoryCache(fetch_text=fetch,clock=lambda:clock[0],monotonic=lambda:0.)
        original=cache.fetch_bundle('BRENT',('1m',))
        failed[0]=True
        clock[0]+=120
        stale=cache.fetch_bundle('BRENT',('1m',))
        self.assertTrue(stale['provider_chart_identity_verified'])
        self.assertEqual(stale['bars_by_timeframe'],original['bars_by_timeframe'])
        self.assertEqual(stale['status_by_timeframe']['1m']['status'],'STALE')
        self.assertEqual(stale['status_by_timeframe']['1m']['fetched_at'],
                         original['status_by_timeframe']['1m']['fetched_at'])
