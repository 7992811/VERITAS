"""R75 fixed, causal research hypotheses. This module never places orders.

Minute triggers + closed-hour trend, tested separately from the live ranking
engine. Spot OHLC cannot validate futures fills. Conservative stop-first bars,
next-open execution, no favorable gap fills, all paid costs, marked drawdown.
"""
import argparse
import hashlib
import json
from pathlib import Path
from collections import Counter
import numpy as np
import pandas as pd

SPEC_PATH=Path(__file__).with_name('trend_research_spec.json')
SPEC=json.loads(SPEC_PATH.read_text())
YEAR=365.25*86400


def timestamp(value):
    return int(pd.Timestamp(value,tz='UTC').timestamp())


def load(path):
    raw=json.loads(Path(path).read_text())
    f=pd.DataFrame(raw['bars']).sort_values('ts').reset_index(drop=True)
    required=['ts','open','high','low','close','volume']
    if f.empty or not np.isfinite(f[required].to_numpy()).all():raise ValueError('Invalid or empty history')
    if not ((f.ts.diff().iloc[1:]==60).all() and (f.ts%60==0).all()):raise ValueError('Gaps/duplicate minute bars')
    if not ((f.low>0)&(f.low<=f[['open','close']].min(axis=1))&
            (f.high>=f[['open','close']].max(axis=1))&(f.volume>=0)).all():raise ValueError('Invalid OHLCV')
    return f


def resample(f,minutes):
    x=f.copy();x.index=pd.to_datetime(x.ts,unit='s',utc=True)
    g=x.resample(f'{minutes}min',closed='left',label='right')
    out=g.agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'})
    out=out[g.size()==minutes]
    out['available_at']=out.index.astype('int64')//10**9
    return out


def atr(f,n=20):
    prev=f.close.shift(1)
    return pd.concat([f.high-f.low,(f.high-prev).abs(),(f.low-prev).abs()],axis=1).max(axis=1).rolling(n).mean()


def extreme_time(f,field,n,maximum):
    offsets=f[field].rolling(n).apply(np.argmax if maximum else np.argmin,raw=True)
    # Equal extrema retain their first observation, not a newly renamed event.
    return (f.available_at-(n-1-offsets)*300).to_numpy()


def align(frame,lookup):
    return frame.reindex(pd.to_datetime(lookup,unit='s',utc=True),method='ffill').reset_index(drop=True)


def features(f):
    five=resample(f,5);hour=resample(f,60)
    five['atr']=atr(five)
    five['sma18']=five.close.rolling(18).mean()
    five['lo6']=five.low.rolling(6).min();five['hi6']=five.high.rolling(6).max()
    five['lo6_at']=extreme_time(five,'low',6,False);five['hi6_at']=extreme_time(five,'high',6,True)
    five['hi12']=five.high.rolling(12).max();five['lo12']=five.low.rolling(12).min()
    five['hi48']=five.high.rolling(48).max();five['lo48']=five.low.rolling(48).min()
    five['hi48_at']=extreme_time(five,'high',48,True);five['lo48_at']=extreme_time(five,'low',48,False)
    five['vbase']=five.volume.rolling(20).median().shift(1)
    for lag in (3,4,5,6):
        for key in ('open','high','low','close','volume','vbase','atr','available_at'):
            five[f'flag{lag}_{key}']=five[key].shift(lag)
        five[f'flag{lag}_hi']=five.high.rolling(lag).max()
        five[f'flag{lag}_lo']=five.low.rolling(lag).min()
    hour['sma18']=hour.close.rolling(18).mean();hour['sma50']=hour.close.rolling(50).mean()
    hour['slope18']=hour.sma18-hour.sma18.shift(3);hour['slope50']=hour.sma50-hour.sma50.shift(3)
    hour['direction']=np.select([
        (hour.close>hour.sma18)&(hour.sma18>hour.sma50)&(hour.slope18>0)&(hour.slope50>0),
        (hour.close<hour.sma18)&(hour.sma18<hour.sma50)&(hour.slope18<0)&(hour.slope50<0)], [1,-1], default=0)
    # Signal is the closed minute at ts+60. Its own high/low never defines its
    # breakout level, even when that minute completes a 5m/hour candle.
    lookup=f.ts.to_numpy()
    a=align(five,lookup);h=align(hour,lookup)
    x=f.copy();x['signal_at']=f.ts+60
    x['prior_high5']=f.high.rolling(5).max().shift(1)
    x['prior_low5']=f.low.rolling(5).min().shift(1)
    x['vbase']=f.volume.rolling(20).median().shift(1)
    x['direction']=h.direction.fillna(0).astype(int)
    x['hour_available_at']=h.available_at
    for col in a.columns:x['f_'+col]=a[col]
    # Closed 15-minute extrema for trailing, available at each signal close.
    fifteen=resample(f,15)
    fifteen['trail_long']=fifteen.low.rolling(2).min()
    fifteen['trail_short']=fifteen.high.rolling(2).max()
    trail=align(fifteen,(f.ts+60).to_numpy())
    x['trail_long']=trail.trail_long;x['trail_short']=trail.trail_short
    return x


