"""VERITAS external historical holdout 2020-2021 for frozen crypto modules.

IMPORTANT: no parameter search. This script only evaluates previously frozen rules
on a historical window not used by the 2022-2026 research loop.

Frozen modules:
1) ETH controlled-pullback SHORT.
2) BTC dual-pullback multi-horizon SHORT.
3) BTC bull-episode compression LONG (diagnostic frozen long candidate).

Research only.
"""
from __future__ import annotations
import csv,io,json,time,zipfile
from datetime import datetime,timezone
from pathlib import Path
from urllib.request import Request,urlopen
import numpy as np,pandas as pd

from research_crypto_manager_library import features,rs,align,ts,FEE,SLIP,FUND_LONG,FUND_SHORT,YEAR_MIN,met

OUT=Path('external_holdout_out');CACHE=OUT/'data';OUT.mkdir(exist_ok=True);CACHE.mkdir(exist_ok=True)
SYMS={'BTC':'BTCUSDT','ETH':'ETHUSDT'}

def jd(o):
 if isinstance(o,np.integer):return int(o)
 if isinstance(o,np.floating):return float(o)
 if isinstance(o,np.bool_):return bool(o)
 raise TypeError(type(o).__name__)

def get(url):
 for k in range(5):
  try:
   with urlopen(Request(url,headers={'User-Agent':'VERITAS-external-holdout/1.0'}),timeout=60) as r:return r.read()
  except Exception:
   if k==4:raise
   time.sleep(1+k)

def readzip(b):
 with zipfile.ZipFile(io.BytesIO(b)) as z:raw=z.read(z.namelist()[0]).decode()
 rows=[]
 for r in csv.reader(io.StringIO(raw)):
  if not r:continue
  t=int(r[0]);t=t//1000000 if t>10**14 else (t//1000 if t>10**11 else t)
  rows.append((t,float(r[1]),float(r[2]),float(r[3]),float(r[4]),float(r[5])))
 return pd.DataFrame(rows,columns=['ts','open','high','low','close','volume'])

def load_old(asset):
 p=CACHE/f'{asset}_2020_21.pkl'
 if p.exists():return pd.read_pickle(p)
 sym=SYMS[asset];parts=[]
 for per in pd.period_range('2019-12','2021-12',freq='M'):
  u=f'https://data.binance.vision/data/spot/monthly/klines/{sym}/1m/{sym}-1m-{per}.zip'
  print(asset,'download',per,flush=True);parts.append(readzip(get(u)))
 f=pd.concat(parts,ignore_index=True).drop_duplicates('ts').sort_values('ts').reset_index(drop=True)
 f.to_pickle(p);return f

def add_eth(x):
 x['vol_med10']=x.volume.rolling(10).median().shift(1);x['vol_med_prev30']=x.volume.shift(10).rolling(30).median();x['vcon']=x.vol_med10/x.vol_med_prev30.replace(0,np.nan)
 anchor=x.close.shift(40);trough=x.low.shift(10).rolling(30).min();pbh=x.high.rolling(10).max().shift(1);imp=(anchor-trough).clip(lower=0);x['depS']=(pbh-trough)/imp.replace(0,np.nan)
 return x

def eth_events(x):
 lvl=x.lo10;cross=(x.close<lvl)&(x.close.shift(1)>=lvl);ext=(lvl-x.close)/x.m5_atr
 m=cross&(x.h1_trend==-1)&(x.h4_trend==-1)&(x.d1_trend!=1)&x.pbS
 m&=(x.vr>=1.1)&(x.bear>=.35)&(x.m5_atr_ratio>=.9)&(ext>.002)&(ext<=.4)
 m&=(x.depS>=.4)&(x.depS<.55)&(x.vcon>=.5)&(x.vcon<.8)
 out=[]
 for i in np.flatnonzero(m.fillna(False).to_numpy()):
  if np.isfinite(x.hi30.iloc[i]):out.append((int(i),float(lvl.iloc[i]),float(x.hi30.iloc[i])))
 return out

