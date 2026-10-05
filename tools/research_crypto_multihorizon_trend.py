"""VERITAS multi-horizon trend continuation research.

Research only. Direction-specific rules combine:
- 1m execution trigger,
- 5m volatility state,
- 15m/1h pullback,
- 1h/4h/1d trend regime,
- structural stops,
- intraday vs 3-day vs 7-day holding logic.

Selection: 2022-2024 discovery + 2025 validation. 2026 Apr-Oct is holdout.
"""
from __future__ import annotations
import gc, json, os
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd

from research_crypto_manager_library import (
    load, features, rs, align, ts, met,
    FEE, SLIP, FUND_LONG, FUND_SHORT, YEAR_MIN,
    TEST_START, TEST_END
)

OUT=Path("multihorizon_out"); OUT.mkdir(exist_ok=True)
STRESS=.0005
COOLDOWN=30

def json_default(o):
    if isinstance(o,np.integer): return int(o)
    if isinstance(o,np.floating): return float(o)
    if isinstance(o,np.bool_): return bool(o)
    raise TypeError(type(o).__name__)

def add_horizon_features(raw,x):
    h1=rs(raw,60); m15=rs(raw,15)
    m15['pbL']=(m15.low<=m15.sma18).rolling(4).max().shift(1)
    m15['pbS']=(m15.high>=m15.sma18).rolling(4).max().shift(1)
    h1['pbL']=(h1.low<=h1.sma18).rolling(3).max().shift(1)
    h1['pbS']=(h1.high>=h1.sma18).rolling(3).max().shift(1)
    for n in (2,4,6,12):
        h1[f'lo{n}']=h1.low.rolling(n).min().shift(1)
        h1[f'hi{n}']=h1.high.rolling(n).max().shift(1)
    am=align(m15,x.ts.to_numpy())
    ah=align(h1,x.ts.to_numpy())
    x['m15_pbL']=am.pbL.fillna(0).to_numpy()>0
    x['m15_pbS']=am.pbS.fillna(0).to_numpy()>0
    x['h1_pbL']=ah.pbL.fillna(0).to_numpy()>0
    x['h1_pbS']=ah.pbS.fillna(0).to_numpy()>0
    for n in (2,4,6,12):
        x[f'h1_lo{n}']=ah[f'lo{n}'].to_numpy()
        x[f'h1_hi{n}']=ah[f'hi{n}'].to_numpy()
    # Need 20m levels for some trigger variants.
    x['hi20']=raw.high.rolling(20).max().shift(1)
    x['lo20']=raw.low.rolling(20).min().shift(1)
    return x

PROFILES=[
 ('NORMAL',10,1.20,.40,.85,1.50,.30),
 ('QUALITY',10,1.50,.50,1.00,1.50,.25),
 ('CALM',10,1.30,.45,.80,1.25,.25),
 ('QUALITY20',20,1.45,.50,.95,1.45,.25),
]

def make_events(x,d,regime,pullback,profile):
    _,lb,vol,body,ar_lo,ar_hi,extmax=profile
    lvl=x[f'hi{lb}'] if d>0 else x[f'lo{lb}']
    cross=(d*(x.close-lvl)>0)&(d*(x.close.shift(1)-lvl)<=0)
    bd=x.bull if d>0 else x.bear
    ext=d*(x.close-lvl)/x.m5_atr
    if regime=='H4D1':
        mask=(x.h4_trend==d)&(x.d1_trend==d)&(x.h1_trend!=-d)
    elif regime=='H1H4D1':
        mask=(x.h1_trend==d)&(x.h4_trend==d)&(x.d1_trend==d)
    elif regime=='H1H4':
        mask=(x.h1_trend==d)&(x.h4_trend==d)&(x.d1_trend!=-d)
    else: raise ValueError(regime)
    if pullback=='M15':
        mask&=(x.m15_pbL if d>0 else x.m15_pbS)
    elif pullback=='H1':
        mask&=(x.h1_pbL if d>0 else x.h1_pbS)
    elif pullback=='BOTH':
        mask&=(x.m15_pbL if d>0 else x.m15_pbS)&(x.h1_pbL if d>0 else x.h1_pbS)
    else: raise ValueError(pullback)
    mask&=cross&(x.vr>=vol)&(bd>=body)&(x.m5_atr_ratio>=ar_lo)&(x.m5_atr_ratio<=ar_hi)&(ext>.002)&(ext<=extmax)&x.contig1440
    out=[]
    for i in np.flatnonzero(mask.fillna(False).to_numpy()):
        if np.isfinite(lvl.iloc[i]) and np.isfinite(x.h1_atr.iloc[i]):
            out.append((int(i),d,float(lvl.iloc[i])))
    return out

