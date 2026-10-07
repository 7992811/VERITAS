"""Reentrant local book ownership with a handoff to waiting protection."""
import math
import threading
import time

ORDINARY_AGING_SECONDS = 20.0


class PriorityRLock:
    """Do not interrupt an owner; give waiting protection the next free turn.

    Reentry stays immediate, including the structural lane's explicit local
    reservation before book_transaction. Nonblocking callers respect queued
    protection. After one handoff window, the oldest blocking ordinary caller
    precedes new entry reservations. PostgreSQL still provides the cross-process
    accounting lock; a current owner is never interrupted.
    """

    def __init__(self):
        self._condition = threading.Condition(threading.Lock())
        self._owner = None
        self._depth = 0
        self._priority_waiters = 0
        self._ordinary_waiters = 0
        self._blocking_waiters = {}
        self._entry_turns = {}
        self._owner_name = None
        self._acquired_at = None
        self._last_hold_seconds = 0.0
        self._last_handoff_reason = None

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
            try:
                if blocking and not priority:
                    self._blocking_waiters[ident] = time.monotonic()
                self._condition.notify_all()
                while True:
                    now = time.monotonic()
                    self._entry_turns = {owner: until for owner, until in self._entry_turns.items() if until > now}
                    reserved_elsewhere = bool(self._entry_turns) and ident not in self._entry_turns
                    oldest = next(iter(self._blocking_waiters), None)
                    age_remaining = (self._blocking_waiters[oldest] + ORDINARY_AGING_SECONDS - now
                                     if oldest is not None else None)
                    aged = oldest if age_remaining is not None and age_remaining <= 0 else None
                    ordinary_blocked = (self._priority_waiters or
                        (aged is not None and aged != ident) or (aged is None and reserved_elsewhere))
                    blocked = self._owner is not None or (not priority and ordinary_blocked)
                    if not blocked:
                        break
                    if not blocking:
                        return False
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return False
                    # A stopped entry worker cannot strand ordinary accounting.
                    # Wake at the lease boundary even without another notify.
                    if not priority and aged is None and reserved_elsewhere:
                        lease = max(0., min(self._entry_turns.values()) - now)
                        remaining = lease if remaining is None else min(remaining, lease)
                    # Self-wake once at aging, even if a reservation is renewed.
                    # Once aged, wait for the owner/guard; never spin at zero.
                    if not priority and age_remaining is not None and age_remaining > 0:
                        remaining = age_remaining if remaining is None else min(remaining, age_remaining)
                    self._condition.wait(remaining)
                self._last_handoff_reason = ('PROTECTIVE' if priority else
                    'AGED_ORDINARY' if aged == ident else
                    'RESERVED_ENTRY' if ident in self._entry_turns else 'ORDINARY')
                self._entry_turns.pop(ident, None)
                self._owner, self._depth = ident, 1
                self._owner_name = threading.current_thread().name[:96]
                self._acquired_at = time.monotonic()
                return True
            finally:
                if blocking and not priority:
                    self._blocking_waiters.pop(ident, None)
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
            oldest = next(iter(self._blocking_waiters.values()), None)
            return {'owner':self._owner_name, 'depth':self._depth,
                    'held_seconds':max(0.0, now-self._acquired_at) if self._acquired_at is not None else 0.0,
                    'last_hold_seconds':self._last_hold_seconds,
                    'protection_waiters':self._priority_waiters,
                    'ordinary_waiters':self._ordinary_waiters,
                    'oldest_blocking_wait_seconds':max(0., now-oldest) if oldest is not None else 0.,
                    'last_handoff_reason':self._last_handoff_reason,
                    'entry_reservations':sum(until > now for until in self._entry_turns.values())}

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *unused):
        self.release()
