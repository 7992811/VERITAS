"""Aging changes only free-book handoff, never an active accounting owner."""
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import veritas_book_lock as BL


class BookFairnessTests(unittest.TestCase):
    def setUp(self):
        self.clock = 0.0
        self.enterContext(patch.object(BL, 'time', SimpleNamespace(monotonic=lambda: self.clock)))
        self.threads, self.releases, self.failures, self.locks = [], [], [], []
        self.order = []
        self.addCleanup(self.finish_threads)

    def finish_threads(self):
        for event in self.releases:
            event.set()
        for lock in self.locks:
            while lock._owner == threading.get_ident():
                lock.release()
        for thread in self.threads:
            thread.join(5)
            self.assertFalse(thread.is_alive(), 'handoff stranded a waiting thread')
        self.assertEqual(self.failures, [])

    def lock(self):
        lock = BL.PriorityRLock()
        self.locks.append(lock)
        return lock

    def advance(self, lock, value):
        with lock._condition:
            self.clock = value
            lock._condition.notify_all()

    def queued(self, lock, ordinary=0, protective=0):
        with lock._condition:
            self.assertTrue(lock._condition.wait_for(lambda:
                lock._ordinary_waiters == ordinary and lock._priority_waiters == protective, 5))

    def waiter(self, lock, name, *, priority=False, timeout=-1):
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        result = {}
        def run():
            acquired = False
            try:
                acquired = lock.acquire(priority=priority, timeout=timeout)
                result['acquired'] = acquired
                if acquired:
                    self.order.append(name)
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError('test did not release '+name)
            except BaseException as error:
                self.failures.append(error)
            finally:
                if acquired:
                    lock.release()
                done.set()
        thread = threading.Thread(target=run, name=name, daemon=True)
        self.threads.append(thread)
        self.releases.append(release)
        thread.start()
        return SimpleNamespace(entered=entered, release=release, done=done, result=result)

    def test_repeated_renewals_and_successful_structural_turns_cannot_reset_age(self):
        lock = self.lock()
        lock.acquire()
        ordinary = self.waiter(lock, 'portfolio')
        self.queued(lock, ordinary=1)
        # Before the aging boundary, preserve the existing structural handoff.
        # Each successful acquisition consumes its lease; the next is new.
        for at in (1., 6., 12., 19.):
            self.advance(lock, at)
            lock.reserve_entry_turn()
            lock.release()
            self.assertTrue(lock.acquire(blocking=False))
            self.assertEqual(lock.snapshot()['last_handoff_reason'], 'RESERVED_ENTRY')
            self.assertFalse(ordinary.entered.is_set())
        self.advance(lock, 20.)
        lock.reserve_entry_turn()  # Even a brand-new live lease cannot restart age.
        self.assertEqual(lock.snapshot()['oldest_blocking_wait_seconds'], 20.)
        lock.release()
        self.assertTrue(ordinary.entered.wait(5))
        self.assertEqual(self.order, ['portfolio'])
        self.assertEqual(lock.snapshot()['last_handoff_reason'], 'AGED_ORDINARY')
        self.assertEqual(lock.snapshot()['entry_reservations'], 1, 'pending API stays intact')
        self.assertFalse(lock.acquire(blocking=False))
        ordinary.release.set()
        self.assertTrue(ordinary.done.wait(5))
        self.assertEqual(lock._blocking_waiters, {})

    def test_protection_then_aged_fifo_precede_reserved_entry_and_allow_owner_reentry(self):
        lock = self.lock()
        lock.acquire()
        first = self.waiter(lock, 'oldest')
        self.queued(lock, ordinary=1)
        self.advance(lock, 5.)
        second = self.waiter(lock, 'second')
        self.queued(lock, ordinary=2)
        self.advance(lock, 25.)
        guard = self.waiter(lock, 'protection', priority=True)
        self.queued(lock, ordinary=2, protective=1)
        lock.reserve_entry_turn()
        self.assertTrue(lock.acquire(blocking=False))
        self.assertTrue(lock.acquire(priority=True))
        self.assertEqual(lock.snapshot()['depth'], 3)
        lock.release(); lock.release(); lock.release()
        self.assertTrue(guard.entered.wait(5))
        self.assertEqual(self.order, ['protection'])
        self.assertEqual(lock.snapshot()['last_handoff_reason'], 'PROTECTIVE')
        self.assertFalse(first.entered.is_set())
        self.assertFalse(second.entered.is_set())
        guard.release.set()
        self.assertTrue(first.entered.wait(5))
        self.assertEqual(self.order, ['protection', 'oldest'])
        self.assertFalse(second.entered.is_set())
        first.release.set()
        self.assertTrue(second.entered.wait(5))
        self.assertEqual(self.order, ['protection', 'oldest', 'second'])
        second.release.set()
        self.assertTrue(second.done.wait(5))
        self.assertEqual(lock._blocking_waiters, {})
        self.assertEqual(lock.snapshot()['oldest_blocking_wait_seconds'], 0.)

    def test_reserved_nonblocking_caller_cannot_barge_free_lock_with_aged_waiter(self):
        lock = self.lock()
        # Deterministic free-lock predicate, without relying on OS wake order.
        lock._blocking_waiters[-1] = 0.
        self.clock = 20.
        lock.reserve_entry_turn()
        self.assertFalse(lock.acquire(blocking=False))
        self.assertEqual(lock._blocking_waiters, {-1: 0.})
        self.assertEqual(lock._ordinary_waiters, 0)
        self.assertTrue(lock.acquire(blocking=False, priority=True))
        lock.release()
        lock._blocking_waiters.clear()
        lock.reserve_entry_turn()
        self.assertTrue(lock.acquire(blocking=False))
        self.assertEqual(lock.snapshot()['last_handoff_reason'], 'RESERVED_ENTRY')
        lock.release()

    def test_aging_self_wakes_before_live_lease_without_external_notification(self):
        lock = self.lock()
        lock._entry_turns[-1] = 30.
        waits = []
        def timed_wait(seconds):
            waits.append(seconds)
            self.clock += seconds
            return False  # A real Condition timeout, not a notify, has this result.
        with patch.object(lock._condition, 'wait', side_effect=timed_wait):
            self.assertTrue(lock.acquire())
        self.assertEqual(waits, [20.])
        self.assertEqual(self.clock, 20.)
        self.assertEqual(lock.snapshot()['last_handoff_reason'], 'AGED_ORDINARY')
        self.assertEqual(lock._blocking_waiters, {})
        lock.release()

    def test_aged_waiter_does_not_spin_while_owner_or_protection_still_blocks(self):
        for blocker in ('owner', 'protection'):
            with self.subTest(blocker=blocker):
                lock = self.lock()
                lock._entry_turns[-2] = 30.
                if blocker == 'owner':
                    lock._owner, lock._depth = -1, 1
                else:
                    lock._priority_waiters = 1
                self.clock, waits = 0., []
                def timed_wait(seconds):
                    waits.append(seconds)
                    if len(waits) == 1:
                        self.assertEqual(seconds, 20.)
                        self.clock = 20.
                    else:
                        self.assertIsNone(seconds, 'aged waiter must wait for release, not spin')
                        lock._owner, lock._depth, lock._priority_waiters = None, 0, 0
                        self.clock = 21.
                    return False
                with patch.object(lock._condition, 'wait', side_effect=timed_wait):
                    self.assertTrue(lock.acquire())
                self.assertEqual(waits, [20., None])
                lock.release()

    def test_timeout_and_wait_exception_remove_queue_head_and_wake_successor(self):
        for failure in ('timeout', 'exception'):
            with self.subTest(failure=failure):
                lock = self.lock()
                self.clock = 0.
                lock._entry_turns[-1] = 30.
                successor = -2
                def wait(seconds):
                    # A later caller queued while the head was asleep.
                    lock._blocking_waiters[successor] = 1.
                    if failure == 'exception':
                        raise KeyboardInterrupt('interrupted waiter')
                    self.clock = 5.
                    return False
                with patch.object(lock._condition, 'notify_all', wraps=lock._condition.notify_all) as notify:
                    with patch.object(lock._condition, 'wait', side_effect=wait):
                        if failure == 'exception':
                            with self.assertRaises(KeyboardInterrupt):
                                lock.acquire(timeout=5.)
                        else:
                            self.assertFalse(lock.acquire(timeout=5.))
                    self.assertGreaterEqual(notify.call_count, 2, 'registration and cleanup notify successors')
                self.assertEqual(lock._blocking_waiters, {successor: 1.})
                self.assertEqual(lock._ordinary_waiters, 0)
                self.assertIsNone(lock._owner)
                lock._blocking_waiters.clear()

    def test_nonblocking_and_protective_callers_are_never_registered_for_aging(self):
        class NoRegistration(dict):
            def __setitem__(self, key, value):
                raise AssertionError('only blocking ordinary callers belong to the aging queue')
        lock = self.lock()
        lock._blocking_waiters = NoRegistration()
        self.assertTrue(lock.acquire(blocking=False))
        self.assertTrue(lock.acquire())  # Immediate owner reentry also never registers.
        lock.release(); lock.release()
        lock._priority_waiters = 1
        self.assertFalse(lock.acquire(blocking=False))
        lock._priority_waiters = 0
        self.assertTrue(lock.acquire(priority=True))
        lock.release()
        self.assertEqual(lock._blocking_waiters, {})


if __name__ == '__main__':
    unittest.main()
