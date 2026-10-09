"""Owner-verified operational refinements layered on immutable CTC v2.

The canonical constitution and TREND_ACCELERATION_POLICY remain the trading
authority. This module records owner review governance, portfolio parity and
self-learning promotion rules without duplicating profit-protection logic.
"""
from __future__ import annotations

TEACHING_ID="USER_TRADE_REVIEW_2026_10_09"
SOURCE_TIMESTAMP="2026-10-09T06:03:00Z"
VERSION="OWNER_REVIEW_POLICY_V2"

PORTFOLIO_PARITY={
    "shared_entry_event_portfolios":("Impulse","Champion","Challenger"),
    "verified_structural_event_entry_permission_is_shared":True,
    "portfolio_role_changes_size_not_event_existence":True,
    "canonical_setup_hard_invalidation_shared_across_portfolios":True,
}
SELF_LEARNING={
    "closed_trade_postmortem_required":True,
    "dimensions":("levels","volatility","indicators","moving_averages","multi_timeframe",
                  "entry_timing","stop","targets","profit_protection","exit","costs",
                  "data_integrity","episode_lifecycle"),
    "canonical_conflict_scan_required":True,
    "parameter_search_default":"SHADOW_ONLY",
    "owner_verification_required_for_rule_promotion":True,
    "owner_comments_are_durable_training_evidence":True,
}
PROFIT_PROTECTION_AUTHORITY="CTC.TREND_ACCELERATION_POLICY.profit_protection"

def snapshot():
    return {"version":VERSION,"teaching_id":TEACHING_ID,"source_timestamp":SOURCE_TIMESTAMP,
            "portfolio_parity":dict(PORTFOLIO_PARITY),"self_learning":dict(SELF_LEARNING),
            "profit_protection_authority":PROFIT_PROTECTION_AUTHORITY}
