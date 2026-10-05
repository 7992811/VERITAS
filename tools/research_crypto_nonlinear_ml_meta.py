"""VERITAS nonlinear ML meta-filter for BTC and ETH.

Research only. This is deliberately a meta-filter over economically defined
candidate events, not a black-box price predictor.

Model:
- HistGradientBoostingClassifier with shallow trees and L2 regularization.
- Separate BTC/ETH and LONG/SHORT models.
- Candidate action library is the same fixed structural-stop / R-multiple set
  used by the interpretable meta-label research.
- Threshold/action choice uses 2024 and 2025 anchored walk-forward only.
- Apr-Oct 2026 is opened once after the choice is frozen.

Leakage controls:
- Features are causal at signal close.
- Entry is next bar.
- Training examples for a validation year come only from prior years.
- Deterministic caps control compute but never sample from the future.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OrdinalEncoder

from research_crypto_manager_library import load
from research_crypto_probability_meta import make_dataset, NUM, CAT, summarize, pf, STRESS, json_default

OUT=Path("ml_meta_out"); OUT.mkdir(exist_ok=True)

THRESHOLDS=(.50,.55,.60,.65,.70,.75)
MAX_TRAIN=220_000
MAX_VAL=160_000

def cap(df,n):
    if len(df)<=n:return df
    # deterministic, stratified by year and outcome where possible
    parts=[]
    keys=["year","win"] if "win" in df.columns else ["year"]
    groups=list(df.groupby(keys,dropna=False))
    per=max(1,n//max(1,len(groups)))
    for _,g in groups:
        parts.append(g.sample(min(per,len(g)),random_state=20261005))
    z=pd.concat(parts,ignore_index=True)
    if len(z)>n:z=z.sample(n,random_state=20261005)
    return z

def model():
    pre=ColumnTransformer([
        ("num","passthrough",NUM),
        ("cat",OrdinalEncoder(handle_unknown="use_encoded_value",unknown_value=-1),CAT),
    ])
    clf=HistGradientBoostingClassifier(
        learning_rate=.05,max_iter=120,max_leaf_nodes=15,max_depth=4,
        min_samples_leaf=80,l2_regularization=2.0,early_stopping=True,
        validation_fraction=.12,random_state=20261005
    )
    return Pipeline([("pre",pre),("clf",clf)])

def fit_predict(train,val):
    tr=cap(train,MAX_TRAIN); va=cap(val,MAX_VAL)
    if tr.win.nunique()<2 or len(va)==0:return None,None,va
    m=model();m.fit(tr[NUM+CAT],tr.win.astype(int))
    p=m.predict_proba(va[NUM+CAT])[:,1]
    return m,p,va

def eval_sel(df,prob,thr):
    q=df.copy();q["prob"]=prob
    q=q[q.prob>=thr]
    if q.empty:return summarize([])
    return summarize(q.net.to_numpy(float))

def wf_action(df,action,direction):
    d=df[(df.action==action)&(df.d==direction)].copy()
    years={}
    preds={}
    for y in (2024,2025):
        tr=d[(d.year>=2022)&(d.year<y)]
        va=d[d.year==y]
        if len(tr)<500 or len(va)<100:continue
        m,p,v=fit_predict(tr,va)
        if m is None:continue
        preds[y]=(v,p)
    if len(preds)<2:return []
    out=[]
    for thr in THRESHOLDS:
        ym={};vals=[];stress=[]
        ok=True
        for y in (2024,2025):
            v,p=preds[y];q=v.copy();q["prob"]=p;q=q[q.prob>=thr]
            s=summarize(q.net.to_numpy(float))
            ss=summarize((q.net-STRESS).to_numpy(float))
            ym[str(y)]={"base":s,"stress":ss}
            if s["n"]<18 or (s["avg"] or -9)<=0 or s["pf"]<1.08 or (ss["avg"] or -9)<=0:
                ok=False
            vals.extend(q.net.to_list());stress.extend((q.net-STRESS).to_list())
        agg=summarize(vals); ags=summarize(stress)
        if ok and agg["n"]>=45 and agg["pf"]>=1.15 and ags["pf"]>=1.05:
            score=1.1*(agg["win_rate"] or 0)+.25*min(agg["pf"],3)+75*(agg["avg"] or 0)-.05*agg["dd"]
            out.append({"action":action,"threshold":thr,"score":float(score),
                        "walkforward":agg,"walkforward_stress":ags,"years":ym})
    return out

def final_test(df,choice,direction):
    d=df[(df.action==choice["action"])&(df.d==direction)].copy()
    tr=d[(d.year>=2022)&(d.year<=2025)]
    te=d[d.year==2026]
    m,p,v=fit_predict(tr,te)
    if m is None:return {"base":summarize([]),"stress":summarize([]),"trades":[]}
    v=v.copy();v["prob"]=p
    q=v[v.prob>=choice["threshold"]].sort_values("open_ts")
    # One position per asset/direction stream.
    kept=[];free=-1
    for r in q.itertuples(index=False):
        if int(r.open_ts)<free:continue
        kept.append({"open_ts":int(r.open_ts),"close_ts":int(r.close_ts),"net":float(r.net),
                     "prob":float(r.prob),"trigger":str(r.trigger),"d":int(r.d),
                     "action":str(r.action)})
        free=int(r.close_ts)+15*60
    vals=np.array([t["net"] for t in kept],float)
    return {"base":summarize(vals),"stress":summarize(vals-STRESS),"trades":kept}

def run_asset(asset,raw,other):
    df=make_dataset(asset,raw,other)
    # Keep fixed quality floor from interpretable research, but no future-return filter.
    results={}
    for d,name in ((1,"LONG"),(-1,"SHORT")):
        candidates=[]
        for action in sorted(df.action.unique()):
            candidates.extend(wf_action(df,action,d))
        candidates.sort(key=lambda z:z["score"],reverse=True)
        if not candidates:
            results[name]={"selected":None,"status":"NO_WALKFORWARD_PASS"}
            continue
        best=candidates[0]
        test=final_test(df,best,d)
        results[name]={"selected":best,"test_2026":test,
                       "status":"RESEARCH_PASS" if test["base"]["n"]>=10 and (test["base"]["avg"] or -9)>0
                                and test["base"]["pf"]>=1.15 and (test["stress"]["avg"] or -9)>0 else "FAILED_2026"}
        print(asset,name,"ML_SELECTED",json.dumps({"selected":best,"test_2026":test["base"],
              "test_stress":test["stress"],"status":results[name]["status"]},
              separators=(",",":"),default=json_default),flush=True)
    return {"n_labels":int(len(df)),"directions":results}

def main():
    btc=load("BTC");eth=load("ETH")
    out={"generated_at":datetime.now(timezone.utc).isoformat(),
         "method":"Shallow regularized HistGradientBoosting meta-filter; 2024-25 anchored WF selection; 2026 frozen research test.",
         "assets":{}}
    out["assets"]["BTC"]=run_asset("BTC",btc,eth)
    out["assets"]["ETH"]=run_asset("ETH",eth,btc)
    (OUT/"result.json").write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False,default=json_default))
    compact={a:v["directions"] for a,v in out["assets"].items()}
    print("VERITAS_ML_META="+json.dumps(compact,separators=(",",":"),default=json_default),flush=True)

if __name__=="__main__":main()
