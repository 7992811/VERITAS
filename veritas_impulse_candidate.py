"""Fixed R73 research hypotheses. Not connected to the order executor.

Compression: first closed minute beyond a pre-existing 30-minute range.
Retest: first closed-minute rejection of the original one-hour breakout level.
Neither rule requires a second five-minute close or moves an old trigger.
"""
from statistics import median


def early_event(bars, minutes, asset, now):
    # On a five-minute boundary the triggering minute is part of the newly
    # closed 5m candle. Its extremes cannot define its own breakout level.
    bars=[b for b in bars if b['ts']+300<=now-60]
    minutes=[b for b in minutes if b['ts']+60<=now][-21:]
    if len(bars)<36 or len(minutes)<21:
        return None
    if any(b['ts']-a['ts']!=300 for a,b in zip(bars[-24:-1],bars[-23:])):
        return None
    obs,prev=minutes[-1],minutes[-2]
    if obs['ts']+60!=now or obs['ts']-prev['ts']!=60:
        return None
    atr=sum(max(b['high']-b['low'],abs(b['high']-a['close']),abs(b['low']-a['close']))
            for a,b in zip(bars[-21:-1],bars[-20:]))/20
    recent=bars[-6:];width=max(b['high'] for b in recent)-min(b['low'] for b in recent)
    broad=max(b['high'] for b in bars[-24:])-min(b['low'] for b in bars[-24:])
    if not atr or width>3*atr or not broad or width>.65*broad:
        return None
    volume=median(b.get('volume',0) for b in minutes[-21:-1])
    span=obs['high']-obs['low']
    if not volume or not span or obs.get('volume',0)<1.5*volume:
        return None
    for d,direction in ((1,'LONG'),(-1,'SHORT')):
        level=max(b['high'] for b in recent) if d==1 else min(b['low'] for b in recent)
        extension=d*(obs['close']-level)/atr
        if not (.05<extension<=.5 and d*(prev['close']-level)<=.05*atr
                and d*(obs['close']-obs['open'])>=.5*span):
            continue
        origin=min(b['low'] for b in minutes[-4:-1]) if d==1 else max(b['high'] for b in minutes[-4:-1])
        return dict(direction=direction,trigger_level=level,atr=atr,stop_price=origin-d*.15*atr,
            signal_at=now,signal_price=obs['close'],bars_since_signal=0,activity_confirmed=True,
            confirmation='1m_CLOSE',event_type='EARLY_COMPRESSION',relative_volume=obs['volume']/volume,
            event_id=f'R73_COMPRESSION|{asset}|{direction}|{level:.10f}|{now}')
    return None


def first_retest(bars, minutes, asset, now, pending):
    bars=[b for b in bars if b['ts']+300<=now-60]
    minutes=[b for b in minutes if b['ts']+60<=now][-21:]
    if len(bars)<36 or len(minutes)<21:
        return None,pending
    obs,prev=minutes[-1],minutes[-2]
    if obs['ts']+60!=now or obs['ts']-prev['ts']!=60:
        return None,None
    if pending:
        p=dict(pending);d=1 if p['direction']=='LONG' else -1;a=p['atr'];level=p['trigger_level']
        if now-p['signal_at']>600 or d*(obs['close']-p['stop_price'])<=0:
            pending=None
        elif now>p['signal_at']:
            touched=obs['low']<=level+.15*a if d==1 else obs['high']>=level-.15*a
            held=.05<d*(obs['close']-level)/a<=.5 and d*(obs['close']-obs['open'])>0
            if touched and held:
                return dict(p,retest_at=now,retest_confirmed=True,event_type='FIRST_RETEST'),None
            return None,pending
    atr=sum(max(b['high']-b['low'],abs(b['high']-a['close']),abs(b['low']-a['close']))
            for a,b in zip(bars[-21:-1],bars[-20:]))/20
    span=obs['high']-obs['low'];volume=median(b.get('volume',0) for b in minutes[-21:-1])
    if not atr or not span or not volume or obs.get('volume',0)<1.2*volume:
        return None,pending
    for d,direction in ((1,'LONG'),(-1,'SHORT')):
        level=max(b['high'] for b in bars[-12:]) if d==1 else min(b['low'] for b in bars[-12:])
        if (d*(obs['close']-level)>.05*atr and d*(prev['close']-level)<=.05*atr
                and d*(obs['close']-obs['open'])>=.5*span):
            origin=min(b['low'] for b in minutes[-4:-1]) if d==1 else max(b['high'] for b in minutes[-4:-1])
            pending=dict(direction=direction,trigger_level=level,atr=atr,stop_price=origin-d*.15*atr,
                signal_at=now,signal_price=obs['close'],bars_since_signal=0,activity_confirmed=True,
                confirmation='1m_CLOSE',relative_volume=obs['volume']/volume,
                event_id=f'R73_RETEST|{asset}|{direction}|{level:.10f}|{now}')
            break
    return None,pending
