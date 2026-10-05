"""Funding-haircut and operational-cost stress for frozen STRONG_21D carry.

Research only. Uses exactly the same historical trades/signals as the frozen
candidate. Revalues each completed trade under:
- 25%, 50%, 75% haircut to realized funding income
- extra +20bp / +50bp pair-level execution/operational cost

This asks whether the edge depends on receiving historical funding perfectly.
No re-entry or parameter changes are allowed.
"""
from __future__ import annotations
import json
from datetime import datetime,timezone
from pathlib import Path
import numpy as np

import research_crypto_adaptive_carry as ac
from research_crypto_manager_library import ts

OUT=Path("carry_haircut_out");OUT.mkdir(exist_ok=True)
POL=next(p for p in ac.POLICIES if p["name"]=="STRONG_21D")
SCENARIOS=[
 ("FUND25",.25,0.0),("FUND50",.50,0.0),("FUND75",.75,0.0),
 ("FUND25_COST20",.25,.0020),("FUND50_COST20",.50,.0020),("FUND75_COST20",.75,.0020),
 ("FUND50_COST50",.50,.0050),
]

def metrics(tr):
 if not tr:return {"n":0,"win_rate":None,"avg":None,"sum":0.,"pf":0.,"dd":0.}
 a=np.asarray([x["net"] for x in tr],float);p=a[a>0].sum();n=-a[a<0].sum()
 eq=np.cumsum(a);pk=np.maximum.accumulate(np.r_[0.,eq])[1:]
 return {"n":len(a),"win_rate":float((a>0).mean()),"avg":float(a.mean()),"sum":float(a.sum()),
         "pf":float(p/n) if n else 99.,"dd":float((pk-eq).max())}

def revalue(base,haircut,extra):
 out=[]
 for r in base:
  pair=float(r["spot"])+float(r["perp"])+(1-haircut)*float(r["funding"])-ac.PAIR_RT-extra
  z=dict(r);z["net"]=pair/2.;out.append(z)
 return out

def yearly(tr):
 out={}
 for y in range(2020,2027):
  a=ts(f"{y}-01-01T00:00:00Z");b=ts(f"{y+1}-01-01T00:00:00Z") if y<2026 else ts("2026-10-04T00:00:00Z")
  out[str(y)]=metrics([r for r in tr if a<=r["opened"]<b])
 return out

def bootstrap(tr,nboot=3000,block=3):
 if len(tr)<12:return {"n":len(tr),"p_positive":None,"p05_sum":None,"p95_dd":None}
 a=np.asarray([r["net"] for r in tr],float);n=len(a);rng=np.random.default_rng(20261005)
 starts=np.arange(max(1,n-block+1));sums=[];dds=[]
 for _ in range(nboot):
  q=[]
  while len(q)<n:
   k=int(rng.choice(starts));q.extend(a[k:k+block].tolist())
  q=np.asarray(q[:n]);eq=np.cumsum(q);pk=np.maximum.accumulate(np.r_[0.,eq])[1:]
  sums.append(float(q.sum()));dds.append(float((pk-eq).max()))
 return {"n":n,"p_positive":float(np.mean(np.asarray(sums)>0)),
         "p05_sum":float(np.quantile(sums,.05)),"p95_dd":float(np.quantile(dds,.95))}

def run(asset):
 z,s,p=ac.prep(asset);base=ac.simulate(z,s,p,POL,0.0)
 out={"asset":asset,"base":metrics(base),"scenarios":{}}
 for name,h,e in SCENARIOS:
  tr=revalue(base,h,e);yr=yearly(tr);m=metrics(tr);boot=bootstrap(tr)
  represented=[v for v in yr.values() if v["n"]>=2]
  pos=sum((v["avg"] or -9)>0 for v in represented)
  out["scenarios"][name]={"funding_haircut":h,"extra_pair_cost":e,"metrics":m,"years":yr,
                          "bootstrap":boot,"represented_years":len(represented),"positive_years":pos}
  print(asset,name,"CARRY_HAIRCUT",json.dumps(out["scenarios"][name],separators=(",",":")),flush=True)
 return out

def main():
 out={"generated_at":datetime.now(timezone.utc).isoformat(),
      "method":"Same frozen STRONG_21D signals revalued under funding haircut and extra costs.",
      "assets":{a:run(a) for a in ("BTC","ETH")}}
 (OUT/"result.json").write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False))
 print("VERITAS_CARRY_HAIRCUT="+json.dumps({a:{k:{"metrics":v["metrics"],"bootstrap":v["bootstrap"],"positive_years":v["positive_years"],"represented_years":v["represented_years"]} for k,v in z["scenarios"].items()} for a,z in out["assets"].items()},separators=(",",":")),flush=True)

if __name__=="__main__":main()
