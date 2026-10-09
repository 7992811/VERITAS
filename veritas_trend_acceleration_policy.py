"""Owner-approved paper trend-acceleration policy.

Kept outside the frozen canonical constitution line ceiling while remaining
imported by that constitution as its single runtime authority. Numerical stage
sizes require replay/OOS validation; live Currency semantics are excluded.
"""

TREND_ACCELERATION_POLICY = {
    "version": "CTC_TREND_ACCELERATION_V1",
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
    "stage_targets_standard": {
        "FAST_CONFIRMED": 0.25,
        "MID_CONFIRMED": 0.50,
        "SENIOR_CONFIRMED": 0.75,
    },
    "stage_targets_aggressive": {
        "FAST_CONFIRMED": 0.75,
        "MID_CONFIRMED": 1.50,
        "SENIOR_CONFIRMED": 2.50,
    },
    "temporary_caps": {
        "IMPULSE_ONLY": {"max_fraction": 0.75, "max_gross": 1.00},
        "CORE": {"max_fraction": 0.75, "max_gross": 2.00},
        "CHALLENGER": {"max_fraction": 0.75, "max_gross": 2.00},
        "AGGRESSIVE": {"max_fraction": 2.50, "max_gross": 5.00},
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
    "reversal_exit": {
        "enabled": True,
        "horizons": ("1m", "5m"),
        "minimum_independent_evidence": 3,
        "minimum_expected_move_pct": 0.0040,
        "accepted_structure_states": ("BUILDING_TREND", "CONFIRMED_TREND"),
        "entry_permission_required_for_exit": False,
        "principle": (
            "Fast confirmed opposite structure may close stale exposure; "
            "opening the opposite side still requires canonical admission."
        ),
    },
    "principle": (
        "Scale a winning confirmed trend by stop-risk, not nominal allocation. "
        "Each new causal confirmation may earn one larger add; never average a "
        "loser, never chase after most of the target is spent, and protect prior "
        "tranches first."
    ),
    "parameter_validation_status": "OWNER_RULE_SHADOW_OOS_REQUIRED",
}
