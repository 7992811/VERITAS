"""Reproduce the GOLD source switch at actual marking/closing boundaries."""
from datetime import datetime,timezone,timedelta
from unittest import TestCase
from unittest.mock import MagicMock,patch
import json

import veritas_price_source as S
import veritas_position_guard as G
import veritas_portfolio as P
import veritas_portfolio_runtime as R
import veritas_trade_view as V
import veritas_trade_audit as A


class SourceLockTests(TestCase):
    def setUp(self):
        self.now=datetime.now(timezone.utc)
        self.enterContext(patch.dict(G._quotes,{},clear=True))
        self.enterContext(patch.dict(G._source_quotes,{},clear=True))

    def quote(self,source='ProFinance',price=4136.08,asset='GOLD',age=0,contract=None):
        q={'price':price,'observed_at':(self.now-timedelta(seconds=age)).isoformat(),
           'source_gate_pass':True,'market_open':True,'source_names':{'primary':source}}
        if contract:q['contract']={'secid':contract}
        if source=='ProFinance':q['raw_label']='Gold' if asset=='GOLD' else 'NASD100_FUT'
        return q

    def position(self,source='ProFinance',asset='GOLD',contract=None):
        identity=S.identity(asset,self.quote(source,asset=asset,contract=contract))
        return {'asset':asset,'direction':'SHORT','avg_entry_price':4137.505959,
                'last_price':4158.875699230643,'stop_price':4182.2998046875,
                'units':47.25234092374513,'active_trade_id':'gold-audit','target_fraction':.2,
                'payload':{'price_source_lock':identity,'entry_primary_source':source,
                           'source_locked_mark':{'identity':identity,'price':4139.7,
                                                 'observed_at':(self.now-timedelta(minutes=4)).isoformat()}}}

    def test_newer_gc_and_proxy_cannot_replace_profinance(self):
        z=self.position()
        G.publish_quote('GOLD',self.quote(age=10))
        for source in ('Yahoo Gold GC=F','GLD proxy bridge','Stooq public futures quote'):
            G.publish_quote('GOLD',self.quote(source,4158.875699230643))
            self.assertEqual(G.quote_for_position(z,now=self.now)['price'],4136.08)
            self.assertEqual(G.position_mark_price(z,self.now),4136.08)

    def test_stale_same_source_freezes_last_verified_mark(self):
        z=self.position();G.publish_quote('GOLD',self.quote(age=600))
        G.publish_quote('GOLD',self.quote('Yahoo Gold GC=F',4158.88))
        self.assertEqual(G.quote_for_position(z,now=self.now),{})
        self.assertEqual(G.position_mark_price(z,self.now),4139.7)

    def test_nav_uses_pinned_quote_instead_of_global_asset_price(self):
        z=self.position();G.publish_quote('GOLD',self.quote())
        p={'initial_nav_rub':1e6,'realized_pnl_rub':0,'fees_rub':0,'funding_rub':0}
        nav,unreal,_,_=P._mark_nav(p,[z],{'GOLD':4158.875699230643})
        self.assertAlmostEqual(unreal,z['units']*(z['avg_entry_price']-4136.08))
        self.assertGreater(nav,1e6)

    def test_all_four_portfolios_filter_foreign_management_rows(self):
        z=self.position();G.publish_quote('GOLD',self.quote())
        wrong=dict(self.quote('GLD proxy bridge',4158.88),asset='GOLD',research_decision='NO_TRADE',
                   trade_plan={'trade_integrity':{'hard_invalidation':True}})
        c=MagicMock();c.execute.return_value.fetchall.return_value=[z]
        for name in ('Impulse','Aggressive','Champion','Challenger'):
            with patch.object(R,'_r80_base_step_one',return_value={}) as base:
                R._step_one(c,name,{}, {'GOLD':wrong},{'GOLD':4158.88},14,84,self.now.isoformat(),.0005,[wrong])
                self.assertEqual(base.call_args.args[3],{})
                self.assertEqual(base.call_args.args[4]['GOLD'],4136.08)
                self.assertEqual(base.call_args.args[-1],[])

    def test_main_close_uses_same_source_at_final_accounting_boundary(self):
        z=self.position();G.publish_quote('GOLD',self.quote())
        G.publish_quote('GOLD',self.quote('GLD proxy bridge',4158.875699230643))
        c=MagicMock();c.execute.return_value.fetchone.return_value=None
        fee=P._v90j_base_close_or_reduce(c,{},'Impulse',z,4158.875699230643,0,1e6,self.now.isoformat(),'HARD_THESIS_INVALIDATION')
        self.assertGreater(fee,0)
        order=next(x for x in c.execute.call_args_list if x.args[0].startswith('INSERT INTO paper_orders'))
        audit=json.loads(order.args[1][10])
        self.assertEqual(audit['reference_price'],4136.08)
        self.assertEqual(audit['price_source_identity']['key'],'PROFINANCE:Gold')
        self.assertLess(order.args[1][5],4140.)

    def test_missing_source_blocks_final_mutation(self):
        z=self.position();G.publish_quote('GOLD',self.quote('Yahoo Gold GC=F',4190))
        c=MagicMock()
        self.assertEqual(P._v90j_base_close_or_reduce(c,{},'Impulse',z,4190,0,1e6,self.now.isoformat(),'STOP'),0)
        c.execute.assert_not_called()

    def test_false_foreign_stop_does_not_close_position(self):
        z=self.position();G.publish_quote('GOLD',self.quote())
        with patch.object(R,'_r80_base_close_or_reduce') as close:
            self.assertEqual(R._close_or_reduce(MagicMock(),{},'Impulse',z,4190,0,1e6,self.now.isoformat(),'STOP'),0)
            close.assert_not_called()
        c=MagicMock()
        self.assertEqual(P._v90j_base_close_or_reduce(c,{},'Impulse',z,4190,0,1e6,self.now.isoformat(),'STOP'),0)
        c.execute.assert_not_called()

    def test_real_same_source_stop_reaches_close(self):
        z=self.position();G.publish_quote('GOLD',self.quote(price=4183))
        with patch.object(R,'_r80_base_close_or_reduce',return_value=1) as close:
            self.assertEqual(R._close_or_reduce(MagicMock(),{},'Impulse',z,4183,0,1e6,self.now.isoformat(),'STOP'),1)
            self.assertEqual(close.call_args.args[4],4183)

    def test_dashboard_ignores_foreign_live_or_stored_mark(self):
        z=self.position();G.publish_quote('GOLD',self.quote())
        with patch.object(V.VPP,'load_accounts',return_value={}),patch.object(V.VPP,'evaluate',return_value={}):
            report=V.enrich_positions({'portfolios':[{'positions':[z]}]},MagicMock())
        shown=report['portfolios'][0]['positions'][0]
        self.assertEqual(shown['last_price'],4136.08)
        self.assertEqual(shown['price_source_status'],'OK')
        self.assertGreater(shown['unrealized_pnl_rub'],0)

    def test_dashboard_marks_stale_pinned_quote(self):
        z=self.position();G.publish_quote('GOLD',self.quote('Yahoo Gold GC=F',4200))
        with patch.object(V.VPP,'load_accounts',return_value={}),patch.object(V.VPP,'evaluate',return_value={}):
            report=V.enrich_positions({'portfolios':[{'positions':[z]}]},MagicMock())
        shown=report['portfolios'][0]['positions'][0]
        self.assertEqual(shown['last_price'],4139.7)
        self.assertEqual(shown['price_source_status'],'PINNED_SOURCE_QUOTE_UNAVAILABLE')

    def test_same_asset_two_sources_remain_separate(self):
        G.publish_quote('GOLD',self.quote())
        G.publish_quote('GOLD',self.quote('Yahoo Gold GC=F',4170))
        self.assertEqual(G.quote_for_position(self.position(),now=self.now)['price'],4136.08)
        self.assertEqual(G.quote_for_position(self.position('Yahoo Gold GC=F'),now=self.now)['price'],4170)

    def test_explicit_contract_cannot_roll_or_disappear(self):
        z=self.position('Yahoo Gold GC=F',contract='GCZ26')
        for cid in ('GCG27',None):
            self.assertFalse(S.matches(z,self.quote('Yahoo Gold GC=F',contract=cid)))
        self.assertTrue(S.matches(z,self.quote('Yahoo Gold GC=F',contract='GCZ26')))

    def test_nq_futures_label_and_proxy_identity_are_distinct(self):
        z=self.position('ProFinance',asset='NQ');q=self.quote('ProFinance',asset='NQ')
        self.assertTrue(S.matches(z,q));q['raw_label']='NASD100'
        self.assertFalse(S.matches(z,q))
        self.assertNotEqual(S.identity('GOLD',self.quote('Yahoo GLD proxy'))['key'],
                            S.identity('GOLD',self.quote('Yahoo Gold GC=F'))['key'])

    def test_refresh_fetches_each_position_source_once(self):
        z=self.position();other=self.position('Yahoo Gold GC=F')
        def fetch(ns,asset,positions):
            return self.quote(S.position_identity(positions[0])['primary_source'])
        with patch.object(G,'fetch_guard_quote',side_effect=fetch) as f:
            G.refresh_position_quotes({},[z,z,other])
        self.assertEqual(f.call_count,2)
        self.assertTrue(G.quote_for_position(z,now=self.now))
        self.assertTrue(G.quote_for_position(other,now=self.now))

    def test_mixed_source_incident_is_excluded_without_rewriting_pnl(self):
        z={'asset':'GOLD','payload':{'data_integrity_status':'MIXED_PRICE_SOURCES'}}
        self.assertEqual(A.evidence_exclusion(z),'DATA_INTEGRITY')
        c=MagicMock();c.__enter__.return_value=c
        with patch.object(R,'_r80_incident_checked',False):
            R._r80_quarantine_source_incident(lambda:c)
        for call in c.execute.call_args_list:
            sql=call.args[0]
            self.assertNotIn('SET net_pnl',sql);self.assertNotIn('SET realized_pnl',sql)
            self.assertIn('payload=',sql)

    def test_entry_persists_source_and_actual_quote_time(self):
        row=dict(self.quote(),asset='GOLD',research_decision='SHORT',horizon='1h',
                 _pwin=.7,_pwin_source='MODEL_QUALITY_SCORE_UNCALIBRATED',
                 trade_plan={'stop_price':4182.3,'target_price':4074.})
        c=MagicMock();c.execute.return_value.fetchone.return_value=None
        with patch.object(P.VX,'entry_gate',return_value={'eligible':True}),\
             patch('veritas_trend_entry.prepare_row',side_effect=lambda row,*args:row),\
             patch.object(P,'_portfolio_canonical_setup_id',return_value='test-source-entry'):
            P._v90j_base_open_or_add(c,{},'Impulse','GOLD','SHORT',4158.88,.2,1e6,self.now.isoformat(),row,'TEST')
        call=next(c for c in c.execute.call_args_list if c.args[0].startswith('INSERT INTO paper_positions'))
        payload=json.loads(call.args[1][-1])
        self.assertEqual(payload['price_source_lock']['key'],'PROFINANCE:Gold')
        self.assertEqual(payload['entry_execution_observed_at'],row['observed_at'])
        self.assertEqual(payload['entry_execution_model']['reference_price'],4136.08)

    def test_add_cannot_switch_position_provider(self):
        row=dict(self.quote('Yahoo Gold GC=F'),asset='GOLD',research_decision='SHORT',horizon='1h')
        c=MagicMock();c.execute.return_value.fetchone.return_value=self.position()
        with patch('veritas_trend_entry.prepare_row',side_effect=lambda row,*args:row):
            self.assertEqual(P._v90j_base_open_or_add(c,{},'Impulse','GOLD','SHORT',4136,.3,1e6,self.now.isoformat(),row,'ADD'),0)
        self.assertEqual(row['_execution_audit']['reason'],'POSITION_SOURCE_MISMATCH')
        self.assertFalse(any(x.args[0].startswith(('INSERT','UPDATE','DELETE')) for x in c.execute.call_args_list))
