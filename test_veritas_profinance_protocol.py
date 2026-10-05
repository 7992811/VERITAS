from datetime import datetime,timezone,timedelta
from unittest import TestCase
from unittest.mock import MagicMock,patch
import veritas_profinance as PF
import veritas_position_guard as G

NOW=datetime(2026,10,5,13,52,32,tzinfo=timezone.utc)


class QuoteProtocolTests(TestCase):
    def test_observed_down_quotes_remain_valid_positive_prices(self):
        # Actual public response, 2026-10-05 16:52 Moscow time.
        text=('1;I=57;S=NASD100_FUT;TICK=NASD100_FUT;LP=-31178.00;T=16:52:30\n'
              '1;I=20;S=Gold;TICK=gold;LP=-4142.88;T=16:52:31\n')
        q=PF.parse_quotes(text,NOW)
        self.assertEqual(q['NQ']['price'],31178.)
        self.assertEqual(q['GOLD']['price'],4142.88)
        self.assertEqual(q['GOLD']['raw_price'],'-4142.88')
        self.assertEqual(q['GOLD']['quote_direction_sign'],'-')
        self.assertEqual(q['NQ']['observed_at'],'2026-10-05T13:52:30+00:00')
        self.assertFalse(q['NQ']['exact_contract_verified'])
        self.assertFalse(q['GOLD']['execution_eligible'])

    def test_up_and_unsigned_prices_match(self):
        for value in ('+4142.88','4142.88'):
            self.assertEqual(PF.parse_quotes('S=Gold;LP='+value+';T=16:52:31',NOW)['GOLD']['price'],4142.88)

    def test_malformed_or_zero_price_is_not_repaired(self):
        for value in ('--4142','+-4142','-0','0','NaN','inf','-inf','4142x',''):
            self.assertEqual(PF.parse_quotes('S=Gold;LP='+value+';T=16:52:31',NOW),{})

    def test_old_clock_and_spot_index_do_not_become_fresh_futures(self):
        self.assertEqual(PF.parse_quotes('S=Gold;LP=-4142.88;T=16:52:31',NOW+timedelta(minutes=4)),{})
        self.assertEqual(PF.parse_quotes('S=NASD100;LP=-31178;T=16:52:31',NOW),{})


class GoldProtectiveSourceTests(TestCase):
    def setUp(self):
        self.enterContext(patch.dict(G._quotes,{},clear=True))
        self.enterContext(patch.dict(G._source_quotes,{},clear=True))

    def position(self,source='ProFinance'):
        return {'asset':'GOLD','direction':'SHORT','stop_price':4182.,'last_price':4140.,
                'payload':{'entry_primary_source':source,
                           'contract_identity':{'primary_source':source,'contract_id':None}}}

    def quote(self,source='ProFinance',price=4183.):
        return {'price':price,'observed_at':NOW.isoformat(),'source_gate_pass':True,
                'raw_label':'Gold','source_names':{'primary':source}}

    def test_gold_guard_fetches_the_entry_feed(self):
        q=self.quote();q['observed_at']=datetime.now(timezone.utc).isoformat()
        fetch=MagicMock(return_value=q);yahoo=MagicMock()
        result=G.fetch_guard_quote({'_v90r61_profinance_quote':fetch,'_yahoo_series':yahoo},
                                  'GOLD',[self.position()])
        self.assertEqual(result['source_names']['primary'],'ProFinance')
        self.assertEqual(result['price'],4183.)
        fetch.assert_called_once_with('GOLD');yahoo.assert_not_called()

    def test_missing_same_feed_never_substitutes_gc_future(self):
        yahoo=MagicMock()
        ns={'_v90r61_profinance_quote':MagicMock(return_value={}), '_yahoo_series':yahoo}
        with patch.dict(G._quotes,{'GOLD':self.quote('Yahoo Gold GC=F')},clear=True):
            self.assertEqual(G.fetch_guard_quote(ns,'GOLD',[self.position()]),{})
        yahoo.assert_not_called()

    def test_stop_and_exit_fill_require_same_gold_basis(self):
        for source,other in (('ProFinance','Yahoo Gold GC=F'),('Yahoo Gold GC=F','ProFinance')):
            z=self.position(source);q=self.quote(other)
            self.assertIsNone(G.protective_reason(z,q,NOW))
            self.assertEqual(G.exit_execution_quote({**z,'_execution_quote':q},NOW),{})
            self.assertEqual(G.protective_reason(z,self.quote(source),NOW),'STOP')

    def test_cross_basis_quote_cannot_mutate_path_or_profit_lock(self):
        conn=MagicMock();conn.__enter__.return_value=conn
        conn.execute.return_value.fetchall.return_value=[self.position()]
        vp=MagicMock()
        self.assertEqual(G.run_protective_pass(vp,lambda:conn,
                         {'GOLD':self.quote('Yahoo Gold GC=F')},NOW),[])
        self.assertFalse(any(call.args[0].startswith(('UPDATE','INSERT','DELETE'))
                             for call in conn.execute.call_args_list))
        vp._close_or_reduce.assert_not_called()

    def test_explicit_different_contracts_remain_ineligible(self):
        z=self.position();z['payload']['entry_contract_secid']='GCZ26'
        q=self.quote();q['contract']={'secid':'GCG27'}
        self.assertIsNone(G.protective_reason(z,q,NOW))
