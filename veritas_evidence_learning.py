"""Checkpoint the existing learning pipeline in its PostgreSQL event ledger.

One admitted stage runs at a time. Completed stages survive process restarts;
an interrupted stage is replayed at least once through its canonical upserts.
This coordinator has no execution, broker or strategy-promotion authority.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import threading
import time
import uuid

from veritas_maintenance import MaintenanceDeferred

VERSION = "EVIDENCE_LEARNING_LOOP_V1"
STATE_KEY = "evidence_learning_state:canonical"
LOCK_ID = 1736210907
STAGES = ("decision_episodes", "event_outcomes", "rule_statistics", "experience_lessons",
          "decision_memory", "verified_evidence", "quality_evaluation")
SUCCESS = {"OK", "DISABLED", "BUILDING", "MEASURABLE", "BOUNDED_RECENT_EPISODE_WINDOW"}
MAX_ATTEMPTS = 3
EVIDENCE_LIMIT = 64


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def compact(result):
    """Persist bounded numeric summaries, not raw proof graphs or exception text."""
    if not isinstance(result, dict):
        return {"status": "ERROR", "error_code": "INVALID_RESULT"}
    out = {"status": str(result.get("status") or "ERROR").upper()[:80]}
    for key in ("inserted", "batches", "written", "rows", "status_changes", "episodes_used",
                "trade_lessons", "rejected_lessons", "abstention_lessons", "archived",
                "learning_fallback", "shadow_covered", "eligible", "unique_market_episodes"):
        value = result.get(key)
        if type(value) in (int, float) and math.isfinite(value):
            out[key] = value
    if isinstance(result.get("items"), list):
        out["items_count"] = len(result["items"])
    if result.get("errors") or result.get("error") or result.get("last_error"):
        out.update(status="DEGRADED", error_code="STAGE_REPORTED_ERRORS")
    if result.get("background_refresh") or result.get("refresh_status"):
        out["status"] = str(result.get("refresh_status") or "DEFERRED_REFRESH").upper()
    paper = result.get("paper_execution_learning")
    if isinstance(paper, dict):
        out["paper_execution_learning"] = compact(paper)
        if out["paper_execution_learning"]["status"] not in SUCCESS:
            out.update(status="DEGRADED", error_code="PAPER_LESSONS_INCOMPLETE")
    return out


def quality_snapshot(result):
    fields = ("status", "index_version", "mode", "calculated_at", "index_vs_start",
              "matched_observations_each_side", "baseline", "current", "components",
              "component_weights", "component_points_vs_baseline")
    out = {key: result.get(key) for key in fields}
    out.update(scope="HISTORICAL_DIAGNOSTIC", causal_improvement_established=False,
               independent_holdout_test="NOT_PERFORMED_BY_THIS_PIPELINE")
    # A small allowlist cannot accidentally archive the shadow-trade graph.
    encoded = json.dumps(out, allow_nan=False)
    if len(encoded) > 12000:
        raise ValueError("quality summary exceeds budget")
    return json.loads(encoded)


class Ledger:
    def __init__(self, connect, model_version):
        self.connect, self.model_version = connect, model_version

    @contextmanager
    def claim(self):
        # The dedicated session owns the lock even while stage callbacks use
        # their own transactions. Connection death releases it automatically.
        with self.connect() as c:
            c.execute("SET statement_timeout TO '5s'")
            c.execute("SET lock_timeout TO '1s'")
            owned = c.execute("SELECT pg_try_advisory_lock(%s) AS owned", (LOCK_ID,)).fetchone()["owned"]
            try:
                yield c if owned else None
            finally:
                if owned:
                    c.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))

    def load(self, c):
        row = c.execute("SELECT payload FROM ledger_events WHERE event_key=%s", (STATE_KEY,)).fetchone()
        return deepcopy(row["payload"]) if row else None

    def save(self, c, state, event=None):
        # The checkpoint and its audit event commit together. A failed journal
        # write never advances the durable cursor past an unrecorded stage.
        encoded = json.dumps(state, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > 64000:
            raise ValueError("learning checkpoint exceeds budget")
        with c.transaction():
            c.execute("""INSERT INTO ledger_events
                (event_key,entity_key,event_type,event_ts,payload,model_version)
                VALUES(%s,'canonical','evidence_learning_state',now(),%s::jsonb,%s)
                ON CONFLICT(event_key) DO UPDATE SET event_ts=EXCLUDED.event_ts,
                  payload=EXCLUDED.payload,model_version=EXCLUDED.model_version""",
                (STATE_KEY, encoded, self.model_version))
            if event:
                key = state["run_id"] + ":" + event
                c.execute("""INSERT INTO ledger_events
                    (event_key,entity_key,event_type,event_ts,payload,model_version)
                    VALUES(%s,%s,'evidence_learning_audit',now(),%s::jsonb,%s)
                    ON CONFLICT(event_key) DO NOTHING""",
                    ("evidence_learning_audit:"+key, state["run_id"], encoded, self.model_version))


class LearningLoop:
    def __init__(self, ns, ledger=None, clock=time.time):
        self.ns, self.clock = ns, clock
        self.started_at = clock()
        self.ledger = ledger or Ledger(ns["pg_connect"], ns["VERSION"])
        self.lock, self.state_lock = threading.Lock(), threading.Lock()
        self.wake, self.stop = threading.Event(), threading.Event()
        self.thread = None
        self.state = {"status": "RESTORING", "protocol_version": VERSION, "durable": False}

    def publish(self, state):
        with self.state_lock:
            changed = self.state != state
            self.state = deepcopy(state)
        if changed:
            self.ns["emit"]("evidence_learning_checkpoint", status=state["status"],
                            run_id=state.get("run_id"), next_stage=state.get("next_stage"),
                            completed_stages=len(state.get("stages", {})), protocol_version=VERSION)

    def snapshot(self):
        with self.state_lock:
            state = deepcopy(self.state)
        state.update(scheduler_alive=bool(self.thread and self.thread.is_alive()),
                     interval_seconds=self.ns["HEAVY_LEARNING_INTERVAL_SECONDS"],
                     memory_start_limit_mb=self.ns["V90_HEAVY_LEARNING_MAX_START_MB"],
                     always_on_confirmed=bool(self.ns.get("PRODUCTION_ALWAYS_ON")),
                     learning_kind="VERIFIED_PAPER_MEMORY_AND_RULE_STATISTICS",
                     model_weights_trained=False, independent_improvement_established=False)
        for stage, key in (("event_outcomes", "event_learning"), ("rule_statistics", "rule_learning"),
                           ("experience_lessons", "experience_learning")):
            state[key] = state.get("stages", {}).get(stage, {"status": "PENDING"})
        return state

    def _fresh(self, now, previous=None):
        previous = previous or {}
        cache = getattr(self.ns.get("learning_progress"), "_cache", None)
        return {"protocol_version": VERSION, "run_id": uuid.uuid4().hex,
                "runtime_started_at": self.started_at,
                "model_version": self.ns["VERSION"], "deploy_sha": self.ns["VR"].deployment_sha(),
                "status": "SCHEDULED", "durable": True, "created_at": timestamp(now),
                "next_due_at": now + self.ns["HEAVY_LEARNING_START_DELAY_SECONDS"],
                "next_stage": STAGES[0], "stages": {}, "attempts": {},
                "runs": int(previous.get("runs", 0)), "last_finished_at": previous.get("last_finished_at"),
                "baseline": quality_snapshot(cache[1]) if cache and isinstance(cache[1], dict) else None,
                "retry_semantics": "AT_LEAST_ONCE_INTERRUPTED_STAGE_CANONICAL_UPSERTS",
                "counts_scope": "STAGE_OBSERVATIONS_NOT_NEW_INDEPENDENT_KNOWLEDGE"}

    def _stage(self, name):
        ns = self.ns
        if name == "decision_episodes":
            return compact(ns["_v90_backfill_decision_episodes"]())
        if name == "event_outcomes":
            return compact(ns["refresh_event_outcomes"]() if ns["EVENT_LEARNING_ENABLED"] else {"status": "DISABLED"})
        if name == "rule_statistics":
            return compact(ns["refresh_rule_stats"]())
        if name == "experience_lessons":
            return compact(ns["refresh_experience_lessons"]())
        if name == "decision_memory":
            return compact(ns["setup_memory_board"](force=True))
        if name == "quality_evaluation":
            value = ns["_learning_progress_refresh_sync"]("evidence_learning")
            return dict(compact(value), quality=quality_snapshot(value))
        import veritas_learning_exports as exports
        with ns["pg_connect"]() as c:
            c.execute("SET statement_timeout TO '5s'")
            rows = exports.decision_lessons(c, EVIDENCE_LIMIT)
        manifest = []
        for row in rows:
            p = row["payload"]
            manifest.append({"asset": row["asset"], "horizon": row["horizon"],
                             "event_id": p["learning_observed_event_id"],
                             "trade_evidence_hashes": p["learning_evidence_hashes"]})
        manifest.sort(key=lambda x: (x["asset"], x["horizon"], x["event_id"]))
        digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        return {"status": "OK", "verified_unique_episodes_in_window": len(manifest),
                "window_limit": EVIDENCE_LIMIT, "evidence_version": exports.LI.VERSION,
                "export_version": exports.VERSION, "manifest_sha256": digest,
                "evidence_ids": [x["event_id"] for x in manifest],
                "scope": exports.SCOPE, "sampling": "RECENT_REVALIDATED_LESSONS",
                "strategy_promotion_authority": False}

    def tick(self, reason="timer"):
        if not self.lock.acquire(blocking=False):
            return False
        try:
            with self.ledger.claim() as c:
                if c is None:
                    return False
                now = self.clock()
                state = self.ledger.load(c)
                if state is None:
                    state = self._fresh(now)
                    self.ledger.save(c, state, "scheduled")
                    self.publish(state)
                    return True
                if state.get("protocol_version") != VERSION or state.get("model_version") != self.ns["VERSION"]:
                    if state.get("runtime_started_at", 0) > self.started_at:
                        # A draining old deployment must not replace the newer
                        # version's checkpoint. A deliberate rollback starts later.
                        self.publish(dict(state, superseded_worker=True))
                        return False
                    # Do not combine checkpoints from different implementations.
                    old = dict(state, status="SUPERSEDED", superseded_at=timestamp(now))
                    self.ledger.save(c, old, "superseded")
                    state = self._fresh(now, state)
                    self.ledger.save(c, state, "scheduled")
                if now < state["next_due_at"]:
                    self.publish(state)
                    return False
                if state["next_stage"] is None:
                    state = self._fresh(now, state)
                    state["next_due_at"] = now
                    self.ledger.save(c, state, "scheduled")
                name = state["next_stage"]
                if state["status"] == "RUNNING":
                    state["interrupted_stage_pending"] = True
                try:
                    with self.ns["_v90_background_maintenance"].permit(
                            "evidence_learning:"+name, self.ns["V90_HEAVY_LEARNING_MAX_START_MB"]):
                        replay = state.pop("interrupted_stage_pending", False)
                        state.update(status="RUNNING", reason=reason, last_started_at=timestamp(now),
                                     stage_started_at=timestamp(now))
                        state["attempts"][name] = state["attempts"].get(name, 0) + 1
                        if replay:
                            state["interrupted_stage_replays"] = state.get("interrupted_stage_replays", 0) + 1
                        self.ledger.save(c, state, name+":start:"+str(state["attempts"][name]))
                        self.publish(state)
                        started = time.monotonic()
                        try:
                            result = self._stage(name)
                        except Exception as exc:
                            result = {"status": "ERROR", "error_code": type(exc).__name__}
                        finally:
                            self.ns["_v90_trim_memory"]("evidence_learning:"+name, force=True, preserve_active_cycle=True)
                        result["duration_seconds"] = round(time.monotonic()-started, 3)
                except MaintenanceDeferred as exc:
                    # Admission does not consume an attempt or reset the due time.
                    changed = state["status"] != exc.status
                    state.update(status=exc.status, reason=reason)
                    if changed:
                        self.ledger.save(c, state, name+":"+exc.status)
                    self.publish(state)
                    return False
                state["stages"][name] = result
                finished = self.clock()
                if result["status"] not in SUCCESS and state["attempts"][name] < MAX_ATTEMPTS:
                    state.update(status="RETRY", next_due_at=finished+60)
                else:
                    index = STAGES.index(name)+1
                    state.update(status="PENDING", next_stage=STAGES[index] if index < len(STAGES) else None,
                                 next_due_at=finished)
                    if state["next_stage"] is None:
                        status = "OK" if all(x["status"] in SUCCESS for x in state["stages"].values()) else "DEGRADED"
                        state.update(status=status, last_finished_at=timestamp(finished), runs=state["runs"]+1,
                                     next_due_at=finished+self.ns["HEAVY_LEARNING_INTERVAL_SECONDS"])
                event = "complete" if state["next_stage"] is None else name+":result:"+str(state["attempts"][name])
                self.ledger.save(c, state, event)
                self.publish(state)
                return True
        except Exception as exc:
            # Keep the last durable checkpoint intact when the audit store fails.
            with self.state_lock:
                self.state = dict(self.state, status="DEFERRED_STORAGE", last_error_code=type(exc).__name__)
            self.ns["emit"]("evidence_learning_storage_error", error_code=type(exc).__name__)
            return False
        finally:
            self.lock.release()

    def run(self):
        self.thread = threading.current_thread()
        while not self.stop.is_set():
            self.tick()
            self.wake.wait(15)
            self.wake.clear()

    def request(self, reason="scheduled", force=False):
        # Calls from the market loop only wake this single worker. Neither
        # force nor a request bypasses the durable schedule/resource permit.
        self.wake.set()
        return bool(self.thread and self.thread.is_alive())


def install(ns):
    if "_evidence_learning_loop" in ns:
        return ns["_evidence_learning_loop"]
    loop = ns["_evidence_learning_loop"] = LearningLoop(ns)
    ns.update(heavy_learning_maintenance_loop=loop.run,
              run_heavy_learning_maintenance=loop.tick,
              maybe_schedule_heavy_learning=loop.request,
              heavy_learning_snapshot=loop.snapshot,
              heavy_learning_due=lambda: loop.clock() >= loop.snapshot().get("next_due_at", float("inf")))
    return loop
