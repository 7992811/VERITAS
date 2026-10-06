"""Canonical VERITAS paper cost policy, shared by planning and accounting.

Commission and slippage are per execution side. The first 24 elapsed hours
of each position are free; funding then accrues at 16% on current notional.
Previously booked costs are historical facts and are never recomputed here.
"""
from datetime import datetime, timedelta, timezone

VERSION = "CTC_V1_COST_POLICY"
COMMISSION_RATE = 0.0004
SLIPPAGE_RATE = 0.0004
COST_BUFFER_MULTIPLE = 1.1
FUNDING_ANNUAL_RATE = 0.16
FUNDING_FREE_SECONDS = 86400.0
YEAR_SECONDS = 365.25 * 86400.0
ROUND_TRIP_RATE = 2 * (COMMISSION_RATE + SLIPPAGE_RATE)


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


def policy():
    return dict(version=VERSION, commission_rate_per_side=COMMISSION_RATE,
                slippage_rate_per_side=SLIPPAGE_RATE,
                round_trip_base_cost_pct=ROUND_TRIP_RATE,
                cost_buffer_multiple=COST_BUFFER_MULTIPLE,
                funding_annual_rate=FUNDING_ANNUAL_RATE,
                funding_free_seconds=FUNDING_FREE_SECONDS,
                funding_basis="ELAPSED_TIME_AFTER_FIRST_24_HOURS_CURRENT_NOTIONAL",
                calendar_basis="ACT/365.25", applies_to="PAPER_PORTFOLIOS",
                observed_spread_separate=True)
