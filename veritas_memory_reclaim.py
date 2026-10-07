"""Coordinate explicit collections without rescanning the live graph per row.

Allocator release and young-cycle collection remain at active boundaries. Old
cycles are collected on completion, after cache eviction, at the existing memory
guard, or at least every five seconds of cleanup activity. No worker, financial
lock, cache eviction or change to Python's automatic GC policy lives here.
"""
import math
import threading
import time

FULL_INTERVAL_SECONDS = 5.0
PRESSURE_GROWTH_MB = 8.0
_creation_lock = threading.Lock()


def _number(value):
    if type(value) in (int, float) and math.isfinite(value):
        return float(value)
    return None


class MemoryReclaimer:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending_young = False
        self._last_full_started = self._last_full_finished = None
        self._last_full_rss = None
        self._pressure_seen = False
        self._allocator = None

    def _malloc_trim(self):
        if self._allocator is None:
            import ctypes
            self._allocator = ctypes.CDLL('libc.so.6').malloc_trim
        self._allocator(0)

    def run(self, *, phase, before, threshold, collect, rss,
            caches_cleared=0, allocator=None):
        requested = self.clock()
        before, threshold = _number(before), _number(threshold)
        # Unknown phases keep the original full-collection contract. Only the
        # numerous horizon/portfolio checkpoints are eligible for coalescing.
        repeated = str(phase).startswith('horizon_') or phase in (
            'portfolio_book_done', 'portfolio_calibration_r29_done',
            'portfolio_calibration_r33_done')
        terminal = not repeated or bool(caches_cleared)
        if not self._lock.acquire(blocking=terminal):
            with self._pending_lock:
                self._pending_young = True
            return {'before_mb': before, 'after_mb': before, 'trimmed': False,
                    'gc_deferred': True, 'gc_coalesced': True,
                    'gc_full_collections': 0, 'gc_young_collections': 0,
                    'gc_collected': 0, 'gc_seconds': 0.0, 'allocator_seconds': 0.0}
        metrics = {'gc_deferred': False, 'gc_coalesced': False,
                   'gc_full_collections': 0, 'gc_young_collections': 0,
                   'gc_collected': 0, 'gc_seconds': 0.0, 'allocator_seconds': 0.0}
        release = allocator or self._malloc_trim

        def pressure(value):
            return value is None or threshold is None or value >= threshold

        def sweep(generation):
            started = self.clock()
            try:
                count = collect() if generation == 2 else collect(0)
                metrics['gc_full_collections' if generation == 2 else 'gc_young_collections'] += 1
                if type(count) is int:
                    metrics['gc_collected'] += count
                if generation == 2:
                    self._last_full_started = started
                    self._last_full_finished = self.clock()
                return True
            except Exception as error:
                metrics['gc_error'] = type(error).__name__
                return False
            finally:
                metrics['gc_seconds'] += max(0., self.clock()-started)

        def trim():
            started = self.clock()
            try:
                release()
            except Exception as error:
                metrics['allocator_error'] = type(error).__name__
            finally:
                metrics['allocator_seconds'] += max(0., self.clock()-started)
            try:
                return _number(rss())
            except Exception:
                return None

        def full():
            succeeded = sweep(2)
            remaining = trim()
            if succeeded:
                self._last_full_rss = remaining
            return remaining

        def young_then_pressure():
            sweep(0)
            remaining = trim()
            # Assess growth after allocator release, not the transient peak
            # before trim. Persistent pressure cannot suppress GC indefinitely.
            if pressure(remaining) and (not self._pressure_seen or
                    remaining is None or self._last_full_rss is None or
                    remaining-self._last_full_rss >= PRESSURE_GROWTH_MB):
                return full()
            return remaining

        try:
            # A terminal requester may wait for a collection that starts after
            # it released its references. That full sweep already covers its
            # old cycles. A collection begun before the request cannot do so.
            covered = (self._last_full_started is not None and
                       self._last_full_started >= requested)
            due = (self._last_full_finished is None or
                   self.clock()-self._last_full_finished >= FULL_INTERVAL_SECONDS)
            first_pressure = pressure(before) and not self._pressure_seen
            if (terminal and not covered) or due or first_pressure or before is None:
                after = full()
            else:
                metrics['gc_coalesced'] = covered
                after = young_then_pressure()
            self._pressure_seen = pressure(after)
            # One bounded drain collects young cycles dropped by concurrent hot
            # callers. Terminal callers wait and retain their own full guarantee.
            with self._pending_lock:
                pending, self._pending_young = self._pending_young, False
            if pending:
                metrics['gc_coalesced'] = True
                after = young_then_pressure()
                self._pressure_seen = pressure(after)
            return {'before_mb': before, 'after_mb': after, 'trimmed': True,
                    **{key: round(value, 6) if type(value) is float else value
                       for key, value in metrics.items()}}
        finally:
            self._lock.release()


def reclaim(namespace, *, phase, before, caches_cleared=0):
    """One coordinator per runtime namespace; only bounded scalar state is kept."""
    coordinator = namespace.get('_v90_memory_reclaimer')
    if coordinator is None:
        with _creation_lock:
            coordinator = namespace.get('_v90_memory_reclaimer')
            if coordinator is None:
                coordinator = MemoryReclaimer()
                namespace['_v90_memory_reclaimer'] = coordinator
    limits = [_number(namespace.get(key)) for key in
              ('MEMORY_SOFT_LIMIT_MB', 'V90_MEMORY_PROTECT_MB')]
    threshold = min((value for value in limits if value is not None), default=None)
    return coordinator.run(phase=phase, before=before, threshold=threshold,
                           collect=namespace['gc'].collect, rss=namespace['rss_mb'],
                           caches_cleared=caches_cleared)