def signals(x,family):
    result=[];span=x.high-x.low
    for d in (1,-1):
        common=(x.direction==d)&(x.vbase>0)&(x.volume>=1.2*x.vbase)&(d*(x.close-x.open)>=.5*span)&(span>0)&(x.f_atr>0)
        if family=='PULLBACK':
            level=x.prior_high5 if d>0 else x.prior_low5
            anchor=x.f_lo6 if d>0 else x.f_hi6
            episode=x.f_lo6_at if d>0 else x.f_hi6_at
            touched=x.f_lo6<=x.f_sma18 if d>0 else x.f_hi6>=x.f_sma18
            depth=x.f_hi12-x.f_lo6 if d>0 else x.f_hi6-x.f_lo12
            setup=touched&(d*(x.close-x.f_sma18)>0)&(depth>=.7*x.f_atr)&(depth<=4*x.f_atr)
            candidates=[(level,anchor,episode,setup)]
        elif family=='CHANNEL':
            level=x.f_hi48 if d>0 else x.f_lo48
            anchor=x.f_lo12 if d>0 else x.f_hi12
            episode=x.f_hi48_at if d>0 else x.f_lo48_at
            candidates=[(level,anchor,episode,pd.Series(True,index=x.index))]
        elif family=='FLAG':
            candidates=[]
            for lag in (3,4,5,6):
                prefix=f'f_flag{lag}_'
                level=x[prefix+'hi'] if d>0 else x[prefix+'lo']
                anchor=x[prefix+'lo'] if d>0 else x[prefix+'hi']
                midpoint=(x[prefix+'high']+x[prefix+'low'])/2
                held=anchor>=midpoint if d>0 else anchor<=midpoint
                setup=(d*(x[prefix+'close']-x[prefix+'open'])>=1.5*x[prefix+'atr'])&held&\
                    (x[prefix+'volume']>=1.5*x[prefix+'vbase'])&((x[prefix+'hi']-x[prefix+'lo'])<=2*x.f_atr)
                candidates.append((level,anchor,x[prefix+'available_at'],setup))
        else:raise ValueError(family)
        for level,anchor,episode,setup in candidates:
            extension=d*(x.close-level)
            crossed=(extension>.05*x.f_atr)&(extension<=.5*x.f_atr)&(d*(x.close.shift(1)-level)<=.05*x.f_atr)
            mask=common&setup&crossed&episode.notna()
            for i in np.flatnonzero(mask.to_numpy()):
                result.append(dict(i=int(i),signal_at=int(x.signal_at.iloc[i]),direction=d,
                    level=float(level.iloc[i]),stop=float(anchor.iloc[i]-d*.15*x.f_atr.iloc[i]),
                    atr=float(x.f_atr.iloc[i]),event_id=f'{family}|{d}|{int(episode.iloc[i])}'))
    # One strongest/first definition per minute, deterministic across reruns.
    return list({s['i']:s for s in sorted(result,key=lambda s:(s['i'],s['event_id']),reverse=True)}.values())[::-1]