def add_btc(x,raw):
 m15=rs(raw,15);h1=rs(raw,60)
 m15['pbS']=(m15.high>=m15.sma18).rolling(4).max().shift(1);h1['pbS']=(h1.high>=h1.sma18).rolling(3).max().shift(1)
 for n in (6,):h1[f'hi{n}']=h1.high.rolling(n).max().shift(1)
 a15=align(m15,x.ts.to_numpy());a1=align(h1,x.ts.to_numpy())
 x['m15_pbS2']=a15.pbS.fillna(0).to_numpy()>0;x['h1_pbS2']=a1.pbS.fillna(0).to_numpy()>0;x['h1_hi6']=a1.hi6.to_numpy()
 # for frozen BTC long diagnostic
 for n in (20,):x[f'hi{n}']=raw.high.rolling(n).max().shift(1);x[f'lo{n}']=raw.low.rolling(n).min().shift(1)
 return x

def btc_short_events(x):
 lvl=x.lo20;cross=(x.close<lvl)&(x.close.shift(1)>=lvl);ext=(lvl-x.close)/x.m5_atr
 m=(x.h1_trend==-1)&(x.h4_trend==-1)&(x.d1_trend!=1)&x.m15_pbS2&x.h1_pbS2
 m&=cross&(x.vr>=1.45)&(x.bear>=.5)&(x.m5_atr_ratio>=.95)&(x.m5_atr_ratio<=1.45)&(ext>.002)&(ext<=.25)
 return [(int(i),float(lvl.iloc[i]),float(x.h1_hi6.iloc[i])) for i in np.flatnonzero(m.fillna(False).to_numpy()) if np.isfinite(x.h1_hi6.iloc[i])]

def sim_short(x,ev,asset,stress=0.):
 T=x.ts.to_numpy();O=x.open.to_numpy();H=x.high.to_numpy();L=x.low.to_numpy();C=x.close.to_numpy();A5=x.m5_atr.to_numpy();A1=x.h1_atr.to_numpy();out=[];free=-1
 for sig,lvl,sr in ev:
  i=sig+1
  if i<free or i>=len(T):continue
  entry=O[i]*(1-SLIP)
  if asset=='ETH':stop=sr+.2*A5[sig];risk=(stop-entry)/entry;hold=1440
  else:stop=sr+.1*A1[sig];risk=(stop-entry)/entry;hold=1440
  if not(.003<=risk<=.05):continue
  tgt=entry*(1-2*risk);cost=FEE+stress/2;j=i;last=min(len(T)-1,i+hold);net=None;reason='TIME'
  while j<=last:
   if j>i:cost+=FUND_SHORT/YEAR_MIN
   if H[j]>=stop:
    q=max(O[j],stop)*(1+SLIP);net=1-q/entry-cost-(FEE+stress/2)*q/entry;reason='STOP';break
   if L[j]<=tgt:
    q=tgt*(1+SLIP);net=1-q/entry-cost-(FEE+stress/2)*q/entry;reason='TARGET';break
   j+=1
  if net is None:
   j=last;q=C[j]*(1+SLIP);net=1-q/entry-cost-(FEE+stress/2)*q/entry
  out.append({'opened':int(T[i]),'closed':int(T[j]),'net':float(net),'reason':reason});free=j+15
 return out

def by_year(x,trades):
 rows={}
 for y in (2020,2021):
  a=ts(f'{y}-01-01T00:00:00Z');b=ts(f'{y+1}-01-01T00:00:00Z');rows[str(y)]=met([t for t in trades if a<=t['opened']<b])
 return rows

def main():
 out={'generated_at':datetime.now(timezone.utc).isoformat(),'method':'Frozen-rule external historical control; no parameter selection on 2020-2021.','assets':{}}
 for asset in ('ETH','BTC'):
  raw=load_old(asset);x=features(raw)
  if asset=='ETH':
   x=add_eth(x);ev=eth_events(x)
  else:
   x=add_btc(x,raw);ev=btc_short_events(x)
  base=sim_short(x,ev,asset,0.);s5=sim_short(x,ev,asset,.0005);s10=sim_short(x,ev,asset,.0010);s20=sim_short(x,ev,asset,.0020)
  out['assets'][asset]={'base':by_year(x,base),'stress5':by_year(x,s5),'stress10':by_year(x,s10),'stress20':by_year(x,s20),'n_events':len(ev)}
  print(asset,'EXTERNAL',json.dumps(out['assets'][asset],separators=(',',':'),default=jd),flush=True)
 (OUT/'result.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False,default=jd))
 print('VERITAS_EXTERNAL_HOLDOUT='+json.dumps(out['assets'],separators=(',',':'),default=jd),flush=True)

if __name__=='__main__':main()
