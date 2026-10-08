"""RAM-only scorecard reads and bounded durable background refreshes.

The caller supplies its isolated scoring namespace; formulas and sample
selection remain in veritas_asset_management_intelligence.
"""
from datetime import datetime, timezone
import json
import time
from veritas_maintenance import MaintenanceDeferred


def publish_cache(ns, value, epoch, observed_at):
    ns["_CACHE"].update(at=observed_at, epoch=epoch, value=value)
    # A single immutable reference is swapped after a complete calculation.
    # HTTP readers never acquire the build lock or observe partial components.
    ns["_SNAPSHOT"] = (epoch, observed_at, value)


def cached_scorecard(ns, production_epoch, max_age_seconds=120):
    """RAM-only last completed result, including honest age/failure metadata."""
    from copy import deepcopy
    saved, refresh = ns["_SNAPSHOT"], dict(ns["_REFRESH_STATE"])
    value = {"status": "BUILDING", "version": ns["VERSION"], "score": None,
             "max_score": 100, "components": {}, "component_status": {}}
    age = observed = None
    if saved is not None and saved[0] == production_epoch:
        value = deepcopy(saved[2])
        age = max(0., time.time()-saved[1])
        observed = datetime.fromtimestamp(saved[1], timezone.utc).isoformat()
    value.update(calculated_at=observed, cache_age_seconds=age,
                 stale=age is None or age > max_age_seconds,
                 background_refresh=age is None or age > max_age_seconds or refresh["status"] == "RUNNING",
                 refresh_status=refresh["status"], last_refresh_error=refresh.get("last_error"),
                 refresh_stage=refresh.get("stage"), refresh_processed=refresh.get("processed"),
                 refresh_sample_n=refresh.get("sample_n"),
                 refresh_last_sql_timeout_ms=refresh.get("last_sql_timeout_ms"),
                 refresh_last_sql_budget_seconds=refresh.get("last_sql_budget_seconds"))
    return value


WORK_VERSION = "AMI_STAGED_INPUTS_V1"
WORK_SLOT = "intelligence_scorecard_work"
WORK_BYTES = 196608
DECISION_CHUNK = 64


def _knowledge_applied(ns, episode):
    payload = episode.get("payload") or {}
    adjustment = payload.get("knowledge_cio_adjustment") or {}
    try:
        score = float(adjustment.get("score_with_experience")
                      if adjustment.get("score_with_experience") is not None
                      else adjustment.get("score") or 0.0)
    except Exception:
        score = 0.0
    return bool(payload.get("knowledge_shadow_matches") and abs(score) > 1e-9)


def _consume_decision_chunk(ns, work, rows):
    """Continue the original ordered reducer, retaining only its last 360 rows."""
    previous = {}
    for asset, horizon, decision, regime, stamp in work.get("previous", []):
        previous[(asset, horizon)] = {"decision": decision, "regime": regime,
            "ts": datetime.fromisoformat(stamp) if stamp is not None else None}
    selected = ns["_independent_episodes"](rows, 360, previous=previous)
    episodes = work.setdefault("episodes", [])
    for episode in selected:
        episodes.append({key: episode[key] for key in
                         ("asset", "horizon", "regime", "decision", "forward_return", "reference_decision")}
                        | {"knowledge_applied": _knowledge_applied(ns, episode)})
    work["episodes"] = episodes[-360:]
    work["previous"] = [[asset, horizon, value["decision"], value["regime"],
                         value["ts"].isoformat() if value["ts"] is not None else None]
                        for (asset, horizon), value in previous.items()]


