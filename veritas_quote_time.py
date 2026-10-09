"""Exchange observation times, distinct from response/server timestamps."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import math

PROTECTIVE_MAX_AGE_SECONDS = 300

def execution_max_age_seconds(asset):
    """Canonical freshness ceiling for any simulated execution quote."""
    return 30 if str(asset or "").upper() in ("BTC","ETH") else 120


def utc_datetime(value):
    if not value:
        return None
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except (TypeError, ValueError):
        return None


def moex_observed_at(row, now=None):
    """SYSTIME identifies the response, never the underlying last trade."""
    now = now or datetime.now(timezone.utc)
    msk = ZoneInfo('Europe/Moscow')
    for field in ('TIME', 'UPDATETIME'):
        value = str(row.get(field) or '').strip()
        if not value:
            continue
        try:
            if len(value) <= 15 and ':' in value:
                date = row.get('TRADEDATE') or row.get('DATE')
                if not date:
                    # A time-only quote cannot establish its trading date.
                    continue
                dt = datetime.fromisoformat(str(date)[:10] + 'T' + value)
            else:
                dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            dt = dt.replace(tzinfo=msk) if dt.tzinfo is None else dt
            dt = dt.astimezone(timezone.utc)
            if dt <= now + timedelta(seconds=5):
                return dt.isoformat()
        except (TypeError, ValueError):
            continue
    raise ValueError('MOEX_QUOTE_EXCHANGE_TIME_MISSING_OR_FUTURE')


def quote_gate(observed_at, horizon=None, now=None, *, protective=False, execution=False, asset=None):
    now = now or datetime.now(timezone.utc)
    dt = utc_datetime(observed_at)
    age = (now - dt).total_seconds() if dt else None
    # One source remains sufficient, but protective execution is stricter than
    # research admission. A stop / take-profit must never be simulated from a
    # materially stale quote: if no <=5 minute observation exists, the protective
    # lane degrades fail-closed until a fresh observation arrives.
    if execution:
        limit = execution_max_age_seconds(asset)
    elif protective:
        limit = PROTECTIVE_MAX_AGE_SECONDS
    else:
        # Entry freshness is horizon-specific. A slower thesis may use older
        # context, but the simulated order still needs a recent observation.
        limit = {
            '1m': 90,
            '5m': 300,
            '1h': 1200,
            '4h': 1800,
            '1d': 3600,
            '3d': 3600,
            '7d': 3600,
        }.get(str(horizon or ''), 3600)
    ok = age is not None and math.isfinite(age) and -5 <= age <= limit
    return {'eligible': ok, 'observed_at': dt.isoformat() if dt else None,
            'age_seconds': age, 'max_age_seconds': limit,
            'reason': None if ok else 'QUOTE_TIME_MISSING' if dt is None else
            'QUOTE_TIME_FUTURE' if age < -5 else 'QUOTE_TOO_OLD_FOR_HORIZON'}
