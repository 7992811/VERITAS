"""R46 borrows only its just-read book snapshot, then releases it before core."""
import ast
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import gc
import io
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

import veritas_portfolio as VP
import veritas_portfolio_runtime as R
import veritas_position_guard as G
from veritas_book_lock import PriorityRLock
import test_veritas_profit_exit_authority as AUTHORITY


def r46_wrapper(namespace, legacy=False):
    path=Path(__file__).with_name('veritas_portfolio_runtime.py')
    node=next(node for node in ast.parse(path.read_text()).body
              if isinstance(node,ast.FunctionDef) and node.name=='_step_one'
              and any(isinstance(call,ast.Call) and isinstance(call.func,ast.Name)
                      and call.func.id=='_v90r46_base_step_one' for call in ast.walk(node)))
    if legacy:
        # Before this optimization the harvest made its own identical SELECT.
        for call in ast.walk(node):
            if (isinstance(call,ast.Call) and isinstance(call.func,ast.Name)
                    and call.func.id=='_v90r46_giveback_harvest'):
                call.keywords=[item for item in call.keywords if item.arg!='positions']
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),namespace)
    return namespace['_step_one']


class CountingConnection(AUTHORITY.Connection):
    def __init__(self,trade,position):
        super().__init__(trade,position)
        self.position_reads=0

    def execute(self,sql,args=None):
        if sql.startswith('SELECT * FROM paper_positions'):
            self.position_reads+=1
        return super().execute(sql,args)


class R46HarvestBorrowingTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime.now(timezone.utc).isoformat()
        self.trade={'trade_id':'T','gross_pnl_rub':0.,'fees_rub':40.,
                    'funding_rub':0.,'max_fraction':.1}

    def run_harvest(self,position,price,borrowed,trade=None):
        c=CountingConnection(self.trade if trade is None else trade,position)
        quote={'asset':'ETH','price':price,'observed_at':self.now,
               'source_gate_pass':True,'source_names':{'primary':'Binance spot'}}
        with patch.object(G,'quote_for_position',return_value=quote), \
                patch.object(VP,'CANONICAL_ACCOUNTING_CLOSE_OR_REDUCE',return_value=.05) as close, \
                redirect_stdout(io.StringIO()):
            kwargs={'positions':[position]} if borrowed else {}
            result=R._v90r46_giveback_harvest(c,{},'Champion',{'ETH':price},1e6,self.now,**kwargs)
        return result,c.writes,[deepcopy(call.args[1:]) for call in close.call_args_list],c.position_reads

    def test_borrowed_rows_preserve_authority_calls_patches_and_inputs(self):
        for direction,price in (('LONG',100.4),('SHORT',99.6)):
            for mode in ('approved','cost_blocked','done','unverified','no_giveback','invalid_units'):
                with self.subTest(direction=direction,mode=mode):
                    position={'asset':'ETH','direction':direction,'units':1000.,
                              'avg_entry_price':100.,'active_trade_id':'T','stop_price':98.,
                              'payload':{'mfe_pct':.8}}
                    trade=dict(self.trade)
                    if mode=='cost_blocked':trade['funding_rub']=2000.
                    if mode=='done':position['payload']['r46_giveback_harvest_done']=True
                    if mode=='unverified':position['payload']['data_integrity_status']='SOURCE_UNVERIFIED'
                    if mode=='no_giveback':position['payload']['mfe_pct']=.4
                    if mode=='invalid_units':position['units']=0.
                    original=deepcopy(position)
                    legacy=self.run_harvest(position,price,False,trade)
                    borrowed=self.run_harvest(position,price,True,trade)
                    self.assertEqual(borrowed[:3],legacy[:3])
                    self.assertEqual((legacy[3],borrowed[3]),(1,0))
                    self.assertEqual(len(borrowed[2]),int(mode=='approved'))
                    self.assertEqual(position,original)

    def test_explicit_empty_positions_does_not_fall_back_to_sql(self):
        c=CountingConnection(self.trade,{'asset':'ETH'})
        self.assertEqual(R._v90r46_giveback_harvest(c,{},'Champion',{},1e6,self.now,positions=[]),[])
        self.assertEqual(c.position_reads,0)

    def test_default_call_retains_read_failure_behavior(self):
        c=SimpleNamespace(execute=lambda *args:(_ for _ in ()).throw(RuntimeError('read failed')))
        self.assertEqual(R._v90r46_giveback_harvest(c,{},'Champion',{},1e6,self.now),[])


class Evidence(dict):
    """Only weak references outside the selected snapshot."""


