"""VERITAS long capitulation / exhaustion reversal research.

Research only. Sequence-based reversal, not generic RSI mean reversion.
Families:
- FAILED_BREAK_RECLAIM: sweep a multi-hour low, close back inside, then micro-high break.
- EXHAUSTION_RECLAIM: large ATR-normalized stretch below 1h/4h value, oversold 15m,
  then volatility decelerates and micro structure turns.
- VOL_CLIMAX: prior high vol/volume selloff, vol peak rolls over, higher-low + break.

Selection 2022-23 discovery; 2024 and 2025 independent validation; 2026 evaluation.
"""
from __future__ import annotations
import gc,json,os
from datetime import datetime,timezone
from pathlib import Path
import numpy as np,pandas as pd
from research_crypto_manager_library import load,features,rs,align,ts,met,FEE,SLIP,FUND_LONG,YEAR_MIN

OUT=Path('long_reversal_out');OUT.mkdir(exist_ok=True)
STRESS=.0005;COOLDOWN=20

def jd(o):
 if isinstance(o,np.integer):return int(o)
 if isinstance(o,np.floating):return float(o)
 if isinstance(o,np.bool_):return bool(o)
 raise TypeError(type(o).__name__)

def rsi(s,n=14):
 d=s.diff();u=d.clip(lower=0).ewm(alpha=1/n,adjust=False,min_periods=n).mean();dn=(-d.clip(upper=0)).ewm(alpha=1/n,adjust=False,min_periods=n).mean();z=u/dn.replace(0,np.nan);return 100-100/(1+z)

def add(raw,x,other):
 m5=rs(raw,5);m15=rs(raw,15);h1=rs(raw,60);h4=rs(raw,240)
 for z in (m5,m15,h1,h4):z['rsi']=rsi(z.close,14);z['dist18']=(z.close-z.sma18)/z.atr.replace(0,np.nan)
 m5['ar_med']=m5.atr.rolling(48).median().shift(1);m5['ar']=m5.atr/m5.ar_med
 m5['ar_peak12']=m5.ar.shift(1).rolling(12).max()
 m5['ar_rollover']=m5.ar/m5.ar_peak12.replace(0,np.nan)
 am5=align(m5,x.ts.to_numpy());am15=align(m15,x.ts.to_numpy());ah1=align(h1,x.ts.to_numpy());ah4=align(h4,x.ts.to_numpy())
 x['m5_rsi']=am5.rsi.to_numpy();x['m15_rsi']=am15.rsi.to_numpy();x['h1_rsi']=ah1.rsi.to_numpy()
 x['h1_dist18']=ah1.dist18.to_numpy();x['h4_dist18']=ah4.dist18.to_numpy();x['m5_ar2']=am5.ar.to_numpy();x['m5_ar_roll']=am5.ar_rollover.to_numpy()
 # prior oversold flags
 x['m15_os30']=(x.m15_rsi<=30).rolling(60).max().shift(1)>0
 x['m15_os35']=(x.m15_rsi<=35).rolling(60).max().shift(1)>0
 # local lows and sweep/reclaim
 for n in (10,30,60,120,240):x[f'lo{n}']=raw.low.rolling(n).min().shift(1);x[f'hi{n}']=raw.high.rolling(n).max().shift(1)
 # A sweep bar in previous 10m: low below prior boundary, close back above it.
 for n in (60,120,240):
  boundary=x[f'lo{n}']
  pen=(boundary-raw.low)/x.m5_atr
  sweep=(pen>=.08)&(raw.close>boundary)&(x.vr>=1.5)
  x[f'sweep{n}_10']=sweep.rolling(10).max().shift(1)>0
  x[f'sweepLow{n}']=raw.low.where(sweep).rolling(10).min().shift(1)
 # higher-low microstructure
 l0=raw.low.rolling(5).min().shift(1);l1=raw.low.shift(5).rolling(5).min();x['higher_low5']=l0>l1
 # volume climax in prior 30m and current normalization/re-expansion
 x['vmax30']=x.vr.shift(1).rolling(30).max();x['vmed10']=x.vr.rolling(10).median().shift(1)
 # selloff returns normalized by h1 ATR
 x['ret60_absatr']=-(raw.close/raw.close.shift(60)-1)/(x.h1_atr/raw.close).replace(0,np.nan)
 # cross market crash/rebound context
 o=other[['ts','close']].copy();o['r60']=o.close/o.close.shift(60)-1;o['r240']=o.close/o.close.shift(240)-1;o.index=pd.to_datetime(o.ts,unit='s',utc=True);a=o.reindex(pd.to_datetime(x.ts,unit='s',utc=True),method='ffill');x['other60']=a.r60.to_numpy();x['other240']=a.r240.to_numpy()
 return x

