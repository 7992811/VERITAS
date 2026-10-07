"""R76 exploratory alternatives; frozen before the May-July control is read.

R75 discovery failed. These are new hypotheses, not threshold tweaks to its
losing rules. Same execution, costs, delays and conservative bar ordering.
"""
import argparse,json,hashlib
from pathlib import Path
import numpy as np
import pandas as pd
import research_trend_regimes as R

FAMILIES=('FAILED_BREAKOUT','EXHAUSTION','TREND_VALUE')


def prepare(f):
    x=R.features(f);hour=R.resample(f,60)
    hour['atr']=R.atr(hour)
    hour['sma18']=hour.close.rolling(18).mean()
    hour['efficiency']=(hour.close-hour.close.shift(12)).abs()/hour.close.diff().abs().rolling(12).sum()
    h=R.align(hour,f.ts.to_numpy())
    x['h_atr']=h.atr;x['h_sma18']=h.sma18;x['h_efficiency']=h.efficiency
    for n in (3,5,15):
        x[f'm_lo{n}']=x.low.rolling(n).min();x[f'm_hi{n}']=x.high.rolling(n).max()
    x['m_lo15_at']=x.ts-(14-x.low.rolling(15).apply(np.argmin,raw=True))*60
    x['m_hi15_at']=x.ts-(14-x.high.rolling(15).apply(np.argmax,raw=True))*60
    return x


def signals(x,family):
    output=[];span=x.high-x.low
    for d in (1,-1):
        common=(x.vbase>0)&(x.volume>=1.2*x.vbase)&(span>0)&(d*(x.close-x.open)>=.5*span)&(x.f_atr>0)
        if family=='FAILED_BREAKOUT':
            level=x.f_lo48 if d>0 else x.f_hi48
            extreme=x.m_lo3 if d>0 else x.m_hi3
            # First close back inside an existing four-hour boundary, with
            # observable wick penetration. Range regime only, no future pivots.
            setup=(x.h_efficiency<=.35)&(d*(extreme-level)<=-.15*x.f_atr)&\
                (d*(x.close-level)>.05*x.f_atr)&(d*(x.close.shift(1)-level)<=.05*x.f_atr)
            episode=x.f_lo48_at if d>0 else x.f_hi48_at
        elif family=='EXHAUSTION':
            level=x.m_hi3.shift(1) if d>0 else x.m_lo3.shift(1)
            extreme=x.m_lo15 if d>0 else x.m_hi15
            stretch=d*(x.close.shift(1)-x.h_sma18)<-2*x.h_atr
            setup=stretch&(d*(x.close-level)>.05*x.f_atr)&(d*(x.close.shift(1)-level)<=.05*x.f_atr)
            episode=x.m_lo15_at if d>0 else x.m_hi15_at
        elif family=='TREND_VALUE':
            level=x.h_sma18
            extreme=x.f_lo12.combine(x.m_lo5,min) if d>0 else x.f_hi12.combine(x.m_hi5,max)
            setup=(x.direction==d)&(d*(x.close-level)>.05*x.f_atr)&(d*(x.close.shift(1)-level)<=.05*x.f_atr)
            episode=x.f_lo6_at if d>0 else x.f_hi6_at
        else:raise ValueError(family)
        extension=d*(x.close-level)/x.f_atr
        mask=common&setup&(extension<=.5)&episode.notna()
        for i in np.flatnonzero(mask.to_numpy()):
            output.append(dict(i=int(i),signal_at=int(x.signal_at.iloc[i]),direction=d,
                level=float(level.iloc[i]),stop=float(extreme.iloc[i]-d*.15*x.f_atr.iloc[i]),
                atr=float(x.f_atr.iloc[i]),event_id=f'{family}|{d}|{int(episode.iloc[i])}'))
    return sorted(output,key=lambda s:s['i'])


def main():
    p=argparse.ArgumentParser();p.add_argument('--asset',required=True);p.add_argument('--input',required=True)
    p.add_argument('--output',required=True);p.add_argument('--period',choices=['discovery','control'],required=True)
    a=p.parse_args();out=Path(a.output);out.mkdir(parents=True,exist_ok=True);path=out/f'{a.asset}.json'
    result=json.loads(path.read_text()) if path.exists() else {}
    digest=hashlib.sha256(Path(__file__).read_bytes()+Path(R.__file__).read_bytes()+R.SPEC_PATH.read_bytes()).hexdigest()
    if result and result.get('spec_sha256')!=digest:raise ValueError('Specification changed')
    if a.period=='control' and not result.get('discovery'):raise ValueError('Record discovery first')
    # Account for BOTH batches' nine rules across two assets, not just winners.
    R.SPEC=dict(R.SPEC,multiple_test_count=36,version='R76_RESEARCH_ONLY',families=list(FAMILIES))
    f=R.load(a.input);x=prepare(f);start,end=map(R.timestamp,R.SPEC[a.period])
    assert f.ts.iloc[0]+3*86400<=start and f.ts.iloc[-1]+60>=end
    result.update(asset=a.asset,spec_sha256=digest,spec=R.SPEC,
                  data_sha256=hashlib.sha256(Path(a.input).read_bytes()).hexdigest())
    period=dict(start=start,end=end,strategies={})
    for family in FAMILIES:
        events=signals(x,family)
        for exit_ in R.SPEC['exits']:
            key=f'{family}|{exit_}';replay=R.simulate(x,events,exit_,start,end)
            score=R.assessment(replay,start,end)
            delayed=R.simulate(x,events,exit_,start,end,delay=2)
            score['one_extra_minute_delay']=R.metrics(delayed)
            score['checks']['delay_robustness']=(score['one_extra_minute_delay']['avg_net'] or 0)>0
            score['passed']=all(score['checks'].values())
            period['strategies'][key]=dict(result=replay,assessment=score)
            print(json.dumps(dict(asset=a.asset,period=a.period,rule=key,**score['metrics'],
                stress_avg_net=score['stress']['avg_net'],lower=score['stress_mean_lower_bound'],
                delay_avg_net=score['one_extra_minute_delay']['avg_net'],passed=score['passed'])),flush=True)
    result[a.period]=period
    if a.period=='discovery':result['frozen_candidates']=[k for k,v in period['strategies'].items() if v['assessment']['passed']]
    if a.period=='control':result['validated_candidates']=[k for k in result['frozen_candidates'] if period['strategies'][k]['assessment']['passed']]
    path.write_text(json.dumps(result,separators=(',',':'),allow_nan=False))


if __name__=='__main__':main()
