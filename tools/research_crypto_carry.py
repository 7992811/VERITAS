"""VERITAS BTC/ETH spot-perpetual carry research.

Fixed market-neutral rules, no directional prediction and no parameter search.
At entry: long spot + short USD-M perpetual, equal notionals.
PnL = spot leg + perp short leg + received funding - fees/slippage.
Entry occurs only AFTER a funding settlement, using prior realized funding history;
there is no use of future/predicted funding.

Research only.
"""
from __future__ import annotations
import argparse,csv,io,json,time,zipfile
from datetime import datetime,timezone
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import numpy as np,pandas as pd

from research_crypto_manager_library import FEE,SLIP,ts
from research_crypto_external_holdout import load_old
from research_crypto_manager_library import load

OUT=Path("carry_out");CACHE=OUT/"data";OUT.mkdir(exist_ok=True);CACHE.mkdir(exist_ok=True)
SYMS={'BTC':'BTCUSDT','ETH':'ETHUSDT'}
LEG_COST=FEE+SLIP

def jd(o):
 if isinstance(o,np.integer):return int(o)
 if isinstance(o,np.floating):return float(o)
 if isinstance(o,np.bool_):return bool(o)
 raise TypeError(type(o).__name__)

def get(url):
 for k in range(4):
  try:
   with urlopen(Request(url,headers={'User-Agent':'VERITAS-carry/1.0'}),timeout=45) as r:return r.read()
  except HTTPError as e:
   if e.code==404:return None
   if k==3:raise
  except Exception:
   if k==3:raise
  time.sleep(1+k)
 return None

def parse_funding(blob):
 if blob is None:return []
 with zipfile.ZipFile(io.BytesIO(blob)) as z:raw=z.read(z.namelist()[0]).decode()
 rows=list(csv.reader(io.StringIO(raw)))
 if not rows:return []
 header=[s.strip() for s in rows[0]]
 has_header=any(x in header for x in ('calc_time','fundingTime','funding_time'))
 out=[]
 if has_header:
  idx={k:i for i,k in enumerate(header)}
  tkey=next((k for k in ('calc_time','fundingTime','funding_time') if k in idx),None)
  rkey=next((k for k in ('last_funding_rate','fundingRate','funding_rate') if k in idx),None)
  if tkey is None or rkey is None:return []
  for r in rows[1:]:
   try:out.append((int(r[idx[tkey]]),float(r[idx[rkey]])))
   except Exception:pass
 else:
  # Binance Vision historically uses calc_time, interval_hours, rate.
  for r in rows:
   try:
    t=int(r[0]);rate=float(r[-1]);out.append((t,rate))
   except Exception:pass
 return out

