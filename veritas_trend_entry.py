"""Closed-bar trend events and execution geometry, shared by paper and replay.

No network, database, broker calls or fitted probabilities. Times are UTC epoch
seconds; candles carry their opening time. Partial buckets never become levels.
"""
from datetime import datetime, timezone
import hashlib
import math
from statistics import median

VERSION = 'R67_LOCAL_LEVEL_BREAKOUT'


def number(value, default=None):
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def timestamp(value):
    if isinstance(value, (float, int)):
        return number(value)
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.timestamp() if dt.tzinfo else None
    except (TypeError, ValueError):
        return None


def closed_bars(bars, now):
    end = timestamp(now)
    out = {}
    for row in bars or []:
        t = number(row.get('ts'))
        vals = {k: number(row.get(k)) for k in ('open', 'high', 'low', 'close')}
        if (t is None or end is None or t % 300 or t + 300 > end
                or row.get('source') in ('PROXY_TIMING_BRIDGE','DIRECT_QUOTE_ANCHOR')
                or any(v is None or v <= 0 for v in vals.values())):
            continue
        if vals['low'] > min(vals['open'], vals['close']) or vals['high'] < max(vals['open'], vals['close']):
            continue
        out[t] = dict(vals, ts=t, volume=max(0., number(row.get('volume'), 0.)))
    return [out[t] for t in sorted(out)][-500:]


def aggregate(bars, seconds):
    groups = {}
    for b in bars:
        key = int(b['ts']) // seconds * seconds
        groups.setdefault(key, []).append(b)
    out = []
    for key, group in sorted(groups.items()):
        expected = list(range(key, key + seconds, 300))
        if [int(b['ts']) for b in group] != expected:
            continue
        out.append({'ts':key, 'available_at':key+seconds, 'open':group[0]['open'],
                    'high':max(b['high'] for b in group), 'low':min(b['low'] for b in group),
                    'close':group[-1]['close'], 'volume':sum(b['volume'] for b in group)})
    return out


def confirmed_levels(bars, timeframe):
    out = []
    for i in range(2, len(bars)-2):
        window = bars[i-2:i+3]
        b = bars[i]
        for kind, field, fn in (('resistance','high',max), ('support','low',min)):
            if b[field] == fn(x[field] for x in window):
                out.append({'price':b[field], 'kind':kind, 'timeframe':timeframe,
                            'pivot_at':b['ts'], 'available_at':bars[i+2]['available_at']})
    return out[-24:]


