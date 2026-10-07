"""Deterministic backpressure, frozen provenance and history permit regressions."""
from copy import deepcopy
from enum import StrEnum
import gc
import json
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

import veritas_maintenance as M


def full(at="2026-10-07T16:04:40Z", price=12.75):
    return {"status": "ok", "at": at, "version": "fixture", "cycle_mode": "FULL",
            "signal_cells": 1, "decisions_written": 1, "outcomes_written": 0,
            "summary": [{"asset": "CNYRUBF", "horizon": "5m", "decision": "LONG", "price": price}]}


def namespace():
    events = []
    def learning():
        raise AssertionError("a checkpoint must not use the implicit background reader")
    learning._cache = (time.time(), {"index_version": "TEST", "index_vs_start": 101.})
    ns = {"emit": lambda event, **kw: events.append((event, kw)),
          "rss_mb": lambda: 180., "pg_enabled": lambda: True,
          "MEMORY_SOFT_LIMIT_MB": 280, "V90_MEMORY_CAUTION_MB": 280,
          "V90_HEAVY_LEARNING_MAX_START_MB": 260.,
          "learning_progress": learning, "VLI": SimpleNamespace(INDEX_VERSION="TEST"),
          "_learning_progress_refresh_sync": lambda reason: learning._cache[1],
          "_v90_daily_intelligence_metrics": lambda lp: {"status": "OK"},
          "save_product_snapshot": lambda **kw: {"status": "SAVED"},
          "_v90r37_storage_retention": lambda: {"status": "OK"},
          "_v90_outcome_state": {"status": "IDLE"},
          "events": events}
    ns["_v90_run_outcome_refresh"] = lambda reason: ns["_v90_outcome_state"].update(status="OK")
    return ns


class Unreadable:
    def __iter__(self):
        raise AssertionError("proof graph traversed")
    def __deepcopy__(self, memo):
        raise AssertionError("proof graph copied")
    def __str__(self):
        raise AssertionError("proof graph stringified")


