"""RAM-only scorecard reads and bounded durable background refreshes.

The caller supplies its isolated scoring namespace; formulas and sample
selection remain in veritas_asset_management_intelligence.
"""
from datetime import datetime, timezone
import json
import time


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
                 refresh_status=refresh["status"], last_refresh_error=refresh.get("last_error"))
    return value


def refresh_snapshot(ns, pg_connect, learning_progress, production_epoch, *, context=None):
    """One scheduled refresh; failed SQL/commit never replaces the last good result."""
    from contextlib import contextmanager
    import veritas_learning_state as store
    if not ns["_BUILD_LOCK"].acquire(blocking=False):
        return {"status": "NO_WORK", "reason": "SCORECARD_REFRESH_IN_PROGRESS"}
    def check():
        if context is not None:
            context.check()
    @contextmanager
    def bounded_connect():
        check()
        with pg_connect() as connection, connection.transaction():
            failed = []
            class BoundedConnection:
                def execute(self, sql, args=None):
                    check()
                    if failed:
                        raise failed[0]
                    try:
                        result = connection.execute(sql, args) if args is not None else connection.execute(sql)
                    except Exception as exc:
                        failed.append(exc)
                        raise
                    check()
                    return result
            milliseconds = max(1, min(2000, int(getattr(context, "sql_timeout_ms", 2000))))
            connection.execute("SET LOCAL statement_timeout = '"+str(milliseconds)+"ms'")
            connection.execute("SET LOCAL lock_timeout = '250ms'")
            yield BoundedConnection()
            if failed:
                raise failed[0]
            check()
    try:
        ns["_REFRESH_STATE"] = {"status": "RUNNING", "last_error": None}
        if ns["_RESTORED_EPOCH"] != production_epoch:
            with bounded_connect() as c:
                saved = store.load_snapshot_in_transaction(c, "intelligence_scorecard", ns["VERSION"])
            if saved and saved["payload"].get("production_epoch") == production_epoch:
                restored = saved["payload"].get("scorecard") or {}
                if restored.get("status") == "OK" and restored.get("version") == ns["VERSION"]:
                    observed = saved["observed_at"]
                    observed = datetime.fromisoformat(observed.replace("Z", "+00:00")) if isinstance(observed, str) else observed
                    publish_cache(ns, restored, production_epoch, observed.timestamp())
            ns["_RESTORED_EPOCH"] = production_epoch
            if saved and ns["_SNAPSHOT"] is not None and ns["_SNAPSHOT"][0] == production_epoch:
                ns["_REFRESH_STATE"] = {"status": "RESTORED", "last_error": None}
                return {"status": "OK", "reason": "RESTORED_COMPLETED_SCORECARD"}
        check()
        value = ns["_build_scorecard_unlocked"](bounded_connect, learning_progress, production_epoch,
                                          cache_seconds=0, publish=False)
        check()
        observed = datetime.now(timezone.utc)
        payload = {"status": "OK", "production_epoch": production_epoch, "scorecard": value}
        with bounded_connect() as c:
            if not store.publish_snapshot_in_transaction(c, "intelligence_scorecard", ns["VERSION"],
                                                          payload, observed_at=observed):
                raise RuntimeError("SCORECARD_SNAPSHOT_PUBLICATION_REJECTED")
        publish_cache(ns, value, production_epoch, observed.timestamp())
        ns["_REFRESH_STATE"] = {"status": "OK", "last_error": None}
        return {"status": "OK", "refreshed_at": observed.isoformat()}
    except Exception as exc:
        # Avoid leaking connection strings or database values through HTTP.
        ns["_REFRESH_STATE"] = {"status": "ERROR", "last_error": type(exc).__name__}
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
    if (lp.get("index_version") != index.INDEX_VERSION or not lp.get("mode")
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