def build_context(bars, now, asset='', minute_bars=None, quote=None):
    if asset=='NQ' and minute_bars:
        from veritas_local_breakout import backfill_five_minutes
        bars=backfill_five_minutes(bars,minute_bars,now)
    bars = closed_bars(bars, now)
    result = {'version':VERSION, 'status':'INSUFFICIENT', 'event':None,
              'closed_at':bars[-1]['ts']+300 if bars else None, 'levels':[], 'bars':len(bars),
              'asset':asset, 'local_breakout_required':asset=='NQ'}
    if len(bars) < 36 or any(bars[i]['ts']-bars[i-1]['ts'] != 300 for i in range(len(bars)-24, len(bars))):
        return result
    trs = [max(b['high']-b['low'], abs(b['high']-bars[i-1]['close']),
               abs(b['low']-bars[i-1]['close'])) if i else b['high']-b['low'] for i,b in enumerate(bars)]
    atr = sum(trs[-20:])/20
    if atr <= 0:
        return result
    h1, h4, m15 = aggregate(bars,3600), aggregate(bars,14400), aggregate(bars,900)
    levels = confirmed_levels(h1,'1h') + confirmed_levels(h4,'4h')
    local = [dict(b,available_at=b['ts']+300) for b in bars]
    pivots = confirmed_levels(local,'5m')
    result.update(status='OK', atr=atr, last_close=bars[-1]['close'],
                  last_two_closes=[b['close'] for b in bars[-2:]], levels=levels,
                  local_support=next((x['price'] for x in reversed(pivots) if x['kind']=='support'),None),
                  local_resistance=next((x['price'] for x in reversed(pivots) if x['kind']=='resistance'),None),
                  confirmation_15m=m15[-1] if m15 else None)
    if asset == 'NQ':
        from veritas_local_breakout import enrich
        return enrich(result,bars,now,minute_bars,quote)
    # Keep the originating breakout through its confirmation, never reset it
    # to the current quote on a later poll of the same event.
    for i in range(max(24,len(bars)-12),len(bars)):
        b=bars[i]; a=sum(trs[i-19:i+1])/20; prior=bars[i-12:i]
        vol=median(x['volume'] for x in bars[i-20:i])
        span=b['high']-b['low']
        if a<=0 or vol<=0 or not .8*a<=span<=2.8*a or b['volume']/vol<1.2:
            continue
        for d,side in ((1,'LONG'),(-1,'SHORT')):
            level=max(x['high'] for x in prior) if d==1 else min(x['low'] for x in prior)
            close_position=(b['close']-b['low'])/span if d==1 else (b['high']-b['close'])/span
            if not (.05*a < d*(b['close']-level) <= .8*a and d*(b['close']-b['open'])>=.35*span and close_position>=.7):
                continue
            tail=bars[i+1:]
            failed=any(d*(x['close']-level)<-.15*a and d*(y['close']-level)<-.15*a for x,y in zip(tail,tail[1:]))
            if failed or d*(bars[-1]['close']-level)<=0:
                continue
            anchor=min(x['low'] for x in bars[max(0,i-5):i+1]) if d==1 else max(x['high'] for x in bars[max(0,i-5):i+1])
            stop=anchor-d*.15*a
            stop=min(stop,b['close']-1.2*a) if d==1 else max(stop,b['close']+1.2*a)
            retests=[x for x in tail if ((x['low']<=level+.25*a) if d==1 else (x['high']>=level-.25*a))
                     and d*(x['close']-level)>.2*a and d*(x['close']-x['open'])>0]
            last=bars[-1]
            continuation=bool(tail and d*(last['close']-b['close'])>=a and d*(last['close']-last['open'])>0
                              and last['volume']>=1.2*median(x['volume'] for x in bars[-21:-1]))
            event={'direction':side,'trigger_level':level,'signal_at':b['ts']+300,
                   'signal_price':b['close'],'stop_price':stop,'atr':a,'bars_since_signal':len(tail),
                   'relative_volume':b['volume']/vol,'retest_confirmed':bool(retests),
                   'retest_at':retests[-1]['ts']+300 if retests else None,
                   'continuation_confirmed':continuation,
                   'confirmed_at':last['ts']+300 if continuation else (retests[-1]['ts']+300 if retests else b['ts']+300),
                   'held_bars':sum(d*(x['close']-level)>0 for x in tail)}
            event['event_id']='R66_'+hashlib.sha256(f'{asset}|5m|{side}|{event["signal_at"]}|{level:.10f}'.encode()).hexdigest()[:20]
            result['event']=event
            return result
    return result


def context_of(row):
    return (row or {}).get('trend_entry_context') or ((row or {}).get('trade_plan') or {}).get('trend_entry_context') or {}


def has_geometry_context(row):
    plan=(row or {}).get('trade_plan') or {}
    return bool(context_of(row).get('status')=='OK' or
                (plan.get('multi_tf_level_context') or {}).get('target_ladder') or plan.get('target_ladder'))


