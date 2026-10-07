"""Reentrant local book ownership with a handoff to waiting protection."""
import threading
import time


class PriorityRLock:
    """Do not interrupt an owner; give waiting protection the next free turn.

    Reentry stays immediate, including the structural lane's explicit local
    reservation before book_transaction. Nonblocking callers respect queued
    protection. PostgreSQL still provides the cross-process accounting lock.
    """

    def __init__(self):
        self._condition = threading.Condition(threading.Lock())
        self._owner = None
        self._depth = 0
        self._priority_waiters = 0
        self._ordinary_waiters = 0

    def acquire(self, blocking=True, timeout=-1, *, priority=False):
        if not blocking and timeout != -1:
            raise ValueError("can't specify a timeout for a non-blocking call")
        if timeout < 0 and timeout != -1:
            raise ValueError('timeout value must be positive')
        ident = threading.get_ident()
        deadline = None if timeout == -1 else time.monotonic() + timeout
        with self._condition:
            if self._owner == ident:
                self._depth += 1
                return True
            counter = '_priority_waiters' if priority else '_ordinary_waiters'
            setattr(self, counter, getattr(self, counter) + 1)
            self._condition.notify_all()
            try:
                while self._owner is not None or (not priority and self._priority_waiters):
                    if not blocking:
                        return False
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return False
                    self._condition.wait(remaining)
                self._owner, self._depth = ident, 1
                return True
            finally:
                setattr(self, counter, getattr(self, counter) - 1)
                self._condition.notify_all()

    def release(self):
        with self._condition:
            if self._owner != threading.get_ident():
                raise RuntimeError('cannot release un-acquired lock')
            self._depth -= 1
            if not self._depth:
                self._owner = None
                self._condition.notify_all()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *unused):
        self.release()
