"""VERITAS quarter-hour order-flow research.

Hypothesis motivated by documented quarter-hour algorithmic activity in crypto
perpetuals: order imbalance at :00/:15/:30/:45 may forecast 4-12h returns.

Fixed causal design:
- Binance USD-M 5m bars
- only 5m bars beginning at quarter-hour boundaries
- taker-buy imbalance z-scored against same clock phase over trailing 14d
- volume burst vs trailing same-phase median
- optional 1h/4h trend non-opposition
- enter at NEXT 5m open
- time exit at 4h, 8h, or 12h; structural ATR stop
- actual historical funding applied to perpetual position
- discovery 2022-23 chooses horizon/variant; 2024 and 2025 must both validate;
  2026 inspected only after pass.
"""
from __future__ import annotations
import argparse,json,math
from datetime import datetime,timezone
from pathlib import Path
import numpy as np,pandas as pd

import research_crypto_derivative_router as der
import research_crypto_carry as carry
from research_crypto_manager_library import FEE,SLIP,ts

OUT=Path("quarter_hour_out");OUT.mkdir(exist_ok=True)
STRESS=.0005

def jd(o):
 if isinstance(o,np.integer):return int(o)
 if isinstance(o,np.floating):return float(o)
 if isinstance(o,np.bool_):return bool(o)
 raise TypeError(type(o).__name__)

