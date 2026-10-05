"""VERITAS bull-episode conditional LONG research.

Why: calendar-year validation can reject a conditional long edge merely because
the number/type of bull regimes differs by year. This study validates across
independent bull episodes instead.

Causal protocol:
- Identify completed-bar bull regimes from 1h/4h/1d trend state.
- Assign independent contiguous regime episodes.
- Candidate 1m entries use pullback/reclaim/compression with 5m vol/volume bands.
- Pre-2026 bull episodes are split chronologically 50% discovery / 25% calibration /
  25% sealed pre-2026 control.
- Parameters must be positive after costs in all three episode partitions.
- Apr-Oct 2026 remains final research OOS.
"""
from __future__ import annotations
import json,os,gc
from datetime import datetime,timezone
from pathlib import Path
import numpy as np,pandas as pd
from research_crypto_manager_library import load,features,rs,align,ts,met,FEE,SLIP,FUND_LONG,YEAR_MIN

OUT=Path('bull_episode_out');OUT.mkdir(exist_ok=True)
STRESS=.0005

def jd(o):
 if isinstance(o,np.integer):return int(o)
 if isinstance(o,np.floating):return float(o)
 if isinstance(o,np.bool_):return bool(o)
 raise TypeError(type(o).__name__)

def add(raw,x,other):
 m15=rs(raw,15);h1=rs(raw,60)
 m15['pb']=(m15.low<=m15.sma18).rolling(4).max().shift(1);h1['pb']=(h1.low<=h1.sma18).rolling(3).max().shift(1)
 m15['reclaim']=(m15.close>m15.sma18)&(m15.close.shift(1)<=m15.sma18.shift(1));h1['reclaim']=(h1.close>h1.sma18)&(h1.close.shift(1)<=h1.sma18.shift(1))
 am=align(m15,x.ts.to_numpy());ah=align(h1,x.ts.to_numpy());x['m15_pb']=am.pb.fillna(0).to_numpy()>0;x['h1_pb']=ah.pb.fillna(0).to_numpy()>0;x['m15_reclaim']=am.reclaim.fillna(False).to_numpy(dtype=bool);x['h1_reclaim']=ah.reclaim.fillna(False).to_numpy(dtype=bool)
 for n in (5,10,20,30,60):x[f'hi{n}']=raw.high.rolling(n).max().shift(1);x[f'lo{n}']=raw.low.rolling(n).min().shift(1)
 # compression
 r=raw.close.pct_change();x['rv30']=r.rolling(30).std().shift(1);x['rv120']=r.rolling(120).std().shift(1);x['rv_ratio']=x.rv30/x.rv120.replace(0,np.nan)
 # cross asset
 o=other[['ts','close']].copy();o['r60']=o.close/o.close.shift(60)-1;o['r240']=o.close/o.close.shift(240)-1;o.index=pd.to_datetime(o.ts,unit='s',utc=True);a=o.reindex(pd.to_datetime(x.ts,unit='s',utc=True),method='ffill');x['or60']=a.r60.to_numpy();x['or240']=a.r240.to_numpy()
 return x

def regime_mask(x,m):
 if m=='H4D1':return (x.h4_trend==1)&(x.d1_trend==1)&(x.h1_trend!=-1)
 if m=='H1H4':return (x.h1_trend==1)&(x.h4_trend==1)&(x.d1_trend!=-1)
 if m=='ALL':return (x.h1_trend==1)&(x.h4_trend==1)&(x.d1_trend==1)
 raise ValueError

def episode_ids(mask,tsarr):
 # New episode after state false or >6h gap.
 m=mask.fillna(False).to_numpy(bool);out=np.full(len(m),-1,dtype=int);eid=-1;prev=-10**18
 for i,ok in enumerate(m):
  if not ok:continue
  if i==0 or not m[i-1] or tsarr[i]-prev>21600:eid+=1
  out[i]=eid;prev=tsarr[i]
 return out

