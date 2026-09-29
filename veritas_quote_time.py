"""Exchange observation times, distinct from response/server timestamps."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import math


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


def quote_gate(observed_at, horizon=None, now=None, *, protective=False):
    now = now or datetime.now(timezone.utc)
    dt = utc_datetime(observed_at)
    age = (now - dt).total_seconds() if dt else None
    # One source remains sufficient. Delayed research quotes must not masquerade
    # as fast entries. Protective exits may use the newest delayed observation.
    limit = 3600 if protective or horizon != '5m' else 300
    ok = age is not None and math.isfinite(age) and -5 <= age <= limit
    return {'eligible': ok, 'observed_at': dt.isoformat() if dt else None,
            'age_seconds': age, 'max_age_seconds': limit,
            'reason': None if ok else 'QUOTE_TIME_MISSING' if dt is None else
            'QUOTE_TIME_FUTURE' if age < -5 else 'QUOTE_TOO_OLD_FOR_HORIZON'}
