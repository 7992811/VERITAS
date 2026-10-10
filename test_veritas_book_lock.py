"""Protection handoff, accounting lifetime and clocks; quote SQL has PostgreSQL tests."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from veritas_book_lock import PriorityRLock
import veritas_position_guard as G


class PriorityLockTests(unittest.TestCase):
    def wait_queued(self, lock, ordinary=0, protective=0):
        with lock._condition:
            self.assertTrue(lock._condition.wait_for(
                lambda: lock._ordinary_waiters == ordinary and
                lock._priority_waiters == protective, timeout=2))

    def test_waiting_guard_precedes_earlier_ordinary_waiter_after_commit(self):
        lock = PriorityRLock()
        order, failures = [], []
        guard_entered, guard_release = threading.Event(), threading.Event()
        lock.acquire()
        def normal():
            try:
                with lock:
                    order.append('portfolio')
            except BaseException as exc:
                failures.append(exc)
        def protective():
            try:
                self.assertTrue(lock.acquire(priority=True))
                try:
                    order.append('guard')
                    guard_entered.set()
                    self.assertTrue(guard_release.wait(2))
                finally:
                    lock.release()
            except BaseException as exc:
                failures.append(exc)
        ordinary = threading.Thread(target=normal)
        guard = threading.Thread(target=protective)
        ordinary.start()
        self.wait_queued(lock, ordinary=1)
        guard.start()
        self.wait_queued(lock, ordinary=1, protective=1)
        # A current owner may finish its nested canonical work even while
        # protection waits; no priority-induced reentry deadlock is allowed.
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()
        lock.release()
        try:
            self.assertTrue(guard_entered.wait(2))
            self.assertEqual(order, ['guard'])
            self.assertFalse(lock.acquire(blocking=False))
        finally:
            guard_release.set()
            ordinary.join(2)
            guard.join(2)
        self.assertFalse(ordinary.is_alive())
        self.assertFalse(guard.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(order, ['guard', 'portfolio'])

    def test_nonblocking_new_owner_cannot_barge_a_queued_guard(self):
        lock = PriorityRLock()
        # Exercise the free-lock/queued-waiter predicate without a scheduler
        # race; the end-to-end threaded handoff is tested above.
        with lock._condition:
            lock._priority_waiters = 1
        try:
            self.assertFalse(lock.acquire(blocking=False))
        finally:
            with lock._condition:
                lock._priority_waiters = 0
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_protective_preclaim_blocks_new_ordinary_and_is_consumed_by_guard(self):
        lock = PriorityRLock()
        lock.reserve_protective_turn(.5)
        result = []
        def ordinary():
            acquired = lock.acquire(blocking=False)
            result.append(acquired)
            if acquired:
                lock.release()
        thread = threading.Thread(target=ordinary)
        thread.start(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [False])
        self.assertEqual(lock.snapshot()['protective_reservations'], 1)

        self.assertTrue(lock.acquire(priority=True))
        self.assertEqual(lock.snapshot()['last_handoff_reason'], 'PROTECTIVE_PRECLAIM')
        self.assertEqual(lock.snapshot()['protective_reservations'], 0)
        lock.release()

        result.clear()
        thread = threading.Thread(target=ordinary)
        thread.start(); thread.join(2)
        self.assertEqual(result, [True])

    def test_protective_preclaim_cancel_wakes_waiting_ordinary(self):
        lock = PriorityRLock()
        lock.reserve_protective_turn(.5)
        entered = threading.Event()
        def ordinary():
            with lock:
                entered.set()
        thread = threading.Thread(target=ordinary)
        thread.start()
        self.wait_queued(lock, ordinary=1)
        self.assertFalse(entered.is_set())
        lock.cancel_protective_turn()
        self.assertTrue(entered.wait(2))
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(lock.snapshot()['protective_reservations'], 0)

    def test_protective_preclaim_expires_if_preparing_worker_disappears(self):
        lock = PriorityRLock()
        ready = threading.Event()
        def prepare_only():
            lock.reserve_protective_turn(.05)
            ready.set()
        thread = threading.Thread(target=prepare_only)
        thread.start(); thread.join(2)
        self.assertTrue(ready.is_set())
        self.assertEqual(lock.snapshot()['protective_reservations'], 1)
        threading.Event().wait(.08)
        result=[]
        def ordinary():
            acquired=lock.acquire(blocking=False)
            result.append(acquired)
            if acquired:
                lock.release()
        thread=threading.Thread(target=ordinary)
        thread.start(); thread.join(2)
        self.assertEqual(result,[True])
        self.assertEqual(lock.snapshot()['protective_reservations'],0)

    def test_protective_preclaim_never_interrupts_current_owner_reentry(self):
        lock = PriorityRLock()
        lock.acquire()
        reserved = threading.Event()
        release = threading.Event()
        def guard_prepare():
            lock.reserve_protective_turn(.5)
            reserved.set()
            release.wait(2)
            lock.cancel_protective_turn()
        thread = threading.Thread(target=guard_prepare)
        thread.start()
        self.assertTrue(reserved.wait(2))
        self.assertTrue(lock.acquire(blocking=False))
        self.assertEqual(lock.snapshot()['depth'], 2)
        lock.release()
        release.set(); thread.join(2)
        lock.release()
        self.assertFalse(thread.is_alive())

    def test_timeout_and_wrong_owner_leave_no_priority_reservation(self):
        lock = PriorityRLock()
        results = []
        with lock:
            def contender():
                results.append(lock.acquire(timeout=0, priority=True))
                try:
                    lock.release()
                except RuntimeError:
                    results.append('wrong-owner')
            thread = threading.Thread(target=contender)
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(results, [False, 'wrong-owner'])
        self.assertEqual(lock._priority_waiters, 0)
        with self.assertRaises(ValueError):
            lock.acquire(blocking=False, timeout=0)
        with self.assertRaises(ValueError):
            lock.acquire(timeout=-2)


class TransactionTimingTests(unittest.TestCase):
    def run_transaction(self, failure=None, blocking=True, db_available=True):
        clock = [0.0]
        events, timing = [], {}
        class Mutex:
            def acquire(self, *, blocking, priority):
                events.append(('acquire', blocking, priority))
                clock[0] += 2.0
                return failure != 'python_busy'
            def release(self):
                events.append(('release',))
        class Connection:
            @contextmanager
            def transaction(self):
                events.append(('begin',))
                try:
                    yield
                    clock[0] += 7.0
                    if failure == 'commit':
                        raise RuntimeError('commit')
                    events.append(('commit',))
                except BaseException:
                    clock[0] += 11.0
                    events.append(('rollback',))
                    raise
            def execute(self, sql, args=()):
                events.append(('sql', sql))
                if 'advisory' in sql:
                    clock[0] += 3.0
                    if failure == 'advisory':
                        raise RuntimeError('advisory')
                return SimpleNamespace(fetchone=lambda: {'acquired': db_available})
        with patch.object(G, '_mutex', Mutex()), patch.object(G.time, 'monotonic', lambda: clock[0]):
            try:
                with G.book_transaction(Connection(), lane='PROTECTIVE',
                                        blocking=blocking, timing=timing) as acquired:
                    events.append(('body', acquired))
                    if acquired is not False:
                        clock[0] += 5.0
                        if failure == 'body':
                            raise RuntimeError('body')
            except RuntimeError:
                pass
        return events, timing

    def test_storage_preflight_runs_before_local_lock_and_apply_after_advisory(self):
        events=[]
        class Mutex:
            def acquire(self, *, blocking, priority):
                events.append('acquire')
                return True
            def release(self):
                events.append('release')
        class Connection:
            @contextmanager
            def transaction(self):
                events.append('begin')
                try:
                    yield self
                finally:
                    events.append('commit')
            def execute(self, sql, args=()):
                events.append('advisory' if 'advisory' in sql else 'sql')
                return SimpleNamespace(fetchone=lambda:{'acquired':True})
        prepared={'requested':'lz4','metadata':{'current_method':'pglz'}}
        def prepare(raw):
            events.append('prepare')
            return prepared
        def configure(raw, *, prepared=None):
            events.append(('configure',prepared))
            return {'status':'APPLIED','elapsed_seconds':0.0}
        timing={}
        with patch.object(G,'_mutex',Mutex()), patch.object(G.BS,'prepare',side_effect=prepare), \
             patch.object(G.BS,'configure',side_effect=configure):
            with G.book_transaction(Connection(),lane='PROTECTIVE',timing=timing):
                events.append('body')
        self.assertLess(events.index('prepare'),events.index('acquire'))
        self.assertLess(events.index('advisory'),events.index(('configure',prepared)))
        self.assertLess(events.index(('configure',prepared)),events.index('body'))
        self.assertEqual(timing['payload_compression']['status'],'APPLIED')
        self.assertIn('storage_preflight_seconds',timing)
        self.assertEqual(events[-2:],['commit','release'])

    def test_hold_includes_commit_and_waits_are_separate(self):
        events, measured = self.run_transaction()
        self.assertEqual(measured, dict(python_lock_wait_seconds=2.0,
            db_lock_wait_seconds=3.0, lock_hold_seconds=15.0, status='COMMITTED'))
        self.assertEqual(events[0], ('acquire', True, True))
        self.assertEqual(events[-2:], [('commit',), ('release',)])
        self.assertIn(('body', None), events)

    def test_body_and_lock_errors_release_only_after_rollback(self):
        for failure, hold in (('body', 19.0), ('advisory', 14.0)):
            with self.subTest(failure=failure):
                events, measured = self.run_transaction(failure)
                self.assertEqual(measured['status'], 'ROLLED_BACK')
                self.assertEqual(measured['lock_hold_seconds'], hold)
                self.assertEqual(measured['db_lock_wait_seconds'], 3.0)
                self.assertEqual(events[-2:], [('rollback',), ('release',)])

    def test_commit_acknowledgement_error_does_not_claim_accounting_rollback(self):
        events, measured = self.run_transaction('commit')
        self.assertEqual(measured['status'], 'COMMIT_UNKNOWN')
        self.assertEqual(measured['lock_hold_seconds'], 26.0)
        self.assertIn(('body', None), events)
        self.assertEqual(events[-1], ('release',))

    def test_busy_only_for_existing_nonblocking_callers(self):
        events, measured = self.run_transaction('python_busy', blocking=False)
        self.assertEqual(measured['status'], 'BUSY')
        self.assertEqual(measured['lock_hold_seconds'], 0.0)
        self.assertNotIn(('begin',), events)
        events, measured = self.run_transaction(blocking=False, db_available=False)
        self.assertEqual(measured['status'], 'BUSY')
        self.assertEqual(events[-2:], [('commit',), ('release',)])
        self.assertIn(('body', False), events)

    def test_actual_mutex_reentry_and_exception_leave_book_available(self):
        class Connection:
            @contextmanager
            def transaction(self):
                yield
            def execute(self, *unused):
                return SimpleNamespace(fetchone=lambda: {'acquired': True})
        mutex = PriorityRLock()
        with patch.object(G, '_mutex', mutex):
            with self.assertRaisesRegex(RuntimeError, 'fixture'):
                with mutex, G.book_transaction(Connection(), blocking=False) as acquired:
                    self.assertTrue(acquired)
                    raise RuntimeError('fixture')
            self.assertTrue(mutex.acquire(blocking=False))
            mutex.release()


class ProtectiveClockTests(unittest.TestCase):
    def test_live_clock_is_sampled_after_both_locks_replay_clock_is_preserved(self):
        before = datetime(2026, 10, 7, 17, 0, tzinfo=timezone.utc)
        after = before + timedelta(minutes=5)
        for explicit in (None, before):
            with self.subTest(explicit=explicit):
                events, seen = [], []
                case = self
                class Clock:
                    @classmethod
                    def now(cls, unused):
                        self.assertIn('locked', events)
                        return after
                class Connection:
                    def __enter__(self): return self
                    def __exit__(self, *unused): pass
                    def execute(self, sql, args=()):
                        case.assertIn('WHERE active_trade_id=ANY(%s)', sql)
                        case.assertIn('FOR UPDATE', sql)
                        case.assertEqual(args, (['T-ETH'],))
                        return SimpleNamespace(fetchall=lambda: [
                            {'asset': 'ETH', 'active_trade_id': 'T-ETH'}])
                @contextmanager
                def transaction(c, **kwargs):
                    self.assertEqual(kwargs['lane'], 'PROTECTIVE')
                    events.append('locked')
                    yield
                def select(position, candidate, now):
                    seen.append(now)
                    return {}
                with patch.object(G, 'datetime', Clock), patch.object(G, 'book_transaction', transaction), \
                     patch.object(G, 'quote_for_position', select), patch.object(G, 'exit_execution_quote', return_value={}):
                    timing = {}
                    self.assertEqual(G.run_protective_pass(
                        None, Connection, {'ETH': {'price': 1.0}}, explicit,
                        timing=timing, eligible_trade_ids=['T-ETH']), [])
                self.assertEqual(seen, [explicit or after])
                self.assertIn('positions_query_seconds', timing)
                self.assertIn('protection_seconds', timing)
                self.assertEqual(timing['lock_scope_positions'], 1)
                self.assertEqual(timing['lock_scope_assets'], 1)

    def test_no_fresh_position_quote_skips_global_book_lock(self):
        timing = {}
        class NeverConnect:
            def __enter__(self):
                raise AssertionError('no DB connection should be opened without a fresh protective quote')
        self.assertEqual(G.run_protective_pass(
            None, NeverConnect, {}, timing=timing, eligible_trade_ids=[]), [])
        self.assertEqual(timing['status'], 'NO_FRESH_QUOTES')
        self.assertEqual(timing['lock_scope_positions'], 0)
        self.assertEqual(timing['positions_query_seconds'], 0.0)


if __name__ == '__main__':
    unittest.main()
