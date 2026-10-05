"""VERITAS BTC/ETH options-volatility research using Deribit DVOL.

Research-only module. DVOL is treated as an independent implied-volatility state,
not as a replacement for price trend. Historical skew/smile data are not silently
invented; those remain live/shadow until reproducible historical surfaces exist.

Protocol: fixed hypotheses; 2022-23 discovery; frozen 2024+2025 validation;
only then Apr-Oct 2026 research inspection.
"""
from __future__ import annotations
import argparse, json, math, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import numpy as np
import pandas as pd

from research_crypto_manager_library import load, FEE, SLIP, TEST_START, TEST_END
from research_crypto_arsenal_expansion import (
    common_features, met, ts, execution_slip, STRESS, COOLDOWN
)

OUT=Path("options_vol_out"); OUT.mkdir(exist_ok=True)
CACHE=OUT/"data"; CACHE.mkdir(exist_ok=True)
MAX_HOLD=576

def jd(o):
    if isinstance(o,np.integer): return int(o)
    if isinstance(o,np.floating): return float(o)
    if isinstance(o,np.bool_): return bool(o)
    raise TypeError(type(o).__name__)

def get_json(url):
    last=None
    for k in range(5):
        try:
            with urlopen(Request(url,headers={"User-Agent":"VERITAS-options-research/1.0"}),timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last=e; time.sleep(1+k)
    raise RuntimeError(last)

def dvol(asset):
    p=CACHE/f"{asset}_dvol.pkl"
    if p.exists(): return pd.read_pickle(p)
    rows=[]
    start=pd.Timestamp("2022-01-01T00:00:00Z")
    end=pd.Timestamp("2026-10-04T00:00:00Z")
    cur=start
    while cur<end:
        nxt=min(cur+pd.Timedelta(days=180),end)
        q=urlencode({
            "currency":asset,
            "start_timestamp":int(cur.timestamp()*1000),
            "end_timestamp":int(nxt.timestamp()*1000),
            "resolution":"43200",
        })
        obj=get_json("https://www.deribit.com/api/v2/public/get_volatility_index_data?"+q)
        data=(obj.get("result") or {}).get("data") or []
        for r in data:
            if len(r)>=5: rows.append((int(r[0]//1000),float(r[1]),float(r[2]),float(r[3]),float(r[4])))
        cur=nxt
    z=pd.DataFrame(rows,columns=["ts","open","high","low","close"]).drop_duplicates("ts").sort_values("ts")
    if not z.empty: z.to_pickle(p)
    return z

def attach_dvol(asset,x):
    v=dvol(asset)
    out=x.copy()
    if v.empty:
        for c in ("dvol","dvol_z","dvol_chg","dvol_pct"): out[c]=np.nan
        return out
    v=v.copy()
    v["ma"]=v.close.rolling(60,min_periods=20).mean().shift(1)
    v["sd"]=v.close.rolling(60,min_periods=20).std().shift(1)
    v["z"]=(v.close-v.ma)/v.sd.replace(0,np.nan)
    v["chg"]=v.close/v.close.shift(2)-1
    v["pct"]=v.close.rolling(180,min_periods=60).rank(pct=True)
    vt=v.ts.to_numpy(np.int64); t=out.ts.to_numpy(np.int64)
    ix=np.searchsorted(vt,t,side="right")-1; good=ix>=0
    for src,dst in [("close","dvol"),("z","dvol_z"),("chg","dvol_chg"),("pct","dvol_pct")]:
        arr=np.full(len(out),np.nan)
        if good.any(): arr[good]=v[src].to_numpy()[ix[good]]
        out[dst]=arr
    return out

def cross(x,d,n):
    lvl=x[f"hi{n}"] if d>0 else x[f"lo{n}"]
    m=(d*(x.close-lvl)>0)&(d*(x.close.shift(1)-lvl)<=0)
    return m,lvl

def events(x):
    out=[]
    for d in (1,-1):
        body=x.bull if d>0 else x.bear
        # IV expansion + price break: options market confirms transition.
        m,lvl=cross(x,d,36)
        q=m&(x.dvol_chg>=.03)&(x.dvol_z>=.25)&(x.atr_ratio>=1.0)&(x.vr>=1.1)&(body>=.35)&(d*x.ret48>0)
        out+=collect(x,q,d,"IV_EXPANSION_BREAK",lvl)

        # Low implied vol + spot compression, then structural breakout.
        m,lvl=cross(x,d,72)
        q=m&(x.dvol_pct<=.30)&(x["squeeze"].shift(3)<=.80)&(x.atr_ratio>=1.05)&(x.vr>=1.2)&(body>=.40)
        out+=collect(x,q,d,"IV_COMPRESSION_BREAK",lvl)

        # IV shock + price exhaustion -> contrarian reversal.
        m,lvl=cross(x,d,12)
        old=(-d*x.z288>=1.75)
        q=m&old&(x.dvol_z>=1.25)&(x.dvol_chg>=.05)&(body>=.30)&(x.vr>=1.05)
        out+=collect(x,q,d,"IV_SHOCK_REVERSAL",lvl)
    seen=set(); z=[]
    for e in sorted(out,key=lambda r:(r["sig"],r["family"],r["d"])):
        k=(e["sig"],e["family"],e["d"])
        if k not in seen: seen.add(k); z.append(e)
    return z

def collect(x,m,d,f,lvl):
    out=[]
    for i in np.flatnonzero(m.fillna(False).to_numpy()):
        if np.isfinite(x.atr.iloc[i]) and np.isfinite(lvl.iloc[i]):
            out.append({"sig":int(i),"d":int(d),"family":f,"atr":float(x.atr.iloc[i])})
    return out

ACTIONS=[
    {"name":"R15","stop":1.0,"rr":1.5},
    {"name":"R20","stop":1.0,"rr":2.0},
    {"name":"W20","stop":1.5,"rr":2.0},
]

def sim(x,ev,a,start,end,stress=0.):
    T=x.ts.to_numpy(np.int64);O=x.open.to_numpy();H=x.high.to_numpy();L=x.low.to_numpy();C=x.close.to_numpy()
    st=ts(start);en=ts(end);i1=int(np.searchsorted(T,en));free=-1;out=[]
    for e in ev:
        sig=e["sig"]
        if T[sig]<st or T[sig]>=en-300:continue
        i=sig+1
        if i<free or i>=i1:continue
        d=e["d"];sl=execution_slip(x,i);entry=O[i]*(1+d*sl)
        stop=entry-d*a["stop"]*e["atr"];risk=d*(entry-stop)/entry
        if not (.0025<=risk<=.05):continue
        target=entry*(1+d*risk*a["rr"]);j=i;last=min(i1-1,i+MAX_HOLD);reason="TIME"
        cost=FEE+stress/2
        while j<=last:
            if (L[j]<=stop if d>0 else H[j]>=stop):
                q=(min(O[j],stop) if d>0 else max(O[j],stop))*(1-d*sl);reason="STOP";break
            if (H[j]>=target if d>0 else L[j]<=target):
                q=target*(1-d*sl);reason="TARGET";break
            j+=1
        if j>last:j=last;q=C[j]*(1-d*sl)
        net=d*(q/entry-1)-cost-(FEE+stress/2)*q/entry
        out.append({"opened":int(T[i]),"closed":int(T[j]),"d":d,"net":float(net),"family":e["family"],"action":a["name"],"reason":reason})
        free=j+COOLDOWN
    return out

def pm(x,ev,a,y,stress=0.):
    return met(sim(x,ev,a,f"{y}-01-01T00:00:00Z",f"{y+1}-01-01T00:00:00Z",stress))

def research_family(x,all_ev,fam):
    ev=[e for e in all_ev if e["family"]==fam]
    if len(ev)<15:return {"family":fam,"status":"TOO_FEW_EVENTS","n_events":len(ev)}
    cand=[]
    for a in ACTIONS:
        m22=pm(x,ev,a,2022);m23=pm(x,ev,a,2023);s22=pm(x,ev,a,2022,STRESS);s23=pm(x,ev,a,2023,STRESS)
        if min(m22["n"],m23["n"])<4:continue
        if min(m22["avg"] or -9,m23["avg"] or -9)<=0:continue
        if min(s22["avg"] or -9,s23["avg"] or -9)<=0:continue
        sc=100*min(m22["avg"],m23["avg"])+.25*min(m22["pf"],m23["pf"])+.25*min(m22["win_rate"],m23["win_rate"])
        cand.append((sc,a,{"2022":m22,"2023":m23},{"2022":s22,"2023":s23}))
    if not cand:return {"family":fam,"status":"NO_DISCOVERY_EDGE","n_events":len(ev)}
    cand.sort(reverse=True,key=lambda z:z[0]);_,a,disc,ds=cand[0]
    v={str(y):pm(x,ev,a,y) for y in (2024,2025)}
    vs={str(y):pm(x,ev,a,y,STRESS) for y in (2024,2025)}
    ok=all(v[str(y)]["n"]>=4 and (v[str(y)]["avg"] or -9)>0 and v[str(y)]["pf"]>=1.05 and
           (vs[str(y)]["avg"] or -9)>0 for y in (2024,2025))
    out={"family":fam,"status":"VALIDATED" if ok else "FAILED_2024_2025","action":a,
         "discovery":disc,"discovery_stress":ds,"validation":v,"validation_stress":vs,"n_events":len(ev)}
    if ok:
        out["test_2026"]=met(sim(x,ev,a,TEST_START,TEST_END))
        out["test_stress_5bp"]=met(sim(x,ev,a,TEST_START,TEST_END,STRESS))
    return out

def run_asset(asset):
    x=common_features(load(asset));x=attach_dvol(asset,x);ev=events(x)
    fams=["IV_EXPANSION_BREAK","IV_COMPRESSION_BREAK","IV_SHOCK_REVERSAL"]
    rows={f:research_family(x,ev,f) for f in fams}
    return {"asset":asset,"dvol_rows":int(np.isfinite(x.dvol).sum()),"families":rows}

def main():
    out={"generated_at":datetime.now(timezone.utc).isoformat(),
         "method":"Deribit DVOL independent options-volatility states; skew/smile excluded until reproducible history is available.",
         "assets":{a:run_asset(a) for a in ("BTC","ETH")}}
    (OUT/"result.json").write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False,default=jd))
    print("VERITAS_OPTIONS_VOL="+json.dumps(out,separators=(",",":"),default=jd),flush=True)

if __name__=="__main__":main()
