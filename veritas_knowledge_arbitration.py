"""Evidence-weighted knowledge arbitration for VERITAS.

This module turns the durable knowledge seed corpus into a light-weight runtime
advisory layer. It NEVER overrides hard data/session/execution/economics gates
and it NEVER changes a directional decision on its own. It may only:
- describe which evidence-backed rules match the current state;
- separate independent factor families;
- quantify agreement/conflict;
- modestly scale an already-admitted staged position;
- surface anti-rules and governance warnings for audit/learning.

New or adapted rules remain shadow until outcome validation promotes them.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional
import json
import math

VERSION = "knowledge-arbitration-v1.0"
GRADE_WEIGHT = {"A": 1.00, "B": 0.80, "C": 0.55, "D": 0.30, "R": 0.0}
STATUS_WEIGHT = {
    "active": 1.00,
    "validated": 1.00,
    "policy": 0.70,
    "shadow": 0.40,
    "research": 0.25,
    "governance": 0.0,
}
ASSET_ALIASES = {
    "NQF": "NQ", "NQ": "NQ", "NDX": "NQ", "NASDAQ": "NQ",
    "IMOEX": "MOEX", "MOEXF": "MOEX", "MOEX": "MOEX",
    "CNYRUB": "CNYRUBF", "CNYRUBF": "CNYRUBF",
    "BR": "BRENT", "BRENT": "BRENT", "XAU": "GOLD", "XAUUSD": "GOLD",
}
_CATALOG_CACHE: Optional[Dict[str, Any]] = None


def _num(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except Exception:
        return default


def _clip(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _direction(value: Any) -> int:
    value = str(value or "").upper()
    if value in ("LONG", "BUY", "BULLISH", "UP"):
        return 1
    if value in ("SHORT", "SELL", "BEARISH", "DOWN"):
        return -1
    return 0


def _asset(value: Any) -> str:
    x = str(value or "").upper().replace("=", "").replace("_", "")
    return ASSET_ALIASES.get(x, x)


def _get(mapping: Mapping[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = mapping
    for part in str(path).split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return default
        cur = cur.get(part)
    return cur


def _first(mapping: Mapping[str, Any], paths: Iterable[str], default: Any = None) -> Any:
    for path in paths:
        value = _get(mapping, path, None)
        if value is not None:
            return value
    return default


def feature_context(payload: Mapping[str, Any]) -> Dict[str, Any]:
    tp = payload.get("trade_plan") if isinstance(payload.get("trade_plan"), Mapping) else {}
    hs = payload.get("horizon_structure") if isinstance(payload.get("horizon_structure"), Mapping) else {}
    ti = payload.get("trend_impulse") if isinstance(payload.get("trend_impulse"), Mapping) else {}
    intr = payload.get("intraday_structure") if isinstance(payload.get("intraday_structure"), Mapping) else {}
    dec = str(payload.get("research_decision") or payload.get("decision") or "NO_TRADE").upper()
    return {
        **dict(payload),
        "asset_norm": _asset(payload.get("asset")),
        "decision_norm": dec,
        "decision_sign": _direction(dec),
        "horizon_norm": str(payload.get("horizon") or ""),
        "regime_norm": str(payload.get("regime") or "").upper(),
        "confidence_norm": _num(payload.get("confidence"), 0.0),
        "calibrated_probability_norm": _num(payload.get("calibrated_probability"), None),
        "effective_evidence_norm": int(_num(payload.get("effective_evidence"), 0) or 0),
        "trend_direction_norm": str(_first(
            payload,
            ("horizon_structure.direction", "trend_impulse.direction", "trend_direction"),
            "",
        ) or "").upper(),
        "trend_score_norm": _num(_first(
            payload,
            ("horizon_structure.score", "trend_impulse.score", "trend_score"),
            0.0,
        ), 0.0),
        "entry_quality_norm": str(_first(
            payload,
            ("trade_plan.entry_quality", "trend_impulse.entry_quality", "entry_quality"),
            "",
        ) or "").upper(),
        "rv": _num(_first(payload, ("realized_vol", "rv", "trend_impulse.sigma_1h"), None), None),
        "volume_ratio": _num(_first(payload, ("relative_volume", "volume_ratio", "intraday_structure.relative_volume"), None), None),
        "taker_buy_share": _num(_first(payload, ("taker_buy_share", "order_flow.taker_buy_share"), None), None),
        "source_divergence": _num(payload.get("source_divergence"), None),
        "spread_bps": _num(_first(payload, ("spread_bps", "execution.spread_bps"), None), None),
        "expected_move_pct": _num(_first(payload, ("trade_plan.expected_move_pct", "expected_move_pct"), None), None),
        "expected_to_stop_ratio": _num(_first(payload, ("trade_plan.expected_to_stop_ratio", "expected_to_stop_ratio"), None), None),
        "event_score": _num(_first(payload, ("event_shadow_score", "event_score"), 0.0), 0.0),
        "structure_score": _num(_first(payload, ("horizon_structure.score", "intraday_structure.score"), 0.0), 0.0),
        "support": _first(payload, ("intraday_structure.support", "support_level", "structural_levels.support"), None),
        "resistance": _first(payload, ("intraday_structure.resistance", "resistance_level", "structural_levels.resistance"), None),
        "market_open_norm": bool(payload.get("market_open", True)),
        "source_gate_norm": bool(payload.get("source_gate", payload.get("source_gate_pass", True))),
        "price": _num(payload.get("price"), None),
        "stop_price": _num(_first(payload, ("trade_plan.stop_price", "stop_price"), None), None),
        "target_price": _num(_first(payload, ("trade_plan.target_price", "target_price"), None), None),
        "native_horizon_direction": str(hs.get("direction") or "").upper(),
        "native_horizon_score": _num(hs.get("score"), 0.0),
        "trend_entry_quality": str(ti.get("entry_quality") or "").upper(),
        "session_efficiency": _num(intr.get("session_efficiency"), None),
    }


def _compare(actual: Any, op: str, expected: Any, ctx: Mapping[str, Any]) -> bool:
    op = str(op or "==").lower()
    if op in ("decision_aligned", "aligned_with_decision"):
        return _direction(actual) != 0 and _direction(actual) == int(ctx.get("decision_sign") or 0)
    if op in ("decision_opposed", "opposed_to_decision"):
        return _direction(actual) != 0 and _direction(actual) == -int(ctx.get("decision_sign") or 0)
    if op in ("in", "not_in"):
        seq = expected if isinstance(expected, (list, tuple, set)) else [expected]
        ok = actual in seq
        return not ok if op == "not_in" else ok
    if op in ("contains", "not_contains"):
        ok = str(expected) in str(actual)
        return not ok if op == "not_contains" else ok
    if op in ("==", "eq"):
        return actual == expected
    if op in ("!=", "ne"):
        return actual != expected
    a, b = _num(actual, None), _num(expected, None)
    if a is None or b is None:
        return False
    if op in (">", "gt"):
        return a > b
    if op in (">=", "gte"):
        return a >= b
    if op in ("<", "lt"):
        return a < b
    if op in ("<=", "lte"):
        return a <= b
    return False


def _conditions_match(rule: Mapping[str, Any], ctx: Mapping[str, Any]) -> bool:
    for cond in rule.get("conditions") or []:
        if not isinstance(cond, Mapping):
            return False
        field = str(cond.get("field") or "")
        actual = _get(ctx, field, None)
        if actual is None and field in ctx:
            actual = ctx.get(field)
        if not _compare(actual, cond.get("op") or "==", cond.get("value"), ctx):
            return False
    return True


def _scope_match(rule: Mapping[str, Any], ctx: Mapping[str, Any]) -> bool:
    assets = [_asset(x) for x in (rule.get("asset_scope") or [])]
    if assets and ctx.get("asset_norm") not in assets:
        return False
    horizons = [str(x) for x in (rule.get("horizons") or [])]
    if horizons and str(ctx.get("horizon_norm") or "") not in horizons:
        return False
    regimes = [str(x).upper() for x in (rule.get("regimes") or [])]
    if regimes:
        regime = str(ctx.get("regime_norm") or "")
        if not any(x in regime for x in regimes):
            return False
    return True


def load_catalog(force: bool = False) -> Dict[str, Any]:
    global _CATALOG_CACHE
    if _CATALOG_CACHE is not None and not force:
        return _CATALOG_CACHE
    root = Path(__file__).resolve().parent
    sources: Dict[str, Dict[str, Any]] = {}
    rules: List[Dict[str, Any]] = []
    anti_rules: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    files = sorted(root.glob("veritas_knowledge_seed*.json"))
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for src in data.get("sources") or []:
            if isinstance(src, Mapping) and src.get("source_id"):
                sources[str(src["source_id"])] = dict(src)
        for rule in data.get("rules") or []:
            if isinstance(rule, Mapping) and rule.get("rule_id"):
                row = dict(rule)
                row["_pack_file"] = path.name
                rules.append(row)
        for rule in data.get("anti_rules") or []:
            if isinstance(rule, Mapping) and rule.get("rule_id"):
                row = dict(rule)
                row["_pack_file"] = path.name
                anti_rules.append(row)
        for item in data.get("conflicts") or []:
            if isinstance(item, Mapping):
                conflicts.append(dict(item))
    _CATALOG_CACHE = {
        "version": VERSION,
        "files": [p.name for p in files],
        "sources": sources,
        "rules": rules,
        "anti_rules": anti_rules,
        "conflicts": conflicts,
    }
    return _CATALOG_CACHE


def _rule_weight(rule: Mapping[str, Any], source: Mapping[str, Any]) -> float:
    grade = str(rule.get("evidence_grade") or source.get("evidence_grade") or "D").upper()
    status = str(rule.get("status") or "shadow").lower()
    prior = _num(rule.get("prior_weight"), 0.25)
    prior = _clip(prior if prior is not None else 0.25)
    return GRADE_WEIGHT.get(grade, 0.30) * STATUS_WEIGHT.get(status, 0.25) * prior


def _anti_rule_applicable(rule: Mapping[str, Any], ctx: Mapping[str, Any]) -> bool:
    return _scope_match(rule, ctx) and _conditions_match(rule, ctx)


def arbitrate(payload: Mapping[str, Any]) -> Dict[str, Any]:
    ctx = feature_context(payload)
    cat = load_catalog()
    matched: List[Dict[str, Any]] = []
    family_scores: Dict[str, Dict[str, float]] = {}
    long_score = short_score = risk_score = 0.0

    for rule in cat["rules"]:
        if not _scope_match(rule, ctx) or not _conditions_match(rule, ctx):
            continue
        source = cat["sources"].get(str(rule.get("source_id") or ""), {})
        action = str(rule.get("action") or "").upper()
        status = str(rule.get("status") or "shadow").lower()
        weight = _rule_weight(rule, source)
        family = str(rule.get("factor_family") or rule.get("school") or rule.get("agent") or "OTHER").upper()
        row = {
            "rule_id": rule.get("rule_id"),
            "source_id": rule.get("source_id"),
            "action": action,
            "status": status,
            "factor_family": family,
            "evidence_grade": str(rule.get("evidence_grade") or source.get("evidence_grade") or "D").upper(),
            "weight": round(weight, 6),
            "hypothesis": rule.get("hypothesis"),
        }
        matched.append(row)
        bucket = family_scores.setdefault(family, {"long": 0.0, "short": 0.0, "risk": 0.0})
        if action == "LONG":
            long_score += weight
            bucket["long"] += weight
        elif action == "SHORT":
            short_score += weight
            bucket["short"] += weight
        elif action in ("NO_TRADE", "DELEVER", "REDUCE_RISK"):
            risk_score += weight
            bucket["risk"] += weight

    directional_total = long_score + short_score
    signed = (long_score - short_score) / directional_total if directional_total > 1e-12 else 0.0
    conflict = (2.0 * min(long_score, short_score) / directional_total) if directional_total > 1e-12 else 0.0
    decision_sign = int(ctx.get("decision_sign") or 0)
    aligned = max(0.0, signed * decision_sign)
    opposed = max(0.0, -signed * decision_sign)

    aligned_families = set()
    opposed_families = set()
    for fam, scores in family_scores.items():
        d = scores["long"] - scores["short"]
        if d * decision_sign > 1e-9:
            aligned_families.add(fam)
        elif d * decision_sign < -1e-9:
            opposed_families.add(fam)

    # Shadow knowledge can adjust size modestly but can never create a trade,
    # reverse direction, or bypass any hard gate.
    if decision_sign and len(aligned_families) >= 2:
        multiplier = 1.0 + 0.08 * _clip(aligned)
    else:
        multiplier = 1.0
    if decision_sign and len(opposed_families) >= 2:
        multiplier -= 0.10 * _clip(opposed)
    multiplier -= 0.08 * _clip(risk_score)
    multiplier = max(0.80, min(1.08, multiplier))

    thesis_challenge = bool(
        decision_sign
        and len(opposed_families) >= 2
        and opposed >= 0.55
        and conflict < 0.75
    )
    anti = [
        {
            "rule_id": r.get("rule_id"),
            "statement": r.get("statement"),
            "policy": r.get("policy"),
            "school": r.get("school"),
        }
        for r in cat["anti_rules"] if _anti_rule_applicable(r, ctx)
    ]
    return {
        "version": VERSION,
        "status": "OK",
        "asset": ctx.get("asset_norm"),
        "horizon": ctx.get("horizon_norm"),
        "decision": ctx.get("decision_norm"),
        "catalog_sources": len(cat["sources"]),
        "catalog_rules": len(cat["rules"]),
        "catalog_anti_rules": len(cat["anti_rules"]),
        "matched_rule_count": len(matched),
        "matched_rules": matched[:40],
        "independent_factor_families": sorted(family_scores),
        "aligned_families": sorted(aligned_families),
        "opposed_families": sorted(opposed_families),
        "long_score": round(long_score, 6),
        "short_score": round(short_score, 6),
        "risk_score": round(risk_score, 6),
        "directional_consensus": round(signed, 6),
        "conflict_score": round(conflict, 6),
        "size_multiplier": round(multiplier, 6),
        "thesis_challenge": thesis_challenge,
        "automatic_veto": False,
        "anti_rules": anti[:30],
        "principles": [
            "knowledge_never_bypasses_data_session_or_execution_safety",
            "shadow_rules_do_not_reverse_direction",
            "independent_factor_families_are_counted_separately",
            "conflicting_schools_are_arbitrated_by_regime_not_averaged_blindly",
            "new_rules_require_outcome_validation_before_promotion",
        ],
    }


def catalog_summary() -> Dict[str, Any]:
    cat = load_catalog()
    by_grade: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    by_family: Dict[str, int] = {}
    for rule in cat["rules"]:
        source = cat["sources"].get(str(rule.get("source_id") or ""), {})
        grade = str(rule.get("evidence_grade") or source.get("evidence_grade") or "D").upper()
        status = str(rule.get("status") or "shadow").lower()
        family = str(rule.get("factor_family") or rule.get("school") or rule.get("agent") or "OTHER").upper()
        by_grade[grade] = by_grade.get(grade, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
        by_family[family] = by_family.get(family, 0) + 1
    return {
        "version": VERSION,
        "source_count": len(cat["sources"]),
        "rule_count": len(cat["rules"]),
        "anti_rule_count": len(cat["anti_rules"]),
        "conflict_count": len(cat["conflicts"]),
        "files": cat["files"],
        "rules_by_grade": by_grade,
        "rules_by_status": by_status,
        "rules_by_family": by_family,
    }
