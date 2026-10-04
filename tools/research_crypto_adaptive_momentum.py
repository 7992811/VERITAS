"""VERITAS adaptive volatility-normalized momentum research.

Research only. Fixed theory-led profiles selected on 2022-2025; Apr-Oct 2026 is
opened only after candidates are frozen. Uses standardized multi-horizon returns,
local breakout timing, volatility expansion, cross-asset confirmation and
structural risk.
"""
from __future__ import annotations
import gc, json, math
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd

from research_crypto_manager_library import (
    load, features, rs, align, ts, met,
    HIST_START, SELECT_END, TEST_START, TEST_END,
    FEE, SLIP, FUND_LONG, FUND_SHORT, YEAR_MIN
)

OUT=Path('adaptive_momentum_out'); OUT.mkdir(exist_ok=True)
MAX_HOLD=1440; COOLDOWN=15

def add_norm_momentum(raw,x,prefix=''):
    r=raw.close.pct_change()
    temp=pd.DataFrame({'ts':raw.ts,'close':raw.close})
    for n in (15,60,240,1440):
        ret=raw.close/raw.close.shift(n)-1
        rv=r.rolling(n,min_periods=n).std().shift(1)*math.sqrt(n)
        temp[f'z{n}']=ret/rv.replace(0,np.nan)
    idx=pd.to_datetime(temp.ts,unit='s',utc=True); temp.index=idx
    target=pd.to_datetime(x.ts,unit='s',utc=True)
    a=temp.reindex(target,method='ffill')
    for n in (15,60,240,1440): x[f'{prefix}z{n}']=a[f'z{n}'].to_numpy()
    return x

def add_pb(raw,x):
    m15=rs(raw,15)
    m15['pbL']=(m15.low<=m15.sma18).rolling(4).max().shift(1)
    m15['pbS']=(m15.high>=m15.sma18).rolling(4).max().shift(1)
    a=align(m15,x.ts.to_numpy())
    x['pbL4']=a.pbL.fillna(0).to_numpy()>0; x['pbS4']=a.pbS.fillna(0).to_numpy()>0
    return x

PROFILES={
 'EARLY': dict(z15=.45,z60=.35,z240=-.25,z1440=-.75,cross60=.0,cross240=-.50,accel=.10),
 'CONFIRMED': dict(z15=.25,z60=.50,z240=.45,z1440=-.25,cross60=.10,cross240=.0,accel=-9.),
 'ACCEL': dict(z15=.60,z60=.25,z240=-.35,z1440=-.75,cross60=.0,cross240=-.50,accel=.35),
 'BROAD': dict(z15=.30,z60=.25,z240=.0,z1440=-.50,cross60=.0,cross240=-.25,accel=-9.),
}
QUALITY={
 'NORMAL':dict(vol=1.20,body=.40,ar=1.00,ext=.35),
 'QUALITY':dict(vol=1.40,body=.50,ar=1.08,ext=.25),
 'IMPULSE':dict(vol=1.70,body=.60,ar=1.18,ext=.18),
}

def setup_events(asset,d,x,profile,quality,level_n,setup):
    p=PROFILES[profile]; q=QUALITY[quality]
    hi=x.high.rolling(level_n).max().shift(1); lo=x.low.rolling(level_n).min().shift(1)
    level=hi if d>0 else lo
    crossed=(d*(x.close-level)>0)&(d*(x.close.shift(1)-level)<=0)
    body=x.bull if d>0 else x.bear
    ext=d*(x.close-level)/x.m5_atr
    accel=d*(x.z60-x.z60.shift(15))
    mask=crossed&(d*x.z15>=p['z15'])&(d*x.z60>=p['z60'])&(d*x.z240>=p['z240'])&(d*x.z1440>=p['z1440'])
    mask&=(d*x.oz60>=p['cross60'])&(d*x.oz240>=p['cross240'])&(accel>=p['accel'])
    mask&=(x.vr>=q['vol'])&(body>=q['body'])&(x.m5_atr_ratio>=q['ar'])&(ext>.005)&(ext<=q['ext'])&x.contig1440
    # Avoid direct opposition from the daily structural trend.
    mask&=(x.d1_trend!=-d)
    if setup=='PULLBACK': mask&=(x.pbL4 if d>0 else x.pbS4)
    elif setup=='SQUEEZE': mask&=(x.m5_squeeze<=.90)
    stopref=x.low.rolling(30).min().shift(1) if d>0 else x.high.rolling(30).max().shift(1)
    out=[]
    for i in np.flatnonzero(mask.fillna(False).to_numpy()):
        if np.isfinite(level.iloc[i]) and np.isfinite(stopref.iloc[i]) and np.isfinite(x.m5_atr.iloc[i]):
            out.append((int(i),d,float(level.iloc[i]),float(stopref.iloc[i]),float(x.m5_atr.iloc[i])))
    return out