def geometry(row, price=None, direction=None, stop_override=None):
    """First unpassed HTF barrier, not the first distant profitable target."""
    row=row or {}; plan=dict(row.get('trade_plan') or {})
    px=number(price,number(row.get('price'))); direction=direction or row.get('research_decision')
    d=1 if direction=='LONG' else -1
    ctx=context_of(row); event=ctx.get('event') or {}
    out={'version':VERSION,'eligible':False,'reason':'R66_INVALID_GEOMETRY'}
    if direction not in ('LONG','SHORT') or not px or px<=0:
        return out
    stop=number(stop_override) if stop_override is not None else number(plan.get('stop_price'))
    if (stop_override is None and (row.get('horizon')=='5m' or ctx.get('local_breakout_required'))
            and event.get('direction')==direction):
        stop=number(event.get('stop_price'),stop)
    if not stop or stop<=0 or d*(px-stop)<=0:
        return out
    levels=[]
    for x in ctx.get('levels') or []:
        if x.get('kind')==('resistance' if d==1 else 'support'):
            levels.append(x)
    # Retain legacy level context for 1h/4h plans as well as cold snapshots.
    level_context=plan.get('multi_tf_level_context') or {}
    nearest_ref=level_context.get('resistance' if d==1 else 'support') or {}
    if nearest_ref.get('timeframe') in ('1h','4h','1d','3d','7d'):
        levels.append(nearest_ref)
    for x in level_context.get('target_ladder') or plan.get('target_ladder') or []:
        if x.get('timeframe') in ('1h','4h','1d','3d','7d'):
            levels.append(x)
    candidates=[(d*(number(x.get('price'),0)-px),x) for x in levels if number(x.get('price'),0)>0]
    ahead=[v for v in candidates if v[0]>max(1e-10,px*.00001)]
    ahead.sort(key=lambda z:z[0])
    planned=number(plan.get('target_price') or plan.get('tactical_target_price'))
    target=planned if planned and d*(planned-px)>0 else None
    nearest=ahead[0][1] if ahead else None
    if nearest and (target is None or d*(float(nearest['price'])-px)<d*(target-px)):
        target=float(nearest['price'])
    if target is None:
        return out
    risk=d*(px-stop)/px; room=d*(target-px)/px
    out.update(eligible=True,reason='R66_GEOMETRY_OK',stop_price=stop,target_price=target,
               remaining_move_pct=room,stop_distance_pct=risk,nearest_level=nearest,
               reward_risk=room/risk,event_id=event.get('event_id'),
               runner_target_price=planned,atr=ctx.get('atr'))
    origin=number(event.get('signal_price'))
    closes=ctx.get('last_two_closes') or []
    if origin and event.get('direction')==direction and len(closes)==2:
        crossed=[x for _,x in candidates if d*(float(x['price'])-origin)>0 and d*(px-float(x['price']))>=0
                 and not all(d*(v-float(x['price']))>0 for v in closes)]
        if crossed:
            out.update(eligible=False,reason='R66_SENIOR_BREAK_NOT_HELD')
    return out


def prepare_row(row, price=None):
    x=dict(row or {}); plan=dict(x.get('trade_plan') or {})
    if not has_geometry_context(x):return x
    g=geometry(x,price)
    if g.get('stop_price'):
        forecast=number(plan.get('expected_move_pct'))
        expected=min(forecast,g['remaining_move_pct']) if forecast is not None and forecast>=0 else g['remaining_move_pct']
        plan.update(stop_price=g['stop_price'],stop_distance_pct=g['stop_distance_pct'],
                    target_price=g['target_price'],expected_move_pct=expected,
                    expected_to_stop_ratio=g['reward_risk'],r66_geometry=g,
                    r66_runner_target_price=g.get('runner_target_price'))
    x['trade_plan']=plan
    return x


