"""VERITAS cycle / regime transition research for BTC and ETH.

Research only. Markets are treated as recurrent state machines, not as exact
repetitions of chart shapes. The classifier is causal and uses only information
available at the timestamp:

- price structure / trend / efficiency / volatility
- funding, perpetual basis and futures flow
- open interest / crowding
- macro liquidity / risk appetite
- optional on-chain confirmation

The output is a library of recurring state transitions and their forward return
distributions at 1h / 4h / 1d. BTC and ETH are estimated independently.

No production trading changes.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from research_crypto_manager_library import load
from research_crypto_arsenal_expansion import (
    common_features, attach_funding_basis, attach_macro, attach_onchain, pf
)
import research_crypto_positioning_router as pos

OUT=Path("cycle_regime_out"); OUT.mkdir(exist_ok=True)

def jd(o):
    if isinstance(o,np.integer): return int(o)
    if isinstance(o,np.floating): return float(o)
    if isinstance(o,np.bool_): return bool(o)
    raise TypeError(type(o).__name__)

def attach_positioning(asset,x):
    out=x.copy()
    try:
        mm=pos.download_metrics(asset)
        f=pos.add_features(mm)
        ft=f.avail_ts.to_numpy(np.int64); t=out.ts.to_numpy(np.int64)
        ix=np.searchsorted(ft,t,side="right")-1; good=ix>=0
        mapping={
            "oi12_z":"oi12","oi_accel_z":"oi_accel",
            "top_z":"top_z","retail_z":"retail_z",
            "smart_retail_z":"smart_retail",
            "taker_z":"taker_z"
        }
        for src,dst in mapping.items():
            arr=np.full(len(out),np.nan)
            if good.any() and src in f:
                arr[good]=f[src].to_numpy()[ix[good]]
            out[dst]=arr
    except Exception as e:
        print(asset,"positioning_attach_failed",str(e)[:180],flush=True)
        for c in ("oi12","oi_accel","top_z","retail_z","smart_retail","taker_z"):
            out[c]=np.nan
    return out

def macro_score(x):
    score=np.zeros(len(x),dtype=float)
    fields=[
        ("vix20",-1),("usd20",-1),("real20",-1),("nfci20",-1)
    ]
    for c,sign in fields:
        if c not in x: continue
        v=x[c].to_numpy(float)
        score += np.where(np.isfinite(v), np.sign(v)*sign, 0.0)
    return score

def trend_score(x):
    c=x.close.to_numpy(float)
    e20=x.ema20.to_numpy(float);e50=x.ema50.to_numpy(float);e200=x.ema200.to_numpy(float)
    r48=x.ret48.to_numpy(float);r288=x.ret288.to_numpy(float)
    s=np.zeros(len(x),dtype=float)
    s += np.where(c>e20,1,-1)
    s += np.where(e20>e50,1,-1)
    s += np.where(e50>e200,1,-1)
    s += np.sign(np.nan_to_num(r48))
    s += np.sign(np.nan_to_num(r288))
    return s

def classify(asset,x):
    x=x.copy()
    x["macro_score"]=macro_score(x)
    x["trend_score"]=trend_score(x)

    # Directional crowding uses current trend direction. Missing derivatives
    # never create a crowding state; they fail closed to neutral.
    td=np.sign(x.trend_score.to_numpy(float))
    funding=np.nan_to_num(x.funding_z.to_numpy(float),nan=0.0)
    basis=np.nan_to_num(x.basis_z.to_numpy(float),nan=0.0)
    oi=np.nan_to_num(x.oi12.to_numpy(float),nan=0.0)
    crowd=td*(0.40*funding+0.35*basis+0.25*oi)
    x["crowding"]=crowd

    st=np.full(len(x),"TRANSITION",dtype=object)
    dirn=np.zeros(len(x),dtype=int)

    eff=x.eff48.to_numpy(float)
    ar=x.atr_ratio.to_numpy(float)
    rv=x.rv_ratio.to_numpy(float)
    z=x.z288.to_numpy(float)
    ms=x.macro_score.to_numpy(float)
    ts=x.trend_score.to_numpy(float)
    oi12=x.oi12.to_numpy(float)
    oiacc=x.oi_accel.to_numpy(float)
    imb=np.nan_to_num(x.imb3.to_numpy(float),nan=0.0)
    r48=x.ret48.to_numpy(float);r288=x.ret288.to_numpy(float)

    # 1. Compression / accumulation: low directionality, low volatility.
    m=(np.abs(ts)<=1)&(eff<=.30)&(ar<=.90)&(rv<=.95)
    st[m]="RANGE_COMPRESSION"

    # 2. Early trend: directional turn + expanding vol but not yet mature.
    bull=(ts>=2)&(r48>0)&(ar>=.95)&(ar<=1.45)&(eff>=.20)
    bear=(ts<=-2)&(r48<0)&(ar>=.95)&(ar<=1.45)&(eff>=.20)
    st[bull]="EARLY_BULL";dirn[bull]=1
    st[bear]="EARLY_BEAR";dirn[bear]=-1

    # 3. Mature persistent trends.
    bull=(ts>=4)&(r288>0)&(eff>=.35)
    bear=(ts<=-4)&(r288<0)&(eff>=.35)
    st[bull]="TREND_BULL";dirn[bull]=1
    st[bear]="TREND_BEAR";dirn[bear]=-1

    # 4. Crowded/euphoric trends: do not chase fresh positions automatically.
    bull=(st=="TREND_BULL")&(crowd>=.80)&(ar>=1.05)
    bear=(st=="TREND_BEAR")&(crowd>=.80)&(ar>=1.05)
    st[bull]="CROWDED_BULL";dirn[bull]=1
    st[bear]="CROWDED_BEAR";dirn[bear]=-1

    # 5. Deleveraging continuation: price down + OI contraction + negative flow.
    delever=(r48<0)&(ar>=1.25)&(rv>=1.15)&(oi12<=-.35)&(oiacc<=0)&(imb<=-.20)
    st[delever]="DELEVERAGING_BEAR";dirn[delever]=-1

    # 6. Short squeeze: sharp upside + OI contraction + positive flow.
    squeeze=(r48>0)&(ar>=1.25)&(rv>=1.15)&(oi12<=-.35)&(imb>=.20)
    st[squeeze]="SHORT_SQUEEZE";dirn[squeeze]=1

    # 7. Capitulation / exhaustion candidates: extreme displacement + vol.
    cap=(z<=-2.0)&(ar>=1.35)&(rv>=1.25)&(oi12<=-.50)
    st[cap]="CAPITULATION"
    eup=(z>=2.0)&(ar>=1.35)&(rv>=1.25)&(crowd>=.60)
    st[eup]="EUPHORIA"

    # 8. Recovery after a bearish state: improving price + supportive macro.
    prev=pd.Series(st).shift(12).fillna("TRANSITION").to_numpy()
    recovery=np.isin(prev,["CAPITULATION","DELEVERAGING_BEAR","TREND_BEAR"])&(r48>0)&(ts>=1)&(ms>=0)&(imb>=0)
    st[recovery]="RECOVERY_BULL";dirn[recovery]=1

    # 9. Distribution after crowded bull: weakening trend / negative flow.
    prev=pd.Series(st).shift(12).fillna("TRANSITION").to_numpy()
    dist=np.isin(prev,["CROWDED_BULL","EUPHORIA"])&(r48<=0)&(eff<=.35)&(imb<=0)
    st[dist]="DISTRIBUTION";dirn[dist]=-1

    x["state"]=st
    x["state_dir"]=dirn
    return x

def eventize(x):
    """Keep only genuine state transitions; repeated 5m snapshots are not events."""
    s=x.state.astype(str)
    changed=s.ne(s.shift(1))
    good=changed&s.ne("TRANSITION")
    idx=np.flatnonzero(good.fillna(False).to_numpy())
    rows=[]
    C=x.close.to_numpy(float)
    T=x.ts.to_numpy(np.int64)
    for i in idx:
        if i+288>=len(x): continue
        rows.append({
            "i":int(i),"ts":int(T[i]),"year":int(pd.to_datetime(T[i],unit="s",utc=True).year),
            "state":str(x.state.iloc[i]),"prev_state":str(x.state.iloc[i-1]) if i else "START",
            "dir":int(x.state_dir.iloc[i]),
            "macro_score":float(x.macro_score.iloc[i]),
            "crowding":float(x.crowding.iloc[i]),
            "ret_1h":float(C[i+12]/C[i]-1),
            "ret_4h":float(C[i+48]/C[i]-1),
            "ret_1d":float(C[i+288]/C[i]-1),
        })
    return pd.DataFrame(rows)

def summarize_transition(g,horizon):
    r=g[horizon].to_numpy(float)
    d=g["dir"].to_numpy(int)
    # For directional states evaluate following the state; for nondirectional
    # states report raw distribution only.
    directional=np.where(d!=0,d*r,np.nan)
    a=directional[np.isfinite(directional)]
    raw=r[np.isfinite(r)]
    out={
        "n":int(len(g)),
        "raw_avg":float(raw.mean()) if len(raw) else None,
        "raw_up_rate":float((raw>0).mean()) if len(raw) else None,
    }
    if len(a):
        out.update({
            "follow_avg":float(a.mean()),
            "follow_win_rate":float((a>0).mean()),
            "follow_pf":pf(a)
        })
    return out

def libraries(events):
    # Fixed transition identities. No parameter fitting here; we inspect recurrence
    # and demand the same sign across validation years.
    out={}
    keys=["state","prev_state"]
    for key,g in events.groupby(keys,dropna=False,sort=False):
        if len(g)<5: continue
        name=f"{key[1]}->{key[0]}"
        row={"n":int(len(g)),"years":{}}
        for y in sorted(g.year.unique()):
            gy=g[g.year==y]
            row["years"][str(int(y))]={
                "1h":summarize_transition(gy,"ret_1h"),
                "4h":summarize_transition(gy,"ret_4h"),
                "1d":summarize_transition(gy,"ret_1d")
            }
        row["all"]={
            "1h":summarize_transition(g,"ret_1h"),
            "4h":summarize_transition(g,"ret_4h"),
            "1d":summarize_transition(g,"ret_1d")
        }
        # A directional recurring transition is "robust" only if 2023-2025
        # each contain observations and the follow direction is positive in
        # at least two of the three years on 4h and 1d.
        robust=False
        if int(g["dir"].abs().max())>0:
            pos4=0;pos1=0;represented=0
            for y in (2023,2024,2025):
                gy=g[g.year==y]
                if len(gy)>=3:
                    represented+=1
                    s4=summarize_transition(gy,"ret_4h")
                    s1=summarize_transition(gy,"ret_1d")
                    if (s4.get("follow_avg") or -9)>0:pos4+=1
                    if (s1.get("follow_avg") or -9)>0:pos1+=1
            robust=represented>=2 and pos4>=2 and pos1>=2
        row["robust_pre2026"]=bool(robust)
        out[name]=row
    return out

STATE_POLICY={
    "RANGE_COMPRESSION":{
        "allow":["MEAN_REVERSION_AT_EDGES","COMPRESSION_BREAK"],
        "block":["TREND_CHASE"],"size":"small until expansion confirmation"
    },
    "EARLY_BULL":{"allow":["BREAKOUT_LONG","EARLY_SCALE_IN"],"block":["COUNTERTREND_SHORT"],"size":"start 25-50%, add on confirmation"},
    "EARLY_BEAR":{"allow":["BREAKOUT_SHORT","EARLY_SCALE_IN"],"block":["COUNTERTREND_LONG"],"size":"start 25-50%, add on confirmation"},
    "TREND_BULL":{"allow":["PULLBACK_LONG","TREND_ACCEL_LONG"],"block":["MEAN_REVERSION_SHORT"],"size":"50-100% by evidence"},
    "TREND_BEAR":{"allow":["PULLBACK_SHORT","TREND_ACCEL_SHORT"],"block":["MEAN_REVERSION_LONG"],"size":"50-100% by evidence"},
    "CROWDED_BULL":{"allow":["HOLD_TRAIL","STRUCTURE_BREAK_REVERSAL_ONLY"],"block":["FRESH_CHASE_LONG"],"size":"do not add without reset"},
    "CROWDED_BEAR":{"allow":["HOLD_TRAIL","STRUCTURE_BREAK_REVERSAL_ONLY"],"block":["FRESH_CHASE_SHORT"],"size":"do not add without reset"},
    "DELEVERAGING_BEAR":{"allow":["SHORT_CONTINUATION","PROFIT_TRAIL"],"block":["BOTTOM_FISHING"],"size":"reduce after volatility climax"},
    "SHORT_SQUEEZE":{"allow":["TACTICAL_LONG","TRAIL"],"block":["BLIND_SHORT"],"size":"tactical unless macro confirms"},
    "CAPITULATION":{"allow":["WAIT_RECLAIM","REVERSAL_AFTER_CONFIRMATION"],"block":["KNIFE_CATCH"],"size":"0 until reversal trigger"},
    "EUPHORIA":{"allow":["TRAIL_LONG","DISTRIBUTION_WATCH"],"block":["LATE_FULL_LONG"],"size":"protect profit"},
    "RECOVERY_BULL":{"allow":["EARLY_LONG","PULLBACK_LONG"],"block":["STRUCTURAL_SHORT"],"size":"25-50%, add on higher low"},
    "DISTRIBUTION":{"allow":["REDUCE_LONG","SHORT_AFTER_BREAK"],"block":["BUY_DIP_WITHOUT_RECLAIM"],"size":"defensive"},
}

def run_asset(asset):
    raw=load(asset)
    x=common_features(raw)
    x=attach_funding_basis(asset,x,raw)
    x=attach_macro(x)
    x=attach_onchain(asset,x)
    x=attach_positioning(asset,x)
    x=classify(asset,x)
    ev=eventize(x)
    lib=libraries(ev)
    current={
        "ts":int(x.ts.iloc[-1]),"state":str(x.state.iloc[-1]),
        "macro_score":float(x.macro_score.iloc[-1]),
        "trend_score":float(x.trend_score.iloc[-1]),
        "crowding":float(x.crowding.iloc[-1]),
        "policy":STATE_POLICY.get(str(x.state.iloc[-1]),{})
    }
    counts=x.state.value_counts().to_dict()
    return {"asset":asset,"current":current,"state_counts":counts,
            "n_transitions":int(len(ev)),"transition_library":lib}

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--asset",choices=["BTC","ETH","ALL"],default="ALL");args=ap.parse_args()
    assets=("BTC","ETH") if args.asset=="ALL" else (args.asset,)
    out={"generated_at":datetime.now(timezone.utc).isoformat(),
         "method":"Causal recurrent market-state machine; repeated transition statistics, macro/derivatives/on-chain conditioned.",
         "policy":STATE_POLICY,"assets":{}}
    for a in assets:
        out["assets"][a]=run_asset(a)
        print(a,"CYCLE_CURRENT",json.dumps(out["assets"][a]["current"],separators=(",",":"),default=jd),flush=True)
        robust={k:v for k,v in out["assets"][a]["transition_library"].items() if v.get("robust_pre2026")}
        print(a,"CYCLE_ROBUST",json.dumps(robust,separators=(",",":"),default=jd),flush=True)
    (OUT/"result.json").write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False,default=jd))
    print("VERITAS_CYCLE_REGIMES="+json.dumps({a:{"current":v["current"],"robust_count":sum(r.get("robust_pre2026",False) for r in v["transition_library"].values())} for a,v in out["assets"].items()},separators=(",",":"),default=jd),flush=True)

if __name__=="__main__":
    main()
