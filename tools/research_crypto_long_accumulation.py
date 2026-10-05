"""VERITAS long accumulation-breakout research.

Research-only. Looks for multi-hour accumulation/compression before a long breakout,
then uses 1m execution with 5m volatility/volume bands and 1h/4h/1d context.
No mirrored short logic.

Selection:
  discovery 2022-2023,
  validation 2024 and 2025 independently,
  Apr-Oct 2026 evaluated after freeze.
"""
from __future__ import annotations
import gc, json, math, os
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd

from research_crypto_manager_library import load, features, rs, align, atr, ts, met, FEE, SLIP, FUND_LONG, YEAR_MIN

OUT=Path("accum_breakout_out"); OUT.mkdir(exist_ok=True)
STRESS=.0005
COOLDOWN=30

def jd(o):
    if isinstance(o,np.integer):return int(o)
    if isinstance(o,np.floating):return float(o)
    if isinstance(o,np.bool_):return bool(o)
    raise TypeError(type(o).__name__)

def add(raw,x,other):
    # Local/multi-hour levels.
    for n in (20,30,60,120,240,480):
        x[f'hi{n}']=raw.high.rolling(n).max().shift(1)
        x[f'lo{n}']=raw.low.rolling(n).min().shift(1)

    # Realized volatility path.
    r=raw.close.pct_change()
    for n in (30,120,240):
        x[f'rv{n}']=r.rolling(n).std().shift(1)
    x['rv30_120']=x.rv30/x.rv120.replace(0,np.nan)
    x['rv120_240']=x.rv120/x.rv240.replace(0,np.nan)

    # Compression width normalized to 1h ATR.
    for n in (120,240,480):
        x[f'range{n}_atr1']=(x[f'hi{n}']-x[f'lo{n}'])/x.h1_atr.replace(0,np.nan)

    # Repeated resistance tests within a tolerance before the break.
    for n in (120,240,480):
        level=x[f'hi{n}']
        eps=.15*x.h1_atr
        near=(raw.high>=level-eps).astype(float)
        x[f'tests{n}']=near.rolling(n).sum().shift(1)

    # Volume quieting before breakout.
    x['v10']=raw.volume.rolling(10).median().shift(1)
    x['v60prev']=raw.volume.shift(10).rolling(60).median()
    x['v_contract']=x.v10/x.v60prev.replace(0,np.nan)

    # 15m / 1h location relative to trend value.
    m15=rs(raw,15);h1=rs(raw,60)
    m15['above18']=m15.close>m15.sma18
    h1['above18']=h1.close>h1.sma18
    h1['above50']=h1.close>h1.sma50
    am=align(m15,x.ts.to_numpy());ah=align(h1,x.ts.to_numpy())
    x['m15_above18']=am.above18.fillna(False).to_numpy(dtype=bool)
    x['h1_above18']=ah.above18.fillna(False).to_numpy(dtype=bool)
    x['h1_above50']=ah.above50.fillna(False).to_numpy(dtype=bool)

    # Cross-asset context / relative strength.
    o=other[['ts','close']].copy()
    for n in (60,240,1440):o[f'ret{n}']=o.close/o.close.shift(n)-1
    o.index=pd.to_datetime(o.ts,unit='s',utc=True)
    ao=o.reindex(pd.to_datetime(x.ts,unit='s',utc=True),method='ffill')
    for n in (60,240,1440):x[f'other_ret{n}']=ao[f'ret{n}'].to_numpy()
    x['rel60']=x.ret60-x.other_ret60
    x['rel240']=x.ret240-x.other_ret240
    return x

def regime(x,m):
    if m=='H4D1':return (x.h4_trend==1)&(x.d1_trend==1)&(x.h1_trend!=-1)
    if m=='H4':return (x.h4_trend==1)&(x.d1_trend!=-1)&(x.h1_trend!=-1)
    if m=='EARLY':return (x.h1_trend==1)&(x.h4_trend!=-1)&(x.d1_trend!=-1)
    if m=='ALL':return (x.h1_trend==1)&(x.h4_trend==1)&(x.d1_trend==1)
    raise ValueError

def crossf(x,m):
    if m=='NONE':return pd.Series(True,index=x.index)
    if m=='CONF':return (x.other_ret240>0)
    if m=='REL':return (x.rel60>0)&(x.rel240>0)
    if m=='MIX':return (x.other_ret240>0)&(x.rel60>0)
    raise ValueError

