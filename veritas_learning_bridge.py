"""Compact immutable learning evidence and the sole bounded decision adapter.

No database or network access occurs on the decision path. A probability lesson
does not create a directional signal or authorize additional portfolio risk.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import threading

import veritas_entry_version as ENTRY
import veritas_learning_integrity as INTEGRITY
import veritas_price_source as SOURCE

VERSION = "CONTINUOUS_LEARNING_EVIDENCE_V1"
_lock = threading.RLock()
_state = {"profiles": [], "candidates": [], "generation": None,
          "trade_revocation_generation": None, "trade_process_epoch": None, "updated_at": None}


def timestamp(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, (float, int)):
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc)
        value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return value.astimezone(timezone.utc) if value.tzinfo else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def number(value):
    try:
        result = float(value) if not isinstance(value, bool) and value is not None else None
        return result if result is not None and math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False, default=str).encode()).hexdigest()


def compact_quote(raw):
    """Capture source metadata without copying bar histories into the ledger."""
    quote = SOURCE.quote_from_row(raw or {})
    return {key: deepcopy(quote[key]) for key in (*SOURCE.QUOTE_FIELDS, "asset", "observed_at") if key in quote}


def compact_rule_matches(matches):
    """Keep frozen rule applications available to the existing outcome ledger."""
    if not isinstance(matches, list):
        return []
    result = []
    for match in matches[:128]:
        if not isinstance(match, dict) or not isinstance(match.get("rule_id"), str):
            continue
        clean = {k: v for k, v in match.items() if k in (
            "rule_id", "source_id", "action", "status", "shadow_score", "weight", "agent",
            "version", "rule_key", "definition_hash", "audit_hash", "registered_at", "contract_hash")
                 and type(v) in (str, int, float, bool, type(None))
                 and not (isinstance(v, str) and len(v) > 256)
                 and not (isinstance(v, float) and not math.isfinite(v))}
        if clean.get("rule_id"):
            result.append(clean)
    return result


def capture_rule(rule):
    import veritas_knowledge_validation as KNOWLEDGE
    return KNOWLEDGE.capture(rule)


def learning_rule_matches(matches, asset, horizon, *, at=None):
    """Reserve room for active rules; rotate other trials before outcomes exist."""
    import veritas_knowledge_validation as KNOWLEDGE
    now = timestamp(at) or datetime.now(timezone.utc)
    profiles = KNOWLEDGE.snapshot(now=now).get("profiles") or []
    active = {p.get("rule_id") for p in profiles
              if (p.get("scope") or {}).get("asset") == asset
              and (p.get("scope") or {}).get("horizon") == horizon}
    candidates = [m for m in compact_rule_matches(matches) if m.get("version") == KNOWLEDGE.VERSION]
    slot = int(now.timestamp())//60
    candidates.sort(key=lambda m: (m["rule_id"] not in active,
                                   digest([m.get("rule_key"), asset, horizon, slot])))
    return candidates[:8]


def knowledge_advice(row, normalized, *, now=None):
    import veritas_knowledge_validation as KNOWLEDGE
    return KNOWLEDGE.adjust(row.get("knowledge_learning_matches", row.get("knowledge_shadow_matches")) or [],
        asset=normalized["asset"], horizon=normalized["horizon"], regime=normalized["regime"],
        policy_hash=normalized["policy_hash"], source_identity=normalized["source_identity"],
        base_direction=row.get("research_decision") or row.get("decision"),
        fraction=normalized.get("fraction"), position_step=normalized.get("position_step"),
        max_fraction=normalized.get("max_fraction"), now=timestamp(now) or datetime.now(timezone.utc))


def quote_evidence(row, asset=None):
    """Use recorded source identity and quote time; never infer a contract."""
    row = row or {}
    features = row.get("features") or {}
    plan = row.get("trade_plan") or {}
    quote = row.get("_execution_quote") or row.get("market_quote") or {}
    if not isinstance(quote, dict):
        quote = {}
    asset = asset or row.get("asset")
    facts = dict(features)
    facts.update({k: v for k, v in row.items() if k in (
        "source_names", "market_source_names", "source", "primary_source", "contract",
        "contract_id", "raw_label", "raw_ticker", "instrument_id", "provider_instrument_id",
        "provider_contract_id", "provider_contract_label", "provider_row_verified")})
    if "contract" not in facts and isinstance(facts.get("market_contract"), dict):
        facts["contract"] = facts["market_contract"]
    facts.update(quote)
    source = SOURCE.identity(asset, facts)
    price = number(quote.get("price", row.get("price", features.get("price"))))
    observed = timestamp(quote.get("observed_at") or quote.get("market_observed_at")
                         or row.get("market_observed_at") or features.get("market_observed_at")
                         or plan.get("market_observed_at"))
    if not source or not source.get("key") or price is None or price <= 0 or observed is None:
        return None
    return {"asset": asset, "price": price, "observed_at": observed.isoformat(),
            "source_identity": source, "source_key": digest(source)}


def advice_row(row):
    """Translate the actual receiver, preserving a frozen unadjusted baseline."""
    row = row or {}
    quote = quote_evidence(row)
    applied = row.get("autonomous_learning") or {}
    probability = number(applied.get("base_probability"))
    if probability is None:
        probability = number(row.get("base_probability", row.get("predicted_probability")))
    if probability is None:
        probability = number((row.get("calibration") or {}).get("probability_correct"))
    if probability is None:
        probability = number(row.get("calibrated_probability"))
    if probability is not None and not 0 <= probability <= 1:
        probability = -1.  # Invalid data is distinct from an absent probability.
    net_rr = number(applied.get("net_reward_risk"))
    if net_rr is None:
        net_rr = number(row.get("_learning_net_reward_risk"))
    if net_rr is None:
        net_rr = number(((row.get("trade_plan") or {}).get("final_economics_gate") or {}).get("net_reward_risk"))
    return {"asset": row.get("asset"), "horizon": row.get("horizon"),
            "regime": row.get("regime") or "UNKNOWN", "policy_hash": ENTRY.policy_hash(),
            "source_identity": quote["source_identity"] if quote else None,
            "predicted_probability": probability, "base_probability": probability,
            "net_reward_risk": net_rr if net_rr is not None and net_rr > 0 else None}


def capture_decision(payload, *, asset=None, horizon=None, at=None):
    """Called before ledger compaction, once the actual decision is formed."""
    p = payload or {}
    asset, horizon = asset or p.get("asset"), horizon or p.get("horizon")
    decided = timestamp(at or p.get("created_at"))
    if decided is None:
        return {"version": VERSION, "eligible": False, "reason": "MISSING_DECISION_TIME"}
    quote = quote_evidence(p, asset)
    plan = p.get("trade_plan") or {}
    event = plan.get("entry_event_snapshot") or {}
    event_id = event.get("event_id") if isinstance(event, dict) else None
    if not event_id:
        event_id = plan.get("entry_event_id")
    source_gate = ((p.get("gates") or {}).get("source") is True
                   or p.get("source_gate_pass") is True)
    eligible = bool(quote and source_gate
                    and 0 <= (decided - timestamp(quote["observed_at"])).total_seconds() <= 120)
    normalized = advice_row(dict(p, asset=asset, horizon=horizon))
    probability = normalized["base_probability"]
    evidence = {
        "version": VERSION, "eligible": eligible,
        "reason": "VERIFIED_DECISION_SNAPSHOT" if eligible else "SOURCE_OR_TIME_UNVERIFIED",
        "asset": asset, "horizon": horizon, "decision_at": decided.isoformat(),
        "direction": p.get("research_decision") or p.get("decision") or "NO_TRADE",
        "regime": p.get("regime") or "UNKNOWN", "quote": quote,
        "policy_hash": ENTRY.policy_hash(), "entry_sha": ENTRY.version_identity().get("strategy_entry_sha"),
        "idea_id": event_id, "independence_basis": "OBSERVED_STRUCTURAL_EVENT" if event_id else "NONOVERLAPPING_FORECAST_WINDOW",
        "base_probability": probability,
        "net_reward_risk": normalized["net_reward_risk"],
        "applied_learning": p.get("autonomous_learning") or {},
        "prospective_candidates": prospective(normalized, now=decided),
        "knowledge_trials": knowledge_advice(p, normalized, now=decided)["trials"] if eligible else [],
    }
    evidence["evidence_hash"] = digest(evidence)
    return evidence


def update(snapshot):
    """Publish only a compact detached profile set in this process."""
    profiles = snapshot.get("profiles") or []
    candidates = snapshot.get("candidates") or []
    if len(profiles) > 128 or len(candidates) > 256:
        raise ValueError("learning profile capacity exceeded")
    integrity = INTEGRITY.memory_state()
    value = {"profiles": deepcopy(profiles), "candidates": deepcopy(candidates),
             "generation": integrity["generation"], "updated_at": snapshot.get("updated_at") or snapshot.get("calculated_at")}
    if len(json.dumps(value, default=str)) > 262144:
        raise ValueError("learning profile snapshot too large")
    with _lock:
        value["trade_revocation_generation"] = _state.get("trade_revocation_generation")
        value["trade_process_epoch"] = _state.get("trade_process_epoch")
        if (snapshot.get("evidence_revalidation_pending") is False
                and integrity.get("process_epoch") is not None
                and integrity.get("revocation_generation") is not None
                and snapshot.get("verified_process_epoch") == integrity["process_epoch"]
                and snapshot.get("verified_revocation_generation") == integrity["revocation_generation"]
                and integrity["ready"] and INTEGRITY.memory_state() == integrity):
            value["trade_revocation_generation"] = integrity["revocation_generation"]
            value["trade_process_epoch"] = integrity["process_epoch"]
        _state.clear()
        _state.update(value)


def _trade_current(state, integrity):
    """Additive evidence may advance generation without undoing a receipt sweep."""
    return bool(integrity["ready"] and integrity.get("process_epoch") is not None
                and integrity.get("revocation_generation") is not None
                and state.get("trade_process_epoch") == integrity["process_epoch"]
                and state.get("trade_revocation_generation") == integrity["revocation_generation"])


def runtime_status():
    integrity = INTEGRITY.memory_state()
    with _lock:
        return {"integrity_ready": integrity["ready"], "generation": integrity["generation"],
                "revocation_generation": integrity.get("revocation_generation"),
                "profiles_current": _state.get("generation") == integrity["generation"],
                "trade_evidence_ready": _trade_current(_state, integrity)}


def advice(row, *, now=None):
    import veritas_autonomous_learning as AUTO
    with _lock:
        state = dict(_state)
    integrity = INTEGRITY.memory_state()
    if not integrity["ready"] or state["generation"] != integrity["generation"]:
        return {"status": "EVIDENCE_REVALIDATION_PENDING", "size_multiplier": 1.0,
                "probability_delta": 0.0, "candidate_ids": []}
    profiles = [p for p in state["profiles"] if p.get("kind") == "CALIBRATION"
                or _trade_current(state, integrity)]
    result = AUTO.apply_advice(profiles, advice_row(row), policy_hash=ENTRY.policy_hash(),
                              now=now or datetime.now(timezone.utc))
    if INTEGRITY.memory_state() != integrity:
        return {"status": "EVIDENCE_REVALIDATION_PENDING", "size_multiplier": 1.,
                "probability_delta": 0., "candidate_ids": []}
    return result


def prospective(normalized, *, now=None):
    import veritas_autonomous_learning as AUTO
    with _lock:
        candidates = _state["candidates"]
    return AUTO.freeze_candidates({"candidates": candidates}, normalized,
                                   now=now or datetime.now(timezone.utc))


def apply_admission(row, decision, policy, *, now=None):
    """Apply a proved allocation lesson after admission and before final risk sizing.

    Step rounding must itself respect the 10% bound. A 5%-point portfolio step
    cannot silently turn a requested 10% reduction into a 50% reduction.
    """
    out = dict(decision)
    if not out.get("open"):
        return out
    integrity_before = INTEGRITY.memory_state()
    row = dict(row, _learning_net_reward_risk=number((out.get("economics") or {}).get("net_reward_risk")))
    result = advice(row, now=now)
    normalized = advice_row(row)
    before = number(out.get("fraction"))
    normalized.update(fraction=before, position_step=number((policy or {}).get("position_step")) or .05,
                      max_fraction=number((policy or {}).get("max_fraction")) or before)
    knowledge = knowledge_advice(row, normalized, now=now)
    application = runtime_status()
    knowledge_proven = bool(knowledge.get("applied_trial_ids") and application["profiles_current"]
                            and application["trade_evidence_ready"])
    auto_multiplier = number(result.get("size_multiplier")) if result.get("profitability_proven") is True else 1.
    knowledge_multiplier = number(knowledge.get("size_multiplier")) if knowledge_proven else 1.
    multiplier = min(auto_multiplier or 1., knowledge_multiplier or 1.)
    profitability_proven = bool(result.get("profitability_proven") or knowledge_proven)
    after = before
    if (profitability_proven and before is not None and before > 0
            and .9 <= multiplier < 1.):
        step = number((policy or {}).get("position_step")) or .05
        target = before * multiplier
        proposed = math.floor(target / step + 1e-9) * step
        cap = number((policy or {}).get("max_fraction")) or before
        if .9 * before - 1e-10 <= proposed <= min(1.1 * before, cap) + 1e-10:
            after = proposed
    if INTEGRITY.memory_state() != integrity_before:
        after, profitability_proven, knowledge_proven = before, False, False
        result = dict(status="EVIDENCE_REVALIDATION_PENDING", candidate_ids=[], probability_delta=0.)
    applied = before is not None and after is not None and not math.isclose(before, after, abs_tol=1e-10)
    out["fraction"] = after if after is not None else out.get("fraction")
    out["autonomous_learning"] = {
        "version": VERSION, "status": result.get("status"),
        "candidate_ids": list(result.get("candidate_ids") or [])[:8],
        "fraction_before": before, "fraction_after": after, "applied": applied,
        "proposed_multiplier": multiplier, "probability_delta": result.get("probability_delta"),
        "profitability_proven": profitability_proven,
        "base_probability": normalized["base_probability"],
        "net_reward_risk": normalized["net_reward_risk"],
        "calibrated_probability": result.get("calibrated_probability"),
        "source_identity": normalized["source_identity"],
        "policy_hash": normalized["policy_hash"],
        "regime": normalized["regime"],
        "decision_at": (timestamp(now) or datetime.now(timezone.utc)).isoformat(),
        "prospective_candidates": prospective(normalized, now=now),
        "knowledge_trials": knowledge["trials"],
        "knowledge_applied_trial_ids": list(knowledge.get("applied_trial_ids") or []) if knowledge_proven else [],
        "knowledge_context_at": row.get("knowledge_context_at"),
        "position_step": number((policy or {}).get("position_step")) or .05,
        "max_fraction": normalized["max_fraction"],
        "reason": "APPLIED_WITHIN_EXISTING_POLICY" if applied else "NEUTRAL_OR_POSITION_STEP",
    }
    if (result.get("candidate_ids") and number(result.get("probability_delta")) not in (None, 0.)
            and result.get("calibrated_probability") is not None):
        out["probability"] = result["calibrated_probability"]
        out["probability_source"] = "PROSPECTIVELY_VALIDATED_CALIBRATION"
    return out


def execution_receipt(admission, units, fill_price, nav):
    """Attach the accounted fill, preserving every prospective proposal verbatim."""
    result = deepcopy(admission) if isinstance(admission, dict) else admission
    if not isinstance(result, dict) or not isinstance(result.get("autonomous_learning"), dict):
        return result
    stamp = result["autonomous_learning"]
    quantity, price, capital = number(units), number(fill_price), number(nav)
    actual = (abs(quantity)*price/capital if quantity is not None and price is not None
              and price > 0 and capital is not None and capital > 0 else None)
    proposed = number(stamp.get("fraction_after"))
    aligned = (actual is not None and proposed is not None
               and math.isclose(actual, proposed, rel_tol=1e-6, abs_tol=1e-8))
    stamp["execution_audit"] = {
        "source": "ACCOUNTED_ORIGINAL_PAPER_FILL", "actual_fraction": actual,
        "proposed_fraction_before": stamp.get("fraction_before"), "proposed_fraction_after": proposed,
        "executed_as_proposed": aligned,
        "actual_profit_effect_proven": False,
        "reason": "ACCOUNTED_SIZE_MATCHES_PROPOSAL" if aligned else "POST_ADMISSION_ALLOCATION_CHANGED",
        "comparison_basis": "PROSPECTIVE_PAPER_SIZE_SIMULATION_REQUIRES_VERIFIED_CLOSED_CASHFLOWS",
    }
    return result