def cfgs():
 out=[]
 for sweep in (60,120,240):
  for cross in ('NONE','OTHER_DOWN','OTHER_RECOVER'):
   out += [
    dict(fam='FAILED_BREAK',sweep=sweep,cross=cross,lb=5,vol_lo=1.15,vol_hi=3.0,body=.40,ar_lo=.85,ar_hi=1.45,ext=.30,dist=-.5),
    dict(fam='FAILED_BREAK',sweep=sweep,cross=cross,lb=10,vol_lo=1.30,vol_hi=2.7,body=.50,ar_lo=.90,ar_hi=1.40,ext=.25,dist=-1.0),
   ]
 for cross in ('NONE','OTHER_DOWN','OTHER_RECOVER'):
  out += [
   dict(fam='EXHAUST',cross=cross,lb=5,stretch=1.5,os=35,climax=2.0,roll=.90,vol_lo=1.15,vol_hi=2.8,body=.40,ar_lo=.9,ar_hi=1.5,ext=.3),
   dict(fam='EXHAUST',cross=cross,lb=10,stretch=2.0,os=30,climax=2.5,roll=.85,vol_lo=1.25,vol_hi=2.6,body=.50,ar_lo=.9,ar_hi=1.45,ext=.25),
   dict(fam='VOL_CLIMAX',cross=cross,lb=5,stretch=1.0,os=35,climax=3.0,roll=.80,vol_lo=1.10,vol_hi=2.5,body=.45,ar_lo=.8,ar_hi=1.4,ext=.3),
  ]
 return out

def crossm(x,m):
 if m=='NONE':return pd.Series(True,index=x.index)
 if m=='OTHER_DOWN':return x.other60<0
 if m=='OTHER_RECOVER':return (x.other60>0)&(x.other240<0)
 raise ValueError

def evs(x,c):
 lvl=x[f'hi{c["lb"]}'];br=(x.close>lvl)&(x.close.shift(1)<=lvl);ext=(x.close-lvl)/x.m5_atr
 m=br&crossm(x,c['cross'])&(x.vr>=c['vol_lo'])&(x.vr<=c['vol_hi'])&(x.bull>=c['body'])&(x.m5_ar2>=c['ar_lo'])&(x.m5_ar2<=c['ar_hi'])&(ext>.001)&(ext<=c['ext'])&x.contig1440
 if c['fam']=='FAILED_BREAK':
  m&=x[f'sweep{c["sweep"]}_10']&(x.h1_dist18<=c['dist'])&x.higher_low5
  stopref=x[f'sweepLow{c["sweep"]}']
 elif c['fam']=='EXHAUST':
  osflag=x.m15_os30 if c['os']==30 else x.m15_os35
  m&=(x.h1_dist18<=-c['stretch'])&osflag&(x.vmax30>=c['climax'])&(x.m5_ar_roll<=c['roll'])&x.higher_low5
  stopref=x.lo30
 else:
  osflag=x.m15_os35;m&=(x.ret60_absatr>=c['stretch'])&osflag&(x.vmax30>=c['climax'])&(x.m5_ar_roll<=c['roll'])&x.higher_low5
  stopref=x.lo30
 out=[]
 for i in np.flatnonzero(m.fillna(False).to_numpy()):
  if np.isfinite(lvl.iloc[i]) and np.isfinite(stopref.iloc[i]) and np.isfinite(x.m5_atr.iloc[i]):out.append((int(i),float(lvl.iloc[i]),float(stopref.iloc[i])))
 return out

MGT=[('R075',.12,.75,360),('R10',.12,1.0,720),('R125',.15,1.25,1440),('R15',.15,1.5,1440),('R20',.20,2.0,2880)]

