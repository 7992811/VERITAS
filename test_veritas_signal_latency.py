"""The signal path consumes completed context; history work stays off-thread.

Only AST-selected helpers run. No service imports, providers or production DB.
"""
import ast
from contextlib import contextmanager
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).with_name('veritas_intelligence.py')
FUNCTIONS = {n.name:n for n in ast.parse(SOURCE.read_text()).body
             if isinstance(n,ast.FunctionDef)}


def load(names, namespace):
    module=ast.Module(body=[FUNCTIONS[name] for name in names],type_ignores=[])
    exec(compile(module,str(SOURCE),'exec'),namespace)
    return namespace


def board(probability=.40, mean=-.002):
    return {'status':'ok','items':[{'asset':'NQ','horizon':'5m','phase':'ONSET',
        'direction':'LONG','n':40,'posterior_continuation_rate':probability,
        'decayed_avg_signed_return':mean}]}


class SignalHistoryLatencyTests(unittest.TestCase):
    def namespace(self, loader, initial=None):
        threads=[]

        def thread_factory(*args,**kwargs):
            worker=threading.Thread(*args,**kwargs)
            threads.append(worker)
            return worker

        ns={'time':time,'threading':SimpleNamespace(Thread=thread_factory),
            'TREND_CASE_MIN_N':20,'ANALYTICS_CACHE_SECONDS':60.,
            '_v90r61_predecision_cache':{} if initial is None else {'trend_cases':(0.,initial)},
            '_v90r61_predecision_lock':threading.Lock(),
            '_v90r63_context_refresh_lock':threading.Lock(),
            '_v90r63_context_refresh_inflight':{},
            'trend_case_learning_board':loader,'emit':lambda *a,**k:None}
        load(('trend_case_multiplier','_v90r63_context_cached_or_background',
              '_v90r63_context_refresh'),ns)
        return ns,threads

    def test_blocked_history_does_not_hold_signal_and_keeps_last_penalty(self):
        started,release=threading.Event(),threading.Event()
        calls=[]

        def slow_history(limit):
            calls.append((limit,threading.get_ident()))
            started.set()
            if not release.wait(3):
                raise AssertionError('test failed to release history worker')
            return board(.60,.002)

        ns,threads=self.namespace(slow_history,board())
        try:
            # No timing threshold can accidentally allow a synchronous query:
            # the worker remains blocked until the assertions have completed.
            values=[ns['trend_case_multiplier']('NQ','5m','ONSET','LONG') for _ in range(30)]
            self.assertTrue(started.wait(1))
            self.assertEqual(values,[.70]*30)
            self.assertEqual(len(threads),1)
            self.assertEqual(len(calls),1)
            self.assertNotEqual(calls[0][1],threading.get_ident())
            self.assertFalse(release.is_set())
        finally:
            release.set()
            for worker in threads:worker.join(3)
        self.assertEqual(ns['trend_case_multiplier']('NQ','5m','ONSET','LONG'),1.)
        self.assertEqual(len(threads),1)

    def test_failed_refresh_cannot_remove_known_statistical_penalty(self):
        def unavailable(limit):
            raise RuntimeError('isolated history unavailable')
        ns,threads=self.namespace(unavailable,board())
        self.assertEqual(ns['trend_case_multiplier']('NQ','5m','ONSET','LONG'),.70)
        for worker in threads:worker.join(1)
        self.assertEqual(ns['_v90r61_predecision_cache']['trend_cases'][1],board())
        self.assertFalse(ns['_v90r63_context_refresh_inflight']['trend_cases'])

    def test_supplied_board_preserves_all_existing_weights_without_loading(self):
        def forbidden(limit):
            raise AssertionError('explicit context must not query history')
        ns,threads=self.namespace(forbidden)
        for probability,expected in ((.45,.70),(.49,.82),(.53,.92),(.60,1.)):
            self.assertEqual(ns['trend_case_multiplier']('NQ','5m','ONSET','LONG',
                board(probability,.001)),expected)
        self.assertEqual(ns['trend_case_multiplier']('NQ','5m','NONE','LONG'),1.)
        self.assertEqual(ns['trend_case_multiplier']('NQ','5m','ONSET','NO_TRADE'),1.)
        self.assertEqual(ns['trend_case_multiplier']('NQ','5m','ONSET','LONG',{}),1.)
        self.assertEqual(threads,[])


class MacroGateProjectionTests(unittest.TestCase):
    def gate(self, latest, *, quality=None, kill=False, unavailable=False):
        queries=[]
        @contextmanager
        def connect():
            class Connection:
                def execute(self,sql):
                    queries.append(sql)
                    if unavailable:raise RuntimeError('isolated DB unavailable')
                    return SimpleNamespace(fetchone=lambda:latest)
            yield Connection()
        ns={'pg_connect':connect,'pg_enabled':lambda:True,
            'pg_storage_status':lambda:{'ok':True},'time':time,
            'BACKTEST_METHOD_VERSION':'METHOD_TEST','KILL_SWITCH':False,
            'runtime_bool':lambda *a:kill,'data_quality_snapshot':lambda:quality or {},
            'now':lambda:'2026-10-07T06:30:00Z'}
        load(('research_activation_gate',),ns)
        return ns['research_activation_gate'](),queries

    def test_gate_reads_only_latest_method_and_status(self):
        result,queries=self.gate({'status':'ok','details':{'method_version':'METHOD_TEST'}})
        self.assertTrue(result['pass'])
        self.assertEqual(len(queries),1)
        self.assertIn('FROM backtest_runs',queries[0])
        self.assertIn('LIMIT 1',queries[0])
        self.assertNotIn('knowledge_backtest',queries[0])

    def test_missing_failed_stale_and_quality_failure_still_close_gate(self):
        for latest in (None,{'status':'error','details':{'method_version':'METHOD_TEST'}},
                       {'status':'ok','details':{'method_version':'OLD'}}):
            with self.subTest(latest=latest):
                self.assertFalse(self.gate(latest)[0]['pass'])
        clean={'status':'ok','details':{'method_version':'METHOD_TEST'}}
        self.assertFalse(self.gate(clean,quality={'critical_failures':['stale']})[0]['pass'])
        self.assertFalse(self.gate(clean,kill=True)[0]['pass'])
        self.assertFalse(self.gate(clean,unavailable=True)[0]['pass'])


if __name__=='__main__':unittest.main()
