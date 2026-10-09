"""VERITAS Canonical Trading Constitution v2.

This module is the machine-readable source of truth for paper/live policy.
Production admission and sizing must read these values directly. Historical
Rxx helpers may remain for replay, telemetry and lifecycle compatibility, but
they have no authority to override canonical admission or portfolio limits.

Principle: one trading decision -> one canonical policy path.
"""
from __future__ import annotations

VERSION = "CTC_V2_2026_10_06"
BASIS_RUNTIME = "CTC_V2_CANONICAL_RUNTIME"

STRATEGY_EPOCH = "EQ7_2026_10_07_INTRABAR_STRUCTURE"
STRATEGY_ROLE_POLICY = {
    "IMPULSE_ONLY": {"name":"EARLY_IMPULSE","horizons":("1m","5m","1h"),"min_independent":2},
    "AGGRESSIVE": {"name":"CONFIRMED_TREND","horizons":("5m","1h","4h","1d"),
                   "require_trend":True,"min_structure_score":0.65,"min_independent":3},
    "CORE": {"name":"CHAMPION_CONTROL","horizons":("1h","4h","1d","3d"),
             "require_trend":True,"min_structure_score":0.70,"min_independent":3},
    "CHALLENGER": {"name":"CHALLENGER_LOCAL_TRIGGER","horizons":("5m","1h","4h"),
                   "require_trend":True,"min_structure_score":0.70,"min_independent":4,
                   "variant":"MATCHED_EVENT_PAPER_ONLY"},
}

# Explicit owner correction, 2026-10-06. These are operational safeguards;
# numeric defaults are not an empirically validated trading edge.
STRUCTURAL_ENTRY_POLICY = {
    "version": "CTC_SAME_TF_STRUCTURE_V1",
    "teaching_id": "USER_TF_STRUCTURE_2026_10_06",
    "required": True, "atr_period": 20, "pivot_left": 2, "pivot_right": 2,
    "stop_buffer_atr": 0.15, "max_stop_atr": 3.0,
    "max_extension_atr": 0.50, "max_signal_age_bars": 1.0,
    "target_r_multiple": 2.0, "min_target_atr": 1.5,
    "same_timeframe_for_trigger_stop_target_atr": True,
    "closed_bar_confirmation": True, "immutable_event_time": True,
    "same_source_candles_and_execution": True,
    "minimum_net_reward_risk": 1.15,
    "parameter_validation_status": "UNVALIDATED_DEFAULTS",
}