def sim(x,ev,stop_h,buf,mode,maxhold,start,end,stress=0.):
    T=x.ts.to_numpy();O=x.open.to_numpy();H=x.high.to_numpy();L=x.low.to_numpy();C=x.close.to_numpy()
    A1=x.h1_atr.to_numpy(); slo=x[f'h1_lo{stop_h}'].to_numpy(); shi=x[f'h1_hi{stop_h}'].to_numpy()
    st=ts(pd.Timestamp(start,tz='UTC')); en=ts(pd.Timestamp(end,tz='UTC')); ei=int(np.searchsorted(T,en))
    out=[];free=-1
    for sig,d,lvl in ev:
        if T[sig]<st or T[sig]>=en-60: continue
        i=sig+1
        if i<free or i>=ei: continue
        sr=slo[sig] if d>0 else shi[sig]
        if not np.isfinite(sr) or not np.isfinite(A1[sig]): continue
        entry=O[i]*(1+d*SLIP); stop=sr-d*buf*A1[sig]; risk=d*(entry-stop)/entry
        if not (.004<=risk<=.05): continue
        target=entry*(1+d*risk*2.0)
        gross=0.;cost=FEE+stress/2;rem=1.;part=False;reason='TIME'
        j=i;last=min(ei-1,i+maxhold)
        while j<=last:
            if j>i: cost+=rem*(FUND_LONG if d>0 else FUND_SHORT)/YEAR_MIN
            if (L[j]<=stop if d>0 else H[j]>=stop):
                q=(min(O[j],stop) if d>0 else max(O[j],stop))*(1-d*SLIP)
                gross+=rem*d*(q/entry-1);cost+=rem*(FEE+stress/2)*q/entry;rem=0.;reason='STOP';break
            if mode=='FIXED2' and (H[j]>=target if d>0 else L[j]<=target):
                q=target*(1-d*SLIP);gross+=rem*d*(q/entry-1);cost+=rem*(FEE+stress/2)*q/entry;rem=0.;reason='TARGET';break
            if mode in ('PART_TRAIL','PART_3R') and not part:
                one=entry*(1+d*risk)
                if (H[j]>=one if d>0 else L[j]<=one):
                    q=one*(1-d*SLIP);gross+=.5*d*(q/entry-1);cost+=.5*(FEE+stress/2)*q/entry;rem=.5;part=True;stop=entry
            if mode=='PART_3R' and part:
                t3=entry*(1+d*risk*3.0)
                if (H[j]>=t3 if d>0 else L[j]<=t3):
                    q=t3*(1-d*SLIP);gross+=rem*d*(q/entry-1);cost+=rem*(FEE+stress/2)*q/entry;rem=0.;reason='TARGET3';break
            if mode=='PART_TRAIL' and part and j>i+60:
                tr=(slo[j] if d>0 else shi[j])-d*.05*A1[j]
                if np.isfinite(tr) and d*(tr-stop)>0 and d*(C[j]-tr)>.10*A1[j]:stop=tr
            j+=1
        if rem:
            j=min(j,last);q=C[j]*(1-d*SLIP);gross+=rem*d*(q/entry-1);cost+=rem*(FEE+stress/2)*q/entry
        out.append({'opened':int(T[i]),'closed':int(T[j]),'d':d,'net':float(gross-cost),'reason':reason})
        free=j+COOLDOWN
    return out

def years(x,ev,p,stress=0.):
    return {str(y):met(sim(x,ev,start=f'{y}-01-01',end=f'{y+1}-01-01',stress=stress,**p)) for y in (2022,2023,2024)}

