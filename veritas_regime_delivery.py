"""Bounded diagnostic history, one reader and a last-good cache."""
from copy import deepcopy
import math
import threading
import time

LIMIT = 10000
_LOCK = threading.Lock()
_CACHE = {'value': None, 'at': 0., 'retry_at': 0., 'error': None}
QUERY = """WITH recent AS MATERIALIZED (
 SELECT id,asset,horizon,event_ts FROM ledger_events WHERE event_type='decision'
 ORDER BY event_ts DESC,id DESC LIMIT %s
) SELECT r.*,left(e.payload->>'regime',80) AS regime FROM recent r
JOIN ledger_events e ON e.id=r.id ORDER BY r.event_ts DESC,r.id DESC"""


def reduce_rows(rows, min_n, truncated=False):
    rows = sorted(rows, key=lambda r:(r['event_ts'],r['id']))
    grouped={}
    for r in rows:
        p={'regime':r.get('regime')}
        grouped.setdefault((r['asset'],r['horizon']),[]).append((r['event_ts'],str(p.get('regime') or 'UNKNOWN')))
    items=[]
    for (asset,h),seq in grouped.items():
        counts={}; outgoing={}
        for i in range(len(seq)-1):
            a,b=seq[i][1],seq[i+1][1]
            counts[(a,b)]=counts.get((a,b),0)+1; outgoing[a]=outgoing.get(a,0)+1
        current=seq[-1][1] if seq else None; nout=outgoing.get(current,0)
        dist=[]
        if current and nout:
            for (a,b),n in counts.items():
                if a==current: dist.append({'next_regime':b,'n':n,'probability':n/nout})
        dist.sort(key=lambda x:x['probability'],reverse=True)
        persistence=next((x['probability'] for x in dist if x['next_regime']==current),None)
        entropy=None
        if dist:
            entropy=-sum(x['probability']*math.log(max(x['probability'],1e-12)) for x in dist)
            entropy=entropy/math.log(len(dist)) if len(dist)>1 else 0.0
        recent=seq[-12:]; flips=sum(1 for i in range(1,len(recent)) if recent[i][1]!=recent[i-1][1])
        flip_rate=flips/max(1,len(recent)-1); ntrans=sum(counts.values())
        if ntrans<min_n: risk='BUILDING'
        elif (persistence is not None and persistence<0.60) or flip_rate>0.35 or (entropy is not None and entropy>0.70): risk='HIGH'
        elif (persistence is not None and persistence<0.78) or flip_rate>0.18: risk='MEDIUM'
        else: risk='LOW'
        items.append({'asset':asset,'horizon':h,'current_regime':current,
                      'transition_observations':ntrans,'persistence_probability':persistence,
                      'normalized_transition_entropy':entropy,'recent_flip_rate':flip_rate,
                      'transition_risk':risk,'next_regime_distribution':dist[:6]})
    return {'status':'ok','min_n':min_n,'items':items,'history_limit':LIMIT,
            'history_truncated':truncated,'coverage':'LATEST_DECISIONS_BOUNDED',
            'note':'Empirical live-state transitions; not a structural Markov forecast.'}



def snapshot(connect, min_n):
    def cached(status=None, error=None):
        out = deepcopy(_CACHE['value']) if _CACHE['value'] else {'status':'WARMING_UP','items':[]}
        if status: out['status'] = status
        if error: out['error_code'] = error
        out['snapshot_age_seconds'] = round(time.monotonic()-_CACHE['at'], 1) if _CACHE['value'] else None
        return out
    if _CACHE['value'] and time.monotonic()-_CACHE['at'] < 60:
        return cached()
    if time.monotonic() < _CACHE['retry_at']:
        return cached('STALE' if _CACHE['value'] else 'UNAVAILABLE', _CACHE['error'])
    if not _LOCK.acquire(blocking=False):
        return cached('STALE' if _CACHE['value'] else 'WARMING_UP')
    try:
        if _CACHE['value'] and time.monotonic()-_CACHE['at'] < 60:
            return cached()
        with connect() as c, c.transaction():
            c.execute("SET LOCAL statement_timeout = '2000ms'")
            c.execute("SET LOCAL lock_timeout = '250ms'")
            rows = c.execute(QUERY, (LIMIT+1,)).fetchall()
        result = reduce_rows(rows[:LIMIT], min_n, len(rows)>LIMIT)
        _CACHE.update(value=result, at=time.monotonic(), retry_at=0., error=None)
        return cached()
    except Exception as exc:
        _CACHE.update(retry_at=time.monotonic()+10, error=type(exc).__name__)
        return cached('STALE' if _CACHE['value'] else 'UNAVAILABLE', type(exc).__name__)
    finally:
        _LOCK.release()