# The owner's 7 October correction explicitly refines the previous closed-bar
# rule. Keep that earlier policy and its immutable teaching record intact.
BREAKOUT_LIFECYCLE_POLICY = {
    "take_profit_is_partial_when_position_allows": True,
    "default_tp_runner_ratio": 0.50,
    "strong_trend_runner_ratio": 0.70,
    "aggressive_tp_runner_ratio": 0.60,
    "minimum_position_step": 0.05,
    "profit_lock_activation_floor_pct": 0.21,
    "principle": (
        "The first take-profit harvests part of a qualifying position and keeps a "
        "structural runner. A full close is reserved for a minimum-size position "
        "or an actual thesis/risk exit."
    ),
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
    _rule("CTC17","signal","A thesis opens risk on a confirmed structural breakout or native daily SMA50/200 rebound, confirmed on the chosen entry timeframe."),
    _rule("CTC18","signal","Refreshing a directional forecast never resets breakout time, restores a spent event, or creates a new current-price trigger."),
    _rule("CTC19","signal","Legacy setups and live orders retain the post-cost R/R floor. Owner-taught causal quote PAPER breakouts use positive weighted historical-target economics and the cost buffer; net R/R is diagnostic."),
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
    _rule("CTC30","multitimeframe","Quote breakouts explicitly record trigger, structural-stop, ATR and historical-target timeframes. A fast entry may borrow a certified parent swing only while that parent risk context remains structurally valid; otherwise it must use independently valid fast-TF risk geometry or be blocked."),
    _rule("CTC31","multitimeframe","Senior directional context can reduce tactical size but does not automatically veto a qualified fast breakout/reversal. An explicit structural failure of a senior timeframe whose swing/ATR is borrowed for risk is a hard risk-context veto."),
    _rule("CTC32","timing","Anti-chase is evaluated at the fresh executable price against the current trigger and realized volatility."),

    _rule("CTC33","economics","Commission is 0.04% per side and paper slippage is 0.04% per side unless a more conservative observed spread applies."),
    _rule("CTC34","economics","Base modeled round trip is 0.16%; model minimum move is max(0.19%, 1.1 x costs). Owner manual orders retain costs without profitability filters."),
    _rule("CTC35","economics","Funding is 16% ACT/365.25 on current notional after a free first 24 hours."),
    _rule("CTC36","economics","Target, stop and adverse modeled fills are recomputed at final entry after all setup/sizing mutations."),
    _rule("CTC37","economics","Model adds need their own remaining room and economics; manual adds retain held levels and the whole-position stop-risk budget."),

    _rule("CTC38","risk","Structural invalidation is chosen first; position size is then fitted to stop-risk, never the reverse."),
    _rule("CTC39","risk","All manual and automatic per-idea stop risk, including costs, is capped at 15% NAV."),
    _rule("CTC40","risk","Drawdown changes size/gross limits; it does not rewrite signal quality."),
    _rule("CTC41","risk","Standard books hard-stop new risk at 15% drawdown; Aggressive at 20%; Currency owner limit is 35%."),
    _rule("CTC42","risk","Model LIVE per-idea, total and correlated stop risk are each capped at 15% NAV. Manual orders use only the 15% allocation stop-risk budget plus broker execution requirements."),
    _rule("CTC43","sizing","New allocations use 5% increments. Historical-target partial exits reduce actual units by the recorded target fractions."),
    _rule("CTC44","sizing","Aggressive starts about 50% on normal signal and 100% on SUPER, then earns leverage only through stronger structure/evidence and protected risk."),
    _rule("CTC45","sizing","Champion/Challenger have no implicit leverage: canonical single-asset fraction is capped at 100% unless separately authorized."),

    _rule("CTC46","lifecycle","A position lifecycle is OPEN -> ADD -> PROTECT -> HARVEST -> RUNNER -> EXIT; only lifecycle authority changes open size."),
    _rule("CTC47","add","Add only after a distinct same-direction confirmation, favorable progress, sufficient remaining edge and stop-risk capacity."),
    _rule("CTC48","add","Never automatically average a losing position; pyramiding is earned by favorable movement and new evidence."),
    _rule("CTC49","stop","LONG stop sits below the previous confirmed swing low of the entry timeframe, SHORT above its swing high, with that same timeframe's ATR buffer."),
    _rule("CTC50","stop","Stops never widen after protection or reload; LONG protection ratchets upward, SHORT downward."),
    _rule("CTC51","profit","Breakeven is true economic breakeven after paid/projected costs. It is armed only after three distinct consecutive profitable management windows and the minimum dwell; the first two transient positive impulses do not alter the original stop architecture."),
    _rule("CTC52","profit","Partial profit is dynamic: stronger trend -> smaller harvest and larger runner; weakening/near obstacle -> larger harvest."),
    _rule("CTC53","profit","After harvest, reload is a new add decision requiring fresh breakout/structure/volume and positive post-cost economics."),
    _rule("CTC54","exit","Soft INVALIDATED, generic WAIT or a tiny opposite fast signal cannot force a fee-negative discretionary exit while thesis and hard risk remain intact."),

    _rule("CTC55","exit","Immediate full exit authority is reserved for true stop/risk breach, explicit hard thesis invalidation, confirmed direction flip/structural failure or portfolio hard stop. A hard invalidation of a canonical setup is shared by every open portfolio copy of that same setup."),
    _rule("CTC56","learning","One market episode is one independent learning observation even if multiple portfolios traded it."),
    _rule("CTC57","learning","Classify direction error separately from late entry, stop error, exit error, sizing error and source/data error."),
    _rule("CTC58","learning","Management-dominated losses must not be interpreted automatically as evidence that trade direction was wrong."),
    _rule("CTC59","learning","Every closed trade receives a structured postmortem. New knowledge/rules and parameter changes begin in SHADOW, must pass canonical-conflict checks plus OOS, Vault, cost and regime/time robustness, and require owner verification before promotion."),
    _rule("CTC60","learning","Intelligence rises from validated decision/outcome quality and clean learning, not from the raw count of stored rules."),
]