def cfgs():
    out=[]
    for reg in ('H4D1','H4','EARLY','ALL'):
      for cross in ('NONE','CONF','REL','MIX'):
       out += [
        dict(reg=reg,cross=cross,range=120,width=1.6,tests=2,rv=.85,vcon=1.0,vol_lo=1.2,vol_hi=2.5,body=.45,atr_lo=.85,atr_hi=1.30,ext=.25),
        dict(reg=reg,cross=cross,range=240,width=2.1,tests=3,rv=.90,vcon=.95,vol_lo=1.35,vol_hi=2.3,body=.50,atr_lo=.90,atr_hi=1.35,ext=.22),
        dict(reg=reg,cross=cross,range=480,width=2.8,tests=3,rv=.95,vcon=1.0,vol_lo=1.45,vol_hi=2.4,body=.55,atr_lo=.90,atr_hi=1.40,ext=.20),
        dict(reg=reg,cross=cross,range=120,width=1.3,tests=2,rv=.75,vcon=.90,vol_lo=1.5,vol_hi=2.8,body=.60,atr_lo=.95,atr_hi=1.35,ext=.18),
       ]
    return out

def evs(x,c):
    n=c['range'];lvl=x[f'hi{n}'];cross=(x.close>lvl)&(x.close.shift(1)<=lvl);ext=(x.close-lvl)/x.m5_atr
    m=regime(x,c['reg'])&crossf(x,c['cross'])&cross
    m&=(x[f'range{n}_atr1']<=c['width'])&(x[f'tests{n}']>=c['tests'])
    m&=(x.rv30_120<=c['rv'])&(x.v_contract<=c['vcon'])
    m&=(x.vr>=c['vol_lo'])&(x.vr<=c['vol_hi'])&(x.bull>=c['body'])
    m&=(x.m5_atr_ratio>=c['atr_lo'])&(x.m5_atr_ratio<=c['atr_hi'])&(ext>.001)&(ext<=c['ext'])&x.contig1440
    # price should have recovered on intraday frames, but not be extended far from 1h value.
    m&=x.m15_above18&x.h1_above50
    out=[]
    low=x[f'lo{n}']
    for i in np.flatnonzero(m.fillna(False).to_numpy()):
        if np.isfinite(lvl.iloc[i]) and np.isfinite(low.iloc[i]) and np.isfinite(x.h1_atr.iloc[i]):
            out.append((int(i),float(lvl.iloc[i]),float(low.iloc[i])))
    return out

MGT=[
 ('RANGE_15',1.5,1440,'RANGE'),
 ('RANGE_20',2.0,4320,'RANGE'),
 ('RANGE_25',2.5,4320,'RANGE'),
 ('RANGE_30',3.0,10080,'RANGE'),
 ('MID_20',2.0,4320,'MID'),
 ('PART_30',3.0,10080,'PART'),
]

def sim(x,ev,mg,start,end,stress=0.):
    name,rr,hold,stype=mg
    T=x.ts.to_numpy();O=x.open.to_numpy();H=x.high.to_numpy();L=x.low.to_numpy();C=x.close.to_numpy();A1=x.h1_atr.to_numpy()
    st=ts(pd.Timestamp(start,tz='UTC'));en=ts(pd.Timestamp(end,tz='UTC'));ei=int(np.searchsorted(T,en))
    out=[];free=-1
    for sig,lvl,range_low in ev:
        if T[sig]<st or T[sig]>=en-60:continue
        i=sig+1
        if i<free or i>=ei:continue
        entry=O[i]*(1+SLIP)
        # Range stop: under lower accumulation boundary; midpoint stop for tighter setup.
        if stype=='MID':
            stop=(lvl+range_low)/2-.10*A1[sig]
        else:
            stop=range_low-.08*A1[sig]
        risk=(entry-stop)/entry
        if not(.004<=risk<=.08):continue
        target=entry*(1+risk*rr);cost=FEE+stress/2;gross=0.;rem=1.;part=False;j=i;last=min(ei-1,i+hold);reason='TIME'
        while j<=last:
            if j>i:cost+=rem*FUND_LONG/YEAR_MIN
            if L[j]<=stop:
                q=min(O[j],stop)*(1-SLIP);gross+=rem*(q/entry-1);cost+=rem*(FEE+stress/2)*q/entry;rem=0.;reason='STOP';break
            if stype=='PART' and not part:
                one=entry*(1+risk)
                if H[j]>=one:
                    q=one*(1-SLIP);gross+=.4*(q/entry-1);cost+=.4*(FEE+stress/2)*q/entry;rem=.6;part=True;stop=entry
            if H[j]>=target:
                q=target*(1-SLIP);gross+=rem*(q/entry-1);cost+=rem*(FEE+stress/2)*q/entry;rem=0.;reason='TARGET';break
            j+=1
        if rem:
            j=min(j,last);q=C[j]*(1-SLIP);gross+=rem*(q/entry-1);cost+=rem*(FEE+stress/2)*q/entry
        out.append({'opened':int(T[i]),'closed':int(T[j]),'net':float(gross-cost),'reason':reason})
        free=j+COOLDOWN
    return out

