"""Protective fills invalidate readers that captured the previous position book.

Execute the real guard loop and SQL refresh function with an in-memory database.
The position SELECT pauses logically at a deterministic callback: the protective
pass closes the position, then the older repeatable-read result finishes. No
provider, real database, worker thread or trading account is used.
"""
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

import veritas_position_guard as G
import veritas_price_source as S


class Result:
    def __init__(self, rows=()):
        self.rows = deepcopy(list(rows))

    def fetchall(self):
        return deepcopy(self.rows)


class SnapshotDatabase:
    def __init__(self):
        now = datetime.now(timezone.utc)
        self.positions = [dict(portfolio_name='Aggressive', asset='BRENT',
            direction='LONG', units=10., avg_entry_price=101., last_price=100.,
            active_trade_id='held-brent', target_fraction=.5, stop_price=100.,
            opened_at=now-timedelta(minutes=20), updated_at=now, trade_horizon='5m',
            payload={'price_source_lock':S.brent_feed_pin_identity()})]
        self.base = dict(name='Aggressive', initial_nav_rub=10000.,
            realized_pnl_rub=0., fees_rub=0., funding_rub=0.,
            benchmark_nav_rub=10000., high_water_nav_rub=10000.,
            last_ruonia=16., last_usdrub=100., last_mark_at=now)
        self.on_position_read = None
        self.reads = 0
        self.snapshot = None

    @contextmanager
    def connect(self):
        yield self

    @contextmanager
    def transaction(self):
        self.snapshot = deepcopy(self.positions)
        try:
            yield self
        finally:
            self.snapshot = None

    def execute(self, sql, args=()):
        if self.snapshot is None:
            raise AssertionError('Read escaped its quantity snapshot')
        if sql.startswith(('SET TRANSACTION', 'SET LOCAL')):
            return Result()
        if sql.startswith('SELECT name,initial_nav'):
            return Result([self.base])
        if sql.startswith('SELECT DISTINCT ON'):
            return Result()
        if 'FROM paper_positions pp' in sql:
            rows = deepcopy(self.snapshot)
            self.reads += 1
            callback, self.on_position_read = self.on_position_read, None
            if callback:
                callback()
            return Result(rows)
        if sql.startswith('SELECT portfolio_name,COUNT'):
            return Result()
        raise AssertionError('Unexpected or non-read-only SQL: '+sql[:100])

    def accounts(self, connection, ids, include_entry_notional=False, include_payload=True):
        if connection is not self or self.snapshot is None:
            raise AssertionError('Costs escaped the quantity snapshot')
        return {tid:{'status':'OPEN', 'fees_rub':0., 'funding_rub':0.} for tid in ids}


class FinishedGuard(BaseException):
    pass


class ProtectiveCacheRevisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse(Path(G.__file__).read_text())
        start = next(node for node in tree.body
                     if isinstance(node, ast.FunctionDef) and node.name == 'start')
        loop = next(node for node in start.body
                    if isinstance(node, ast.FunctionDef) and node.name == 'loop')
        cls.guard_code = compile(ast.Module(body=[loop], type_ignores=[]), G.__file__, 'exec')
        path = Path(G.__file__).with_name('veritas_intelligence.py')
        tree = ast.parse(path.read_text())
        refresh = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                       and node.name == '_v90r25_portfolios_refresh')
        cls.refresh_code = compile(ast.Module(body=[refresh], type_ignores=[]), str(path), 'exec')

    def setup_reader(self, revision=7):
        self.db = SnapshotDatabase()
        self.live = {'portfolio_autopilot':{'status':'PARTIAL', 'portfolios':[
            {'name':'Aggressive', 'positions':deepcopy(self.db.positions)}]}, 'other':'keep'}
        self.cache = {'at':0., 'value':{'prior':'snapshot'}}
        if revision is not None:
            self.cache['revision'] = revision
        self.shared = dict(_v90r25_pf_lock=threading.Lock(),
            _v90r25_pf_cache=self.cache, lock=threading.Lock(), last_cycle=self.live)
        self.reader = dict(self.shared, time=time, datetime=datetime, timezone=timezone,
            math=math, V90_CANONICAL_PORTFOLIOS=('Aggressive',),
            pg_enabled=lambda:True, pg_connect=self.db.connect,
            VTV=SimpleNamespace(enrich_positions=lambda report, *_args, **_kw:deepcopy(report),
                                VPP=SimpleNamespace(load_accounts=self.db.accounts)),
            VP=SimpleNamespace(VCP=SimpleNamespace(decorate_report=lambda report:report), POLICIES={}),
            VX=SimpleNamespace(VC=SimpleNamespace(COMMISSION_RATE=.0004)),
            CLOSED_METRICS_SQL='0 AS diagnostic', closed_trade_metrics=lambda *_:{})
        exec(self.refresh_code, self.reader)

    def run_guard_once(self, *, changes=True, failure=False):
        database = self.db
        class GuardConnection:
            def __enter__(self):
                return self
            def __exit__(self, *unused):
                pass
            def execute(self, sql):
                if sql != G.QUOTE_POSITION_SQL:
                    raise AssertionError('Unexpected protective quote read')
                return Result(database.positions)
        def protective(_vp, _connect, quotes, *, timing=None):
            self.assertEqual(quotes, {'BRENT':quote})
            if failure:
                raise RuntimeError('protective transaction rolled back')
            if not changes:
                return []
            database.positions = []
            return [{'asset':'BRENT', 'trade_id':'held-brent', 'reason':'STOP'}]
        def stop(_seconds):
            raise FinishedGuard()
        quote = dict(price=99.5, observed_at=datetime.now(timezone.utc).isoformat(),
                     source='ProFinance', raw_label='Brent oil', raw_ticker='brent', instrument_id='27')
        state = {'status':'STARTING', 'last_changes':[]}
        ns = dict(self.shared, VP=object(), pg_connect=GuardConnection,
            _v90r23_trade_lock=threading.Lock(),
            _v90r23_trade_cache={'at':40., 'value':{'prior':'trades'}}, emit=lambda *_a, **_kw:None)
        namespace = dict(ns=ns, _state=state, snapshot=lambda:dict(state),
            time=SimpleNamespace(monotonic=time.monotonic, sleep=stop),
            datetime=datetime, timezone=timezone, QUOTE_POSITION_SQL=G.QUOTE_POSITION_SQL,
            refresh_position_quotes=lambda *_:None, quote_for_position=lambda _:deepcopy(quote),
            run_protective_pass=protective, market_state=lambda _:{'market_open':True},
            expected_exchange_session_open=lambda *_:True)
        exec(self.guard_code, namespace)
        with self.assertRaises(FinishedGuard):
            namespace['loop']()
        self.assertEqual(state['status'], 'ERROR' if failure else 'OK', state)
        return ns

    def test_protective_close_retires_read_started_before_the_fill(self):
        for initial in (None, 7):
            with self.subTest(initial_revision=initial):
                self.setup_reader(revision=initial)
                self.db.on_position_read = self.run_guard_once
                old = self.reader['_v90r25_portfolios_refresh']()
                self.assertEqual(len(old['portfolios'][0]['positions']), 1)
                self.assertEqual(self.db.positions, [])
                self.assertEqual(self.cache.get('revision'), (initial or 0)+1)
                self.assertIsNone(self.cache['value'],
                    'A pre-close SELECT republished the closed position into the shared cache')
                self.assertEqual(self.cache['at'], 0.)
                self.assertEqual(self.live, {'portfolio_autopilot':{}, 'other':'keep'})
                current = self.reader['_v90r25_portfolios_refresh']()
                self.assertEqual(current['portfolios'][0]['positions'], [])
                self.assertEqual(current['portfolios'][0]['gross_leverage'], 0.)
                self.assertEqual(self.cache['value'], current)
                self.assertEqual(self.db.reads, 2)

    def test_new_revision_is_published_only_after_previous_live_book_is_retired(self):
        self.setup_reader()
        observations = []
        live = self.live
        class ObservedCache(dict):
            def update(self, *args, **kwargs):
                super().update(*args, **kwargs)
                observations.append((self.get('revision'), deepcopy(live['portfolio_autopilot'])))
        cache = ObservedCache(self.cache)
        self.shared['_v90r25_pf_cache'] = cache
        self.run_guard_once()
        self.assertEqual(observations, [(8, {})])

    def test_no_change_or_failed_transaction_keeps_existing_cache_and_revision(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                self.setup_reader()
                before_cache, before_live = deepcopy(self.cache), deepcopy(self.live)
                self.run_guard_once(changes=False, failure=failure)
                self.assertEqual(self.cache, before_cache)
                self.assertEqual(self.live, before_live)
                self.assertEqual(len(self.db.positions), 1)


if __name__ == '__main__':
    unittest.main()
