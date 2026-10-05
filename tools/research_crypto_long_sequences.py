"""VERITAS direction-specific LONG sequence research.

Goal: find robust BTC/ETH long modules without mirroring short logic.
Causal 1m execution + 5m volatility/volume + 15m/1h pullback + 4h/1d regime.

Families:
- DUAL_PULLBACK: 4h/1d or 1h/4h uptrend, both 15m and 1h pulled back, then 1m re-break.
- COMPRESSION_BREAK: higher-TF uptrend, low-vol compression, repeated resistance tests, then moderate expansion.
- RECLAIM: daily/4h uptrend, 1h correction, 15m trend recovers and 1m confirms.
- TREND_ACCEL: established trend, shallow pullback, efficient path and renewed breakout.

Selection protocol:
- discovery 2022-2023
- validation 2024
- validation 2025
- Apr-Oct 2026 evaluated only after candidates are frozen.
Research only.
"""
from __future__ import annotations
import gc, json, math, os
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd

from research_crypto_manager_library import (
    load, features, rs, align, atr, ts, met,
    FEE, SLIP, FUND_LONG, YEAR_MIN
)

OUT=Path("long_sequence_out"); OUT.mkdir(exist_ok=True)
STRESS=.0005
COOLDOWN=20
MAX_HOLD_DEFAULT=4320

def json_default(o):
    if isinstance(o,np.integer):return int(o)
    if isinstance(o,np.floating):return float(o)
    if isinstance(o,np.bool_):return bool(o)
    raise TypeError(type(o).__name__)

def add_seq(raw,x,other):
    # Missing local levels.
    for n in (5,10,20,30,60,120):
        x[f'hi{n}']=raw.high.rolling(n).max().shift(1)
        x[f'lo{n}']=raw.low.rolling(n).min().shift(1)

    # 15m / 1h pullbacks and reclaims.
    m15=rs(raw,15); h1=rs(raw,60)
    for z in (m15,h1):
        z['ema9']=z.close.ewm(span=9,adjust=False).mean()
        z['ema21']=z.close.ewm(span=21,adjust=False).mean()
        z['rsi14']=rsi(z.close,14)
    m15['pb18']=(m15.low<=m15.sma18).rolling(4).max().shift(1)
    h1['pb18']=(h1.low<=h1.sma18).rolling(3).max().shift(1)
    h1['pb50']=(h1.low<=h1.sma50).rolling(4).max().shift(1)
    h1['reclaim18']=(h1.close>h1.sma18)&(h1.close.shift(1)<=h1.sma18.shift(1))
    m15['reclaim18']=(m15.close>m15.sma18)&(m15.close.shift(1)<=m15.sma18.shift(1))
    for n in (2,4,6):
        h1[f'lo{n}']=h1.low.rolling(n).min().shift(1)
    am=align(m15,x.ts.to_numpy()); ah=align(h1,x.ts.to_numpy())
    x['m15_pb18']=am.pb18.fillna(0).to_numpy()>0
    x['h1_pb18']=ah.pb18.fillna(0).to_numpy()>0
    x['h1_pb50']=ah.pb50.fillna(0).to_numpy()>0
    x['m15_reclaim18']=am.reclaim18.fillna(False).to_numpy(dtype=bool)
    x['h1_reclaim18']=ah.reclaim18.fillna(False).to_numpy(dtype=bool)
    x['m15_rsi']=am.rsi14.to_numpy(); x['h1_rsi']=ah.rsi14.to_numpy()
    for n in (2,4,6):x[f'h1_lo{n}']=ah[f'lo{n}'].to_numpy()

    # Volatility path and Bollinger compression.
    r=raw.close.pct_change()
    x['rv30']=r.rolling(30).std().shift(1)
    x['rv120']=r.rolling(120).std().shift(1)
    x['rv_ratio']=x.rv30/x.rv120.replace(0,np.nan)
    x['atr1']=atr(raw,20)
    x['atr1_med120']=x.atr1.rolling(120).median().shift(1)
    x['atr1_ratio']=x.atr1/x.atr1_med120.replace(0,np.nan)
    mid=raw.close.rolling(20).mean().shift(1); sd=raw.close.rolling(20).std().shift(1)
    x['bb_width']=4*sd/mid.replace(0,np.nan)
    x['bb_med120']=x.bb_width.rolling(120).median().shift(1)
    x['bb_ratio']=x.bb_width/x.bb_med120.replace(0,np.nan)

    # Volume path: pullback should generally dry up before re-expansion.
    x['vol10_med']=raw.volume.rolling(10).median().shift(1)
    x['vol_prev30_med']=raw.volume.shift(10).rolling(30).median()
    x['vol_contract']=x.vol10_med/x.vol_prev30_med.replace(0,np.nan)

    # Resistance tests: count touches of evolving 60m high.
    h60=raw.high.rolling(60).max().shift(1)
    eps=.12*x.m5_atr
    x['tests60']=((raw.high>=h60-eps).astype(float)).rolling(60).sum().shift(1)

    # Path efficiency / shallow pullback geometry.
    absr=r.abs()
    x['eff30']=(raw.close/raw.close.shift(30)-1).abs()/absr.rolling(30).sum().replace(0,np.nan)
    # Previous 60m impulse then recent 15m retracement.
    anchor=raw.close.shift(60)
    peak=raw.high.shift(15).rolling(45).max()
    recent_low=raw.low.rolling(15).min().shift(1)
    impulse=(peak-anchor).clip(lower=0)
    x['impulse_atr']=impulse/x.m5_atr.replace(0,np.nan)
    x['pb_depth']=(peak-recent_low)/impulse.replace(0,np.nan)

    # Cross asset and relative strength.
    o=other[['ts','close']].copy()
    for n in (60,240,1440):o[f'ret{n}']=o.close/o.close.shift(n)-1
    o.index=pd.to_datetime(o.ts,unit='s',utc=True)
    ao=o.reindex(pd.to_datetime(x.ts,unit='s',utc=True),method='ffill')
    for n in (60,240,1440):x[f'other_ret{n}']=ao[f'ret{n}'].to_numpy()
    x['rel60']=x.ret60-x.other_ret60
    x['rel240']=x.ret240-x.other_ret240
    return x