class BookConnection:
    def __init__(self,book_lock):
        self.book_lock=book_lock
        self.book={'initial_nav_rub':1e6,'realized_pnl_rub':0.,'fees_rub':0.,'funding_rub':0.}
        self.position={}
        self.position_reads=0
        self.references=[]
        self.trace=[]
        self.writes=[]

    def rows(self):
        payload=Evidence(deepcopy(self.position['payload']))
        self.references.append(weakref.ref(payload))
        return [dict(self.position,payload=payload)]

    def execute(self,sql,args=None):
        if self.book_lock._owner!=threading.get_ident():
            raise AssertionError('book read/write without ownership')
        if sql.startswith('SELECT * FROM paper_portfolios'):
            self.trace.append('read_book')
            return SimpleNamespace(fetchone=lambda:dict(self.book))
        if sql.startswith('SELECT * FROM paper_positions'):
            self.position_reads+=1
            self.trace.append('read_positions')
            return SimpleNamespace(fetchall=self.rows)
        if sql.startswith('UPDATE paper_positions'):
            self.position['payload'].update(json.loads(args[0]))
        elif not sql.startswith('UPDATE paper_trades'):
            raise AssertionError('unexpected SQL: '+sql)
        self.writes.append((sql,args))
        return None


class R46WrapperBorrowingTests(unittest.TestCase):
    def run_passes(self,legacy):
        ownership=PriorityRLock();c=BookConnection(ownership)
        closes=[];core=[];released=[];mark_reads=[]
        def close(connection,book,name,position,price,target,nav,stamp,reason):
            self.assertIs(connection,c)
            self.assertEqual(ownership._owner,threading.get_ident())
            closes.append((position['active_trade_id'],position['units'],target,nav,reason))
            c.position['units']=position['units']*.7
            return .05
        def mark(book,positions,prices):
            before=deepcopy((book,positions));trace=list(c.trace);writes=list(c.writes)
            result=VP._mark_nav(book,positions,prices)
            self.assertEqual((book,positions),before)
            self.assertEqual((c.trace,c.writes),(trace,writes))
            mark_reads.append(c.position_reads)
            return result
        def delegate(*args):
            gc.collect()
            released.append(all(ref() is None for ref in c.references))
            # The downstream core must still see committed-in-this-transaction
            # harvest changes via its own fresh fetch, not the borrowed rows.
            book,positions=VP._portfolio_rows(c,'Champion')
            core.append((positions[0]['active_trade_id'],positions[0]['units'],
                         positions[0]['payload']['r46_giveback_harvest_done']))
            return {'status':'core_complete'}
        namespace={'_portfolio_rows':VP._portfolio_rows,'_mark_nav':mark,
                   '_v90j_update_excursions':lambda *args,**kwargs:None,
                   '_v90r46_mark_trend_hold':lambda *args,**kwargs:None,
                   '_v90r46_giveback_harvest':R._v90r46_giveback_harvest,
                   '_v90r46_base_step_one':delegate}
        step=r46_wrapper(namespace,legacy)
        with patch.object(R,'canonical_close_or_reduce',new=close), \
                patch.object(G,'quote_for_position',new=lambda *args,**kwargs:{}),redirect_stdout(io.StringIO()):
            for index,units in enumerate((1000.,1200.),start=1):
                with ownership:
                    c.position={'asset':'ETH','direction':'LONG','units':units,
                        'avg_entry_price':100.,'active_trade_id':'T'+str(index),
                        'payload':{'mfe_pct':.8,'entry_event_snapshot':{'proof':'bar;'*20000}}}
                    self.assertEqual(step(c,'Champion',{},[],{'ETH':100.4},0.,0.,
                                          '2026-10-07T18:00:00Z',.0004),{'status':'core_complete'})
        return {'closes':closes,'core':core,'writes':c.writes,'released':released,
                'position_reads':c.position_reads,'reads_at_mark':mark_reads}

    def test_one_read_saved_per_pass_with_fresh_core_reads_and_bounded_lifetime(self):
        legacy=self.run_passes(True);borrowed=self.run_passes(False)
        for key in ('closes','core','writes','released'):
            self.assertEqual(borrowed[key],legacy[key],key)
        self.assertEqual(borrowed['released'],[True,True])
        self.assertEqual([item[:2] for item in borrowed['closes']],[('T1',1000.),('T2',1200.)])
        self.assertEqual(borrowed['core'],[('T1',700.,True),('T2',840.,True)])
        self.assertEqual((legacy['position_reads'],borrowed['position_reads']),(6,4))
        self.assertEqual(borrowed['reads_at_mark'],[1,3])


if __name__=='__main__':unittest.main()