def event_gate(row, price, direction, now=None):
    ctx=context_of(row); event=ctx.get('event') or {}
    required = row.get('asset')=='NQ' or ctx.get('local_breakout_required')
    sources=(row.get('_execution_quote') or {}).get('source_names') or row.get('source_names') or row.get('market_source_names') or {}
    if required and 'proxy' in str(sources.get('primary') or row.get('verification_mode') or '').lower():
        return {'eligible':False,'reason':'R67_DIRECT_NQ_QUOTE_REQUIRED'}
    if required and (ctx.get('status')!='OK' or not event):
        return {'eligible':False,'reason':'R67_LOCAL_CONTEXT_REQUIRED' if ctx.get('status')!='OK' else 'R67_WAIT_LOCAL_BREAKOUT'}
    if not event:
        return {'eligible':True,'reason':'R66_LEGACY_SIGNAL_PATH'}
    t=timestamp(now); closed=number(ctx.get('closed_at'))
    if event.get('direction')!=direction:
        return {'eligible':False,'reason':'R66_LOCAL_EVENT_OPPOSED'}
    if t and (closed is None or not -5<=t-closed<=900):
        return {'eligible':False,'reason':'R66_CLOSED_CONTEXT_STALE'}
    a=number(event.get('atr'),0); level=number(event.get('trigger_level'),0); px=number(price,0)
    d=1 if direction=='LONG' else -1
    extension=d*(px-level)/a if a>0 else math.inf
    recent_retest=bool(event.get('retest_at') and closed and closed-event['retest_at']<=600)
    timely=event.get('bars_since_signal',99)<=2 or recent_retest
    ok=bool(timely and -.15<=extension<=(1.5 if required else 1.2))
    return {'eligible':ok,'reason':'R66_EVENT_READY' if ok else 'R66_WAIT_RETEST',
            'event_id':event.get('event_id'),'extension_atr':extension,'recent_retest':recent_retest,
            'trigger_level':level,'stop_price':event.get('stop_price'), 'signal_at':event.get('signal_at')}


def scale_decision(position,row,price,requested,nav,cost=.002,risk_cap=.01):
    p=position.get('payload') or {}; d=1 if position.get('direction')=='LONG' else -1
    px=number(price,0); entry=number(position.get('avg_entry_price'),0); units=number(position.get('units'),0)
    cur=units*px/max(nav,1); ctx=context_of(row); ev=ctx.get('event') or {}
    out={'eligible':False,'reason':'R66_ADD_NEEDS_CONFIRMATION','fraction':cur}
    if px<=0 or entry<=0 or d*(px-entry)/entry<=cost or p.get('r17_tp1_done'):
        return out
    g=geometry(row,px,position.get('direction'),position.get('stop_price'))
    if not g.get('eligible') or g['remaining_move_pct']-cost<1.2*(g['stop_distance_pct']+cost):
        return dict(out,reason='R66_ADD_INSUFFICIENT_ROOM')
    last=timestamp(p.get('r66_last_confirmation_at') or p.get('r55_last_scale_at') or position.get('opened_at'))
    confirmed=number(ev.get('confirmed_at'))
    same=ev.get('direction')==position.get('direction')
    if not (same and confirmed and (last is None or confirmed>last)
            and (ev.get('retest_confirmed') or ev.get('continuation_confirmed'))):
        return out
    initial=number(p.get('r66_initial_fraction') or p.get('opening_fraction'),cur)
    # At most half the original allocation per distinct confirmation. Large
    # portfolios retain their configured leverage ceiling outside this function.
    step=max(.05, math.floor(initial*.5/.05+1e-9)*.05)
    budget=max(0.,risk_cap-units*(max(0.,d*(entry-position['stop_price']))+px*cost)/max(nav,1))
    add_cap=budget/max(g['stop_distance_pct']+cost,1e-9)
    target=min(requested,cur+step,cur+add_cap)
    target=math.floor((target+1e-9)/.05)*.05
    out.update(eligible=target>cur+.025,reason='R66_ADD_CONFIRMED' if target>cur+.025 else 'R66_ADD_RISK_LIMIT',
               fraction=target,confirmation_at=confirmed,event_id=ev.get('event_id'),geometry=g)
    return out


def trailing_stop(position,row,price,cost=.002):
    ctx=context_of(row); d=1 if position.get('direction')=='LONG' else -1
    px=number(price,0); entry=number(position.get('avg_entry_price'),0); old=number(position.get('stop_price'))
    a=number(ctx.get('atr'),0); anchor=number(ctx.get('local_support' if d==1 else 'local_resistance'))
    if not old or not anchor or min(px,entry,a)<=0:
        return None
    initial=number((position.get('payload') or {}).get('initial_stop_price'),old)
    if d*(px-entry)<max(abs(entry-initial),2.5*cost*entry):
        return None
    stop=anchor-d*.15*a
    if d*(stop-old)>0 and d*(px-stop)>.3*a:
        return stop
    return None
