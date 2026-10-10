"""An assessed exit books the selected source/quote and exactly its closed units."""
import ast
import copy
import inspect
import json
import unittest
import weakref
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import veritas_costs as VC
import veritas_portfolio as P
import veritas_portfolio_runtime as R
import veritas_position_guard as G
import veritas_price_source as S
import veritas_execution_snapshot as ES


class Result:
    def __init__(self, row=None):
        self.row=copy.deepcopy(row)
    def fetchone(self):
        return copy.deepcopy(self.row)


class ExitAccountingDB:
    """Only the paper ledger is simulated; guard, fills and arithmetic are real."""
    def __init__(self, position, trade):
        self.position=copy.deepcopy(position)
        self.trade=copy.deepcopy(trade)
        self.orders=[]
        self.writes=[]
    def execute(self, sql, args=()):
        query=" ".join(sql.split())
        if query.startswith("SELECT 1 AS ok FROM paper_orders"):
            return Result()
        if query.startswith("SELECT * FROM paper_trades"):
            return Result(self.trade)
        if query.startswith("SELECT COALESCE(SUM(notional_rub),0) AS exit_notional_rub"):
            exits=[o for o in self.orders if o[1]==args[0] and o[4] in ('SELL','BUY_TO_COVER')]
            notional=sum(float(o[6]) for o in exits)
            units=sum(float(o[6])/float(o[5]) for o in exits if float(o[5])>0)
            return Result({'exit_notional_rub':notional,'exit_units':units,'exit_fill_count':len(exits)})
        self.writes.append((query,args))
        if query.startswith("UPDATE paper_portfolios SET realized_pnl_rub="):
            pass
        elif query.startswith("UPDATE paper_trades SET gross_pnl_rub="):
            self.trade['gross_pnl_rub']+=args[0]
            self.trade['fees_rub']+=args[1]
        elif query.startswith("INSERT INTO paper_orders"):
            self.orders.append(args)
        elif query.startswith("UPDATE paper_trades SET payload=payload ||"):
            self.trade['payload'].update(json.loads(args[0]))
        elif query.startswith("UPDATE paper_trades SET payload=%s::jsonb"):
            self.trade['payload']=json.loads(args[0])
        elif query.startswith("UPDATE paper_trades SET closed_at="):
            self.trade.update(closed_at=args[0],avg_exit_price=args[1],net_pnl_rub=args[2],
                              return_on_entry_nav=args[3],profitable=args[4],
                              meaningful_win=args[5],status=args[6])
            self.trade['payload'].update(json.loads(args[7]))
        elif query.startswith("DELETE FROM paper_positions"):
            self.position=None
        elif query.startswith("UPDATE paper_positions SET units="):
            self.position.update(units=args[0],last_price=args[1],target_fraction=args[2],updated_at=args[3])
        else:
            raise AssertionError("Unexpected paper accounting query: "+query)
        return Result()


class ExitSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,10,7,10,0,tzinfo=timezone.utc)
        for cache in (G._quotes,G._source_quotes,G._market_state):
            self.enterContext(patch.dict(cache,{},clear=True))

    def quote(self,price,*,source='Binance spot',at=None):
        return {'asset':'ETH','price':price,'best_bid':price-.01,'best_ask':price+.01,
                'observed_at':(at or self.now).isoformat(),'source_gate_pass':True,
                'market_open':True,'source_names':{'primary':source}}

    def position(self,direction='LONG',units=50):
        return {'portfolio_name':'Champion','asset':'ETH','direction':direction,
                'units':units,'avg_entry_price':100.,'active_trade_id':'ETH_EXIT_TEST',
                'stop_price':95. if direction=='LONG' else 105.,
                'opened_at':(self.now-timedelta(hours=2)).isoformat(),
                'payload':{'price_source_lock':S.identity('ETH',self.quote(100)),
                           'entry_execution_observed_at':(self.now-timedelta(hours=2)).isoformat()}}

    def trade(self,position,**changes):
        trade={'trade_id':position['active_trade_id'],'direction':position['direction'],
               'avg_entry_price':100.,'gross_pnl_rub':-10.,'fees_rub':2.,'funding_rub':.3,
               'status':'OPEN','max_fraction':.05,'payload':{'entry_nav_rub':1e6}}
        trade.update(changes)
        return trade

    def test_profitable_assessment_keeps_its_quote_and_fill_after_cache_changes(self):
        real_assessment=G.profit_exit_assessment
        for direction,selected_price,new_price in (('LONG',102.,90.),('SHORT',98.,110.)):
            with self.subTest(direction=direction):
                position=self.position(direction)
                selected=self.quote(selected_price)
                later=self.quote(new_price,at=self.now+timedelta(seconds=1))
                G.publish_quote('ETH',selected)
                db=ExitAccountingDB(position,self.trade(position))
                assessments=[]
                def assess_then_refresh(z,q,trade,nav,commission=VC.COMMISSION_RATE):
                    assessment=real_assessment(z,q,trade,nav,commission)
                    self.assertTrue(assessment['eligible'],assessment)
                    assessments.append(assessment)
                    G.publish_quote('ETH',later)
                    return assessment
                with patch.object(G,'profit_exit_assessment',side_effect=assess_then_refresh):
                    R.canonical_close_or_reduce(db,{},'Champion',position,selected_price,0.,
                        1e6,self.now+timedelta(seconds=1),'TAKE_PROFIT_FULL_MIN_POSITION_R17')
                self.assertEqual(len(assessments),1)
                self.assertEqual(len(db.orders),1)
                order=db.orders[0];audit=json.loads(order[10])
                self.assertAlmostEqual(order[5],assessments[0]['fill_price'])
                self.assertEqual(audit['reference_price'],selected_price)
                self.assertEqual(audit['market_observed_at'],selected['observed_at'])
                self.assertAlmostEqual(db.trade['net_pnl_rub'],assessments[0]['whole_cycle_net_pnl_rub'])
                self.assertEqual(G.quote_for_position(position,now=self.now+timedelta(seconds=1))['price'],new_price)
                # Clear the later quote before running the other direction.
                for cache in (G._quotes,G._source_quotes,G._market_state):
                    cache.clear()

    def test_invalid_selected_pinned_quote_blocks_without_cache_or_scalar_fallback(self):
        position=self.position()
        G.publish_quote('ETH',self.quote(103.))
        invalid=[{},dict(self.quote(103.),source_gate_pass=False),
                 self.quote(103.,at=self.now-timedelta(minutes=10)),
                 self.quote(103.,source='Coinbase spot'),
                 {k:v for k,v in self.quote(103.).items() if k!='price'},
                 dict(self.quote(103.),price=float('nan'))]
        for quote in invalid:
            with self.subTest(quote=quote):
                selected=dict(position,_execution_quote=quote)
                db=ExitAccountingDB(position,self.trade(position))
                with patch.object(G,'quote_for_position',side_effect=AssertionError('Selected quote was refreshed')):
                    self.assertEqual(G.exit_execution_quote(selected,self.now),{})
                    with self.assertRaisesRegex(ValueError,'EXIT_EXECUTION_QUOTE_REQUIRED'):
                        G.exit_fill(selected,999.,.05,self.now)
                    result=P._v90j_base_close_or_reduce(db,{},'Champion',selected,999.,0.,
                                                       1e6,self.now,'HARD_THESIS_INVALIDATION')
                self.assertEqual(result,0)
                self.assertEqual(db.writes,[])

    def test_published_stale_book_cannot_become_fresh_through_cache_or_entry_refresh(self):
        from test_veritas_timeframe_policy import valid_row
        for field in ('orderbook_observed_at','book_observed_at','orderbook_ts'):
            with self.subTest(field=field):
                quote=self.quote(2700.)
                quote[field]=(self.now-timedelta(minutes=20)).isoformat()
                position=self.position('SHORT')
                G.publish_quote('ETH',quote)
                self.assertEqual(G.quote_for_position(position,now=self.now),{})
                self.assertEqual(G.exit_execution_quote(position,self.now),{})
                db=ExitAccountingDB(position,self.trade(position))
                self.assertEqual(R.canonical_close_or_reduce(
                    db,{},'Champion',position,2700.,0.,1e6,self.now,'HARD_THESIS_INVALIDATION'),0.)
                self.assertEqual(db.writes,[])
                row=valid_row(asset='ETH',horizon='1h',direction='SHORT',price=2700.,
                              now=self.now,source='Binance spot')
                refreshed=G.refresh_execution_row(dict(row,_execution_quote=quote),now=self.now)
                self.assertEqual(S.quote_from_row(refreshed)[field],quote[field])
                gate=G.VX.entry_gate(refreshed,2700.,'SHORT',.05,now=self.now)
                self.assertFalse(gate['eligible'])
                self.assertIn('EXECUTION_ORDERBOOK_STALE',gate['blockers'])

    def test_quote_cache_is_bounded_and_detached_from_publisher_and_consumer(self):
        quote=self.quote(2700.)
        quote.update(orderbook_observed_at=self.now.isoformat(),history=[{'close':2690.}])
        expected=copy.deepcopy(quote)
        G.publish_quote('ETH',quote)
        self.assertNotIn('history',G._quotes['ETH'])
        self.assertNotIn('history',next(iter(G._source_quotes.values())))
        quote['source_names']['primary']='Coinbase spot'
        quote['best_bid']=1.
        quote['orderbook_observed_at']=(self.now-timedelta(hours=1)).isoformat()
        selected=G.quote_for_position(self.position(),now=self.now)
        for key in ('source_names','best_bid','orderbook_observed_at'):
            self.assertEqual(selected[key],expected[key])
        selected['source_names']['primary']='ANOTHER_SOURCE'
        selected['best_bid']=2.
        again=G.quote_for_position(self.position(),now=self.now)
        self.assertEqual(again['source_names'],expected['source_names'])
        self.assertEqual(again['best_bid'],expected['best_bid'])

    def test_publish_projects_before_copying_and_does_not_retain_unused_signal_graphs(self):
        class UnusedSignalGraph:
            def __deepcopy__(self, memo):
                raise AssertionError('A quote copied the complete signal/market graph')
        proof=UnusedSignalGraph(); retained=weakref.ref(proof)
        quote=self.quote(2700.)
        quote.update(features=proof,history=proof,trade_plan=proof,timeframe_entry_context=proof,
                     paper_eligible=False,production_eligible=False,orders_enabled=False,
                     market_observed_at=self.now.isoformat(),orderbook_observed_at=self.now.isoformat())
        expected=ES.quote_snapshot(quote)
        G.publish_quote('ETH',quote)
        selected=G.quote_for_position(self.position(),now=self.now)
        self.assertEqual(ES.quote_snapshot(selected),expected)
        for key in ('paper_eligible','production_eligible','orders_enabled'):
            self.assertIs(selected[key],False)
            self.assertIs(next(iter(G._source_quotes.values()))[key],False)
        self.assertEqual(selected['market_observed_at'],quote['market_observed_at'])
        self.assertIs(quote['features'],proof, 'Publication must not modify its caller')
        for copied in (selected,G._quotes['ETH'],*G._source_quotes.values()):
            self.assertFalse({'features','history','trade_plan','timeframe_entry_context'} & set(copied))
        del quote,proof
        self.assertIsNone(retained(), 'Quote caches retained an unused evidence graph')

    def test_candidate_and_frozen_quotes_are_projected_before_return_copy(self):
        class UnusedSignalGraph:
            def __deepcopy__(self, memo):
                raise AssertionError('Selected quote copied unused evidence')
        for frozen in (False,True):
            with self.subTest(frozen=frozen):
                quote=self.quote(102.)
                quote.update(features=UnusedSignalGraph(),paper_eligible=False,
                             production_eligible=False,orders_enabled=False)
                position=self.position()
                if frozen:
                    position.update(_execution_quote=quote,_execution_quote_frozen=True)
                selected=G.quote_for_position(position,None if frozen else quote,self.now)
                self.assertEqual(ES.quote_snapshot(selected),ES.quote_snapshot(quote))
                self.assertNotIn('features',selected)
                self.assertIs(selected['paper_eligible'],False)
                selected['source_names']['primary']='MUTATED'
                self.assertEqual(quote['source_names']['primary'],'Binance spot')

    def test_tbank_quotes_keep_both_pinned_instrument_uids_when_asset_quote_rolls(self):
        position=dict(self.position(),asset='CNYRUBF')
        old=dict(self.quote(12.75),asset='CNYRUBF',contract={'instrument_uid':'held-uid'},
                 source_names={'primary':'TBANK_GRPC CNYRUBF'},paper_eligible=False)
        new=dict(old,price=12.99,contract={'instrument_uid':'other-uid'},
                 observed_at=(self.now+timedelta(seconds=1)).isoformat())
        position['payload']['price_source_lock']=S.identity('CNYRUBF',old)
        G.publish_quote('CNYRUBF',old)
        G.publish_quote('CNYRUBF',new)
        self.assertEqual(set(G._source_quotes),
            {('CNYRUBF','TBANK_GRPC:CNYRUBF','held-uid'),('CNYRUBF','TBANK_GRPC:CNYRUBF','other-uid')})
        with patch('veritas_direct_cny.quote',return_value={}):
            held=G.quote_for_position(position,now=self.now+timedelta(seconds=1))
            other=copy.deepcopy(position)
            other['payload']['price_source_lock']=S.identity('CNYRUBF',new)
            rolled=G.quote_for_position(other,now=self.now+timedelta(seconds=1))
        self.assertEqual(held['price'],12.75)
        self.assertEqual(rolled['price'],12.99)
        self.assertIs(held['paper_eligible'],False)
        self.assertEqual(S.identity('CNYRUBF',held),S.position_identity(position))

    def test_ineligible_market_state_still_updates_without_copying_evidence_or_replacing_quote(self):
        class UnusedSignalGraph:
            def __deepcopy__(self, memo):
                raise AssertionError('Rejected market observation copied unused evidence')
        original=self.quote(102.)
        G.publish_quote('ETH',original)
        rejected=dict(self.quote(500.,at=self.now+timedelta(seconds=1)),
                      source_gate_pass=False,market_open=False,features=UnusedSignalGraph())
        G.publish_quote('ETH',rejected)
        self.assertEqual(G.market_state('ETH'),
                         {'observed_at':rejected['observed_at'],'market_open':False,'source_gate_pass':False})
        self.assertEqual(G.quote_for_position(self.position(),now=self.now)['price'],102.)
        self.assertEqual(len(G._source_quotes),1)

    def test_projection_preserves_real_exit_assessment_fill_fees_and_recorded_quote_hash(self):
        for direction,price in (('LONG',101.),('SHORT',99.)):
            with self.subTest(direction=direction):
                quote=self.quote(price)
                quote.update(history=[{'close':1.}]*100,
                             features={'unused':'complete signal proof'},
                             orderbook_observed_at=self.now.isoformat())
                position=self.position(direction,units=1000)
                trade=self.trade(position,gross_pnl_rub=-125.,fees_rub=80.,funding_rub=15.,max_fraction=.1)
                runs=[]
                for legacy in (True,False):
                    for cache in (G._quotes,G._source_quotes,G._market_state):
                        cache.clear()
                    # The original cache/selector deep-copied the whole input.
                    # Freeze that behavior while running the same real math.
                    with (patch.object(G,'_detached_quote',side_effect=copy.deepcopy) if legacy else nullcontext()):
                        G.publish_quote('ETH',quote)
                        selected=G.quote_for_position(position,now=self.now)
                        assessment=G.profit_exit_assessment(position,selected,trade,1e6)
                        fill=G.exit_fill(dict(position,_execution_quote=selected),price,.05,self.now)
                        db=ExitAccountingDB(position,trade)
                        result=R.canonical_close_or_reduce(db,{},'Champion',position,price,.05,1e6,
                                                          self.now,'DYNAMIC_PARTIAL_PROFIT')
                        runs.append((assessment,fill,result,db.orders,db.trade,db.position,
                                     ES._digest(ES.quote_snapshot(selected))))
                self.assertTrue(runs[0][0]['eligible'])
                self.assertEqual(len(runs[0][3]),1)
                self.assertEqual(runs[1],runs[0])

    def test_summary_harvest_keeps_book_clock_for_later_entry_and_exit_checks(self):
        from test_veritas_timeframe_policy import valid_row
        for field in ('orderbook_observed_at','book_observed_at','orderbook_ts'):
            with self.subTest(field=field):
                row=valid_row(asset='ETH',horizon='1h',direction='SHORT',price=2700.,
                              now=self.now,source='Binance spot')
                row.update(self.quote(2700.))
                row[field]=(self.now-timedelta(seconds=20)).isoformat()
                with patch.object(G,'datetime',wraps=datetime) as clock, \
                     patch.object(G,'_entry_namespace',{}), \
                     patch.object(G,'fetch_guard_quote',side_effect=AssertionError('Unexpected network refresh')):
                    clock.now.return_value=self.now
                    refreshed=G.refresh_entry_quotes([row])[0]
                selected=S.quote_from_row(refreshed)
                self.assertEqual(selected[field],row[field])
                fresh=G.VX.entry_gate(refreshed,2700.,'SHORT',.05,now=self.now)
                self.assertTrue(fresh['eligible'],fresh)
                later=self.now+timedelta(seconds=20)
                self.assertTrue(G.quote_gate(selected['observed_at'],now=later,
                                            execution=True,asset='ETH')['eligible'])
                stale=G.VX.entry_gate(refreshed,2700.,'SHORT',.05,now=later)
                self.assertFalse(stale['eligible'])
                self.assertIn('EXECUTION_ORDERBOOK_STALE',stale['blockers'])
                exit_at=self.now+timedelta(seconds=281)
                self.assertTrue(G.quote_gate(selected['observed_at'],now=exit_at,
                                            protective=True)['eligible'])
                self.assertEqual(G.exit_execution_quote(
                    dict(self.position('SHORT'),_execution_quote=selected),exit_at),{})

    def test_delayed_quote_cannot_mutate_protective_path_before_strict_exit_check(self):
        position=self.position('SHORT')
        quote=dict(self.quote(106.,at=self.now-timedelta(minutes=10)),
                   data_latency_class='DELAYED_RESEARCH')
        self.assertTrue(G.VX.paper_quote_time_gate(quote,now=self.now,protective=True)['eligible'])
        connection=MagicMock()
        connection.__enter__.return_value=connection
        connection.execute.return_value.fetchall.return_value=[position]
        book=MagicMock()
        with patch.object(G.VOP,'observe',wraps=G.VOP.observe) as observe:
            self.assertEqual(G.run_protective_pass(book,lambda:connection,{'ETH':quote},self.now),[])
        observe.assert_not_called()
        book._close_or_reduce.assert_not_called()
        writes=[call for call in connection.execute.call_args_list
                if call.args[0].lstrip().startswith(('UPDATE','INSERT','DELETE'))]
        self.assertEqual(writes,[])

    def test_partial_fees_cover_closed_units_and_whole_cycle_projection_reconciles(self):
        for direction,price in (('LONG',101.),('SHORT',99.)):
            with self.subTest(direction=direction):
                position=self.position(direction,units=1000)
                quote=self.quote(price)
                trade=self.trade(position,gross_pnl_rub=-125.,fees_rub=80.,funding_rub=15.,max_fraction=.1)
                before=G.profit_exit_assessment(position,quote,trade,1e6)
                self.assertTrue(before['eligible'],before)
                G.publish_quote('ETH',quote)
                db=ExitAccountingDB(position,trade)
                R.canonical_close_or_reduce(db,{},'Champion',position,price,.05,1e6,
                                            self.now,'DYNAMIC_PARTIAL_PROFIT')
                self.assertEqual(len(db.orders),1)
                order=db.orders[0]
                closed_units=1000-50000./price
                fill=before['fill_price']
                closed_gross=closed_units*(fill-100.)*(1 if direction=='LONG' else -1)
                closed_fee=closed_units*fill*VC.COMMISSION_RATE
                self.assertAlmostEqual(order[6],closed_units*fill)
                self.assertAlmostEqual(order[7],closed_fee)
                self.assertAlmostEqual(db.trade['gross_pnl_rub'],-125.+closed_gross)
                self.assertAlmostEqual(db.trade['fees_rub'],80.+closed_fee)
                self.assertEqual(db.trade['funding_rub'],15.)
                self.assertAlmostEqual(db.position['units'],50000./price)
                after=G.profit_exit_assessment(db.position,quote,db.trade,1e6)
                self.assertAlmostEqual(after['whole_cycle_net_pnl_rub'],before['whole_cycle_net_pnl_rub'])
                self.assertAlmostEqual(after['whole_cycle_net_pnl_rub'],
                    after['booked_cycle_net_pnl_rub']+after['remaining_net_before_paid_cycle_costs_rub'])
                for cache in (G._quotes,G._source_quotes,G._market_state):
                    cache.clear()


