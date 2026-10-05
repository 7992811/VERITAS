"""VERITAS adaptive strategy committee.

Research only. Uses a SMALL FIXED library of economically distinct modules.
No parameter optimization by calendar year. Each module is either ACTIVE or
NO_TRADE based only on its own completed historical trades before the decision.

Fixed module library:
- trend fast
- trend slow
- failed breakout / mean reversion
- volatility expansion
- carry-aligned trend / crowded carry fade
- macro-confirmed trend
- on-chain trend when real data are available
- ETH/BTC pair mean reversion and pair momentum

Online gate (fixed ex ante):
- trailing 365d completed history n >= 6
- trailing 365d net EV > 0 after +5bp stress
- PF >= 1.05
- trailing 90d must not have materially negative EV
- exponentially weighted EV must be positive
- module score determines 25/50/75% notional
- opposite signals at similar confidence cancel to NO_TRADE

The evaluation is chronological and causal. No future year is used to activate a
module in the current month.
"""
from __future__ import annotations
import json, math
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd

import research_crypto_arsenal_expansion as ar
import research_crypto_carry as carry
from research_crypto_external_holdout import load_old
from research_crypto_manager_library import load, ts

OUT=Path("adaptive_committee_out");OUT.mkdir(exist_ok=True)
STRESS=0.0005

MODULE_ACTION={
 "TREND_FAST":"P30",
 "TREND_SLOW":"P30",
 "MEAN_REVERSION":"F15",
 "FAILED_BREAKOUT":"F15",
 "VOL_EXPANSION":"P30",
 "CARRY_TREND":"F20",
 "CROWDED_FADE":"F15",
 "MACRO_TREND":"P30",
 "ONCHAIN_TREND":"P30",
}
ACTION={a["name"]:a for a in ar.ACTIONS}

def jd(o):
    if isinstance(o,np.integer):return int(o)
    if isinstance(o,np.floating):return float(o)
    if isinstance(o,np.bool_):return bool(o)
    raise TypeError(type(o).__name__)

def combine(asset):
    old=load_old(asset);new=load(asset)
    f=pd.concat([old,new],ignore_index=True).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    return f[(f.ts>=ts("2020-01-01T00:00:00Z"))&(f.ts<ts("2026-10-04T00:00:00Z"))].reset_index(drop=True)

def module_streams(asset,raw):
    x=ar.common_features(raw)
    # Missing derivative/macro/on-chain history naturally becomes NaN and causes
    # those modules to remain inactive instead of fabricating signals.
    x=ar.attach_funding_basis(asset,x,raw)
    x=ar.attach_macro(x)
    x=ar.attach_onchain(asset,x)
    ev=ar.build_events(asset,x)
    streams={}
    for fam,aname in MODULE_ACTION.items():
        z=[e for e in ev if e["family"]==fam]
        streams[fam]=ar.simulate(x,z,ACTION[aname],
                                 "2020-01-01T00:00:00Z","2026-10-04T00:00:00Z",0.0)
    return x,streams

def pair_streams(btc5,eth5):
    p=ar.pair_trade_frame(btc5,eth5)
    return {
      "PAIR_MEANREV": ar.sim_pair(p,"PAIR_MEANREV","2020-01-01T00:00:00Z","2026-10-04T00:00:00Z",0.0),
      "PAIR_MOMENTUM": ar.sim_pair(p,"PAIR_MOMENTUM","2020-01-01T00:00:00Z","2026-10-04T00:00:00Z",0.0),
    }

def carry_stream(asset):
    f=carry.funding(asset)
    z,s,p=carry.attach_prices(asset,f)
    rule=next(r for r in carry.RULES if r["name"]=="PERSISTENT_CARRY")
    rows=carry.simulate(z,s,p,rule,0.0)
    # Market-neutral sleeve: direction 0, independent from directional exposure.
    for r in rows:
        r["d"]=0
    return rows

def sleeve(module):
    if module.startswith("BTC_"): return "BTC_DIRECTIONAL"
    if module.startswith("ETH_"): return "ETH_DIRECTIONAL"
    if module=="PAIR_MEANREV" or module=="PAIR_MOMENTUM": return "PAIR"
    if module=="BTC_CARRY": return "BTC_CARRY"
    if module=="ETH_CARRY": return "ETH_CARRY"
    return module

def metrics(vals):
    a=np.asarray(vals,float)
    if not len(a):return {"n":0,"avg":None,"pf":0.0,"win_rate":None,"dd":0.0}
    pos=a[a>0].sum();neg=-a[a<0].sum();eq=np.cumsum(a);pk=np.maximum.accumulate(np.r_[0.,eq])[1:]
    return {"n":int(len(a)),"avg":float(a.mean()),"pf":float(pos/neg) if neg else 99.,
            "win_rate":float((a>0).mean()),"dd":float((pk-eq).max())}

def ew_ev(rows,cutoff,half_life_days=120):
    if not rows:return None
    c=pd.Timestamp(cutoff,unit="s",tz="UTC")
    vals=[];weights=[]
    for r in rows:
        age=(c-pd.Timestamp(r["closed"],unit="s",tz="UTC")).total_seconds()/86400
        if age<0:continue
        w=0.5**(age/half_life_days)
        vals.append(float(r["net"])-STRESS);weights.append(w)
    if not vals:return None
    return float(np.average(vals,weights=weights))

