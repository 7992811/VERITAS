"""VERITAS causal online regime policy for BTC and ETH.

Goal
----
Test whether recurrent market-state transitions can support a production-style
policy without a fixed strategy that assumes stationary edge.

Key constraints
---------------
- BTC and ETH are independent.
- 15m state engine; entry at NEXT 15m open.
- At every decision timestamp, only fully completed historical transition
  outcomes that were already observable are eligible.
- Rolling 730d memory with exponential decay; no future-year selection.
- Three fixed horizons: 4h, 12h, 1d.
- Exact transition statistics are preferred; current-state fallback is allowed
  only with a larger evidence requirement.
- Conservative expected value must remain positive after round-trip fees,
  slippage and an uncertainty haircut.
- NO_TRADE is the default.
- Fixed structural stop, break-even and trailing management.
- Results are reported chronologically for 2021-2026 with a +5bp stress rerun.

Research only. No production writes.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from research_crypto_external_holdout import load_old
from research_crypto_manager_library import load, ts, FEE, SLIP

OUT = Path("online_regime_out")
OUT.mkdir(exist_ok=True)

LOOKBACK_DAYS = 730
HALF_LIFE_DAYS = 180
HORIZONS = {"4H": 16, "12H": 48, "1D": 96}  # 15m bars
BAR = 900
BASE_RT_COST = 2 * (FEE + SLIP)
STRESS = 0.0005

POLICIES = {
    "BALANCED": {
        "exact_n": 24, "state_n": 55, "min_neff": 14.0,
        "min_wr": 0.52, "min_pf": 1.12, "min_lcb_net": 0.00015,
        "z_haircut": 0.67449,
    },
    "STRICT": {
        "exact_n": 36, "state_n": 80, "min_neff": 22.0,
        "min_wr": 0.56, "min_pf": 1.22, "min_lcb_net": 0.00050,
        "z_haircut": 1.00,
    },
}

def jd(o):
    if isinstance(o, np.integer): return int(o)
    if isinstance(o, np.floating): return float(o)
    if isinstance(o, np.bool_): return bool(o)
    raise TypeError(type(o).__name__)

def combine(asset):
    old = load_old(asset)
    new = load(asset)
    z = pd.concat([old, new], ignore_index=True)
    z = z.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    return z[(z.ts >= ts("2019-12-01T00:00:00Z")) &
             (z.ts < ts("2026-10-04T00:00:00Z"))].reset_index(drop=True)

def resample15(raw):
    z = raw.copy()
    z.index = pd.to_datetime(z.ts, unit="s", utc=True)
    g = z.resample("15min", closed="left", label="right")
    x = g.agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"})
    cnt = g.size()
    x = x[cnt == 15].copy()
    x["ts"] = (x.index.view("int64") // 10**9).astype(np.int64)
    return x.reset_index(drop=True)

def features(raw):
    x = resample15(raw)
    pc = x.close.shift(1)
    tr = pd.concat([x.high-x.low, (x.high-pc).abs(), (x.low-pc).abs()], axis=1).max(axis=1)
    x["atr"] = tr.rolling(20, min_periods=20).mean()
    x["atr_med"] = x.atr.rolling(96, min_periods=48).median().shift(1)
    x["atr_ratio"] = x.atr / x.atr_med.replace(0, np.nan)
    x["vmed"] = x.volume.rolling(96, min_periods=48).median().shift(1)
    x["vr"] = x.volume / x.vmed.replace(0, np.nan)

    x["ema18"] = x.close.ewm(span=18, adjust=False).mean()
    x["ema50"] = x.close.ewm(span=50, adjust=False).mean()
    x["ema200"] = x.close.ewm(span=200, adjust=False).mean()

    r = x.close.pct_change()
    x["ret16"] = x.close / x.close.shift(16) - 1
    x["ret48"] = x.close / x.close.shift(48) - 1
    x["ret96"] = x.close / x.close.shift(96) - 1
    x["eff16"] = (x.close/x.close.shift(16)-1).abs() / r.abs().rolling(16).sum().replace(0, np.nan)
    x["eff48"] = (x.close/x.close.shift(48)-1).abs() / r.abs().rolling(48).sum().replace(0, np.nan)

    x["m96"] = x.close.rolling(96, min_periods=96).mean().shift(1)
    x["s96"] = x.close.rolling(96, min_periods=96).std().shift(1)
    x["z96"] = (x.close-x.m96) / x.s96.replace(0, np.nan)

    rv = r.rolling(16, min_periods=16).std()
    x["rv"] = rv
    x["rv_med"] = rv.rolling(96, min_periods=48).median().shift(1)
    x["rv_ratio"] = rv / x.rv_med.replace(0, np.nan)

    x["lo8"] = x.low.rolling(8, min_periods=8).min().shift(1)
    x["hi8"] = x.high.rolling(8, min_periods=8).max().shift(1)
    x["lo4"] = x.low.rolling(4, min_periods=4).min().shift(1)
    x["hi4"] = x.high.rolling(4, min_periods=4).max().shift(1)

    span = (x.high-x.low).replace(0, np.nan)
    x["bull_body"] = (x.close-x.open)/span
    x["bear_body"] = (x.open-x.close)/span
    return x

def classify(x):
    x = x.copy()
    c=x.close.to_numpy(float); e18=x.ema18.to_numpy(float)
    e50=x.ema50.to_numpy(float); e200=x.ema200.to_numpy(float)
    r16=x.ret16.to_numpy(float); r96=x.ret96.to_numpy(float)
    eff=x.eff48.to_numpy(float); ar=x.atr_ratio.to_numpy(float)
    rv=x.rv_ratio.to_numpy(float); z=x.z96.to_numpy(float)
    vr=x.vr.to_numpy(float)

    st=np.full(len(x),"TRANSITION",dtype=object)

    m=(np.abs(z)<.85)&(eff<=.28)&(ar<=.90)
    st[m]="RANGE"

    m=(ar<=.78)&(rv<=.85)&(eff<=.25)
    st[m]="COMPRESSION"

    m=(c>e18)&(e18>e50)&(r16>0)&(eff>=.22)&(ar>=.85)&(ar<=1.45)
    st[m]="EARLY_UP"
    m=(c<e18)&(e18<e50)&(r16<0)&(eff>=.22)&(ar>=.85)&(ar<=1.45)
    st[m]="EARLY_DOWN"

    m=(c>e18)&(e18>e50)&(e50>e200)&(r96>0)&(eff>=.32)
    st[m]="TREND_UP"
    m=(c<e18)&(e18<e50)&(e50<e200)&(r96<0)&(eff>=.32)
    st[m]="TREND_DOWN"

    m=(z>=2.0)&(ar>=1.20)&(rv>=1.10)
    st[m]="EXTREME_UP"
    m=(z<=-2.0)&(ar>=1.20)&(rv>=1.10)
    st[m]="EXTREME_DOWN"

    m=(ar>=1.35)&(rv>=1.20)&(vr>=1.20)&(r16>0)
    st[m]="VOL_UP"
    m=(ar>=1.35)&(rv>=1.20)&(vr>=1.20)&(r16<0)
    st[m]="VOL_DOWN"

    x["state"]=st
    return x

def events(x):
    s=x.state.astype(str)
    changed=s.ne(s.shift(1))
    idx=np.flatnonzero((changed & s.ne("TRANSITION")).fillna(False).to_numpy())
    C=x.close.to_numpy(float); T=x.ts.to_numpy(np.int64)
    rows=[]
    for i in idx:
        if i<200: continue
        row={
            "i":int(i),"ts":int(T[i]),"prev":str(s.iloc[i-1]) if i else "START",
            "state":str(s.iloc[i]),"atr":float(x.atr.iloc[i]) if np.isfinite(x.atr.iloc[i]) else None,
            "lo8":float(x.lo8.iloc[i]) if np.isfinite(x.lo8.iloc[i]) else None,
            "hi8":float(x.hi8.iloc[i]) if np.isfinite(x.hi8.iloc[i]) else None,
        }
        for hn,h in HORIZONS.items():
            if i+h < len(x):
                row[f"ret_{hn}"]=float(C[i+h]/C[i]-1)
                row[f"end_i_{hn}"]=int(i+h)
            else:
                row[f"ret_{hn}"]=None
                row[f"end_i_{hn}"]=None
        rows.append(row)
    return pd.DataFrame(rows)

def prepare_history(ev):
    exact=defaultdict(dict)
    state=defaultdict(dict)
    for hn in HORIZONS:
        for key,g in ev.dropna(subset=[f"ret_{hn}",f"end_i_{hn}"]).groupby(["prev","state"],sort=False):
            exact[hn][key]={
                "ts":g.ts.to_numpy(np.int64),
                "end":g[f"end_i_{hn}"].to_numpy(np.int64),
                "ret":g[f"ret_{hn}"].to_numpy(float),
            }
        for key,g in ev.dropna(subset=[f"ret_{hn}",f"end_i_{hn}"]).groupby("state",sort=False):
            state[hn][key]={
                "ts":g.ts.to_numpy(np.int64),
                "end":g[f"end_i_{hn}"].to_numpy(np.int64),
                "ret":g[f"ret_{hn}"].to_numpy(float),
            }
    return exact,state

def window(hist, current_i, current_ts):
    if hist is None: return np.array([],float),np.array([],np.int64)
    hi=np.searchsorted(hist["end"],current_i,side="left")
    cutoff=current_ts-LOOKBACK_DAYS*86400
    lo=np.searchsorted(hist["ts"],cutoff,side="left")
    if hi<=lo: return np.array([],float),np.array([],np.int64)
    return hist["ret"][lo:hi], hist["ts"][lo:hi]

def weighted_stats(vals,times,now,direction,z_haircut):
    if len(vals)==0:return None
    r=direction*np.asarray(vals,float)
    age=(now-np.asarray(times,np.int64))/86400.0
    w=np.power(.5,age/HALF_LIFE_DAYS)
    sw=w.sum()
    if sw<=0:return None
    mean=float(np.sum(w*r)/sw)
    var=float(np.sum(w*(r-mean)**2)/sw)
    neff=float(sw**2/np.sum(w*w))
    se=math.sqrt(max(var,0)/max(neff,1))
    lcb=mean-z_haircut*se
    wr=float(np.sum(w*(r>0))/sw)
    pos=float(np.sum(w*np.where(r>0,r,0)))
    neg=float(-np.sum(w*np.where(r<0,r,0)))
    pfr=float(pos/neg) if neg>0 else 99.0
    return {"n":int(len(r)),"neff":neff,"mean":mean,"lcb":lcb,"wr":wr,"pf":pfr}

def choose_signal(row,exact,state,policy,cost):
    cfg=POLICIES[policy]
    candidates=[]
    for hn,h in HORIZONS.items():
        eh=exact[hn].get((row.prev,row.state))
        sh=state[hn].get(row.state)
        ev,et=window(eh,int(row.i),int(row.ts))
        source="EXACT"
        need=cfg["exact_n"]
        if len(ev)<need:
            ev,et=window(sh,int(row.i),int(row.ts))
            source="STATE"
            need=cfg["state_n"]
        if len(ev)<need: continue
        for d in (1,-1):
            st=weighted_stats(ev,et,int(row.ts),d,cfg["z_haircut"])
            if st is None or st["neff"]<cfg["min_neff"]: continue
            net_lcb=st["lcb"]-cost
            if st["wr"]<cfg["min_wr"] or st["pf"]<cfg["min_pf"] or net_lcb<cfg["min_lcb_net"]:
                continue
            score=net_lcb + .15*max(st["wr"]-.5,0) + .02*min(max(st["pf"]-1,0),2)
            candidates.append({
                "horizon":hn,"bars":h,"d":d,"source":source,"score":float(score),
                "net_lcb":float(net_lcb),"stats":st
            })
    if not candidates:return None
    candidates.sort(key=lambda z:z["score"],reverse=True)
    best=candidates[0]
    # If opposite directions are both nearly as convincing, refuse the trade.
    opp=[c for c in candidates[1:] if c["d"]==-best["d"]]
    if opp and opp[0]["score"]>=best["score"]*.90:return None
    return best

def trade(x,row,sig,stress):
    T=x.ts.to_numpy(np.int64);O=x.open.to_numpy(float);H=x.high.to_numpy(float)
    L=x.low.to_numpy(float);C=x.close.to_numpy(float);A=x.atr.to_numpy(float)
    i=int(row.i)+1
    if i>=len(x):return None
    d=int(sig["d"])
    slip=SLIP
    entry=O[i]*(1+d*slip)
    av=float(A[int(row.i)])
    if not np.isfinite(av) or av<=0:return None
    if d>0:
        sr=float(row.lo8) if row.lo8 is not None else entry-1.5*av
        stop=sr-.10*av
    else:
        sr=float(row.hi8) if row.hi8 is not None else entry+1.5*av
        stop=sr+.10*av
    risk=d*(entry-stop)/entry
    if not (.003<=risk<=.04):return None

    last=min(len(x)-1,i+int(sig["bars"]))
    be=False;reason="TIME";j=i
    while j<=last:
        if (L[j]<=stop if d>0 else H[j]>=stop):
            q=(min(O[j],stop) if d>0 else max(O[j],stop))*(1-d*slip)
            reason="STOP";break
        one=entry*(1+d*risk)
        if not be and (H[j]>=one if d>0 else L[j]<=one):
            be=True;stop=entry
        if be and j>i+4:
            a=max(i,j-4)
            tr=(np.min(L[a:j])-.08*A[j]) if d>0 else (np.max(H[a:j])+.08*A[j])
            if np.isfinite(tr) and d*(tr-stop)>0:
                stop=tr
        j+=1
    if j>last:
        j=last;q=C[j]*(1-d*slip)
    gross=d*(q/entry-1)
    cost=2*(FEE+slip)+stress
    return {
        "opened":int(T[i]),"closed":int(T[j]),"d":d,"net":float(gross-cost),
        "risk":float(risk),"state":str(row.state),"prev":str(row.prev),
        "horizon":sig["horizon"],"source":sig["source"],"net_lcb":sig["net_lcb"],
        "wr_est":sig["stats"]["wr"],"pf_est":sig["stats"]["pf"],"reason":reason,
    }

def metrics(rows):
    a=np.asarray([r["net"] for r in rows],float)
    if not len(a):
        return {"n":0,"win_rate":None,"avg":None,"sum":0.0,"pf":0.0,"dd":0.0}
    pos=a[a>0].sum();neg=-a[a<0].sum()
    eq=np.cumsum(a);pk=np.maximum.accumulate(np.r_[0.,eq])[1:]
    return {"n":int(len(a)),"win_rate":float((a>0).mean()),"avg":float(a.mean()),
            "sum":float(a.sum()),"pf":float(pos/neg) if neg>0 else 99.0,
            "dd":float((pk-eq).max())}

def run_policy(x,ev,policy,stress):
    exact,state=prepare_history(ev)
    rows=[];free=-1
    start=ts("2021-01-01T00:00:00Z")
    end=ts("2026-10-04T00:00:00Z")
    decision_cost=BASE_RT_COST+stress
    for r in ev.itertuples(index=False):
        if r.ts<start or r.ts>=end:continue
        if r.ts<free:continue
        sig=choose_signal(r,exact,state,policy,decision_cost)
        if sig is None:continue
        tr=trade(x,r,sig,stress)
        if tr is None:continue
        rows.append(tr);free=tr["closed"]+BAR
    return rows

def yearly(rows):
    out={}
    for y in range(2021,2027):
        a=ts(f"{y}-01-01T00:00:00Z")
        b=ts(f"{y+1}-01-01T00:00:00Z") if y<2026 else ts("2026-10-04T00:00:00Z")
        out[str(y)]=metrics([r for r in rows if a<=r["opened"]<b])
    return out

def bootstrap_risk(rows, nboot=1000, block=5):
    if len(rows)<12:
        return {"n":len(rows),"p_positive":None,"p05_sum":None,"p95_dd":None,"max_loss_streak":None}
    a=np.asarray([r["net"] for r in rows],float)
    rng=np.random.default_rng(20261005)
    sums=[];dds=[]
    n=len(a)
    starts=np.arange(max(1,n-block+1))
    for _ in range(nboot):
        z=[]
        while len(z)<n:
            s=int(rng.choice(starts))
            z.extend(a[s:s+block].tolist())
        z=np.asarray(z[:n],float)
        eq=np.cumsum(z);pk=np.maximum.accumulate(np.r_[0.,eq])[1:]
        sums.append(float(z.sum()));dds.append(float((pk-eq).max()))
    streak=0;best=0
    for v in a:
        streak=streak+1 if v<=0 else 0
        best=max(best,streak)
    return {"n":n,"p_positive":float(np.mean(np.asarray(sums)>0)),
            "p05_sum":float(np.quantile(sums,.05)),
            "p95_dd":float(np.quantile(dds,.95)),
            "max_loss_streak":int(best)}

def diagnostics(rows):
    if not rows:
        return {"by_state":{},"by_horizon":{},"bootstrap":bootstrap_risk([])}
    df=pd.DataFrame(rows)
    by_state={str(k):metrics(g.to_dict("records")) for k,g in df.groupby("state",sort=False)}
    by_h={str(k):metrics(g.to_dict("records")) for k,g in df.groupby("horizon",sort=False)}
    return {"by_state":by_state,"by_horizon":by_h,"bootstrap":bootstrap_risk(rows)}

def run_asset(asset):
    raw=combine(asset)
    x=classify(features(raw))
    ev=events(x)
    out={"asset":asset,"n_bars":int(len(x)),"n_state_events":int(len(ev)),"policies":{}}
    for p in POLICIES:
        base=run_policy(x,ev,p,0.0)
        stress=run_policy(x,ev,p,STRESS)
        out["policies"][p]={
            "base":metrics(base),"stress5":metrics(stress),
            "years":yearly(base),"stress_years":yearly(stress),
            "diagnostics":diagnostics(base),
            "trades":base,
        }
        print(asset,p,"ONLINE_REGIME",json.dumps({
            "base":out["policies"][p]["base"],
            "stress5":out["policies"][p]["stress5"],
            "years":out["policies"][p]["years"],
            "stress_years":out["policies"][p]["stress_years"],
        },separators=(",",":"),default=jd),flush=True)
    return out

def main():
    out={"generated_at":datetime.now(timezone.utc).isoformat(),
         "method":"Fully causal rolling regime-transition policy; 730d memory; no calendar-year parameter selection.",
         "policies":POLICIES,"assets":{}}
    for a in ("BTC","ETH"):
        out["assets"][a]=run_asset(a)
    (OUT/"result.json").write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False,default=jd))
    compact={a:{p:{k:v[k] for k in ("base","stress5","years","stress_years")} for p,v in z["policies"].items()} for a,z in out["assets"].items()}
    print("VERITAS_ONLINE_REGIME="+json.dumps(compact,separators=(",",":"),default=jd),flush=True)

if __name__=="__main__":
    main()
