"""Fast-trigger parent-risk validation outside the frozen canonical runtime."""
from __future__ import annotations

VERSION="BORROWED_PARENT_RISK_CONTEXT_V1"
HARD_FAILURES={
    "STRUCTURAL_BREAKOUT_LEVEL_NOT_HELD","STRUCTURAL_CONTEXT_INVALID",
    "STRUCTURAL_STOP_ALREADY_REACHED","STRUCTURAL_DIRECTION_OR_TIMEFRAME_MISMATCH",
    "STRUCTURAL_EVENT_PROOF_INVALID","STRUCTURAL_PARENT_POSITION_BINDING_INVALID",
    "HARD_INVALIDATION","FAST_TF_CONFLICT",
}

def validate(row, summary):
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
        bad=next((x for x in reasons if x in HARD_FAILURES),None)
        if bad:
            return {**out,"eligible":False,"reason":"BORROWED_PARENT_RISK_CONTEXT_INVALID",
                    "parent_failure":bad,"parent_decision":str((pr or {}).get("research_decision")
                    or (pr or {}).get("decision") or "NO_TRADE")}
    return out