def gate(rows,cutoff):
    # Only trades CLOSED before the decision time are known.
    hist=[r for r in rows if r["closed"]<cutoff and r["closed"]>=cutoff-365*86400]
    recent=[r for r in hist if r["closed"]>=cutoff-90*86400]
    m=metrics([r["net"]-STRESS for r in hist])
    r=metrics([r["net"]-STRESS for r in recent])
    ew=ew_ev(hist,cutoff)
    active=(m["n"]>=6 and (m["avg"] or -9)>0 and m["pf"]>=1.05 and
            ew is not None and ew>0 and (r["n"]<2 or (r["avg"] or -9)>-0.00025))
    if not active:return {"active":False,"long":m,"recent":r,"ew_ev":ew,"score":-99.,"size":0.0}
    # Confidence rewards evidence and conservatism, not just point return.
    score=80*min(m["avg"],.01)+0.20*min(m["pf"],3.)+0.25*(m["win_rate"] or 0)+50*min(ew,.01)
    if m["n"]>=16 and m["pf"]>=1.30 and ew>=.0015:size=.75
    elif m["n"]>=10 and m["pf"]>=1.15 and ew>=.00075:size=.50
    else:size=.25
    return {"active":True,"long":m,"recent":r,"ew_ev":ew,"score":float(score),"size":size}

def monthly_decisions(streams,start="2021-01-01",end="2026-10-01"):
    months=pd.date_range(start,end,freq="MS",tz="UTC")
    out=[]
    for t0 in months:
        t1=min(t0+pd.offsets.MonthBegin(1),pd.Timestamp("2026-10-04",tz="UTC"))
        c=int(t0.timestamp());e=int(t1.timestamp())
        gates={name:gate(rows,c) for name,rows in streams.items()}
        # Collect signals opened this month from currently active modules.
        cand=[]
        for name,rows in streams.items():
            g=gates[name]
            if not g["active"]:continue
            for r in rows:
                if c<=r["opened"]<e:
                    cand.append({**r,"module":name,"score":g["score"],"size":g["size"],
                                 "weighted_net":float(r["net"])*g["size"]})
        cand.sort(key=lambda r:(r["opened"],-r["score"]))
        # One position per sleeve. BTC, ETH, pair-RV and market-neutral carry
        # can coexist; conflicting directional signals inside the same sleeve cancel.
        kept=[];free_by={};i=0
        while i<len(cand):
            t=cand[i]["opened"]
            same=[]
            while i<len(cand) and cand[i]["opened"]==t:
                same.append(cand[i]);i+=1
            for sl in sorted(set(sleeve(q["module"]) for q in same)):
                group=[q for q in same if sleeve(q["module"])==sl]
                if t<free_by.get(sl,-1):continue
                group.sort(key=lambda r:r["score"],reverse=True)
                best=group[0]
                opp=[q for q in group[1:] if best.get("d",0)!=0 and q.get("d",0)==-best.get("d",0)]
                if opp and opp[0]["score"]>=best["score"]*.92:
                    continue
                kept.append(best);free_by[sl]=best["closed"]+15*60
        out.append({"month":t0.strftime("%Y-%m"),"gates":gates,"trades":kept})
    return out

def summarize_months(months):
    alltr=[t for m in months for t in m["trades"]]
    vals=[t["weighted_net"] for t in alltr]
    base=metrics(vals)
    years={}
    for y in range(2021,2027):
        a=ts(f"{y}-01-01T00:00:00Z");b=ts(f"{y+1}-01-01T00:00:00Z") if y<2026 else ts("2026-10-04T00:00:00Z")
        z=[t["weighted_net"] for t in alltr if a<=t["opened"]<b]
        years[str(y)]=metrics(z)
    by_module={}
    for name in sorted(set(t["module"] for t in alltr)):
        z=[t["weighted_net"] for t in alltr if t["module"]==name]
        by_module[name]=metrics(z)
    return {"aggregate":base,"years":years,"by_module":by_module,"n_trades":len(alltr),"trades":alltr}

def main():
    raw={a:combine(a) for a in ("BTC","ETH")}
    x={};streams={}
    for a in ("BTC","ETH"):
        x[a],s=module_streams(a,raw[a])
        for name,rows in s.items():streams[f"{a}_{name}"]=rows
    pair=pair_streams(x["BTC"],x["ETH"])
    streams.update(pair)
    streams["BTC_CARRY"]=carry_stream("BTC")
    streams["ETH_CARRY"]=carry_stream("ETH")
    months=monthly_decisions(streams)
    result=summarize_months(months)
    out={"generated_at":datetime.now(timezone.utc).isoformat(),
         "method":"Fixed multi-strategy library with causal rolling 365d/90d EV gate; no future-year selection.",
         "gate":{"lookback_days":365,"recent_days":90,"min_trades":6,"min_pf":1.05,"stress":STRESS},
         "result":result,
         "monthly_gates":[{"month":m["month"],"active":[k for k,v in m["gates"].items() if v["active"]]} for m in months]}
    (OUT/"result.json").write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False,default=jd))
    print("VERITAS_ADAPTIVE_COMMITTEE="+json.dumps({"aggregate":result["aggregate"],"years":result["years"],
          "by_module":result["by_module"],"n_trades":result["n_trades"]},separators=(",",":"),default=jd),flush=True)

if __name__=="__main__":main()
