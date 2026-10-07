"""Bounded archival work and periodic learning share one history resource.

Market decisions, source clocks and position protection never acquire this
permit. A request owns only a frozen scalar FULL matrix, not live proof graphs.
Existing snapshot/retention intervals and statistical populations stay in their
canonical callbacks. Deferred work remains visible and is retried, not discarded.
Small registered learning batches can use measured physical headroom without
changing the existing heavy-history admission guard.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
import json
import math
import re
import threading
import time
import traceback

VERSION = "BOUNDED_BACKGROUND_MAINTENANCE_V2"
MAX_CELLS = 128
MAX_REQUEST_BYTES = 200_000
CYCLE_FIELDS = ("status", "at", "version", "cycle_mode", "signal_cells",
                "decisions_written", "outcomes_written")
ROW_FIELDS = ("asset", "horizon", "decision", "research_decision", "confidence",
              "price", "regime", "signal_tier", "execution_eligible",
              "decision_stage", "horizon_structure_state", "horizon_structure_score",
              "entry_quality", "stop_price", "target_price", "expected_move_pct",
              "expected_to_stop_ratio")
STAGES = ("snapshot", "daily_checkpoint", "retention", "outcomes")
SUCCESS = {"OK", "SAVED", "NOT_DUE", "NOT_DUE_QUOTA_GUARD"}
PERIODIC_SUCCESS = SUCCESS | {"IDLE", "NO_WORK", "COMPLETE", "PROGRESS"}
MAX_PERIODIC_JOBS = 16
LIGHT_RESERVE_MB = 64.0
MAX_LIGHT_WORKING_SET_MB = 32.0


def _finite_number(value, name, minimum, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"invalid {name}")
    return float(value)


def _periodic_result(result):
    """Keep only small scalar progress; a failed query is never a completed job."""
    value = _stage_result(result)
    status = value["status"]
    if status in PERIODIC_SUCCESS and isinstance(result, dict):
        bad = result.get("error") or result.get("last_error") or result.get("error_code")
        negative = ("ERROR", "DEFERRED", "UNAVAILABLE", "RETRY", "DEGRADED", "FAILED", "TIMEOUT")
        children = [result.get("stage_status"), result.get("query_status")]
        for key in ("result", "stage_result"):
            child_result = result.get(key)
            if isinstance(child_result, dict):
                children.append(child_result.get("status"))
                bad = bad or child_result.get("error") or child_result.get("error_code")
        failed = next((str(child).upper() for child in children
                       if isinstance(child, str) and str(child).upper().startswith(negative)), None)
        if bad or failed or result.get("ok") is False:
            value = {"status": "ERROR", "error": str(bad or failed or "callback reported ok=false")[:512]}
    if isinstance(result, dict):
        for key in ("stage", "processed", "written", "eligible", "excluded", "pending",
                    "batches", "retry_after_seconds", "error_code", "reason"):
            item = result.get(key)
            if type(item) in (int, float) and math.isfinite(item):
                value[key] = item
            elif type(item) is str and len(item) <= 512:
                value[key] = item
            elif type(item) in (bool, type(None)):
                value[key] = item
    return value


def _scalar(value):
    if value is None or type(value) is bool:
        return value
    if type(value) in (int, float) and math.isfinite(value):
        return value
    if type(value) is str and len(value) <= 512:
        return value
    raise ValueError("archive projection requires bounded scalar fields")


def compact_cycle(cycle):
    """Project before copying; no iteration or serialization of unused proofs."""
    rows = cycle.get("summary") or []
    if not isinstance(rows, (list, tuple)) or len(rows) > MAX_CELLS:
        raise ValueError("archive matrix exceeds its bounded request contract")
    result = {key: _scalar(cycle.get(key)) for key in CYCLE_FIELDS}
    result["summary"] = [{key: _scalar(row.get(key)) for key in ROW_FIELDS}
                         for row in rows if isinstance(row, dict)]
    return result


def _stage_result(result):
    """Drop reader output before releasing the shared history permit."""
    if not isinstance(result, dict) or not isinstance(result.get("status"), str):
        return {"status": "ERROR", "error": "stage did not confirm a valid completion status"}
    out = {"status": str.__str__(result["status"])[:80].upper()}
    for key in ("error", "rss_mb", "memory_start_limit_mb"):
        value = result.get(key)
        if type(value) in (str, int, float, bool, type(None)):
            out[key] = value[:512] if type(value) is str else value
    return out


@dataclass(frozen=True)
class CycleRequest:
    payload: str
    cycle_at: str
    cycle_mode: str
    requested_at: float


@dataclass
class _Job:
    request: CycleRequest
    stages: dict = field(default_factory=dict)


@dataclass
class _PeriodicJob:
    name: str
    callback: object
    interval_seconds: float
    lightweight: bool
    estimated_peak_mb: float
    max_seconds: float
    retry_seconds: float
    next_run: float
    requested: bool = False
    running: bool = False
    attempts: int = 0
    completions: int = 0
    last_success_at: float | None = None
    blocked_since: float | None = None
    max_observed_peak_mb: float = 0.
    last_result: dict = field(default_factory=dict)


class MaintenanceDeferred(Exception):
    def __init__(self, status, **details):
        super().__init__(status)
        self.status, self.details = status, details

    def result(self):
        return {"status": self.status, **self.details}


class MaintenanceLane:
    def __init__(self, ns, retry_seconds=15.0, error_retry_seconds=60.0):
        self.ns, self.retry_seconds = ns, max(.01, float(retry_seconds))
        self.error_retry_seconds = max(self.retry_seconds, float(error_retry_seconds))
        self._condition = threading.Condition()
        self._permit = threading.RLock()
        self._local = threading.local()
        self._thread = self._active = self._pending = None
        self._closed = False
        self._state = {"status": "IDLE", "requests": 0, "coalesced": 0,
                       "runs": 0, "last_finished_at": None, "last_error": None}
        self._history_state = {}
        self._periodic = {}
        self._periodic_turn = 0
        self._scheduled_waiter = None
        self._last_light_trim = -math.inf

    def _start_locked(self):
        if self._thread is not None and self._thread.is_alive():
            return not self._closed
        self._closed = False
        try:
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="veritas-maintenance")
            self._thread.start()
            return True
        except Exception as exc:
            self._thread = None
            self._state.update(status="ERROR", last_error=f"{type(exc).__name__}: {exc}"[:512])
            self.emit("maintenance_worker_start_error", error=self._state["last_error"])
            return False

    def start_periodic(self):
        """Start the one worker, including when no FULL cycle or UI request exists.

        A stopped worker can resume its bounded in-process requests. Durable job
        cursors and leases belong to the registered callback and survive a process
        restart; this scheduler never substitutes its RAM counters for that state.
        """
        with self._condition:
            started = self._start_locked()
            self._condition.notify_all()
            return started

    def register_periodic(self, name, callback, interval_seconds=30, lightweight=False,
                          estimated_peak_mb=16, max_seconds=6, retry_seconds=None):
        """Register trusted, bounded work once; callbacks perform one small stage.

        Lightweight admission is only for projected SQL/compact reducers, never
        an alias around the existing heavy-learning or full archive callback.
        The callback must apply current_budget()['sql_timeout_ms'] to SQL and
        call check_budget() between batches; Python threads cannot be killed
        safely. A slow callback is never overlapped by a replacement worker.
        """
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", name):
            raise ValueError("invalid periodic job name")
        if not callable(callback) or type(lightweight) is not bool:
            raise ValueError("invalid periodic callback")
        interval = _finite_number(interval_seconds, "interval", .01, 86400.)
        estimate = _finite_number(estimated_peak_mb, "working set", 1., MAX_LIGHT_WORKING_SET_MB)
        duration = _finite_number(max_seconds, "time budget", .01, 30.)
        retry = self.retry_seconds if retry_seconds is None else _finite_number(retry_seconds, "retry", .01, 300.)
        with self._condition:
            if name in self._periodic:
                return False
            if len(self._periodic) >= MAX_PERIODIC_JOBS:
                raise ValueError("periodic job capacity exceeded")
            self._periodic[name] = _PeriodicJob(name, callback, interval, lightweight,
                                               estimate, duration, retry, time.monotonic())
            self._condition.notify_all()
        return True

    def request(self, name):
        """Coalesce demand without creating a thread or bypassing failed-job backoff."""
        with self._condition:
            job = self._periodic.get(name)
            if job is None or self._closed:
                return False
            job.requested = True
            status = job.last_result.get("status")
            if not job.running and (not status or status in PERIODIC_SUCCESS):
                job.next_run = min(job.next_run, time.monotonic())
            self._condition.notify_all()
            return True

    def current_budget(self):
        value = getattr(self._local, "budget", None)
        if value is None:
            return {}
        remaining = max(0., value["deadline"] - time.monotonic())
        return {key: item for key, item in value.items() if key != "deadline"} | {
            "remaining_seconds": remaining,
            "sql_timeout_ms": max(1, min(2000, int(remaining * 1000))),
        }

    def check_budget(self):
        """Cooperative boundary before/after each bounded SQL or reduction batch."""
        value = getattr(self._local, "budget", None)
        if value is None:
            return
        if self._closed:
            raise MaintenanceDeferred("STOPPED", reason="scheduler_stopped")
        rss = self.ns["rss_mb"]()
        if rss is None or not math.isfinite(float(rss)):
            raise MaintenanceDeferred("DEFERRED_MEMORY_UNKNOWN")
        rss = float(rss)
        value["peak_rss_mb"] = max(value["peak_rss_mb"], rss)
        value["observed_peak_mb"] = max(0., value["peak_rss_mb"] - value["start_rss_mb"])
        if rss > value["max_rss_mb"]:
            raise MaintenanceDeferred("DEFERRED_MEMORY_BUDGET", rss_mb=rss,
                                      memory_start_limit_mb=value["max_rss_mb"])
        if time.monotonic() >= value["deadline"]:
            raise MaintenanceDeferred("DEFERRED_TIME_BUDGET", reason="batch_deadline")

    def emit(self, event, **values):
        try:
            self.ns["emit"](event, maintenance_version=VERSION, **values)
        except Exception:
            pass

    def prepare(self, cycle):
        """Called while publishing the FULL state, before its owners are freed."""
        try:
            value = compact_cycle(cycle)
            if value["cycle_mode"] != "FULL":
                return None
            payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
            if len(payload.encode("utf-8")) > MAX_REQUEST_BYTES:
                raise ValueError("archive request exceeds byte budget")
            return CycleRequest(payload, value["at"], "FULL", time.time())
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:512]
            with self._condition:
                self._state.update(last_error=error, request_status="ERROR")
            self.emit("maintenance_request_error", error=error)
            return None

    def submit(self, request):
        if isinstance(request, dict):
            request = self.prepare(request)
        if request is None:
            return False
        if not isinstance(request, CycleRequest):
            raise TypeError("a frozen cycle request is required")
        with self._condition:
            if self._closed:
                return False
            self._state["requests"] += 1
            self._state["coalesced"] += int(self._pending is not None)
            self._pending = request
            self._state["request_status"] = "QUEUED"
            if not self._start_locked():
                self._state["request_status"] = "ERROR"
                return False
            self._condition.notify_all()
        self.emit("maintenance_requested", cycle_at=request.cycle_at,
                  cycle_mode=request.cycle_mode)
        return True

    def memory_limit(self):
        return min(float(self.ns.get("MEMORY_SOFT_LIMIT_MB", 280)),
                   float(self.ns.get("V90_MEMORY_CAUTION_MB", 280)))

    def admit(self, stage, limit=None):
        rss = self.ns["rss_mb"]()
        ceiling = self.memory_limit() if limit is None else float(limit)
        if rss is None or not math.isfinite(float(rss)):
            raise MaintenanceDeferred("DEFERRED_MEMORY_UNKNOWN", stage=stage)
        if float(rss) > ceiling:
            raise MaintenanceDeferred("DEFERRED_MEMORY", stage=stage,
                                      rss_mb=rss, memory_start_limit_mb=ceiling)

    @contextmanager
    def _resource(self, owner, limit=None, trim=False):
        depth = getattr(self._local, "depth", 0)
        # A ready scheduler callback gets the next free history slot. The clock
        # bypass and position-protection paths never use this history resource.
        with self._condition:
            if depth == 0 and self._scheduled_waiter not in (None, owner):
                raise MaintenanceDeferred("DEFERRED_BUSY", owner=owner)
        if not self._permit.acquire(blocking=False):
            raise MaintenanceDeferred("DEFERRED_BUSY", owner=owner)
        self._local.depth = depth + 1
        try:
            if trim and time.monotonic() - self._last_light_trim >= 30.:
                rss = self.ns["rss_mb"]()
                trimmer = self.ns.get("_v90_trim_memory")
                if rss is not None and float(rss) > float(limit) and callable(trimmer):
                    self._last_light_trim = time.monotonic()
                    # Never evict active market objects or completed learning
                    # snapshots merely to admit a background batch.
                    trimmer("learning_admission", force=True, preserve_active_cycle=True)
            self.admit(owner, limit)
            if depth == 0:
                with self._condition:
                    self._state.update(resource_owner=owner, resource_started_at=time.time())
            yield
        finally:
            self._local.depth = depth
            if depth == 0:
                with self._condition:
                    self._state.update(resource_owner=None, resource_started_at=None)
            self._permit.release()

    @contextmanager
    def permit(self, owner, limit=None):
        with self._resource(owner, limit):
            yield

    @contextmanager
    def _periodic_permit(self, job):
        owner = "periodic:" + job.name
        hard_limit = min(512., _finite_number(
            self.ns.get("V90_MEMORY_HARD_LIMIT_MB", 512.), "physical memory limit", 128., 65536.))
        ceiling = hard_limit - LIGHT_RESERVE_MB - job.estimated_peak_mb if job.lightweight else self.memory_limit()
        with self._resource(owner, ceiling, trim=job.lightweight):
            rss = float(self.ns["rss_mb"]())
            previous = getattr(self._local, "budget", None)
            self._local.budget = {
                "job": job.name, "lightweight": job.lightweight,
                "max_seconds": job.max_seconds, "deadline": time.monotonic() + job.max_seconds,
                "start_rss_mb": rss, "peak_rss_mb": rss, "observed_peak_mb": 0.,
                "estimated_peak_mb": job.estimated_peak_mb,
                "memory_start_limit_mb": ceiling,
                "max_rss_mb": min(hard_limit - LIGHT_RESERVE_MB, rss + job.estimated_peak_mb),
            }
            try:
                self.check_budget()
                yield
            finally:
                self._local.budget = previous

    def _periodic_delay_locked(self):
        due = [job.next_run - time.monotonic() for job in self._periodic.values() if not job.running]
        return max(.01, min(due, default=30.))

    def _run_periodic_once(self):
        with self._condition:
            jobs = list(self._periodic.values())
            if self._closed or not jobs:
                return False
            job = None
            for offset in range(len(jobs)):
                index = (self._periodic_turn + offset) % len(jobs)
                candidate = jobs[index]
                if not candidate.running and candidate.next_run <= time.monotonic():
                    job = candidate
                    self._periodic_turn = (index + 1) % len(jobs)
                    break
            if job is None:
                return False
            job.running, job.requested = True, False
            job.attempts += 1
            owner = "periodic:" + job.name
            self._scheduled_waiter = owner
        started = time.monotonic()
        attempted_at = time.time()
        result, budget = None, {}
        try:
            with self._periodic_permit(job):
                raw = None
                try:
                    raw = job.callback()
                    result = _periodic_result(raw)
                    raw = None
                    self.check_budget()
                except MaintenanceDeferred as exc:
                    result = _periodic_result(exc.result())
                    traceback.clear_frames(exc.__traceback__)
                except Exception as exc:
                    result = {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"[:512],
                              "error_code": type(exc).__name__}
                    traceback.clear_frames(exc.__traceback__)
                finally:
                    raw = None
                    budget = self.current_budget()
        except MaintenanceDeferred as exc:
            result = _periodic_result(exc.result())
        except Exception as exc:
            result = {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"[:512],
                      "error_code": type(exc).__name__}
            traceback.clear_frames(exc.__traceback__)
        finally:
            with self._condition:
                if self._scheduled_waiter == owner:
                    # Keep a reservation after contention: a stream of ad-hoc
                    # history reads must not repeatedly barge ahead of a due
                    # learning job. Market/protection paths do not use it.
                    if self._closed or not result or result.get("status") != "DEFERRED_BUSY":
                        self._scheduled_waiter = None
        result = result or {"status": "ERROR", "error": "missing periodic completion"}
        result.update(duration_seconds=round(time.monotonic() - started, 4),
                      attempted_at=attempted_at, finished_at=time.time())
        for key in ("start_rss_mb", "peak_rss_mb", "observed_peak_mb", "estimated_peak_mb",
                    "memory_start_limit_mb", "max_rss_mb"):
            if key in budget:
                result[key] = round(budget[key], 3)
        with self._condition:
            old = job.last_result
            job.last_result = result
            job.running = False
            succeeded = result["status"] in PERIODIC_SUCCESS
            delay = job.interval_seconds if succeeded else (
                self.error_retry_seconds if result["status"] == "ERROR" else job.retry_seconds)
            retry = result.get("retry_after_seconds")
            if not succeeded and type(retry) in (int, float) and math.isfinite(retry):
                delay = max(delay, min(300., max(0., retry)))
            if succeeded:
                job.completions += 1
                job.last_success_at = time.time()
                job.blocked_since = None
                if job.requested:
                    delay = min(delay, max(.01, job.retry_seconds))
            elif job.blocked_since is None:
                job.blocked_since = attempted_at
            job.max_observed_peak_mb = max(job.max_observed_peak_mb,
                                          result.get("observed_peak_mb", 0.))
            job.next_run = time.monotonic() + delay
            result["next_attempt_at"] = time.time() + delay
            self._condition.notify_all()
        if succeeded or old.get("status") != result["status"]:
            self.emit("maintenance_periodic_complete" if succeeded else "maintenance_periodic_deferred",
                      job=job.name, **result)
        return True

    def _completed_learning(self):
        reader = self.ns["learning_progress"]
        cache = getattr(reader, "_cache", None)
        try:
            value, age = cache[1], time.time() - float(cache[0])
            ttl = max(60., float(self.ns.get("ANALYTICS_CACHE_SECONDS", 60.)))
            if (math.isfinite(age) and -30. <= age < ttl and isinstance(value, dict)
                    and value.get("index_version") == self.ns["VLI"].INDEX_VERSION):
                return value
        except (TypeError, ValueError, IndexError):
            pass
        return None

    def _checkpoint(self):
        value = self._completed_learning()
        if value is None:
            self.ns["_learning_progress_refresh_sync"]("maintenance_checkpoint")
            value = self._completed_learning()
        if value is None:
            return {"status": "DEFERRED_LEARNING"}
        # An explicit completed value avoids starting an uncoordinated refresh.
        return self.ns["_v90_daily_intelligence_metrics"](lp=value)

    def _call_stage(self, stage, request):
        if not self.ns["pg_enabled"]():
            return {"status": "DEFERRED_STORAGE"}
        if stage == "snapshot":
            return self.ns["save_product_snapshot"](
                cycle_snapshot=json.loads(request.payload), admission=self.admit)
        if stage == "daily_checkpoint":
            return self._checkpoint()
        if stage == "retention":
            return self.ns["_v90r37_storage_retention"]()
        self.ns["_v90_run_outcome_refresh"]("full_cycle_maintenance")
        return {"status": self.ns["_v90_outcome_state"].get("status", "ERROR")}

    def _attempt(self, stage, request):
        started = time.monotonic()
        try:
            with self.permit("maintenance:" + stage):
                self.emit("maintenance_stage_start", stage=stage,
                          cycle_at=request.cycle_at, cycle_mode=request.cycle_mode,
                          rss_mb=self.ns["rss_mb"]())
                result = _stage_result(self._call_stage(stage, request))
        except MaintenanceDeferred as exc:
            result = _stage_result(exc.result())
        except Exception as exc:
            result = {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"[:512]}
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        result["attempted_at"] = time.time()
        delay = self.error_retry_seconds if result["status"] == "ERROR" else self.retry_seconds
        result["retry_at_monotonic"] = time.monotonic() + delay
        result["cycle_at"] = request.cycle_at
        return result

    def _run(self):
        try:
            self._work()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:512]
            with self._condition:
                self._state.update(status="ERROR_WORKER", last_error=error)
            self.emit("maintenance_worker_error", error=error)

    def _work(self):
        while True:
            with self._condition:
                while not self._closed and self._active is None and self._pending is None and not self._periodic:
                    self._condition.wait()
                if self._closed:
                    return
                if self._active is None and self._pending is not None:
                    self._active, self._pending = _Job(self._pending), None
                job = self._active
            if job is None:
                ran = self._run_periodic_once()
                with self._condition:
                    if self._closed:
                        return
                    if self._active is None and self._pending is None:
                        # More ready jobs get their round-robin turn immediately.
                        delay = self._periodic_delay_locked()
                        if not ran or delay > .01:
                            self._condition.wait(delay)
                continue
            for stage in STAGES:
                # Alternate a bounded periodic callback with every FULL stage.
                # A continuously due microjob cannot starve archive progress,
                # and a repeatedly deferred old archive cannot starve learning.
                self._run_periodic_once()
                with self._condition:
                    # An old deferred archive must not starve the latest due
                    # checkpoint, retention or outcome pass. Only their scalar
                    # completion stamps are retained; the queue is still one.
                    request = job.request if stage == "snapshot" else (self._pending or job.request)
                old = job.stages.get(stage, {})
                if old.get("status") in SUCCESS and old.get("cycle_at") == request.cycle_at:
                    continue
                if old.get("status") not in SUCCESS and time.monotonic() < old.get("retry_at_monotonic", 0):
                    continue
                with self._condition:
                    if self._closed:
                        return
                    self._state.update(status="RUNNING", stage=stage)
                result = self._attempt(stage, request)
                with self._condition:
                    job.stages[stage] = result
                    self._state.update(status=result["status"], last_error=result.get("error"))
                # A persistent memory deferral must not flood the runtime log.
                if old.get("status") != result["status"] or result["status"] in SUCCESS:
                    event = "maintenance_stage_complete" if result["status"] in SUCCESS else "maintenance_stage_deferred"
                    self.emit(event, stage=stage, cycle_mode=request.cycle_mode, **result)
            with self._condition:
                latest_at = (self._pending or job.request).cycle_at
                complete = all(job.stages.get(stage, {}).get("status") in SUCCESS
                               and (stage == "snapshot" or job.stages[stage].get("cycle_at") == latest_at)
                               for stage in STAGES)
                if complete:
                    self._state.update(status="OK", stage=None, last_finished_at=time.time(),
                                       last_cycle_at=job.request.cycle_at, last_stages=dict(job.stages),
                                       runs=self._state["runs"] + 1)
                    self._active = _Job(self._pending) if self._pending else None
                    self._pending = None
                    if self._active is not None:
                        self._active.stages = {key: dict(value) for key, value in job.stages.items()
                                              if key != "snapshot" and value.get("cycle_at") == latest_at}
                else:
                    self._state["status"] = "DEFERRED"
            if complete:
                self.emit("maintenance_complete", cycle_at=job.request.cycle_at,
                          cycle_mode=job.request.cycle_mode)
                job = None
            else:
                with self._condition:
                    if not self._closed:
                        latest_at = (self._pending or job.request).cycle_at
                        remaining = [value["retry_at_monotonic"] - time.monotonic()
                                     if value["status"] not in SUCCESS else 0.
                                     for key, value in job.stages.items()
                                     if value["status"] not in SUCCESS
                                     or (key != "snapshot" and value.get("cycle_at") != latest_at)]
                        self._condition.wait(max(.01, min(
                            min(remaining, default=self.retry_seconds), self._periodic_delay_locked())))

    def snapshot(self):
        with self._condition:
            active, pending = self._active, self._pending
            stamp = time.time()
            state = dict(self._state)
            if "last_stages" in state:
                state["last_stages"] = {key: dict(value) for key, value in state["last_stages"].items()}
            return {"version": VERSION, **state,
                    "active_cycle_at": active.request.cycle_at if active else None,
                    "active_age_seconds": round(stamp - active.request.requested_at, 3) if active else None,
                    "stages": {key: dict(value) for key, value in active.stages.items()} if active else {},
                    "pending": int(pending is not None), "pending_limit": 1,
                    "pending_cycle_at": pending.cycle_at if pending else None,
                    "pending_age_seconds": round(stamp - pending.requested_at, 3) if pending else None,
                    "history": {key: dict(value) for key, value in self._history_state.items()},
                    "periodic": {name: {
                        "status": "RUNNING" if job.running else job.last_result.get("status", "QUEUED"),
                        "attempts": job.attempts, "completions": job.completions,
                        "last_success_at": job.last_success_at,
                        "blocked_age_seconds": round(max(0., stamp - job.blocked_since), 3)
                        if job.blocked_since is not None else None,
                        "max_observed_peak_mb": job.max_observed_peak_mb,
                        "capacity_review_required": bool(job.lightweight and (
                            job.max_observed_peak_mb > MAX_LIGHT_WORKING_SET_MB or (
                                job.blocked_since is not None and stamp - job.blocked_since >= 300.
                                and job.last_result.get("status", "").startswith("DEFERRED_MEMORY")))),
                        "next_in_seconds": round(max(0., job.next_run - time.monotonic()), 3),
                        "requested": job.requested, "lightweight": job.lightweight,
                        "estimated_peak_mb": job.estimated_peak_mb, "max_seconds": job.max_seconds,
                        "last_result": dict(job.last_result),
                    } for name, job in self._periodic.items()},
                    "periodic_limit": MAX_PERIODIC_JOBS,
                    "scheduled_resource_waiter": self._scheduled_waiter,
                    "worker_alive": bool(self._thread and self._thread.is_alive()),
                    "memory_start_limit_mb": self.memory_limit()}

    def close(self, timeout=1.0):
        with self._condition:
            self._closed = True
            self._scheduled_waiter = None
            self._condition.notify_all()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout)
        return not bool(self._thread and self._thread.is_alive())

    def coordinate(self, name, function, deferred, bypass=None, limit=None):
        @wraps(function)
        def wrapped(*args, **kwargs):
            if bypass is not None and bypass(*args, **kwargs):
                return function(*args, **kwargs)
            try:
                with self.permit(name, limit):
                    result = function(*args, **kwargs)
                with self._condition:
                    status = result.get("status", "FINISHED") if isinstance(result, dict) else "FINISHED"
                    self._history_state[name] = {"status": str(status)[:80], "at": time.time()}
                return result
            except MaintenanceDeferred as exc:
                value = exc.result()
                with self._condition:
                    old = self._history_state.get(name, {}).get("status")
                    self._history_state[name] = dict(value, at=time.time())
                if old != value["status"]:
                    self.emit("history_resource_deferred", reader=name, **value)
                return deferred(value, *args, **kwargs)
        return wrapped


def install(ns):
    """Install after the existing storage guard; callbacks are resolved at run time."""
    if "_v90_background_maintenance" in ns:
        return ns["_v90_background_maintenance"]
    lane = ns["_v90_background_maintenance"] = MaintenanceLane(ns)

    def reset_flag(flag, lock_name):
        def reset(value, *args, **kwargs):
            with ns[lock_name]:
                ns[flag] = False
            return value
        return reset

    def context_deferred(value, key):
        with ns["_v90r63_context_refresh_lock"]:
            ns["_v90r63_context_refresh_inflight"][key] = False
        return value

    def learning_deferred(value, *args, **kwargs):
        cache = getattr(ns["learning_progress"], "_cache", None)
        return dict(cache[1] if cache else {"status": "BUILDING"},
                    background_refresh=True, refresh_status=value["status"])

    def heavy_deferred(value, reason="scheduled"):
        with ns["heavy_learning_state_lock"]:
            ns["heavy_learning_state"].update(value, reason=reason)
        return value

    def outcome_deferred(value, *args, **kwargs):
        ns["_v90_outcome_state"].update(value)
        return value

    bindings = (
        ("_v90r63_refresh_calibration", reset_flag("_v90r63_calibration_refresh_inflight", "_v90r63_calibration_refresh_lock"), None, None),
        ("_v90r63_refresh_agent_perf", reset_flag("_v90r63_perf_refresh_inflight", "_v90r63_perf_refresh_lock"), None, None),
        ("_v90r63_context_refresh", context_deferred, lambda key: key == "clock", None),
        ("_learning_progress_refresh_sync", learning_deferred, None, None),
        ("run_heavy_learning_maintenance", heavy_deferred, None, ns.get("V90_HEAVY_LEARNING_MAX_START_MB", 260)),
        ("_v90_run_outcome_refresh", outcome_deferred, None, None),
    )
    for name, deferred, bypass, limit in bindings:
        ns[name] = lane.coordinate(name, ns[name], deferred, bypass, limit)
    return lane