def rsi(s,n=14):
    d=s.diff();up=d.clip(lower=0).ewm(alpha=1/n,adjust=False,min_periods=n).mean()
    dn=(-d.clip(upper=0)).ewm(alpha=1/n,adjust=False,min_periods=n).mean()
    rs_=up/dn.replace(0,np.nan);return 100-100/(1+rs_)

def regime(x,mode):
    if mode=='H1H4':
        return (x.h1_trend==1)&(x.h4_trend==1)&(x.d1_trend!=-1)
    if mode=='H4D1':
        return (x.h4_trend==1)&(x.d1_trend==1)&(x.h1_trend!=-1)
    if mode=='ALL':
        return (x.h1_trend==1)&(x.h4_trend==1)&(x.d1_trend==1)
    if mode=='EARLY':
        return (x.m15_trend==1)&(x.h1_trend==1)&(x.h4_trend!=-1)&(x.d1_trend!=-1)
    raise ValueError(mode)

def cross_filter(x,mode):
    if mode=='NONE':return pd.Series(True,index=x.index)
    if mode=='CONFIRM':return (x.other_ret60>0)&(x.other_ret240>0)
    if mode=='REL':return (x.rel60>0)&(x.rel240>0)
    if mode=='EITHER':return ((x.other_ret240>0)&(x.rel60>0))
    raise ValueError(mode)

def base_break(x,lb,vol_lo,vol_hi,body,atr_lo,atr_hi,extmax):
    lvl=x[f'hi{lb}']
    crossed=(x.close>lvl)&(x.close.shift(1)<=lvl)
    ext=(x.close-lvl)/x.m5_atr
    mask=crossed&(x.vr>=vol_lo)&(x.vr<=vol_hi)&(x.bull>=body)&(x.m5_atr_ratio>=atr_lo)&(x.m5_atr_ratio<=atr_hi)&(ext>.002)&(ext<=extmax)&x.contig1440
    return lvl,mask,ext

