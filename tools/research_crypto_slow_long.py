"""VERITAS slow 4h/1d LONG trend research with 1m execution.

Research-only. Long-term trend regime is defined on completed 4h/daily bars.
Entry waits for a 1h pullback/recovery and 1m confirmation. Exit is not a short
static TP only: tests weekly/multi-week trailing / regime exits.

Selection: 2022-23 discovery, 2024 and 2025 independent validation, 2026 research test.
"""
from __future__ import annotations
import gc, json, os
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd

from research_crypto_manager_library import load, features, rs, align, ts, met, FEE, SLIP, FUND_LONG, YEAR_MIN

OUT=Path("slow_long_out"); OUT.mkdir(exist_ok=True)
STRESS=.0005

def jd(o):
    if isinstance(o,np.integer):return int(o)
    if isinstance(o,np.floating):return float(o)
    if isinstance(o,np.bool_):return bool(o)
    raise TypeError(type(o).__name__)

def add(raw,x,other):
    h1=rs(raw,60);h4=rs(raw,240);d1=rs(raw,1440)
    for z in (h1,h4,d1):
        z['slope18_6']=z.sma18-z.sma18.shift(6)
        z['dist18']=(z.close-z.sma18)/z.atr.replace(0,np.nan)
    h1['pb18']=(h1.low<=h1.sma18).rolling(4).max().shift(1)
    h1['pb50']=(h1.low<=h1.sma50).rolling(8).max().shift(1)
    h1['reclaim18']=(h1.close>h1.sma18)&(h1.close.shift(1)<=h1.sma18.shift(1))
    for n in (6,12,24,48):
        h1[f'lo{n}']=h1.low.rolling(n).min().shift(1)
    h4['exit18']=(h4.close<h4.sma18)
    h4['exit50']=(h4.close<h4.sma50)
    ah1=align(h1,x.ts.to_numpy());ah4=align(h4,x.ts.to_numpy());ad1=align(d1,x.ts.to_numpy())
    x['h1_pb18']=ah1.pb18.fillna(0).to_numpy()>0
    x['h1_pb50']=ah1.pb50.fillna(0).to_numpy()>0
    x['h1_reclaim18']=ah1.reclaim18.fillna(False).to_numpy(dtype=bool)
    for n in (6,12,24,48):x[f'h1_lo{n}']=ah1[f'lo{n}'].to_numpy()
    x['h4_slope18_6']=ah4.slope18_6.to_numpy();x['d1_slope18_6']=ad1.slope18_6.to_numpy()
    x['h4_dist18']=ah4.dist18.to_numpy();x['d1_dist18']=ad1.dist18.to_numpy()
    x['h4_exit18']=ah4.exit18.fillna(False).to_numpy(dtype=bool)
    x['h4_exit50']=ah4.exit50.fillna(False).to_numpy(dtype=bool)
    for n in (20,60):x[f'hi{n}']=raw.high.rolling(n).max().shift(1)

    # Cross-market trend context
    o=other[['ts','close']].copy()
    for n in (240,1440):o[f'ret{n}']=o.close/o.close.shift(n)-1
    o.index=pd.to_datetime(o.ts,unit='s',utc=True)
    ao=o.reindex(pd.to_datetime(x.ts,unit='s',utc=True),method='ffill')
    x['other240']=ao.ret240.to_numpy();x['other1440']=ao.ret1440.to_numpy()
    return x

def profiles():
    out=[]
    for cross in ('NONE','CONF4H','CONFD1'):
      out += [
       dict(name='CORE20',cross=cross,lb=20,vol_lo=1.10,vol_hi=2.5,body=.40,ar_lo=.80,ar_hi=1.40,ext=.30,pb='18'),
       dict(name='QUAL20',cross=cross,lb=20,vol_lo=1.35,vol_hi=2.3,body=.50,ar_lo=.90,ar_hi=1.35,ext=.25,pb='18'),
       dict(name='CORE60',cross=cross,lb=60,vol_lo=1.20,vol_hi=2.5,body=.45,ar_lo=.85,ar_hi=1.40,ext=.25,pb='18'),
       dict(name='DEEP20',cross=cross,lb=20,vol_lo=1.20,vol_hi=2.5,body=.45,ar_lo=.85,ar_hi=1.40,ext=.30,pb='50'),
      ]
    return out