def sim(x,ev,buf,rr,mode,start,end,stress=0.,time_stop=0):
    T=x.ts.to_numpy(); O=x.open.to_numpy(); H=x.high.to_numpy(); L=x.low.to_numpy(); C=x.close.to_numpy(); A=x.m5_atr.to_numpy()
    out=[]; next_i=-1; end_i=int(np.searchsorted(T,end))
    for sig,d,level,sr,av in ev:
        if T[sig]<start or T[sig]>=end-60: continue
        i=sig+1
        if i<next_i or i>=end_i: continue
        entry=O[i]*(1+d*SLIP); stop=sr-d*buf*av; risk=d*(entry-stop)/entry
        if not (.003<=risk<=.035): continue
        target=entry*(1+d*risk*rr); gross=0.; cost=FEE+stress/2; rem=1.; partial=False; reason='TIME'
        j=i; last=min(end_i-1,i+MAX_HOLD); mfe=0.
        while j<=last:
            if j>i: cost+=rem*(FUND_LONG if d>0 else FUND_SHORT)/YEAR_MIN
            fav=(H[j]/entry-1) if d>0 else (1-L[j]/entry); mfe=max(mfe,float(fav))
            if (L[j]<=stop if d>0 else H[j]>=stop):
                q=(min(O[j],stop) if d>0 else max(O[j],stop))*(1-d*SLIP)
                gross+=rem*d*(q/entry-1); cost+=rem*(FEE+stress/2)*q/entry; rem=0.; reason='STOP'; break
            if time_stop and j>=i+time_stop and mfe<.20*risk:
                q=C[j]*(1-d*SLIP); gross+=rem*d*(q/entry-1); cost+=rem*(FEE+stress/2)*q/entry; rem=0.; reason='STALL'; break
            if mode in ('PARTIAL075','PARTIAL1') and not partial:
                pr=.75 if mode=='PARTIAL075' else 1.
                plev=entry*(1+d*risk*pr)
                if (H[j]>=plev if d>0 else L[j]<=plev):
                    qty=.5; q=plev*(1-d*SLIP)
                    gross+=qty*d*(q/entry-1); cost+=qty*(FEE+stress/2)*q/entry; rem-=qty; partial=True
                    stop=entry*(1+d*.05*risk)
            if (H[j]>=target if d>0 else L[j]<=target):
                q=target*(1-d*SLIP); gross+=rem*d*(q/entry-1); cost+=rem*(FEE+stress/2)*q/entry; rem=0.; reason='TARGET'; break
            if mode=='TRAIL' and j>i+15:
                a=max(i,j-30); tr=(np.min(L[a:j]) if d>0 else np.max(H[a:j]))-d*.10*A[j]
                if np.isfinite(tr) and d*(tr-stop)>0 and d*(C[j]-tr)>.15*A[j]: stop=tr
            j+=1
        if rem:
            j=min(j,last); q=C[j]*(1-d*SLIP); gross+=rem*d*(q/entry-1); cost+=rem*(FEE+stress/2)*q/entry
        out.append({'opened':int(T[i]),'closed':int(T[j]),'d':d,'net':float(gross-cost),'reason':reason})
        next_i=j+COOLDOWN
    return out

def eval_years(x,ev,pars,stress=0.):
    return {str(y):met(sim(x,ev,start=ts(f'{y}-01-01T00:00:00Z'),end=ts(f'{y+1}-01-01T00:00:00Z'),stress=stress,**pars)) for y in (2022,2023,2024,2025)}

def stable(yrs,syrs):
    vals=list(yrs.values()); svals=list(syrs.values())
    if sum(m['n']>=7 for m in vals)<3:return False
    if sum((m['avg'] or -9)>0 for m in vals)<3:return False
    if sum((m['avg'] or -9)>0 for m in svals)<2:return False
    agg_n=sum(m['n'] for m in vals)
    return agg_n>=35

def score(yrs,syrs):
    vals=[m for m in yrs.values() if m['n']>=7]; svals=[m for m in syrs.values() if m['n']>=7]
    return min(m['pf'] for m in vals)+.30*min(m['pf'] for m in svals)+80*min(m['avg'] for m in vals)+.35*np.median([m['win_rate'] for m in vals])

