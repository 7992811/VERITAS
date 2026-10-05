"""VERITAS market-neutral ETH/BTC perpetual relative-value research.

Research only. Uses Binance USD-M perpetual 1h history and realized funding.
The trade is beta-normalized:
- long spread = long ETH perp, short beta-weighted BTC perp
- short spread = short ETH perp, long beta-weighted BTC perp

Rolling beta and spread z-score are causal. Three fixed theory-led mean-reversion
variants are evaluated unchanged across 2020-2026. No parameter mining.
"""
from __future__ import annotations
import json
from datetime import datetime,timezone
from pathlib import Path
import numpy as np,pandas as pd

import research_crypto_carry as carry
from research_crypto_manager_library import FEE,SLIP,ts

OUT=Path("rv_perp_out");OUT.mkdir(exist_ok=True)
STRESS=.0005
PAIR_COST=4*(FEE+SLIP)

VARIANTS=[
 {"name":"RV_Z2","entry_z":2.0,"exit_z":.40,"stop_z":3.5,"max_hours":168},
 {"name":"RV_Z25","entry_z":2.5,"exit_z":.50,"stop_z":4.0,"max_hours":168},
 {"name":"RV_Z3","entry_z":3.0,"exit_z":.75,"stop_z":4.5,"max_hours":240},
]

def jd(o):
 if isinstance(o,np.integer):return int(o)
 if isinstance(o,np.floating):return float(o)
 if isinstance(o,np.bool_):return bool(o)
 raise TypeError(type(o).__name__)

def prep():
 b=carry.futures_1h("BTC")[["ts","open","close"]].rename(columns={"open":"bo","close":"bc"})
 e=carry.futures_1h("ETH")[["ts","open","close"]].rename(columns={"open":"eo","close":"ec"})
 x=b.merge(e,on="ts",how="inner").sort_values("ts").reset_index(drop=True)
 x=x[(x.ts>=ts("2019-12-01T00:00:00Z"))&(x.ts<ts("2026-10-04T00:00:00Z"))].copy()
 rb=np.log(x.bc/x.bc.shift(1));re=np.log(x.ec/x.ec.shift(1))
 win=24*90;minp=24*45
 cov=re.rolling(win,min_periods=minp).cov(rb).shift(1)
 var=rb.rolling(win,min_periods=minp).var().shift(1)
 x["beta"]=(cov/var.replace(0,np.nan)).clip(.35,3.0)
 x["spread"]=np.log(x.ec)-x.beta*np.log(x.bc)
 x["sm"]=x.spread.rolling(win,min_periods=minp).mean().shift(1)
 x["ss"]=x.spread.rolling(win,min_periods=minp).std().shift(1)
 x["z"]=(x.spread-x.sm)/x.ss.replace(0,np.nan)
 return x

def funding_cum(asset,times):
 f=carry.funding(asset)[["ts","rate"]].copy().sort_values("ts")
 T=np.asarray(times,np.int64)
 vals=np.zeros(len(T),float)
 if f.empty:return vals
 ft=f.ts.to_numpy(np.int64);fr=f.rate.to_numpy(float)
 # cumulative realized funding up to each hourly timestamp
 cs=np.cumsum(fr)
 ix=np.searchsorted(ft,T,side="right")-1
 good=ix>=0
 vals[good]=cs[ix[good]]
 return vals

def metrics(tr):
 if not tr:return {"n":0,"win_rate":None,"avg":None,"sum":0.,"pf":0.,"dd":0.}
 a=np.asarray([r["net"] for r in tr],float)
 p=a[a>0].sum();n=-a[a<0].sum();eq=np.cumsum(a);pk=np.maximum.accumulate(np.r_[0.,eq])[1:]
 return {"n":len(a),"win_rate":float((a>0).mean()),"avg":float(a.mean()),"sum":float(a.sum()),
         "pf":float(p/n) if n else 99.,"dd":float((pk-eq).max())}

