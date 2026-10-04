"""VERITAS focused ETH-long robustness / exit engineering.

Fixed theory-led high-quality momentum-pullback core plus a narrow parameter
neighborhood. Selection on 2022-2025 only; Apr-Oct 2026 opened after freeze.
Research-only.
"""
from __future__ import annotations
import json, math, gc
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
from research_crypto_manager_library import load,features,ts,met,FEE,SLIP,FUND_LONG,YEAR_MIN,TEST_START,TEST_END

OUT=Path('eth_long_neighborhood_out');OUT.mkdir(exist_ok=True)
MAX_HOLD=1440;COOLDOWN=15

ENTRY=[
 ('CORE',1.80,.65,1.25,.15),
 ('WIDER_A',1.70,.65,1.25,.15),
 ('WIDER_B',1.80,.60,1.25,.15),
 ('WIDER_C',1.80,.65,1.20,.15),
 ('WIDER_D',1.80,.65,1.25,.18),
 ('TIGHT_A',1.90,.65,1.25,.15),
 ('TIGHT_B',1.80,.70,1.25,.15),
 ('TIGHT_C',1.80,.65,1.30,.15),
 ('TIGHT_D',1.80,.65,1.25,.12),
 ('BALANCED_WIDE',1.70,.60,1.20,.18),
 ('BALANCED_TIGHT',1.90,.70,1.30,.12),
]

def events(x,p):
    _,vol,body,ar,extmax=p
    level=x.hi5;cross=(x.close>level)&(x.close.shift(1)<=level)
    ext=(x.close-level)/x.m5_atr
    vote=np.sign(x.ret60)+np.sign(x.ret240)+np.sign(x.ret1440)
    mask=(vote>=2)&x.pbL&cross&(x.vr>=vol)&(x.bull>=body)&(x.m5_atr_ratio>=ar)&(ext>.0)&(ext<=extmax)&x.contig1440
    idx=np.flatnonzero(mask.fillna(False).to_numpy());out=[]
    for i in idx:
      if np.isfinite(level.iloc[i]) and np.isfinite(x.lo30.iloc[i]) and np.isfinite(x.m5_atr.iloc[i]):
        out.append((int(i),1,float(level.iloc[i]),float(x.lo30.iloc[i]),float(x.m5_atr.iloc[i])))
    return out

def simulate(x,ev,buf,first_r,first_qty,final_r,be,trail,max_hold,time_stop,mfe_gate,start,end,stress=0.):
    T=x.ts.to_numpy();O=x.open.to_numpy();H=x.high.to_numpy();L=x.low.to_numpy();C=x.close.to_numpy();A=x.m5_atr.to_numpy()
    res=[];next_i=-1;i1=int(np.searchsorted(T,end))
    for sig,d,level,sr,av in ev:
      if T[sig]<start or T[sig]>=end-60:continue
      i=sig+1
      if i<next_i or i>=i1:continue
      entry=O[i]*(1+SLIP);stop=sr-buf*av;risk=(entry-stop)/entry;extension=(entry-level)/av
      rc=2*(FEE+SLIP)+stress
      if not(max(.003,2.5*rc)<=risk<=.035 and -.10<=extension<=.75):continue
      target=entry*(1+risk*final_r);gross=0.;fees=FEE+stress/2;rem=1.;part=False;mfe=0.;reason='TIME'
      j=i;last=min(i1-1,i+max_hold)
      while j<=last:
        if j>i:fees+=rem*FUND_LONG/YEAR_MIN
        fav=H[j]/entry-1;mfe=max(mfe,float(fav))
        if L[j]<=stop:
          q=min(O[j],stop)*(1-SLIP);gross+=rem*(q/entry-1);fees+=rem*(FEE+stress/2)*q/entry;rem=0.;reason='STOP';break
        if time_stop and j>=i+time_stop and mfe<mfe_gate*risk:
          q=C[j]*(1-SLIP);gross+=rem*(q/entry-1);fees+=rem*(FEE+stress/2)*q/entry;rem=0.;reason='STALL';break
        if first_qty>0 and not part:
          plev=entry*(1+risk*first_r)
          if H[j]>=plev:
            q=plev*(1-SLIP);gross+=first_qty*(q/entry-1);fees+=first_qty*(FEE+stress/2)*q/entry
            rem-=first_qty;part=True
            if be=='BE':stop=entry
            elif be=='BE_PLUS':stop=entry*(1+.05*risk)
        if rem>0 and H[j]>=target:
          q=target*(1-SLIP);gross+=rem*(q/entry-1);fees+=rem*(FEE+stress/2)*q/entry;rem=0.;reason='TARGET';break
        if trail and part and j>i+10:
          a=max(i,j-20);tr=np.min(L[a:j])-.08*A[j]
          if np.isfinite(tr) and tr>stop and C[j]>tr+.10*A[j]:stop=tr
        j+=1
      if rem>0:
        j=min(j,last);q=C[j]*(1-SLIP);gross+=rem*(q/entry-1);fees+=rem*(FEE+stress/2)*q/entry
      res.append({'opened':int(T[i]),'closed':int(T[j]),'net':float(gross-fees),'risk':float(risk),'reason':reason})
      next_i=j+COOLDOWN
    return res

