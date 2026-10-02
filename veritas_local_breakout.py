"""Causal NQ range breakout. Structure and execution quote are separate inputs.

Levels exist before the crossing. Oversized candles still create an event so
that a missed impulse cannot later be relabelled as a fresh breakout.
"""
import hashlib


def closed_minutes(minute_bars, now):
    from veritas_trend_entry import number, timestamp
    end=timestamp(now);minutes={}
    for b in minute_bars or []:
        t=number(b.get('ts'))
        values=[number(b.get(k)) for k in ('open','high','low','close')]
        if (t is None or end is None or t % 60 or t+60 > end or any(x is None or x<=0 for x in values)
                or b.get('source') in ('PROXY_TIMING_BRIDGE','DIRECT_QUOTE_ANCHOR')):
            continue
        op,hi,lo,cl=values
        if lo>min(op,cl) or hi<max(op,cl) or lo>hi:
            continue
        minutes[t]=dict(b,ts=t,available_at=t+60)
    return minutes


def backfill_five_minutes(bars, minute_bars, now):
    """Fresh completed minute buckets can outpace the cached 5m request."""
    from veritas_trend_entry import closed_bars
    native={b['ts']:b for b in closed_bars(bars,now)}
    minutes=closed_minutes(minute_bars,now)
    for start in sorted({int(t)//300*300 for t in minutes}):
        times=list(range(start,start+300,60))
        if start in native or not all(t in minutes for t in times):
            continue
        group=[minutes[t] for t in times]
        native[start]={'ts':start,'open':group[0]['open'],'close':group[-1]['close'],
                       'high':max(b['high'] for b in group),'low':min(b['low'] for b in group),
                       'volume':sum(float(b.get('volume') or 0) for b in group),
                       'source':'DIRECT_1M_AGGREGATION'}
    return [native[t] for t in sorted(native)]


def _arm(prior, direction):
    if len(prior) < 36:
        return None
    side = 1 if direction == 'LONG' else -1
    atr = sum(max(b['high']-b['low'], abs(b['high']-a['close']),
                  abs(b['low']-a['close'])) for a,b in zip(prior[-21:-1],prior[-20:]))/20
    if atr <= 0:
        return None
    field, extreme = ('high', max) if side == 1 else ('low', min)
    level = extreme(b[field] for b in prior)
    touches = []
    for b in prior:
        if abs(b[field]-level) <= .5*atr and (not touches or b['ts']-touches[-1] >= 600):
            touches.append(b['ts'])
    if len(touches) < 2 or side*(prior[-1]['close']-level) > .05*atr:
        return None
    anchor = min(b['low'] for b in prior[-12:]) if side == 1 else max(b['high'] for b in prior[-12:])
    identity = f'NQ|{direction}|{level:.8f}|{touches[0]}'
    return {'direction':direction, 'trigger_level':level, 'atr':atr,
            'stop_price':anchor-side*.15*atr, 'impulse_origin':anchor,
            'level_available_at':prior[-1]['ts']+300, 'touches':len(touches),
            'event_id':'R67_'+hashlib.sha256(identity.encode()).hexdigest()[:20]}


def enrich(context, bars, now, minute_bars=None, quote=None):
    from veritas_trend_entry import number, timestamp
    result = dict(context, event=None, local_breakout_required=True,
                  structure_source='DIRECT_NQ_CLOSED_BARS', armed_levels=[])
    if context.get('status') != 'OK':
        return result
    end = timestamp(now)
    minutes = closed_minutes(minute_bars,now)
    # Include the still-open 5m bucket: a completed 1m candle can trigger it.
    points = [dict(b, available_at=b['ts']+300) for b in bars]
    if bars and bars[-1]['ts']+300 < end:
        points.append({'ts':bars[-1]['ts']+300})
    active = None
    start = max(36,len(bars)-48)
    for i in range(start,len(points)):
        prior = bars[max(0,i-72):i]
        if len(prior)<36 or any(b['ts']-a['ts'] != 300 for a,b in zip(prior[-24:-1],prior[-23:])):
            continue
        point = points[i]
        arms = [_arm(prior,d) for d in ('LONG','SHORT')]
        arms = [a for a in arms if a]
        observations = [minutes[t] for t in sorted(minutes) if point['ts'] <= t < point['ts']+300]
        if point.get('close') and (not observations or observations[-1]['available_at']<point['available_at']):
            observations.append(point)
        for obs in observations:
            if active:
                side=1 if active['direction']=='LONG' else -1
                if side*(obs['close']-active['trigger_level']) < -.15*active['atr']:
                    active['_failed'] = active.get('_failed',0)+1
                else:
                    active['_failed']=0
                if active['_failed'] >= 2:
                    active=None
            if active:
                continue
            for arm in arms:
                side=1 if arm['direction']=='LONG' else -1
                # Do not filter out a large crossing candle; the entry gate
                # rejects its overshoot while retaining its original identity.
                if (side*(obs['close']-arm['trigger_level']) > .05*arm['atr']
                        and side*(obs['close']-obs['open']) > 0):
                    active=dict(arm,signal_at=obs['available_at'],signal_price=obs['close'],
                                confirmation='1m_CLOSE' if obs['ts'] in minutes else '5m_CLOSE',
                                event_type='LOCAL_RANGE_BREAKOUT')
                    break
    result['armed_levels']=[a for a in (_arm(bars[-72:],d) for d in ('LONG','SHORT')) if a]
    q=quote or {}; qp=number(q.get('price')); qt=timestamp(q.get('observed_at'))
    # A fresh direct paper quote may cross a level prepared on earlier bars.
    # A QQQ-derived price is not an observed NQ crossing.
    if not active and qp and qt and q.get('direct') and -5 <= end-qt <= 120:
        for arm in result['armed_levels']:
            side=1 if arm['direction']=='LONG' else -1
            if (arm['level_available_at'] <= qt and qt-arm['level_available_at'] <= 900
                    and .1 <= side*(qp-arm['trigger_level'])/arm['atr'] <= .8):
                active=dict(arm,signal_at=qt,signal_price=qp,confirmation='LIVE_QUOTE_PROVISIONAL',
                            event_type='LOCAL_RANGE_BREAKOUT')
                break
    if active:
        side=1 if active['direction']=='LONG' else -1
        tail=[b for b in bars if b['ts'] >= active['signal_at']]
        a=active['atr'];level=active['trigger_level']
        retests=[b for b in tail if side*(b['close']-level) > .2*a
                 and (b['low']<=level+.25*a if side==1 else b['high']>=level-.25*a)
                 and side*(b['close']-b['open'])>0]
        continuation=bool(tail and side*(tail[-1]['close']-active['signal_price']) >= a
                          and side*(tail[-1]['close']-tail[-1]['open'])>0)
        active.update(bars_since_signal=max(0,int((end-active['signal_at'])//300)),
                      retest_confirmed=bool(retests),retest_at=retests[-1]['ts']+300 if retests else None,
                      continuation_confirmed=continuation,
                      confirmed_at=tail[-1]['ts']+300 if continuation else
                          (retests[-1]['ts']+300 if retests else active['signal_at']),
                      held_bars=sum(side*(b['close']-level)>0 for b in tail))
        active.pop('_failed',None)
        result['event']=active
    return result