def refresh_snapshot(ns, pg_connect, learning_progress, production_epoch, *, context=None, cursor=None):
    """One durable microstep; incomplete inputs never replace the public score.

    Frozen ID pairs retain the original 2,200-row sample and tie ordering.
    Only projected chunks, reducer state and the selected 360 compact episodes
    cross steps. Existing scoring formulas run once all inputs are complete.
    """
    from contextlib import contextmanager
    import uuid
    import veritas_learning_state as store
    from veritas_learning_memory import ami_decision_sample, ami_decision_chunk
    if not ns["_BUILD_LOCK"].acquire(blocking=False):
        return {"status": "NO_WORK", "reason": "SCORECARD_REFRESH_IN_PROGRESS", "cursor": cursor or {}}
    started = time.monotonic()
    active_stage = "restore"
    last_sql = {"timeout_ms": None, "budget_seconds": None}
    def check():
        if context is not None:
            context.check()
    def remaining():
        lane = getattr(context, "lane", None)
        if lane is not None:
            return float(lane.current_budget().get("remaining_seconds", 0))
        return max(0., 6.-(time.monotonic()-started))
    @contextmanager
    def bounded_connect():
        check()
        with pg_connect() as connection, connection.transaction():
            failed = []
            class BoundedConnection:
                def __init__(self, initial_sql_timeout_ms):
                    self.initial_sql_timeout_ms = initial_sql_timeout_ms
                    self.sql_timeout_ms = initial_sql_timeout_ms
                def execute(self, sql, args=None):
                    check()
                    if failed:
                        raise failed[0]
                    # Diagnostic scalars describe the last client SQL call;
                    # no SQL text, parameters or source timestamps are copied.
                    last_sql.update(timeout_ms=self.sql_timeout_ms, budget_seconds=None)
                    try:
                        last_sql["budget_seconds"] = round(max(0., remaining()), 4)
                    except Exception:
                        pass
                    try:
                        result = connection.execute(sql, args) if args is not None else connection.execute(sql)
                    except Exception as exc:
                        failed.append(exc)
                        raise
                    check()
                    return result
            milliseconds = max(1, min(2000, int(getattr(context, "sql_timeout_ms", 2000))))
            connection.execute("SET LOCAL statement_timeout = '"+str(milliseconds)+"ms'; "
                               "SET LOCAL lock_timeout = '250ms'")
            yield BoundedConnection(milliseconds)
            if failed:
                raise failed[0]
            check()
    value = restored = work = None
    deferred = False
    try:
        now = time.time()
        if (cursor or {}).get("next_refresh_at", 0) > now and ns["_SNAPSHOT"] is not None and ns["_SNAPSHOT"][0] == production_epoch:
            return {"status": "NO_WORK", "reason": "SCORECARD_REFRESH_NOT_DUE", "cursor": cursor}
        ns["_REFRESH_STATE"] = {"status": "RUNNING", "last_error": None}
        if ns["_RESTORED_EPOCH"] != production_epoch:
            with bounded_connect() as c:
                saved = store.load_snapshot_in_transaction(c, "intelligence_scorecard", ns["VERSION"])
            if saved and saved["payload"].get("production_epoch") == production_epoch:
                candidate = saved["payload"].get("scorecard") or {}
                if candidate.get("status") == "OK" and candidate.get("version") == ns["VERSION"]:
                    observed = saved["observed_at"]
                    observed = datetime.fromisoformat(observed.replace("Z", "+00:00")) if isinstance(observed, str) else observed
                    restored = candidate
                    publish_cache(ns, restored, production_epoch, observed.timestamp())
            ns["_RESTORED_EPOCH"] = production_epoch
            ns["_REFRESH_STATE"] = {"status": "RESTORED" if restored else "COLLECTING", "last_error": None, "stage": "sample"}
            # Restore is deliberately its own small step, even when empty.
            return {"status": "OK" if restored else "PROGRESS",
                    "reason": "RESTORED_COMPLETED_SCORECARD" if restored else "SCORECARD_RESTORE_CHECKED",
                    "cursor": {"next_refresh_at": observed.timestamp()+120} if restored else {"stage": "sample"}}
        active_stage = "work_state"
        with bounded_connect() as c:
            saved = store.load_snapshot_in_transaction(c, WORK_SLOT, WORK_VERSION, for_update=True)
            work = saved["payload"] if saved else None
            if (not work or work.get("epoch") != production_epoch or work.get("ami_version") != ns["VERSION"]
                    or (work.get("stage") == "complete" and work.get("next_refresh_at", 0) <= now)
                    or now-float(work.get("started_at", 0)) > 1800):
                active_stage = "sample"
                sample = ami_decision_sample(c)
                # The original reducer does this stable sort; ties retain their
                # original selected order, even when they span chunk boundaries.
                sample.sort(key=lambda row: str(row.get("event_ts") or ""))
                work = {"status": "BUILDING", "epoch": production_epoch, "ami_version": ns["VERSION"],
                        "cycle_id": uuid.uuid4().hex, "started_at": now, "stage": "decisions", "offset": 0,
                        "pairs": [[row["decision_id"], row["outcome_id"]] for row in sample],
                        "episodes": [], "previous": [], "learning_progress": {
                            key: (learning_progress or {}).get(key) for key in
                            ("status", "index_vs_start", "calculated_at", "index_version", "mode")}}
            elif work["stage"] == "complete":
                active_stage = "restore_complete"
                # Recover the publication/outer-checkpoint gap without
                # repeating the sample or losing the completed RAM value.
                completed = store.load_snapshot_in_transaction(c, "intelligence_scorecard", ns["VERSION"])
                if not completed or completed["payload"].get("production_epoch") != production_epoch:
                    raise RuntimeError("COMPLETED_SCORECARD_SNAPSHOT_MISSING")
                value = completed["payload"]["scorecard"]
                observed = completed["observed_at"]
                if isinstance(observed, str):
                    observed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
            elif work["stage"] == "decisions":
                active_stage = "decisions"
                phase_started, chunks = time.monotonic(), 0
                query_timeout = c.initial_sql_timeout_ms
                while work["offset"] < len(work["pairs"]) and chunks < 8:
                    # Leave time for this durable write and the caller's fenced
                    # job checkpoint. Short remaining slices can still make
                    # progress with a smaller query timeout, never a larger cap.
                    allowance = remaining()-2.2
                    if allowance < .2 or (chunks and time.monotonic()-phase_started >= 1.5):
                        break
                    timeout = max(1, min(query_timeout, int(allowance*1000)))
                    if timeout < query_timeout:
                        c.execute("SET LOCAL statement_timeout = '"+str(timeout)+"ms'")
                        query_timeout = timeout
                        c.sql_timeout_ms = timeout
                        if remaining() <= 2.2:
                            break
                    # Scale the requested work with the timeout already in
                    # force; every frozen pair still advances the same reducer.
                    pair_count = max(1, min(DECISION_CHUNK, DECISION_CHUNK*query_timeout//2000))
                    pairs = work["pairs"][work["offset"]:work["offset"]+pair_count]
                    rows = ami_decision_chunk(c, pairs)
                    if len(rows) != len(pairs):
                        raise RuntimeError("SCORECARD_FROZEN_SAMPLE_CHANGED")
                    _consume_decision_chunk(ns, work, rows)
                    work["offset"] += len(rows)
                    chunks += 1
                    rows = None
                    check()
                deferred = chunks == 0 and work["offset"] < len(work["pairs"])
                if work["offset"] == len(work["pairs"]):
                    work["sample_n"] = len(work.pop("pairs"))
                    work.pop("previous", None)
                    work["stage"] = "portfolio"
            elif work["stage"] == "portfolio":
                active_stage = "portfolio"
                work["portfolio"] = ns["_query_fresh_portfolio"](c, production_epoch)
                work["stage"] = "learning"
            elif work["stage"] == "learning":
                active_stage = "learning"
                work["learning"] = ns["_query_learning"](c)
                work["stage"] = "knowledge"
            elif work["stage"] == "knowledge":
                active_stage = "knowledge"
                knowledge = ns["_query_knowledge"](c, [])
                applied = [e for e in work["episodes"] if e["knowledge_applied"]]
                metrics = ns["_decision_metrics"](applied) if applied else {}
                knowledge.update(application_n=len(applied),
                    application_rate=len(applied)/len(work["episodes"]) if work["episodes"] else 0.,
                    applied_hit_rate=metrics.get("hit_rate"), applied_utility=metrics.get("avg_normalized_utility"))
                work["knowledge"] = knowledge
                work["stage"] = "publish"
            elif work["stage"] == "publish":
                active_stage = "publish"
                @contextmanager
                def ready_connection():
                    yield c
                value = ns["_build_scorecard_unlocked"](ready_connection, work["learning_progress"], production_epoch,
                    cache_seconds=0, publish=False, inputs=work)
                check()
                observed = datetime.now(timezone.utc)
                payload = {"status": "OK", "production_epoch": production_epoch, "scorecard": value}
                if not store.publish_snapshot_in_transaction(c, "intelligence_scorecard", ns["VERSION"],
                                                              payload, observed_at=observed):
                    raise RuntimeError("SCORECARD_SNAPSHOT_PUBLICATION_REJECTED")
                work = {"status": "COMPLETE", "epoch": production_epoch, "ami_version": ns["VERSION"],
                        "stage": "complete", "cycle_id": work["cycle_id"], "offset": work["offset"],
                        "sample_n": work["sample_n"], "started_at": work["started_at"],
                        "next_refresh_at": observed.timestamp()+120}
            else:
                active_stage = "unknown"
                raise RuntimeError("UNKNOWN_SCORECARD_WORK_STAGE")
            active_stage = "checkpoint"
            check()
            store._json(work, WORK_BYTES)
            if not deferred and not store.publish_snapshot_in_transaction(c, WORK_SLOT, WORK_VERSION, work,
                                                                           observed_at=datetime.now(timezone.utc)):
                raise RuntimeError("SCORECARD_WORK_PUBLICATION_REJECTED")
            active_stage = "commit"
        check()
        if value is not None:
            publish_cache(ns, value, production_epoch, observed.timestamp())
        ns["_REFRESH_STATE"] = {"status": "DEFERRED" if deferred else "OK" if value is not None else "COLLECTING", "last_error": None,
                                "stage": work["stage"], "processed": work["offset"],
                                "sample_n": work.get("sample_n", len(work.get("pairs", [])))}
        return {"status": "DEFERRED_SCORECARD_BUDGET" if deferred else "OK" if value is not None else "PROGRESS", "stage": work["stage"],
                "reason": "CHECKPOINT_TIME_RESERVED" if deferred else None,
                "processed": work["offset"], "sample_n": ns["_REFRESH_STATE"]["sample_n"],
                "cursor": {key: work[key] for key in ("cycle_id", "stage", "offset", "next_refresh_at") if key in work}}
    except Exception as exc:
        cooperative = isinstance(exc, MaintenanceDeferred)
        status = exc.status if cooperative else "ERROR"
        error_type = None if cooperative else type(exc).__name__
        ns["_REFRESH_STATE"] = {"status": status, "last_error": error_type,
                                "stage": active_stage,
                                "last_sql_timeout_ms": last_sql["timeout_ms"],
                                "last_sql_budget_seconds": last_sql["budget_seconds"]}
        try:
            emit = getattr(getattr(context, "lane", None), "emit", None)
            if callable(emit):
                emit("ami_refresh_deferred" if cooperative else "ami_refresh_error",
                     status=status, error_type=error_type,
                     stage=active_stage, last_sql_timeout_ms=last_sql["timeout_ms"],
                     last_sql_budget_seconds=last_sql["budget_seconds"])
        except Exception:
            # Diagnostic delivery cannot replace the original error or deferral.
            pass
        raise
    finally:
        ns["_BUILD_LOCK"].release()


def refresh_daily(ns, pg_connect, learning_progress, *, context=None):
    """Persist the existing comparable daily audit in one bounded background step.

    Legacy all-trade maturity/capture requires a large payload scan. It is not
    the AMI score and remains unavailable here; the realized learning index,
    original baselines and component attribution retain their exact meanings.
    """
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    import math
    import veritas_learning_index as index

    lp = dict(learning_progress or {})
    if (lp.get("index_version") != index.INDEX_VERSION or not index.audit_identity_ready(lp)
            or str(lp.get("status", "")).upper() not in ("MEASURABLE", "BUILDING")):
        return {"status": "DEFERRED_LEARNING_UNAVAILABLE"}
    local = datetime.now(ZoneInfo("Europe/Moscow"))
    day = local.strftime("%Y-%m-%d")
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start, end = start.astimezone(timezone.utc), (start+timedelta(days=1)).astimezone(timezone.utc)
    try:
        measured = datetime.fromisoformat(str(lp.get("calculated_at") or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        measured = None
    if measured is None or measured.tzinfo is None or not start <= measured <= local:
        return {"status": "DEFERRED_LEARNING_CLOCK", "reason": "CURRENT_MSK_DAY_MEASUREMENT_REQUIRED"}
    def check():
        if context is not None:
            context.check()
    def number(value):
        if value is None or isinstance(value, bool):
            return None
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    current = number(lp.get("index_vs_start"))
    if str(lp.get("status", "")).upper() != "MEASURABLE":
        current = None
    lp["index_vs_start"] = current
    check()
    with pg_connect() as connection, connection.transaction():
        milliseconds = max(1, min(2000, int(getattr(context, "sql_timeout_ms", 2000))))
        connection.execute("SET LOCAL statement_timeout = '"+str(milliseconds)+"ms'")
        connection.execute("SET LOCAL lock_timeout = '250ms'")
        class BoundedAudit:
            def execute(self, sql, args=None):
                check()
                result = connection.execute(sql, args) if args is not None else connection.execute(sql)
                check()
                return result
            def transaction(self):
                return connection.transaction()
        c = BoundedAudit()
        # The previous daily counters, using scalar columns only. No trade or
        # ledger JSON is fetched, and each result is one fixed-size row.
        totals = c.execute("""SELECT s.sources,s.sources_today,r.rules,r.rules_today,
              e.total_eligible,e.today_eligible,
              (SELECT COUNT(DISTINCT entity_key) FROM ledger_events
               WHERE event_type='outcome' AND event_ts>=%s AND event_ts<%s) AS outcomes_today
            FROM (SELECT COUNT(*) AS sources,
                    COUNT(*) FILTER (WHERE imported_at>=%s AND imported_at<%s) AS sources_today
                  FROM knowledge_sources) s
            CROSS JOIN (SELECT COUNT(*) AS rules,
                    COUNT(*) FILTER (WHERE created_at>=%s AND created_at<%s) AS rules_today
                  FROM knowledge_rules) r
            CROSS JOIN (SELECT COUNT(*) FILTER (WHERE learning_eligible=TRUE) AS total_eligible,
                    COUNT(*) FILTER (WHERE learning_eligible=TRUE AND closed_at>=%s AND closed_at<%s) AS today_eligible
                  FROM v90_learning_episodes) e""", (start, end)*4).fetchone()
        base, bp, created, audit = index.daily_audit(
            c, lp, day, None, totals["sources"], totals["rules"], totals["total_eligible"])
        if audit.get("history_status") != "OK":
            raise RuntimeError("DAILY_HISTORY_"+str(audit.get("history_error") or "UNAVAILABLE"))
        check()
    check()
    previous = number(bp.get("learning_index"))
    # The audit also checks weights; a same-version/mode weight change must
    # never appear as measured learning against the unchanged stored baseline.
    comparable = audit.get("component_attribution_status") == "OK"
    delta = round(current-previous, 2) if comparable and current is not None and previous is not None else None
    value = {
        "status": "OK", **audit, "date_msk": day, "timezone": "Europe/Moscow",
        "baseline_at": str(base["created_at"]), "baseline_learning_index": previous,
        "current_learning_index": current, "learning_index_delta_today": delta,
        "baseline_maturity_index": number(bp.get("maturity_proxy")), "current_maturity_index": None,
        "maturity_index_delta_today": None, "intelligence_delta_today": delta,
        "current_intelligence_metric": current, "delta_basis": "CORE_LEARNING_INDEX" if current is not None else "UNAVAILABLE",
        "trend": "BUILDING" if delta is None else "UP" if delta > .05 else "DOWN" if delta < -.05 else "FLAT",
        "learning_status": lp.get("status"), "learning_confidence": lp.get("confidence"),
        "trade_learning_episodes_today": totals["today_eligible"], "decision_outcomes_today": totals["outcomes_today"],
        "knowledge_sources_added_today": totals["sources_today"], "knowledge_rules_added_today": totals["rules_today"],
        "current_sources": totals["sources"], "current_rules": totals["rules"],
        "current_closed_trades": None, "current_win_rate": None, "current_avg_capture_ratio": None,
        "current_telemetry_coverage": None, "baseline_created_now": created, "baseline_proxy_backfilled_now": False,
        "calculated_at": datetime.now(timezone.utc).isoformat(), "learning_calculated_at": lp.get("calculated_at"),
        "definition": "Daily change in the comparable realized learning index since its recorded baseline; knowledge additions are informational. Legacy maturity is unavailable without its original evidence.",
    }
    ns["_v90_daily_intelligence_cache"].update(at=time.time(), value=value)
    return {"status": "OK", "refreshed_at": value["calculated_at"], "baseline_created_now": created}


def startup_snapshot(ns, production_epoch, delay_seconds=12):
    """Emit one bounded diagnostic snapshot after service startup."""
    try:
        time.sleep(max(0.0, float(delay_seconds or 0.0)))
        # The maintenance lane schedules SQL work after durable bootstrap.
        # Startup logging must not compete with it on a separate connection.
        value = cached_scorecard(ns, production_epoch)
        b = value.get("benchmarks") or {}
        stateless = b.get("stateless_ai") or {}
        print(json.dumps({
            "event": "V90_ASSET_MANAGEMENT_INTELLIGENCE",
            "version": value.get("version"),
            "score": value.get("score"),
            "stage": value.get("stage"),
            "confidence": value.get("confidence"),
            "components": value.get("components"),
            "component_status": value.get("component_status"),
            "coverage": value.get("coverage"),
            "initial_veritas": b.get("initial_veritas_decision_learning"),
            "stateless_ai": {
                "status": stateless.get("status"),
                "sample_n": stateless.get("sample_n"),
                "hit_rate_delta_pp": stateless.get("hit_rate_delta_pp"),
                "large_move_capture_delta_pp": stateless.get("large_move_capture_delta_pp"),
                "normalized_utility_delta": stateless.get("normalized_utility_delta"),
            },
            "fresh_candidate_trades": ((value.get("evidence") or {}).get("fresh_candidate_trades") or {}).get("n"),
        }, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)
    except Exception as ex:
        print(json.dumps({
            "event": "V90_ASSET_MANAGEMENT_INTELLIGENCE_ERROR",
            "error": f"{type(ex).__name__}: {ex}"[:300],
        }, ensure_ascii=False, separators=(",", ":")), flush=True)
