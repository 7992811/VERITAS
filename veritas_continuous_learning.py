"""Small durable learning jobs, independent of dashboard requests and FULL scans.

Forecast outcomes use the first verified quote observed after the declared
horizon, on the exact captured provider/instrument. This is a separately
versioned directional experiment; it does not manufacture executable P&L.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import threading
import time

import veritas_autonomous_learning as AUTO
import veritas_learning_bridge as BRIDGE
import veritas_learning_index as INDEX
import veritas_learning_state as STORE
import veritas_price_source as SOURCE

VERSION = "CONTINUOUS_LEARNING_V1"
OUTCOME_VERSION = "FIRST_VERIFIED_QUOTE_AFTER_HORIZON_V1"
PROGRESS_VERSION = "NONOVERLAPPING_LEARNING_PROGRESS_V1"
HORIZON_SECONDS = {"1m": 60, "5m": 300, "1h": 3600, "4h": 14400,
                   "1d": 86400, "3d": 259200, "7d": 604800}
BATCH = 32
MAX_PENDING = 4096
MAX_PROVENANCE_BYTES = 16384


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

    def check(self):
        self.lane.check_budget()


@contextmanager
def transaction(connect, context):
    context.check()
    with connect() as c, c.transaction():
        c.execute("SELECT set_config('statement_timeout',%s,true)", (str(context.sql_timeout_ms),))
        c.execute("SET LOCAL lock_timeout = '250ms'")
        yield c
        context.check()


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
        self._stats = {"status": "collecting", "processed_decisions": 0, "resolved_forecasts": 0,
                       "abstention_observations": 0, "excluded_forecasts": 0,
                       "last_success_at": None, "last_error": None}
        self.trade = None
        jobs = (("learning_bootstrap", self.bootstrap, 10, 6),
                ("learning_ingest", self.ingest, 15, 6),
                ("learning_outcomes", self.outcomes, 15, 6),
                ("learning_candidates", self.candidates, 30, 6),
                ("learning_trade_evidence", self.trades, 30, 6),
                ("learning_progress", self.progress, 120, 6),
                ("learning_memory", self.memory, 300, 6))
        for name, fn, interval, seconds in jobs:
            callback = fn if name == "learning_bootstrap" else self._callback(name, fn)
            self.lane.register_periodic(name, callback, interval_seconds=interval,
                                        lightweight=True, estimated_peak_mb=16, max_seconds=seconds)

    def start(self):
        return self.lane.start_periodic()

    def _callback(self, name, operation):
        def run():
            if not self.ready:
                return {"status": "RETRY", "reason": "LEARNING_BOOTSTRAP_PENDING"}
            context = Budget(self.lane)
            lease = STORE.claim_job(self.connect, name, VERSION, lease_seconds=30)
            if not lease:
                return {"status": "RETRY", "reason": "DURABLE_JOB_LEASE_BUSY"}
            cursor = lease.get("cursor") or {}
            try:
                result, cursor = operation(context, cursor)
                context.check()
                if str(result.get("status", "")).upper() not in ("OK", "NO_WORK", "PROGRESS"):
                    raise RuntimeError("incomplete learning job: "+str(result.get("status")))
                if not STORE.checkpoint_job(self.connect, lease, status="OK", cursor=cursor, result=result):
                    raise RuntimeError("learning lease expired before checkpoint")
                with self._lock:
                    self._stats.update(last_success_at=clock().isoformat(), last_error=None)
                return result
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
        return run

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
        saved = AUTO.snapshot(self.connect)
        progress = STORE.load_snapshot(self.connect, "learning_progress", PROGRESS_VERSION)
        with self._lock:
            self._snapshot = saved
        BRIDGE.update(saved)
        if progress:
            self.ns["learning_progress"]._cache = (0., progress["payload"])
        self.ready = True
        self.ns["emit"]("continuous_learning_ready", version=VERSION,
                        processed=saved.get("counts"), profiles=len(saved.get("profiles") or []))
        return {"status": "OK", "stage": "RESTORED"}

    def ingest(self, context, cursor):
        last_id = int(cursor.get("last_id") or 0)
        inserted = missing = 0
        with transaction(self.connect, context) as c:
            if "last_id" not in cursor:
                # A newly installed evidence protocol starts at its first
                # captured row, without spending hours replaying legacy JSON.
                tail = c.execute("SELECT id,payload ? 'learning_provenance' AS captured FROM ledger_events WHERE event_type='decision' ORDER BY event_ts DESC LIMIT 2000").fetchall()
                captured = [r["id"] for r in tail if r["captured"]]
                last_id = min(captured)-1 if captured else max((r["id"] for r in tail), default=0)
                cursor = dict(cursor, initial_watermark=last_id)
                context.check()
            pending = c.execute("SELECT COUNT(*) n FROM (SELECT id FROM learning_forecasts WHERE status='PENDING' LIMIT %s) q", (MAX_PENDING,)).fetchone()["n"]
            if pending >= MAX_PENDING:
                raise RuntimeError("PENDING_FORECAST_CAPACITY_REACHED")
            rows = c.execute("""SELECT id,entity_key,asset,horizon,
                CASE WHEN octet_length((payload->'learning_provenance')::text)<=%s
                     THEN payload->'learning_provenance' END provenance
                FROM ledger_events WHERE id>%s AND event_type='decision' ORDER BY id LIMIT %s""",
                             (MAX_PROVENANCE_BYTES, last_id, BATCH)).fetchall()
            for row in rows:
                context.check()
                forecast, reason = forecast_from_ledger(row)
                if forecast:
                    got = c.execute("""INSERT INTO learning_forecasts
                        (entity_key,decision_at,due_at,expires_at,asset,horizon,source_key,evidence)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                        ON CONFLICT(entity_key) DO NOTHING RETURNING id""",
                        tuple(forecast[k] for k in ("entity_key", "decision_at", "due_at", "expires_at", "asset", "horizon", "source_key"))+(_json(forecast["evidence"]),)).fetchone()
                    inserted += bool(got)
                else:
                    missing += 1
                last_id = row["id"]
        cursor = dict(cursor, last_id=last_id, imported=int(cursor.get("imported") or 0)+inserted,
                      missing_provenance=int(cursor.get("missing_provenance") or 0)+missing)
        with self._lock:
            self._stats["processed_decisions"] = cursor["imported"]
        return {"status": "OK", "imported": inserted, "excluded_missing_provenance": missing,
                "pending_before": pending, "cursor": last_id}, cursor

    def _quote(self, forecast, now):
        import veritas_position_guard as GUARD
        identity = forecast["evidence"]["quote"]["source_identity"]
        return GUARD.quote_for_position({"asset": forecast["asset"], "payload": {"price_source_lock": identity}}, now=now)

    def outcomes(self, context, cursor):
        now = clock()
        resolved = excluded = 0
        with transaction(self.connect, context) as c:
            rows = c.execute("""SELECT id,entity_key,decision_at,due_at,expires_at,asset,horizon,evidence
                FROM learning_forecasts WHERE status='PENDING' AND due_at<=%s
                ORDER BY due_at,id LIMIT %s FOR UPDATE SKIP LOCKED""", (now, BATCH)).fetchall()
            for row in rows:
                context.check()
                outcome = resolve_forecast(row, {} if now > row["expires_at"] else self._quote(row, now), now=now)
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
        return {"status": "OK", "resolved": resolved, "excluded": excluded}, cursor

    def candidates(self, context, cursor):
        with transaction(self.connect, context) as c:
            rows = c.execute("SELECT id,outcome FROM learning_forecasts WHERE status='READY' ORDER BY decision_at,id LIMIT %s", (BATCH,)).fetchall()
        observations = [r["outcome"] for r in rows if r["outcome"].get("direction") in ("LONG", "SHORT")]
        snapshot = AUTO.run_batch(self.connect, observations, context=context)
        context.check()
        # Deduplication and the model snapshot committed first. A crash before
        # this acknowledgement replays the same immutable IDs without inflation.
        with transaction(self.connect, context) as c:
            if rows:
                c.execute("UPDATE learning_forecasts SET status='LEARNED',learned_at=now(),updated_at=now() WHERE id=ANY(%s) AND status='READY'", ([r["id"] for r in rows],))
            # Raw examples are bounded; learned sufficient statistics and trial
            # audit remain durable. Never delete pending or unconsumed evidence.
            c.execute("DELETE FROM learning_forecasts WHERE id IN (SELECT id FROM learning_forecasts WHERE learned_at<now()-interval '30 days' ORDER BY learned_at LIMIT 64)")
        abstentions = len(rows)-len(observations)
        cursor = dict(cursor, abstentions=int(cursor.get("abstentions") or 0)+abstentions)
        with self._lock:
            self._snapshot = snapshot
            self._stats["abstention_observations"] = cursor["abstentions"]
        BRIDGE.update(snapshot)
        return {"status": "OK", "observations": len(observations), "abstentions": abstentions,
                "counts": snapshot.get("counts"), "profiles": len(snapshot.get("profiles") or [])}, cursor

    def trades(self, context, cursor):
        result = self.trade.process(context)
        if result.get("snapshot"):
            with self._lock:
                self._snapshot = result["snapshot"]
            BRIDGE.update(result["snapshot"])
        return {k: v for k, v in result.items() if k != "snapshot"}, cursor

    def memory(self, context, cursor):
        return self.trade.refresh_memory(context), cursor

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
        result["outcome_protocol"] = OUTCOME_VERSION
        result["last_error"] = result["continuous"]["last_error"]
        result["active_profile_count"] = len(result.get("profiles") or [])
        return result


def install(ns):
    if "_continuous_learning" not in ns:
        ns["_continuous_learning"] = ContinuousLearning(ns)
    return ns["_continuous_learning"]
