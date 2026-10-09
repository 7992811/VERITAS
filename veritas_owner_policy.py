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
    "required_profit_excursions":3,
    "qualifying_excursion_positive_windows":3,
    "window_seconds_floor":300,
    "minimum_dwell_seconds":600,
    "first_two_profit_excursions_observe_only":True,
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

def snapshot():
    return {"version":VERSION,"teaching_id":TEACHING_ID,"source_timestamp":SOURCE_TIMESTAMP,
            "profit_maturity":dict(PROFIT_MATURITY),"portfolio_parity":dict(PORTFOLIO_PARITY),
            "self_learning":dict(SELF_LEARNING)}
