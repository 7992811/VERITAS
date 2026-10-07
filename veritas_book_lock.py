"""Reentrant local book ownership with a handoff to waiting protection."""
import math
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
        self._entry_turns = {}
        self._owner_name = None
        self._acquired_at = None
        self._last_hold_seconds = 0.0

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
                while True:
                    now = time.monotonic()
                    self._entry_turns = {owner: until for owner, until in self._entry_turns.items() if until > now}
                    reserved_elsewhere = bool(self._entry_turns) and ident not in self._entry_turns
                    blocked = self._owner is not None or (not priority and (self._priority_waiters or reserved_elsewhere))
                    if not blocked:
                        break
                    if not blocking:
                        return False
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return False
                    # A stopped entry worker cannot strand ordinary accounting.
                    # Wake at the lease boundary even without another notify.
                    if not priority and reserved_elsewhere:
                        lease = max(0., min(self._entry_turns.values()) - now)
                        remaining = lease if remaining is None else min(remaining, lease)
                    self._condition.wait(remaining)
                self._entry_turns.pop(ident, None)
                self._owner, self._depth = ident, 1
                self._owner_name = threading.current_thread().name[:96]
                self._acquired_at = time.monotonic()
                return True
            finally:
                setattr(self, counter, getattr(self, counter) - 1)
                self._condition.notify_all()

    def reserve_entry_turn(self, seconds=20.):
        """A bounded retry handoff; never outrank a queued protective exit."""
        if not math.isfinite(seconds) or not 0 < seconds <= 30:
            raise ValueError('entry handoff must be within 30 seconds')
        with self._condition:
            self._entry_turns[threading.get_ident()] = time.monotonic() + seconds
            self._condition.notify_all()

    def cancel_entry_turn(self):
        with self._condition:
            self._entry_turns.pop(threading.get_ident(), None)
            self._condition.notify_all()

    def release(self):
        with self._condition:
            if self._owner != threading.get_ident():
                raise RuntimeError('cannot release un-acquired lock')
            self._depth -= 1
            if not self._depth:
                self._last_hold_seconds = max(0.0, time.monotonic()-self._acquired_at)
                self._owner = self._owner_name = self._acquired_at = None
                self._condition.notify_all()

    def snapshot(self):
        """Bounded diagnostics only: no frames, local variables or account data."""
        with self._condition:
            now = time.monotonic()
            return {'owner':self._owner_name, 'depth':self._depth,
                    'held_seconds':max(0.0, now-self._acquired_at) if self._acquired_at is not None else 0.0,
                    'last_hold_seconds':self._last_hold_seconds,
                    'protection_waiters':self._priority_waiters,
                    'ordinary_waiters':self._ordinary_waiters,
                    'entry_reservations':sum(until > now for until in self._entry_turns.values())}

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *unused):
        self.release()
