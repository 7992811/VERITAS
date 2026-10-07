"""Pure evidence-availability helpers for the fixed 100-point AMI scale.

Missing measurements are represented by None. Available weights describe
coverage only; they never renormalize the score or manufacture a baseline gain.
"""
from __future__ import annotations

import math


def finite_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _scale(value, low, high):
    value = finite_number(value)
    return 0.0 if value is None else max(0.0, min(1.0, (value - low) / (high - low)))


def self_learning_component(learning, progress):
    progress = progress or {}
    source_status = str(progress.get("status") or "").upper()
    index = finite_number(progress.get("index_vs_start"))
    if source_status in {"BUILDING", "ERROR", "UNAVAILABLE", "NOT_STARTED", "POSTGRES_REQUIRED"}:
        index = None
    bad = realization = None
    if learning.get("n", 0) >= 2:
        early, recent = finite_number(learning.get("early_bad_rate")), finite_number(learning.get("recent_bad_rate"))
        if early is not None and recent is not None:
            bad = early - recent
        early, recent = finite_number(learning.get("early_realization")), finite_number(learning.get("recent_realization"))
        if early is not None and recent is not None:
            realization = recent - early
    available = 5 * (index is not None) + 5 * (bad is not None) + 3 * (realization is not None)
    if not available:
        unavailable = source_status in {"ERROR", "UNAVAILABLE", "POSTGRES_REQUIRED"}
        return None, {
            "status": "UNAVAILABLE" if unavailable else "BUILDING", "observed_max_points": 0,
            "reason": "LEARNING_REFRESH_UNAVAILABLE" if unavailable else "NO_COMPARABLE_COMPLETED_EVIDENCE",
            "learning_index_status": source_status or "NOT_STARTED",
            "eligible_episodes": int(learning.get("n") or 0),
        }
    score = (5.0 * _scale(index, 80.0, 120.0) + 5.0 * _scale(bad, -.15, .20)
             + 3.0 * _scale(realization, -.15, .20))
    if learning.get("n", 0) > 0:
        score += 2.0 * min(1.0, learning["n"] / 50.0)
        available += 2
    return round(score, 2), {
        "status": "MEASURED" if available == 15 else "PARTIAL", "observed_max_points": available,
        "reason": None if available == 15 else "SOME_COMPARISON_CHANNELS_UNAVAILABLE",
        "learning_index_status": source_status or "MEASURED",
        "eligible_episodes": int(learning.get("n") or 0),
    }


def component_measurement(components, maximums, decision, learning, knowledge, self_status):
    """Return measurement statuses and observed weight without changing scores."""
    statuses = {key: {
        "status": "MEASURED" if value is not None else "BUILDING",
        "observed_max_points": maximums[key] if value is not None else 0,
        "reason": None if value is not None else "NO_COMPLETED_EVIDENCE",
    } for key, value in components.items()}
    observed = {
        "decision_intelligence": 9 * (decision.get("hit_rate") is not None)
            + 5 * (decision.get("capture_rate") is not None)
            + 3 * (decision.get("wrong_side_rate") is not None)
            + 3 * (decision.get("avg_normalized_utility") is not None),
        "movement_risk_management": 6 * (learning.get("avg_capture_ratio") is not None)
            + 3 * (learning.get("avg_movement_realization_ratio") is not None)
            + 2 * sum(learning.get(key) is not None for key in
                      ("stop_error_rate", "exit_capture_error_rate", "cost_drag_rate")),
        "knowledge_application": 7 + 4 * bool(decision.get("n"))
            + 2 * (knowledge.get("applied_hit_rate") is not None)
            + 2 * (knowledge.get("applied_utility") is not None),
        "experience_depth": 7 * bool(decision.get("n")) + 3 * (learning.get("n", 0) > 0),
    }
    for key, points in observed.items():
        statuses[key]["observed_max_points"] = points
        if components[key] is not None and points < maximums[key]:
            statuses[key].update(status="PARTIAL", reason="SOME_MEASUREMENTS_UNAVAILABLE")
    statuses["self_learning_effectiveness"] = self_status
    coverage = {
        "status": "COMPLETE" if all(v["observed_max_points"] == maximums[k]
                                     for k, v in statuses.items()) else "PARTIAL",
        "available_components": sum(value is not None for value in components.values()),
        "total_components": len(components),
        "observed_max_points": sum(v["observed_max_points"] for v in statuses.values()),
        "total_max_points": sum(maximums.values()), "score_renormalized": False,
        "missing_components": [key for key, value in components.items() if value is None],
        "partial_components": [key for key, value in statuses.items() if value["status"] == "PARTIAL"],
    }
    return statuses, coverage


def baseline_is_comparable(baseline, version, components, component_status):
    return (baseline.get("version") == version and
            baseline.get("observed_maximums") == {
                key: value["observed_max_points"] for key, value in component_status.items()} and
            all((baseline.get("components", {}).get(key) is None) == (value is None)
                for key, value in components.items()))


def score_stage(score):
    if score >= 85:
        return "ВЫСОКО ПОДТВЕРЖДЁННЫЙ"
    if score >= 70:
        return "ПРОДВИНУТЫЙ"
    if score >= 55:
        return "РАБОЧИЙ"
    if score >= 40:
        return "РАЗВИВАЮЩИЙСЯ"
    return "НАЧАЛЬНЫЙ"
