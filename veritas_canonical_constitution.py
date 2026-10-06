"""VERITAS Canonical Trading Constitution v1.

This module is the machine-readable policy registry for the VERITAS trading
system. It does not itself place orders. R85 remains the final runtime authority
until a separately reviewed integration binds runtime decisions to this policy.

Principle: one trading decision -> one canonical policy path.
Historical Rxx helpers remain audit history, not policy authority.
"""
from __future__ import annotations

VERSION = "CTC_V1_2026_10_06"
BASIS_RUNTIME = "R85_FINAL_AUTHORITY_LOCK"

STAGE_ORDER = (
    "DATA",
    "THESIS",
    "TIMING",
    "ECONOMICS",
    "RISK",
    "SIZE",
    "LIFECYCLE",
    "LEARNING",
)

OBJECTIVE_POLICY = {
    "hard_constraint": "POSITIVE_POST_COST_ECONOMICS_OR_EXPLICIT_RESEARCH_PROBE",
    "priority_order": (
        "SUSTAINABLE_HIGH_WIN_RATE",
        "SUSTAINABLE_POSITIVE_POST_COST_PROFIT",
        "LARGE_MOVE_CAPTURE",
        "DRAWDOWN_CONTROL",
    ),
    "target_win_rate": 0.65,
    "principle": (
        "A trade with negative target economics is never admitted. Among trades "
        "with admissible economics, prefer the policy with the highest sustainable "
        "win rate, then net profit/capture quality, then drawdown."
    ),
}

COST_POLICY = {
    "commission_rate_per_side": 0.0005,
    "slippage_rate_per_side": 0.0004,
    "round_trip_base_cost_pct": 0.0018,
    "cost_buffer_multiple": 1.2,
    "minimum_expected_move_floor_pct": 0.0019,
    "minimum_expected_move_formula": "max(0.19%, 1.2 * modeled_round_trip_cost)",
    "funding_annual_rate": 0.16,
    "funding_free_seconds": 86400,
    "funding_basis": "ACT/365.25_AFTER_FIRST_24H_ON_CURRENT_NOTIONAL",
}

HARD_VETOES = frozenset({
    "UNSUPPORTED_ASSET_OR_SOURCE",
    "PRIMARY_SOURCE_GATE_FAILED",
    "MARKET_TIME_GATE_FAILED",
    "EXECUTION_QUOTE_STALE",
    "SOURCE_IDENTITY_MISMATCH",
    "EXACT_CONTRACT_MISMATCH",
    "R67_DIRECT_NQ_QUOTE_REQUIRED",
    "PRIMARY_PRICE_INVALID",
    "STOP_MISSING_OR_DIRECTION_INVALID",
    "TARGET_MISSING_OR_DIRECTION_INVALID",
    "HARD_THESIS_INVALIDATION",
    "EXECUTION_TF_DIRECTION_CONFLICT",
    "UNCONFIRMED_5M_COUNTER_SENIOR",
    "ACTUAL_PRICE_LATE_ENTRY_CHASE",
    "NEGATIVE_VALIDATED_SETUP_EDGE",
    "TARGET_NOT_PROFITABLE_AFTER_COSTS",
    "EXPECTED_MOVE_BELOW_COST_BUFFER",
    "STOP_RISK_CAP_EXCEEDED",
    "PORTFOLIO_HARD_DRAWDOWN_STOP",
    "EVENT_REUSE_WITHOUT_NEW_CONFIRMATION",
})

SOFT_VETOES = frozenset({
    "RR_BELOW_FINAL_FLOOR_BUT_NET_POSITIVE",
    "NET_REWARD_RISK_BELOW_FLOOR_BUT_NET_POSITIVE",
    "OLD_PARENT_EVENT_STALE",
    "OLD_PARENT_TARGET_REACHED",
    "OLD_PARENT_WAIT_RETEST",
    "OLD_PARENT_EXTENSION",
    "INSUFFICIENT_LEARNING_SAMPLE",
    "WEAK_CALIBRATION",
    "SENIOR_CONTEXT_CAUTION",
    "MARGINAL_SETUP_HISTORY",
    "MANAGEMENT_DOMINATED_NEGATIVE_HISTORY",
    "LOWER_TF_SOFT_INVALIDATION_OF_SENIOR_CORE",
})