RESOLVED_IMPLEMENTATION_GAPS = [
    {"id":"GAP01","resolution":"Champion single-asset cap is 100% in canonical/runtime policy."},
    {"id":"GAP02","resolution":"Challenger single-asset cap is 100% in canonical/runtime policy."},
    {"id":"GAP03","resolution":"Currency: 10,000 RUB, CNYRUBF only, 10x, 35% hard DD, weekend carry."},
    {"id":"GAP04","resolution":"CanonicalAdmissionEngine v2 owns production admission; legacy admission is non-authoritative."},
    {"id":"GAP05","resolution":"Objective policy is canonical."},
    {"id":"GAP06","resolution":"Costs are canonical: 0.04% commission, 0.04% slippage, one 1.1x cost buffer."},
    {"id":"GAP07","resolution":"Cost module reads CTC directly."},
    {"id":"GAP08","resolution":"External knowledge remains shadow-first and independently validated."},
    {"id":"GAP09","resolution":"Runtime binding is import-order independent."},
    {"id":"GAP10","resolution":"Five canonical portfolios are required by API/runtime."},
    {"id":"GAP11","resolution":"Portfolio policies are generated from CTC instead of duplicated literals."},
    {"id":"GAP12","resolution":"Hard/soft blocker severity is classified centrally by CTC and unknown blockers fail closed."},
    {"id":"GAP13","resolution":"Impulse gross ceiling is canonical 0.50x instead of generic 2.00x."},
    {"id":"GAP14","resolution":"Currency initial/probe sizing is explicit in CTC."},
    {"id":"GAP15","resolution":"Legacy v72 source-rewrite launcher is archived outside production root."},
    {"id":"GAP16","resolution":"Release identity exposes product/CTC/UI/DB/deploy SHA consistently."},
    {"id":"GAP17","resolution":"Architecture guard rejects canonical authority that calls legacy admission fallbacks."},
    {"id":"GAP18","resolution":"Production candidate routing is patched to canonical signal-first selectors."},
]
IMPLEMENTATION_GAPS = []

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
    if COST_POLICY["entry_cost_multiple"] != COST_POLICY["cost_buffer_multiple"]:
        raise ValueError("entry cost buffer differs from the owner-approved canonical buffer")
    if len(CANONICAL_RULES) != 60:
        raise ValueError("expected 60 canonical rules")
    if tuple(PORTFOLIO_POLICIES) != PORTFOLIO_ORDER:
        raise ValueError("portfolio order/policy registry mismatch")
    if PORTFOLIO_POLICIES["Impulse"]["max_gross"] != 0.50:
        raise ValueError("Impulse max gross drift")
    if any(cap != 0.15 for cap in (PAPER_RISK_POLICY["per_idea_structural_stop_risk_cap_nav"],
            LIVE_RISK_POLICY["max_stop_risk_nav"], LIVE_RISK_POLICY["max_total_open_stop_risk_nav"],
            LIVE_RISK_POLICY["max_correlated_stop_risk_nav"])):
        raise ValueError("owner stop-risk cap drift")
    return True


validate_constitution()