def prep(asset):
 f=der.download_futures_5m(asset).copy()
 f=f[(f.open_ts>=ts("2021-12-01T00:00:00Z"))&(f.open_ts<ts("2026-10-04T00:00:00Z"))].reset_index(drop=True)
 dt=pd.to_datetime(f.open_ts,unit='s',utc=True)
 f['minute']=dt.dt.minute
 f['phase']=dt.dt.minute%15
 f['quarter']=(f['phase']==0)
 f['imb']=2*f.taker_buy/f.volume.replace(0,np.nan)-1
 # same clock-phase normalization, trailing 14d = 14*24*4 quarter-hour samples
 q=f[f.quarter].copy()
 win=14*24*4
 q['imb_mu']=q.imb.rolling(win,min_periods=7*24*4).mean().shift(1)
 q['imb_sd']=q.imb.rolling(win,min_periods=7*24*4).std().shift(1)
 q['imb_z']=(q.imb-q.imb_mu)/q.imb_sd.replace(0,np.nan)
 q['vol_med']=q.volume.rolling(win,min_periods=7*24*4).median().shift(1)
 q['vr']=q.volume/q.vol_med.replace(0,np.nan)
 # ATR
 pc=f.close.shift(1);tr=pd.concat([f.high-f.low,(f.high-pc).abs(),(f.low-pc).abs()],axis=1).max(axis=1)
 f['atr20']=tr.rolling(20).mean()
 q=q.merge(f[['open_ts','atr20']],on='open_ts',how='left')
 # 1h/4h causal EMA trend from completed 5m bars.
 x=f.copy();x.index=pd.to_datetime(x.open_ts,unit='s',utc=True)
 def tf(m):
  g=x.resample(f'{m}min',closed='left',label='right')
  z=g.agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'})
  z=z[g.size()==m//5].copy()
  z['e18']=z.close.ewm(span=18,adjust=False).mean();z['e50']=z.close.ewm(span=50,adjust=False).mean()
  z['trend']=np.select([(z.close>z.e18)&(z.e18>z.e50),(z.close<z.e18)&(z.e18<z.e50)],[1,-1],default=0)
  return z
 h1=tf(60);h4=tf(240)
 qi=pd.to_datetime(q.open_ts,unit='s',utc=True)
 q['h1']=h1.trend.reindex(qi,method='ffill').to_numpy()
 q['h4']=h4.trend.reindex(qi,method='ffill').to_numpy()
 return f,q

def funding_map(asset):
 f=carry.funding(asset)
 return f[['ts','rate']].copy()

def funding_pnl(fund,open_ts,close_ts,d):
 z=fund[(fund.ts>open_ts)&(fund.ts<=close_ts)]
 # positive rate: longs pay, shorts receive
 return float((-d*z.rate).sum()) if len(z) else 0.

VARIANTS=[
 {'name':'FLOW_ONLY','z':1.0,'vr':1.20,'gate':'NONE'},
 {'name':'FLOW_TREND','z':1.0,'vr':1.20,'gate':'NONOPPOSE'},
 {'name':'FLOW_STRONG','z':1.5,'vr':1.35,'gate':'NONOPPOSE'},
]
HORIZONS=[48,96,144] # 4h,8h,12h in 5m bars

def events(q,v):
 out=[]
 for r in q.itertuples():
  if not np.isfinite(r.imb_z) or not np.isfinite(r.vr) or abs(r.imb_z)<v['z'] or r.vr<v['vr']:continue
  d=1 if r.imb_z>0 else -1
  if v['gate']=='NONOPPOSE' and (r.h1==-d or r.h4==-d):continue
  out.append((int(r.open_ts),d,float(r.atr20),float(r.imb_z),float(r.vr)))
 return out

def met(tr):
 a=np.asarray([x['net'] for x in tr],float)
 if not len(a):return {'n':0,'win_rate':None,'avg':None,'sum':0.,'pf':0.,'dd':0.}
 p=a[a>0].sum();n=-a[a<0].sum();eq=np.cumsum(a);pk=np.maximum.accumulate(np.r_[0.,eq])[1:]
 return {'n':len(a),'win_rate':float((a>0).mean()),'avg':float(a.mean()),'sum':float(a.sum()),
         'pf':float(p/n) if n else 99.,'dd':float((pk-eq).max())}

def sim(f,fund,ev,hold,start,end,stress=0.):
 T=f.open_ts.to_numpy();O=f.open.to_numpy();H=f.high.to_numpy();L=f.low.to_numpy();C=f.close.to_numpy()
 st=ts(start);en=ts(end);i0=np.searchsorted(T,st);i1=np.searchsorted(T,en);out=[];free=-1
 for sig_ts,d,atr,z,vr in ev:
  si=np.searchsorted(T,sig_ts)
  if si<i0 or si>=i1-1:continue
  i=si+1
  if i<free or i>=i1:continue
  entry=O[i]*(1+d*SLIP);stop=entry-d*1.5*atr
  risk=d*(entry-stop)/entry
  if not(.002<=risk<=.04):continue
  j=i;last=min(i1-1,i+hold);reason='TIME'
  while j<=last:
   if (L[j]<=stop if d>0 else H[j]>=stop):
    q=(min(O[j],stop) if d>0 else max(O[j],stop))*(1-d*SLIP);reason='STOP';break
   j+=1
  if j>last:j=last;q=C[j]*(1-d*SLIP)
  gross=d*(q/entry-1)
  fp=funding_pnl(fund,int(T[i]),int(T[j]),d)
  net=gross+fp-2*FEE-2*SLIP-stress
  out.append({'opened':int(T[i]),'closed':int(T[j]),'d':d,'net':float(net),'funding':fp,'z':z,'vr':vr,'reason':reason})
  free=j+3
 return out

def yr(f,fund,ev,hold,y,stress=0.):
 return met(sim(f,fund,ev,hold,f'{y}-01-01T00:00:00Z',f'{y+1}-01-01T00:00:00Z',stress))

def run(asset):
 f,q=prep(asset);fund=funding_map(asset);cand=[]
 for v in VARIANTS:
  ev=events(q,v)
  for hold in HORIZONS:
   m22=yr(f,fund,ev,hold,2022);m23=yr(f,fund,ev,hold,2023)
   s22=yr(f,fund,ev,hold,2022,STRESS);s23=yr(f,fund,ev,hold,2023,STRESS)
   if m22['n']<20 or m23['n']<20:continue
   if (m22['avg'] or -9)<=0 or (m23['avg'] or -9)<=0:continue
   if m22['pf']<1.05 or m23['pf']<1.05 or (s22['avg'] or -9)<=0 or (s23['avg'] or -9)<=0:continue
   score=120*min(m22['avg'],m23['avg'])+.3*min(m22['win_rate'],m23['win_rate'])+.2*min(m22['pf'],m23['pf'])
   cand.append((score,v,hold,ev,{'2022':m22,'2023':m23},{'2022':s22,'2023':s23}))
 cand.sort(key=lambda z:z[0],reverse=True)
 out={'asset':asset,'generated_at':datetime.now(timezone.utc).isoformat(),'selected':None}
 if not cand:
  out['reason']='NO_DISCOVERY_EDGE'
 else:
  sc,v,hold,ev,disc,ds=cand[0]
  v24=yr(f,fund,ev,hold,2024);v25=yr(f,fund,ev,hold,2025);s24=yr(f,fund,ev,hold,2024,STRESS);s25=yr(f,fund,ev,hold,2025,STRESS)
  valid=v24['n']>=20 and v25['n']>=20 and (v24['avg'] or -9)>0 and (v25['avg'] or -9)>0 and v24['pf']>=1.05 and v25['pf']>=1.05 and (s24['avg'] or -9)>0 and (s25['avg'] or -9)>0
  sel={'variant':v,'hold_bars':hold,'hold_hours':hold*5/60,'discovery':disc,'discovery_stress':ds,
       'validation':{'2024':v24,'2025':v25},'validation_stress':{'2024':s24,'2025':s25},'validated':valid}
  if valid:
   test=met(sim(f,fund,ev,hold,'2026-04-04T00:00:00Z','2026-10-04T00:00:00Z',0.))
   tst=met(sim(f,fund,ev,hold,'2026-04-04T00:00:00Z','2026-10-04T00:00:00Z',STRESS))
   sel['test_2026']=test;sel['test_stress']=tst
  out['selected']=sel
 p=OUT/asset.lower();p.mkdir(parents=True,exist_ok=True);(p/'result.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False,default=jd))
 print('VERITAS_QUARTER_HOUR='+json.dumps(out,separators=(',',':'),default=jd),flush=True)

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--asset',choices=['BTC','ETH'],required=True);args=ap.parse_args();run(args.asset)
if __name__=='__main__':main()
