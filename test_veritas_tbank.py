import json
import unittest
import threading
from http.server import ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import grpc
import veritas_tbank as T
from vendor.tbank import readonly_pb2 as P


class FakeReader:
    def __init__(self):
        self.calls = []
        self.now = T.iso()
        self.ids = {'CNYRUBF':'exact-contract', 'NAZ6':'nasd-contract', 'IMOEXF':'imoex-contract'}

    def call(self, name, **kw):
        self.calls.append((name, kw))
        if name == 'trading_status':
            return {'trading_status':'SECURITY_TRADING_STATUS_NORMAL_TRADING','api_trade_available_flag':True}
        if name == 'accounts':
            return {'accounts': [{'id':'private-account', 'name':'Private name',
                'status':'ACCOUNT_STATUS_OPEN','access_level':'ACCOUNT_ACCESS_LEVEL_READ_ONLY'}]}
        if name == 'portfolio':
            return {'account_id':kw['account_id'], 'total_amount_portfolio':{'units':'1000000','currency':'rub'}}
        if name == 'find':
            ticker=kw['query']
            return {'instruments':[{'ticker':ticker,'uid':self.ids[ticker]},
                                   {'ticker':ticker+'_OTHER','uid':'other'}]}
        if name == 'instrument':
            ticker=next(t for t,uid in self.ids.items() if uid==kw['id'])
            return {'instrument': {'uid':kw['id'],'ticker':ticker,'lot':1,'instrument_type':'futures'}}
        if name == 'future':
            ticker=next(t for t,uid in self.ids.items() if uid==kw['id'])
            return {'instrument': {'uid':kw['id'],'ticker':ticker,'lot':1,
                'real_exchange':'REAL_EXCHANGE_MOEX','exchange':'MOEX','name':ticker,
                'expiration_date':T.iso(T.utcnow()+timedelta(days=365)),
                'min_price_increment':{'units':'0','nano':1000000},
                'min_price_increment_amount':{'units':'1'}}}
        if name == 'prices':
            return {'last_prices':[{'instrument_uid':uid,'time':self.now,
                                   'price':{'units':'12','nano':759000000}} for uid in kw['instrument_id']]}
        if name == 'book':
            return {'instrument_uid':kw['instrument_id'],'orderbook_ts':self.now,
                    'bids':[{'price':{'units':'12','nano':758000000},'quantity':'10'}]}
        if name == 'candles':
            closed={'time':self.now, 'is_complete':True, 'open':{'units':'12'},
                    'high':{'units':'13'},'low':{'units':'11'},'close':{'units':'12'},'volume':'20'}
            return {'candles':[closed, {**closed,'is_complete':False,'close':{'units':'999'}}]}
        raise AssertionError(name)