def disc(x,ev,mg,stress=0.):
    return {str(y):met(sim(x,ev,mg,f'{y}-01-01',f'{y+1}-01-01',stress)) for y in (2022,2023)}

def search(asset,x):
    cand=[]
    for c in cfgs():
      ev=evs(x,c)
      if len(ev)<16:continue
      for mg in MGT:
        d=disc(x,ev,mg,0.);ds=disc(x,ev,mg,STRESS)
        if any(d[str(y)]['n']<5 or (d[str(y)]['avg'] or -9)<=0 for y in (2022,2023)):continue
        if any((ds[str(y)]['avg'] or -9)<=0 for y in (2022,2023)):continue
        v24=met(sim(x,ev,mg,'2024-01-01','2025-01-01',0.));s24=met(sim(x,ev,mg,'2024-01-01','2025-01-01',STRESS))
        v25=met(sim(x,ev,mg,'2025-01-01','2026-01-01',0.));s25=met(sim(x,ev,mg,'2025-01-01','2026-01-01',STRESS))
        if min(v24['n'],v25['n'])<4:continue
        if min(v24['avg'] or -9,v25['avg'] or -9)<=0 or min(s24['avg'] or -9,s25['avg'] or -9)<=0:continue
        if min(v24['pf'],v25['pf'])<1.05:continue
        score=120*min(v24['avg'],v25['avg'])+.35*min(v24['win_rate'],v25['win_rate'])+.22*min(v24['pf'],v25['pf'])
        cand.append((score,c,mg,ev,d,ds,v24,s24,v25,s25))
    cand.sort(key=lambda z:z[0],reverse=True)
    frozen=[];seen=set()
    for z in cand:
        c=z[1];mg=z[2];key=(c['reg'],c['cross'],c['range'],mg[0])
        if key in seen:continue
        seen.add(key);frozen.append(z)
        if len(frozen)>=20:break
    out=[]
    for rank,z in enumerate(frozen,1):
        sc,c,mg,ev,d,ds,v24,s24,v25,s25=z
        test=met(sim(x,ev,mg,'2026-04-04','2026-10-04',0.));stress=met(sim(x,ev,mg,'2026-04-04','2026-10-04',STRESS))
        blocks=[met(sim(x,ev,mg,a,b,0.)) for a,b in [('2026-04-04','2026-06-01'),('2026-06-01','2026-08-01'),('2026-08-01','2026-10-04')]]
        pb=sum((m['avg'] or -9)>0 for m in blocks)
        passed=test['n']>=6 and (test['avg'] or -9)>0 and test['pf']>=1.20 and (stress['avg'] or -9)>0 and stress['pf']>=1.10 and pb>=2
        out.append({'rank':rank,'rule':c,'management':{'name':mg[0],'rr':mg[1],'hold':mg[2],'stop':mg[3]},
                    'discovery':d,'discovery_stress':ds,'val2024':v24,'val2024_stress':s24,'val2025':v25,'val2025_stress':s25,
                    'test2026':test,'test_stress':stress,'blocks':blocks,'positive_blocks':pb,'passed':passed})
    print(asset,'ACCUM candidates',len(cand),'frozen',len(out),'passes',sum(r['passed'] for r in out),flush=True)
    return out

def main():
    asset=os.environ.get('RESEARCH_ASSET','ETH').upper();raw=load(asset);other=load('BTC' if asset=='ETH' else 'ETH')
    x=add(raw,features(raw),other);rows=search(asset,x)
    out={'generated_at':datetime.now(timezone.utc).isoformat(),'asset':asset,'method':'Long accumulation/compression breakout; discovery 2022-23, validation 2024/25, 2026 evaluation.',
         'finalists':rows,'passes':[r for r in rows if r['passed']]}
    (OUT/'result.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False,default=jd))
    print('VERITAS_ACCUM_PASSES='+json.dumps(out['passes'][:8],ensure_ascii=False,separators=(',',':'),default=jd),flush=True)

if __name__=='__main__':main()
