"""Owner-approved paper trend-acceleration policy.

Kept outside the frozen canonical constitution line ceiling while remaining
imported by that constitution as its single runtime authority. Numerical stage
sizes require replay/OOS validation; live Currency semantics are excluded.
"""

TREND_ACCELERATION_POLICY = {
    "version": "CTC_TREND_ACCELERATION_V2",
    "teaching_id": "USER_TREND_ACCELERATION_2026_10_09",
    "enabled": True,
    "scope": "PAPER_NON_CURRENCY_PORTFOLIOS",
    "minimum_independent_evidence": 3,
    "minimum_expected_move_pct": 0.0040,
    "maximum_target_progress": 0.65,
    "accepted_structure_states": ("BUILDING_TREND", "CONFIRMED_TREND"),
    "fast_horizons": ("1m", "5m"),
    "mid_confirmation_timeframes": ("15m", "30m"),
    "senior_confirmation_timeframes": ("1h", "4h"),
    # Owner P0 rule, 2026-10-10: on a verified event impulse the first
    # breakout must establish risk quickly, then every distinct confirmation
    # earns another step up to the portfolio's maximum permitted exposure.
    "stage_targets_standard": {
        "FAST_CONFIRMED": 0.50,
        "MID_CONFIRMED": 0.75,
        "SENIOR_CONFIRMED": 1.00,
    },
    "stage_targets_aggressive": {
        "FAST_CONFIRMED": 1.50,
        "MID_CONFIRMED": 3.00,
        "SENIOR_CONFIRMED": 5.00,
    },
    "temporary_caps": {
        "IMPULSE_ONLY": {"max_fraction": 1.00, "max_gross": 1.00},
        "CORE": {"max_fraction": 1.00, "max_gross": 2.00},
        "CHALLENGER": {"max_fraction": 1.00, "max_gross": 2.00},
        "AGGRESSIVE": {"max_fraction": 5.00, "max_gross": 5.00},
    },
    "event_impulse": {
        "enabled": True,
        "owner_priority": "P0_HIGHEST",
        "owner_rule_authority": True,
        "additional_proof_required": False,
        "entry_requires_news": False,
        "news_confirmation_role": "HOLD_AND_SCALE_CONFIRMATION",
        "game_changer_immediate_max": True,
        "game_changer_requires_senior_level_break": True,
        "game_changer_requires_move_above_completed_1h_volatility": True,
        "forming_bar_close_required": False,
        "game_changer_role": "IMMEDIATE_MAX_ALLOWED_BY_PORTFOLIO_AND_STOP_RISK",
        "accepted_regimes": ("UPTREND_HIGH_VOL", "DOWNTREND_HIGH_VOL"),
        "accepted_fast_tiers": ("LONG", "SUPER_LONG", "SHORT", "SUPER_SHORT"),
        "minimum_structure_score": 0.90,
        "minimum_independent_evidence": 4,
        "breakout_required": True,
        "activity_or_volume_confirmation_required": True,
        "volatility_expansion_required": True,
        "relative_volume_floor_if_available": 1.25,
        "fast_target_standard": 0.50,
        "fast_target_aggressive": 1.50,
        "confirmed_target_standard": 1.00,
        "confirmed_target_aggressive": 5.00,
        "defer_fixed_take_profit": True,
        "target_reference_mode": "HIGHER_TIMEFRAME_HIGHS_AND_ZONES",
        "reassess_targets_after_impulse_exhaustion": True,
        "old_target_reactivation_forbidden": True,
        "volatility_contraction_required": True,
        "exhaustion_confirmation_observations": 2,
        "post_impulse_target_basis": "CURRENT_STRUCTURE_AND_SENIOR_EXTREMES",
        "reacceleration_defers_fixed_take_profit_again": True,
        "premature_tp_impulse_reentry": True,
        "premature_tp_reentry_requires_signal_tier": ("SUPER_LONG", "SUPER_SHORT"),
        "premature_tp_reentry_requires_active_impulse": True,
        "premature_tp_reentry_requires_no_volatility_contraction": True,
        "premature_tp_same_event_reuse_allowed": True,
        "exit_authority": (
            "STRUCTURAL_BREAK", "CONFIRMED_REVERSAL", "TRAILING_STOP",
            "RISK_HARD_STOP", "PORTFOLIO_HARD_STOP",
        ),
        "principle": (
            "A sharp breakout is confirmed at the verified quote, not at the close "
            "of the forming candle. If a forming fast candle crosses a previously "
            "known senior structural high/low and its move already exceeds completed "
            "1m/5m/1h volatility, classify GAME_CHANGER_EXTREME and request the "
            "maximum portfolio exposure permitted by stop-risk immediately. "
            "Less extreme event impulses may scale in stages. News strengthens hold "
            "conviction but never delays the first structural entry. Do not execute "
            "a fixed take-profit while the impulse remains active. After confirmed "
            "impulse exhaustion and volatility contraction, discard the entry-time "
            "target ladder and rebuild TP/TP2 from current structure and senior-TF "
            "extrema. If an old TP nevertheless closes a correct position while a "
            "SUPER signal and the impulse remain active without volatility contraction, "
            "restore the same-direction exposure as an impulse continuation, not as "
            "reuse of a stale setup. Reacceleration disables fixed TP again."
        ),
    },
    "profit_protection": {
        "mfe_activation_pct_points": 0.15,
        "immediate_activation_pct_points": 0.30,
        "minimum_positive_net_pct": 0.0002,
        "hold_seconds_by_timeframe": {
            "1m": 45, "5m": 90, "15m": 120, "30m": 150,
            "1h": 180, "4h": 600, "1d": 1800,
        },
    },
    "execution_efficiency": {
        "max_repeated_add_fee_to_positive_edge": 0.25,
        "require_positive_incremental_net_reward": True,
        "block_add_on_fast_opposite_confirmation": True,
        "critical_excursion_durable": True,
        "persistence_timer_durable": True,
        "diagnostic_gap_must_not_drop_fresh_quote": True,
    },
    "reversal_exit": {
        "enabled": True,
        "horizons": ("1m", "5m"),
        "minimum_independent_evidence": 3,
        "minimum_expected_move_pct": 0.0040,
        "accepted_structure_states": ("BUILDING_TREND", "CONFIRMED_TREND"),
        "entry_permission_required_for_exit": False,
        "fast_exit_held_horizons": ("1h",),
        "require_exact_held_horizon_opposite": True,
        "require_fast_confirmed_opposite": True,
        "principle": (
            "Fast confirmed opposite structure may close stale exposure; "
            "opening the opposite side still requires canonical admission."
        ),
    },
    "principle": (
        "Scale a winning confirmed trend by stop-risk, not nominal allocation. "
        "Each new causal confirmation may earn one larger add; never average a "
        "loser. During an active event impulse, a consumed fixed target is a "
        "continuation/replan trigger rather than a reason to abandon the trend. "
        "Protect prior tranches first and defer fixed profit taking until the "
        "impulse structurally exhausts."
    ),
    "parameter_validation_status": "OWNER_P0_CANONICAL_NO_ADDITIONAL_PROOF",
}