def search(asset,d,x):
    candidates=[]
    for profile in PROFILES:
      for quality in QUALITY:
       for level in (5,10,20,30):
        for setup in ('BREAKOUT','PULLBACK','SQUEEZE'):
            ev=setup_events(asset,d,x,profile,quality,level,setup)
            if len(ev)<35: continue
            for buf,rr,mode,tstop in [
              (.10,1.0,'FIXED',0),(.10,1.5,'FIXED',0),(.10,2.0,'FIXED',0),
              (.20,1.5,'FIXED',0),(.20,2.0,'FIXED',0),(.20,2.5,'FIXED',0),
              (.10,1.5,'PARTIAL075',0),(.10,2.0,'PARTIAL1',0),
              (.20,2.0,'PARTIAL1',0),(.20,2.0,'TRAIL',0),
              (.20,2.0,'FIXED',30),
            ]:
                pars=dict(buf=buf,rr=rr,mode=mode,time_stop=tstop)
                yrs=eval_years(x,ev,pars,0.); syrs=eval_years(x,ev,pars,.0005)
                if not stable(yrs,syrs): continue
                candidates.append((score(yrs,syrs),profile,quality,level,setup,pars,yrs,syrs,ev))
    candidates.sort(key=lambda z:z[0],reverse=True)
    frozen=[]; seen=set()
    for z in candidates:
        key=(z[1],z[2],z[4],z[5]['rr'],z[5]['mode'])
        if key in seen: continue
        seen.add(key); frozen.append(z)
        if len(frozen)>=12: break
    out=[]
    for rank,z in enumerate(frozen,1):
        sc,profile,quality,level,setup,pars,yrs,syrs,ev=z
        test=met(sim(x,ev,start=ts(TEST_START),end=ts(TEST_END),stress=0.,**pars))
        st=met(sim(x,ev,start=ts(TEST_START),end=ts(TEST_END),stress=.0005,**pars))
        blocks=[met(sim(x,ev,start=ts(a+'T00:00:00Z'),end=ts(b+'T00:00:00Z'),stress=0.,**pars)) for a,b in
                [('2026-04-04','2026-06-01'),('2026-06-01','2026-08-01'),('2026-08-01','2026-10-04')]]
        pb=sum((m['avg'] or -9)>0 for m in blocks)
        passed=test['n']>=10 and (test['avg'] or -9)>0 and test['pf']>=1.25 and (st['avg'] or -9)>0 and st['pf']>=1.15 and pb>=2
        out.append({'rank':rank,'profile':profile,'quality':quality,'level_min':level,'setup':setup,'params':pars,
                    'selection':yrs,'selection_stress':syrs,'test_2026':test,'test_stress':st,'blocks':blocks,'positive_blocks':pb,'passed':passed})
    print(asset,'LONG' if d>0 else 'SHORT','candidates',len(candidates),'frozen',len(out),'passes',sum(r['passed'] for r in out),flush=True)
    return out

def main():
    btc=load('BTC'); eth=load('ETH')
    report={'generated_at':datetime.now(timezone.utc).isoformat(),'method':'vol-normalized adaptive momentum; selection 2022-2025 only','modules':{}}
    for asset,raw,other in [('ETH',eth,btc),('BTC',btc,eth)]:
        x=features(raw); x=add_pb(raw,x); x=add_norm_momentum(raw,x,'')
        ox=pd.DataFrame({'ts':other.ts,'close':other.close})
        r=other.close.pct_change()
        for n in (15,60,240,1440):
            ox[f'z{n}']=(other.close/other.close.shift(n)-1)/(r.rolling(n,min_periods=n).std().shift(1)*math.sqrt(n)).replace(0,np.nan)
        ox.index=pd.to_datetime(ox.ts,unit='s',utc=True); idx=pd.to_datetime(x.ts,unit='s',utc=True); oa=ox.reindex(idx,method='ffill')
        for n in (15,60,240,1440): x[f'oz{n}']=oa[f'z{n}'].to_numpy()
        report['modules'][asset+'_LONG']=search(asset,1,x)
        report['modules'][asset+'_SHORT']=search(asset,-1,x)
        del x; gc.collect()
    report['passes']={k:[r for r in v if r['passed']] for k,v in report['modules'].items()}
    (OUT/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False))
    print('VERITAS_ADAPTIVE_PASSES='+json.dumps({k:v[:3] for k,v in report['passes'].items()},ensure_ascii=False,separators=(',',':')),flush=True)

if __name__=='__main__': main()
