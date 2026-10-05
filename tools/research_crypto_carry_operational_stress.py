"""VERITAS carry legging / hedge-delay operational stress.

Research only. Uses the frozen STRONG_21D carry trades from the exact-accounting
model and the real 1-minute spot path around each modeled entry/exit.

Purpose:
- quantify the damage if one leg fills before the other;
- stress 1m / 5m / 15m hedge delays;
- estimate a conservative short-leg margin proxy from the maximum spot rally
  while the carry position is open.

The hedge-delay penalty assumes the UNFAVOURABLE leg remains temporarily naked:
the worst absolute spot move over the delay window is charged against one
notional. Since the carry portfolio uses two equal initial notionals, capital
return penalty = move / 2 at entry and again at exit.

This is deliberately conservative and is not an execution forecast.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd

import research_crypto_adaptive_carry as ac
from research_crypto_external_holdout import load_old
from research_crypto_manager_library import load, ts

OUT=Path("carry_operational_stress_out");OUT.mkdir(exist_ok=True)
POL=next(p for p in ac.POLICIES if p["name"]=="STRONG_21D")
DELAYS=(1,5,15)

def combine(asset):
    old=load_old(asset);new=load(asset)
    z=pd.concat([old,new],ignore_index=True).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    return z[(z.ts>=ts("2019-12-01T00:00:00Z"))&(z.ts<ts("2026-10-04T00:00:00Z"))].reset_index(drop=True)

def metrics(tr):
    if not tr:return {"n":0,"win_rate":None,"avg":None,"sum":0.,"pf":0.,"dd":0.}
    a=np.asarray([x["net"] for x in tr],float);p=a[a>0].sum();n=-a[a<0].sum()
    eq=np.cumsum(a);pk=np.maximum.accumulate(np.r_[0.,eq])[1:]
    return {"n":len(a),"win_rate":float((a>0).mean()),"avg":float(a.mean()),
            "sum":float(a.sum()),"pf":float(p/n) if n else 99.,"dd":float((pk-eq).max())}

def adverse_abs(raw,t,mins):
    T=raw.ts.to_numpy(np.int64);O=raw.open.to_numpy(float);H=raw.high.to_numpy(float);L=raw.low.to_numpy(float)
    i=np.searchsorted(T,int(t),side="left")
    if i>=len(raw):return None
    ref=float(O[i])
    j=np.searchsorted(T,int(t)+mins*60,side="right")
    if j<=i:j=i+1
    j=min(j,len(raw))
    if ref<=0:return None
    up=float(np.max(H[i:j])/ref-1.)
    dn=float(1.-np.min(L[i:j])/ref)
    return max(0.,up,dn)

def short_adverse_proxy(raw,opened,closed):
    T=raw.ts.to_numpy(np.int64);O=raw.open.to_numpy(float);H=raw.high.to_numpy(float)
    i=np.searchsorted(T,int(opened),side="left")
    j=np.searchsorted(T,int(closed),side="right")
    if i>=len(raw) or j<=i:return None
    ref=float(O[i])
    if ref<=0:return None
    return float(max(0.,np.max(H[i:j])/ref-1.))

def revalue(base,raw,delay):
    out=[]
    for r in base:
        e=adverse_abs(raw,r["opened"],delay)
        x=adverse_abs(raw,r["closed"],delay)
        if e is None or x is None:continue
        # Each naked leg is one of two equal capital notionals.
        penalty=.5*(e+x)
        z=dict(r);z["entry_delay_adverse"]=float(e);z["exit_delay_adverse"]=float(x)
        z["legging_penalty"]=float(penalty);z["net"]=float(r["net"]-penalty)
        z["short_adverse_proxy"]=short_adverse_proxy(raw,r["opened"],r["closed"])
        out.append(z)
    return out

def percentile(vals,q):
    a=np.asarray([v for v in vals if v is not None and np.isfinite(v)],float)
    return float(np.quantile(a,q)) if len(a) else None

def yearly(tr):
    out={}
    for y in range(2020,2027):
        a=ts(f"{y}-01-01T00:00:00Z");b=ts(f"{y+1}-01-01T00:00:00Z") if y<2026 else ts("2026-10-04T00:00:00Z")
        out[str(y)]=metrics([r for r in tr if a<=r["opened"]<b])
    return out

def run(asset):
    z,s,p=ac.prep(asset);base=ac.simulate(z,s,p,POL,0.0);raw=combine(asset)
    out={"asset":asset,"base":metrics(base),"delays":{}}
    proxy=[short_adverse_proxy(raw,r["opened"],r["closed"]) for r in base]
    out["short_margin_proxy"]={
      "max_adverse_rally":max([v for v in proxy if v is not None],default=None),
      "p95_adverse_rally":percentile(proxy,.95),
      "p99_adverse_rally":percentile(proxy,.99),
      "interpretation":"Spot 1m rally used as proxy for short-perpetual adverse move; not a liquidation-price model."
    }
    for d in DELAYS:
        tr=revalue(base,raw,d)
        pen=[r["legging_penalty"] for r in tr]
        out["delays"][f"{d}m"]={
          "metrics":metrics(tr),"years":yearly(tr),
          "avg_legging_penalty":float(np.mean(pen)) if pen else None,
          "p95_legging_penalty":percentile(pen,.95),
          "worst_legging_penalty":max(pen) if pen else None,
        }
        print(asset,d,"MIN_LEGGING",json.dumps(out["delays"][f"{d}m"],separators=(",",":")),flush=True)
    return out

def main():
    out={"generated_at":datetime.now(timezone.utc).isoformat(),
         "method":"Same frozen carry trades, pessimistically revalued for naked-leg delays using real 1m spot extremes.",
         "assets":{a:run(a) for a in ("BTC","ETH")}}
    (OUT/"result.json").write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False))
    print("VERITAS_CARRY_OPERATIONAL="+json.dumps({a:{"base":z["base"],"delays":z["delays"],"short_margin_proxy":z["short_margin_proxy"]} for a,z in out["assets"].items()},separators=(",",":")),flush=True)

if __name__=="__main__":main()
