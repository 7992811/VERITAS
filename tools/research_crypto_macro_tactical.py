"""VERITAS macro-to-micro tactical trend system.

One fixed rule architecture, no threshold search:
1W macro direction -> 1D confirmation -> 4H trend -> 1H pullback ->
15m structural re-break -> 1m delayed execution.

Research only. Evaluated unchanged over calendar years 2020-2026.
"""
from __future__ import annotations
import json, math
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd

from research_crypto_manager_library import (
    load, rs, atr, FEE, SLIP, FUND_LONG, FUND_SHORT, YEAR_MIN, met, ts
)
from research_crypto_external_holdout import load_old

OUT=Path("macro_tactical_out");OUT.mkdir(exist_ok=True)
MAX_HOLD=14*1440
COOLDOWN=60

def jd(o):
    if isinstance(o,np.integer):return int(o)
    if isinstance(o,np.floating):return float(o)
    if isinstance(o,np.bool_):return bool(o)
    raise TypeError(type(o).__name__)

def combine(asset):
    old=load_old(asset);new=load(asset)
    f=pd.concat([old,new],ignore_index=True).drop_duplicates('ts').sort_values('ts').reset_index(drop=True)
    return f[(f.ts>=ts('2019-01-01T00:00:00Z'))&(f.ts<ts('2026-10-04T00:00:00Z'))].reset_index(drop=True)

def ema_trend(z,fast,slow,slope_n=2):
    z=z.copy();z['ef']=z.close.ewm(span=fast,adjust=False).mean();z['es']=z.close.ewm(span=slow,adjust=False).mean()
    z['trend']=np.select([
        (z.close>z.ef)&(z.ef>z.es)&(z.ef>z.ef.shift(slope_n)),
        (z.close<z.ef)&(z.ef<z.es)&(z.ef<z.ef.shift(slope_n))
    ],[1,-1],default=0)
    z['atr']=atr(z,20)
    return z

def resample_direct(raw,m):
    x=raw.copy();x.index=pd.to_datetime(x.ts,unit='s',utc=True);g=x.resample(f'{m}min',closed='left',label='right')
    z=g.agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'});cnt=g.size()
    z=z[cnt>=max(1,int(m*.95))].copy()
    return z

def align(z,t):
    return z.reindex(pd.to_datetime(t,unit='s',utc=True),method='ffill').reset_index(drop=True)

def features(raw):
    x=raw.copy()
    m15=ema_trend(resample_direct(raw,15),18,50,2)
    h1=ema_trend(resample_direct(raw,60),18,50,2)
    h4=ema_trend(resample_direct(raw,240),18,50,2)
    d1=ema_trend(resample_direct(raw,1440),18,50,2)
    w1=ema_trend(resample_direct(raw,10080),13,26,2)

    # 15m trigger features.
    m15['hi4']=m15.high.rolling(4).max().shift(1);m15['lo4']=m15.low.rolling(4).min().shift(1)
    m15['vmed20']=m15.volume.rolling(20).median().shift(1);m15['vr']=m15.volume/m15.vmed20.replace(0,np.nan)
    m15['atr_med48']=m15.atr.rolling(48).median().shift(1);m15['ar']=m15.atr/m15.atr_med48.replace(0,np.nan)
    span=(m15.high-m15.low).replace(0,np.nan);m15['bull']=(m15.close-m15.open)/span;m15['bear']=(m15.open-m15.close)/span

    # 1h pullback, 4h structural stop.
    h1['pbL']=(h1.low<=h1.ef).rolling(3).max().shift(1);h1['pbS']=(h1.high>=h1.ef).rolling(3).max().shift(1)
    h4['lo3']=h4.low.rolling(3).min().shift(1);h4['hi3']=h4.high.rolling(3).max().shift(1)

    for tag,z,cols in [
        ('m15',m15,['close','atr','trend','hi4','lo4','vr','ar','bull','bear']),
        ('h1',h1,['close','atr','trend','pbL','pbS']),
        ('h4',h4,['close','atr','trend','lo3','hi3']),
        ('d1',d1,['close','atr','trend']),
        ('w1',w1,['close','atr','trend']),
    ]:
        a=align(z,x.ts.to_numpy())
        for c in cols:x[f'{tag}_{c}']=a[c].to_numpy()
    return x