class MaintenanceTests(unittest.TestCase):
    def lane(self, ns=None, retry_seconds=.02):
        ns = ns or namespace()
        lane = M.MaintenanceLane(ns, retry_seconds=retry_seconds, error_retry_seconds=retry_seconds)
        self.addCleanup(lane.close)
        return ns, lane

    def wait_for(self, predicate, timeout=2.):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("worker did not reach the expected state")
            threading.Event().wait(.005)

    def test_compact_request_has_no_reference_to_large_or_cyclic_proofs(self):
        ns, lane = self.lane()
        proof = Unreadable()
        proof.cycle = proof
        reference = weakref.ref(proof)
        cycle = full()
        cycle["summary"][0]["native_history"] = proof
        cycle["portfolio_autopilot"] = {"proof": proof}
        request = lane.prepare(cycle)
        expected = M.compact_cycle(full())
        self.assertEqual(json.loads(request.payload), expected)
        cycle["summary"][0]["price"] = 999.
        self.assertEqual(json.loads(request.payload)["summary"][0]["price"], 12.75)
        del proof, cycle
        gc.collect()
        self.assertIsNone(reference())
        self.assertLess(len(request.payload), 2000)

    def test_fast_requests_and_invalid_scalar_matrices_never_reach_worker(self):
        ns, lane = self.lane()
        self.assertIsNone(lane.prepare(dict(full(), cycle_mode="FAST_5M")))
        for invalid in (dict(full(), summary=[{}] * (M.MAX_CELLS + 1)),
                        dict(full(), at="x" * 513), dict(full(), at={"large": Unreadable()})):
            with self.subTest(kind=type(invalid["at"])):
                self.assertIsNone(lane.prepare(invalid))
        bad = full()
        bad["summary"][0]["price"] = float("nan")
        self.assertIsNone(lane.prepare(bad))
        self.assertFalse(lane.submit(None))
        self.assertFalse(lane.snapshot()["worker_alive"])
        self.assertEqual(lane.snapshot()["request_status"], "ERROR")

    def test_blocked_archive_allows_submission_and_coalesces_one_pending_full(self):
        ns, lane = self.lane()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        captured = []
        def archive(cycle_snapshot, admission):
            captured.append(deepcopy(cycle_snapshot))
            if len(captured) == 1:
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test did not release archive")
            return {"status": "SAVED"}
        ns["save_product_snapshot"] = archive
        self.assertTrue(lane.submit(full("FIRST")))
        self.assertTrue(entered.wait(1))
        worker = lane._thread
        for i in range(100):
            self.assertTrue(lane.submit(full("PENDING_" + str(i), float(i))))
        state = lane.snapshot()
        self.assertEqual(state["active_cycle_at"], "FIRST")
        self.assertEqual(state["pending_cycle_at"], "PENDING_99")
        self.assertEqual(state["pending"], 1)
        self.assertEqual(state["coalesced"], 99)
        self.assertIs(lane._thread, worker)
        self.assertEqual(len(captured), 1)
        self.assertFalse(release.is_set())
        release.set()
        self.wait_for(lambda: lane.snapshot()["runs"] == 2)
        self.assertEqual([x["at"] for x in captured], ["FIRST", "PENDING_99"])
        self.assertEqual(captured[1]["summary"][0]["price"], 99.)

    def test_memory_deferred_request_remains_visible_and_retries_without_new_full(self):
        ns, lane = self.lane()
        memory = [379.]
        ns["rss_mb"] = lambda: memory[0]
        archive_calls = []
        ns["save_product_snapshot"] = lambda **kw: archive_calls.append(kw) or {"status": "SAVED"}
        lane.submit(full())
        self.wait_for(lambda: len(lane.snapshot()["stages"]) == 4)
        state = lane.snapshot()
        self.assertEqual(state["runs"], 0)
        self.assertTrue(all(s["status"] == "DEFERRED_MEMORY" for s in state["stages"].values()))
        self.assertEqual(state["active_cycle_at"], full()["at"])
        self.assertGreaterEqual(state["active_age_seconds"], 0)
        self.assertEqual(archive_calls, [])
        memory[0] = 200.
        self.wait_for(lambda: lane.snapshot()["runs"] == 1)
        self.assertEqual(len(archive_calls), 1)

    def test_unknown_memory_defers_and_does_not_run_any_history(self):
        ns, lane = self.lane()
        ns["rss_mb"] = lambda: None
        result = lane._attempt("snapshot", lane.prepare(full()))
        self.assertEqual(result["status"], "DEFERRED_MEMORY_UNKNOWN")

    def test_database_unavailable_does_not_confirm_or_drop_archive(self):
        ns, lane = self.lane()
        ns["pg_enabled"] = lambda: False
        lane.submit(full())
        self.wait_for(lambda: len(lane.snapshot()["stages"]) == 4)
        self.assertTrue(all(x["status"] == "DEFERRED_STORAGE" for x in lane.snapshot()["stages"].values()))
        self.assertEqual(lane.snapshot()["runs"], 0)
        ns["pg_enabled"] = lambda: True
        self.wait_for(lambda: lane.snapshot()["runs"] == 1)

    def test_failed_snapshot_does_not_block_other_stages_or_repeat_completed_writes(self):
        ns, lane = self.lane(retry_seconds=.1)
        counts = {stage: 0 for stage in M.STAGES}
        def archive(**kw):
            counts["snapshot"] += 1
            if counts["snapshot"] == 1:
                raise RuntimeError("isolated archive failure")
            return {"status": "SAVED"}
        def checkpoint(lp):
            counts["daily_checkpoint"] += 1
            return {"status": "OK"}
        def retention():
            counts["retention"] += 1
            return {"status": "OK"}
        def outcomes(reason):
            counts["outcomes"] += 1
            ns["_v90_outcome_state"].update(status="OK")
        ns.update(save_product_snapshot=archive, _v90_daily_intelligence_metrics=checkpoint,
                  _v90r37_storage_retention=retention, _v90_run_outcome_refresh=outcomes)
        lane.submit(full())
        self.wait_for(lambda: lane.snapshot()["runs"] == 1)
        self.assertEqual(counts, {"snapshot": 2, "daily_checkpoint": 1, "retention": 1, "outcomes": 1})
        errors = [kw for e, kw in ns["events"] if kw.get("status") == "ERROR"]
        self.assertEqual(len(errors), 1)
        self.assertIn("isolated archive failure", errors[0]["error"])

    def test_none_or_invalid_callback_result_is_not_success(self):
        ns, lane = self.lane()
        request = lane.prepare(full())
        for result in (None, {}, {"status": {}}, {"status": "ERROR", "error": "x" * 10000}):
            ns["save_product_snapshot"] = lambda **kw: result
            out = lane._attempt("snapshot", request)
            self.assertEqual(out["status"], "ERROR")
            self.assertLessEqual(len(out["error"]), 512)

    def test_string_enum_result_cannot_kill_worker_during_projection(self):
        class Status(StrEnum):
            SAVED = "SAVED"
        ns, lane = self.lane()
        ns["save_product_snapshot"] = lambda **kw: {"status": Status.SAVED}
        lane.submit(full())
        self.wait_for(lambda: lane.snapshot()["runs"] == 1)
        self.assertEqual(lane.snapshot()["last_stages"]["snapshot"]["status"], "SAVED")

    def test_persistently_deferred_archive_cannot_starve_new_full_maintenance(self):
        ns, lane = self.lane(retry_seconds=.05)
        calls = {key: 0 for key in M.STAGES}
        quota = [True]
        snapshots = []
        def archive(cycle_snapshot, admission):
            calls["snapshot"] += 1
            snapshots.append(cycle_snapshot["at"])
            return {"status": "DEFERRED_STORAGE_QUOTA" if quota[0] else "SAVED"}
        def daily(lp):
            calls["daily_checkpoint"] += 1
            return {"status": "OK"}
        def retention():
            calls["retention"] += 1
            return {"status": "OK"}
        def outcomes(reason):
            calls["outcomes"] += 1
            ns["_v90_outcome_state"].update(status="OK")
        ns.update(save_product_snapshot=archive, _v90_daily_intelligence_metrics=daily,
                  _v90r37_storage_retention=retention, _v90_run_outcome_refresh=outcomes)
        lane.submit(full("A"))
        self.wait_for(lambda: calls["outcomes"] == 1)
        lane.submit(full("B"))
        self.wait_for(lambda: calls["outcomes"] == 2)
        lane.submit(full("C"))
        self.wait_for(lambda: calls["outcomes"] == 3)
        state = lane.snapshot()
        self.assertEqual(state["active_cycle_at"], "A")
        self.assertEqual(state["pending_cycle_at"], "C")
        self.assertEqual(state["stages"]["outcomes"]["cycle_at"], "C")
        self.assertEqual(set(snapshots), {"A"})
        self.assertEqual(calls["daily_checkpoint"], 3)
        self.assertEqual(calls["retention"], 3)
        self.assertEqual(state["runs"], 0)
        quota[0] = False
        self.wait_for(lambda: lane.snapshot()["runs"] == 2)
        self.assertEqual(snapshots[-2:], ["A", "C"])
        self.assertEqual(calls["outcomes"], 3, "already completed C outcome pass must not run twice")
        self.assertEqual(calls["daily_checkpoint"], 3)

    def test_callback_result_graph_is_released_before_telemetry_is_retained(self):
        ns, lane = self.lane()
        refs = []
        def archive(**kw):
            proof = Unreadable()
            refs.append(weakref.ref(proof))
            return {"status": "SAVED", "full_history": proof}
        ns["save_product_snapshot"] = archive
        lane.submit(full())
        self.wait_for(lambda: lane.snapshot()["runs"] == 1)
        self.assertIsNone(refs[0]())
        self.assertNotIn("full_history", str(lane.snapshot()))

    def test_result_graph_is_released_before_another_history_reader_can_acquire_permit(self):
        ns, lane = self.lane()
        refs = []
        owner = self
        class CheckedPermit:
            def __init__(self):
                self.lock = threading.RLock()
            def acquire(self, blocking=False):
                return self.lock.acquire(blocking=blocking)
            def release(self):
                owner.assertIsNone(refs[0](), "reader result outlived its resource permit")
                self.lock.release()
        def archive(**kw):
            proof = Unreadable()
            refs.append(weakref.ref(proof))
            return {"status": "SAVED", "proof": proof}
        lane._permit = CheckedPermit()
        ns["save_product_snapshot"] = archive
        self.assertEqual(lane._attempt("snapshot", lane.prepare(full()))["status"], "SAVED")

    def test_unexpected_worker_failure_is_reported_and_new_full_restarts_with_pending_work(self):
        ns, lane = self.lane()
        original = lane._work
        with patch.object(lane, "_work", side_effect=RuntimeError("isolated worker error")):
            lane.submit(full("A"))
            self.wait_for(lambda: not lane.snapshot()["worker_alive"])
        self.assertEqual(lane.snapshot()["status"], "ERROR_WORKER")
        self.assertEqual(lane.snapshot()["pending"], 1)
        self.assertEqual(lane._work, original)
        lane.submit(full("B"))
        self.wait_for(lambda: lane.snapshot()["runs"] == 1)
        self.assertEqual(lane.snapshot()["last_cycle_at"], "B")

    def test_thread_start_failure_is_fail_soft_and_next_submission_recovers(self):
        ns, lane = self.lane()
        request = lane.prepare(full())
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("thread budget")):
            self.assertFalse(lane.submit(request))
        self.assertEqual(lane.snapshot()["pending"], 1)
        self.assertEqual(lane.snapshot()["request_status"], "ERROR")
        self.assertTrue(lane.submit(request))
        self.wait_for(lambda: lane.snapshot()["runs"] == 1)

    def test_thread_constructor_failure_also_stays_off_market_path(self):
        ns, lane = self.lane()
        with patch.object(threading, "Thread", side_effect=RuntimeError("thread construction")):
            self.assertFalse(lane.submit(full()))
        self.assertEqual(lane.snapshot()["pending"], 1)
        self.assertEqual(lane.snapshot()["request_status"], "ERROR")

    def test_retry_backoff_survives_bursts_of_new_requests(self):
        ns, lane = self.lane(retry_seconds=2.)
        ns["rss_mb"] = lambda: 500.
        lane.submit(full())
        self.wait_for(lambda: len(lane.snapshot()["stages"]) == 4)
        attempted = {key: value["attempted_at"] for key, value in lane.snapshot()["stages"].items()}
        for i in range(50):
            lane.submit(full(str(i)))
        self.assertEqual({key: value["attempted_at"] for key, value in lane.snapshot()["stages"].items()}, attempted)

    def test_shared_permit_is_reentrant_and_released_after_failure(self):
        ns, lane = self.lane()
        nested = []
        with lane.permit("outer"):
            with lane.permit("inner"):
                nested.append(lane.snapshot()["resource_owner"])
        self.assertEqual(nested, ["outer"])
        self.assertIsNone(lane.snapshot()["resource_owner"])
        with self.assertRaises(RuntimeError):
            with lane.permit("failed"):
                raise RuntimeError("isolated")
        with lane.permit("next"):
            self.assertEqual(lane.snapshot()["resource_owner"], "next")

    def test_checkpoint_receives_completed_value_and_does_not_launch_implicit_refresh(self):
        ns, lane = self.lane()
        supplied = []
        ns["_v90_daily_intelligence_metrics"] = lambda lp: supplied.append(lp) or {"status": "OK"}
        out = lane._attempt("daily_checkpoint", lane.prepare(full()))
        self.assertEqual(out["status"], "OK")
        self.assertIs(supplied[0], ns["learning_progress"]._cache[1])

    def test_missing_learning_is_explicitly_deferred_without_persisting_fake_baseline(self):
        ns, lane = self.lane()
        del ns["learning_progress"]._cache
        ns["_learning_progress_refresh_sync"] = lambda reason: {"status": "BUILDING"}
        ns["_v90_daily_intelligence_metrics"] = lambda **kw: self.fail("invalid baseline persisted")
        out = lane._attempt("daily_checkpoint", lane.prepare(full()))
        self.assertEqual(out["status"], "DEFERRED_LEARNING")

    def test_stale_learning_must_refresh_before_new_daily_checkpoint(self):
        ns, lane = self.lane()
        old = ns["learning_progress"]._cache[1]
        ns["learning_progress"]._cache = (0., old)
        calls = []
        def refresh(reason):
            calls.append(reason)
            value = dict(old, index_vs_start=102.)
            ns["learning_progress"]._cache = (time.time(), value)
            return value
        ns["_learning_progress_refresh_sync"] = refresh
        supplied = []
        ns["_v90_daily_intelligence_metrics"] = lambda lp: supplied.append(lp) or {"status": "OK"}
        self.assertEqual(lane._checkpoint()["status"], "OK")
        self.assertEqual(calls, ["maintenance_checkpoint"])
        self.assertEqual(supplied[0]["index_vs_start"], 102.)
        ns["learning_progress"]._cache = (0., old)
        ns["_learning_progress_refresh_sync"] = lambda reason: dict(old, refresh_status="DEFERRED_BUSY")
        self.assertEqual(lane._checkpoint()["status"], "DEFERRED_LEARNING")
        self.assertEqual(len(supplied), 1)