def profiles():
 out=[]
 for reg in ('H4D1','H1H4','ALL'):
  for cross in ('NONE','CONF'):
   for fam in ('PULLBACK','RECLAIM','BREAK','COMP'):
    out += [
     dict(reg=reg,cross=cross,fam=fam,lb=10,vol_lo=1.15,vol_hi=2.5,body=.4,ar_lo=.85,ar_hi=1.35,ext=.3),
     dict(reg=reg,cross=cross,fam=fam,lb=20,vol_lo=1.35,vol_hi=2.3,body=.5,ar_lo=.9,ar_hi=1.3,ext=.25),
     dict(reg=reg,cross=cross,fam=fam,lb=10,vol_lo=1.5,vol_hi=2.6,body=.6,ar_lo=.95,ar_hi=1.25,ext=.2),
    ]
 return out

def events(x,p,eids):
 lvl=x[f'hi{p["lb"]}'];br=(x.close>lvl)&(x.close.shift(1)<=lvl);ext=(x.close-lvl)/x.m5_atr
 m=(eids>=0)&br&(x.vr>=p['vol_lo'])&(x.vr<=p['vol_hi'])&(x.bull>=p['body'])&(x.m5_atr_ratio>=p['ar_lo'])&(x.m5_atr_ratio<=p['ar_hi'])&(ext>.001)&(ext<=p['ext'])&x.contig1440
 if p['cross']=='CONF':m&=(x.or60>0)&(x.or240>0)
 if p['fam']=='PULLBACK':m&=x.m15_pb
 elif p['fam']=='RECLAIM':m&=x.m15_pb&(x.m15_reclaim|x.h1_reclaim)
 elif p['fam']=='COMP':m&=(x.rv_ratio<=.85)
 elif p['fam']=='BREAK':pass
 out=[]
 for i in np.flatnonzero(np.asarray(m,dtype=bool)):
  out.append((int(i),float(lvl.iloc[i]),int(eids[i])))
 return out

MG=[('L15',15,.15,1.5,1440),('L20',15,.15,2.,1440),('H15',30,.15,1.5,4320),('H20',30,.15,2.,4320),('H25',30,.15,2.5,4320)]

def sim(x,ev,mg,allowed_eps,start,end,stress=0.):
 name,look,buf,rr,hold=mg;T=x.ts.to_numpy();O=x.open.to_numpy();H=x.high.to_numpy();L=x.low.to_numpy();C=x.close.to_numpy();A=x.m5_atr.to_numpy();lo=x[f'lo{look}'].to_numpy();st=ts(pd.Timestamp(start,tz='UTC'));en=ts(pd.Timestamp(end,tz='UTC'));ei=int(np.searchsorted(T,en));out=[];free=-1
 aset=set(allowed_eps)
 for sig,lvl,eid in ev:
  if eid not in aset or T[sig]<st or T[sig]>=en-60:continue
  i=sig+1
  if i<free or i>=ei:continue
  sr=lo[sig]
  if not np.isfinite(sr):continue
  entry=O[i]*(1+SLIP);stop=sr-buf*A[sig];risk=(entry-stop)/entry
  if not(.0035<=risk<=.05):continue
  tgt=entry*(1+risk*rr);cost=FEE+stress/2;j=i;last=min(ei-1,i+hold);net=None;reason='TIME'
  while j<=last:
   if j>i:cost+=FUND_LONG/YEAR_MIN
   if L[j]<=stop:
    q=min(O[j],stop)*(1-SLIP);net=q/entry-1-cost-(FEE+stress/2)*q/entry;reason='STOP';break
   if H[j]>=tgt:
    q=tgt*(1-SLIP);net=q/entry-1-cost-(FEE+stress/2)*q/entry;reason='TARGET';break
   j+=1
  if net is None:
   j=last;q=C[j]*(1-SLIP);net=q/entry-1-cost-(FEE+stress/2)*q/entry
  out.append({'opened':int(T[i]),'closed':int(T[j]),'net':float(net),'episode':eid,'reason':reason});free=j+20
 return out

