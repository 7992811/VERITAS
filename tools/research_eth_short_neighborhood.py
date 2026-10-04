"""VERITAS focused ETH-short robustness neighborhood.

Starts from the best manager-library entry found without using 2026:
TREND_PULLBACK / SHORT / EARLY. Searches only a narrow, predeclared neighborhood
of entry quality and trade-management variants on 2022-2025. The 2026 window is
opened only after finalists are frozen.

Research only. No production changes.
"""
from __future__ import annotations
import json, math, gc
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd

from research_crypto_manager_library import (
    load, features, ts, met, FEE, SLIP, FUND_LONG, FUND_SHORT, YEAR_MIN,
    HIST_START, SELECT_END, TEST_START, TEST_END
)

OUT=Path("eth_short_neighborhood_out"); OUT.mkdir(exist_ok=True)
MAX_HOLD=1440; COOLDOWN=15

ENTRY_PROFILES=[
    # name, vol, body, atr expansion, max extension
    ('CORE',1.50,.50,1.10,.25),
    ('WIDER_A',1.40,.50,1.10,.25),
    ('WIDER_B',1.50,.45,1.10,.25),
    ('WIDER_C',1.50,.50,1.05,.25),
    ('WIDER_D',1.50,.50,1.10,.30),
    ('QUALITY_A',1.60,.50,1.10,.25),
    ('QUALITY_B',1.50,.55,1.10,.25),
    ('QUALITY_C',1.50,.50,1.15,.25),
    ('QUALITY_D',1.50,.50,1.10,.20),
    ('BALANCED_WIDE',1.40,.45,1.05,.30),
    ('BALANCED_TIGHT',1.60,.55,1.15,.20),
]

def btc_context(btc, target_ts):
    b=btc[['ts','close']].copy()
    for n in (60,240):
        b[f'ret{n}']=b.close/b.close.shift(n)-1
    b.index=pd.to_datetime(b.ts,unit='s',utc=True)
    idx=pd.to_datetime(target_ts,unit='s',utc=True)
    a=b.reindex(idx,method='ffill')
    return a.ret60.to_numpy(), a.ret240.to_numpy()

def make_events(x, profile, btc60, btc240, cross):
    _,vol,body,ar,extmax=profile
    d=-1; level=x.lo10
    crossed=(x.close<level)&(x.close.shift(1)>=level)
    ext=(level-x.close)/x.m5_atr
    mask=(x.m15_trend==-1)&(x.h1_trend==-1)&(x.h4_trend!=1)&(x.d1_trend!=1)
    mask&=crossed&(x.vr>=vol)&(x.bear>=body)&(x.m5_atr_ratio>=ar)&(ext<=extmax)&x.pbS&x.contig1440
    if cross=='BTC1H': mask&=(btc60<0)
    elif cross=='BTC1H4H': mask&=(btc60<0)&(btc240<0)
    elif cross=='BTC4H': mask&=(btc240<0)
    elif cross!='NONE': raise ValueError(cross)
    idx=np.flatnonzero(mask.fillna(False).to_numpy())
    out=[]
    for i in idx:
        if np.isfinite(level.iloc[i]) and np.isfinite(x.hi30.iloc[i]) and np.isfinite(x.m5_atr.iloc[i]):
            out.append((int(i),-1,float(level.iloc[i]),float(x.hi30.iloc[i]),float(x.m5_atr.iloc[i])))
    return out

