"""An assessed exit books the selected source/quote and exactly its closed units."""
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import veritas_costs as VC
import veritas_portfolio as P
import veritas_portfolio_runtime as R
import veritas_position_guard as G
import veritas_price_source as S


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


if __name__=='__main__':
    unittest.main()
