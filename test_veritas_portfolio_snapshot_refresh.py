"""A delayed read cannot block HTTP, duplicate I/O, or revive an older book."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import gc
import threading
import time
from types import SimpleNamespace
import unittest
import weakref
from unittest.mock import Mock, patch

import veritas_portfolio_read_model as R


def book(*, age=0, trade_id='test-open-trade'):
    stamp=(datetime.now(timezone.utc)-timedelta(seconds=age)).isoformat()
    return {'status':'OK','positions_complete':True,'accounting_complete':True,
            'positions_checked_at':stamp,'portfolios':[
                {'name':'Currency','positions_checked_at':stamp,
                 'positions':[{'active_trade_id':trade_id,'units':10,
                               'last_price':12.5,'last_mark_at':stamp}]}]}


class BoundedSnapshotTests(unittest.TestCase):
    def test_first_reader_and_followers_return_while_one_database_read_remains_pending(self):
        for previous in (None,book(age=90)):
            with self.subTest(warm=previous is not None):
                started=threading.Event(); release=threading.Event(); finished=threading.Event()
                cache={'at':time.time()-90,'value':deepcopy(previous),'revision':4}
                before=deepcopy(cache); lock=threading.Lock(); calls=[]
                def refresh():
                    calls.append('read'); started.set()
                    try:
                        if not release.wait(5): raise AssertionError('Reader was not released')
                        return book()
                    finally: finished.set()
                try:
                    start=time.monotonic()
                    first=R.portfolio_snapshot_read(refresh,cache,lock,wait_seconds=.02)
                    self.assertLess(time.monotonic()-start,1.)
                    self.assertTrue(started.wait(1))
                    self.assertFalse(finished.is_set())
                    replies=[first]+[R.portfolio_snapshot_read(refresh,cache,lock) for _ in range(4)]
                    self.assertEqual(calls,['read'])
                    self.assertEqual(cache,before)
                    for reply in replies:
                        self.assertFalse(reply['positions_complete'])
                        self.assertFalse(reply['accounting_complete'])
                        self.assertEqual(reply['refresh_status'],'UPDATING')
                        if previous is None:
                            self.assertEqual(reply['portfolios'],[])
                            self.assertNotIn('positions_checked_at',reply)
                        else:
                            self.assertEqual(reply['positions_checked_at'],previous['positions_checked_at'])
                            self.assertEqual(reply['portfolios'],previous['portfolios'])
                finally:
                    release.set()
                    self.assertTrue(finished.wait(2))
                    # The callback's completion precedes releasing its owner.
                    self.assertTrue(R._snapshot_refresh_lock.acquire(timeout=2))
                    R._snapshot_refresh_lock.release()

    def test_completed_background_read_is_available_without_another_database_query(self):
        cache={'at':0.,'value':None,'revision':2}; lock=threading.Lock()
        started=threading.Event(); release=threading.Event(); published=threading.Event()
        complete=book(); calls=[]
        def refresh():
            calls.append('read'); started.set()
            if not release.wait(5): raise AssertionError('Reader was not released')
            with lock: cache.update(at=time.time(),value=complete)
            published.set()
            return complete
        try:
            pending=R.portfolio_snapshot_read(refresh,cache,lock,wait_seconds=.01)
            self.assertFalse(pending['positions_complete'])
            self.assertTrue(started.wait(1)); release.set()
            self.assertTrue(published.wait(2))
            actual=R.portfolio_snapshot_read(refresh,cache,lock)
            self.assertTrue(actual['positions_complete'])
            self.assertTrue(actual['accounting_complete'])
            self.assertEqual(actual['portfolios'],complete['portfolios'])
            self.assertEqual(actual['positions_checked_at'],complete['positions_checked_at'])
            self.assertEqual(actual['api_source'],'memory_cache')
            self.assertEqual(calls,['read'])
        finally:
            release.set()
            self.assertTrue(R._snapshot_refresh_lock.acquire(timeout=2))
            R._snapshot_refresh_lock.release()

    def test_current_complete_cache_does_not_start_or_wait_for_a_reader(self):
        cached=book(); cache={'at':time.time(),'value':cached}; lock=threading.Lock()
        refresh=Mock(side_effect=AssertionError('No SQL read is needed'))
        self.assertTrue(R._snapshot_refresh_lock.acquire(timeout=1))
        try:
            out=R.portfolio_snapshot_read(refresh,cache,lock)
        finally: R._snapshot_refresh_lock.release()
        refresh.assert_not_called()
        self.assertEqual(out,dict(cached,api_source='memory_cache'))
        self.assertNotIn('api_source',cached)

    def test_recent_cache_publication_does_not_rejuvenate_old_book_observations(self):
        for age in (90,-60):
            with self.subTest(age=age):
                cached=book(age=age); cache={'at':time.time(),'value':cached}; lock=threading.Lock()
                refresh=Mock(return_value={'status':'OK','fresh_read':True})
                out=R.portfolio_snapshot_read(refresh,cache,lock)
                refresh.assert_called_once_with()
                self.assertTrue(out['fresh_read'])
                self.assertEqual(cache['value'],cached)

    def test_completed_slow_reads_reach_next_ui_poll_with_original_observation_time(self):
        # No real-time sleeps: advance the wall clock from SQL start to finish
        # and to the dashboard's subsequent 15-second poll.
        for duration,next_poll in ((2.,15.1),(34.,45.)):
            with self.subTest(read_seconds=duration,next_poll_seconds=next_poll):
                wall=[datetime(2001,2,3,9,tzinfo=timezone.utc).timestamp()]
                initial=wall[0]
                class SnapshotClock(datetime):
                    @classmethod
                    def now(cls,tz=None):
                        return datetime.fromtimestamp(wall[0],tz=tz)

                cache={'at':0.,'value':None,'revision':4}; lock=threading.Lock()
                release=threading.Semaphore(0); started=threading.Event(); calls=[]
                def refresh():
                    stamp=SnapshotClock.now(timezone.utc).isoformat()
                    calls.append(stamp)
                    started.set()
                    if not release.acquire(timeout=5):
                        raise AssertionError('Reader was not released')
                    value=book()
                    value['positions_checked_at']=stamp
                    value['portfolios'][0]['positions_checked_at']=stamp
                    with lock: cache.update(at=wall[0],value=value)
                    return value

                with patch.object(R,'datetime',SnapshotClock),patch.object(R.time,'time',lambda:wall[0]):
                    try:
                        first=R.portfolio_snapshot_read(refresh,cache,lock,wait_seconds=.01)
                        self.assertFalse(first['positions_complete'])
                        wall[0]=initial+duration
                        release.release()
                        self.assertTrue(R._snapshot_refresh_lock.acquire(timeout=2))
                        R._snapshot_refresh_lock.release()
                        self.assertEqual(cache['completed_read_revision'],4)
                        self.assertEqual(cache['completed_read_at'],wall[0])

                        wall[0]=initial+next_poll
                        started.clear()
                        out=R.portfolio_snapshot_read(refresh,cache,lock)
                        self.assertTrue(started.wait(1))
                        followers=[R.portfolio_snapshot_read(refresh,cache,lock) for _ in range(3)]
                        for reply in [out]+followers:
                            self.assertTrue(reply['positions_complete'])
                            self.assertTrue(reply['accounting_complete'])
                            self.assertTrue(reply['snapshot_stale'])
                            self.assertEqual(reply['api_source'],'completed_snapshot')
                            self.assertEqual(reply['positions_checked_at'],calls[0])
                            self.assertEqual(reply['portfolios'][0]['positions_checked_at'],calls[0])
                        self.assertEqual(len(calls),2)
                    finally:
                        release.release()
                        self.assertTrue(R._snapshot_refresh_lock.acquire(timeout=2))
                        R._snapshot_refresh_lock.release()

    def test_copied_completed_snapshot_is_rejected_if_fill_invalidates_before_return(self):
        for existing_worker in (True,False):
            with self.subTest(existing_worker=existing_worker):
                published_at=time.time(); previous=book(age=60)
                cache={'at':published_at,'value':previous,'revision':4,
                       'completed_read_revision':4,'completed_read_at':published_at}
                lock=threading.Lock(); ownership=threading.Lock()
                release=threading.Event(); acquisitions=[]
                def acquire(*args,**kwargs):
                    obtained=ownership.acquire(*args,**kwargs)
                    if not acquisitions:
                        # A committed close retires the copied candidate after
                        # its cache read but before either return branch.
                        with lock: cache.update(at=0.,value=None,revision=5)
                    acquisitions.append(obtained)
                    return obtained
                guard=SimpleNamespace(acquire=acquire,release=ownership.release)
                def callback():
                    if not release.wait(5): raise AssertionError('Reader was not released')
                    return book()
                refresh=Mock(side_effect=callback)
                if existing_worker: self.assertTrue(ownership.acquire())
                with patch.object(R,'_snapshot_refresh_lock',guard):
                    try:
                        out=R.portfolio_snapshot_read(refresh,cache,lock,wait_seconds=.01)
                        self.assertFalse(out['positions_complete'])
                        self.assertFalse(out['accounting_complete'])
                        self.assertEqual(out['portfolios'],[])
                        if existing_worker: refresh.assert_not_called()
                        else: refresh.assert_called_once_with()
                    finally:
                        release.set()
                        if existing_worker: ownership.release()
                        else:
                            self.assertTrue(ownership.acquire(timeout=2))
                            ownership.release()

    def test_newer_fill_rejects_the_waiting_http_result_as_well_as_cache_publication(self):
        old=book(); cache={'at':0.,'value':None,'revision':8}; lock=threading.Lock()
        def refresh():
            # The SQL read began before the protective close committed.
            with lock: cache.update(at=0.,value=None,revision=9)
            return old
        out=R.portfolio_snapshot_read(refresh,cache,lock)
        self.assertFalse(out['positions_complete'])
        self.assertFalse(out['accounting_complete'])
        self.assertEqual(out['portfolios'],[])
        self.assertEqual(out['reason'],'PORTFOLIO_SNAPSHOT_CHANGED_DURING_REFRESH')
        self.assertIsNone(cache['value'])
        self.assertEqual(cache['revision'],9)

    def test_worker_creation_failure_releases_ownership(self):
        cache={'at':0.,'value':None}; lock=threading.Lock(); refresh=Mock()
        with patch.object(R.threading,'Thread',side_effect=RuntimeError('test resource exhaustion')):
            with self.assertRaises(RuntimeError): R.portfolio_snapshot_read(refresh,cache,lock)
        refresh.assert_not_called()
        retry=Mock(return_value={'status':'OK'})
        self.assertEqual(R.portfolio_snapshot_read(retry,cache,lock),{'status':'OK'})
        retry.assert_called_once_with()

    def test_late_database_failure_preserves_cache_and_allows_a_later_retry(self):
        cached=book(age=90); cache={'at':0.,'value':cached,'revision':1}; lock=threading.Lock()
        release=threading.Event(); failed=threading.Event()
        def refresh():
            try:
                if not release.wait(5): raise AssertionError('Reader was not released')
                raise TimeoutError('test database timeout')
            finally: failed.set()
        try:
            with self.assertLogs(R.__name__,level='WARNING') as logs:
                out=R.portfolio_snapshot_read(refresh,cache,lock,wait_seconds=.01)
                self.assertFalse(out['positions_complete'])
                release.set(); self.assertTrue(failed.wait(2))
                self.assertTrue(R._snapshot_refresh_lock.acquire(timeout=2))
                R._snapshot_refresh_lock.release()
            self.assertIn('TimeoutError',logs.output[0])
            self.assertNotIn('test database timeout',logs.output[0])
            self.assertEqual(cache['value'],cached)
            retry=Mock(return_value={'status':'OK','recovered':True})
            self.assertTrue(R.portfolio_snapshot_read(retry,cache,lock)['recovered'])
            retry.assert_called_once_with()
        finally: release.set()

    def test_late_chained_failure_releases_query_payload_without_cyclic_collection(self):
        class QueryPayload:
            pass

        cache={'at':0.,'value':None}; lock=threading.Lock()
        release=threading.Event(); observed=[]
        def refresh():
            query_payload=QueryPayload()
            observed.append(weakref.ref(query_payload))
            try:
                raise ValueError('inner database error')
            except ValueError as original:
                if not release.wait(5): raise AssertionError('Reader was not released')
                raise TimeoutError('outer database error') from original

        # A later GC pass must not be required to release a failed reader's
        # decoded rows. Chained exceptions retain both stacks unless cleared.
        gc_was_enabled=gc.isenabled()
        gc.disable()
        try:
            with self.assertLogs(R.__name__,level='WARNING'):
                out=R.portfolio_snapshot_read(refresh,cache,lock,wait_seconds=.01)
                self.assertFalse(out['positions_complete'])
                self.assertEqual(len(observed),1)
                self.assertIsNotNone(observed[0]())
                release.set()
                self.assertTrue(R._snapshot_refresh_lock.acquire(timeout=2))
                R._snapshot_refresh_lock.release()
            self.assertIsNone(observed[0]())
        finally:
            release.set()
            if gc_was_enabled: gc.enable()


if __name__=='__main__':
    unittest.main()