class ConnectionTests(unittest.TestCase):
    def test_without_token_never_opens_connection_or_thread(self):
        factory = unittest.mock.Mock()
        c = T.TBankConnection({}, factory)
        c.start()
        self.assertIsNone(c.worker)
        self.assertEqual(c.status()['status'], 'WAITING_TOKEN')
        self.assertFalse(c.status()['orders_enabled'])
        factory.assert_not_called()

    def test_refresh_downloads_closed_history_and_exact_market_data(self):
        c = T.TBankConnection({'TBANK_API_TOKEN':'test-secret'})
        c.reader = FakeReader()
        c.refresh()
        self.assertEqual(c.status()['status'],'CONNECTED')
        self.assertEqual(c.market_data()['quotes']['CNYRUBF']['price'],12.759)
        self.assertEqual(c.market_data()['order_books']['CNYRUBF']['source'],'TBANK_GRPC')
        candles=c.candle_snapshot('CNYRUBF','5m')
        self.assertEqual(len(candles['candles']),1)
        self.assertEqual(candles['candles'][0]['close'],12)
        self.assertEqual(candles['candles'][0]['volume_lots'],20)
        self.assertTrue(c.status()['paper_source_switch_enabled'])

    def test_public_status_and_quotes_do_not_leak_account_or_secret(self):
        c = T.TBankConnection({'TBANK_API_TOKEN':'test-secret'})
        c.reader = FakeReader()
        c.refresh()
        public=json.dumps([c.status(),c.market_data()])
        for value in ('test-secret','private-account','Private name','1000000'):
            self.assertNotIn(value,public)
        self.assertEqual(c.private_snapshot('accounts')['accounts'][0]['id'],'private-account')

    def test_private_auth_fails_closed_when_not_configured(self):
        self.assertFalse(T.private_access({},{}))
        self.assertFalse(T.private_access({'X-Veritas-Token':'x'},{}))
        env={'VERITAS_APP_AUTH_TOKEN':'configured'}
        self.assertFalse(T.private_access({},env))
        self.assertFalse(T.private_access({'X-Veritas-Token':'wrong'},env))
        self.assertTrue(T.private_access({'X-Veritas-Token':'configured'},env))

    def test_cached_data_is_copy_and_stale_prices_stay_stale(self):
        c = T.TBankConnection({})
        c.quotes['CNYRUBF']={'price':12,'observed_at':T.iso(T.utcnow()-timedelta(minutes=20))}
        q=c.market_data()
        self.assertTrue(q['quotes']['CNYRUBF']['stale'])
        q['quotes']['CNYRUBF']['price']=99
        self.assertEqual(c.quotes['CNYRUBF']['price'],12)
        c.state='CONNECTED';c.checked_at=T.iso(T.utcnow()-timedelta(minutes=5))
        self.assertEqual(c.status()['status'],'STALE')

    def test_future_or_missing_quote_time_is_not_fresh(self):
        self.assertIsNone(T.age_seconds('not-a-time'))
        self.assertIsNone(T.age_seconds(T.iso(T.utcnow()+timedelta(hours=1))))

    def test_stream_ignores_unknown_contract_and_older_messages(self):
        c=T.TBankConnection({})
        def quote(uid,at,n):
            return {'instrument_uid':uid,'time':T.iso(at),'price':{'units':str(n)}}
        now=T.utcnow()
        c._put_quote(quote('right',now,12), {'right':'CNYRUBF'})
        c._put_quote(quote('wrong',now,99), {'right':'CNYRUBF'})
        c._put_quote(quote('right',now-timedelta(seconds=60),10), {'right':'CNYRUBF'})
        self.assertEqual(c.quotes['CNYRUBF']['price'],12)

    def test_ambiguous_ticker_is_never_silently_resolved(self):
        reader=unittest.mock.Mock()
        reader.call.return_value={'instruments':[{'ticker':'CNYRUBF','uid':'a'}, {'ticker':'CNYRUBF','uid':'b'}]}
        with self.assertRaisesRegex(T.TBankError,'INSTRUMENT_AMBIGUOUS'):
            T.exact_instrument(reader,'CNYRUBF')
        reader.call.assert_called_once()

    def test_similar_ticker_is_not_an_exact_match(self):
        reader=unittest.mock.Mock()
        reader.call.return_value={'instruments':[{'ticker':'CNYRUBF_OTHER','uid':'a'}]}
        with self.assertRaisesRegex(T.TBankError,'INSTRUMENT_NOT_FOUND'):
            T.exact_instrument(reader,'CNYRUBF')

    def test_account_outside_token_scope_is_rejected(self):
        c=T.TBankConnection({'TBANK_ACCOUNT_ID':'another-account'})
        c.reader=FakeReader()
        with self.assertRaisesRegex(T.TBankError,'ACCOUNT_NOT_ACCESSIBLE'):
            c.refresh()
        self.assertFalse(any(name=='portfolio' for name,_ in c.reader.calls))

    def test_native_decimal_units_are_not_lot_adjusted(self):
        self.assertAlmostEqual(T.price({'units':'-1','nano':-500000000}),-1.5)
        self.assertAlmostEqual(T.price({'units':'0','nano':1}),0.000000001)

    def test_missing_instrument_is_reported_without_inventing_mapping(self):
        c=T.TBankConnection({})
        c.reader=FakeReader()
        with patch.object(T,'exact_instrument',side_effect=T.TBankError('INSTRUMENT_NOT_FOUND')):
            c.refresh()
        self.assertEqual(c.status()['instrument_errors'],{a:'INSTRUMENT_NOT_FOUND' for a in T.DEFAULT_TICKERS})
        self.assertEqual(c.market_data()['quotes'],{})


class WireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.metadata=[]
        cls.server=grpc.server(ThreadPoolExecutor(max_workers=2))
        def accounts(request,context):
            cls.metadata.append(dict(context.invocation_metadata()))
            if request.status!=P.ACCOUNT_STATUS_OPEN:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT,'private diagnostic with test-secret')
            return P.GetAccountsResponse(accounts=[P.Account(id='test-account',access_level=P.ACCOUNT_ACCESS_LEVEL_READ_ONLY)])
        def stream(request,context):
            assert request.subscribe_last_price_request.instruments[0].instrument_id=='contract'
            yield P.MarketDataResponse(last_price=P.LastPrice(instrument_uid='contract',price=P.Quotation(units=12)))
        def future(request,context):
            assert request.id=='nasd-contract' and request.id_type==P.INSTRUMENT_ID_TYPE_UID
            return P.FutureResponse(instrument=P.Future(uid=request.id,ticker='NAZ6',lot=1,
                real_exchange=P.REAL_EXCHANGE_MOEX, basic_asset='NASDAQ100',
                min_price_increment=P.Quotation(units=1), min_price_increment_amount=P.Quotation(nano=800000000),
                expiration_date={'seconds':int((T.utcnow()+timedelta(days=30)).timestamp())}))
        cls.server.add_generic_rpc_handlers((
            grpc.method_handlers_generic_handler(T.PACKAGE+'.UsersService',{'GetAccounts':grpc.unary_unary_rpc_method_handler(accounts,request_deserializer=P.GetAccountsRequest.FromString,response_serializer=lambda x:x.SerializeToString())}),
            grpc.method_handlers_generic_handler(T.PACKAGE+'.MarketDataStreamService',{'MarketDataServerSideStream':grpc.unary_stream_rpc_method_handler(stream,request_deserializer=P.MarketDataServerSideStreamRequest.FromString,response_serializer=lambda x:x.SerializeToString())}),
            grpc.method_handlers_generic_handler(T.PACKAGE+'.InstrumentsService',{'FutureBy':grpc.unary_unary_rpc_method_handler(future,request_deserializer=P.InstrumentRequest.FromString,response_serializer=lambda x:x.SerializeToString())}),
        ))
        cls.port=cls.server.add_insecure_port('127.0.0.1:0')
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop(0).wait()

    def setUp(self):
        self.reader=T.GrpcReader('test-secret',channel=grpc.insecure_channel('127.0.0.1:'+str(self.port)))

    def tearDown(self):
        self.reader.close()

    def test_real_grpc_wire_authorization_and_decoding(self):
        data=self.reader.call('accounts',status='ACCOUNT_STATUS_OPEN')
        self.assertEqual(data['accounts'][0]['access_level'],'ACCOUNT_ACCESS_LEVEL_READ_ONLY')
        self.assertEqual(self.metadata[-1]['authorization'],'Bearer test-secret')

    def test_real_grpc_stream_request_and_decoding(self):
        values=list(self.reader.stream_prices(['contract']))
        self.assertEqual(values[0]['last_price']['instrument_uid'],'contract')

    def test_future_specification_on_actual_grpc_wire(self):
        result=T.future_metadata(self.reader,{'uid':'nasd-contract','ticker':'NAZ6'})
        self.assertEqual(result['basic_asset'],'NASDAQ100')
        self.assertEqual(T.price(result['min_price_increment_amount']),0.8)
        self.assertFalse(result['perpetual'])

    def test_rpc_error_details_are_not_exposed(self):
        with self.assertRaises(T.TBankError) as ex:
            self.reader.call('accounts')
        self.assertEqual(str(ex.exception),'INVALID_ARGUMENT')
        self.assertNotIn('test-secret',repr(self.reader))

    def test_no_order_or_transfer_methods_exist(self):
        for name in ('PostOrder','CancelOrder','CurrencyTransfer','orders','/OrdersService/PostOrder'):
            with self.assertRaisesRegex(T.TBankError,'METHOD_NOT_ALLOWED'):
                self.reader.call(name)

    def test_secure_channel_has_scoped_roots_and_fixed_target(self):
        with patch('grpc.secure_channel') as secure, patch('grpc.ssl_channel_credentials') as tls:
            T.GrpcReader('test-secret')
        self.assertEqual(secure.call_args.args[0],T.TARGET)
        roots=tls.call_args.args[0]
        self.assertGreater(roots.count(b'BEGIN CERTIFICATE'),1)