def simulate(x,ev,buf,first_r,first_qty,final_r,be_rule,trail,max_hold,time_stop,mfe_gate,start,end,stress=0.):
    T=x.ts.to_numpy();O=x.open.to_numpy();H=x.high.to_numpy();L=x.low.to_numpy();C=x.close.to_numpy();A=x.m5_atr.to_numpy()
    result=[];next_i=-1;i1=int(np.searchsorted(T,end))
    for sig,d,level,sr,av in ev:
        if T[sig]<start or T[sig]>=end-60: continue
        i=sig+1
        if i<next_i or i>=i1: continue
        entry=O[i]*(1+d*SLIP);stop=sr-d*buf*av;risk=d*(entry-stop)/entry;extension=d*(entry-level)/av
        round_cost=2*(FEE+SLIP)+stress
        if not (max(.003,2.5*round_cost)<=risk<=.035 and -.10<=extension<=.75): continue
        final_target=entry*(1+d*risk*final_r)
        partial_done=False;gross=0.;fees=FEE+stress/2;rem=1.;mfe=0.;reason='TIME'
        j=i;last=min(i1-1,i+max_hold)
        while j<=last:
            if j>i: fees+=rem*(FUND_SHORT/YEAR_MIN)
            fav=1-L[j]/entry;mfe=max(mfe,float(fav))
            if H[j]>=stop:
                q=max(O[j],stop)*(1+SLIP)
                gross+=rem*d*(q/entry-1);fees+=rem*(FEE+stress/2)*q/entry;rem=0.;reason='STOP';break
            if time_stop and j>=i+time_stop and mfe<mfe_gate*risk:
                q=C[j]*(1+SLIP)
                gross+=rem*d*(q/entry-1);fees+=rem*(FEE+stress/2)*q/entry;rem=0.;reason='STALL';break
            if first_qty>0 and not partial_done:
                plev=entry*(1+d*risk*first_r)
                if L[j]<=plev:
                    q=plev*(1+SLIP)
                    gross+=first_qty*d*(q/entry-1);fees+=first_qty*(FEE+stress/2)*q/entry
                    rem-=first_qty;partial_done=True
                    if be_rule=='BE': stop=entry
                    elif be_rule=='BE_PLUS': stop=entry*(1-d*.05*risk)
            if rem>0 and L[j]<=final_target:
                q=final_target*(1+SLIP)
                gross+=rem*d*(q/entry-1);fees+=rem*(FEE+stress/2)*q/entry;rem=0.;reason='TARGET';break
            if trail and partial_done and j>i+10:
                a=max(i,j-20);tr=np.max(H[a:j])+.08*A[j]
                if np.isfinite(tr) and tr<stop and C[j]<tr-.10*A[j]: stop=tr
            j+=1
        if rem>0:
            j=min(j,last);q=C[j]*(1+SLIP)
            gross+=rem*d*(q/entry-1);fees+=rem*(FEE+stress/2)*q/entry
        result.append({'opened':int(T[i]),'closed':int(T[j]),'net':float(gross-fees),'risk':float(risk),'reason':reason})
        next_i=j+COOLDOWN
    return result

EXITS=[
 # name, buf, firstR, firstQty, finalR, be, trail, maxHold, timeStop, mfeGate
 ('FIXED_1R',.20,0,0,1.0,'NONE',False,1440,0,0),
 ('FIXED_1_5R',.20,0,0,1.5,'NONE',False,1440,0,0),
 ('FIXED_2R',.20,0,0,2.0,'NONE',False,1440,0,0),
 ('PART50_050_2R',.20,.50,.50,2.0,'BE',False,1440,0,0),
 ('PART50_075_2R',.20,.75,.50,2.0,'BE',False,1440,0,0),
 ('PART50_1R_2R',.20,1.0,.50,2.0,'BE',False,1440,0,0),
 ('PART70_050_2R',.20,.50,.70,2.0,'BE',False,1440,0,0),
 ('PART70_075_2R',.20,.75,.70,2.0,'BE',False,1440,0,0),
 ('PART70_1R_2R',.20,1.0,.70,2.0,'BE',False,1440,0,0),
 ('PART50_075_2_5R',.20,.75,.50,2.5,'BE',True,1440,0,0),
 ('PART50_1R_2_5R',.20,1.0,.50,2.5,'BE',True,1440,0,0),
 ('STALL30',.20,0,0,2.0,'NONE',False,1440,30,.20),
 ('STALL60',.20,0,0,2.0,'NONE',False,1440,60,.25),
 ('HOLD4H',.20,0,0,2.0,'NONE',False,240,0,0),
 ('HOLD8H',.20,0,0,2.0,'NONE',False,480,0,0),
 # stop-neighborhood robustness
 ('FIXED2_BUF10',.10,0,0,2.0,'NONE',False,1440,0,0),
 ('FIXED2_BUF30',.30,0,0,2.0,'NONE',False,1440,0,0),
]

def pars(ex):
    n,b,fr,fq,fin,be,tr,mh,ts_,mg=ex
    return dict(buf=b,first_r=fr,first_qty=fq,final_r=fin,be_rule=be,trail=tr,max_hold=mh,time_stop=ts_,mfe_gate=mg)