def funding(asset):
 p=CACHE/f'{asset}_funding.pkl'
 if p.exists():return pd.read_pickle(p)
 sym=SYMS[asset];parts=[]
 for per in pd.period_range('2020-01','2026-08',freq='M'):
  u=f'https://data.binance.vision/data/futures/um/monthly/fundingRate/{sym}/{sym}-fundingRate-{per}.zip'
  b=get(u)
  if b:parts.extend(parse_funding(b))
 for day in pd.date_range('2026-09-01','2026-10-03',freq='D',tz='UTC'):
  ds=day.strftime('%Y-%m-%d')
  u=f'https://data.binance.vision/data/futures/um/daily/fundingRate/{sym}/{sym}-fundingRate-{ds}.zip'
  b=get(u)
  if b:parts.extend(parse_funding(b))
 f=pd.DataFrame(parts,columns=['ms','rate']).drop_duplicates('ms').sort_values('ms')
 f['ts']=(f.ms//1000).astype('int64')
 f=f[(f.ts>=ts('2020-01-01T00:00:00Z'))&(f.ts<ts('2026-10-04T00:00:00Z'))].reset_index(drop=True)
 f['mean3']=f.rate.rolling(3).mean().shift(1);f['mean9']=f.rate.rolling(9).mean().shift(1)
 mu=f.rate.rolling(90,min_periods=30).mean().shift(1);sd=f.rate.rolling(90,min_periods=30).std().shift(1)
 f['rate_z']=(f.rate-mu)/sd.replace(0,np.nan)
 f.to_pickle(p);print(asset,'funding_rows',len(f),flush=True);return f

def futures_1h(asset):
 p=CACHE/f'{asset}_fut1h.pkl'
 if p.exists():return pd.read_pickle(p)
 sym=SYMS[asset];rows=[]
 for per in pd.period_range('2019-12','2026-08',freq='M'):
  u=f'https://data.binance.vision/data/futures/um/monthly/klines/{sym}/1h/{sym}-1h-{per}.zip'
  b=get(u)
  if not b:continue
  with zipfile.ZipFile(io.BytesIO(b)) as z:raw=z.read(z.namelist()[0]).decode()
  for r in csv.reader(io.StringIO(raw)):
   try:
    t=int(r[0]);t=t//1000000 if t>10**14 else (t//1000 if t>10**11 else t)
    rows.append((t,float(r[1]),float(r[4])))
   except Exception:pass
 # Daily recent files.
 for day in pd.date_range('2026-09-01','2026-10-03',freq='D',tz='UTC'):
  ds=day.strftime('%Y-%m-%d');u=f'https://data.binance.vision/data/futures/um/daily/klines/{sym}/1h/{sym}-1h-{ds}.zip';b=get(u)
  if not b:continue
  with zipfile.ZipFile(io.BytesIO(b)) as z:raw=z.read(z.namelist()[0]).decode()
  for r in csv.reader(io.StringIO(raw)):
   try:
    t=int(r[0]);t=t//1000000 if t>10**14 else (t//1000 if t>10**11 else t)
    rows.append((t,float(r[1]),float(r[4])))
   except Exception:pass
 f=pd.DataFrame(rows,columns=['ts','open','close']).drop_duplicates('ts').sort_values('ts').reset_index(drop=True);f.to_pickle(p);return f

def spot_1h(asset):
 old=load_old(asset);new=load(asset);r=pd.concat([old,new],ignore_index=True).drop_duplicates('ts').sort_values('ts')
 r.index=pd.to_datetime(r.ts,unit='s',utc=True);g=r.resample('1h',closed='left',label='left')
 z=g.agg({'open':'first','close':'last'}).dropna();z['ts']=z.index.astype('int64')//10**9
 return z.reset_index(drop=True)[['ts','open','close']]

def attach_prices(asset,f):
 s=spot_1h(asset);p=futures_1h(asset)
 # Strict causal sequencing:
 # funding settlement at T is known at/after T;
 # basis observation uses the COMPLETED 1h bar immediately before T;
 # entry is delayed to the first 1h open >= T+1h.
 ft=f.ts.to_numpy(np.int64)
 st=s.ts.to_numpy(np.int64);pt=p.ts.to_numpy(np.int64)
 obs_si=np.searchsorted(st,ft,side='left')-1
 obs_pi=np.searchsorted(pt,ft,side='left')-1
 et=ft+3600
 ent_si=np.searchsorted(st,et,side='left')
 ent_pi=np.searchsorted(pt,et,side='left')
 good=(obs_si>=0)&(obs_pi>=0)&(ent_si<len(s))&(ent_pi<len(p))
 z=f.loc[good].copy()
 obs_si=obs_si[good];obs_pi=obs_pi[good];ent_si=ent_si[good];ent_pi=ent_pi[good]
 z['basis_obs_ts']=np.minimum(st[obs_si]+3600,pt[obs_pi]+3600)
 z['basis']=p.close.to_numpy()[obs_pi]/s.close.to_numpy()[obs_si]-1
 z['spot_open']=s.open.to_numpy()[ent_si]
 z['perp_open']=p.open.to_numpy()[ent_pi]
 z['entry_ts']=np.maximum(st[ent_si],pt[ent_pi])
 # Guard: every feature timestamp must precede execution.
 z=z[z.basis_obs_ts<=z.entry_ts].copy()
 mu=z.basis.rolling(90,min_periods=30).mean().shift(1);sd=z.basis.rolling(90,min_periods=30).std().shift(1)
 z['basis_z']=(z.basis-mu)/sd.replace(0,np.nan)
 return z.reset_index(drop=True),s,p

RULES=[
 {'name':'PERSISTENT_CARRY','entry':lambda r:(r.mean3>=.00010 and r.mean9>=.000075 and r.rate>=.00005 and r.basis>=-.001),
  'exit':lambda r:(r.mean3<=.000025 or r.rate<0 or r.basis<-.003),'max_events':42},
 {'name':'EXTREME_CARRY','entry':lambda r:(r.rate_z>=1.0 and r.mean3>0 and r.basis_z>=.50),
  'exit':lambda r:(r.rate_z<=0 or r.mean3<=0 or r.basis_z<=0),'max_events':21},
]

def nearest_open(df,t):
 i=np.searchsorted(df.ts.to_numpy(),t,side='left')
 if i>=len(df):return None
 return float(df.open.iloc[i]),int(df.ts.iloc[i])

def close_before(df,t):
 i=np.searchsorted(df.ts.to_numpy(),t,side='left')-1
 if i<0:return None
 return float(df.close.iloc[i]),int(df.ts.iloc[i]+3600)

def simulate(z,s,p,rule,stress=0.):
 trades=[];i=0
 while i<len(z):
  r=z.iloc[i]
  if not np.isfinite(r.mean3) or not np.isfinite(r.basis_z) or not rule['entry'](r):
   i+=1;continue
  entry_i=i;entry_ts=int(r.entry_ts);spot0=float(r.spot_open);perp0=float(r.perp_open)
  funding_sum=0.;j=i+1;reason='MAX'
  last=min(len(z)-1,i+rule['max_events'])
  while j<=last:
   rr=z.iloc[j]
   # Position is already open, so funding paid at this future settlement is
   # earned. USD-M funding is charged on CURRENT position value. With fixed
   # coin quantity q=N/perp0, funding relative to initial notional is
   # rate * current_mark/perp0.
   mk=close_before(p,int(rr.ts))
   if mk is not None:
    mark,_=mk
    funding_sum+=float(rr.rate)*(mark/perp0)
   if rule['exit'](rr):
    reason='CARRY_END';break
   j+=1
  if j>last:j=last
  exit_event_ts=int(z.iloc[j].ts)+3600
  so=nearest_open(s,exit_event_ts);po=nearest_open(p,exit_event_ts)
  if so is None or po is None:break
  spot1,exit_ts_s=so;perp1,exit_ts_p=po
  # Equal initial-notional long spot + short perp.
  spot_ratio=spot1/spot0
  perp_ratio=perp1/perp0
  spot_pnl=spot_ratio-1.
  perp_pnl=1.-perp_ratio
  # Entry executions each cost one initial notional. Exit costs scale with
  # the actual exit notionals. SLIP is modeled as a cost here (not in prices).
  execution_cost=LEG_COST*(1.+1.+spot_ratio+perp_ratio)
  costs=execution_cost+stress
  net_one_notional=spot_pnl+perp_pnl+funding_sum-costs
  gross_capital_return=net_one_notional/2.
  trades.append({'opened':entry_ts,'closed':max(exit_ts_s,exit_ts_p),'net':float(gross_capital_return),
                 'raw_pair_pnl':float(net_one_notional),'funding':float(funding_sum),'spot':float(spot_pnl),
                 'perp':float(perp_pnl),'execution_cost':float(execution_cost),
                 'spot_exit_ratio':float(spot_ratio),'perp_exit_ratio':float(perp_ratio),
                 'events':int(j-entry_i),'reason':reason})
  i=j+1
 return trades

def metric(v):
 if not v:return {'n':0,'win_rate':None,'avg':None,'sum':0.,'pf':0.,'dd':0.}
 a=np.array([x['net'] for x in v]);pos=a[a>0].sum();neg=-a[a<0].sum();eq=np.cumsum(a);pk=np.maximum.accumulate(np.r_[0.,eq])[1:]
 return {'n':len(a),'win_rate':float((a>0).mean()),'avg':float(a.mean()),'sum':float(a.sum()),
         'pf':float(pos/neg) if neg else 99.,'dd':float((pk-eq).max())}

def by_year(tr):
 out={}
 for y in range(2020,2027):
  a=ts(f'{y}-01-01T00:00:00Z');b=ts(f'{y+1}-01-01T00:00:00Z') if y<2026 else ts('2026-10-04T00:00:00Z')
  out[str(y)]=metric([x for x in tr if a<=x['opened']<b])
 return out

def run(asset):
 f=funding(asset);z,s,p=attach_prices(asset,f);out={'asset':asset,'generated_at':datetime.now(timezone.utc).isoformat(),'rules':{}}
 for rule in RULES:
  base=simulate(z,s,p,rule,0.);st=simulate(z,s,p,rule,.0005)
  out['rules'][rule['name']]={'years':by_year(base),'stress5':by_year(st),'aggregate':metric(base),'aggregate_stress5':metric(st),'trades':base}
  print(asset,rule['name'],json.dumps({k:out['rules'][rule['name']][k] for k in ('years','aggregate','aggregate_stress5')},separators=(',',':'),default=jd),flush=True)
 q=OUT/asset.lower();q.mkdir(parents=True,exist_ok=True);(q/'result.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False,default=jd))
 print('VERITAS_CARRY='+json.dumps({'asset':asset,'rules':{k:{kk:v[kk] for kk in ('years','aggregate','aggregate_stress5')} for k,v in out['rules'].items()}},separators=(',',':'),default=jd),flush=True)

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--asset',choices=['BTC','ETH'],required=True);args=ap.parse_args();run(args.asset)
if __name__=='__main__':main()
