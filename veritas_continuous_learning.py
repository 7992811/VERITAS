"""Small durable learning jobs, independent of dashboard requests and FULL scans.

Forecast outcomes use the first verified quote observed after the declared
horizon, on the exact captured provider/instrument. This is a separately
versioned directional experiment; it does not manufacture executable P&L.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
import sys
import threading
import time

import veritas_autonomous_learning as AUTO
import veritas_learning_bridge as BRIDGE
import veritas_learning_index as INDEX
import veritas_learning_state as STORE
import veritas_price_source as SOURCE
import veritas_knowledge_validation as KNOWLEDGE
import veritas_asset_management_intelligence as INTELLIGENCE
import veritas_scorecard_delivery as SCORECARD
import veritas_learning_v2 as LEARNING_V2
import veritas_learning_v2_registry as LEARNING_V2_REGISTRY
import veritas_learning_v2_replay_eval as LEARNING_V2_REPLAY_EVAL
from veritas_maintenance import MaintenanceDeferred

VERSION = "CONTINUOUS_LEARNING_V1"
OUTCOME_VERSION = "FIRST_VERIFIED_QUOTE_AFTER_HORIZON_V1"
PROGRESS_VERSION = "NONOVERLAPPING_LEARNING_PROGRESS_V1"
HORIZON_SECONDS = {"1m": 60, "5m": 300, "1h": 3600, "4h": 14400,
                   "1d": 86400, "3d": 259200, "7d": 604800}
BATCH = 32
INGEST_BATCH = 8
INITIAL_LOOKBACK = 2000
MAX_PENDING = 4096
MAX_PROVENANCE_BYTES = 16384
CANDIDATE_WORK_VERSION = "FORECAST_CONSUMPTION_V1"
CANDIDATE_MAINTENANCE_SECONDS = 120
LEARNING_V2_SNAPSHOT_NAME = "learning_v2_shadow"
LEARNING_V2_ASSETS = ("BTC","ETH","NQ","BRENT","GOLD","MOEX","CNYRUBF")
LEARNING_V2_INPUT_LIMIT = 128


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=str, separators=(",", ":"))


def clock():
    return datetime.now(timezone.utc)


class Budget:
    def __init__(self, lane):
        self.lane = lane

    @property
    def sql_timeout_ms(self):
        return max(1, min(2000, int(self.lane.current_budget().get("sql_timeout_ms", 2000))))

    @property
    def remaining_seconds(self):
        return max(0., float(self.lane.current_budget().get("remaining_seconds", 6.)))

    def check(self):
        self.lane.check_budget()


@contextmanager
def transaction(connect, context):
    context.check()
    with connect() as c:
        # Checkout and BEGIN may each outlive the remaining cooperative budget.
        # Never turn an already expired turn into a sequence of 1ms SQL calls.
        context.check()
        with c.transaction():
            context.check()
            c.execute("SELECT set_config('statement_timeout',%s,true)", (str(context.sql_timeout_ms),))
            c.execute("SET LOCAL lock_timeout = '250ms'")
            context.check()
            yield c
            context.check()


@contextmanager
def _job_connection(connect, context):
    """Borrow one idle connection without merging the job's transactions."""
    context.check()
    manager = connect()
    connection = manager.__enter__()
    try:
        # Connection setup consumes the original job deadline too. An outer
        # transaction would turn the independently durable phases into savepoints.
        context.check()
        if (connection.autocommit is not True
                or connection.info.transaction_status.name != "IDLE"):
            raise RuntimeError("learning intelligence requires an idle autocommit connection")
        yield lambda: nullcontext(connection)
    except BaseException:
        try:
            manager.__exit__(*sys.exc_info())
        except BaseException:
            pass  # Cleanup must not replace the original work/commit failure.
        raise
    else:
        manager.__exit__(None, None, None)


