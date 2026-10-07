"""Real young/old cycles and concurrent completion must survive GC coalescing."""
import gc
import threading
import time
from types import SimpleNamespace
import unittest
import weakref

from veritas_memory_reclaim import MemoryReclaimer, reclaim


class Cycle:
    __slots__ = ('self_reference', '__weakref__')


def unreachable_cycle(*, old=False):
    value = Cycle()
    value.self_reference = value
    reference = weakref.ref(value)
    if old:
        gc.collect()
    return reference


class MemoryReclaimTests(unittest.TestCase):
    def setUp(self):
        enabled = gc.isenabled()
        gc.disable()
        self.addCleanup(lambda: gc.enable() if enabled else None)
        self.addCleanup(gc.collect)
        self.at = 100.
        self.rss = 200.
        self.allocator_rss = []
        self.sweeps, self.trims = [], 0
        self.coordinator = MemoryReclaimer(clock=lambda: self.at)

    def collect(self, generation=2):
        self.sweeps.append(generation)
        return gc.collect(generation)

    def allocator(self):
        self.trims += 1
        if self.allocator_rss:
            self.rss = self.allocator_rss.pop(0)

    def run_phase(self, phase='horizon_ETH_5m', **kwargs):
        return self.coordinator.run(phase=phase, before=self.rss, threshold=320.,
                    collect=self.collect, rss=lambda: self.rss,
                    allocator=self.allocator, **kwargs)

    def test_young_cycles_are_freed_without_repeated_old_generation_sweeps(self):
        self.run_phase()
        old = unreachable_cycle(old=True)
        young = unreachable_cycle()
        self.at += 1.
        result = self.run_phase()
        self.assertEqual(self.sweeps, [2, 0])
        self.assertIsNone(young())
        self.assertIsNotNone(old())
        self.assertEqual(result['gc_full_collections'], 0)
        self.assertEqual(self.trims, 2)
        self.at += 4.
        result = self.run_phase()
        self.assertEqual(self.sweeps, [2, 0, 2])
        self.assertIsNone(old())
        self.assertEqual(result['gc_full_collections'], 1)

    def test_each_terminal_boundary_reclaims_old_cycles_before_returning(self):
        self.run_phase()
        for phase in ('asset_ETH', 'cycle_end', 'snapshot_drift_done',
                      'snapshot_performance_done', 'snapshot_health_done',
                      'snapshot_end', 'heavy_learning_end', 'portfolio_book_failed',
                      'portfolio_book_uncertain', 'portfolio_unexpected', 'unknown'):
            with self.subTest(phase=phase):
                old = unreachable_cycle(old=True)
                self.at += .1
                result = self.run_phase(phase)
                self.assertIsNone(old())
                self.assertEqual(result['gc_full_collections'], 1)
                self.assertEqual(result['gc_young_collections'], 0)

    def test_first_pressure_is_full_and_growth_is_measured_after_allocator_release(self):
        self.run_phase()
        self.at += 1.
        self.rss, self.allocator_rss = 400., [390.]
        result = self.run_phase()
        self.assertEqual(result['gc_full_collections'], 1)
        self.assertEqual(result['gc_young_collections'], 0)
        old = unreachable_cycle(old=True)
        self.at += 1.
        self.rss, self.allocator_rss = 420., [395.]
        result = self.run_phase()
        self.assertEqual(result['gc_full_collections'], 0)
        self.assertEqual(result['gc_young_collections'], 1)
        self.assertIsNotNone(old())
        self.at += 1.
        self.rss, self.allocator_rss = 420., [399., 389.]
        result = self.run_phase()
        self.assertEqual(result['gc_full_collections'], 1)
        self.assertEqual(result['gc_young_collections'], 1)
        self.assertEqual(result['after_mb'], 389.)
        self.assertIsNone(old())

    def test_constant_pressure_has_a_bounded_five_second_old_cycle_lifetime(self):
        self.rss = 390.
        self.run_phase()
        old = unreachable_cycle(old=True)
        for index in range(1, 5):
            self.at = 100.+index
            self.assertEqual(self.run_phase()['gc_full_collections'], 0)
            self.assertIsNotNone(old())
        self.at = 105.
        self.assertEqual(self.run_phase()['gc_full_collections'], 1)
        self.assertIsNone(old())

    def test_cache_eviction_and_unknown_rss_cannot_suppress_full_collection(self):
        self.run_phase()
        for changes in ({'caches_cleared': 1}, {}):
            old = unreachable_cycle(old=True)
            self.at += .1
            if not changes:
                self.rss = None
            result = self.run_phase(**changes)
            self.assertIsNone(old())
            self.assertEqual(result['gc_full_collections'], 1)

    def test_namespace_wrapper_uses_the_existing_stricter_memory_guard(self):
        ns = {'MEMORY_SOFT_LIMIT_MB': 320., 'V90_MEMORY_PROTECT_MB': 340.,
              'gc': SimpleNamespace(collect=self.collect), 'rss_mb': lambda: self.rss,
              '_v90_memory_reclaimer': self.coordinator}
        self.coordinator._allocator = lambda _: self.allocator()
        reclaim(ns, phase='horizon_ETH_5m', before=self.rss)
        old = unreachable_cycle(old=True)
        self.at += 1.
        self.rss = 330.
        result = reclaim(ns, phase='horizon_ETH_1h', before=self.rss)
        self.assertIsNone(old())
        self.assertEqual(result['gc_full_collections'], 1)
        self.assertEqual(ns['MEMORY_SOFT_LIMIT_MB'], 320.)
        self.assertEqual(ns['V90_MEMORY_PROTECT_MB'], 340.)

    def test_gc_failure_still_releases_allocator_and_retries_on_next_boundary(self):
        calls = []
        def failed(generation=2):
            calls.append(generation)
            raise RuntimeError('collector failed')
        for _ in range(2):
            result = self.coordinator.run(phase='horizon_ETH_5m', before=200.,
                threshold=320., collect=failed, rss=lambda: 200., allocator=self.allocator)
            self.assertEqual(result['gc_error'], 'RuntimeError')
        self.assertEqual(calls, [2, 2])
        self.assertEqual(self.trims, 2)

    def test_busy_hot_request_returns_without_duplicate_full_and_drains_new_young_cycles(self):
        coordinator = MemoryReclaimer()
        collected, release = threading.Event(), threading.Event()
        results, errors, generations = [], [], []
        def collect(generation=2):
            generations.append(generation)
            result = gc.collect(generation)
            if len(generations) == 1:
                collected.set()
                if not release.wait(3.):
                    raise AssertionError('test did not release collector')
            return result
        def run():
            try:
                results.append(coordinator.run(phase='horizon_ETH_1m', before=200.,
                    threshold=320., collect=collect, rss=lambda: 200., allocator=lambda: None))
            except BaseException as error:
                errors.append(error)
        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(collected.wait(3.))
            young = unreachable_cycle()
            result = coordinator.run(phase='portfolio_book_done', before=200.,
                threshold=320., collect=collect, rss=lambda: 200., allocator=lambda: None)
            self.assertTrue(result['gc_deferred'])
            self.assertFalse(result['trimmed'])
            self.assertIsNotNone(young())
        finally:
            release.set()
            worker.join(3.)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(generations, [2, 0])
        self.assertIsNone(young())
        self.assertTrue(results[0]['gc_coalesced'])

    def test_waiting_terminals_share_only_a_full_sweep_started_after_their_requests(self):
        first_collected, release, queued = (threading.Event() for _ in range(3))
        requests, results, errors, generations = set(), [], [], []
        request_lock = threading.Lock()
        def clock():
            stamp = time.monotonic()
            name = threading.current_thread().name
            if name.startswith('terminal-'):
                with request_lock:
                    requests.add(name)
                    if len(requests) == 2:
                        queued.set()
            return stamp
        coordinator = MemoryReclaimer(clock=clock)
        def collect(generation=2):
            generations.append(generation)
            result = gc.collect(generation)
            if len(generations) == 1:
                first_collected.set()
                if not release.wait(3.):
                    raise AssertionError('test did not release collector')
            return result
        def run(phase):
            try:
                results.append(coordinator.run(phase=phase, before=200., threshold=320.,
                    collect=collect, rss=lambda: 200., allocator=lambda: None))
            except BaseException as error:
                errors.append(error)
        value = Cycle(); value.self_reference = value
        reference = weakref.ref(value)
        gc.collect()
        owner = threading.Thread(target=run, args=('horizon_ETH_1m',), name='hot-owner')
        workers = [threading.Thread(target=run, args=('cycle_end',), name='terminal-'+str(i))
                   for i in range(2)]
        owner.start()
        try:
            self.assertTrue(first_collected.wait(3.))
            value = None  # Old cycle becomes dead after the first sweep.
            for worker in workers:
                worker.start()
            self.assertTrue(queued.wait(3.))
            self.assertIsNotNone(reference())
        finally:
            release.set()
            owner.join(3.)
            for worker in workers:
                if worker.ident is not None:
                    worker.join(3.)
        self.assertTrue(all(not worker.is_alive() for worker in [owner, *workers]))
        self.assertEqual(errors, [])
        self.assertIsNone(reference())
        self.assertEqual(generations.count(2), 2)
        self.assertEqual(generations.count(0), 1)
        self.assertEqual(sum(result['gc_coalesced'] for result in results), 1)


if __name__ == '__main__':
    unittest.main()