PORTFOLIO_POLICIES = {
    "Impulse": {
        "mode": "IMPULSE_ONLY",
        "initial_normal": 0.20,
        "initial_super": 0.40,
        "max_single_asset_fraction": 0.50,
        "max_gross": 0.50,
        "hard_drawdown": 0.15,
        "position_step": 0.05,
    },
    "Aggressive": {
        "mode": "AGGRESSIVE",
        "initial_normal": 0.50,
        "initial_super": 1.00,
        "max_single_asset_fraction": 5.00,
        "max_gross": 5.00,
        "leverage_limit": 5.00,
        "hard_drawdown": 0.20,
        "position_step": 0.05,
        "scale_ladder": (0.50, 0.75, 1.00, 1.25, 1.50, 2.00, 3.00, 4.00, 5.00),
    },
    "Champion": {
        "mode": "CORE",
        "initial_normal": 0.10,
        "initial_super": 0.25,
        "max_single_asset_fraction": 1.00,
        "max_gross": 2.00,
        "hard_drawdown": 0.15,
        "position_step": 0.05,
    },
    "Challenger": {
        "mode": "CHALLENGER",
        "initial_normal": 0.10,
        "initial_super": 0.25,
        "max_single_asset_fraction": 1.00,
        "max_gross": 2.00,
        "hard_drawdown": 0.15,
        "position_step": 0.05,
    },
    "Currency": {
        "display_name": "Валютный портфель",
        "mode": "CURRENCY",
        "allowed_assets": ("CNYRUBF",),
        "initial_nav_rub": 10_000.0,
        "directions": ("LONG", "SHORT", "CASH"),
        "max_gross": 10.00,
        "leverage_limit": 10.00,
        "hard_drawdown": 0.35,
        "weekend_carry_allowed": True,
        "position_step": 0.05,
        "runtime_status": "OWNER_DEFINED_PENDING_IMPLEMENTATION",
        "stop_risk_note": "Use structural risk governor; no new per-trade override is invented here.",
    },
}

PAPER_RISK_POLICY = {
    "per_idea_structural_stop_risk_cap_nav": 0.02,
    "standard_drawdown_profile": {
        "normal_until": 0.08,
        "caution_until": 0.11,
        "defense_until": 0.135,
        "hard_stop": 0.15,
    },
    "aggressive_drawdown_profile": {
        "normal_until": 0.10,
        "caution_until": 0.14,
        "defense_until": 0.17,
        "hard_stop": 0.20,
    },
}

LIVE_RISK_POLICY = {
    "max_stop_risk_nav": 0.005,
    "max_total_open_stop_risk_nav": 0.025,
    "max_correlated_stop_risk_nav": 0.0125,
    "max_single_asset_fraction": 0.25,
    "max_gross": 1.25,
    "daily_loss_stop": 0.02,
    "weekly_loss_stop": 0.05,
    "hard_drawdown_stop": 0.10,
    "allow_new_risk_without_durable_storage": False,
    "principle": "Live account remains independently fail-closed and stricter than research books.",
}

SOURCE_POLICY = {
    "one_valid_primary_source_for_paper": True,
    "source_identity_pinned_for_position_lifetime": True,
    "same_source_for_entry_mark_mfe_mae_stop_target_exit": True,
    "exact_contract_pinned_when_available": True,
    "foreign_source_cannot_close_position": True,
    "stale_or_missing_pinned_source_action": "FREEZE_MARK_AND_EXECUTION",
    "mixed_source_episode_learning_eligible": False,
    "nq_proxy_execution_allowed": False,
}

SIGNAL_POLICY = {
    "published_direction_is_execution_authority": True,
    "published_direction": ("LONG", "SHORT"),
    "principle": (
        "Quality filtering happens before publishing a directional signal. Once "
        "a current LONG/SHORT is published, start staged risk unless a canonical "
        "hard veto is present."
    ),
    "normal_signal_can_probe_below_rr_floor_if_net_positive": True,
    "cost_negative_probe_allowed": False,
    "actual_price_anti_chase_remains_hard": True,
}

