"""Legacy paper-admission compatibility shim extracted from veritas_portfolio.

Final canonical admission still belongs to veritas_portfolio_runtime. This module
only preserves the pre-binding R41/R61 compatibility behavior without growing
the frozen portfolio monolith.
"""


def legacy_paper_admission(
    row, policy, drawdown, *,
    execution, base_admission, aggressive_quality, stop_risk_cap, clip, round_step,
):
    row = row or {}
    paper_ok = row.get("paper_eligible")
    if str(row.get("asset") or "") in execution.PAPER_ASSETS:
        source_gate = execution.paper_source_gate(str(row.get("asset") or ""), row)
        paper_ok = source_gate["eligible"] and paper_ok is not False
        row["paper_eligible"] = paper_ok
        row["paper_execution_reason"] = (
            source_gate["reason"] if paper_ok
            else source_gate["reason"] if not source_gate["eligible"]
            else "paper_explicit_denial"
        )
    if not bool(paper_ok if paper_ok is not None else row.get("execution_eligible")):
        return {
            "open": False, "fraction": 0.0, "reason": "R42_PAPER_SOURCE_GATE",
            "execution_reason": row.get("execution_reason"),
            "paper_execution_reason": row.get("paper_execution_reason"),
            "production_eligible": bool(row.get("production_eligible")),
            "research_signal_preserved": True,
        }

    plan = row.get("trade_plan") or {}
    econ = execution.entry_gate(
        row, row.get("price"), row.get("research_decision"),
        plan.get("initial_position_fraction", 0.1),
    )
    if econ.get("status") == "BLOCK":
        return {
            "open": False, "fraction": 0.0, "reason": "R41_FINAL_ECONOMICS_GATE",
            "economics_blockers": econ.get("blockers") or [],
            "research_signal_preserved": True,
        }

    out = base_admission(row, policy, drawdown)
    if not isinstance(out, dict):
        return out

    if str((policy or {}).get("mode") or "") == "AGGRESSIVE" and out.get("open"):
        q = aggressive_quality(row)
        conf = float(row.get("confidence") or row.get("_pwin") or 0.0)
        floor = 0.0
        horizon = str(row.get("horizon") or "")
        supporting = set(row.get("_supporting_horizons") or [])
        if horizon == "5m" and (q.get("fresh") or q.get("confirmed") or q.get("super")):
            floor = 0.50
            if "1h" in supporting:
                floor = 0.75
            if "1h" in supporting and "4h" in supporting and q.get("super"):
                floor = 1.00
        elif q.get("super"):
            floor = (
                1.00
                if conf >= 0.82 and q.get("independent", 0) >= 5
                and q.get("rr", 0) >= 1.35 and q.get("alignment", 0) >= 3
                else 0.75
            )
        elif q.get("fresh"):
            floor = (
                1.00
                if conf >= 0.82 and q.get("independent", 0) >= 5
                and q.get("rr", 0) >= 1.50 and q.get("alignment", 0) >= 3
                else 0.75
                if conf >= 0.72 and q.get("independent", 0) >= 4 and q.get("rr", 0) >= 1.30
                else 0.50
            )
        elif q.get("confirmed"):
            floor = (
                0.75 if q.get("independent", 0) >= 5 and q.get("rr", 0) >= 1.50
                else 0.50 if q.get("independent", 0) >= 4 and q.get("rr", 0) >= 1.30
                else 0.0
            )

        if floor > 0:
            risk_cap = stop_risk_cap(row)
            if risk_cap is not None:
                floor = min(floor, float(risk_cap))
            max_fraction = float((policy or {}).get("max_fraction") or 5.0)
            floor = clip(round_step(floor), 0.05, max_fraction)
            before = float(out.get("fraction") or 0.0)
            if floor > before:
                out["fraction"] = floor
                out["open"] = True
                out["r61_fraction_before_floor"] = before
                out["r61_sizing_floor_applied"] = True
                out["r61_final_aggressive_floor"] = floor
                out["r61_quality"] = q

    out["paper_source_quality"] = (
        "PRODUCTION_GRADE" if row.get("production_eligible") else "RESEARCH_GRADE"
    )
    out["paper_is_live_fill_evidence"] = False
    return out