def profiles():
    out=[]
    # Dual pullback: both 15m and 1h have reset toward SMA18.
    for reg in ('H1H4','H4D1','ALL'):
      for cross in ('NONE','CONFIRM','REL','EITHER'):
        out += [
          dict(family='DUAL_PULLBACK',reg=reg,cross=cross,lb=10,vol_lo=1.15,vol_hi=2.4,body=.40,atr_lo=.85,atr_hi=1.35,ext=.30,depth_lo=.15,depth_hi=.65,vcon=1.05),
          dict(family='DUAL_PULLBACK',reg=reg,cross=cross,lb=20,vol_lo=1.35,vol_hi=2.2,body=.50,atr_lo=.90,atr_hi=1.30,ext=.25,depth_lo=.20,depth_hi=.55,vcon=.95),
          dict(family='DUAL_PULLBACK',reg=reg,cross=cross,lb=10,vol_lo=1.50,vol_hi=2.5,body=.60,atr_lo=.95,atr_hi=1.25,ext=.20,depth_lo=.20,depth_hi=.50,vcon=.90),
        ]
    # Compression breakout: avoid extreme volatility climax.
    for reg in ('EARLY','H1H4','H4D1'):
      for cross in ('NONE','CONFIRM','REL'):
        out += [
          dict(family='COMPRESSION_BREAK',reg=reg,cross=cross,lb=20,vol_lo=1.25,vol_hi=2.5,body=.45,atr_lo=.90,atr_hi=1.30,ext=.25,bb=.80,rv=.90,tests=2),
          dict(family='COMPRESSION_BREAK',reg=reg,cross=cross,lb=30,vol_lo=1.45,vol_hi=2.3,body=.55,atr_lo=.95,atr_hi=1.35,ext=.20,bb=.70,rv=.85,tests=3),
        ]
    # Reclaim after deeper 1h correction within 4h/daily uptrend.
    for cross in ('NONE','CONFIRM','REL'):
        out += [
          dict(family='RECLAIM',reg='H4D1',cross=cross,lb=10,vol_lo=1.20,vol_hi=2.5,body=.45,atr_lo=.85,atr_hi=1.35,ext=.30,use50=False),
          dict(family='RECLAIM',reg='H4D1',cross=cross,lb=20,vol_lo=1.35,vol_hi=2.2,body=.50,atr_lo=.90,atr_hi=1.30,ext=.25,use50=True),
        ]
    # Trend acceleration after shallow pullback and efficient structure.
    for reg in ('H1H4','ALL'):
      for cross in ('NONE','CONFIRM','REL'):
        out += [
          dict(family='TREND_ACCEL',reg=reg,cross=cross,lb=10,vol_lo=1.20,vol_hi=2.3,body=.45,atr_lo=.90,atr_hi=1.35,ext=.25,eff=.35,depth_hi=.45),
          dict(family='TREND_ACCEL',reg=reg,cross=cross,lb=20,vol_lo=1.40,vol_hi=2.2,body=.55,atr_lo=.95,atr_hi=1.30,ext=.20,eff=.45,depth_hi=.35),
        ]
    return out

def events(x,p):
    lvl,m,ext=base_break(x,p['lb'],p['vol_lo'],p['vol_hi'],p['body'],p['atr_lo'],p['atr_hi'],p['ext'])
    m&=regime(x,p['reg'])&cross_filter(x,p['cross'])
    f=p['family']
    if f=='DUAL_PULLBACK':
        m&=x.m15_pb18&x.h1_pb18&(x.impulse_atr>=.6)&(x.pb_depth>=p['depth_lo'])&(x.pb_depth<=p['depth_hi'])&(x.vol_contract<=p['vcon'])
    elif f=='COMPRESSION_BREAK':
        m&=(x.bb_ratio<=p['bb'])&(x.rv_ratio<=p['rv'])&(x.tests60>=p['tests'])
    elif f=='RECLAIM':
        m&=x.m15_pb18&(x.h1_pb50 if p['use50'] else x.h1_pb18)&(x.m15_reclaim18|x.h1_reclaim18)
    elif f=='TREND_ACCEL':
        m&=x.m15_pb18&(x.eff30>=p['eff'])&(x.pb_depth>=.05)&(x.pb_depth<=p['depth_hi'])
    else:raise ValueError(f)
    out=[]
    for i in np.flatnonzero(m.fillna(False).to_numpy()):
        if np.isfinite(lvl.iloc[i]) and np.isfinite(x.m5_atr.iloc[i]):
            out.append((int(i),float(lvl.iloc[i])))
    return out