class MultiAssetHistoryTests(unittest.TestCase):
    def connection(self):
        c=T.TBankConnection({'TBANK_API_TOKEN':'test-secret'})
        c.reader=FakeReader()
        return c

    def test_all_three_contracts_have_separate_uids_and_all_requested_intervals(self):
        c=self.connection();c.refresh();c.refresh()
        s=c.status()
        self.assertEqual(set(s['instruments']),{'CNYRUBF','NQF','MOEXF'})
        self.assertEqual(len({v['uid'] for v in s['instruments'].values()}),3)
        self.assertEqual(s['instruments']['NQF']['ticker'],'NAZ6')
        self.assertEqual(s['instruments']['MOEXF']['ticker'],'IMOEXF')
        self.assertEqual(s['requested_timeframes'],['1m','5m','1h','4h','1d','3d','7d'])
        for asset in s['instruments']:
            for tf in T.HISTORY:
                snapshot=c.candle_snapshot(asset,tf)
                self.assertEqual(snapshot['status'],'OK')
                self.assertEqual(snapshot['instrument_uid'],s['instruments'][asset]['uid'])
        self.assertEqual(c.candle_snapshot('NQf','1h')['status'],'OK')
        self.assertEqual(c.candle_snapshot('NQf','bad')['status'],'INVALID_INTERVAL')

    def test_compatible_request_windows_no_optional_filters_and_no_redundant_downloads(self):
        c=self.connection();c.refresh();c.refresh();c.refresh()
        calls=[kw for name,kw in c.reader.calls if name=='candles']
        self.assertEqual(len(calls),24)
        self.assertEqual(sum(name=='accounts' for name,_ in c.reader.calls),1)
        for kw in calls:
            config=next(v for v in T.HISTORY.values() if v[0]==kw['interval'])
            self.assertNotIn('limit',kw)
            self.assertNotIn('candle_source_type',kw)
            begin=datetime.fromisoformat(kw['from'].replace('Z','+00:00'))
            end=datetime.fromisoformat(kw['to'].replace('Z','+00:00'))
            self.assertEqual((end-begin).days,min(7,config[1]) if kw['interval']=='CANDLE_INTERVAL_HOUR' else config[1])
        hourly=[kw for kw in calls if kw['interval']=='CANDLE_INTERVAL_HOUR']
        self.assertEqual(len(hourly),9)  # 3 instruments x 3 bounded 7-day chunks.

    def test_one_failed_interval_does_not_block_other_contracts_or_fabricate_history(self):
        c=self.connection();original=c.reader.call
        def call(name,**kw):
            if name=='candles' and kw['interval']=='CANDLE_INTERVAL_DAY' and kw['instrument_id']=='nasd-contract':
                raise T.TBankError('RESOURCE_EXHAUSTED')
            return original(name,**kw)
        with patch.object(c.reader,'call',side_effect=call):
            c.refresh();c.refresh()
        self.assertEqual(c.status()['status'],'CONNECTED')
        self.assertEqual(c.candle_snapshot('NQF','1d')['status'],'ERROR')
        self.assertEqual(c.candle_snapshot('NQF','3d')['status'],'ERROR')
        self.assertEqual(c.candle_snapshot('MOEXF','1d')['status'],'OK')
        self.assertEqual(c.candle_snapshot('NQF','7d')['status'],'OK')

    def test_expired_contract_is_not_loaded_or_replaced_by_another_expiry(self):
        c=self.connection();original=c.reader.call
        def call(name,**kw):
            result=original(name,**kw)
            if name=='future' and kw['id']=='nasd-contract':
                result['instrument']['expiration_date']=T.iso(T.utcnow()-timedelta(days=1))
            return result
        with patch.object(c.reader,'call',side_effect=call):c.refresh()
        self.assertEqual(c.status()['instrument_errors']['NQF'],'FUTURE_EXPIRED_OR_EXPIRY_MISSING')
        self.assertNotIn('NQF',c.market_data()['quotes'])
        self.assertEqual(set(c.instruments),{'CNYRUBF','MOEXF'})

    def test_future_uid_venue_and_expiry_must_be_verified(self):
        for field,value,code in [('uid','other','FUTURE_IDENTITY_MISMATCH'),
            ('real_exchange','REAL_EXCHANGE_RTS','FUTURE_VENUE_MISMATCH'),
            ('expiration_date','bad','FUTURE_EXPIRED_OR_EXPIRY_MISSING')]:
            with self.subTest(field=field):
                reader=FakeReader();reply=reader.call('future',id='nasd-contract');reply['instrument'][field]=value
                with patch.object(reader,'call',return_value=reply),self.assertRaisesRegex(T.TBankError,code):
                    T.future_metadata(reader,{'uid':'nasd-contract','ticker':'NAZ6'})

    def test_closed_bar_validation_rejects_future_invalid_ohlc_and_forming_candles(self):
        now=T.utcnow();base={'time':T.iso(now-timedelta(minutes=1)), 'is_complete':True,
            'open':{'units':'12'},'high':{'units':'13'},'low':{'units':'11'},'close':{'units':'12'},'volume':'20'}
        items=[base,base,{**base,'is_complete':False},{**base,'time':T.iso(now+timedelta(days=1))},
               {**base,'high':{'units':'1'}},{**base,'volume':'-1'},
               {**base,'candle_source':'CANDLE_SOURCE_DEALER_WEEKEND'}]
        self.assertEqual(len(T.closed_candles(items,now)),1)

    def test_three_day_ohlcv_has_fixed_boundaries_no_current_bucket_and_no_initial_partial(self):
        width=3*86400
        epoch=datetime(2026,1,10,tzinfo=timezone.utc).timestamp()
        start=datetime.fromtimestamp(int(epoch//width)*width,timezone.utc)
        daily=[{'time':T.iso(start+timedelta(days=i)),'open':10+i,'high':12+i,'low':9+i,
                'close':11+i,'volume_lots':i+1} for i in range(8)]
        result=T.three_day_candles(daily,start+timedelta(days=8))
        self.assertEqual(len(result),2)
        self.assertEqual(result[0],{'time':T.iso(start),'open':10,'high':14,'low':9,'close':13,
                                   'volume_lots':6,'source_bar_count':3})
        trimmed=T.three_day_candles(daily[1:],start+timedelta(days=8))
        self.assertEqual(len(trimmed),1)
        self.assertEqual(trimmed[0]['time'],T.iso(start+timedelta(days=3)))

    def test_no_candle_is_created_for_dates_missing_from_native_daily_history(self):
        width=3*86400;start=int(datetime(2026,1,10,tzinfo=timezone.utc).timestamp()//width)*width
        daily=[{'time':T.iso(datetime.fromtimestamp(start+i*86400,timezone.utc)),
            'open':10,'high':12,'low':9,'close':11,'volume_lots':10} for i in (0,2,6)]
        result=T.three_day_candles(daily,datetime.fromtimestamp(start+9*86400,timezone.utc))
        self.assertEqual(len(result),2)
        self.assertEqual(result[0]['volume_lots'],20)
        self.assertEqual(result[0]['source_bar_count'],2)

    def test_stale_download_is_reported_even_when_candles_remain_cached(self):
        c=self.connection();c.refresh()
        c.candles[('NQF','1m')]['loaded_at']=T.iso(T.utcnow()-timedelta(minutes=4))
        self.assertEqual(c.candle_snapshot('NQF','1m')['status'],'STALE')
        self.assertEqual(c.status()['timeframes']['NQF']['1m']['status'],'STALE')


class HttpPrivacyTests(unittest.TestCase):
    def test_public_and_private_routes_use_the_actual_http_handler(self):
        import httpx
        import veritas_intelligence as app
        c=T.TBankConnection({'TBANK_API_TOKEN':'test-secret'})
        c.reader=FakeReader()
        c.refresh()
        with patch.object(T,'connection',c), patch.dict('os.environ',{
            'VERITAS_APP_AUTH_TOKEN':'app-secret','VERITAS_AUTOMATION_TOKEN':''}):
            server=ThreadingHTTPServer(('127.0.0.1',0),app.H)
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            base='http://127.0.0.1:'+str(server.server_port)
            try:
                with httpx.Client(base_url=base,trust_env=False) as client:
                    public=client.get('/api/v1/integrations/tbank')
                    self.assertEqual(public.status_code,200)
                    self.assertNotIn('private-account',public.text)
                    for kind in ('accounts','portfolio'):
                        path='/api/v1/integrations/tbank/'+kind
                        self.assertEqual(client.get(path).status_code,403)
                        self.assertEqual(client.get(path,headers={'X-Veritas-Token':'test-secret'}).status_code,403)
                        response=client.get(path,headers={'X-Veritas-Token':'app-secret'})
                        self.assertEqual(response.status_code,200)
                        self.assertIn('private-account',response.text)
                    page=client.get('/integrations/tbank')
                    self.assertEqual(page.status_code,200)
                    self.assertIn('Соединение установлено',page.text)
                    self.assertNotIn('test-secret',page.text)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__=='__main__':
    unittest.main()
