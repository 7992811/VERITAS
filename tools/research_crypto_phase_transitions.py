"""VERITAS phase-transition research: sequence of market changes, not static snapshots.

Research-only. Causal 1m data with 5m/15m/1h/4h/1d context.
Candidate rules describe transitions:
  COMPRESSION_BREAK: quiet -> repeated level tests -> vol/volume expansion -> break.
  PULLBACK_CONT: impulse -> controlled lower-volume retracement -> micro break -> re-expansion.
  TREND_ACCEL: established efficient trend -> shallow structure -> renewed acceleration.
  EARLY_REV: stretched old trend -> 15m reversal -> 1m structural confirmation.

All rule/parameter selection uses 2022-2025 only. Apr-Oct 2026 is held out.
"""
from __future__ import annotations
import gc, json, math
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd

from research_crypto_manager_library import (
    load, features, atr, ts, met,
    HIST_START, SELECT_END, TEST_START, TEST_END,
    FEE, SLIP, FUND_LONG, FUND_SHORT, YEAR_MIN
)

OUT=Path("phase_transition_out"); OUT.mkdir(exist_ok=True)
STRESS=.0005
MAX_HOLD=1440
COOLDOWN=15

def json_default(o):
    if isinstance(o,np.integer): return int(o)
    if isinstance(o,np.floating): return float(o)
    if isinstance(o,np.bool_): return bool(o)
    raise TypeError(type(o).__name__)

def add_sequence_features(raw,x,other):
    # 1m volatility path.
    r=raw.close.pct_change()
    rv30=r.rolling(30,min_periods=30).std().shift(1)
    rv120=r.rolling(120,min_periods=120).std().shift(1)
    x['rv_ratio']=rv30/rv120.replace(0,np.nan)
    x['rv_ratio_pre5']=x.rv_ratio.shift(5)

    # Volume drying before a trigger: last 10m median vs prior 30m median.
    x['vol_med10']=raw.volume.rolling(10).median().shift(1)
    x['vol_med_prev30']=raw.volume.shift(10).rolling(30).median()
    x['vol_contract']=x.vol_med10/x.vol_med_prev30.replace(0,np.nan)

    # Path efficiency / approach speed.
    absret=r.abs()
    x['eff15_1m']=(raw.close/raw.close.shift(15)-1).abs()/absret.rolling(15).sum().replace(0,np.nan)
    x['eff30_1m']=(raw.close/raw.close.shift(30)-1).abs()/absret.rolling(30).sum().replace(0,np.nan)
    x['atr1']=atr(raw,20)
    x['atr1_med120']=x.atr1.rolling(120).median().shift(1)
    x['atr1_ratio']=x.atr1/x.atr1_med120.replace(0,np.nan)

    # Repeated tests of the current evolving 60m boundary.
    hi60=raw.high.rolling(60).max().shift(1); lo60=raw.low.rolling(60).min().shift(1)
    eps=.12*x.m5_atr
    near_hi=(raw.high>=hi60-eps).astype(float)
    near_lo=(raw.low<=lo60+eps).astype(float)
    x['tests_hi60']=near_hi.rolling(60).sum().shift(1)
    x['tests_lo60']=near_lo.rolling(60).sum().shift(1)

    # Simple directional structure from three successive 10m extrema blocks.
    l0=raw.low.rolling(10).min().shift(1)
    l1=raw.low.shift(10).rolling(10).min()
    l2=raw.low.shift(20).rolling(10).min()
    h0=raw.high.rolling(10).max().shift(1)
    h1=raw.high.shift(10).rolling(10).max()
    h2=raw.high.shift(20).rolling(10).max()
    x['higher_lows']=(l0>l1)&(l1>l2)
    x['lower_highs']=(h0<h1)&(h1<h2)

    # Prior impulse and controlled pullback. Use fixed 40m impulse/pullback geometry.
    anchorL=raw.close.shift(40)
    peak=raw.high.shift(10).rolling(30).max()
    pb_low=raw.low.rolling(10).min().shift(1)
    impL=(peak-anchorL).clip(lower=0)
    x['impulseL_atr']=impL/x.m5_atr.replace(0,np.nan)
    x['pullbackL_depth']=(peak-pb_low)/impL.replace(0,np.nan)

    anchorS=raw.close.shift(40)
    trough=raw.low.shift(10).rolling(30).min()
    pb_high=raw.high.rolling(10).max().shift(1)
    impS=(anchorS-trough).clip(lower=0)
    x['impulseS_atr']=impS/x.m5_atr.replace(0,np.nan)
    x['pullbackS_depth']=(pb_high-trough)/impS.replace(0,np.nan)

    # 15m trend transition observed causally through repeated aligned values.
    x['m15_prev']=x.m15_trend.shift(15)
    x['h1_prev']=x.h1_trend.shift(60)

    # Cross-market momentum.
    o=other[['ts','close']].copy()
    for n in (60,240,1440):
        o[f'ret{n}']=o.close/o.close.shift(n)-1
    o.index=pd.to_datetime(o.ts,unit='s',utc=True)
    a=o.reindex(pd.to_datetime(x.ts,unit='s',utc=True),method='ffill')
    for n in (60,240,1440): x[f'cross_ret{n}']=a[f'ret{n}'].to_numpy()
    return x

