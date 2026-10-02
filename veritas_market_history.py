"""Recent-first MOEX minute history; retrieval time never certifies freshness."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import math
import time

MOSCOW = ZoneInfo('Europe/Moscow')


def moex_minute(row):
    try:
        dt = datetime.fromisoformat(str(row.get('begin') or row.get('BEGIN')))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MOSCOW)
        values = {k: float(row.get(k, row.get(k.upper()))) for k in ('open','high','low','close')}
        if (any(not math.isfinite(v) or v <= 0 for v in values.values())
                or values['low'] > min(values['open'], values['close'])
                or values['high'] < max(values['open'], values['close'])):
            return None
        # IMOEX has zero contract volume; the published index turnover is value.
        turnover = float(row.get('value', row.get('VALUE')) or 0.)
        volume = float(row.get('volume', row.get('VOLUME')) or 0.)
        activity = turnover if turnover > 0 else volume
        if not math.isfinite(activity) or activity < 0:
            return None
        return dict(values, ts=dt.timestamp(), volume=activity,
                    activity_basis='value' if turnover > 0 else 'volume')
    except (TypeError, ValueError, OverflowError):
        return None


def complete_five_minutes(minutes, now):
    buckets = {}
    for b in sorted({b['ts']: b for b in minutes}.values(), key=lambda b:b['ts']):
        t = b['ts']
        if t % 60 or t + 60 > now:
            continue
        buckets.setdefault(int(t)//300*300, []).append(b)
    result = []
    for start, group in sorted(buckets.items()):
        if [b['ts'] for b in group] != list(range(start, start+300, 60)):
            continue
        result.append({'ts':float(start), 'open':group[0]['open'], 'close':group[-1]['close'],
                       'high':max(b['high'] for b in group), 'low':min(b['low'] for b in group),
                       'volume':sum(b['volume'] for b in group), 'source':'MOEX_1M_COMPLETE',
                       'activity_basis':group[-1].get('activity_basis','value')})
    return result[-500:]


def recent_moex_minutes(fetch_page, now, cached=(), budget_seconds=7., monotonic=time.monotonic):
    """Fetch the latest hour first, then backfill bounded earlier hourly slices.

    Each inclusive MOEX slice contains at most 64 minutes, below its 100-row
    page size. Timeout after any older slice retains already fetched new data.
    Cached minutes are merged by timestamp; partial five-minute buckets remain
    available for completion on the next refresh. No invented zero-volume bars.
    """
    end = int(now)//60*60-60
    floor = end-4*86400
    rows = {b['ts']:dict(b) for b in cached if floor <= b['ts'] <= end}
    deadline = monotonic()+budget_seconds
    fetched = 0
    error = None
    # Begin on a 5m boundary so every requested hour contains complete buckets.
    right = end
    for _ in range(96):
        if monotonic() >= deadline:
            break
        left = int((right-59*60)//300)*300
        expected = set(range(left, right+1, 60))
        # Always refresh the newest slice; older complete cached slices cost no IO.
        if fetched == 0 or not expected.issubset(rows):
            params = {'from':datetime.fromtimestamp(left,MOSCOW).strftime('%Y-%m-%d %H:%M:%S'),
                      'till':datetime.fromtimestamp(right,MOSCOW).strftime('%Y-%m-%d %H:%M:%S'),
                      'interval':1,'start':0,'iss.meta':'off','iss.only':'candles'}
            try:
                page = fetch_page(params, max(.1, deadline-monotonic()))
                fetched += 1
                for item in page:
                    b = moex_minute(item)
                    if b and left <= b['ts'] <= right and b['ts']+60 <= now:
                        rows[b['ts']] = b
            except Exception as exc:
                error = f'{type(exc).__name__}: {exc}'[:220]
                break
        right = left-60
        if len(rows) >= 2500 or right < floor:
            break
    minutes = [rows[t] for t in sorted(rows)][-5000:]
    bars = complete_five_minutes(minutes, now)
    closed = bars[-1]['ts']+300 if bars else None
    return {'minutes':minutes,'bars':bars,'pages':fetched,'error':error,
            'last_closed_at':closed,'history_age_seconds':now-closed if closed else None,
            'fresh':bool(closed and -5 <= now-closed <= 900)}