def discovery_ok(ys):
    vals=[m for m in ys.values() if m['n']>=6]
    if len(vals)<2:return False
    if sum((m['avg'] or -9)>0 for m in vals)<2:return False
    if sum(m['n'] for m in vals)<25:return False
    return True

def research(asset,x):
    rows=[]
    for d in (1,-1):
      for regime in ('H4D1','H1H4D1','H1H4'):
       for pb in ('M15','H1','BOTH'):
        for prof in PROFILES:
            ev=make_events(x,d,regime,pb,prof)
            if len(ev)<20:continue
            for stop_h in (2,4,6):
             for mode in ('FIXED2','PART_TRAIL','PART_3R'):
              for hold in (1440,4320,10080):
                p=dict(stop_h=stop_h,buf=.10,mode=mode,maxhold=hold)
                ys=years(x,ev,p,0.)
                if not discovery_ok(ys):continue
                val=met(sim(x,ev,start='2025-01-01',end='2026-01-01',stress=0.,**p))
                vst=met(sim(x,ev,start='2025-01-01',end='2026-01-01',stress=STRESS,**p))
                if val['n']<6 or (val['avg'] or -9)<=0 or val['pf']<1.05 or (vst['avg'] or -9)<=0:continue
                # Freeze before 2026.
                score=100*val['avg']+.30*val['win_rate']+.20*min(val['pf'],3.)
                rows.append((score,d,regime,pb,prof,p,ev,ys,val,vst))
    rows.sort(key=lambda z:z[0],reverse=True)
    frozen=[];seen=set()
    for z in rows:
        key=(z[1],z[2],z[3],z[4][0],z[5]['mode'],z[5]['maxhold'])
        if key in seen:continue
        seen.add(key);frozen.append(z)
        if len(frozen)>=24:break
    out=[]
    for rank,z in enumerate(frozen,1):
        sc,d,regime,pb,prof,p,ev,ys,val,vst=z
        test=met(sim(x,ev,start='2026-04-04',end='2026-10-04',stress=0.,**p))
        stress=met(sim(x,ev,start='2026-04-04',end='2026-10-04',stress=STRESS,**p))
        blocks=[met(sim(x,ev,start=a,end=b,stress=0.,**p)) for a,b in [('2026-04-04','2026-06-01'),('2026-06-01','2026-08-01'),('2026-08-01','2026-10-04')]]
        pbcount=sum((m['avg'] or -9)>0 for m in blocks)
        passed=test['n']>=8 and (test['avg'] or -9)>0 and test['pf']>=1.20 and (stress['avg'] or -9)>0 and stress['pf']>=1.10 and pbcount>=2
        out.append({'rank':rank,'direction':'LONG' if d>0 else 'SHORT','regime':regime,'pullback':pb,
                    'entry_profile':{'name':prof[0],'level_min':prof[1],'volume':prof[2],'body':prof[3],'atr_lo':prof[4],'atr_hi':prof[5],'ext':prof[6]},
                    'management':p,'discovery':ys,'validation_2025':val,'validation_stress':vst,
                    'test_2026':test,'test_stress':stress,'blocks':blocks,'positive_blocks':pbcount,'passed':passed})
    print(asset,'multihorizon_pre2026',len(rows),'frozen',len(out),'passes',sum(r['passed'] for r in out),flush=True)
    return out

def main():
    asset=os.environ.get('RESEARCH_ASSET','ETH').upper()
    raw=load(asset)
    x=add_horizon_features(raw,features(raw))
    out={'generated_at':datetime.now(timezone.utc).isoformat(),'asset':asset,
         'method':'Direction-specific multi-horizon trend continuation; discovery 2022-24, validation 2025, holdout 2026.',
         'finalists':research(asset,x)}
    out['passes']=[r for r in out['finalists'] if r['passed']]
    (OUT/'result.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False,default=json_default))
    print('VERITAS_MULTIHORIZON_PASSES='+json.dumps(out['passes'][:8],ensure_ascii=False,separators=(',',':'),default=json_default),flush=True)

if __name__=='__main__':main()