def search(asset,x):
 cand=[]
 for reg in ('H4D1','H1H4','ALL'):
  mask=regime_mask(x,reg);eids=episode_ids(mask,x.ts.to_numpy());pre_eps=sorted(set(eids[(x.ts<ts('2026-01-01T00:00:00Z')) & (eids>=0)]))
  if len(pre_eps)<8:continue
  n=len(pre_eps);a=pre_eps[:max(1,int(n*.5))];b=pre_eps[max(1,int(n*.5)):max(2,int(n*.75))];c=pre_eps[max(2,int(n*.75)):]
  splits=[a,b,c]
  for p in [q for q in profiles() if q['reg']==reg]:
   ev=events(x,p,eids)
   if len(ev)<15:continue
   for mg in MG:
    ms=[];sms=[];ok=True
    for eps in splits:
     z=met(sim(x,ev,mg,eps,'2022-01-01','2026-01-01',0.));s=met(sim(x,ev,mg,eps,'2022-01-01','2026-01-01',STRESS));ms.append(z);sms.append(s)
     if z['n']<4 or (z['avg'] or -9)<=0 or z['pf']<1.03 or (s['avg'] or -9)<=0:ok=False;break
    if not ok:continue
    score=120*min(m['avg'] for m in ms)+.35*min(m['win_rate'] for m in ms)+.2*min(m['pf'] for m in ms)
    cand.append((score,p,mg,ev,eids,pre_eps,ms,sms))
 cand.sort(key=lambda z:z[0],reverse=True);out=[]
 for rank,z in enumerate(cand[:20],1):
  sc,p,mg,ev,eids,pre,ms,sms=z;test_eps=sorted(set(eids[(x.ts>=ts('2026-04-04T00:00:00Z'))&(x.ts<ts('2026-10-04T00:00:00Z'))&(eids>=0)]));test=met(sim(x,ev,mg,test_eps,'2026-04-04','2026-10-04'));stress=met(sim(x,ev,mg,test_eps,'2026-04-04','2026-10-04',STRESS));blocks=[]
  for aa,bb in [('2026-04-04','2026-06-01'),('2026-06-01','2026-08-01'),('2026-08-01','2026-10-04')]:
   ep=sorted(set(eids[(x.ts>=ts(aa+'T00:00:00Z'))&(x.ts<ts(bb+'T00:00:00Z'))&(eids>=0)]));blocks.append(met(sim(x,ev,mg,ep,aa,bb)))
  pb=sum((m['avg'] or -9)>0 for m in blocks);passed=test['n']>=6 and (test['avg'] or -9)>0 and test['pf']>=1.2 and (stress['avg'] or -9)>0 and stress['pf']>=1.1 and pb>=2
  out.append({'rank':rank,'rule':p,'management':{'name':mg[0],'look':mg[1],'buf':mg[2],'rr':mg[3],'hold':mg[4]},'pre_splits':ms,'pre_stress':sms,'n_pre_episodes':len(pre),'test2026':test,'test_stress':stress,'blocks':blocks,'positive_blocks':pb,'passed':passed})
 print(asset,'BULL_EP candidates',len(cand),'finalists',len(out),'passes',sum(r['passed'] for r in out),flush=True);return out

def main():
 asset=os.environ.get('RESEARCH_ASSET','ETH').upper();raw=load(asset);other=load('BTC' if asset=='ETH' else 'ETH');x=add(raw,features(raw),other);rows=search(asset,x);out={'generated_at':datetime.now(timezone.utc).isoformat(),'asset':asset,'finalists':rows,'passes':[r for r in rows if r['passed']]};(OUT/'result.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False,default=jd));print('VERITAS_BULL_EP_PASSES='+json.dumps(out['passes'][:8],separators=(',',':'),default=jd),flush=True)
if __name__=='__main__':main()
