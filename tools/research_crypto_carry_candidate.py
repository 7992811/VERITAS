"""Frozen central carry candidate evaluation for shadow admission.

The candidate is NOT selected by best historical return. It is the middle
theory-led policy from the previously fixed neighborhood:
    STRONG_21D

BTC and ETH are evaluated independently, unchanged over 2020-2026.

This file measures:
- same-signal +5/+10/+20bp execution stress
- half-year stability
- compounded sequential return
- exposure and average holding time
- max loss streak
- worst hedged pair price MAE

It can earn SHADOW_CANDIDATE status only. It can never set LIVE=true.
"""
from __future__ import annotations
import json,math
from datetime import datetime,timezone
from pathlib import Path
import numpy as np,pandas as pd

import research_crypto_adaptive_carry as ac
from research_crypto_manager_library import ts

OUT=Path("carry_candidate_out");OUT.mkdir(exist_ok=True)
POL=next(p for p in ac.POLICIES if p["name"]=="STRONG_21D")

def met(tr):
 return ac.met(tr)

def compounded(tr):
 if not tr:return 0.0
 a=np.asarray([r["net"] for r in tr],float)
 return float(np.prod(1+a)-1)

def streak(tr):
 best=cur=0
 for r in tr:
  if r["net"]<=0:cur+=1;best=max(best,cur)
  else:cur=0
 return int(best)

def exposure(tr,start,end):
 if not tr:return {"fraction":0.0,"avg_hold_days":None,"median_hold_days":None,"max_hold_days":None}
 secs=np.asarray([max(0,r["closed"]-r["opened"]) for r in tr],float)
 total=max(1,end-start)
 return {"fraction":float(secs.sum()/total),
         "avg_hold_days":float(secs.mean()/86400),
         "median_hold_days":float(np.median(secs)/86400),
         "max_hold_days":float(secs.max()/86400)}

def halfyears(tr):
 rows={}
 for y in range(2020,2027):
  for h,(m0,m1) in enumerate(((1,7),(7,13)),1):
   a=pd.Timestamp(year=y,month=m0,day=1,tz="UTC")
   if y==2026 and h==2:
    b=pd.Timestamp("2026-10-04T00:00:00Z")
   else:
    b=pd.Timestamp(year=y+(1 if m1==13 else 0),month=(1 if m1==13 else m1),day=1,tz="UTC")
   aa=int(a.timestamp());bb=int(b.timestamp())
   q=[r for r in tr if aa<=r["opened"]<bb]
   rows[f"{y}H{h}"]=met(q)
 return rows

def summary(tr,start,end):
 m=met(tr)
 blocks=halfyears(tr)
 represented=[v for v in blocks.values() if v["n"]>=2]
 positive=sum((v["avg"] or -9)>0 for v in represented)
 return {
   "metrics":m,
   "compounded":compounded(tr),
   "exposure":exposure(tr,start,end),
   "max_loss_streak":streak(tr),
   "worst_trade":float(min((r["net"] for r in tr),default=0.0)),
   "best_trade":float(max((r["net"] for r in tr),default=0.0)),
   "worst_pair_mae":float(min((r.get("pair_price_mae",0.0) for r in tr),default=0.0)),
   "halfyears":blocks,
   "represented_halfyears":len(represented),
   "positive_halfyears":positive,
 }

def run(asset):
 z,s,p=ac.prep(asset)
 start=ts("2020-01-01T00:00:00Z");end=ts("2026-10-04T00:00:00Z")
 b=ac.simulate(z,s,p,POL,0.0)
 s5=ac.simulate(z,s,p,POL,.0005)
 s10=ac.simulate(z,s,p,POL,.0010)
 s20=ac.simulate(z,s,p,POL,.0020)
 base=summary(b,start,end);st20=summary(s20,start,end)
 boot=ac.bootstrap(b,nboot=5000,block=3)

 crit={
   "min_trades_40":base["metrics"]["n"]>=40,
   "win_rate_75":(base["metrics"]["win_rate"] or 0)>=.75,
   "positive_expectancy":(base["metrics"]["avg"] or -9)>0,
   "stress20_positive":(st20["metrics"]["avg"] or -9)>0,
   "stress20_win_rate_65":(st20["metrics"]["win_rate"] or 0)>=.65,
   "bootstrap_99":(boot["p_positive"] or 0)>=.99,
   "pair_mae_under_3pct":base["worst_pair_mae"]>=-.03,
   "halfyear_breadth":base["represented_halfyears"]>=8 and
                      base["positive_halfyears"]>=max(7,math.ceil(.75*base["represented_halfyears"])),
 }
 shadow=all(crit.values())
 return {
   "asset":asset,"policy":POL,
   "base":base,"stress5":summary(s5,start,end),"stress10":summary(s10,start,end),
   "stress20":st20,"bootstrap":boot,"criteria":crit,
   "status":"SHADOW_CANDIDATE" if shadow else "RESEARCH_ONLY",
   "live_allowed":False,
 }

def main():
 out={"generated_at":datetime.now(timezone.utc).isoformat(),
      "method":"Frozen central adaptive carry policy; no parameter reselection.",
      "assets":{}}
 for a in ("BTC","ETH"):
  out["assets"][a]=run(a)
  z=out["assets"][a]
  print(a,"CARRY_CANDIDATE",json.dumps({
   "status":z["status"],"base":z["base"]["metrics"],"compounded":z["base"]["compounded"],
   "stress20":z["stress20"]["metrics"],"bootstrap":z["bootstrap"],
   "exposure":z["base"]["exposure"],"positive_halfyears":z["base"]["positive_halfyears"],
   "represented_halfyears":z["base"]["represented_halfyears"],"criteria":z["criteria"]
  },separators=(",",":")),flush=True)
 (OUT/"result.json").write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False))
 print("VERITAS_CARRY_CANDIDATE="+json.dumps({a:{k:v[k] for k in ("status","criteria","live_allowed")} for a,v in out["assets"].items()},separators=(",",":")),flush=True)

if __name__=="__main__":main()