def evs(x,p):
    lvl=x[f'hi{p["lb"]}'];cross=(x.close>lvl)&(x.close.shift(1)<=lvl);ext=(x.close-lvl)/x.m5_atr
    # slow bull regime; H1 may be recovering from correction
    m=(x.h4_trend==1)&(x.d1_trend==1)&(x.h4_slope18_6>0)&(x.d1_slope18_6>0)
    m&=(x.h1_trend!=-1)&(x.h1_pb18 if p['pb']=='18' else x.h1_pb50)
    m&=cross&(x.vr>=p['vol_lo'])&(x.vr<=p['vol_hi'])&(x.bull>=p['body'])
    m&=(x.m5_atr_ratio>=p['ar_lo'])&(x.m5_atr_ratio<=p['ar_hi'])&(ext>.001)&(ext<=p['ext'])&x.contig1440
    # Don't enter if already far above daily value.
    m&=(x.d1_dist18<=2.5)
    if p['cross']=='CONF4H':m&=(x.other240>0)
    elif p['cross']=='CONFD1':m&=(x.other1440>0)
    out=[]
    for i in np.flatnonzero(m.fillna(False).to_numpy()):
        if np.isfinite(lvl.iloc[i]):out.append((int(i),float(lvl.iloc[i])))
    return out

MGMT=[
 ('H1_12_7D',12,.10,10080,'REG18'),
 ('H1_24_14D',24,.10,20160,'REG18'),
 ('H1_24_28D',24,.10,40320,'REG18'),
 ('H1_48_28D',48,.10,40320,'REG18'),
 ('H1_24_28D_50',24,.10,40320,'REG50'),
 ('PART_H1_24_28D',24,.10,40320,'PART'),
]

def sim(x,ev,mg,start,end,stress=0.):
    name,look,buf,maxhold,mode=mg
    T=x.ts.to_numpy();O=x.open.to_numpy();H=x.high.to_numpy();L=x.low.to_numpy();C=x.close.to_numpy();A1=x.h1_atr.to_numpy();lo=x[f'h1_lo{look}'].to_numpy()
    exit18=x.h4_exit18.to_numpy();exit50=x.h4_exit50.to_numpy()
    st=ts(pd.Timestamp(start,tz='UTC'));en=ts(pd.Timestamp(end,tz='UTC'));ei=int(np.searchsorted(T,en))
    out=[];free=-1
    for sig,lvl in ev:
        if T[sig]<st or T[sig]>=en-60:continue
        i=sig+1
        if i<free or i>=ei:continue
        sr=lo[sig]
        if not np.isfinite(sr) or not np.isfinite(A1[sig]):continue
        entry=O[i]*(1+SLIP);stop=sr-buf*A1[sig];risk=(entry-stop)/entry
        if not(.005<=risk<=.10):continue
        cost=FEE+stress/2;gross=0.;rem=1.;part=False;j=i;last=min(ei-1,i+maxhold);reason='TIME'
        while j<=last:
            if j>i:cost+=rem*FUND_LONG/YEAR_MIN
            if L[j]<=stop:
                q=min(O[j],stop)*(1-SLIP);gross+=rem*(q/entry-1);cost+=rem*(FEE+stress/2)*q/entry;rem=0.;reason='STOP';break
            if mode=='PART' and not part and H[j]>=entry*(1+risk):
                q=entry*(1+risk)*(1-SLIP);gross+=.4*(q/entry-1);cost+=.4*(FEE+stress/2)*q/entry;rem=.6;part=True;stop=entry
            reg_exit=(exit18[j] if mode in ('REG18','PART') else exit50[j])
            # require position at least 4h old before regime exit to avoid same-bar noise
            if j>=i+240 and reg_exit:
                q=C[j]*(1-SLIP);gross+=rem*(q/entry-1);cost+=rem*(FEE+stress/2)*q/entry;rem=0.;reason='REGIME';break
            j+=1
        if rem:
            j=min(j,last);q=C[j]*(1-SLIP);gross+=rem*(q/entry-1);cost+=rem*(FEE+stress/2)*q/entry
        out.append({'opened':int(T[i]),'closed':int(T[j]),'net':float(gross-cost),'reason':reason})
        free=j+60
    return out