class ProtectiveLoopLifetimeTests(unittest.TestCase):
    """Run the actual nested loop without starting a thread or a live reader."""

    def run_loop(self, *, release=True, assets=('ETH',), changed=False,
                 failure=None, iterations=1):
        start=ast.parse(inspect.getsource(G.start)).body[0]
        loop=next(node for node in start.body
                  if isinstance(node,ast.FunctionDef) and node.name=='loop')
        if not release:
            # Negative control removes only the two lifetime releases. SQL,
            # quote preparation, counts, status and invalidation stay identical.
            class RetainOriginalSnapshots(ast.NodeTransformer):
                removed=0
                def visit_Assign(self,node):
                    names={item.id for item in node.targets if isinstance(item,ast.Name)}
                    if (isinstance(node.value,ast.Constant) and node.value.value is None
                            and names in ({'positions','z'},{'positions','z','c','q'})):
                        self.removed+=1
                        return ast.copy_location(ast.Pass(),node)
                    return node
            rewrite=RetainOriginalSnapshots()
            loop=rewrite.visit(loop)
            self.assertEqual(rewrite.removed,2)

        class WatchedPayload(dict):
            __slots__=('__weakref__',)
        class FinishedLoop(BaseException):
            pass

        refs=[]; connections=[]; reads=[]; passes=[]; sleeps=[]; events=[]; logs=[]
        moment=datetime(2026,10,7,15,0,tzinfo=timezone.utc)
        state={'status':'STARTING','interval_seconds':15,'last_changes':[{'previous':True}]}
        def alive():
            return sum(ref() is not None for ref in refs)
        def fail(stage):
            if failure==stage:
                raise RuntimeError('fixture '+stage)

        class Connection:
            def __enter__(self):
                events.append(('connection_enter',))
                return self
            def __exit__(self,*unused):
                events.append(('connection_closed',))
            def execute(self,sql):
                events.append(('sql',sql))
                return self
            def fetchall(self):
                rows=[]
                for asset in assets:
                    graph=WatchedPayload(history=[{'close':101.0}],source='original-proof')
                    payload=WatchedPayload(entry_decision_snapshot=graph)
                    refs.extend((weakref.ref(graph),weakref.ref(payload)))
                    rows.append({'asset':asset,'units':10.0,'payload':payload,
                                 'active_trade_id':None if asset=='NQ' else 'trade-'+asset})
                fail('read')
                return rows
        def connect():
            reads.append(alive())
            connection=Connection()
            connections.append(weakref.ref(connection))
            return connection
        def refresh(ns,positions):
            events.append(('refresh',tuple((z['asset'],z['units']) for z in positions)))
            self.assertGreaterEqual(alive(),2*len(assets))
            for z in positions:
                self.assertEqual(z['payload']['entry_decision_snapshot']['source'],'original-proof')
            fail('refresh')
        selected={'asset':'ETH','price':101.0,'observed_at':moment.isoformat(),
                  'source_names':{'primary':'Binance spot'},'source_gate_pass':True}
        def quote(position):
            events.append(('quote',position['asset']))
            fail('quote')
            return copy.deepcopy(selected) if position['asset']=='ETH' else {}
        book=object()
        changes=[{'portfolio':'Champion','asset':'ETH','reason':'existing-protective-result'}]
        def protective(vp,pg_connect,quotes,*,timing=None,eligible_trade_ids=None):
            self.assertIs(vp,book)
            self.assertIs(pg_connect,connect)
            passes.append(alive())
            events.append(('protective',copy.deepcopy(quotes)))
            fail('protective')
            return copy.deepcopy(changes) if changed else []
        def emit(event,**fields):
            logs.append((event,copy.deepcopy(fields)))
        def sleep(seconds):
            sleeps.append((alive(),sum(ref() is not None for ref in connections),seconds))
            if len(sleeps)>=iterations:
                raise FinishedLoop()
        class Clock:
            @classmethod
            def now(cls,tz):
                return moment
        ns={'VP':book,'pg_connect':connect,'emit':emit,
            '_v90r25_pf_lock':nullcontext(),'lock':nullcontext(),
            '_v90r23_trade_lock':nullcontext(),
            '_v90r25_pf_cache':{'at':55.0,'value':{'preserve':'positions'},'revision':7},
            '_v90r23_trade_cache':{'at':44.0,'value':{'preserve':'trades'}},
            'last_cycle':{'portfolio_autopilot':{'preserve':'live'},'other':'keep'}}
        protective_mutex=SimpleNamespace(
            reserve_protective_turn=lambda seconds: None,
            cancel_protective_turn=lambda: None)
        environment={'ns':ns,'_state':state,'snapshot':lambda:dict(state),
            '_mutex':protective_mutex,'PROTECTIVE_PRECLAIM_SECONDS':G.PROTECTIVE_PRECLAIM_SECONDS,
            'time':SimpleNamespace(monotonic=lambda:100.0,sleep=sleep),
            'datetime':Clock,'timezone':timezone,'refresh_position_quotes':refresh,
            'QUOTE_POSITION_SQL':G.QUOTE_POSITION_SQL,
            'quote_for_position':quote,'run_protective_pass':protective,
            'market_state':lambda asset:{'market_open':asset!='BTC'},
            'expected_exchange_session_open':lambda asset,now:asset!='GOLD'}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[loop],type_ignores=[])),
                     G.__file__,'exec'),environment)
        with self.assertRaises(FinishedLoop):
            environment['loop']()
        return {'reads':reads,'passes':passes,'sleeps':sleeps,'events':events,'logs':logs,
                'state':state,'portfolio_cache':ns['_v90r25_pf_cache'],
                'trade_cache':ns['_v90r23_trade_cache'],'last_cycle':ns['last_cycle']}

    def assert_behavior_unchanged(self,old,new):
        for field in ('events','logs','state','portfolio_cache','trade_cache','last_cycle'):
            self.assertEqual(new[field],old[field],field)
        self.assertTrue(all(live==0 for live in new['passes']))
        self.assertTrue(all(live==0 and connections==0 and seconds==15.0
                            for live,connections,seconds in new['sleeps']))

    def test_completed_quote_book_is_released_before_locked_pass_with_same_status_and_quotes(self):
        for assets,changed,status in ((('ETH',),False,'OK'),(('ETH',),True,'OK'),
                (('ETH','BTC','GOLD','NQ'),False,'DEGRADED'),
                (('BTC','GOLD'),False,'PAUSED_MARKET_CLOSED'),((),False,'OK')):
            with self.subTest(assets=assets,changed=changed):
                old=self.run_loop(release=False,assets=assets,changed=changed)
                new=self.run_loop(assets=assets,changed=changed)
                self.assert_behavior_unchanged(old,new)
                self.assertEqual(new['state']['status'],status)
                self.assertEqual(new['state']['open_positions'],len(assets))
                self.assertEqual(new['state']['quotes'],int('ETH' in assets))
                if assets:
                    self.assertEqual(old['passes'],[2*len(assets)])
                    self.assertEqual(old['sleeps'][0][0],2*len(assets))
                else:
                    self.assertEqual(new['passes'],[])
                if changed:
                    self.assertEqual(new['portfolio_cache'],{'at':0.0,'value':None,'revision':8})
                    self.assertEqual(new['trade_cache'],{'at':0.0,'value':None})
                    self.assertEqual(new['last_cycle'],{'portfolio_autopilot':{},'other':'keep'})
                    self.assertEqual(new['state']['last_changes'][0]['reason'],'existing-protective-result')
                else:
                    self.assertEqual(new['portfolio_cache']['at'],55.0)
                    self.assertEqual(new['state']['last_changes'],[{'previous':True}])
                if status=='DEGRADED':
                    self.assertEqual(new['state']['errors'],{'NQ':'PINNED_SOURCE_QUOTE_UNAVAILABLE'})
                    self.assertEqual(new['state']['paused'],
                                     {'trade-BTC':'MARKET_CLOSED','trade-GOLD':'MARKET_CLOSED'})

    def test_errors_release_full_book_and_closed_connection_before_sleep(self):
        for failure in ('read','refresh','quote','protective'):
            with self.subTest(failure=failure):
                old=self.run_loop(release=False,failure=failure)
                new=self.run_loop(failure=failure)
                self.assert_behavior_unchanged(old,new)
                self.assertEqual(new['state']['status'],'ERROR')
                self.assertEqual(new['state']['error'],'RuntimeError: fixture '+failure)
                self.assertEqual(new['logs'][0][0],'paper_protective_guard_error')
                self.assertEqual(old['sleeps'][0][1],1)
                if failure!='read':
                    self.assertEqual(old['sleeps'][0][0],2)

    def test_finished_iteration_cannot_retain_previous_book_during_next_read(self):
        old=self.run_loop(release=False,iterations=2)
        new=self.run_loop(iterations=2)
        self.assert_behavior_unchanged(old,new)
        self.assertEqual(old['reads'],[0,2])
        self.assertEqual(new['reads'],[0,0])
        self.assertEqual(old['passes'],[2,2])
        self.assertEqual(new['passes'],[0,0])


if __name__=='__main__':
    unittest.main()