# Second owner refinement, 2026-10-09: reserve the largest paper exposure for
# trend days that are already confirmed by multiple independent structural
# dimensions. Cross-asset context is recorded but remains shadow-only until OOS.
TREND_DAY_EFFICIENCY_POLICY = {
    "version": "CTC_TREND_DAY_EFFICIENCY_V1",
    "teaching_id": "USER_TREND_DAY_EFFICIENCY_2026_10_09",
    "enabled": True,
    "scope": "PAPER_NON_CURRENCY_PORTFOLIOS",
    "accepted_phases": ("TREND_DAY", "IMPULSE_TREND"),
    "minimum_score": 0.78,
    "minimum_coverage": 0.75,
    "minimum_independent_evidence": 4,
    "minimum_expected_move_pct": 0.0060,
    "maximum_target_progress": 0.50,
    "require_mid_confirmation": True,
    "require_senior_confirmation": True,
    "extreme_target_standard": 1.00,
    "extreme_target_aggressive": 5.00,
    "temporary_caps": {
        "IMPULSE_ONLY": {"max_fraction": 1.00, "max_gross": 1.00},
        "CORE": {"max_fraction": 1.00, "max_gross": 2.00},
        "CHALLENGER": {"max_fraction": 1.00, "max_gross": 2.00},
        "AGGRESSIVE": {"max_fraction": 5.00, "max_gross": 5.00},
    },
    "runner": {
        "requires_profit_protection": True,
        "trend_day_ratio": 0.75,
        "impulse_trend_ratio": 0.85,
        "maximum_ratio": 0.85,
    },
    "cross_asset": {
        "context_only": True,
        "size_influence_enabled": False,
        "maximum_shadow_bonus": 0.03,
        "principle": (
            "Cross-asset and breadth context may be recorded for later OOS "
            "validation but cannot block or enlarge a paper trade in this version."
        ),
    },
    "parameter_validation_status": "OWNER_P0_CANONICAL_NO_ADDITIONAL_PROOF",
    "principle": (
        "The largest allocation is earned only when the existing trend-day "
        "classifier, intraday structure, intermediate timeframes and senior "
        "timeframes agree while meaningful target distance remains. TP1 should "
        "harvest less of a protected trend-day winner so a larger runner can "
        "capture the tail."
    ),
}