def metp(x,ev,mg,a,b,s=0.):return met(sim(x,ev,mg,a,b,s))

def search(asset,x):
    cand=[]
    for p in profiles():
      ev=evs(x,p)
      if len(ev)<12:continue
      for mg in MGMT:
        y22=metp(x,ev,mg,'2022-01-01','2023-01-01');y23=metp(x,ev,mg,'2023-01-01','2024-01-01')
        if min(y22['n'],y23['n'])<3 or min(y22['avg'] or -9,y23['avg'] or -9)<=0:continue
        v24=metp(x,ev,mg,'2024-01-01','2025-01-01');s24=metp(x,ev,mg,'2024-01-01','2025-01-01',STRESS)
        v25=metp(x,ev,mg,'2025-01-01','2026-01-01');s25=metp(x,ev,mg,'2025-01-01','2026-01-01',STRESS)
        if min(v24['n'],v25['n'])<3 or min(v24['avg'] or -9,v25['avg'] or -9)<=0:continue
        if min(s24['avg'] or -9,s25['avg'] or -9)<=0:continue
        if min(v24['pf'],v25['pf'])<1.05:continue
        score=100*min(v24['avg'],v25['avg'])+.25*min(v24['win_rate'],v25['win_rate'])+.20*min(v24['pf'],v25['pf'])
        cand.append((score,p,mg,ev,y22,y23,v24,s24,v25,s25))
    cand.sort(key=lambda z:z[0],reverse=True)
    out=[]
    for rank,z in enumerate(cand[:16],1):
        sc,p,mg,ev,y22,y23,v24,s24,v25,s25=z
        test=metp(x,ev,mg,'2026-04-04','2026-10-04');stress=metp(x,ev,mg,'2026-04-04','2026-10-04',STRESS)
        blocks=[metp(x,ev,mg,a,b) for a,b in [('2026-04-04','2026-06-01'),('2026-06-01','2026-08-01'),('2026-08-01','2026-10-04')]]
        pb=sum((m['avg'] or -9)>0 for m in blocks)
        passed=test['n']>=4 and (test['avg'] or -9)>0 and test['pf']>=1.20 and (stress['avg'] or -9)>0 and stress['pf']>=1.10 and pb>=2
        out.append({'rank':rank,'rule':p,'management':{'name':mg[0],'look_h1':mg[1],'buf':mg[2],'hold_min':mg[3],'mode':mg[4]},
                    'y2022':y22,'y2023':y23,'val2024':v24,'val2024_stress':s24,'val2025':v25,'val2025_stress':s25,
                    'test2026':test,'test_stress':stress,'blocks':blocks,'positive_blocks':pb,'passed':passed})
    print(asset,'SLOW_LONG candidates',len(cand),'finalists',len(out),'passes',sum(r['passed'] for r in out),flush=True)
    return out

def main():
    asset=os.environ.get('RESEARCH_ASSET','ETH').upper();raw=load(asset);other=load('BTC' if asset=='ETH' else 'ETH')
    x=add(raw,features(raw),other);rows=search(asset,x)
    out={'generated_at':datetime.now(timezone.utc).isoformat(),'asset':asset,'method':'Slow 4h/1d long trend with 1m execution; dual validation 2024/25.',
         'finalists':rows,'passes':[r for r in rows if r['passed']]}
    (OUT/'result.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False,default=jd))
    print('VERITAS_SLOW_LONG_PASSES='+json.dumps(out['passes'][:8],ensure_ascii=False,separators=(',',':'),default=jd),flush=True)

if __name__=='__main__':main()