def forecast_from_ledger(row):
    """Accept only provenance frozen by the new decision writer, never backfill it."""
    p = row.get("provenance")
    if (not isinstance(p, dict) or p.get("version") != BRIDGE.VERSION
            or p.get("eligible") is not True or p.get("asset") != row.get("asset")
            or p.get("horizon") != row.get("horizon")):
        return None, "MISSING_VERIFIED_DECISION_PROVENANCE"
    seal = dict(p)
    expected = seal.pop("evidence_hash", None)
    if not expected or expected != BRIDGE.digest(seal):
        return None, "DECISION_PROVENANCE_HASH_MISMATCH"
    decided = BRIDGE.timestamp(p.get("decision_at"))
    quote = p.get("quote")
    if not isinstance(quote, dict) or not isinstance(quote.get("source_identity"), dict):
        return None, "INVALID_FORECAST_SOURCE_OBJECT"
    known = BRIDGE.timestamp(quote.get("observed_at"))
    seconds = HORIZON_SECONDS.get(str(p.get("horizon")))
    if (not decided or not known or not seconds or not 0 <= (decided-known).total_seconds() <= 120
            or not p.get("policy_hash") or not quote.get("source_key")
            or BRIDGE.number(quote.get("price")) is None or float(quote["price"]) <= 0
            or quote.get("source_key") != BRIDGE.digest(quote.get("source_identity"))):
        return None, "INVALID_FORECAST_CLOCK_OR_SOURCE"
    due = decided + timedelta(seconds=seconds)
    # The permitted observation lag is fixed before the outcome is known.
    lateness = max(90, min(3600, seconds // 4))
    return {"entity_key": row["entity_key"], "decision_at": decided,
            "due_at": due, "expires_at": due+timedelta(seconds=lateness),
            "asset": p["asset"], "horizon": p["horizon"], "source_key": quote["source_key"],
            "evidence": p}, None


def resolve_forecast(forecast, quote, *, now=None):
    now = now or clock()
    due, expires = BRIDGE.timestamp(forecast["due_at"]), BRIDGE.timestamp(forecast["expires_at"])
    if not due or not expires or now < due:
        return None
    frozen = forecast["evidence"]
    observed = BRIDGE.timestamp((quote or {}).get("observed_at"))
    entry = frozen["quote"]
    price = BRIDGE.number((quote or {}).get("price"))
    identity = SOURCE.identity(forecast["asset"], quote or {})
    source_ok = bool(identity and BRIDGE.digest(identity) == entry["source_key"]
                     and (quote or {}).get("source_gate_pass") is True
                     and (quote or {}).get("market_open") is True)
    valid = bool(source_ok and observed and due <= observed <= min(now, expires)
                 and (now-observed).total_seconds() <= 120 and price is not None and price > 0)
    if not valid:
        if now > expires:
            return {"status": "EXCLUDED", "reason": "NO_VERIFIED_QUOTE_IN_FROZEN_WINDOW",
                    "protocol": OUTCOME_VERSION, "observed_at": now.isoformat()}
        return None
    # Verify the native quote metadata as well as its declared source label.
    import veritas_execution as EXECUTION
    if not EXECUTION.paper_quote_time_gate(dict(quote, asset=forecast["asset"]), now=now).get("eligible"):
        return None if now <= expires else {"status": "EXCLUDED", "reason": "QUOTE_PROOF_INVALID"}
    result = {"status": "READY", "episode_key": forecast["entity_key"],
              "idea_id": frozen.get("idea_id") or "forecast:"+forecast["entity_key"],
              "asset": forecast["asset"], "horizon": forecast["horizon"],
              "regime": frozen.get("regime") or "UNKNOWN", "direction": frozen.get("direction"),
              "decision_at": frozen["decision_at"], "known_at": entry["observed_at"],
              "outcome_at": observed.isoformat(), "observed_at": now.isoformat(),
              "forward_return": price/float(entry["price"])-1.,
              "predicted_probability": frozen.get("base_probability"),
              "net_reward_risk": frozen.get("net_reward_risk"),
              "knowledge_trials": deepcopy(frozen.get("knowledge_trials") or []),
              "source_identity": identity, "source_verified": True, "evidence_valid": True,
              "independence_verified": True, "independence_basis": frozen["independence_basis"],
              "evidence_version": OUTCOME_VERSION, "policy_hash": frozen["policy_hash"],
              "decision_evidence_hash": frozen["evidence_hash"],
              "outcome_price": price, "observation_lag_seconds": (observed-due).total_seconds(),
              "protocol": OUTCOME_VERSION, "profitability_proven": False}
    result["evidence_hash"] = BRIDGE.digest(result)
    return result


def independent_slices(early, recent):
    """Separate chronological windows before reducing correlated forecasts."""
    unique = {r["entity_key"]: dict(r) for r in list(early)+list(recent)}
    ordered = sorted(unique.values(), key=lambda r: (str(r["decision_ts"]), r["entity_key"]))
    middle = len(ordered)//2
    halves = (ordered[:middle], ordered[middle:])
    result = []
    for half in halves:
        last, reduced = {}, []
        for row in half:
            key = (row["asset"], row["horizon"])
            at = BRIDGE.timestamp(row["decision_ts"])
            outcome_at = BRIDGE.timestamp(row.get("outcome_ts"))
            previous = last.get(key)
            if (at is None or outcome_at is None or outcome_at <= at
                    or (previous and (at-previous).total_seconds() < HORIZON_SECONDS.get(row["horizon"], 86400))):
                continue
            last[key] = at
            reduced.append(dict(row, event_ts=at, op={"forward_return": row["forward_return"],
                                "mfe": row.get("mfe"), "mae": row.get("mae")}))
        result.append(reduced)
    # The two observation windows may not overlap across their boundary either.
    if result[0] and result[1]:
        cut = min(BRIDGE.timestamp(r["decision_ts"]) for r in result[1])
        result[0] = [r for r in result[0] if BRIDGE.timestamp(r["outcome_ts"]) < cut]
    return result


class ContinuousLearning:
    def __init__(self, ns):
        self.ns, self.connect, self.lane = ns, ns["pg_connect"], ns["_v90_background_maintenance"]
        self.ready, self.boot_phase = False, 0
        self._lock = threading.RLock()
        self._snapshot = AUTO.snapshot()
        self._learning_v2 = {"version": LEARNING_V2.VERSION, "status": "WARMING_UP",
                             "automatic_production_promotion": False, "hypotheses": [], "counts": {}}
        self._stats = {"status": "collecting", "processed_decisions": 0, "resolved_forecasts": 0,
                       "abstention_observations": 0, "excluded_forecasts": 0,
                       "last_success_at": None, "last_error": None}
        self.trade = None
        jobs = (("learning_bootstrap", self.bootstrap, 10, 6),
                ("learning_ingest", self.ingest, 15, 6),
                ("learning_outcomes", self.outcomes, 15, 6),
                ("learning_candidates", self.candidates, 30, 6),
                ("learning_knowledge_catalog", self.knowledge_catalog, 60, 6),
                ("learning_trade_evidence", self.trades, 30, 6),
                ("learning_progress", self.progress, 120, 6),
                ("learning_intelligence", self.intelligence, 15, 6),
                ("learning_memory", self.memory, 300, 6),
                ("learning_v2_shadow", self.learning_v2_shadow, 60, 5),
                ("learning_v2_replay", self.learning_v2_replay, 180, 5))
        for name, fn, interval, seconds in jobs:
            if name == "learning_bootstrap":
                callback = fn
            elif name == "learning_trade_evidence":
                callback = self._trade_callback
            else:
                callback = self._callback(name, fn)
            self.lane.register_periodic(name, callback, interval_seconds=interval,
                                        lightweight=True, estimated_peak_mb=16, max_seconds=seconds)

    def start(self):
        return self.lane.start_periodic()

    def _callback(self, name, operation):
        def run(context=None, job_connect=None):
            if not self.ready:
                return {"status": "RETRY", "reason": "LEARNING_BOOTSTRAP_PENDING"}
            context = Budget(self.lane) if context is None else context
            job_connect = self.connect if job_connect is None else job_connect
            lease = STORE.claim_job(job_connect, name, VERSION, lease_seconds=30)
            if not lease:
                return {"status": "RETRY", "reason": "DURABLE_JOB_LEASE_BUSY"}
            cursor = lease.get("cursor") or {}
            try:
                if name == "learning_intelligence":
                    result, cursor = operation(context, cursor, pg_connect=job_connect)
                else:
                    result, cursor = operation(context, cursor)
                context.check()
                status = str(result.get("status", "")).upper()
                if status == "RETRY" or status.startswith("DEFERRED"):
                    # Waiting for a shared lease is not a failed experiment.
                    # It must neither advance the source cursor nor replace the
                    # last successful result with a misleading completion.
                    if not STORE.checkpoint_job(job_connect, lease, status="RETRY", result=result,
                                                retry_after_seconds=10):
                        raise RuntimeError("learning lease expired before retry checkpoint")
                    return result
                if status not in ("OK", "NO_WORK", "PROGRESS"):
                    raise RuntimeError("incomplete learning job: "+str(result.get("status")))
                if not STORE.checkpoint_job(job_connect, lease, status="OK", cursor=cursor, result=result):
                    raise RuntimeError("learning lease expired before checkpoint")
                if status == "PROGRESS" and name in ("learning_candidates", "learning_outcomes"):
                    # Existing fair scheduler demand is coalesced and still
                    # observes its normal retry delay and resource guards.
                    self.lane.request(name)
                with self._lock:
                    self._stats.update(last_success_at=clock().isoformat(), last_error=None)
                return result
            except MaintenanceDeferred as deferred:
                # Exhausted work remains retryable with its original cursor;
                # it is not a failed SQL query or a completed learning batch.
                try:
                    STORE.checkpoint_job(self.connect, lease, status="RETRY", result=deferred.result(),
                                         retry_after_seconds=10)
                except Exception:
                    pass  # A lost checkpoint is recovered by the fenced lease.
                raise
            except Exception as error:
                message = (type(error).__name__+": "+str(error))[:300]
                with self._lock:
                    self._stats["last_error"] = message
                # A failed attempt never advances its cursor or last-good result.
                try:
                    STORE.checkpoint_job(self.connect, lease, status="ERROR", result={"error": message},
                                         retry_after_seconds=10)
                except Exception:
                    pass  # Fenced lease expiry permits recovery after a DB outage.
                raise
        if name != "learning_intelligence":
            return run

        def scoped_run():
            if not self.ready:
                return {"status": "RETRY", "reason": "LEARNING_BOOTSTRAP_PENDING"}
            context = Budget(self.lane)
            result = None
            try:
                with _job_connection(self.connect, context) as job_connect:
                    result = run(context, job_connect)
                return result
            except Exception as error:
                if result is not None and not isinstance(error, MaintenanceDeferred):
                    # The checkpoint already committed. Report a later close
                    # failure without replaying work or undoing its success time.
                    with self._lock:
                        self._stats["last_error"] = (type(error).__name__+": "+str(error))[:300]
                raise
        return scoped_run

    def _trade_callback(self):
        # TradeLearning owns the durable closed_trade_learning lease, phase
        # cursor and retry. A second lease adds two database transactions to
        # every bounded stage without protecting any additional progress.
        if not self.ready:
            return {"status": "RETRY", "reason": "LEARNING_BOOTSTRAP_PENDING"}
        context = Budget(self.lane)
        try:
            context.check()
            result, _ = self.trades(context, {})
            context.check()
            status = str(result.get("status", "")).upper()
            if status == "RETRY" or status.startswith("DEFERRED"):
                return result
            if status not in ("OK", "NO_WORK", "PROGRESS"):
                raise RuntimeError("incomplete learning job: "+str(result.get("status")))
            with self._lock:
                self._stats.update(last_success_at=clock().isoformat(), last_error=None)
            return result
        except MaintenanceDeferred:
            # The sole trade lease already retains its retry/cursor state.
            # A cooperative budget deferral is not a new SQL/runtime error.
            raise
        except Exception as error:
            with self._lock:
                self._stats["last_error"] = (type(error).__name__+": "+str(error))[:300]
            raise

    def bootstrap(self):
        if self.ready:
            return {"status": "NO_WORK"}
        context = Budget(self.lane)
        if self.boot_phase == 0:
            AUTO.ensure_schema(self.connect)
            self.boot_phase = 1
            return {"status": "PROGRESS", "stage": "STATE_SCHEMA"}
        if self.boot_phase == 1:
            with transaction(self.connect, context) as c:
                c.execute("""CREATE TABLE IF NOT EXISTS learning_forecasts (
                    id BIGSERIAL PRIMARY KEY, entity_key TEXT UNIQUE NOT NULL,
                    decision_at TIMESTAMPTZ NOT NULL, due_at TIMESTAMPTZ NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL, asset TEXT NOT NULL, horizon TEXT NOT NULL,
                    source_key TEXT NOT NULL, evidence JSONB NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING', outcome JSONB,
                    learned_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    CHECK(octet_length(evidence::text)<=32768))""")
                c.execute("CREATE INDEX IF NOT EXISTS learning_forecasts_pending ON learning_forecasts(due_at,id) WHERE status='PENDING'")
                c.execute("CREATE INDEX IF NOT EXISTS learning_forecasts_pending_scope ON learning_forecasts(asset,horizon,source_key) WHERE status='PENDING'")
                c.execute("CREATE INDEX IF NOT EXISTS learning_forecasts_ready ON learning_forecasts(decision_at,id) WHERE status='READY'")
                c.execute("CREATE INDEX IF NOT EXISTS learning_forecasts_learned ON learning_forecasts(learned_at) WHERE learned_at IS NOT NULL")
            self.boot_phase = 2
            return {"status": "PROGRESS", "stage": "FORECAST_SCHEMA"}
        if self.boot_phase == 2:
            from veritas_trade_learning import TradeLearning
            self.trade = TradeLearning(self.ns)
            if hasattr(self.trade, "ensure_schema"):
                self.trade.ensure_schema(context)
            self.boot_phase = 3
            return {"status": "PROGRESS", "stage": "TRADE_SCHEMA"}
        if self.boot_phase == 3:
            KNOWLEDGE.ensure_schema(self.connect, context=context)
            KNOWLEDGE.restore(self.connect, context=context)
            with transaction(self.connect, context) as c:
                LEARNING_V2_REGISTRY.ensure_schema(c)
                LEARNING_V2_REPLAY_EVAL.ensure_schema(c)
            self.boot_phase = 4
            return {"status": "PROGRESS", "stage": "KNOWLEDGE_AND_LEARNING_V2_SCHEMA"}
        saved = AUTO.snapshot(self.connect)
        progress = STORE.load_snapshot(self.connect, "learning_progress", PROGRESS_VERSION)
        learning_v2 = STORE.load_snapshot(self.connect, LEARNING_V2_SNAPSHOT_NAME, LEARNING_V2.VERSION)
        with self._lock:
            self._snapshot = saved
            if learning_v2 and isinstance(learning_v2.get("payload"), dict):
                self._learning_v2 = deepcopy(learning_v2["payload"])
        BRIDGE.update(saved)
        if progress:
            self.ns["learning_progress"]._cache = (0., progress["payload"])
        self.ready = True
        self.ns["emit"]("continuous_learning_ready", version=VERSION,
                        processed=saved.get("counts"), profiles=len(saved.get("profiles") or []))
        return {"status": "OK", "stage": "RESTORED"}

    def ingest(self, context, cursor):
        last_id = int(cursor.get("last_id") or 0)
        inserted = missing = coalesced = 0
        with transaction(self.connect, context) as c:
            if "last_id" not in cursor:
                # Discover the replay boundary using only indexed metadata.
                # Looking for one JSON key in 2,000 legacy payloads repeatedly
                # decompressed their full TOAST values and timed out in prod.
                tail = c.execute("SELECT id FROM ledger_events WHERE event_type='decision' ORDER BY id DESC LIMIT %s",
                                 (INITIAL_LOOKBACK,)).fetchall()
                ids = [r["id"] for r in tail]
                last_id = max(ids, default=0)
                cursor = dict(cursor, initial_watermark=last_id,
                              backfill_last_id=min(ids)-1 if ids else 0,
                              backfill_until_id=last_id)
                context.check()
            pending = c.execute("SELECT COUNT(*) n FROM (SELECT id FROM learning_forecasts WHERE status='PENDING' LIMIT %s) q", (MAX_PENDING,)).fetchone()["n"]
            if pending >= MAX_PENDING:
                raise RuntimeError("PENDING_FORECAST_CAPACITY_REACHED")
            def read(after, limit, until=None):
                if limit <= 0:
                    return []
                upper = " AND id<=%s" if until is not None else ""
                args = (after, until, limit, MAX_PROVENANCE_BYTES) if until is not None else (after, limit, MAX_PROVENANCE_BYTES)
                return c.execute("""WITH ids AS MATERIALIZED (
                    SELECT id,entity_key,asset,horizon FROM ledger_events
                    WHERE id>%s AND event_type='decision'"""+upper+""" ORDER BY id LIMIT %s
                ), extracted AS MATERIALIZED (
                    SELECT i.*,e.payload->'learning_provenance' AS provenance
                    FROM ids i JOIN ledger_events e ON e.id=i.id
                ) SELECT id,entity_key,asset,horizon,
                    CASE WHEN octet_length(provenance::text)<=%s THEN provenance END provenance
                  FROM extracted ORDER BY id""", args).fetchall()
            # New decisions retain priority while the bounded legacy tail is
            # replayed. Otherwise a large old tail can make every short-horizon
            # observation expire before the reader ever reaches current data.
            live = read(last_id, INGEST_BATCH)
            backfill_id = int(cursor.get("backfill_last_id") or 0)
            backfill_until = int(cursor.get("backfill_until_id") or 0)
            backfill = read(backfill_id, INGEST_BATCH-len(live), backfill_until) if backfill_id < backfill_until else []
            rows = live+backfill
            existing = {r["entity_key"]: r["evidence_hash"] for r in c.execute(
                "SELECT entity_key,evidence->>'evidence_hash' evidence_hash FROM learning_forecasts WHERE entity_key=ANY(%s)",
                ([r["entity_key"] for r in rows],)).fetchall()} if rows else {}
            for row in rows:
                context.check()
                forecast, reason = forecast_from_ledger(row)
                if forecast:
                    if existing.get(row["entity_key"]) == forecast["evidence"]["evidence_hash"]:
                        # A previous transaction may have committed the seal
                        # before its separate job checkpoint was interrupted.
                        # The immutable row counts once when that cursor retries.
                        inserted += 1
                        continue
                    got = c.execute("""INSERT INTO learning_forecasts
                        (entity_key,decision_at,due_at,expires_at,asset,horizon,source_key,evidence)
                        SELECT %s,%s,%s,%s,%s,%s,%s,%s::jsonb
                        WHERE NOT EXISTS (SELECT 1 FROM learning_forecasts
                          WHERE status='PENDING' AND asset=%s AND horizon=%s AND source_key=%s
                            AND evidence->>'regime'=%s AND evidence->>'policy_hash'=%s)
                        ON CONFLICT(entity_key) DO NOTHING RETURNING id""",
                        tuple(forecast[k] for k in ("entity_key", "decision_at", "due_at", "expires_at", "asset", "horizon", "source_key"))+
                        (_json(forecast["evidence"]),forecast["asset"],forecast["horizon"],forecast["source_key"],
                         forecast["evidence"]["regime"],forecast["evidence"]["policy_hash"])).fetchone()
                    inserted += bool(got)
                    coalesced += not bool(got)
                else:
                    missing += 1
            if live:
                last_id = live[-1]["id"]
            if backfill:
                backfill_id = backfill[-1]["id"]
            elif len(live) < INGEST_BATCH:
                backfill_id = backfill_until
        cursor = dict(cursor, last_id=last_id, imported=int(cursor.get("imported") or 0)+inserted,
                      backfill_last_id=backfill_id,
                      scanned=int(cursor.get("scanned") or 0)+len(rows),
                      coalesced=int(cursor.get("coalesced") or 0)+coalesced,
                      missing_provenance=int(cursor.get("missing_provenance") or 0)+missing)
        with self._lock:
            self._stats["processed_decisions"] = cursor["imported"]
            self._stats["scanned_decisions"] = cursor["scanned"]
            self._stats["excluded_missing_provenance"] = cursor["missing_provenance"]
            self._stats["backfill_pending"] = backfill_id < backfill_until
        return {"status": "OK", "imported": inserted, "coalesced": coalesced, "excluded_missing_provenance": missing,
                "scanned": len(rows), "backfill_pending": backfill_id < backfill_until,
                "pending_before": pending, "cursor": last_id}, cursor

    def _quote(self, forecast, now):
        import veritas_position_guard as GUARD
        identity = forecast["evidence"]["quote"]["source_identity"]
        return GUARD.quote_for_position({"asset": forecast["asset"], "payload": {"price_source_lock": identity}}, now=now)

    def outcomes(self, context, cursor):
        now = clock()
        resolved = excluded = scanned = 0
        deferred = False
        with transaction(self.connect, context) as c:
            rows = c.execute("""SELECT id,entity_key,decision_at,due_at,expires_at,asset,horizon,evidence
                FROM learning_forecasts WHERE status='PENDING' AND due_at<=%s
                ORDER BY due_at,id LIMIT %s FOR UPDATE SKIP LOCKED""", (now, BATCH)).fetchall()
            timeout = context.sql_timeout_ms
            for row in rows:
                context.check()
                # Finish a committed prefix instead of rolling all 32 writes
                # back at the deadline. Reserve commit + outer checkpoint time.
                allowance = float(getattr(context, "remaining_seconds", 6.))-2.2
                if allowance < .2:
                    deferred = True
                    break
                smaller = max(1, min(timeout, int(allowance*1000)))
                if smaller < timeout:
                    c.execute("SELECT set_config('statement_timeout',%s,true)", (str(smaller),))
                    timeout = smaller
                    if float(getattr(context, "remaining_seconds", 6.)) <= 2.2:
                        deferred = True
                        break
                outcome = resolve_forecast(row, {} if now > row["expires_at"] else self._quote(row, now), now=now)
                scanned += 1
                if outcome is None:
                    continue
                c.execute("UPDATE learning_forecasts SET status=%s,outcome=%s::jsonb,updated_at=now(),learned_at=%s WHERE id=%s AND status='PENDING'",
                          (outcome["status"], _json(outcome), now if outcome["status"] == "EXCLUDED" else None, row["id"]))
                resolved += outcome["status"] == "READY"
                excluded += outcome["status"] == "EXCLUDED"
        cursor = dict(cursor, resolved=int(cursor.get("resolved") or 0)+resolved,
                      excluded=int(cursor.get("excluded") or 0)+excluded)
        with self._lock:
            self._stats.update(resolved_forecasts=cursor["resolved"], excluded_forecasts=cursor["excluded"])
        status = ("PROGRESS" if resolved+excluded else "DEFERRED_OUTCOME_BUDGET") if deferred else (
            "PROGRESS" if len(rows) == BATCH and resolved+excluded else "OK")
        return {"status": status, "resolved": resolved, "excluded": excluded, "scanned": scanned,
                "remaining_selected": len(rows)-scanned, "reason": "CHECKPOINT_TIME_RESERVED" if deferred else None}, cursor

    def _publish_candidate_snapshot(self, snapshot):
        """A delayed phase must not replace advice published by another job."""
        with self._lock:
            current_at = BRIDGE.timestamp(self._snapshot.get("updated_at"))
            pending_at = BRIDGE.timestamp(snapshot.get("updated_at"))
            if current_at is not None and (pending_at is None or current_at >= pending_at):
                return deepcopy(self._snapshot)
            self._snapshot = deepcopy(snapshot)
            with BRIDGE._lock:
                bridge_at = BRIDGE.timestamp(BRIDGE._state.get("updated_at"))
                if bridge_at is None or (pending_at is not None and pending_at >= bridge_at):
                    BRIDGE.update(snapshot)
            return deepcopy(self._snapshot)

    def candidates(self, context, cursor):
        """Resume one original forecast through AUTO, individual KV trials, ACK.

        The fenced outer job stores only immutable identity and stage indexes.
        Original outcomes remain in the forecast table; every replay verifies
        their complete seal before either consumer runs or the row is ACKed.
        """
        cursor = deepcopy(cursor)
        work = cursor.get("candidate_work")
        now = clock()
        if work and work.get("phase") == "maintenance":
            snapshot = AUTO.run_batch(self.connect, (), context=context)
            snapshot = self._publish_candidate_snapshot(snapshot)
            cursor.update(candidate_work={"phase": "retention"},
                          next_auto_maintenance_at=now.timestamp()+CANDIDATE_MAINTENANCE_SECONDS)
            return {"status": "PROGRESS", "stage": "MAINTENANCE", "counts": snapshot.get("counts")}, cursor
        if work and work.get("phase") == "retention":
            with transaction(self.connect, context) as c:
                c.execute("DELETE FROM learning_forecasts WHERE id IN (SELECT id FROM learning_forecasts WHERE learned_at<now()-interval '30 days' ORDER BY learned_at LIMIT 64)")
            cursor.pop("candidate_work")
            cursor["next_retention_at"] = now.timestamp()+CANDIDATE_MAINTENANCE_SECONDS
            return {"status": "OK", "stage": "RETENTION"}, cursor
        with transaction(self.connect, context) as c:
            row = (c.execute("SELECT id,entity_key,status,outcome FROM learning_forecasts WHERE id=%s", (work["id"],)).fetchone()
                   if work else c.execute("SELECT id,entity_key,status,outcome FROM learning_forecasts WHERE status='READY' ORDER BY decision_at,id LIMIT 1").fetchone())
        if not row:
            if work:
                raise RuntimeError("PINNED_FORECAST_MISSING")
            if now.timestamp() >= float(cursor.get("next_auto_maintenance_at") or 0):
                cursor["candidate_work"] = {"phase": "maintenance"}
                return {"status": "PROGRESS", "stage": "MAINTENANCE_DUE"}, cursor
            return {"status": "NO_WORK", "stage": "IDLE", "counts": self._snapshot.get("counts")}, cursor
        raw = row["outcome"]
        if not isinstance(raw, dict) or row["status"] not in ("READY", "LEARNED"):
            raise RuntimeError("PINNED_FORECAST_NOT_READY")
        seal = dict(raw)
        evidence_hash = seal.pop("evidence_hash", None)
        if (not evidence_hash or evidence_hash != BRIDGE.digest(seal)
                or raw.get("episode_key") != row["entity_key"] or raw.get("status") != "READY"):
            raise RuntimeError("FORECAST_OUTCOME_SEAL_MISMATCH")
        stamps = raw.get("knowledge_trials")
        if stamps is None:
            stamps = []
        if not isinstance(stamps, list) or len(stamps) > KNOWLEDGE.MAX_TRIALS or any(not isinstance(stamp, dict) for stamp in stamps):
            raise ValueError("invalid original forecast knowledge trials")
        for stamp in stamps:
            STORE._json(stamp, 2048)
        directional = raw.get("direction") in ("LONG", "SHORT")
        if not work:
            cursor["candidate_work"] = {"version": CANDIDATE_WORK_VERSION, "id": row["id"],
                "entity_key": row["entity_key"], "evidence_hash": evidence_hash, "phase": "AUTO", "trial_index": 0}
            return {"status": "PROGRESS", "stage": "PINNED", "forecast_id": row["id"]}, cursor
        if (work.get("version") != CANDIDATE_WORK_VERSION or work.get("entity_key") != row["entity_key"]
                or work.get("evidence_hash") != evidence_hash):
            raise RuntimeError("PINNED_FORECAST_EVIDENCE_CHANGED")
        phase = work["phase"]
        if phase == "AUTO":
            if directional:
                snapshot = AUTO.run_batch(self.connect, [raw], context=context)
                self._publish_candidate_snapshot(snapshot)
                cursor["next_auto_maintenance_at"] = now.timestamp()+CANDIDATE_MAINTENANCE_SECONDS
            work["phase"] = "KV" if stamps else "ACK"
        elif phase == "KV":
            index = work["trial_index"]
            if type(index) is not int or not 0 <= index < len(stamps):
                raise RuntimeError("INVALID_FORECAST_TRIAL_CURSOR")
            KNOWLEDGE.run_batch(self.connect, [raw], context=context, trial_window=(index, index+1))
            work["trial_index"] = index+1
            if index+1 == len(stamps):
                work["phase"] = "ACK"
        elif phase == "ACK":
            with transaction(self.connect, context) as c:
                # Recheck under the row lock: data cannot change between the
                # proof comparison and ACK, even if another writer is active.
                locked = c.execute("SELECT status,outcome FROM learning_forecasts WHERE id=%s FOR UPDATE", (row["id"],)).fetchone()
                if (not locked or locked["status"] not in ("READY", "LEARNED")
                        or BRIDGE.digest(locked["outcome"]) != BRIDGE.digest(raw)):
                    raise RuntimeError("PINNED_FORECAST_CHANGED_BEFORE_ACK")
                if locked["status"] == "READY":
                    c.execute("UPDATE learning_forecasts SET status='LEARNED',learned_at=now(),updated_at=now() WHERE id=%s", (row["id"],))
                # A different process may have advanced AUTO between stages.
                # Read the compact durable model on this connection, without
                # repeating either learner or publishing before ACK commits.
                saved = STORE.load_snapshot_in_transaction(c, AUTO.SNAPSHOT_NAME, AUTO.VERSION)
            if saved:
                self._publish_candidate_snapshot(AUTO.public_snapshot(saved["payload"]))
            abstentions = int(not directional)
            cursor["abstentions"] = int(cursor.get("abstentions") or 0)+abstentions
            cursor["completed_forecasts"] = int(cursor.get("completed_forecasts") or 0)+1
            cursor.pop("candidate_work")
            if not directional and now.timestamp() >= float(cursor.get("next_auto_maintenance_at") or 0):
                cursor["candidate_work"] = {"phase": "maintenance"}
            elif now.timestamp() >= float(cursor.get("next_retention_at") or 0):
                cursor["candidate_work"] = {"phase": "retention"}
            with self._lock:
                self._stats["abstention_observations"] = cursor["abstentions"]
            return {"status": "PROGRESS", "stage": "ACK", "observations": int(directional),
                    "abstentions": abstentions, "forecast_id": row["id"], "counts": self._snapshot.get("counts")}, cursor
        else:
            raise RuntimeError("INVALID_FORECAST_STAGE")
        return {"status": "PROGRESS", "stage": phase, "forecast_id": row["id"],
                "next_stage": work["phase"], "trial_index": work["trial_index"], "counts": self._snapshot.get("counts")}, cursor

    def trades(self, context, cursor):
        result = self.trade.process(context)
        KNOWLEDGE.update_integrity_verification(self.trade.validation_status())
        if result.get("snapshot"):
            with self._lock:
                self._snapshot = result["snapshot"]
            BRIDGE.update(result["snapshot"])
        return {k: v for k, v in result.items() if k != "snapshot"}, cursor

    def knowledge_catalog(self, context, cursor):
        result = KNOWLEDGE.refresh_catalog(self.connect, context=context, cursor=cursor)
        return result, result.get("cursor", cursor)

    def memory(self, context, cursor):
        return self.trade.refresh_memory(context), cursor

    def learning_v2_shadow(self, context, cursor):
        """Build one asset's bounded shadow research from verified outcomes.

        Rotating assets prevents the research lane from competing with trading
        for the full 512 MiB process budget.  Outcomes come from the compact
        materialized episode table; only the matching decision payload is read
        for frozen pre-outcome context.  No entry/stop/exit/risk mutation occurs.
        """
        cursor=deepcopy(cursor)
        index=int(cursor.get("asset_index") or 0)%len(LEARNING_V2_ASSETS)
        asset=LEARNING_V2_ASSETS[index]
        limit=LEARNING_V2_INPUT_LIMIT
        job_started=time.monotonic()
        read_started=time.monotonic()
        with transaction(self.connect, context) as c:
            decisions=c.execute("""
              WITH recent AS MATERIALIZED (
                SELECT entity_key,decision_ts,asset,horizon,regime,decision,forward_return
                FROM v90_decision_episodes
                WHERE asset=%s
                ORDER BY decision_ts DESC
                LIMIT %s
              )
              SELECT d.id AS decision_id,e.entity_key,e.decision_ts AS event_ts,
                     e.asset,e.horizon,e.regime,e.decision,e.forward_return,
                     COALESCE(d.payload->>'setup_family',d.payload->>'strategy_family',
                              d.payload#>>'{trade_plan,setup_family}','') AS setup_family,
                     COALESCE(d.payload#>>'{learning_provenance,policy_hash}',
                              d.payload->>'strategy_policy_hash','') AS policy_hash,
                     COALESCE(d.payload#>>'{learning_provenance,source_identity,key}',
                              d.payload#>>'{timeframe_entry_context,source_identity,key}',
                              d.payload#>>'{trade_plan,timeframe_entry_context,source_identity,key}','') AS source_key,
                     COALESCE(d.payload#>>'{learning_provenance,source_identity,contract_id}',
                              d.payload#>>'{timeframe_entry_context,source_identity,contract_id}',
                              d.payload#>>'{trade_plan,timeframe_entry_context,source_identity,contract_id}','') AS contract_id,
                     CASE WHEN e.decision IN ('LONG','SHORT') THEN e.decision
                          ELSE COALESCE(d.payload#>>'{timeframe_entry_context,event,direction}',
                                        d.payload#>>'{trade_plan,timeframe_entry_context,event,direction}',
                                        NULLIF(d.payload->>'horizon_structure_direction','NO_TRADE'),'') END AS candidate_direction,
                     CASE
                          WHEN COALESCE(d.payload->>'plan_eligible',
                                        d.payload#>>'{trade_plan,eligible}')='false'
                            OR COALESCE(d.payload->>'trade_entry_eligible',
                                        d.payload#>>'{execution_eligibility,eligible}',
                                        d.payload#>>'{execution_eligibility,paper_eligible}')='false'
                          THEN false
                          WHEN COALESCE(d.payload->>'plan_eligible',
                                        d.payload#>>'{trade_plan,eligible}')='true'
                            AND COALESCE(d.payload->>'trade_entry_eligible',
                                         d.payload#>>'{execution_eligibility,eligible}',
                                         d.payload#>>'{execution_eligibility,paper_eligible}')='true'
                          THEN true
                          ELSE NULL END AS admission_eligible,
                     COALESCE(NULLIF(d.payload->>'final_gate_status',''),
                              CASE WHEN COALESCE(d.payload->>'plan_eligible',
                                                 d.payload#>>'{trade_plan,eligible}')='false'
                                      OR COALESCE(d.payload->>'trade_entry_eligible',
                                                  d.payload#>>'{execution_eligibility,eligible}',
                                                  d.payload#>>'{execution_eligibility,paper_eligible}')='false'
                                   THEN 'BLOCK' ELSE '' END) AS final_gate_status,
                     COALESCE(d.payload->'final_gate_blockers',
                              d.payload#>'{execution_eligibility,paper_source_blockers}',
                              '[]'::jsonb) AS final_gate_blockers,
                     COALESCE(NULLIF(d.payload->>'plan_reason',''),
                              d.payload#>>'{trade_plan,reason}','') AS plan_reason,
                     COALESCE(NULLIF(d.payload->>'trade_entry_reason',''),
                              d.payload#>>'{execution_eligibility,reason}','') AS trade_entry_reason,
                     COALESCE(NULLIF(d.payload->>'execution_reason',''),
                              d.payload#>>'{execution_eligibility,reason}','') AS execution_reason,
                     COALESCE(NULLIF(d.payload->>'paper_execution_reason',''),
                              d.payload#>>'{execution_eligibility,paper_execution_reason}','') AS paper_execution_reason
              FROM recent e
              CROSS JOIN LATERAL (
                SELECT id,payload FROM ledger_events d
                WHERE d.entity_key=e.entity_key AND d.event_type='decision'
                ORDER BY d.id DESC LIMIT 1
              ) d
              ORDER BY e.decision_ts DESC
            """,(asset,limit)).fetchall()
            decision_read_seconds=time.monotonic()-read_started
            context.check()
            trade_read_started=time.monotonic()
            trades=c.execute("""
              WITH ranked AS (
                 SELECT e.closed_at,e.asset,e.horizon,e.regime,e.setup_family,
                        t.payload#>>'{entry_event_snapshot,event_id}' AS event_id,
                        COALESCE(t.payload->>'strategy_policy_hash','') AS policy_hash,
                        COALESCE(t.payload#>>'{price_source_lock,key}',
                                 t.payload#>>'{entry_execution_source_identity,key}','') AS source_key,
                        COALESCE(t.payload#>>'{price_source_lock,contract_id}',
                                 t.payload#>>'{entry_execution_source_identity,contract_id}','') AS contract_id,
                        e.mae_pct AS mae,e.mfe_pct AS mfe,e.capture_ratio,
                        e.net_pnl_rub,e.primary_attribution,
                        e.learning_eligible AS path_learning_eligible,
                        COALESCE((e.payload->>'outcome_learning_eligible')::boolean,FALSE) AS outcome_learning_eligible,
                        ROW_NUMBER() OVER (
                          PARTITION BY t.payload#>>'{entry_event_snapshot,event_id}'
                          ORDER BY e.closed_at ASC,e.trade_id ASC
                        ) AS event_rank
                 FROM v90_learning_episodes e
                 JOIN paper_trades t ON t.trade_id=e.trade_id
                 WHERE (e.learning_eligible=TRUE
                        OR COALESCE((e.payload->>'outcome_learning_eligible')::boolean,FALSE)=TRUE)
                   AND e.primary_attribution<>'ADMINISTRATIVE_EXIT_EXCLUDED'
                   AND e.asset=%s
                   AND NULLIF(t.payload#>>'{entry_event_snapshot,event_id}','') IS NOT NULL
               )
               SELECT closed_at,asset,horizon,regime,setup_family,event_id,policy_hash,
                      source_key,contract_id,mae,mfe,capture_ratio,net_pnl_rub,
                      primary_attribution,path_learning_eligible,outcome_learning_eligible
               FROM ranked
               WHERE event_rank=1
               ORDER BY closed_at DESC
               LIMIT %s
            """,(asset,limit)).fetchall()
            trade_read_seconds=time.monotonic()-trade_read_started
        decision_rows=[dict(row) for row in decisions or []]
        trade_rows=[dict(row) for row in trades or []]
        research_started=time.monotonic()
        current=LEARNING_V2.research_snapshot(decision_rows,trade_rows)
        research_seconds=time.monotonic()-research_started
        current.update(asset=asset,
                       input_counts={"decisions":len(decision_rows),"trades":len(trade_rows)},
                       generated_at=clock().isoformat(),
                       source="MATERIALIZED_OUTCOMES_PLUS_FROZEN_DECISION_CONTEXT",
                       automatic_production_promotion=False)
        context.check()
        registry_started=time.monotonic()
        with transaction(self.connect, context) as c:
            registry=LEARNING_V2_REGISTRY.sync(
                c,current,decision_rows,trade_rows,now=clock())
        registry_seconds=time.monotonic()-registry_started
        context.check()
        with self._lock:
            prior=deepcopy(self._learning_v2)
        assets=dict(prior.get("assets") or {})
        assets[asset]={k:deepcopy(current.get(k)) for k in (
            "status","counts","entry_false_block","diagnostics","hypotheses","input_counts","generated_at")}
        combined={}
        for name in LEARNING_V2_ASSETS:
            for h in (assets.get(name) or {}).get("hypotheses") or []:
                if isinstance(h,dict) and h.get("hypothesis_id"):
                    combined[h["hypothesis_id"]]=h
        hypotheses=sorted(combined.values(),key=lambda h:(str(h.get("kind")),str(h.get("hypothesis_id"))))[:LEARNING_V2.MAX_HYPOTHESES]
        counts={}
        for h in hypotheses:
            kind=str(h.get("kind") or "UNKNOWN")
            counts[kind]=counts.get(kind,0)+1
        value={
            "version":LEARNING_V2.VERSION,
            "status":"SHADOW_READY" if hypotheses else "BUILDING",
            "automatic_production_promotion":False,
            "assets":assets,
            "last_asset":asset,
            "hypotheses":hypotheses,
            "counts":counts,
            "registry":registry,
            "generated_at":current["generated_at"],
            "source":current["source"],
            "principle":"Rotating bounded cohorts; observed evidence may create shadow hypotheses, never live mutations.",
        }
        if not STORE.publish_snapshot(self.connect,LEARNING_V2_SNAPSHOT_NAME,LEARNING_V2.VERSION,
                                      value,observed_at=clock()):
            raise RuntimeError("learning v2 snapshot rejected")
        with self._lock:
            self._learning_v2=deepcopy(value)
        cursor["asset_index"]=(index+1)%len(LEARNING_V2_ASSETS)
        metrics={"decision_read_seconds":round(decision_read_seconds,4),
                 "trade_read_seconds":round(trade_read_seconds,4),
                 "research_seconds":round(research_seconds,4),
                 "registry_seconds":round(registry_seconds,4),
                 "duration_seconds":round(time.monotonic()-job_started,4)}
        diag=current.get("diagnostics") or {}
        self.ns["emit"]("learning_v2_shadow_snapshot",version=LEARNING_V2.VERSION,
                        asset=asset,decisions=len(decision_rows),trades=len(trade_rows),
                        hypotheses=len(current.get("hypotheses") or []),
                        total_hypotheses=len(hypotheses),
                        registry_counts=registry.get("counts") or {},
                        shadow_champions=len(registry.get("shadow_champions") or []),
                        blocked_directional=diag.get("blocked_directional",0),
                        missed_directional=diag.get("missed_directional_episodes",0),
                        learnable_missed=diag.get("learnable_missed_directional",0),
                        unparsed_blocked=diag.get("unparsed_blocked_directional",0),
                        largest_context_n=diag.get("largest_decision_context_n",0),
                        contexts_ge_min=diag.get("decision_contexts_ge_min",0),
                        max_learnable_blocker_n=diag.get("max_learnable_false_block_n_in_context",0),
                        zero_candidate_reason=diag.get("zero_entry_candidate_reason"),
                        known_blockers=diag.get("known_blockers") or {},
                        production_influence=False,**metrics)
        return {"status":"OK","asset":asset,"hypotheses":len(current.get("hypotheses") or []),
                "total_hypotheses":len(hypotheses),"counts":counts,
                "registry_counts":registry.get("counts") or {},
                "metrics":metrics,
                "missed_directional_episodes":(current.get("entry_false_block") or {}).get("missed_directional_episodes",0)},cursor

    def learning_v2_replay(self, context, cursor):
        """Evaluate one bounded Stop/Exit candidate on cached exact-source paths."""
        import veritas_breakout_runtime as BREAKOUT_RUNTIME
        started=time.monotonic()
        result=LEARNING_V2_REPLAY_EVAL.process(
            self.connect,BREAKOUT_RUNTIME.cached_closed_bars,now=clock(),context=context)
        self.ns["emit"]("learning_v2_replay_snapshot",
                        version=LEARNING_V2_REPLAY_EVAL.VERSION,
                        status=result.get("status"),candidate_id=result.get("candidate_id"),
                        kind=result.get("kind"),trades_considered=result.get("trades_considered",0),
                        receipts_written=result.get("receipts_written",0),
                        registry_status=result.get("registry_status"),
                        production_influence=False,
                        duration_seconds=round(time.monotonic()-started,4))
        return result,cursor

    def intelligence(self, context, cursor, *, pg_connect=None):
        pg_connect = self.connect if pg_connect is None else pg_connect
        if cursor.get("phase") == "daily":
            result = SCORECARD.refresh_daily(self.ns, pg_connect, self.ns["learning_progress"](), context=context)
            if result.get("status") in ("OK", "NO_WORK"):
                cursor = dict(cursor, phase="scorecard")
            return result, cursor
        epoch = os.getenv("VERITAS_PRODUCTION_CANDIDATE_EPOCH", "2026-09-30T04:59:29.357862+00:00")
        result = INTELLIGENCE.refresh_snapshot(pg_connect, self.ns["learning_progress"](), epoch,
                                               context=context, cursor=cursor.get("scorecard_work"))
        if result.get("status") in ("OK", "PROGRESS", "NO_WORK") and "cursor" in result:
            cursor = dict(cursor, scorecard_work=result["cursor"])
        if result.get("status") == "OK":
            cursor = dict(cursor, phase="daily")
        return result, cursor

    def progress(self, context, cursor):
        value = self.compute_progress(context)
        context.check()
        if not STORE.publish_snapshot(self.connect, "learning_progress", PROGRESS_VERSION, value,
                                       observed_at=clock()):
            raise RuntimeError("learning progress snapshot rejected")
        self.ns["learning_progress"]._cache = (time.time(), value)
        self.ns["_learning_progress_state"].update(status="OK", last_finished_at=clock().isoformat(), last_error=None)
        return {"status": "OK", "learning_status": value["status"],
                "matched_observations": value.get("matched_observations_each_side")}, cursor

    def compute_progress(self, context):
        fields = "entity_key,decision_ts,outcome_ts,asset,horizon,regime,decision,forward_return,mfe,mae"
        with transaction(self.connect, context) as c:
            early = c.execute("SELECT "+fields+" FROM v90_decision_episodes ORDER BY decision_ts ASC LIMIT 400").fetchall()
            context.check()
            recent = c.execute("SELECT "+fields+" FROM v90_decision_episodes ORDER BY decision_ts DESC LIMIT 400").fetchall()
            context.check()
            trades = c.execute("""(SELECT trade_id,closed_at,total_pnl_fraction FROM shadow_trades
                 WHERE status<>'ACTIVE' AND total_pnl_fraction IS NOT NULL ORDER BY closed_at ASC LIMIT 80)
                 UNION (SELECT trade_id,closed_at,total_pnl_fraction FROM shadow_trades
                 WHERE status<>'ACTIVE' AND total_pnl_fraction IS NOT NULL ORDER BY closed_at DESC LIMIT 80)""").fetchall()
            context.check()
            knowledge = c.execute("SELECT (SELECT COUNT(*) FROM knowledge_sources) current_sources, (SELECT COUNT(*) FROM knowledge_rules) current_rules").fetchone()
        early, recent = independent_slices(early, recent)
        by = []
        for rows in (early, recent):
            groups = {}
            for row in rows:
                groups.setdefault((row["asset"], row["horizon"], row["regime"]), []).append(row)
            by.append(groups)
        pairs = []
        for key in sorted(set(by[0]) & set(by[1])):
            n = min(len(by[0][key]), len(by[1][key]), int(self.ns.get("LEARNING_INDEX_MAX_PER_STRATUM", 30)))
            if n >= int(self.ns.get("LEARNING_INDEX_STRATA_MIN_N", 3)):
                pairs.append((n, self.ns["_learning_metrics_extended"](by[0][key][:n]),
                              self.ns["_learning_metrics_extended"](by[1][key][-n:])))
        def metrics(index):
            out = {"n": sum(p[0] for p in pairs)}
            for field in ("hit_rate", "avg_signed_return", "no_trade_miss_rate", "capture_rate", "wrong_side_rate"):
                vals = [p[index].get(field) for p in pairs if p[index].get(field) is not None]
                out[field] = sum(vals)/len(vals) if vals else None
            return out
        matched = {"baseline": metrics(1), "current": metrics(2), "matched_strata": len(pairs),
                   "matched_observations_each_side": sum(p[0] for p in pairs)}
        ordered = sorted(trades, key=lambda r: (str(r["closed_at"]), str(r["trade_id"])))
        middle = len(ordered)//2
        def trade_metrics(rows):
            values = [float(r["total_pnl_fraction"]) for r in rows]
            return {"n": len(values), "positive_rate": sum(v>0 for v in values)/len(values) if values else None,
                    "avg_pnl": sum(values)/len(values) if values else None}
        shadow = {"status": "MEASURABLE" if middle >= int(self.ns.get("LEARNING_INDEX_TRADE_MIN_N", 30)) else "BUILDING",
                  "baseline": trade_metrics(ordered[:middle]), "current": trade_metrics(ordered[middle:]),
                  "sampling": "DISJOINT_CHRONOLOGICAL_WINDOWS", "decision_influence": False}
        value = INDEX.calculate(matched, shadow)
        value.update(mode=value["mode"]+"_NONOVERLAP_V1", knowledge_growth=dict(knowledge),
                     sampling="BOUNDED_DISJOINT_CHRONOLOGICAL_WINDOWS", outcome_evidence="LEGACY_DIAGNOSTIC_INDEX",
                     autonomy_evidence="SEPARATE_PROSPECTIVE_SOURCE_LOCKED_TRIALS", raw_limit_each_side=400,
                     legacy_index_v1=None, background_refresh=False)
        return value

    def snapshot(self):
        with self._lock:
            result = deepcopy(self._snapshot)
            result["continuous"] = dict(self._stats, ready=self.ready, version=VERSION)
            result["learning_v2"] = deepcopy(self._learning_v2)
        current = clock()
        result["application_status"] = BRIDGE.runtime_status()
        result["profiles"] = [p for p in result.get("profiles", []) if p.get("evidence_valid") is True
                              and BRIDGE.timestamp(p.get("valid_until")) is not None
                              and BRIDGE.timestamp(p["valid_until"]) > current
                              and result["application_status"]["integrity_ready"]
                              and result["application_status"]["profiles_current"]
                              and (p.get("kind") == "CALIBRATION" or result["application_status"]["trade_evidence_ready"])]
        result["jobs"] = self.lane.snapshot().get("periodic", {})
        result["knowledge"] = dict(self.ns.get("knowledge_automation_state") or {})
        result["knowledge"].update(automation_enabled=bool(self.ns.get("KNOWLEDGE_AUTOMATION")),
                                    compiler_configured=bool(self.ns.get("KNOWLEDGE_LLM_ENABLED") and self.ns.get("OPENAI_API_KEY")))
        result["knowledge_validation"] = KNOWLEDGE.snapshot()
        result["outcome_protocol"] = OUTCOME_VERSION
        result["last_error"] = result["continuous"]["last_error"]
        from veritas_operational_status import learning_operation
        result["operational"] = learning_operation(result["jobs"], self.ready)
        result["active_profile_count"] = len(result.get("profiles") or [])
        return result


def install(ns):
    if "_continuous_learning" not in ns:
        ns["_continuous_learning"] = ContinuousLearning(ns)
    return ns["_continuous_learning"]