EXITS=[
 ('FIXED_075R',.20,0,0,.75,'NONE',False,1440,0,0),
 ('FIXED_1R',.20,0,0,1.,'NONE',False,1440,0,0),
 ('FIXED_1_25R',.20,0,0,1.25,'NONE',False,1440,0,0),
 ('FIXED_1_5R',.20,0,0,1.5,'NONE',False,1440,0,0),
 ('FIXED_2R',.20,0,0,2.,'NONE',False,1440,0,0),
 ('FIXED_2_5R',.20,0,0,2.5,'NONE',False,1440,0,0),
 ('PART50_050_1_5R',.20,.5,.5,1.5,'BE',False,1440,0,0),
 ('PART50_075_2R',.20,.75,.5,2.,'BE',False,1440,0,0),
 ('PART70_050_1_5R',.20,.5,.7,1.5,'BE',False,1440,0,0),
 ('PART70_075_2R',.20,.75,.7,2.,'BE',False,1440,0,0),
 ('PART50_1R_2_5R',.20,1.,.5,2.5,'BE',True,1440,0,0),
 ('STALL30',.20,0,0,2.,'NONE',False,1440,30,.20),
 ('STALL60',.20,0,0,2.,'NONE',False,1440,60,.25),
 ('HOLD4H',.20,0,0,2.,'NONE',False,240,0,0),
 ('HOLD8H',.20,0,0,2.,'NONE',False,480,0,0),
 ('FIXED1_BUF10',.10,0,0,1.,'NONE',False,1440,0,0),
 ('FIXED1_BUF30',.30,0,0,1.,'NONE',False,1440,0,0),
]
def pars(e):
 n,b,fr,fq,fin,be,tr,mh,ts_,mg=e
 return dict(buf=b,first_r=fr,first_qty=fq,final_r=fin,be=be,trail=tr,max_hold=mh,time_stop=ts_,mfe_gate=mg)

def yearly(x,ev,p,stress=0.):
 return {str(y):met(simulate(x,ev,start=ts(f'{y}-01-01T00:00:00Z'),end=ts(f'{y+1}-01-01T00:00:00Z'),stress=stress,**p)) for y in (2022,2023,2024,2025)}

def main():
 eth=load('ETH');x=features(eth);cand=[]
 for prof in ENTRY:
  ev=events(x,prof)
  if len(ev)<40:continue
  for ex in EXITS:
   p=pars(ex);yrs=yearly(x,ev,p,0.);syrs=yearly(x,ev,p,.0005)
   vals=[m for m in yrs.values() if m['n']>=8];sv=[m for m in syrs.values() if m['n']>=8]
   if len(vals)<3 or sum((m['avg'] or -9)>0 for m in yrs.values())<3 or sum((m['avg'] or -9)>0 for m in syrs.values())<3:continue
   if sum(m['n'] for m in yrs.values())<45:continue
   minpf=min(m['pf'] for m in vals);smin=min(m['pf'] for m in sv);medwr=float(np.median([m['win_rate'] for m in vals]));minavg=min(m['avg'] for m in vals)
   if minpf<1.0 or smin<.95:continue
   score=minpf+.35*smin+.75*medwr+80*minavg
   cand.append((score,prof,ex,ev,yrs,syrs))
 cand.sort(key=lambda z:z[0],reverse=True);frozen=[];seen=set()
 for z in cand:
  key=(z[1][0],z[2][0])
  if key in seen:continue
  seen.add(key);frozen.append(z)
  if len(frozen)>=20:break
 out=[]
 for rank,z in enumerate(frozen,1):
  sc,prof,ex,ev,yrs,syrs=z;p=pars(ex)
  test=met(simulate(x,ev,start=ts(TEST_START),end=ts(TEST_END),stress=0.,**p))
  stress=met(simulate(x,ev,start=ts(TEST_START),end=ts(TEST_END),stress=.0005,**p))
  blocks=[met(simulate(x,ev,start=ts(a+'T00:00:00Z'),end=ts(b+'T00:00:00Z'),stress=0.,**p)) for a,b in [('2026-04-04','2026-06-01'),('2026-06-01','2026-08-01'),('2026-08-01','2026-10-04')]]
  pb=sum((m['avg'] or -9)>0 for m in blocks)
  passed=test['n']>=15 and (test['avg'] or -9)>0 and test['pf']>=1.25 and (test['win_rate'] or 0)>=.58 and (stress['avg'] or -9)>0 and stress['pf']>=1.15 and pb>=2
  out.append({'rank':rank,'entry':{'name':prof[0],'vol':prof[1],'body':prof[2],'atr_ratio':prof[3],'ext':prof[4]},'exit':ex[0],
              'selection_years':yrs,'selection_stress':syrs,'test_2026':test,'test_stress':stress,'blocks':blocks,'positive_blocks':pb,'passed':passed})
 report={'generated_at':datetime.now(timezone.utc).isoformat(),'candidates_pre2026':len(cand),'finalists':out,'passes':[r for r in out if r['passed']]}
 (OUT/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False))
 print('ETH_LONG_NEIGHBORHOOD finalists',len(out),'passes',len(report['passes']),flush=True)
 print('ETH_LONG_PASSES='+json.dumps(report['passes'][:5],ensure_ascii=False,separators=(',',':')),flush=True)

if __name__=='__main__':main()