def sim(x,ev,mg,a,b,s=0.):
 name,buf,rr,hold=mg;T=x.ts.to_numpy();O=x.open.to_numpy();H=x.high.to_numpy();L=x.low.to_numpy();C=x.close.to_numpy();A=x.m5_atr.to_numpy();st=ts(pd.Timestamp(a,tz='UTC'));en=ts(pd.Timestamp(b,tz='UTC'));ei=int(np.searchsorted(T,en));out=[];free=-1
 for sig,lvl,sr in ev:
  if T[sig]<st or T[sig]>=en-60:continue
  i=sig+1
  if i<free or i>=ei:continue
  entry=O[i]*(1+SLIP);stop=sr-buf*A[sig];risk=(entry-stop)/entry
  if not(.003<=risk<=.06):continue
  tgt=entry*(1+risk*rr);cost=FEE+s/2;j=i;last=min(ei-1,i+hold);net=None;reason='TIME'
  while j<=last:
   if j>i:cost+=FUND_LONG/YEAR_MIN
   if L[j]<=stop:
    q=min(O[j],stop)*(1-SLIP);net=q/entry-1-cost-(FEE+s/2)*q/entry;reason='STOP';break
   if H[j]>=tgt:
    q=tgt*(1-SLIP);net=q/entry-1-cost-(FEE+s/2)*q/entry;reason='TARGET';break
   j+=1
  if net is None:
   j=last;q=C[j]*(1-SLIP);net=q/entry-1-cost-(FEE+s/2)*q/entry
  out.append({'opened':int(T[i]),'closed':int(T[j]),'net':float(net),'reason':reason});free=j+COOLDOWN
 return out

def search(asset,x):
 cand=[]
 for c in cfgs():
  ev=evs(x,c)
  if len(ev)<15:continue
  for mg in MGT:
   y22=met(sim(x,ev,mg,'2022-01-01','2023-01-01'));y23=met(sim(x,ev,mg,'2023-01-01','2024-01-01'))
   if min(y22['n'],y23['n'])<4 or min(y22['avg'] or -9,y23['avg'] or -9)<=0:continue
   s22=met(sim(x,ev,mg,'2022-01-01','2023-01-01',STRESS));s23=met(sim(x,ev,mg,'2023-01-01','2024-01-01',STRESS))
   if min(s22['avg'] or -9,s23['avg'] or -9)<=0:continue
   v24=met(sim(x,ev,mg,'2024-01-01','2025-01-01'));v25=met(sim(x,ev,mg,'2025-01-01','2026-01-01'))
   sv24=met(sim(x,ev,mg,'2024-01-01','2025-01-01',STRESS));sv25=met(sim(x,ev,mg,'2025-01-01','2026-01-01',STRESS))
   if min(v24['n'],v25['n'])<4 or min(v24['avg'] or -9,v25['avg'] or -9)<=0:continue
   if min(sv24['avg'] or -9,sv25['avg'] or -9)<=0 or min(v24['pf'],v25['pf'])<1.05:continue
   score=120*min(v24['avg'],v25['avg'])+.4*min(v24['win_rate'],v25['win_rate'])+.2*min(v24['pf'],v25['pf'])
   cand.append((score,c,mg,ev,y22,y23,v24,v25,sv24,sv25))
 cand.sort(key=lambda z:z[0],reverse=True);out=[]
 for rank,z in enumerate(cand[:20],1):
  sc,c,mg,ev,y22,y23,v24,v25,sv24,sv25=z;test=met(sim(x,ev,mg,'2026-04-04','2026-10-04'));stress=met(sim(x,ev,mg,'2026-04-04','2026-10-04',STRESS));blocks=[met(sim(x,ev,mg,a,b)) for a,b in [('2026-04-04','2026-06-01'),('2026-06-01','2026-08-01'),('2026-08-01','2026-10-04')]];pb=sum((m['avg'] or -9)>0 for m in blocks);passed=test['n']>=6 and (test['avg'] or -9)>0 and test['pf']>=1.2 and (stress['avg'] or -9)>0 and stress['pf']>=1.1 and pb>=2
  out.append({'rank':rank,'rule':c,'management':{'name':mg[0],'buf':mg[1],'rr':mg[2],'hold':mg[3]},'y2022':y22,'y2023':y23,'v2024':v24,'v2025':v25,'s2024':sv24,'s2025':sv25,'test2026':test,'test_stress':stress,'blocks':blocks,'positive_blocks':pb,'passed':passed})
 print(asset,'REV_LONG candidates',len(cand),'finalists',len(out),'passes',sum(r['passed'] for r in out),flush=True);return out

def main():
 asset=os.environ.get('RESEARCH_ASSET','ETH').upper();raw=load(asset);other=load('BTC' if asset=='ETH' else 'ETH');x=add(raw,features(raw),other);rows=search(asset,x);out={'generated_at':datetime.now(timezone.utc).isoformat(),'asset':asset,'finalists':rows,'passes':[r for r in rows if r['passed']]};(OUT/'result.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False,default=jd));print('VERITAS_REV_LONG_PASSES='+json.dumps(out['passes'][:8],separators=(',',':'),default=jd),flush=True)
if __name__=='__main__':main()