def align_mask(x,d,mode):
    if mode=='EARLY':
        return (x.m15_trend==d)&(x.h1_trend==d)&(x.h4_trend!=-d)&(x.d1_trend!=-d)
    if mode=='CONF':
        return (x.h1_trend==d)&(x.h4_trend==d)&(x.d1_trend!=-d)
    if mode=='MOM':
        vote=np.sign(x.ret60)+np.sign(x.ret240)+np.sign(x.ret1440)
        return vote>=2 if d>0 else vote<=-2
    if mode=='TRANS':
        return (x.m15_trend==d)&(x.m15_prev!=d)&(x.h1_trend!=-d)&(x.h4_trend!=-d)
    raise ValueError(mode)

def cross_mask(x,d,mode):
    if mode=='NONE': return pd.Series(True,index=x.index)
    if mode=='X4': return d*x.cross_ret240>0
    if mode=='X14': return (d*x.cross_ret60>0)&(d*x.cross_ret240>0)
    raise ValueError(mode)

def structure_mask(x,d):
    return x.higher_lows if d>0 else x.lower_highs

def level_series(x,d,n):
    return x[f'hi{n}'] if d>0 else x[f'lo{n}']

def trigger_common(x,d,lookback,vol_thr,body_thr,extmax):
    level=level_series(x,d,lookback)
    cross=(d*(x.close-level)>0)&(d*(x.close.shift(1)-level)<=0)
    body=x.bull if d>0 else x.bear
    ext=d*(x.close-level)/x.m5_atr
    return level,cross&(x.vr>=vol_thr)&(body>=body_thr)&(ext>.002)&(ext<=extmax)&x.contig1440,ext

def templates():
    out=[]
    # Theory-led profiles, no giant Cartesian threshold mine.
    for mode in ('EARLY','CONF','MOM'):
      for cross in ('NONE','X4','X14'):
        out += [
          dict(family='COMPRESSION_BREAK',mode=mode,cross=cross,lb=20,comp=.75,tests=2,vol=1.35,body=.50,ar=1.05,ext=.30),
          dict(family='COMPRESSION_BREAK',mode=mode,cross=cross,lb=30,comp=.85,tests=3,vol=1.55,body=.60,ar=1.10,ext=.25),
          dict(family='COMPRESSION_BREAK',mode=mode,cross=cross,lb=20,comp=.65,tests=2,vol=1.70,body=.65,ar=1.15,ext=.20),
        ]
    for mode in ('EARLY','CONF','MOM'):
      for cross in ('NONE','X4','X14'):
        out += [
          dict(family='PULLBACK_CONT',mode=mode,cross=cross,lb=5,imp=.75,dlo=.18,dhi=.65,vcon=1.00,vol=1.25,body=.40,ar=.95,ext=.30),
          dict(family='PULLBACK_CONT',mode=mode,cross=cross,lb=10,imp=1.00,dlo=.25,dhi=.60,vcon=.90,vol=1.45,body=.50,ar=1.05,ext=.25),
          dict(family='PULLBACK_CONT',mode=mode,cross=cross,lb=5,imp=1.25,dlo=.20,dhi=.50,vcon=.85,vol=1.60,body=.60,ar=1.10,ext=.20),
        ]
    for mode in ('CONF','MOM'):
      for cross in ('NONE','X4','X14'):
        out += [
          dict(family='TREND_ACCEL',mode=mode,cross=cross,lb=10,eff=.30,vol=1.25,body=.45,ar=1.00,ext=.30),
          dict(family='TREND_ACCEL',mode=mode,cross=cross,lb=20,eff=.40,vol=1.45,body=.55,ar=1.08,ext=.25),
          dict(family='TREND_ACCEL',mode=mode,cross=cross,lb=10,eff=.50,vol=1.65,body=.60,ar=1.15,ext=.20),
        ]
    for cross in ('NONE','X4'):
        out += [
          dict(family='EARLY_REV',mode='TRANS',cross=cross,lb=5,vol=1.35,body=.45,ar=.95,ext=.30,stretch=1.0),
          dict(family='EARLY_REV',mode='TRANS',cross=cross,lb=10,vol=1.55,body=.55,ar=1.05,ext=.25,stretch=1.5),
        ]
    return out

