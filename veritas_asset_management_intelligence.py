"""Evidence-weighted asset-management intelligence for VERITAS.

This is not an IQ score. It measures demonstrated capability to make market
decisions, convert them into post-cost portfolio outcomes, apply validated
knowledge, manage movement/risk, accumulate independent experience, and improve
through closed-loop learning.

A stateless reference AI is reconstructed from saved agent votes on the same
completed episodes. It deliberately ignores memory, adaptive weights,
knowledge adjustments and trade-learning feedback.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import time

VERSION = "ami-v1.0"
_CACHE = {"at": 0.0, "epoch": None, "value": None}

MOVE_THRESHOLDS = {
    "5m": 0.0015,
    "1h": 0.012,
    "4h": 0.020,
    "1d": 0.035,
    "3d": 0.060,
    "7d": 0.100,
}
EPISODE_GAPS = {
    "5m": 900,
    "1h": 1800,
    "4h": 7200,
    "1d": 21600,
    "3d": 43200,
    "7d": 86400,
}
BAD_LEARNING_ATTRS = {
    "ENTRY_DIRECTION_ERROR", "EDGE_OVERFORECAST", "COST_DRAG",
}


def _clip(x, lo=0.0, hi=1.0):
    try:
        return max(lo, min(hi, float(x)))
    except Exception:
        return lo


def _scale(x, lo, hi):
    if x is None or hi <= lo:
        return 0.0
    return _clip((float(x) - lo) / (hi - lo))


def _j(v):
    if isinstance(v, dict):
        return v
    try:
        return json.loads(v or "{}")
    except Exception:
        return {}


def _mean(values):
    vals = [float(x) for x in values if x is not None and math.isfinite(float(x))]
    return sum(vals) / len(vals) if vals else None


def _threshold(horizon):
    return MOVE_THRESHOLDS.get(str(horizon), MOVE_THRESHOLDS["1d"])


def _static_ai_decision(payload):
    """Equal-weight confidence vote: a deliberately simple AI without memory."""
    agents = (payload or {}).get("agents") or []
    signed = total = 0.0
    for a in agents:
        if not isinstance(a, dict):
            continue
        d = str(a.get("direction") or "").upper()
        if d not in ("LONG", "SHORT"):
            continue
        try:
            c = max(0.0, min(1.0, float(a.get("confidence") or 0.0)))
        except Exception:
            continue
        if c <= 0:
            continue
        signed += c if d == "LONG" else -c
        total += c
    if total <= 1e-12:
        return "NO_TRADE"
    margin = signed / total
    if margin >= 0.18:
        return "LONG"
    if margin <= -0.18:
        return "SHORT"
    return "NO_TRADE"


def _independent_episodes(rows, limit=360):
    ordered = sorted(rows, key=lambda r: str(r.get("event_ts") or ""))
    last = {}
    out = []
    for raw in ordered:
        r = dict(raw)
        dp, op = _j(r.get("dp")), _j(r.get("op"))
        fr = op.get("forward_return")
        if fr is None:
            continue
        try:
            fr = float(fr)
        except Exception:
            continue
        asset, horizon = str(r.get("asset") or ""), str(r.get("horizon") or "")
        decision = str(dp.get("research_decision") or dp.get("decision") or "NO_TRADE").upper()
        regime = str(dp.get("regime") or "UNKNOWN")
        ts = r.get("event_ts")
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                ts = None
        key = (asset, horizon)
        prev = last.get(key)
        fresh = prev is None or decision != prev["decision"] or regime != prev["regime"]
        if not fresh and ts is not None and prev.get("ts") is not None:
            fresh = (ts - prev["ts"]).total_seconds() > EPISODE_GAPS.get(horizon, 86400)
        last[key] = {"decision": decision, "regime": regime, "ts": ts}
        if not fresh:
            continue
        out.append({
            "event_ts": r.get("event_ts"), "asset": asset, "horizon": horizon,
            "regime": regime, "decision": decision, "forward_return": fr,
            "payload": dp, "reference_decision": _static_ai_decision(dp),
        })
    return out[-int(limit):]


def _decision_metrics(episodes, decision_key="decision"):
    directional = hits = large = captured = wrong = no_trade = missed = 0
    utility = []
    for e in episodes:
        d = str(e.get(decision_key) or "NO_TRADE")
        fr, th = float(e.get("forward_return") or 0.0), _threshold(e.get("horizon"))
        large_move = abs(fr) >= th
        if d in ("LONG", "SHORT"):
            directional += 1
            sr = fr if d == "LONG" else -fr
            hits += 1 if sr > 0 else 0
            utility.append(max(-2.0, min(2.0, sr / max(th, 1e-9))))
        else:
            no_trade += 1
            if large_move:
                missed += 1
                utility.append(-1.0)
            else:
                utility.append(0.15)
        if large_move:
            large += 1
            correct = (d == "LONG" and fr > 0) or (d == "SHORT" and fr < 0)
            opposite = d in ("LONG", "SHORT") and not correct
            captured += 1 if correct else 0
            wrong += 1 if opposite else 0
    return {
        "n": len(episodes),
        "directional_n": directional,
        "hit_rate": hits / directional if directional else None,
        "avg_normalized_utility": _mean(utility),
        "large_moves": large,
        "capture_rate": captured / large if large else None,
        "wrong_side_rate": wrong / large if large else None,
        "no_trade_miss_rate": missed / no_trade if no_trade else None,
    }


def _query_decision_episodes(c):
    rows = c.execute("""
      SELECT d.event_ts,d.asset,d.horizon,d.payload AS dp,o.payload AS op
      FROM ledger_events d
      JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'
      WHERE d.event_type='decision' AND o.payload ? 'forward_return'
      ORDER BY d.event_ts DESC
      LIMIT 2200
    """).fetchall()
    return _independent_episodes([dict(r) for r in rows or []], 360)


def _query_fresh_portfolio(c, epoch):
    r = c.execute("""
      SELECT COUNT(*) AS n,
             COUNT(*) FILTER(WHERE net_pnl_rub>0) AS wins,
             COALESCE(SUM(net_pnl_rub),0) AS net,
             COALESCE(AVG(net_pnl_rub),0) AS avg_net,
             COALESCE(AVG(return_on_entry_nav),0) AS avg_return,
             COALESCE(SUM(CASE WHEN net_pnl_rub>0 THEN net_pnl_rub ELSE 0 END),0) AS gross_win,
             ABS(COALESCE(SUM(CASE WHEN net_pnl_rub<0 THEN net_pnl_rub ELSE 0 END),0)) AS gross_loss
      FROM paper_trades
      WHERE portfolio_name IN ('Champion','Challenger')
        AND opened_at >= %s::timestamptz
        AND (closed_at IS NOT NULL OR status IN ('CLOSED','CLOSE','EXITED'))
    """, (epoch,)).fetchone() or {}
    dd = c.execute("""
      WITH x AS (
        SELECT portfolio_name,observed_at,nav_rub,
               MAX(nav_rub) OVER (
                 PARTITION BY portfolio_name ORDER BY observed_at
                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
               ) AS hwm
        FROM paper_nav_history
        WHERE portfolio_name IN ('Champion','Challenger')
          AND observed_at >= %s::timestamptz
      )
      SELECT COALESCE(MAX(CASE WHEN hwm>0 THEN (hwm-nav_rub)/hwm ELSE 0 END),0) AS dd
      FROM x
    """, (epoch,)).fetchone() or {}
    n, wins = int(r.get("n") or 0), int(r.get("wins") or 0)
    gw, gl = float(r.get("gross_win") or 0.0), float(r.get("gross_loss") or 0.0)
    return {
        "n": n, "wins": wins, "win_rate": wins / n if n else None,
        "net_pnl_rub": float(r.get("net") or 0.0),
        "avg_net_pnl_rub": float(r.get("avg_net") or 0.0),
        "avg_return_on_entry_nav": float(r.get("avg_return") or 0.0),
        "profit_factor": (gw / gl) if gl > 1e-9 else (9.99 if gw > 0 else None),
        "max_drawdown": float(dd.get("dd") or 0.0),
    }


def _query_learning(c):
    rows = c.execute("""
      SELECT closed_at,asset,horizon,regime,capture_ratio,movement_realization_ratio,
             giveback_pct,primary_attribution,attributions,net_pnl_rub
      FROM v90_learning_episodes
      WHERE learning_eligible=TRUE
        AND primary_attribution<>'ADMINISTRATIVE_EXIT_EXCLUDED'
      ORDER BY closed_at ASC
      LIMIT 500
    """).fetchall()
    z = []
    for r0 in rows or []:
        r = dict(r0)
        attrs = r.get("attributions")
        if isinstance(attrs, str):
            try:
                attrs = json.loads(attrs)
            except Exception:
                attrs = []
        r["attributions"] = attrs or []
        z.append(r)
    n = len(z)
    capture = _mean([r.get("capture_ratio") for r in z])
    realization = _mean([r.get("movement_realization_ratio") for r in z])
    giveback = _mean([r.get("giveback_pct") for r in z])
    def rate(name):
        return sum(1 for r in z if name in set(r.get("attributions") or [])) / n if n else 0.0
    half = max(1, n // 2)
    early, recent = z[:half], z[-half:]
    def bad_rate(a):
        return sum(1 for r in a if set(r.get("attributions") or []) & BAD_LEARNING_ATTRS) / len(a) if a else None
    def avg_real(a):
        return _mean([r.get("movement_realization_ratio") for r in a])
    return {
        "n": n, "avg_capture_ratio": capture,
        "avg_movement_realization_ratio": realization,
        "avg_giveback_pct": giveback,
        "entry_error_rate": rate("ENTRY_DIRECTION_ERROR"),
        "cost_drag_rate": rate("COST_DRAG"),
        "exit_capture_error_rate": rate("EXIT_CAPTURE_ERROR"),
        "stop_error_rate": rate("STOP_STRUCTURE_ERROR"),
        "overforecast_rate": rate("EDGE_OVERFORECAST"),
        "early_bad_rate": bad_rate(early), "recent_bad_rate": bad_rate(recent),
        "early_realization": avg_real(early), "recent_realization": avg_real(recent),
    }


def _query_knowledge(c, episodes):
    totals = c.execute("""
      SELECT
        (SELECT COUNT(*) FROM knowledge_sources) AS sources,
        (SELECT COUNT(*) FROM knowledge_rules) AS rules
    """).fetchone() or {}
    try:
        oos = c.execute("""
          SELECT COUNT(DISTINCT rule_id) AS n
          FROM knowledge_backtest_oos_stats
          WHERE sample='OOS' AND n>=20 AND hit_rate>=0.53 AND avg_signed_return>0
        """).fetchone() or {}
        validated = int(oos.get("n") or 0)
    except Exception:
        validated = 0
    applied = []
    for e in episodes:
        p = e.get("payload") or {}
        adj = p.get("knowledge_cio_adjustment") or {}
        matches = p.get("knowledge_shadow_matches") or []
        try:
            score = float(adj.get("score_with_experience")
                          if adj.get("score_with_experience") is not None
                          else adj.get("score") or 0.0)
        except Exception:
            score = 0.0
        if matches and abs(score) > 1e-9:
            applied.append(e)
    met = _decision_metrics(applied) if applied else {}
    return {
        "sources": int(totals.get("sources") or 0),
        "rules": int(totals.get("rules") or 0),
        "validated_oos_rules": validated,
        "application_n": len(applied),
        "application_rate": len(applied) / len(episodes) if episodes else 0.0,
        "applied_hit_rate": met.get("hit_rate"),
        "applied_utility": met.get("avg_normalized_utility"),
    }


def _stage(score):
    if score >= 85:
        return "ВЫСОКО ПОДТВЕРЖДЁННЫЙ"
    if score >= 70:
        return "ПРОДВИНУТЫЙ"
    if score >= 55:
        return "РАБОЧИЙ"
    if score >= 40:
        return "РАЗВИВАЮЩИЙСЯ"
    return "НАЧАЛЬНЫЙ"


def _baseline(c, score, components):
    key = "asset_management_intelligence_v1_rollout"
    try:
        row = c.execute(
            "SELECT created_at,payload FROM learning_baselines WHERE baseline_key=%s", (key,)
        ).fetchone()
        if not row:
            payload = {
                "version": VERSION, "score": score, "components": components,
                "captured_at": datetime.now(timezone.utc).isoformat(),
            }
            c.execute("""
              INSERT INTO learning_baselines(baseline_key,created_at,payload)
              VALUES(%s,NOW(),%s::jsonb) ON CONFLICT DO NOTHING
            """, (key, json.dumps(payload, ensure_ascii=False)))
            row = c.execute(
                "SELECT created_at,payload FROM learning_baselines WHERE baseline_key=%s", (key,)
            ).fetchone()
        p = _j((row or {}).get("payload"))
        return {
            "score": p.get("score"), "components": p.get("components") or {},
            "captured_at": p.get("captured_at") or str((row or {}).get("created_at") or ""),
        }
    except Exception:
        return {"score": None, "components": {}, "captured_at": None}


def build_scorecard(pg_connect, learning_progress, production_epoch, cache_seconds=55):
    now = time.time()
    if (_CACHE.get("value") is not None and _CACHE.get("epoch") == production_epoch
            and now - float(_CACHE.get("at") or 0.0) < cache_seconds):
        return dict(_CACHE["value"])

    with pg_connect() as c:
        try:
            c.execute("SET LOCAL statement_timeout TO '7s'")
        except Exception:
            pass
        episodes = _query_decision_episodes(c)
        veritas = _decision_metrics(episodes)
        generic = _decision_metrics(episodes, "reference_decision")
        portfolio = _query_fresh_portfolio(c, production_epoch)
        learning = _query_learning(c)
        knowledge = _query_knowledge(c, episodes)

        # 1) Market decision intelligence: 20.
        hit = veritas.get("hit_rate")
        cap = veritas.get("capture_rate")
        wrong = veritas.get("wrong_side_rate")
        util = veritas.get("avg_normalized_utility")
        decision_score = (
            9.0 * _scale(hit, 0.40, 0.65)
            + 5.0 * _scale(cap, 0.25, 0.65)
            + 3.0 * _scale(None if wrong is None else 1.0 - wrong, 0.70, 0.95)
            + 3.0 * _scale(util, -0.10, 0.25)
        )

        # 2) Fresh production-candidate outcomes: 25. Lack of evidence earns no credit.
        n = portfolio["n"]
        raw_outcome = (
            10.0 * _scale(portfolio.get("win_rate"), 0.35, 0.65)
            + 6.0 * _scale(portfolio.get("profit_factor"), 0.80, 1.25)
            + 4.0 * _scale(portfolio.get("avg_return_on_entry_nav"), -0.0005, 0.0010)
            + 2.0 * (1.0 if portfolio.get("net_pnl_rub", 0.0) > 0 else 0.0)
            + 3.0 * _scale(0.15 - portfolio.get("max_drawdown", 0.15), 0.0, 0.12)
        )
        outcome_score = raw_outcome * min(1.0, n / 50.0)

        # 3) Movement/risk management: 15, based on clean completed learning episodes.
        movement_score = (
            6.0 * _scale(learning.get("avg_capture_ratio"), 0.15, 0.60)
            + 3.0 * _scale(learning.get("avg_movement_realization_ratio"), 0.15, 0.60)
            + 2.0 * (1.0 - _clip(learning.get("stop_error_rate")))
            + 2.0 * (1.0 - _clip(learning.get("exit_capture_error_rate")))
            + 2.0 * (1.0 - _clip(learning.get("cost_drag_rate")))
        )

        # 4) Knowledge: breadth is deliberately only 2/15; validation/application dominate.
        applied_quality = 0.5 * _scale(knowledge.get("applied_hit_rate"), 0.45, 0.65)                           + 0.5 * _scale(knowledge.get("applied_utility"), -0.10, 0.25)
        knowledge_score = (
            1.0 * min(1.0, knowledge["sources"] / 150.0)
            + 1.0 * min(1.0, knowledge["rules"] / 150.0)
            + 5.0 * min(1.0, knowledge["validated_oos_rules"] / 20.0)
            + 4.0 * min(1.0, knowledge["application_rate"] / 0.60)
            + 4.0 * applied_quality
        )

        # 5) Independent experience and diversity: 10.
        assets = len({e["asset"] for e in episodes if e.get("asset")})
        horizons = len({e["horizon"] for e in episodes if e.get("horizon")})
        regimes = len({e["regime"] for e in episodes if e.get("regime")})
        diversity = (min(1.0, assets / 7.0) + min(1.0, horizons / 6.0)
                     + min(1.0, regimes / 8.0)) / 3.0
        experience_score = (
            4.0 * min(1.0, len(episodes) / 300.0)
            + 3.0 * diversity
            + 3.0 * min(1.0, learning["n"] / 100.0)
        )

        # 6) Self-learning effectiveness: 15.
        li = (learning_progress or {}).get("index_vs_start")
        bad_improvement = None
        if learning.get("early_bad_rate") is not None and learning.get("recent_bad_rate") is not None:
            bad_improvement = learning["early_bad_rate"] - learning["recent_bad_rate"]
        real_improvement = None
        if learning.get("early_realization") is not None and learning.get("recent_realization") is not None:
            real_improvement = learning["recent_realization"] - learning["early_realization"]
        self_learning_score = (
            5.0 * _scale(li, 80.0, 120.0)
            + 5.0 * _scale(bad_improvement, -0.15, 0.20)
            + 3.0 * _scale(real_improvement, -0.15, 0.20)
            + 2.0 * min(1.0, learning["n"] / 50.0)
        )

        components = {
            "decision_intelligence": round(decision_score, 2),
            "portfolio_outcome_quality": round(outcome_score, 2),
            "movement_risk_management": round(movement_score, 2),
            "knowledge_application": round(knowledge_score, 2),
            "experience_depth": round(experience_score, 2),
            "self_learning_effectiveness": round(self_learning_score, 2),
        }
        maximums = {
            "decision_intelligence": 20, "portfolio_outcome_quality": 25,
            "movement_risk_management": 15, "knowledge_application": 15,
            "experience_depth": 10, "self_learning_effectiveness": 15,
        }
        score = round(sum(components.values()), 1)
        baseline = _baseline(c, score, components)

    generic_status = "MEASURABLE" if generic.get("n", 0) >= 30 else "BUILDING"
    hit_delta = None
    if veritas.get("hit_rate") is not None and generic.get("hit_rate") is not None:
        hit_delta = 100.0 * (veritas["hit_rate"] - generic["hit_rate"])
    cap_delta = None
    if veritas.get("capture_rate") is not None and generic.get("capture_rate") is not None:
        cap_delta = 100.0 * (veritas["capture_rate"] - generic["capture_rate"])
    utility_delta = None
    if veritas.get("avg_normalized_utility") is not None and generic.get("avg_normalized_utility") is not None:
        utility_delta = veritas["avg_normalized_utility"] - generic["avg_normalized_utility"]

    rollout_delta = None
    if baseline.get("score") is not None:
        rollout_delta = score - float(baseline["score"])
    start_index = (learning_progress or {}).get("index_vs_start")
    confidence = (
        "HIGH" if len(episodes) >= 200 and portfolio["n"] >= 50 and generic.get("n", 0) >= 100
        else "MEDIUM" if len(episodes) >= 80 and portfolio["n"] >= 20
        else "LOW"
    )
    value = {
        "status": "OK", "version": VERSION, "score": score, "max_score": 100,
        "stage": _stage(score), "confidence": confidence,
        "interpretation": "evidence_weighted_asset_management_capability_not_IQ",
        "components": components, "component_maximums": maximums,
        "benchmarks": {
            "initial_veritas_decision_learning": {
                "baseline": 100.0, "current": start_index,
                "delta_points": None if start_index is None else round(float(start_index) - 100.0, 2),
                "definition": "100 = initial matched independent decision-quality window",
            },
            "rollout_absolute_score": {
                "baseline_score": baseline.get("score"),
                "current_score": score,
                "delta_points": None if rollout_delta is None else round(rollout_delta, 2),
                "captured_at": baseline.get("captured_at"),
            },
            "stateless_ai": {
                "status": generic_status, "sample_n": generic.get("n", 0),
                "definition": "equal-weight confidence vote of the same saved agents; no memory, adaptive weights, knowledge adjustment or closed-loop trade learning",
                "veritas": veritas, "reference": generic,
                "hit_rate_delta_pp": None if hit_delta is None else round(hit_delta, 2),
                "large_move_capture_delta_pp": None if cap_delta is None else round(cap_delta, 2),
                "normalized_utility_delta": None if utility_delta is None else round(utility_delta, 4),
            },
        },
        "evidence": {
            "independent_decision_episodes": len(episodes),
            "fresh_candidate_trades": portfolio,
            "learning_episodes": learning,
            "knowledge": knowledge,
        },
        "principles": [
            "Knowledge count alone cannot materially raise the score.",
            "Fresh Champion/Challenger post-cost results carry the largest single weight.",
            "A losing trade after positive MFE receives zero movement-capture credit through the learning episode model.",
            "Experience is independent episodes and regime/asset/horizon diversity, not duplicate portfolio executions.",
            "The stateless AI benchmark is a measured reference on the same completed episodes, not a claimed generic-model IQ.",
        ],
    }
    _CACHE.update({"at": now, "epoch": production_epoch, "value": value})
    return dict(value)