def yearly(x,ev,p,stress=0.):
    return {str(y):met(simulate(x,ev,start=ts(f'{y}-01-01T00:00:00Z'),end=ts(f'{y+1}-01-01T00:00:00Z'),stress=stress,**p)) for y in (2022,2023,2024,2025)}

def aggregate_years(yrs):
    n=sum(m['n'] for m in yrs.values())
    pos=sum((m['avg'] or -9)>0 for m in yrs.values())
    enough=sum(m['n']>=8 for m in yrs.values())
    minpf=min((m['pf'] for m in yrs.values() if m['n']>=8),default=0)
    medwr=float(np.median([m['win_rate'] or 0 for m in yrs.values() if m['n']>=8])) if enough else 0
    minavg=min((m['avg'] for m in yrs.values() if m['n']>=8 and m['avg'] is not None),default=-9)
    return n,pos,enough,minpf,medwr,minavg

def main():
    eth=load('ETH');btc=load('BTC');x=features(eth)
    btc60,btc240=btc_context(btc,x.ts.to_numpy())
    candidates=[]
    for prof in ENTRY_PROFILES:
      for cross in ('NONE','BTC1H','BTC4H','BTC1H4H'):
        ev=make_events(x,prof,btc60,btc240,cross)
        if len(ev)<40: continue
        for ex in EXITS:
            p=pars(ex);yrs=yearly(x,ev,p,0.);syrs=yearly(x,ev,p,.0005)
            n,pos,enough,minpf,medwr,minavg=aggregate_years(yrs)
            sn,spos,senough,sminpf,smedwr,sminavg=aggregate_years(syrs)
            if n<45 or enough<3 or pos<3 or spos<3: continue
            if minpf<1.0 or sminpf<.95: continue
            # Rank for robustness first, then hit-rate and expectancy.
            score=minpf+.35*sminpf+.55*medwr+80*minavg
            candidates.append((score,prof,cross,ex,ev,yrs,syrs))
    candidates.sort(key=lambda z:z[0],reverse=True)
    frozen=[];seen=set()
    for z in candidates:
        _,prof,cross,ex,*_=z
        key=(prof[0],cross,ex[0])
        if key in seen: continue
        seen.add(key);frozen.append(z)
        if len(frozen)>=20:break
    out=[]
    for rank,z in enumerate(frozen,1):
        sc,prof,cross,ex,ev,yrs,syrs=z;p=pars(ex)
        test=met(simulate(x,ev,start=ts(TEST_START),end=ts(TEST_END),stress=0.,**p))
        stress=met(simulate(x,ev,start=ts(TEST_START),end=ts(TEST_END),stress=.0005,**p))
        blocks=[met(simulate(x,ev,start=ts(a+'T00:00:00Z'),end=ts(b+'T00:00:00Z'),stress=0.,**p)) for a,b in
                [('2026-04-04','2026-06-01'),('2026-06-01','2026-08-01'),('2026-08-01','2026-10-04')]]
        pb=sum((m['avg'] or -9)>0 for m in blocks)
        passed=(test['n']>=15 and (test['avg'] or -9)>0 and test['pf']>=1.35 and (test['win_rate'] or 0)>=.60
                and (stress['avg'] or -9)>0 and stress['pf']>=1.20 and pb>=2)
        out.append({'rank':rank,'entry_profile':{'name':prof[0],'vol':prof[1],'body':prof[2],'atr_ratio':prof[3],'ext':prof[4]},
                    'cross':cross,'exit':ex[0],'selection_years':yrs,'selection_stress':syrs,
                    'test_2026':test,'test_stress':stress,'blocks':blocks,'positive_blocks':pb,'passed':passed})
    report={'generated_at':datetime.now(timezone.utc).isoformat(),'protocol':'Entry/exit neighborhood selected on 2022-2025 only; 2026 evaluated after freeze.',
            'candidates_pre2026':len(candidates),'finalists':out,'passes':[r for r in out if r['passed']]}
    (OUT/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False))
    print('ETH_SHORT_NEIGHBORHOOD finalists',len(out),'passes',len(report['passes']),flush=True)
    print('ETH_SHORT_PASSES='+json.dumps(report['passes'][:5],ensure_ascii=False,separators=(',',':')),flush=True)

if __name__=='__main__':main()
