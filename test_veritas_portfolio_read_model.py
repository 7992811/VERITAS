"""Regression checks for current quantities, ledger balances and read-only API."""
import ast
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import Mock

import veritas_currency_portfolio as Currency
import veritas_portfolio_read_model as RM


NAMES = ('Impulse', 'Aggressive', 'Champion', 'Challenger', 'Currency')
NOW = datetime(2026, 10, 7, 10, 48, tzinfo=timezone.utc)


def base(name='Currency', **changes):
    return dict(name=name, initial_nav_rub=10000. if name=='Currency' else 1000000.,
                realized_pnl_rub=0., fees_rub=0., funding_rub=0., high_water_nav_rub=10000.,
                benchmark_nav_rub=10000., last_usdrub=100., last_ruonia=16., **changes)


def short_position():
    return dict(portfolio_name='Currency', asset='CNYRUBF', direction='SHORT',
                units=783.9666027, avg_entry_price=12.756141918, last_price=12.743,
                target_fraction=1., stop_price=12.78, active_trade_id='observed-currency-short',
                opened_at=NOW-timedelta(hours=1), updated_at=NOW-timedelta(minutes=20), payload={})


class PortfolioReadModelTests(unittest.TestCase):
    def empty_books(self):
        rows=[dict(name=name, positions=[], positions_checked_at=NOW.isoformat(),
                   positions_status='COMPLETE', accounting_base=base(name), gross_leverage=0.) for name in NAMES]
        return dict(status='OK', positions_complete=True, portfolios=rows)

    def test_new_short_revalues_old_nav_history_without_double_charging_costs(self):
        b=base(); b.update(realized_pnl_rub=21., fees_rub=4.0002, funding_rub=3.)
        position=short_position()
        old=dict(observed_at='2026-10-07T09:37:09Z', nav_rub=10000., gross_leverage=0.)
        original=dict(status='OK',portfolios=[dict(name='Currency',positions=[position],latest=old,
                                                  nav_rub=10000.,gross_leverage=0.,net_exposure=0.,
                                                  cash_equivalent_fraction=1.)])
        result=RM.revalue_report(original,bases={'Currency':b},checked_at=NOW.isoformat(),required_names=['Currency'])
        p=result['portfolios'][0]
        uplift=position['units']*(position['avg_entry_price']-position['last_price'])
        expected=10000.+21.-4.0002-3.+uplift
        self.assertAlmostEqual(p['nav_rub'],expected)
        self.assertAlmostEqual(p['nav_usd'],expected/100.)
        self.assertAlmostEqual(p['unrealized_pnl_rub'],uplift)
        self.assertAlmostEqual(p['gross_leverage'],position['units']*position['last_price']/expected)
        self.assertAlmostEqual(p['net_exposure'],-p['gross_leverage'])
        self.assertAlmostEqual(p['cash_equivalent_fraction'],max(0.,1.-p['gross_leverage']))
        self.assertEqual(p['latest'],old)
        self.assertEqual(p['positions_checked_at'],NOW.isoformat())
        self.assertTrue(result['accounting_complete'])
        self.assertEqual(original['portfolios'][0]['nav_rub'],10000.)

    def test_last_position_closed_produces_zero_exposure_and_booked_balance(self):
        b=base(); b.update(realized_pnl_rub=120.,fees_rub=8.,funding_rub=5.,high_water_nav_rub=10200.)
        report={'portfolios':[{'name':'Currency','positions':[],'latest':{'nav_rub':9900.,'gross_leverage':1.}}]}
        p=RM.revalue_report(report,bases={'Currency':b},checked_at=NOW.isoformat())['portfolios'][0]
        self.assertEqual(p['nav_rub'],10107.)
        self.assertEqual(p['gross_leverage'],0.)
        self.assertEqual(p['net_exposure'],0.)
        self.assertEqual(p['cash_equivalent_fraction'],1.)
        self.assertAlmostEqual(p['drawdown_pct'],100.*(1.-10107./10200.))

    def test_unknown_funding_and_missing_book_are_not_zero_balance_evidence(self):
        b=base(); b['funding_rub']=None
        report={'portfolios':[{'name':'Currency','positions':[]}]}
        result=RM.revalue_report(report,bases={'Currency':b},checked_at=NOW.isoformat())
        self.assertEqual(result['status'],'PARTIAL')
        self.assertIsNone(result['portfolios'][0]['nav_rub'])
        self.assertEqual(result['portfolios'][0]['positions_status'],'COMPLETE')
        missing=RM.revalue_report(report,bases={},checked_at=NOW.isoformat())
        self.assertFalse(missing['positions_complete'])
        self.assertEqual(missing['portfolios'][0]['positions_status'],'UNAVAILABLE')
        self.assertIsNone(missing['portfolios'][0]['gross_leverage'])

    def test_all_five_books_are_required_even_when_other_books_have_positions(self):
        report=self.empty_books()
        self.assertTrue(RM.memory_complete(report,NAMES,now=NOW))
        report['portfolios'].pop()
        report['portfolios'][0]['positions']=[short_position()]
        report['portfolios'][0]['gross_leverage']=1.
        self.assertFalse(RM.memory_complete(report,NAMES,now=NOW))

    def test_open_positions_require_accounts_from_same_sql_snapshot(self):
        report=self.empty_books()
        report['portfolios'][-1].update(positions=[short_position()],gross_leverage=1.)
        self.assertFalse(RM.memory_complete(report,NAMES,now=NOW))
        closed=self.empty_books()
        closed['portfolios'][-1]['positions_changed_at']=NOW.isoformat()
        self.assertFalse(RM.memory_complete(closed,NAMES,now=NOW))

    def test_quote_marks_and_new_cache_time_cannot_rejuvenate_old_quantities(self):
        report=self.empty_books()
        report['portfolios'][-1]['positions_checked_at']=(NOW-timedelta(seconds=16)).isoformat()
        report['portfolios'][-1]['last_mark_at']=NOW.isoformat()
        self.assertFalse(RM.memory_complete(report,NAMES,now=NOW))
        self.assertFalse(RM.snapshot_fresh(report,now=NOW))
        report['portfolios'][-1]['positions_checked_at']=(NOW+timedelta(seconds=1)).isoformat()
        self.assertFalse(RM.snapshot_fresh(report,now=NOW))


