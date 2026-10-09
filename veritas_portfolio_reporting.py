"""Small report decorators extracted from the legacy portfolio monolith.

This module must stay presentation-only: no admission, sizing, accounting or
execution authority belongs here.
"""


def paper_quantity_metadata(units):
    try:
        normalized = abs(float(units or 0.0))
    except Exception:
        normalized = 0.0
    return {
        "normalized_units": normalized,
        "quantity_semantics": "NORMALIZED_PAPER_RETURN_UNITS",
        "broker_quantity": None,
        "broker_quantity_source": None,
        "broker_ready_quantity": False,
    }


def execution_safety_report(data, execution, constitution, jsonable):
    d = dict(data or {})
    d["execution_safety_r40"] = {
        "version": execution.VERSION,
        "paper_fill_model": "CONSERVATIVE_NORMALIZED_PAPER_FILL_V1",
        "idempotent_client_order_ids": True,
        "final_economics_gate": True,
        "live_risk_profile": dict(execution.LIVE_RISK_PROFILE),
        "live_broker_execution_enabled": False,
        "objective_hard_constraint": constitution.OBJECTIVE_POLICY["hard_constraint"],
        "objective_priority": list(constitution.OBJECTIVE_POLICY["priority_order"]),
        "principle": constitution.OBJECTIVE_POLICY["principle"],
    }
    return jsonable(d)


def paper_execution_quality_report(data, json_parser, jsonable):
    d = dict(data or {})
    for portfolio in d.get("portfolios") or []:
        for position in portfolio.get("positions") or []:
            payload = json_parser(position.get("payload"))
            position.update(paper_quantity_metadata(position.get("units")))
            payload.setdefault("quantity_semantics", "NORMALIZED_PAPER_RETURN_UNITS")
    d["paper_execution_quality_r41"] = {
        "enabled": True,
        "research_only_signals_can_open_positions": True,
        "requires_paper_eligible": True,
        "requires_production_eligible": False,
        "requires_final_economics_gate": True,
        "pnl_interpretation": (
            "research-grade or execution-grade normalized paper P&L; never broker-fill proof"
        ),
        "quantity_semantics": "NORMALIZED_PAPER_RETURN_UNITS",
        "normalized_units_are_broker_quantity": False,
        "broker_quantity_requires_instrument_registry": True,
        "blocked_assets_without_sufficient_feed": (
            "paper may use current research-grade feeds; live capital remains production-gated"
        ),
    }
    return jsonable(d)
