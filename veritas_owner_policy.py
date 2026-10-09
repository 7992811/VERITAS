"""Owner-verified operational refinements layered on immutable CTC v2.

CTC remains the frozen constitution. This module contains owner-approved
operational refinements and their provenance. It may narrow/sequence lifecycle
behavior but may not relax CTC hard source, economics, risk or data gates.
"""
from __future__ import annotations

TEACHING_ID="USER_TRADE_REVIEW_2026_10_09"
SOURCE_TIMESTAMP="2026-10-09T06:03:00Z"
VERSION="OWNER_REVIEW_POLICY_V1"

PROFIT_MATURITY={
    "required_positive_windows":3,
    "window_seconds_floor":300,
    "minimum_dwell_seconds":600,
    "first_two_positive_windows_observe_only":True,
    "floor":"TRUE_ECONOMIC_BREAK_EVEN_AFTER_COSTS",
    "structural_trailing_before_maturity":False,
    "structural_trailing_after_maturity":True,
}
PORTFOLIO_PARITY={
    "shared_entry_event_portfolios":("Impulse","Champion","Challenger"),
    "verified_structural_event_entry_permission_is_shared":True,
    "portfolio_role_changes_size_not_event_existence":True,
    "canonical_setup_hard_invalidation_shared_across_portfolios":True,
}
SELF_LEARNING={
    "closed_trade_postmortem_required":True,
    "dimensions":("levels","volatility","indicators","moving_averages","multi_timeframe",
                  "entry_timing","stop","targets","profit_protection","exit","costs","data_integrity"),
    "canonical_conflict_scan_required":True,
    "parameter_search_default":"SHADOW_ONLY",
    "owner_verification_required_for_rule_promotion":True,
    "owner_comments_are_durable_training_evidence":True,
}

_PARENT_RISK_HARD_FAILURES={
    "STRUCTURAL_BREAKOUT_LEVEL_NOT_HELD","STRUCTURAL_CONTEXT_INVALID",
    "STRUCTURAL_STOP_ALREADY_REACHED","STRUCTURAL_DIRECTION_OR_TIMEFRAME_MISMATCH",
    "STRUCTURAL_EVENT_PROOF_INVALID","STRUCTURAL_PARENT_POSITION_BINDING_INVALID",
    "HARD_INVALIDATION","FAST_TF_CONFLICT",
}

def borrowed_parent_risk_context(row, summary):
    r=row or {}
    ctx=r.get("timeframe_entry_context") or (r.get("trade_plan") or {}).get("timeframe_entry_context") or {}
    event=ctx.get("event") or {}
    trigger=str(event.get("trigger_timeframe") or r.get("horizon") or "")
    parent=str(event.get("structural_timeframe") or event.get("stop_timeframe") or "")
    out={"eligible":True,"borrowed":False,"trigger_timeframe":trigger,
         "structural_timeframe":parent,"reason":"INDEPENDENT_OR_NATIVE_RISK_CONTEXT"}
    if not parent or not trigger or parent==trigger:
        return out
    out.update(borrowed=True,reason="BORROWED_PARENT_RISK_CONTEXT_OK")
    parents=[x for x in (summary or []) if str((x or {}).get("asset") or "")==str(r.get("asset") or "")
             and str((x or {}).get("horizon") or "")==parent]
    for pr in parents:
        pp=(pr or {}).get("trade_plan") or {}
        reasons=[str(x) for x in ((pr or {}).get("trade_entry_reason"),pp.get("reason")) if x]
        reasons.extend(str(x) for x in ((pr or {}).get("final_gate_blockers") or []))
        bad=next((x for x in reasons if x in _PARENT_RISK_HARD_FAILURES),None)
        if bad:
            return {**out,"eligible":False,"reason":"BORROWED_PARENT_RISK_CONTEXT_INVALID",
                    "parent_failure":bad,"parent_decision":str((pr or {}).get("research_decision")
                    or (pr or {}).get("decision") or "NO_TRADE")}
    return out

def snapshot():
    return {"version":VERSION,"teaching_id":TEACHING_ID,"source_timestamp":SOURCE_TIMESTAMP,
            "profit_maturity":dict(PROFIT_MATURITY),"portfolio_parity":dict(PORTFOLIO_PARITY),
            "self_learning":dict(SELF_LEARNING)}
