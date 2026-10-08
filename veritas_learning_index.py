"""Learning score arithmetic and bounded, comparable daily audit (VERITAS v9.0)."""
from datetime import datetime, timezone
import json
import math
from veritas_maintenance import MaintenanceDeferred

INDEX_VERSION = "2.1"
LABELS = {
    "hit": "Точность направления",
    "miss_control": "Контроль пропущенных движений",
    "edge": "Результат направленных решений",
    "large_move_capture": "Захват сильных движений",
    "wrong_side_control": "Контроль решений против движения",
    "execution": "Результат виртуальных сделок",
}


def bounded(value):
    return max(0.5, min(1.5, value))


def ratio_good(current, baseline, floor=0.20):
    """A stabilised relative CHANGE: identical inputs always contribute 1.0.

    The floor limits sensitivity near zero; it must not penalise the level of
    an unchanged low baseline. Above the floor this equals current / baseline.
    """
    if current is None or baseline is None:
        return 1.0
    current, baseline = float(current), float(baseline)
    if not (math.isfinite(current) and math.isfinite(baseline)):
        raise ValueError("Non-finite learning metric")
    return bounded(1.0 + (current - baseline) / max(baseline, floor))


def calculate(matched, trades):
    baseline, current = matched.get("baseline") or {}, matched.get("current") or {}
    components = {}
    for component, field in (("hit", "hit_rate"), ("large_move_capture", "capture_rate")):
        components[component] = ratio_good(current.get(field), baseline.get(field))
    for component, field in (("miss_control", "no_trade_miss_rate"), ("wrong_side_control", "wrong_side_rate")):
        b, r = baseline.get(field), current.get(field)
        components[component] = ratio_good(None if r is None else 1-float(r), None if b is None else 1-float(b))
    b, r = baseline.get("avg_signed_return"), current.get("avg_signed_return")
    components["edge"] = 1.0 if b is None or r is None else bounded(1+(float(r)-float(b))/0.01)
    if trades.get("status") == "MEASURABLE":
        b, r = trades["baseline"], trades["current"]
        win = ratio_good(r.get("positive_rate"), b.get("positive_rate"))
        pnl = 1.0 if b.get("avg_pnl") is None or r.get("avg_pnl") is None else bounded(1+(float(r["avg_pnl"])-float(b["avg_pnl"]))/0.01)
        components["execution"] = .70*win + .30*pnl
        weights = dict(hit=.32, miss_control=.16, edge=.10, large_move_capture=.12, wrong_side_control=.10, execution=.20)
        mode = "MATCHED_STRATA_PLUS_SHADOW_TRADES"
    else:
        weights = dict(hit=.40, miss_control=.20, edge=.15, large_move_capture=.15, wrong_side_control=.10)
        mode = "MATCHED_STRATA_PROXY_UNTIL_TRADE_SAMPLE"
    points = {key: 100*weight*(components[key]-1) for key, weight in weights.items()}
    score = 100 + sum(points.values())
    if not math.isfinite(score):
        raise ValueError("Non-finite learning index")
    n = int(matched.get("matched_observations_each_side") or 0)
    out = {
        "status": "MEASURABLE" if n >= 20 else "BUILDING",
        "index_vs_start": round(score, 1) if n >= 20 else None,
        "unrounded_index": round(score, 8) if n >= 20 else None,
        "baseline_index": 100, "index_version": INDEX_VERSION, "mode": mode,
        "calculated_at": datetime.now(timezone.utc).isoformat(),
        "confidence": "HIGH" if n >= 120 and trades.get("status") == "MEASURABLE" else "MEDIUM" if n >= 50 else "LOW",
        "matched_strata": matched.get("matched_strata"), "matched_observations_each_side": n,
        "baseline": baseline, "current": current,
        "components": {key: round(value, 8) for key, value in components.items()},
        "component_weights": weights,
        "component_points_vs_baseline": {key: round(value, 8) for key, value in points.items()},
        "shadow_trade_learning": trades,
        "definition": "100 = unchanged matched decision quality; stabilised relative changes are centred on 1. Knowledge counts do not increase the index.",
    }
    out["components"].setdefault("execution", None)
    for field, label in (("hit_rate", "hit_rate_delta_pp"), ("no_trade_miss_rate", "no_trade_miss_delta_pp"), ("capture_rate", "large_move_capture_delta_pp")):
        b, r = baseline.get(field), current.get(field)
        out[label] = None if b is None or r is None else round(100*(r-b), 2)
    return out


def snapshot(lp):
    keys = ("calculated_at", "index_version", "mode", "status", "index_vs_start", "unrounded_index",
            "components", "component_weights", "component_points_vs_baseline", "baseline", "current",
            "matched_strata", "matched_observations_each_side", "shadow_trade_learning", "knowledge_growth")
    return {key: lp.get(key) for key in keys}


def audit_identity_ready(lp):
    """A bootstrap placeholder has no computed formula/sample-mode identity."""
    return all(isinstance(lp.get(key), str) and bool(lp[key].strip())
               for key in ("index_version", "mode"))


def payload(row):
    value = (row or {}).get("payload") or {}
    return value if isinstance(value, dict) else json.loads(value)


