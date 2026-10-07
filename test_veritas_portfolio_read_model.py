"""Regression checks for current quantities, ledger balances and read-only API."""
import ast
from concurrent.futures import ThreadPoolExecutor
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
from urllib.parse import urlparse

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
        if sql.startswith(('SET TRANSACTION', 'SET LOCAL')): return QueryResult()
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
        functions=[node for node in tree.body if isinstance(node,ast.FunctionDef)
                   and node.name in ('_v90r25_portfolios_fast','_v90r25_portfolios_refresh')]
        self.assertEqual(len(functions),2)
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
        exec(compile(ast.Module(body=functions,type_ignores=[]),'<read-only-portfolio-api>','exec'),ns)
        out=ns['_v90r25_portfolios_fast']()
        cur=next(p for p in out['portfolios'] if p['name']=='Currency')
        self.assertEqual(db.connections,1)
        self.assertEqual(db.queries[:3],[
            'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY',
            "SET LOCAL lock_timeout = '1500ms'",
            "SET LOCAL statement_timeout = '10000ms'"])
        self.assertTrue(all(q.startswith(('SELECT','SET TRANSACTION','SET LOCAL')) for q in db.queries))
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


class SnapshotConcurrencyTests(unittest.TestCase):
    def test_concurrent_readers_return_without_second_refresh_or_rejuvenating_cache(self):
        old_snapshot=dict(status='OK',positions_complete=True,accounting_complete=True,
                          positions_checked_at=NOW.isoformat(),portfolios=[
                              dict(name='Currency',positions=[short_position()],
                                   positions_checked_at=NOW.isoformat())])
        for previous in (None,old_snapshot):
            with self.subTest(cached=previous is not None):
                entered=threading.Event(); release=threading.Event()
                cache=dict(at=123.,value=deepcopy(previous),revision=3)
                original=deepcopy(cache); cache_lock=threading.Lock(); calls=[]
                def refresh():
                    calls.append('read')
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError('Blocked refresh was not released')
                    return {'status':'OK','owner':True}
                def read():
                    return RM.portfolio_snapshot_read(refresh,cache,cache_lock)
                with ThreadPoolExecutor(max_workers=6) as pool:
                    owner=pool.submit(read)
                    try:
                        self.assertTrue(entered.wait(1))
                        readers=[pool.submit(read) for _ in range(5)]
                        replies=[reader.result(timeout=1) for reader in readers]
                        self.assertEqual(calls,['read'])
                        self.assertFalse(owner.done())
                        self.assertEqual(cache,original)
                        for reply in replies:
                            self.assertEqual(reply['refresh_status'],'UPDATING')
                            self.assertFalse(reply['positions_complete'])
                            self.assertFalse(reply['accounting_complete'])
                            if previous is None:
                                self.assertEqual(reply['status'],'UPDATING')
                                self.assertEqual(reply['api_source'],'refresh_in_progress')
                            else:
                                self.assertEqual(reply['status'],'PARTIAL')
                                self.assertEqual(reply['api_source'],'stale_cache')
                                self.assertEqual(reply['positions_checked_at'],NOW.isoformat())
                                self.assertEqual(reply['portfolios'],previous['portfolios'])
                    finally:
                        release.set()
                    self.assertEqual(owner.result(timeout=1),{'status':'OK','owner':True})

    def test_failed_refresh_releases_singleflight_for_next_request(self):
        cache={'at':0.,'value':None}; cache_lock=threading.Lock()
        failed=Mock(side_effect=TimeoutError('snapshot query canceled'))
        with self.assertRaises(TimeoutError):
            RM.portfolio_snapshot_read(failed,cache,cache_lock)
        retry=Mock(return_value={'status':'OK'})
        self.assertEqual(RM.portfolio_snapshot_read(retry,cache,cache_lock),{'status':'OK'})
        retry.assert_called_once_with()
        self.assertEqual(cache,{'at':0.,'value':None})


class StartupHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree=ast.parse(Path(__file__).with_name('veritas_intelligence.py').read_text())
        handler=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='H')
        cls.handler_code=compile(ast.Module(body=[handler],type_ignores=[]),'<startup-http>','exec')

    def request(self,path,ready=False):
        overview=Mock(return_value={'status':'OK','ready':True})
        portfolios=Mock(return_value={'status':'OK','portfolios':[]})
        ns=dict(BaseHTTPRequestHandler=object,_BOOTSTRAP_READY=ready,VERSION='test',
                SERVICE_ROLE='web',SERVICE_STARTED_AT=time.time()-1,time=time,rss_mb=lambda:123.,
                VR=SimpleNamespace(snapshot=lambda:{'product_version':'test'}),
                DASHBOARD_HTML='<html>dashboard</html>',urlparse=urlparse,
                product_overview=overview,_v90r25_portfolios_fast=portfolios)
        exec(self.handler_code,ns)
        handler=ns['H'].__new__(ns['H'])
        handler.path=path; handler.reply=Mock(); handler.reply_html=Mock()
        handler.do_GET()
        return handler,overview,portfolios

    def test_starting_api_never_reaches_database_backed_routes(self):
        for path in ('/api/v1/paper-portfolios','/api/v1/dashboard-bootstrap?view=signals',
                     '/api/v1/overview','/api/v1/portfolio-trades','/api/v1/history'):
            with self.subTest(path=path):
                handler,overview,portfolios=self.request(path)
                handler.reply.assert_called_once()
                body,code=handler.reply.call_args.args
                self.assertEqual(code,503)
                self.assertEqual(body['status'],'STARTING')
                self.assertFalse(body['bootstrap_ready'])
                self.assertFalse(body['positions_complete'])
                self.assertFalse(body['accounting_complete'])
                self.assertNotIn('portfolios',body)
                overview.assert_not_called(); portfolios.assert_not_called()

    def test_healthz_and_dashboard_remain_available_during_startup(self):
        handler,_,_=self.request('/healthz')
        handler.reply.assert_called_once()
        body=handler.reply.call_args.args[0]
        self.assertTrue(body['ok'])
        self.assertEqual(body['phase'],'STARTING')
        self.assertFalse(body['bootstrap_ready'])
        for path in ('/app','/app?tab=portfolios','/'):
            with self.subTest(path=path):
                handler,_,_=self.request(path)
                handler.reply_html.assert_called_once_with('<html>dashboard</html>')
                handler.reply.assert_not_called()

    def test_finished_bootstrap_resumes_normal_api_even_without_assuming_database_health(self):
        handler,overview,portfolios=self.request('/api/v1/overview',ready=True)
        overview.assert_called_once_with(); portfolios.assert_not_called()
        handler.reply.assert_called_once_with({'status':'OK','ready':True})
        handler,overview,portfolios=self.request('/api/v1/paper-portfolios',ready=True)
        portfolios.assert_called_once_with(); overview.assert_not_called()
        handler.reply.assert_called_once_with({'status':'OK','portfolios':[]})


class StartupStateEventTests(unittest.TestCase):
    def test_canonical_init_failure_is_not_reported_as_success(self):
        tree=ast.parse(Path(__file__).with_name('veritas_intelligence.py').read_text())
        main=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='main')
        block=next(node for node in ast.walk(main) if isinstance(node,ast.Try)
                   and any(isinstance(child,ast.Assign)
                           and any(isinstance(target,ast.Name) and target.id=='canonical_state'
                                   for target in child.targets) for child in node.body))
        code=compile(ast.Module(body=[block],type_ignores=[]),'<canonical-startup-event>','exec')
        for status in ('OK','ERROR','UNAVAILABLE','DEGRADED'):
            with self.subTest(status=status):
                emit=Mock(); ensure=Mock(return_value={'status':status,'count':5 if status=='OK' else 0})
                exec(code,dict(emit=emit,_v90r24_ensure_canonical_portfolios=ensure))
                ensure.assert_called_once_with()
                emit.assert_called_once()
                self.assertEqual(emit.call_args.args,('v90_live_state_ready',))
                self.assertEqual(emit.call_args.kwargs['status'],status)


if __name__=='__main__':
    unittest.main()