def make_events(x,c):
    events=[]
    for d in (1,-1):
        level,base,ext=trigger_common(x,d,c['lb'],c['vol'],c['body'],c['ext'])
        mask=base&align_mask(x,d,c['mode'])&cross_mask(x,d,c['cross'])
        fam=c['family']
        if fam=='COMPRESSION_BREAK':
            tests=x.tests_hi60 if d>0 else x.tests_lo60
            mask&=(x.rv_ratio_pre5<=c['comp'])&(tests>=c['tests'])&(x.m5_atr_ratio>=c['ar'])&(x.vol_contract<=1.10)
        elif fam=='PULLBACK_CONT':
            imp=x.impulseL_atr if d>0 else x.impulseS_atr
            dep=x.pullbackL_depth if d>0 else x.pullbackS_depth
            mask&=(imp>=c['imp'])&(dep>=c['dlo'])&(dep<=c['dhi'])&(x.vol_contract<=c['vcon'])&(x.m5_atr_ratio>=c['ar'])
        elif fam=='TREND_ACCEL':
            eff=np.maximum(x.eff15_1m.fillna(0),x.eff30_1m.fillna(0))
            mask&=structure_mask(x,d)&(eff>=c['eff'])&(x.m5_atr_ratio>=c['ar'])&(x.rv_ratio>=1.0)
        elif fam=='EARLY_REV':
            # Old trend was opposite on 15m, now 15m flipped; 1h not opposing.
            dist=d*(x.close-x.h1_sma18)/x.h1_atr.replace(0,np.nan)
            mask&=(x.m15_prev==-d)&(x.m15_trend==d)&(x.h1_trend!=-d)&(dist>=-c['stretch'])&(x.m5_atr_ratio>=c['ar'])
        else: raise ValueError(fam)
        stopref=x.lo30 if d>0 else x.hi30
        for i in np.flatnonzero(mask.fillna(False).to_numpy()):
            vals=(level.iloc[i],stopref.iloc[i],x.m5_atr.iloc[i],ext.iloc[i])
            if all(np.isfinite(v) for v in vals):
                events.append((int(i),d,float(vals[0]),float(vals[1]),float(vals[2]),fam))
    events.sort()
    return events

def sim(x,ev,buf,rr,mode,start,end,stress=0.):
    T=x.ts.to_numpy();O=x.open.to_numpy();H=x.high.to_numpy();L=x.low.to_numpy();C=x.close.to_numpy();A=x.m5_atr.to_numpy()
    out=[];next_i=-1;i1=int(np.searchsorted(T,end))
    for sig,d,level,sr,av,fam in ev:
        if T[sig]<start or T[sig]>=end-60: continue
        i=sig+1
        if i<next_i or i>=i1: continue
        entry=O[i]*(1+d*SLIP);stop=sr-d*buf*av;risk=d*(entry-stop)/entry;extension=d*(entry-level)/av
        if not (max(.003,2.5*(2*(FEE+SLIP)+stress))<=risk<=.035 and -.10<=extension<=.70):continue
        target=entry*(1+d*risk*rr);gross=0.;cost=FEE+stress/2;rem=1.;part=False;reason='TIME'
        j=i;last=min(i1-1,i+MAX_HOLD)
        while j<=last:
            if j>i:cost+=rem*(FUND_LONG if d>0 else FUND_SHORT)/YEAR_MIN
            if (L[j]<=stop if d>0 else H[j]>=stop):
                q=(min(O[j],stop) if d>0 else max(O[j],stop))*(1-d*SLIP)
                gross+=rem*d*(q/entry-1);cost+=rem*(FEE+stress/2)*q/entry;rem=0.;reason='STOP';break
            if mode=='PARTIAL' and not part:
                one=entry*(1+d*risk)
                if (H[j]>=one if d>0 else L[j]<=one):
                    q=one*(1-d*SLIP);qty=.5;gross+=qty*d*(q/entry-1);cost+=qty*(FEE+stress/2)*q/entry;rem-=qty;part=True;stop=entry
            if (H[j]>=target if d>0 else L[j]<=target):
                q=target*(1-d*SLIP);gross+=rem*d*(q/entry-1);cost+=rem*(FEE+stress/2)*q/entry;rem=0.;reason='TARGET';break
            if mode=='TRAIL' and j>i+15:
                a=max(i,j-30);tr=(np.min(L[a:j]) if d>0 else np.max(H[a:j]))-d*.10*A[j]
                if np.isfinite(tr) and d*(tr-stop)>0 and d*(C[j]-tr)>.15*A[j]:stop=tr
            j+=1
        if rem:
            j=min(j,last);q=C[j]*(1-d*SLIP);gross+=rem*d*(q/entry-1);cost+=rem*(FEE+stress/2)*q/entry
        out.append({'opened':int(T[i]),'closed':int(T[j]),'d':d,'net':float(gross-cost),'family':fam,'reason':reason})
        next_i=j+COOLDOWN
    return out

