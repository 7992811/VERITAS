"""Canonical VERITAS paper cost policy, shared by planning and accounting.

Commission and slippage are per execution side. The first 24 elapsed hours
of each position are free; funding then accrues at 16% on current notional.
Previously booked costs are historical facts and are never recomputed here.
"""
from datetime import datetime, timedelta, timezone
import veritas_canonical_constitution as CTC

VERSION = "CTC_V2_COST_POLICY"
COMMISSION_RATE = float(CTC.COST_POLICY["commission_rate_per_side"])
SLIPPAGE_RATE = float(CTC.COST_POLICY["slippage_rate_per_side"])
COST_BUFFER_MULTIPLE = float(CTC.COST_POLICY["cost_buffer_multiple"])
FUNDING_ANNUAL_RATE = float(CTC.COST_POLICY["funding_annual_rate"])
FUNDING_FREE_SECONDS = float(CTC.COST_POLICY["funding_free_seconds"])
YEAR_SECONDS = 365.25 * 86400.0
ROUND_TRIP_RATE = float(CTC.COST_POLICY["round_trip_base_cost_pct"])


def utc_datetime(value):
    if isinstance(value, datetime):
        d = value
    else:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def funding_fraction(hold_seconds):
    return FUNDING_ANNUAL_RATE * max(0.0, float(hold_seconds) - FUNDING_FREE_SECONDS) / YEAR_SECONDS


def funding_between(notional, opened_at, last_mark_at, now):
    end = utc_datetime(now)
    start = max(utc_datetime(last_mark_at),
                utc_datetime(opened_at) + timedelta(seconds=FUNDING_FREE_SECONDS))
    return abs(float(notional)) * FUNDING_ANNUAL_RATE * max(0.0, (end-start).total_seconds()) / YEAR_SECONDS


def entry_cost_multiple(asset=None):
    """Resolve the canonical entry threshold for the selected instrument."""
    overrides = CTC.COST_POLICY.get("entry_cost_multiple_by_asset") or {}
    return float(overrides.get(str(asset or "").strip().upper(),
                               CTC.COST_POLICY["entry_cost_multiple"]))


def policy(asset=None):
    multiple = entry_cost_multiple(asset)
    floor = float(CTC.COST_POLICY["minimum_expected_move_floor_pct"])
    return dict(version=VERSION, commission_rate_per_side=COMMISSION_RATE,
                slippage_rate_per_side=SLIPPAGE_RATE,
                round_trip_base_cost_pct=ROUND_TRIP_RATE,
                cost_buffer_multiple=COST_BUFFER_MULTIPLE,
                entry_cost_multiple=multiple,
                entry_cost_multiple_by_asset=dict(CTC.COST_POLICY.get("entry_cost_multiple_by_asset") or {}),
                minimum_expected_move_floor_pct=floor,
                minimum_expected_move_formula=f"max({100*floor:.2f}%, {multiple:.1f} * modeled_round_trip_cost)",
                funding_annual_rate=FUNDING_ANNUAL_RATE,
                funding_free_seconds=FUNDING_FREE_SECONDS,
                funding_basis="ELAPSED_TIME_AFTER_FIRST_24_HOURS_CURRENT_NOTIONAL",
                calendar_basis="ACT/365.25", applies_to="PAPER_PORTFOLIOS",
                observed_spread_separate=True)