MANAGEMENT=[
 ('LOCAL_15',15,.15,1.5,1440,'FIXED'),
 ('LOCAL_20',15,.15,2.0,1440,'FIXED'),
 ('LOCAL_25',15,.15,2.5,1440,'FIXED'),
 ('H1_15',4,.10,1.5,4320,'H1'),
 ('H1_20',4,.10,2.0,4320,'H1'),
 ('H1_25',4,.10,2.5,4320,'H1'),
 ('H1_PART',4,.10,2.5,4320,'H1PART'),
]

def sim(x,ev,mg,start,end,stress=0.):
    name,look,buf,rr,maxhold,stype=mg
    T=x.ts.to_numpy();O=x.open.to_numpy();H=x.high.to_numpy();L=x.low.to_numpy();C=x.close.to_numpy();A5=x.m5_atr.to_numpy();A1=x.h1_atr.to_numpy()
    lo_local=x.lo15.to_numpy()
    lo_h1=x[f'h1_lo{look}'].to_numpy() if stype.startswith('H1') else None
    st=ts(pd.Timestamp(start,tz='UTC'));en=ts(pd.Timestamp(end,tz='UTC'));ei=int(np.searchsorted(T,en))
    out=[];free=-1
    for sig,lvl in ev:
        if T[sig]<st or T[sig]>=en-60:continue
        i=sig+1
        if i<free or i>=ei:continue
        if stype=='FIXED':
            sr=lo_local[sig];a=A5[sig]
        else:
            sr=lo_h1[sig];a=A1[sig]
        if not np.isfinite(sr) or not np.isfinite(a):continue
        entry=O[i]*(1+SLIP);stop=sr-buf*a;risk=(entry-stop)/entry
        if not (.0035<=risk<=.05):continue
        target=entry*(1+risk*rr);cost=FEE+stress/2;gross=0.;rem=1.;part=False;reason='TIME';j=i;last=min(ei-1,i+maxhold)
        while j<=last:
            if j>i:cost+=rem*FUND_LONG/YEAR_MIN
            if L[j]<=stop:
                q=min(O[j],stop)*(1-SLIP);gross+=rem*(q/entry-1);cost+=rem*(FEE+stress/2)*q/entry;rem=0.;reason='STOP';break
            if stype=='H1PART' and not part:
                one=entry*(1+risk)
                if H[j]>=one:
                    q=one*(1-SLIP);gross+=.5*(q/entry-1);cost+=.5*(FEE+stress/2)*q/entry;rem=.5;part=True;stop=entry
            if H[j]>=target:
                q=target*(1-SLIP);gross+=rem*(q/entry-1);cost+=rem*(FEE+stress/2)*q/entry;rem=0.;reason='TARGET';break
            j+=1
        if rem:
            j=min(j,last);q=C[j]*(1-SLIP);gross+=rem*(q/entry-1);cost+=rem*(FEE+stress/2)*q/entry
        out.append({'opened':int(T[i]),'closed':int(T[j]),'net':float(gross-cost),'reason':reason})
        free=j+COOLDOWN
    return out

def yearly_discovery(x,ev,mg,stress=0.):
    return {str(y):met(sim(x,ev,mg,f'{y}-01-01',f'{y+1}-01-01',stress)) for y in (2022,2023)}