EXITS=[
 ('F1',.10,1.0,'FIXED'),('F15',.10,1.5,'FIXED'),('F2',.10,2.0,'FIXED'),
 ('W15',.20,1.5,'FIXED'),('W2',.20,2.0,'FIXED'),('W25',.20,2.5,'FIXED'),
 ('P2',.20,2.0,'PARTIAL'),('T2',.20,2.0,'TRAIL')
]

def yearly(x,ev,p,stress=0.):
    return {str(y):met(sim(x,ev,start=ts(f'{y}-01-01T00:00:00Z'),end=ts(f'{y+1}-01-01T00:00:00Z'),stress=stress,**p)) for y in (2022,2023,2024,2025)}

def stable(yrs,syrs):
    vals=[m for m in yrs.values() if m['n']>=7];sv=[m for m in syrs.values() if m['n']>=7]
    if len(vals)<3:return False
    if sum((m['avg'] or -9)>0 for m in vals)<3:return False
    if sum((m['avg'] or -9)>0 for m in sv)<3:return False
    if sum(m['n'] for m in vals)<45:return False
    if min(m['pf'] for m in vals)<1.0:return False
    if min(m['pf'] for m in sv)<.95:return False
    return True

def rank(yrs,syrs):
    vals=[m for m in yrs.values() if m['n']>=7];sv=[m for m in syrs.values() if m['n']>=7]
    return min(m['pf'] for m in vals)+.30*min(m['pf'] for m in sv)+90*min(m['avg'] for m in vals)+.35*np.median([m['win_rate'] for m in vals])

def research(asset,x):
    candidates=[]
    for c in templates():
        ev=make_events(x,c)
        if len(ev)<35: continue
        for _,buf,rr,mode in EXITS:
            p=dict(buf=buf,rr=rr,mode=mode)
            yrs=yearly(x,ev,p,0.);syrs=yearly(x,ev,p,STRESS)
            if not stable(yrs,syrs):continue
            candidates.append((rank(yrs,syrs),c,p,ev,yrs,syrs))
    candidates.sort(key=lambda z:z[0],reverse=True)
    frozen=[];seen=set()
    for z in candidates:
        c=z[1];p=z[2];key=(c['family'],c['mode'],c['cross'],p['rr'],p['mode'])
        if key in seen:continue
        seen.add(key);frozen.append(z)
        if len(frozen)>=24:break
    out=[]
    for rank_i,z in enumerate(frozen,1):
        sc,c,p,ev,yrs,syrs=z
        test=met(sim(x,ev,start=ts(TEST_START),end=ts(TEST_END),stress=0.,**p))
        stress=met(sim(x,ev,start=ts(TEST_START),end=ts(TEST_END),stress=STRESS,**p))
        blocks=[met(sim(x,ev,start=ts(a+'T00:00:00Z'),end=ts(b+'T00:00:00Z'),stress=0.,**p)) for a,b in
                [('2026-04-04','2026-06-01'),('2026-06-01','2026-08-01'),('2026-08-01','2026-10-04')]]
        pb=sum((m['avg'] or -9)>0 for m in blocks)
        passed=(test['n']>=12 and (test['avg'] or -9)>0 and test['pf']>=1.25 and
                (stress['avg'] or -9)>0 and stress['pf']>=1.15 and pb>=2)
        out.append({'rank':rank_i,'rule':c,'params':p,'selection_years':yrs,'selection_stress':syrs,
                    'test_2026':test,'test_stress':stress,'blocks':blocks,'positive_blocks':pb,'passed':passed})
    print(asset,'phase_candidates',len(candidates),'frozen',len(out),'passes',sum(r['passed'] for r in out),flush=True)
    return out

def main():
    btc=load('BTC');eth=load('ETH')
    out={'generated_at':datetime.now(timezone.utc).isoformat(),
         'method':'Causal phase-transition templates; selection 2022-2025 only; 2026 held out.',
         'assets':{}}
    for asset,raw,other in [('ETH',eth,btc),('BTC',btc,eth)]:
        x=features(raw);x=add_sequence_features(raw,x,other)
        out['assets'][asset]=research(asset,x)
        del x;gc.collect()
    out['passes']={a:[r for r in rows if r['passed']] for a,rows in out['assets'].items()}
    (OUT/'result.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False,default=json_default))
    print('VERITAS_PHASE_PASSES='+json.dumps({a:v[:8] for a,v in out['passes'].items()},ensure_ascii=False,separators=(',',':'),default=json_default),flush=True)

if __name__=='__main__':main()
