"""R69 causal minute trigger against a previously closed five-minute range."""
from statistics import median
import hashlib
import math

VERSION='R69_MINUTE_STRUCTURAL_ENTRY'


def enrich(context, bars, now, minute_bars=None, quote=None):
    from veritas_trend_entry import timestamp
    from veritas_local_breakout import closed_minutes
    end=timestamp(now)
    out=dict(context,event=None,local_breakout_required=True,structure_source='CLOSED_5M_RANGE_1M_TRIGGER',
             minute_closed_at=None,minute_status='UNAVAILABLE',armed_levels=[],failed_breakouts=0)
    minutes=closed_minutes(minute_bars,now)
    if minutes:
        out['minute_closed_at']=max(minutes)+60
        out['minute_status']='OK' if -5<=end-out['minute_closed_at']<=90 else 'STALE'
    if context.get('status')!='OK':return out
    zones=[];active=None
    for i in range(max(36,len(bars)-72),len(bars)+1):
        prior=bars[:i]
        if len(prior)<36 or any(b['ts']-a['ts']!=300 for a,b in zip(prior[-24:-1],prior[-23:])):continue
        window=prior[-12:];closed=prior[-1]['ts']+300
        atr=sum(max(b['high']-b['low'],abs(b['high']-a['close']),abs(b['low']-a['close'])) for a,b in zip(prior[-21:-1],prior[-20:]))/20
        if atr<=0:continue
        arms=[]
        for d,direction in ((1,'LONG'),(-1,'SHORT')):
            level=max(b['high'] for b in window) if d==1 else min(b['low'] for b in window)
            touches=[b['ts'] for b in window if abs(b['high' if d==1 else 'low']-level)<=.25*atr]
            if len(touches)<2 or touches[-1]-touches[0]<300:continue
            zone=next((z for z in zones if z['direction']==direction and abs(z['trigger_level']-level)<=.5*z['atr']),None)
            if zone is None:
                exhausted=[z for z in zones if z['direction']==direction and z['failures']>=2]
                if exhausted:
                    last=max(z.get('last_failure_at',0) for z in exhausted)
                    # Require a genuinely new closed range, not just a shifted high.
                    if window[0]['ts']<last:continue
                anchor=min(b['low'] for b in window[-6:]) if d==1 else max(b['high'] for b in window[-6:])
                identity=f'{context.get("asset")}|{direction}|{level:.10f}|{touches[0]}'
                zone=dict(direction=direction,trigger_level=level,atr=atr,stop_price=anchor-d*.15*atr,
                          impulse_origin=anchor,level_available_at=closed,zone_started_at=touches[0],
                          event_id='R69_'+hashlib.sha256(identity.encode()).hexdigest()[:20],failures=0,
                          activity_basis=window[-1].get('activity_basis','volume'))
                zones.append(zone)
            arms.append(zone)
        if i==len(bars):out['armed_levels']=[dict(z) for z in arms]
        observations=[minutes[t] for t in sorted(minutes) if closed<=t<closed+300]
        if i<len(bars) and (not observations or observations[-1]['available_at']<closed+300):
            observations.append(dict(bars[i],available_at=closed+300,resolution=300))
        for obs in observations:
            if obs['available_at']>end:continue
            resolution=obs.get('resolution',60)
            if active:
                d=1 if active['direction']=='LONG' else -1
                failed=d*(obs['close']-active['trigger_level'])<-.15*active['atr']
                active['_below']=active.get('_below',0)+1 if failed else 0
                if active['_below']>=2:
                    active['_zone']['failures']+=1
                    active['_zone']['last_failure_at']=obs['available_at'];active=None
                elif obs['available_at']>active['signal_at']:
                    touch=obs['low']<=active['trigger_level']+.15*active['atr'] if d==1 else obs['high']>=active['trigger_level']-.15*active['atr']
                    if touch and d*(obs['close']-active['trigger_level'])>.1*active['atr'] and d*(obs['close']-obs['open'])>0:
                        active['retest_at']=obs['available_at'];active['retest_confirmed']=True
                    if d*(obs['close']-active['signal_price'])>=active['atr']:
                        active['continuation_confirmed']=True;active['confirmed_at']=obs['available_at']
            if active:continue
            history=([minutes[t]['volume'] for t in sorted(minutes) if t+60<=obs['ts']][-20:] if resolution==60 else [b['volume'] for b in prior[-20:]])
            baseline=median(history) if len(history)>=20 else 0
            relative=obs.get('volume',0)/baseline if baseline>0 else 0
            span=obs['high']-obs['low']
            for z in arms:
                d=1 if z['direction']=='LONG' else -1
                # The crossing is recorded even if overextended: later polls
                # cannot rename a spent impulse as a new opportunity.
                if z['failures']>=2 or d*(obs['close']-z['trigger_level'])<=.05*z['atr']:continue
                previous=minutes.get(obs['ts']-60) if resolution==60 else prior[-1]
                if not previous or d*(previous['close']-z['trigger_level'])>.05*z['atr']:continue
                body=d*(obs['close']-obs['open'])/span if span>0 else 0
                quality=relative>=1.2 and body>=.5
                event_identity=f'{context.get("asset")}|{z["direction"]}|{z["trigger_level"]:.10f}|{obs["available_at"]}'
                active=dict(z,signal_at=obs['available_at'],signal_price=obs['close'],
                    confirmation='1m_CLOSE' if resolution==60 else '5m_CLOSE',event_type='LOCAL_RANGE_BREAKOUT',
                    relative_volume=relative,activity_confirmed=quality,confirmed_at=obs['available_at'],
                    continuation_confirmed=False,retest_confirmed=False,retest_at=None,_zone=z)
                active['event_id']='R69_'+hashlib.sha256(event_identity.encode()).hexdigest()[:20]
                break
    out['failed_breakouts']=max((z['failures'] for z in zones),default=0)
    if active:
        active.pop('_zone',None);active.pop('_below',None)
        active['bars_since_signal']=max(0,int((end-active['signal_at'])//300))
        active['age_seconds']=end-active['signal_at'];out['event']=active
    return out


def minute_features(base,raw,now):
    from veritas_local_breakout import closed_minutes
    from veritas_trend_entry import timestamp
    f=dict(base);rows=list(closed_minutes(raw.get('structure_minute_bars'),now).values());rows.sort(key=lambda b:b['ts'])
    age=timestamp(now)-(rows[-1]['ts']+60) if rows else None
    ready=len(rows)>=36 and age is not None and -5<=age<=90 and all(b['ts']-a['ts']==60 for a,b in zip(rows[-21:-1],rows[-20:]))
    f.update(horizon='1m',minute_data_status='OK' if ready else 'STALE_OR_INCOMPLETE',
             minute_closed_at=rows[-1]['ts']+60 if rows else None)
    if ready:
        c=[b['close'] for b in rows];rr=[b/a-1 for a,b in zip(c[-21:-1],c[-20:])]
        f.update(ret_h=c[-1]/c[-2]-1,momentum=c[-1]/c[-6]-1,
                 rv=math.sqrt(sum(r*r for r in rr)/len(rr)),
                 volume_ratio=rows[-1]['volume']/max(median(b['volume'] for b in rows[-21:-1]),1e-12))
    else:f.update(ret_h=0.,momentum=0.,rv=0.,volume_ratio=0.)
    return f


def structural_fraction(row,mode,cap):
    """Quality affects size; it is not presented as a win probability."""
    from veritas_trend_entry import context_of
    e=context_of(row).get('event') or {}
    if not e.get('activity_confirmed'):return 0.
    strong=float(e.get('relative_volume') or 0)>=1.8 and e.get('continuation_confirmed')
    size=(1. if strong else .5) if mode=='AGGRESSIVE' else .15 if mode=='IMPULSE_ONLY' else .10
    return max(0.,math.floor(min(size,cap)/.05+1e-9)*.05)


_MINUTE_CACHE={}

def attach_minutes(raw,symbol,namespace):
    """Observed minute bars only; no 5m interpolation and no quote anchors."""
    from datetime import datetime,timezone
    import time
    import httpx
    import veritas_market_history as MH
    r=dict(raw);asset=r.get('asset');now=time.time()
    if asset=='NQ':return r # native minutes already fetched with futures history
    try:
        if asset in ('BTC','ETH'):
            data=namespace['get_json']('https://data-api.binance.vision/api/v3/klines',{'symbol':symbol,'interval':'1m','limit':300})
            minutes=[dict(ts=float(b[0])/1000,open=float(b[1]),high=float(b[2]),low=float(b[3]),close=float(b[4]),volume=float(b[5])) for b in data]
        elif asset=='MOEX':
            minutes=list(namespace['_v90r16_moex5_cache'].get('minutes') or [])
        elif asset=='CNYRUBF' or (asset=='BRENT' and (r.get('contract') or {}).get('secid')):
            secid=(r.get('contract') or {}).get('secid') or 'CNYRUBF'
            url=f'https://iss.moex.com/iss/engines/futures/markets/forts/securities/{secid}/candles.json'
            key=(asset,secid)
            with httpx.Client(timeout=2.) as h:
                def fetch(params,remaining):
                    resp=h.get(url,params=params,timeout=min(2.,remaining));resp.raise_for_status()
                    block=resp.json().get('candles') or {}
                    return [dict(zip(block['columns'],b)) for b in block.get('data',[])]
                result=MH.recent_moex_minutes(fetch,now,_MINUTE_CACHE.get(key,()),budget_seconds=3.)
            minutes=result['minutes'];_MINUTE_CACHE[key]=minutes
        else:
            ticker={'GOLD':'GC%3DF','BRENT':'BZ%3DF'}.get(asset)
            minutes=namespace['_yahoo_series'](ticker,'1d','1m',True)[0] if ticker else []
        r['structure_minute_bars']=minutes
        r['minute_data_source']='DIRECT_OBSERVED'
    except Exception as exc:
        r['structure_minute_bars']=[]
        r['minute_data_error']=f'{type(exc).__name__}: {exc}'[:180]
    return r
