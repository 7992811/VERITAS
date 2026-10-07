"""Continuous learning gets bounded, fair work without a FULL cycle or HTTP."""
import gc
import threading
import time
import unittest
from unittest.mock import patch
import weakref

import veritas_maintenance as M
from test_veritas_maintenance import full, namespace


class PeriodicMaintenanceTests(unittest.TestCase):
    def lane(self, memory=200., retry=.02):
        ns = namespace()
        rss = [memory]
        ns["rss_mb"] = lambda: rss[0]
        lane = M.MaintenanceLane(ns, retry_seconds=retry, error_retry_seconds=retry)
        self.addCleanup(lane.close)
        return ns, lane, rss

    def wait_for(self, predicate, timeout=2.):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("periodic worker did not reach the expected state")
            threading.Event().wait(.003)

    def test_tick_runs_without_full_cycle_ui_or_new_thread_for_requests(self):
        ns, lane, rss = self.lane()
        calls = []
        lane.register_periodic("learning", lambda: calls.append(1) or {"status": "OK"},
                               interval_seconds=.03, lightweight=True)
        self.assertTrue(lane.start_periodic())
        self.wait_for(lambda: len(calls) >= 2)
        worker = lane._thread
        for _ in range(100):
            self.assertTrue(lane.request("learning"))
        self.assertIs(lane._thread, worker)
        self.assertEqual(lane.snapshot()["pending"], 0)
        self.assertEqual(lane.snapshot()["requests"], 0)
        self.assertEqual(len(lane.snapshot()["periodic"]), 1)

    def test_periodic_learning_runs_at_418_without_relaxing_legacy_or_heavy_guard(self):
        ns, lane, rss = self.lane(418.9)
        called, budgets = [], []
        def learning():
            budgets.append(lane.current_budget())
            called.append("light")
            return {"status": "PROGRESS", "processed": 128}
        lane.register_periodic("light", learning, lightweight=True)
        lane.register_periodic("heavy", lambda: called.append("heavy") or {"status": "OK"})
        self.assertTrue(lane._run_periodic_once())
        self.assertTrue(lane._run_periodic_once())
        self.assertEqual(called, ["light"])
        self.assertEqual(budgets[0]["memory_start_limit_mb"], 432.)
        self.assertEqual(budgets[0]["max_rss_mb"], 434.9)
        self.assertLessEqual(budgets[0]["sql_timeout_ms"], 2000)
        self.assertEqual(lane.memory_limit(), 280.)
        self.assertEqual(lane.snapshot()["periodic"]["heavy"]["status"], "DEFERRED_MEMORY")
        with self.assertRaises(M.MaintenanceDeferred):
            with lane.permit("ordinary_history"):
                self.fail("legacy history crossed its 280 MiB guard")

    def test_light_callback_cannot_launder_nested_heavy_admission(self):
        ns, lane, rss = self.lane(418.9)
        entered = []
        def callback():
            with lane.permit("heavy_learning", 260.):
                entered.append(True)
            return {"status": "OK"}
        lane.register_periodic("learning", callback, lightweight=True)
        lane._run_periodic_once()
        state = lane.snapshot()["periodic"]["learning"]
        self.assertEqual(state["status"], "DEFERRED_MEMORY")
        self.assertEqual(state["completions"], 0)
        self.assertEqual(entered, [])

    def test_repeated_archive_deferral_does_not_starve_periodic_or_grow_full_queue(self):
        ns, lane, rss = self.lane(418.9)
        ticks = []
        entered, release = threading.Event(), threading.Event()
        completed, resume = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.addCleanup(resume.set)
        def learning():
            if not ticks:
                # The worker has dequeued A before its first periodic turn.
                entered.set()
                if not release.wait(2.):
                    raise AssertionError("test did not release the first periodic turn")
            ticks.append(1)
            return {"status": "OK"}
        emit = ns["emit"]
        def after_completion(event, **values):
            emit(event, **values)
            if (event == "maintenance_periodic_complete" and len(ticks) >= 3
                    and len(lane.snapshot()["stages"]) == 4 and not completed.is_set()):
                # Completion counters are committed before this notification;
                # hold the worker so another callback cannot race assertions.
                completed.set()
                resume.wait(2.)
        ns["emit"] = after_completion
        lane.register_periodic("learning", learning,
                               interval_seconds=.01, lightweight=True)
        self.assertTrue(lane.submit(full("A")))
        self.assertTrue(entered.wait(1.))
        for index in range(20):
            self.assertTrue(lane.submit(full("B" + str(index))))
        release.set()
        self.assertTrue(completed.wait(2.))
        state = lane.snapshot()
        self.assertEqual(state["active_cycle_at"], "A")
        self.assertEqual(state["pending_cycle_at"], "B19")
        self.assertEqual(state["pending"], 1)
        self.assertEqual(state["pending_limit"], 1)
        self.assertEqual(state["coalesced"], 19)
        self.assertEqual(state["runs"], 0)
        self.assertGreaterEqual(len(ticks), 3)
        self.assertEqual(state["periodic"]["learning"]["completions"], len(ticks))
        self.assertTrue(all(s["status"] == "DEFERRED_MEMORY" for s in state["stages"].values()))
        resume.set()

    def test_always_requested_microjob_cannot_starve_full_stages(self):
        ns, lane, rss = self.lane()
        calls = []
        def callback():
            calls.append("micro")
            lane.request("learning")
            return {"status": "OK"}
        lane.register_periodic("learning", callback, interval_seconds=.01, lightweight=True)
        lane.submit(full())
        self.wait_for(lambda: lane.snapshot()["runs"] == 1)
        self.assertTrue(calls)
        self.assertEqual(lane.snapshot()["last_stages"]["outcomes"]["status"], "OK")

    def test_round_robin_keeps_failed_due_job_from_starving_other_jobs(self):
        ns, lane, rss = self.lane()
        calls, clock = [], [100.]
        with patch.object(M.time, "monotonic", lambda: clock[0]):
            for name, status in (("a", "DEFERRED_STORAGE"), ("b", "OK"), ("c", "RETRY")):
                lane.register_periodic(name, lambda n=name, s=status: calls.append(n) or {"status": s},
                                       interval_seconds=.02, retry_seconds=.02, lightweight=True)
            for _ in range(9):
                lane._run_periodic_once()
                clock[0] += .03
            self.assertEqual(calls, ["a", "b", "c"] * 3)
            state = lane.snapshot()["periodic"]
            self.assertEqual(state["a"]["completions"], 0)
            self.assertEqual(state["b"]["completions"], 3)
            self.assertEqual(state["c"]["completions"], 0)

    def test_busy_reservation_prevents_new_history_from_barging_ahead(self):
        ns, lane, rss = self.lane(retry=.03)
        acquired, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def existing_history():
            with lane.permit("existing_history"):
                acquired.set()
                release.wait(2.)
        holder = threading.Thread(target=existing_history)
        holder.start()
        self.assertTrue(acquired.wait(1.))
        calls = []
        lane.register_periodic("learning", lambda: calls.append(1) or {"status": "OK"}, lightweight=True)
        lane._run_periodic_once()
        self.assertEqual(lane.snapshot()["scheduled_resource_waiter"], "periodic:learning")
        release.set()
        holder.join(1.)
        with self.assertRaises(M.MaintenanceDeferred):
            with lane.permit("new_history"):
                self.fail("new history overtook the waiting periodic job")
        lane.start_periodic()
        self.wait_for(lambda: len(calls) == 1)
        self.assertIsNone(lane.snapshot()["scheduled_resource_waiter"])

    def test_requests_do_not_bypass_query_error_retry_and_later_success_recovers(self):
        ns, lane, rss = self.lane(retry=.03)
        class QueryCanceled(Exception):
            pass
        count = [0]
        def callback():
            count[0] += 1
            if count[0] == 1:
                raise QueryCanceled("statement timeout")
            return {"status": "OK", "processed": 1}
        lane.register_periodic("learning", callback, lightweight=True)
        lane._run_periodic_once()
        initial = lane.snapshot()["periodic"]["learning"]
        self.assertEqual(initial["last_result"]["error_code"], "QueryCanceled")
        self.assertIsNone(initial["last_success_at"])
        for _ in range(200):
            lane.request("learning")
            lane._run_periodic_once()
        self.assertEqual(count[0], 1)
        lane.start_periodic()
        self.wait_for(lambda: lane.snapshot()["periodic"]["learning"]["completions"] == 1)
        self.assertEqual(count[0], 2)

    def test_negative_and_invalid_results_are_not_success(self):
        values = [None, {}, {"status": "ERROR"}, {"status": "DEGRADED"},
                  {"status": "UNAVAILABLE"}, {"status": "RETRY"},
                  {"status": "OK", "error": "SQL timeout"},
                  {"status": "OK", "stage_status": "DEFERRED_STORAGE"},
                  {"status": "OK", "error_code": "QueryCanceled"},
                  {"status": "OK", "stage_result": {"status": "DEGRADED"}},
                  {"status": "OK", "result": {"status": "RETRY"}},
                  {"status": "OK", "ok": False}]
        for index, value in enumerate(values):
            with self.subTest(value=value):
                ns, lane, rss = self.lane()
                lane.register_periodic("learning", lambda v=value: v, lightweight=True)
                lane._run_periodic_once()
                state = lane.snapshot()["periodic"]["learning"]
                self.assertEqual(state["completions"], 0)
                self.assertIsNone(state["last_success_at"])

    def test_safe_trim_rechecks_headroom_without_evicting_active_cache(self):
        ns, lane, rss = self.lane(450.)
        trims, completed = [], []
        def trim(phase, **kw):
            trims.append((phase, kw))
            rss[0] = 420.
        ns["_v90_trim_memory"] = trim
        lane.register_periodic("learning", lambda: completed.append(1) or {"status": "OK"}, lightweight=True)
        lane._run_periodic_once()
        self.assertEqual(completed, [1])
        self.assertEqual(trims, [("learning_admission", {"force": True, "preserve_active_cycle": True})])
        self.assertEqual(lane.snapshot()["periodic"]["learning"]["last_result"]["start_rss_mb"], 420.)

    def test_unknown_memory_or_insufficient_headroom_cannot_execute_light_callback(self):
        for memory, status in ((None, "DEFERRED_MEMORY_UNKNOWN"), (450., "DEFERRED_MEMORY")):
            with self.subTest(memory=memory):
                ns, lane, rss = self.lane(memory)
                calls = []
                lane.register_periodic("learning", lambda: calls.append(1) or {"status": "OK"}, lightweight=True)
                lane._run_periodic_once()
                self.assertEqual(calls, [])
                self.assertEqual(lane.snapshot()["periodic"]["learning"]["status"], status)

    def test_measured_working_set_violation_cannot_be_reported_as_success(self):
        ns, lane, rss = self.lane(418.)
        def callback():
            rss[0] = 442.
            lane.check_budget()
            self.fail("batch continued beyond its reserved working set")
        lane.register_periodic("learning", callback, lightweight=True, estimated_peak_mb=16)
        lane._run_periodic_once()
        state = lane.snapshot()["periodic"]["learning"]
        self.assertEqual(state["status"], "DEFERRED_MEMORY_BUDGET")
        self.assertEqual(state["completions"], 0)
        self.assertEqual(state["last_result"]["observed_peak_mb"], 24.)
        self.assertEqual(state["last_result"]["max_rss_mb"], 434.)

    def test_sustained_insufficient_capacity_is_visible_instead_of_silent_deferral(self):
        ns, lane, rss = self.lane(450.)
        clock = [1000.]
        with patch.object(M.time, "time", lambda: clock[0]):
            lane.register_periodic("learning", lambda: {"status": "OK"}, lightweight=True)
            lane._run_periodic_once()
            self.assertFalse(lane.snapshot()["periodic"]["learning"]["capacity_review_required"])
            clock[0] += 301.
            state = lane.snapshot()["periodic"]["learning"]
            self.assertEqual(state["blocked_age_seconds"], 301.)
            self.assertTrue(state["capacity_review_required"])

    def test_time_budget_is_checked_between_batches_and_after_callback(self):
        for cooperative in (True, False):
            with self.subTest(cooperative=cooperative):
                ns, lane, rss = self.lane()
                clock = [100.]
                def callback():
                    clock[0] += 1.
                    if cooperative:
                        lane.check_budget()
                    return {"status": "OK"}
                with patch.object(M.time, "monotonic", lambda: clock[0]):
                    lane.register_periodic("learning", callback, lightweight=True, max_seconds=.1)
                    lane._run_periodic_once()
                    state = lane.snapshot()["periodic"]["learning"]
                    self.assertEqual(state["status"], "DEFERRED_TIME_BUDGET")
                    self.assertEqual(state["completions"], 0)

    def test_close_start_resumes_bounded_registration_without_parallel_worker(self):
        ns, lane, rss = self.lane(retry=.01)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        calls = []
        def callback():
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                release.wait(2.)
            return {"status": "OK"}
        lane.register_periodic("learning", callback, lightweight=True, interval_seconds=.02)
        lane.start_periodic()
        first = lane._thread
        self.assertTrue(entered.wait(1.))
        self.assertFalse(lane.close(timeout=.01))
        self.assertFalse(lane.start_periodic())
        self.assertIs(lane._thread, first)
        release.set()
        first.join(1.)
        self.assertFalse(first.is_alive())
        self.assertEqual(lane.snapshot()["periodic"]["learning"]["status"], "STOPPED")
        self.assertTrue(lane.start_periodic())
        self.wait_for(lambda: lane.snapshot()["periodic"]["learning"]["completions"] >= 1)
        self.assertIsNot(lane._thread, first)

    def test_large_callback_payload_is_not_retained_in_telemetry(self):
        ns, lane, rss = self.lane()
        references = []
        class Proof:
            pass
        def callback():
            proof = Proof()
            proof.cycle = proof
            references.append(weakref.ref(proof))
            return {"status": "OK", "processed": 1, "unused_history": proof}
        lane.register_periodic("learning", callback, lightweight=True)
        lane._run_periodic_once()
        gc.collect()
        self.assertIsNone(references[0]())
        result = lane.snapshot()["periodic"]["learning"]["last_result"]
        self.assertNotIn("unused_history", result)
        self.assertEqual(result["processed"], 1)

    def test_registration_capacity_and_budgets_are_bounded(self):
        ns, lane, rss = self.lane()
        for index in range(M.MAX_PERIODIC_JOBS):
            self.assertTrue(lane.register_periodic(f"job:{index}", lambda: {"status": "OK"}))
        self.assertFalse(lane.register_periodic("job:0", lambda: {"status": "OK"}))
        with self.assertRaises(ValueError):
            lane.register_periodic("overflow", lambda: {"status": "OK"})
        for kwargs in ({"estimated_peak_mb": 500}, {"max_seconds": 300},
                       {"interval_seconds": float("nan")}, {"retry_seconds": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                lane.register_periodic("bad", lambda: {"status": "OK"}, **kwargs)


if __name__ == "__main__":
    unittest.main()