class QueryResult:
    def __init__(self,rows=()): self.rows=deepcopy(list(rows))
    def fetchall(self): return deepcopy(self.rows)


class ReadSnapshotDB:
    def __init__(self):
        self.queries=[]; self.transactions=0; self.connections=0
        self.bases=[base(name) for name in NAMES]
        self.bases[-1]['fees_rub']=4.0002
        self.position=short_position()
        self.accounts={self.position['active_trade_id']:{'status':'OPEN','fees_rub':4.0002}}
    @contextmanager
    def connect(self):
        self.connections+=1
        yield self
    @contextmanager
    def transaction(self):
        self.transactions+=1
        try: yield self
        finally: self.transactions-=1
    def execute(self,sql,args=()):
        self.queries.append(' '.join(sql.split()))
        if not self.transactions: raise AssertionError('Read outside snapshot transaction')
        if sql.startswith('SET TRANSACTION'): return QueryResult()
        if sql.startswith('SELECT name,initial_nav'): return QueryResult(self.bases)
        if sql.startswith('SELECT DISTINCT ON'):
            return QueryResult([dict(portfolio_name='Currency',observed_at=NOW-timedelta(hours=2),
                                     nav_rub=10000.,gross_leverage=0.,payload={})])
        if sql.startswith('SELECT pp.portfolio_name'): return QueryResult([self.position])
        if sql.startswith('SELECT portfolio_name,COUNT'): return QueryResult()
        raise AssertionError('Unexpected or non-read-only SQL: '+sql[:100])
    def load_accounts(self,conn,ids,include_entry_notional=False):
        if conn is not self or not self.transactions: raise AssertionError('Accounts outside quantity snapshot')
        return deepcopy(self.accounts)


class FastPortfolioAPITests(unittest.TestCase):
    def exercise_api(self, invalidate_during_read=False):
        tree=ast.parse(Path(__file__).with_name('veritas_intelligence.py').read_text())
        fn=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='_v90r25_portfolios_fast')
        db=ReadSnapshotDB()
        def enrich(report,connection,*,preloaded_accounts=None):
            self.assertEqual(preloaded_accounts,db.accounts)
            self.assertEqual(db.transactions,0)
            if invalidate_during_read:
                ns['_v90r25_pf_cache'].update(at=0.,value=None,revision=1)
            return deepcopy(report)
        view=SimpleNamespace(enrich_positions=Mock(side_effect=enrich),VPP=SimpleNamespace(load_accounts=db.load_accounts))
        live={'status':'OK','portfolios':[{'name':name,'positions':[],'gross_leverage':0.} for name in NAMES[:-1]]}
        ns=dict(_v90r25_pf_lock=threading.Lock(),_v90r25_pf_cache={'at':0.,'value':None},
                lock=threading.Lock(),last_cycle={'portfolio_autopilot':live},time=time,datetime=datetime,
                timezone=timezone,math=math,V90_CANONICAL_PORTFOLIOS=NAMES,pg_enabled=lambda:True,
                pg_connect=db.connect,VTV=view,VP=SimpleNamespace(VCP=Currency,POLICIES={}),
                VX=SimpleNamespace(VC=SimpleNamespace(COMMISSION_RATE=.0004)),
                CLOSED_METRICS_SQL='0 AS diagnostic',closed_trade_metrics=lambda *_:{})
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'<read-only-portfolio-api>','exec'),ns)
        out=ns['_v90r25_portfolios_fast']()
        cur=next(p for p in out['portfolios'] if p['name']=='Currency')
        self.assertEqual(db.connections,1)
        self.assertEqual(db.queries[0],'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        self.assertTrue(all(q.startswith(('SELECT','SET TRANSACTION')) for q in db.queries))
        self.assertEqual(len(cur['positions']),1)
        self.assertGreater(cur['gross_leverage'],0.)
        self.assertLess(cur['net_exposure'],0.)
        uplift=db.position['units']*(db.position['avg_entry_price']-db.position['last_price'])
        self.assertAlmostEqual(cur['nav_rub'],10000.-4.0002+uplift)
        self.assertEqual(out['api_source'],'fast_sql_enriched')
        self.assertTrue(out['positions_complete'])
        self.assertTrue(out['accounting_complete'])
        return ns,out

    def test_missing_currency_forces_complete_sql_and_uses_matching_accounts(self):
        ns,out=self.exercise_api()
        self.assertEqual(ns['_v90r25_pf_cache']['value'],out)

    def test_read_started_before_fill_cannot_repopulate_invalidated_cache(self):
        ns,_=self.exercise_api(invalidate_during_read=True)
        self.assertIsNone(ns['_v90r25_pf_cache']['value'])
        self.assertEqual(ns['_v90r25_pf_cache']['revision'],1)


if __name__=='__main__':
    unittest.main()