def valid_discovery(ys):
    vals=[m for m in ys.values() if m['n']>=6]
    return len(vals)==2 and all((m['avg'] or -9)>0 and m['pf']>=1.02 for m in vals) and sum(m['n'] for m in vals)>=20

def research(asset,x):
    cand=[]
    for p in profiles():
        ev=events(x,p)
        if len(ev)<18:continue
        for mg in MANAGEMENT:
            d=yearly_discovery(x,ev,mg,0.);ds=yearly_discovery(x,ev,mg,STRESS)
            if not valid_discovery(d):continue
            if sum((m['avg'] or -9)>0 for m in ds.values())<2:continue
            v24=met(sim(x,ev,mg,'2024-01-01','2025-01-01',0.))
            s24=met(sim(x,ev,mg,'2024-01-01','2025-01-01',STRESS))
            v25=met(sim(x,ev,mg,'2025-01-01','2026-01-01',0.))
            s25=met(sim(x,ev,mg,'2025-01-01','2026-01-01',STRESS))
            if min(v24['n'],v25['n'])<5:continue
            if min(v24['avg'] or -9,v25['avg'] or -9)<=0:continue
            if min(s24['avg'] or -9,s25['avg'] or -9)<=0:continue
            if min(v24['pf'],v25['pf'])<1.05:continue
            score=100*min(v24['avg'],v25['avg'])+.30*min(v24['win_rate'],v25['win_rate'])+.20*min(v24['pf'],v25['pf'])
            cand.append((score,p,mg,ev,d,ds,v24,s24,v25,s25))
    cand.sort(key=lambda z:z[0],reverse=True)
    frozen=[];seen=set()
    for z in cand:
        p=z[1];mg=z[2];key=(p['family'],p['reg'],p['cross'],p['lb'],mg[0])
        if key in seen:continue
        seen.add(key);frozen.append(z)
        if len(frozen)>=24:break
    out=[]
    for rank,z in enumerate(frozen,1):
        sc,p,mg,ev,d,ds,v24,s24,v25,s25=z
        test=met(sim(x,ev,mg,'2026-04-04','2026-10-04',0.))
        stress=met(sim(x,ev,mg,'2026-04-04','2026-10-04',STRESS))
        blocks=[met(sim(x,ev,mg,a,b,0.)) for a,b in [('2026-04-04','2026-06-01'),('2026-06-01','2026-08-01'),('2026-08-01','2026-10-04')]]
        pb=sum((m['avg'] or -9)>0 for m in blocks)
        passed=test['n']>=8 and (test['avg'] or -9)>0 and test['pf']>=1.20 and (stress['avg'] or -9)>0 and stress['pf']>=1.10 and pb>=2
        out.append({'rank':rank,'rule':p,'management':{'name':mg[0],'look':mg[1],'buf':mg[2],'rr':mg[3],'maxhold':mg[4],'type':mg[5]},
                    'discovery':d,'discovery_stress':ds,'validation_2024':v24,'validation_2024_stress':s24,
                    'validation_2025':v25,'validation_2025_stress':s25,
                    'test_2026':test,'test_stress':stress,'blocks':blocks,'positive_blocks':pb,'passed':passed})
    print(asset,'LONG_SEQ candidates',len(cand),'frozen',len(out),'passes',sum(r['passed'] for r in out),flush=True)
    return out

def main():
    asset=os.environ.get('RESEARCH_ASSET','ETH').upper()
    raw=load(asset);other=load('BTC' if asset=='ETH' else 'ETH')
    x=add_seq(raw,features(raw),other)
    rows=research(asset,x)
    out={'generated_at':datetime.now(timezone.utc).isoformat(),'asset':asset,
         'method':'Asymmetric long sequences; discovery 2022-23; dual validation 2024/25; 2026 evaluation.',
         'finalists':rows,'passes':[r for r in rows if r['passed']]}
    (OUT/'result.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False,default=json_default))
    print('VERITAS_LONG_SEQUENCE_PASSES='+json.dumps(out['passes'][:8],ensure_ascii=False,separators=(',',':'),default=json_default),flush=True)

if __name__=='__main__':main()
