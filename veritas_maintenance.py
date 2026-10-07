"""Bounded archival work and one nonblocking permit for background history.

Market decisions, source clocks and position protection never acquire this
permit. A request owns only a frozen scalar FULL matrix, not live proof graphs.
Existing snapshot/retention intervals and statistical populations stay in their
canonical callbacks. Deferred work remains visible and is retried, not discarded.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
import json
import math
import threading
import time

VERSION = "BOUNDED_BACKGROUND_MAINTENANCE_V1"
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
            if self._thread is None or not self._thread.is_alive():
                try:
                    self._thread = threading.Thread(target=self._run, daemon=True,
                                                    name="veritas-maintenance")
                    self._thread.start()
                except Exception as exc:
                    self._thread = None
                    self._state.update(status="ERROR", request_status="ERROR",
                                       last_error=f"{type(exc).__name__}: {exc}"[:512])
                    self.emit("maintenance_worker_start_error", error=self._state["last_error"])
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
    def permit(self, owner, limit=None):
        if not self._permit.acquire(blocking=False):
            raise MaintenanceDeferred("DEFERRED_BUSY", owner=owner)
        depth = getattr(self._local, "depth", 0)
        self._local.depth = depth + 1
        try:
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
                while not self._closed and self._active is None and self._pending is None:
                    self._condition.wait()
                if self._closed:
                    return
                if self._active is None:
                    self._active, self._pending = _Job(self._pending), None
                job = self._active
            for stage in STAGES:
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
                        self._condition.wait(max(.01, min(remaining, default=self.retry_seconds)))

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
                    "worker_alive": bool(self._thread and self._thread.is_alive()),
                    "memory_start_limit_mb": self.memory_limit()}

    def close(self, timeout=1.0):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)

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