class InstalledPermitTests(unittest.TestCase):
    wait_for = MaintenanceTests.wait_for
    def installed(self):
        ns = namespace()
        calls = []
        ns.update(_v90r63_calibration_refresh_lock=threading.Lock(),
                  _v90r63_perf_refresh_lock=threading.Lock(),
                  _v90r63_context_refresh_lock=threading.Lock(),
                  _v90r63_calibration_refresh_inflight=True,
                  _v90r63_perf_refresh_inflight=True,
                  _v90r63_context_refresh_inflight={"clock": True, "analogs": True, "trend_cases": True},
                  _v90r61_predecision_cache={"trend_cases": (123., {"penalty": .7})},
                  heavy_learning_state_lock=threading.Lock(), heavy_learning_state={"status": "NOT_RUN"},
                  _v90r63_refresh_calibration=lambda: calls.append("calibration"),
                  _v90r63_refresh_agent_perf=lambda: calls.append("agent_perf"),
                  _v90r63_context_refresh=lambda key: calls.append(key),
                  run_heavy_learning_maintenance=lambda reason="scheduled": ns["_learning_progress_refresh_sync"](reason))
        lane = M.install(ns)
        self.addCleanup(lane.close)
        return ns, lane, calls

    def test_background_histories_defer_while_archive_runs_but_source_clock_does_not(self):
        ns, lane, calls = self.installed()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def archive(**kw):
            entered.set()
            release.wait(2)
            return {"status": "SAVED"}
        ns["save_product_snapshot"] = archive
        lane.submit(full())
        self.assertTrue(entered.wait(1))
        original_cache = deepcopy(ns["_v90r61_predecision_cache"])
        ns["_v90r63_refresh_calibration"]()
        ns["_v90r63_refresh_agent_perf"]()
        ns["_v90r63_context_refresh"]("trend_cases")
        ns["_v90r63_context_refresh"]("analogs")
        ns["_v90r63_context_refresh"]("clock")
        out = ns["run_heavy_learning_maintenance"]("test")
        learned = ns["_learning_progress_refresh_sync"]("test")
        self.assertEqual(out["status"], "DEFERRED_BUSY")
        self.assertEqual(learned["refresh_status"], "DEFERRED_BUSY")
        self.assertEqual(learned["index_vs_start"], 101.)
        self.assertEqual(ns["_v90r61_predecision_cache"], original_cache)
        self.assertFalse(ns["_v90r63_calibration_refresh_inflight"])
        self.assertFalse(ns["_v90r63_perf_refresh_inflight"])
        self.assertFalse(ns["_v90r63_context_refresh_inflight"]["trend_cases"])
        self.assertFalse(ns["_v90r63_context_refresh_inflight"]["analogs"])
        self.assertEqual(calls, ["clock"])
        release.set()
        self.wait_for(lambda: lane.snapshot()["runs"] == 1)
        ns["_v90r63_refresh_agent_perf"]()
        self.assertEqual(calls, ["clock", "agent_perf"])

    def test_heavy_nested_learning_acquires_reentrant_permit_and_preserves_260_limit(self):
        ns, lane, calls = self.installed()
        self.assertEqual(ns["run_heavy_learning_maintenance"]("nested")["index_vs_start"], 101.)
        ns["rss_mb"] = lambda: 270.
        result = ns["run_heavy_learning_maintenance"]("over_budget")
        self.assertEqual(result["status"], "DEFERRED_MEMORY")
        self.assertEqual(result["memory_start_limit_mb"], 260.)
        self.assertIsNone(lane.snapshot()["resource_owner"])
        self.assertIs(M.install(ns), lane)


if __name__ == "__main__":
    unittest.main()