def baseline_key(day, lp):
    state = "measurable" if lp.get("index_vs_start") is not None else "building"
    return f"daily_intelligence_msk_{day}:{lp.get('index_version')}:{lp.get('mode')}:{state}"


def component_changes(lp, base):
    previous = base.get("learning_snapshot") or {}
    comparable = (lp.get("index_vs_start") is not None and previous.get("index_vs_start") is not None
                  and lp.get("index_version") == previous.get("index_version")
                  and lp.get("mode") == previous.get("mode")
                  and lp.get("component_weights") == previous.get("component_weights"))
    before, after = previous.get("component_points_vs_baseline") or {}, lp.get("component_points_vs_baseline") or {}
    if not comparable or not before or before.keys() != after.keys():
        return {"component_attribution_status": "NOT_COMPARABLE", "component_deltas_today": []}
    rows = [{"component": key, "label": LABELS[key], "points": round(after[key]-before[key], 8)} for key in after]
    delta = round(lp["index_vs_start"] - previous["index_vs_start"], 2)
    return {"component_attribution_status": "OK", "component_deltas_today": rows,
            "rounding_delta_points": round(delta - sum(row["points"] for row in rows), 8)}


def daily_audit(connection, lp, day, maturity, sources, rules, eligible):
    """Never compare formulas/modes or rewrite an earlier daily baseline."""
    if not audit_identity_ready(lp):
        raise ValueError("LEARNING_AUDIT_IDENTITY_REQUIRED")
    key = baseline_key(day, lp)
    select = "SELECT created_at,payload FROM learning_baselines WHERE baseline_key=%s"
    base = connection.execute(select, (key,)).fetchone()
    created = False
    if not base:
        old = connection.execute("""SELECT created_at,payload FROM learning_baselines
            WHERE baseline_key LIKE %s ORDER BY created_at DESC LIMIT 1""",
            ("daily_intelligence_msk_"+day+"%",)).fetchone()
        previous = payload(old)
        origin = "FORMULA_CHANGE" if old and previous.get("index_version") != lp.get("index_version") else "SAMPLE_MODE_CHANGE" if old else "FIRST_MEASUREMENT"
        data = {"date_msk": day, "learning_index": lp.get("index_vs_start"), "maturity_proxy": maturity,
                "sources": sources, "rules": rules, "eligible_trade_episodes": eligible,
                "captured_at": datetime.now(timezone.utc).isoformat(), "index_version": lp.get("index_version"),
                "mode": lp.get("mode"), "baseline_origin": origin, "learning_snapshot": snapshot(lp)}
        if old:
            data["previous_baseline"] = {"created_at": str(old["created_at"]), "learning_index": previous.get("learning_index"),
                                         "index_version": previous.get("index_version"), "mode": previous.get("mode")}
        inserted = connection.execute("""INSERT INTO learning_baselines(baseline_key,created_at,payload)
            VALUES(%s,NOW(),%s::jsonb) ON CONFLICT DO NOTHING RETURNING baseline_key""",
            (key, json.dumps(data, ensure_ascii=False))).fetchone()
        created = bool(inserted)
        # Read the winning insert, including concurrent requests/instances.
        base = connection.execute(select, (key,)).fetchone()
    bp = payload(base)
    audit = {**component_changes(lp, bp), "baseline_origin": bp.get("baseline_origin"),
             "previous_baseline": bp.get("previous_baseline"), "index_version": lp.get("index_version"),
             "comparison_scope": "SINCE_RECORDED_BASELINE", "history_status": "OK",
             "history_interval_minutes": 15, "history_retention_days": 30}
    # Small snapshots only: at most one per 15 min/formula/mode, retained 30 days.
    # A savepoint keeps optional audit failure from rolling back the baseline.
    try:
        with connection.transaction():
            at = datetime.fromisoformat(lp.get("calculated_at") or datetime.now(timezone.utc).isoformat())
            bucket = at.replace(minute=(at.minute//15)*15, second=0, microsecond=0)
            saved = connection.execute("""INSERT INTO intelligence_score_history(bucket_at,index_version,mode,payload)
                VALUES(%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING RETURNING bucket_at""",
                (bucket, lp.get("index_version"), lp.get("mode"), json.dumps(snapshot(lp), ensure_ascii=False))).fetchone()
            if saved:
                connection.execute("DELETE FROM intelligence_score_history WHERE bucket_at<NOW()-INTERVAL '30 days'")
    except MaintenanceDeferred:
        # Cooperative work limits belong to the scheduler retry path. Preserve
        # the original exception so the enclosing daily transaction rolls back.
        raise
    except Exception as error:
        audit["history_status"] = "ERROR"
        audit["history_error"] = type(error).__name__
    return base, bp, created, audit


def history(connect):
    with connect() as connection:
        connection.execute("SET LOCAL statement_timeout TO '3s'")
        rows = connection.execute("""SELECT bucket_at,payload FROM intelligence_score_history
            WHERE bucket_at>=NOW()-INTERVAL '24 hours' ORDER BY bucket_at DESC LIMIT 100""").fetchall()
    return {"status": "OK", "interval_minutes": 15, "retention_days": 30,
            "items": [{"bucket_at": str(row["bucket_at"]), **payload(row)} for row in reversed(rows)]}