def events(x):
    out=[]
    # signal only when a new completed 15m value arrives: minute timestamp divisible by 900.
    boundary=(x.ts%900==0)
    for d in (1,-1):
        lvl=x.m15_hi4 if d>0 else x.m15_lo4
        # Compare last completed 15m close with its preceding boundary.
        prev_close=x.m15_close.shift(15);prev_lvl=lvl.shift(15)
        crossed=(d*(x.m15_close-lvl)>0)&(d*(prev_close-prev_lvl)<=0)
        body=x.m15_bull if d>0 else x.m15_bear
        pb=(x.h1_pbL>0) if d>0 else (x.h1_pbS>0)
        m=boundary&(x.w1_trend==d)&(x.d1_trend==d)&(x.h4_trend==d)&(x.h1_trend!=-d)&pb
        m&=crossed&(x.m15_vr>=1.15)&(body>=.45)&(x.m15_ar>=.85)&(x.m15_ar<=1.55)
        ext=d*(x.m15_close-lvl)/x.m15_atr
        m&=(ext>.0)&(ext<=.30)
        stopref=x.h4_lo3 if d>0 else x.h4_hi3
        for i in np.flatnonzero(m.fillna(False).to_numpy()):
            if np.isfinite(stopref.iloc[i]) and np.isfinite(x.h4_atr.iloc[i]):
                out.append((int(i),d,float(stopref.iloc[i]),float(x.h4_atr.iloc[i])))
    out.sort();return out

def sim(x,ev,start,end,stress=0.):
    T=x.ts.to_numpy();O=x.open.to_numpy();H=x.high.to_numpy();L=x.low.to_numpy();C=x.close.to_numpy()
    st=ts(start);en=ts(end);ei=int(np.searchsorted(T,en));out=[];free=-1
    for sig,d,sr,a4 in ev:
        if T[sig]<st or T[sig]>=en-120:continue
        # One full minute delay after the completed 15m signal.
        i=int(np.searchsorted(T,T[sig]+60))
        if i<free or i>=ei:continue
        entry=O[i]*(1+d*SLIP);stop=sr-d*.10*a4;risk=d*(entry-stop)/entry
        if not(.006<=risk<=.06):continue
        gross=0.;cost=FEE+stress/2;rem=1.;part=False;reason='TIME';j=i;last=min(ei-1,i+MAX_HOLD)
        t3=entry*(1+d*3*risk)
        while j<=last:
            if j>i:cost+=rem*(FUND_LONG if d>0 else FUND_SHORT)/YEAR_MIN
            if (L[j]<=stop if d>0 else H[j]>=stop):
                q=(min(O[j],stop) if d>0 else max(O[j],stop))*(1-d*SLIP)
                gross+=rem*d*(q/entry-1);cost+=rem*(FEE+stress/2)*q/entry;rem=0.;reason='STOP';break
            if not part:
                one=entry*(1+d*risk)
                if (H[j]>=one if d>0 else L[j]<=one):
                    q=one*(1-d*SLIP);gross+=.5*d*(q/entry-1);cost+=.5*(FEE+stress/2)*q/entry
                    rem=.5;part=True;stop=entry
            if part and (H[j]>=t3 if d>0 else L[j]<=t3):
                q=t3*(1-d*SLIP);gross+=rem*d*(q/entry-1);cost+=rem*(FEE+stress/2)*q/entry;rem=0.;reason='TARGET3';break
            # Trail remaining half using latest completed 4h swing, causal.
            if part and j>i+240:
                tr=(x.h4_lo3.iloc[j] if d>0 else x.h4_hi3.iloc[j])-d*.05*x.h4_atr.iloc[j]
                if np.isfinite(tr) and d*(tr-stop)>0 and d*(C[j]-tr)>.10*x.h4_atr.iloc[j]:stop=tr
            j+=1
        if rem:
            j=min(j,last);q=C[j]*(1-d*SLIP);gross+=rem*d*(q/entry-1);cost+=rem*(FEE+stress/2)*q/entry
        out.append({'opened':int(T[i]),'closed':int(T[j]),'d':d,'net':float(gross-cost),'reason':reason})
        free=j+COOLDOWN
    return out

def run(asset):
    raw=combine(asset);x=features(raw);ev=events(x)
    years={}
    for y in range(2020,2027):
        end=f'{y+1}-01-01T00:00:00Z' if y<2026 else '2026-10-04T00:00:00Z'
        base=met(sim(x,ev,f'{y}-01-01T00:00:00Z',end,0.))
        st=met(sim(x,ev,f'{y}-01-01T00:00:00Z',end,.0005))
        years[str(y)]={'base':base,'stress5':st}
    alltr=sim(x,ev,'2020-01-01T00:00:00Z','2026-10-04T00:00:00Z',0.)
    alls=sim(x,ev,'2020-01-01T00:00:00Z','2026-10-04T00:00:00Z',.0005)
    out={'asset':asset,'generated_at':datetime.now(timezone.utc).isoformat(),
         'rule':'1W+1D+4H trend; 1H pullback; 15m breakout; 1m delayed execution; half at 1R, remainder 3R/4H trail.',
         'events':len(ev),'years':years,'aggregate':met(alltr),'aggregate_stress5':met(alls)}
    p=OUT/asset.lower();p.mkdir(parents=True,exist_ok=True);(p/'result.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False,default=jd))
    print('VERITAS_MACRO_TACTICAL='+json.dumps(out,separators=(',',':'),default=jd),flush=True)

def main():
    import argparse
    ap=argparse.ArgumentParser();ap.add_argument('--asset',choices=['BTC','ETH'],required=True);args=ap.parse_args();run(args.asset)

if __name__=='__main__':main()