SETUP_GRADES = {
    "A+": "Institutional quality; eligible for strongest scaling subject to risk.",
    "A": "High quality; normal/full staged execution.",
    "B": "Exploratory; Impulse/Aggressive only at deliberately small size.",
    "C": "NO_TRADE.",
}

LEARNING_POLICY = {
    "independent_market_episode_not_portfolio_copy": True,
    "exclude_mixed_price_sources": True,
    "exclude_contract_mismatch": True,
    "exclude_administrative_rebase": True,
    "exclude_missing_mfe_mae_path": True,
    "direction_error_separate_from_execution_error": True,
    "management_error_must_not_auto_penalize_direction": True,
    "new_rules_default": "SHADOW",
    "promotion_requires": (
        "OOS",
        "VAULT",
        "COST_STRESS",
        "TIME_STABILITY",
        "REGIME_STABILITY",
        "SUFFICIENT_SAMPLE",
    ),
    "knowledge_count_does_not_raise_intelligence_by_itself": True,
}


def _rule(rule_id, domain, statement):
    return {"id": rule_id, "domain": domain, "status": "CANON", "statement": statement}


CANONICAL_RULES = [
    _rule("CTC01","governance","One current canonical policy path owns every trading decision; historical Rxx helpers are audit history."),
    _rule("CTC02","governance","Decision order is DATA -> THESIS -> TIMING -> ECONOMICS -> RISK -> SIZE -> LIFECYCLE -> LEARNING."),
    _rule("CTC03","governance","A later stage may never override a failed canonical hard gate from an earlier stage."),
    _rule("CTC04","objective","Post-cost target economics must be positive; negative target economics is a hard block."),
    _rule("CTC05","objective","Among economically valid policies prioritize sustainable win rate >=65%, then net profit/capture, then drawdown."),
    _rule("CTC06","classification","Every executable setup is A+, A, B or C; grade never overrides data, invalidation or risk hard gates."),
    _rule("CTC07","classification","B is exploratory only for Impulse/Aggressive; C is NO_TRADE."),
    _rule("CTC08","governance","A famous manager, paper or expert principle is a hypothesis until VERITAS validates it on its own clean outcomes."),

    _rule("CTC09","data","A paper position may use one valid primary source after source/session/freshness checks."),
    _rule("CTC10","data","The position owns its entry source identity for its entire lifetime."),
    _rule("CTC11","data","Entry, marking, MFE/MAE, stops, targets and exits use the same source identity and exact contract when known."),
    _rule("CTC12","data","A foreign/proxy source cannot close or invalidate an existing position."),
    _rule("CTC13","data","If the pinned source is stale/unavailable, freeze execution and retain the last verified mark rather than cross-source repricing."),
    _rule("CTC14","data","NQ execution requires a direct futures quote; QQQ/cash-index proxy cannot confirm or fill NQ."),
    _rule("CTC15","data","Mixed-source, contract-mismatch and corrupted-price episodes are excluded from learning without rewriting the accounting ledger."),

    _rule("CTC16","signal","Quality filtering occurs before publication of LONG/SHORT."),
    _rule("CTC17","signal","A current published LONG/SHORT starts staged risk unless a canonical hard veto is present."),
    _rule("CTC18","signal","Old parent WAIT_RETEST, event age, target reached or extension cannot flatten a freshly rebased current signal."),
    _rule("CTC19","signal","Sub-floor R/R may reduce a net-positive current signal to a probe; it is not automatically a full block."),
    _rule("CTC20","signal","A cost-negative target or expected move below the canonical cost buffer is never eligible even as a probe."),
    _rule("CTC21","signal","A confirmed execution-timeframe direction conflict remains a hard veto for new risk."),
    _rule("CTC22","signal","A lower-timeframe soft conflict cannot by itself liquidate an intact senior-horizon core position."),
    _rule("CTC23","signal","Absolute setup invalidation blocks new risk; soft INVALIDATED/NO_TRADE telemetry alone does not force an open position to exit."),
    _rule("CTC24","signal","A new continuation event receives a new identity; lateness is measured from the new event, not from the spent parent breakout."),

    _rule("CTC25","structure","Breakout levels and originating event identity are fixed from closed structural observations and cannot chase price."),
    _rule("CTC26","structure","LONG continuation is HH/HL; SHORT continuation is LH/LL; structure is symmetrical."),
    _rule("CTC27","structure","Breakout quality uses level break, acceptance, volume/activity, volatility expansion and subsequent structure."),
    _rule("CTC28","structure","RANGE_LOW_VOL requires stronger evidence because false-breakout risk is elevated."),
    _rule("CTC29","structure","Retest/hold after a break is an independent entry family and may define a fresh continuation event."),
    _rule("CTC30","multitimeframe","1m/5m time execution; 1h/4h manage trade structure; 1d/3d/7d define senior context/core thesis."),
    _rule("CTC31","multitimeframe","Senior context can reduce tactical size but does not automatically veto a qualified fast breakout/reversal."),
    _rule("CTC32","timing","Anti-chase is evaluated at the fresh executable price against the current trigger and realized volatility."),

    _rule("CTC33","economics","Commission is 0.05% per side and paper slippage is 0.04% per side unless a more conservative observed spread applies."),
    _rule("CTC34","economics","Base modeled round trip is 0.18%; minimum move is max(0.19%, 1.2 x modeled round-trip cost)."),
    _rule("CTC35","economics","Funding is 16% ACT/365.25 on current notional after a free first 24 hours."),
    _rule("CTC36","economics","Target, stop and adverse modeled fills are recomputed at final entry after all setup/sizing mutations."),
    _rule("CTC37","economics","Adds must have their own remaining room and economics; the original target cannot justify a fresh add."),

    _rule("CTC38","risk","Structural invalidation is chosen first; position size is then fitted to stop-risk, never the reverse."),
    _rule("CTC39","risk","Model paper per-idea structural stop risk is capped at 2% NAV."),
    _rule("CTC40","risk","Drawdown changes size/gross limits; it does not rewrite signal quality."),
    _rule("CTC41","risk","Standard books hard-stop new risk at 15% drawdown; Aggressive at 20%; Currency owner limit is 35%."),
    _rule("CTC42","risk","Live capital is independently fail-closed with stricter 0.5% per-idea and portfolio risk limits."),
    _rule("CTC43","sizing","Position fractions move in 5% increments."),
    _rule("CTC44","sizing","Aggressive starts about 50% on normal signal and 100% on SUPER, then earns leverage only through stronger structure/evidence and protected risk."),
    _rule("CTC45","sizing","Champion/Challenger have no implicit leverage: canonical single-asset fraction is capped at 100% unless separately authorized."),

    _rule("CTC46","lifecycle","A position lifecycle is OPEN -> ADD -> PROTECT -> HARVEST -> RUNNER -> EXIT; only lifecycle authority changes open size."),
    _rule("CTC47","add","Add only after a distinct same-direction confirmation, favorable progress, sufficient remaining edge and stop-risk capacity."),
    _rule("CTC48","add","Never automatically average a losing position; pyramiding is earned by favorable movement and new evidence."),
    _rule("CTC49","stop","LONG stop sits below confirmed local support/swing low; SHORT stop above confirmed local resistance/swing high with volatility-aware buffer."),
    _rule("CTC50","stop","Stops never widen after protection or reload; LONG protection ratchets upward, SHORT downward."),
    _rule("CTC51","profit","Breakeven is economic breakeven after paid/projected costs, not simply the entry price."),
    _rule("CTC52","profit","Partial profit is dynamic: stronger trend -> smaller harvest and larger runner; weakening/near obstacle -> larger harvest."),
    _rule("CTC53","profit","After harvest, reload is a new add decision requiring fresh breakout/structure/volume and positive post-cost economics."),
    _rule("CTC54","exit","Soft INVALIDATED, generic WAIT or a tiny opposite fast signal cannot force a fee-negative discretionary exit while thesis and hard risk remain intact."),

    _rule("CTC55","exit","Immediate full exit authority is reserved for true stop/risk breach, explicit hard thesis invalidation, confirmed direction flip/structural failure or portfolio hard stop."),
    _rule("CTC56","learning","One market episode is one independent learning observation even if multiple portfolios traded it."),
    _rule("CTC57","learning","Classify direction error separately from late entry, stop error, exit error, sizing error and source/data error."),
    _rule("CTC58","learning","Management-dominated losses must not be interpreted automatically as evidence that trade direction was wrong."),
    _rule("CTC59","learning","New knowledge/rules begin in SHADOW and require OOS, Vault, cost and regime/time robustness before promotion."),
    _rule("CTC60","learning","Intelligence rises from validated decision/outcome quality and clean learning, not from the raw count of stored rules."),
]