def simulate(x,events,exit_rule,start,end,delay=1):
    """Nonoverlapping positions; timestamps are bar OPEN times except signals."""
    spec=SPEC;fee=spec['fee_each_side'];slip=spec['slippage_each_side']
    arrays={k:x[k].to_numpy() for k in ('ts','open','high','low','close','trail_long','trail_short','f_atr')}
    ts=arrays['ts'];events={e['i']+delay:e for e in events if start<=e['signal_at']<end}
    trades=[];blocks=Counter();seen=set();p=None;cooldown=0
    realized=0.;peak=1.;drawdown=0.;daily={};initial_index=int(np.searchsorted(ts,start));last_index=int(np.searchsorted(ts,end))-1

    def mark(i):
        nonlocal peak,drawdown
        net=realized
        if p:
            price=arrays['close'][i]*(1-p['d']*slip)
            net+=p['fraction']*(p['gross']+p['units']*p['d']*(price-p['entry'])-p['fees']-p['funding']-p['units']*price*fee)
        equity=1+net;peak=max(peak,equity);drawdown=max(drawdown,1-equity/peak)
        daily[int(ts[i])//86400]=equity

    def close(price,now,reason,qty=None):
        nonlocal p,realized,cooldown
        qty=p['units'] if qty is None else min(qty,p['units'])
        fill=price*(1-p['d']*slip)
        p['gross']+=qty*p['d']*(fill-p['entry']);p['fees']+=qty*fill*fee
        p['turnover']+=qty*fill;p['units']-=qty
        p['fills'].append(dict(at=int(now),price=fill,quantity=qty,reason=reason))
        if p['units']<1e-12:
            p.update(closed=int(now),reason=reason,net=p['gross']-p['fees']-p['funding'])
            p['stress_net']=p['net']-spec['stress_extra_each_fill']*p['turnover']
            realized+=p['fraction']*p['net'];trades.append(p);p=None;cooldown=now+1800

    for i in range(max(0,initial_index),min(len(x),last_index+1)):
        now=int(ts[i]);op=arrays['open'][i];hi=arrays['high'][i];lo=arrays['low'][i];cl=arrays['close'][i]
        if p is None and now>=cooldown and i in events:
            event=events[i];d=event['direction']
            if event['event_id'] not in seen:
                fill=op*(1+d*slip);risk=d*(fill-event['stop'])/fill
                funding=spec['annual_funding_long'] if d>0 else spec['annual_funding_short']
                costs=2*(fee+slip)+funding*spec['max_holding_hours']/24/365.25
                extension=d*(fill-event['level'])/event['atr']
                if not (-.1<=extension<=.75):blocks['CHASE_OR_GAP']+=1
                elif not max(spec['min_risk_fraction'],spec['risk_to_cost_floor']*costs)<=risk<=spec['max_risk_fraction']:
                    blocks['RISK_VS_COST']+=1
                else:
                    fraction=min(spec['max_position_fraction'],spec['risk_per_trade']/(risk+costs))
                    fraction=np.floor(fraction/.05+1e-9)*.05
                    if fraction>=.05:
                        target_r=2 if exit_rule=='FIXED_2R' else 1
                        p=dict(opened=now,signal_at=event['signal_at'],entry=fill,d=d,units=1/fill,
                            initial_units=1/fill,fraction=float(fraction),risk=risk,stop=event['stop'],
                            initial_stop=event['stop'],target=fill*(1+d*risk*target_r),gross=0.,fees=fee,
                            funding=0.,funding_rate=funding,turnover=1.,tp1=False,mfe=0.,mae=0.,
                            event_id=event['event_id'],fills=[dict(at=now,price=fill,quantity=1/fill,reason='ENTRY')])
                        seen.add(event['event_id'])
        if p:
            d=p['d']
            p['funding']+=p['units']*op*p['funding_rate']*60/YEAR
            p['mfe']=max(p['mfe'],d*((hi if d>0 else lo)/p['entry']-1))
            p['mae']=min(p['mae'],d*((lo if d>0 else hi)/p['entry']-1))
            hit=lo<=p['stop'] if d>0 else hi>=p['stop']
            if hit:close(min(op,p['stop']) if d>0 else max(op,p['stop']),now+60,'STOP')
            else:
                tp=hi>=p['target'] if d>0 else lo<=p['target']
                if tp and not p['tp1']:
                    if exit_rule=='HALF_1R_TRAIL':
                        close(p['target'],now+60,'TP1',p['initial_units']*.5);p['tp1']=True
                    else:close(p['target'],now+60,'TARGET')
                if p and now+60-p['opened']>=spec['max_holding_hours']*3600 and i<last_index:
                    close(arrays['open'][i+1],int(ts[i+1]),'TIME_LIMIT')
                elif p and exit_rule=='HALF_1R_TRAIL' and p['tp1']:
                    anchor=arrays['trail_long' if d>0 else 'trail_short'][i]
                    candidate=anchor-d*.15*arrays['f_atr'][i]
                    # New close-derived stop only applies to NEXT candle.
                    if np.isfinite(candidate) and d*(candidate-p['stop'])>0 and d*(cl-candidate)>.3*arrays['f_atr'][i]:
                        p['stop']=float(candidate)
        if p and i==last_index:close(cl,min(now+60,end),'END_OF_SAMPLE')
        mark(i)
    return dict(trades=trades,blocks=dict(blocks),marked_drawdown=float(drawdown),
        return_on_allocated_book=float(realized),daily_equity=daily,exit_rule=exit_rule,delay=delay)


def metrics(result,stress=False):
    trades=result['trades'];field='stress_net' if stress else 'net'
    net=np.array([t[field] for t in trades]);positive=net[net>0].sum();negative=-net[net<0].sum()
    months={}
    for t in trades:
        key=pd.Timestamp(t['opened'],unit='s',tz='UTC').strftime('%Y-%m')
        months.setdefault(key,[]).append(t[field])
    return dict(trades=len(trades),wins=int((net>0).sum()),win_rate=float((net>0).mean()) if len(net) else None,
        avg_net=float(net.mean()) if len(net) else None,net_sum=float(net.sum()),
        profit_factor=float(positive/negative) if negative else None,
        avg_hold_hours=float(np.mean([(t['closed']-t['opened'])/3600 for t in trades])) if trades else None,
        marked_drawdown=result['marked_drawdown'] if not stress else None,
        months={k:dict(trades=len(v),net_sum=sum(v),avg_net=sum(v)/len(v)) for k,v in months.items()},
        exits=dict(Counter(t['reason'] for t in trades)),
        same_fill_stress=stress)


def bootstrap_bound(result,start,end,seed=752026):
    """Weekly block bootstrap of per-day trade net; family-wise alpha over 18 trials.

    This is a conservative rejection screen, not proof of future alpha. Groups
    preserve clustered trades; dependence longer than a week remains a limit.
    """
    first=start//86400;days=int(np.ceil(end/86400))-first
    pnl=np.zeros(days);counts=np.zeros(days)
    for t in result['trades']:
        day=t['opened']//86400-first
        pnl[day]+=t['stress_net'];counts[day]+=1
    n=days//7
    if n<4 or counts.sum()==0:return None
    # Include trailing incomplete week without silently dropping its trades.
    n=int(np.ceil(days/7));pad=n*7-days
    pp=np.pad(pnl,(0,pad)).reshape(n,7).sum(axis=1);nn=np.pad(counts,(0,pad)).reshape(n,7).sum(axis=1)
    rng=np.random.default_rng(seed);idx=rng.integers(0,n,(20000,n));den=nn[idx].sum(axis=1)
    estimates=pp[idx].sum(axis=1)/np.maximum(den,1)
    return float(np.quantile(estimates,.05/SPEC['multiple_test_count']))


def assessment(result,start,end):
    ordinary=metrics(result);stress=metrics(result,True);lower=bootstrap_bound(result,start,end)
    def pf(m):return m['profit_factor']>=SPEC['min_profit_factor'] if m['profit_factor'] is not None else m['wins']==m['trades'] and m['trades']>0
    checks=dict(sample=ordinary['trades']>=SPEC['min_trades_each_period'],
        win_rate=(ordinary['win_rate'] or 0)>=SPEC['min_win_rate'],
        positive_expectancy=(ordinary['avg_net'] or 0)>0,profit_factor=pf(ordinary),
        execution_stress=(stress['avg_net'] or 0)>0 and pf(stress),
        drawdown=result['marked_drawdown']<=SPEC['max_marked_drawdown'],
        multiple_test_bootstrap=lower is not None and lower>0)
    return dict(metrics=ordinary,stress=stress,stress_mean_lower_bound=lower,checks=checks,passed=all(checks.values()))


def code_hash():
    return hashlib.sha256(Path(__file__).read_bytes()+SPEC_PATH.read_bytes()).hexdigest()


def main():
    p=argparse.ArgumentParser();p.add_argument('--asset',required=True);p.add_argument('--input',required=True)
    p.add_argument('--output',required=True);p.add_argument('--period',choices=['discovery','control'],required=True)
    a=p.parse_args();out=Path(a.output);out.mkdir(parents=True,exist_ok=True);path=out/f'{a.asset}.json'
    result=json.loads(path.read_text()) if path.exists() else {}
    digest=code_hash()
    if result and result.get('spec_sha256')!=digest:raise ValueError('Frozen specification changed: new research version required')
    if a.period=='control' and not result.get('discovery'):raise ValueError('Discovery must be recorded before opening control')
    f=load(a.input);x=features(f)
    result.update(asset=a.asset,spec_sha256=digest,data_sha256=hashlib.sha256(Path(a.input).read_bytes()).hexdigest(),spec=SPEC)
    start,end=map(timestamp,SPEC[a.period]);period=dict(start=start,end=end,strategies={})
    assert f.ts.iloc[0]+3*86400<=start and f.ts.iloc[-1]+60>=end,'Incomplete coverage or warmup'
    for family in SPEC['families']:
        events=signals(x,family)
        for exit_ in SPEC['exits']:
            key=f'{family}|{exit_}'
            replay=simulate(x,events,exit_,start,end)
            score=assessment(replay,start,end)
            # Execution delay is a separate robustness test of unchanged rules.
            delayed=simulate(x,events,exit_,start,end,delay=2)
            score['one_extra_minute_delay']=metrics(delayed)
            score['checks']['delay_robustness']=(score['one_extra_minute_delay']['avg_net'] or 0)>0
            score['passed']=all(score['checks'].values())
            period['strategies'][key]=dict(result=replay,assessment=score)
            print(json.dumps(dict(asset=a.asset,period=a.period,rule=key,**score['metrics'],
                stress_avg_net=score['stress']['avg_net'],lower=score['stress_mean_lower_bound'],
                delay_avg_net=score['one_extra_minute_delay']['avg_net'],passed=score['passed'])),flush=True)
    result[a.period]=period
    if a.period=='discovery':
        result['frozen_candidates']=[k for k,v in period['strategies'].items() if v['assessment']['passed']]
    if a.period=='control':
        result['validated_candidates']=[k for k in result['frozen_candidates'] if period['strategies'][k]['assessment']['passed']]
    path.write_text(json.dumps(result,separators=(',',':'),allow_nan=False))


if __name__=='__main__':main()