def sim(x,v,stress=0.):
 T=x.ts.to_numpy(np.int64);BO=x.bo.to_numpy(float);BC=x.bc.to_numpy(float)
 EO=x.eo.to_numpy(float);EC=x.ec.to_numpy(float);Z=x.z.to_numpy(float);B=x.beta.to_numpy(float)
 fbtc=funding_cum("BTC",T);feth=funding_cum("ETH",T)
 out=[];free=-1
 for sig in range(1,len(x)-2):
  if sig<free or not np.isfinite(Z[sig]) or not np.isfinite(B[sig]):continue
  side=1 if Z[sig]<=-v["entry_z"] else (-1 if Z[sig]>=v["entry_z"] else 0)
  if side==0:continue
  i=sig+1
  beta=float(B[sig])
  we=1/(1+abs(beta));wb=abs(beta)/(1+abs(beta))
  # next 1h open execution
  e0=EO[i]*(1+side*SLIP)
  bsign=-side
  b0=BO[i]*(1+bsign*SLIP)
  j=i;last=min(len(x)-1,i+v["max_hours"]);reason="TIME"
  while j<=last:
   z=float(Z[j]) if np.isfinite(Z[j]) else float(Z[sig])
   if side>0:
    if z>=-v["exit_z"]:reason="TARGET";break
    if z<=-v["stop_z"]:reason="STOP";break
   else:
    if z<=v["exit_z"]:reason="TARGET";break
    if z>=v["stop_z"]:reason="STOP";break
   j+=1
  if j>last:j=last
  e1=EC[j]*(1-side*SLIP)
  b1=BC[j]*(1-bsign*SLIP)
  epnl=we*side*(e1/e0-1)
  bpnl=wb*bsign*(b1/b0-1)
  # Positive funding: longs pay, shorts receive.
  fe=float(feth[j]-feth[i]);fb=float(fbtc[j]-fbtc[i])
  fpnl=we*(-side)*fe + wb*(-bsign)*fb
  cost=PAIR_COST+stress
  net=epnl+bpnl+fpnl-cost
  out.append({"opened":int(T[i]),"closed":int(T[j]),"net":float(net),
              "eth_pnl":float(epnl),"btc_pnl":float(bpnl),"funding":float(fpnl),
              "side":int(side),"beta":beta,"entry_z":float(Z[sig]),"exit_z":float(Z[j]),"reason":reason})
  free=j+1
 return out

def yearly(tr):
 out={}
 for y in range(2020,2027):
  a=ts(f"{y}-01-01T00:00:00Z");b=ts(f"{y+1}-01-01T00:00:00Z") if y<2026 else ts("2026-10-04T00:00:00Z")
  out[str(y)]=metrics([r for r in tr if a<=r["opened"]<b])
 return out

def bootstrap(tr,nboot=2000,block=4):
 if len(tr)<12:return {"n":len(tr),"p_positive":None,"p05_sum":None,"p95_dd":None}
 a=np.asarray([r["net"] for r in tr],float);n=len(a);rng=np.random.default_rng(20261005)
 starts=np.arange(max(1,n-block+1));sums=[];dds=[]
 for _ in range(nboot):
  z=[]
  while len(z)<n:
   k=int(rng.choice(starts));z.extend(a[k:k+block].tolist())
  z=np.asarray(z[:n],float);eq=np.cumsum(z);pk=np.maximum.accumulate(np.r_[0.,eq])[1:]
  sums.append(z.sum());dds.append((pk-eq).max())
 return {"n":n,"p_positive":float(np.mean(np.asarray(sums)>0)),
         "p05_sum":float(np.quantile(sums,.05)),"p95_dd":float(np.quantile(dds,.95))}

def main():
 x=prep();out={"generated_at":datetime.now(timezone.utc).isoformat(),
               "method":"Causal beta-neutral ETH/BTC perpetual relative-value; realized funding; fixed variants.",
               "variants":{}}
 for v in VARIANTS:
  b=sim(x,v,0.);s=sim(x,v,STRESS)
  out["variants"][v["name"]]={"params":v,"base":metrics(b),"stress5":metrics(s),
                               "years":yearly(b),"stress_years":yearly(s),
                               "bootstrap":bootstrap(b),"trades":b}
  print(v["name"],"RV_PERP",json.dumps({k:out["variants"][v["name"]][k] for k in ("base","stress5","years","stress_years","bootstrap")},separators=(",",":"),default=jd),flush=True)
 (OUT/"result.json").write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False,default=jd))
 print("VERITAS_RV_PERP="+json.dumps({k:{q:v[q] for q in ("base","stress5","years","stress_years","bootstrap")} for k,v in out["variants"].items()},separators=(",",":"),default=jd),flush=True)

if __name__=="__main__":main()