IMPLEMENTATION_GAPS = [
    {
        "id": "GAP01",
        "severity": "HIGH",
        "area": "portfolio",
        "current": "Champion max_fraction=2.0",
        "canonical": "Champion max_single_asset_fraction=1.0",
        "action": "Cap Champion single-asset exposure at 100% unless leverage is separately authorized.",
    },
    {
        "id": "GAP02",
        "severity": "HIGH",
        "area": "portfolio",
        "current": "Challenger max_fraction=2.0",
        "canonical": "Challenger max_single_asset_fraction=1.0",
        "action": "Cap Challenger single-asset exposure at 100% unless leverage is separately authorized.",
    },
    {
        "id": "GAP03",
        "severity": "HIGH",
        "area": "currency",
        "current": "Currency portfolio remains SETUP_PENDING with zero capital/gross.",
        "canonical": "10,000 RUB, CNYRUBF only, long/short/cash, max 10x gross, 35% hard DD, weekend carry allowed.",
        "action": "Implement owner-defined Currency policy without inventing an unspecified per-trade risk override.",
    },
    {
        "id": "GAP04",
        "severity": "MEDIUM",
        "area": "architecture",
        "current": "Many historical _signal_first_admission definitions remain importable.",
        "canonical": "One canonical admission path.",
        "action": "Keep R85 final authority lock now; later collapse historical wrappers behind one CanonicalAdmissionEngine.",
    },
    {
        "id": "GAP05",
        "severity": "MEDIUM",
        "area": "objective",
        "current": "R35 and R40 describe objective priority differently.",
        "canonical": "Positive post-cost economics is a hard constraint; then win rate, net profit/capture, drawdown.",
        "action": "Make dashboard/API objective text use the canonical reconciled objective.",
    },
    {
        "id": "GAP06",
        "severity": "LOW",
        "area": "documentation",
        "current": "README_R82_COST_POLICY.md still describes pre-R81 0.04% commission / 1.1x buffer.",
        "canonical": "0.05% commission / 1.2x buffer; base round trip 0.18%.",
        "action": "Mark R82 README explicitly historical or add a current canonical cost-policy document.",
    },
    {
        "id": "GAP07",
        "severity": "LOW",
        "area": "naming",
        "current": "veritas_costs.VERSION still says R82_USER_COST_POLICY although R81 restored owner parameters.",
        "canonical": "Canonical cost values are authoritative independent of historical label.",
        "action": "Rename version only when backward-compatibility impact is checked.",
    },
    {
        "id": "GAP08",
        "severity": "MEDIUM",
        "area": "learning",
        "current": "External knowledge automation is disabled in the current runtime.",
        "canonical": "External knowledge may expand only through shadow/validation; market outcome learning remains active.",
        "action": "Do not enable automatic external rule promotion; decide separately whether discovery/import automation should run.",
    },
]


def validate_constitution():
    ids = [r["id"] for r in CANONICAL_RULES]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate canonical rule id")
    if set(HARD_VETOES) & set(SOFT_VETOES):
        raise ValueError("hard and soft veto sets overlap")
    if tuple(STAGE_ORDER) != (
        "DATA","THESIS","TIMING","ECONOMICS","RISK","SIZE","LIFECYCLE","LEARNING"
    ):
        raise ValueError("canonical stage order changed")
    if COST_POLICY["round_trip_base_cost_pct"] != 2 * (
        COST_POLICY["commission_rate_per_side"] + COST_POLICY["slippage_rate_per_side"]
    ):
        raise ValueError("cost policy arithmetic mismatch")
    if len(CANONICAL_RULES) != 60:
        raise ValueError("expected 60 canonical rules")
    return True


validate_constitution()
