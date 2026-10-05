"""Regime-gated external validation for frozen BTC/ETH specialists.

Purpose: test the hypothesis that previously observed specialist edge is
state-dependent rather than permanent.

Protocol:
1) Specialist entry/exit rules remain frozen.
2) Market regime definitions are fixed ex ante and price-only, so they can be
   computed consistently across 2020-2026.
3) Allowed regimes are selected only from 2022-2025 specialist trades.
4) The frozen regime gate is then applied to 2020-2021 external control.
5) 2026 is reported as diagnostic only because it has already been inspected.

Research only.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd

from research_crypto_external_holdout import load_old
from research_crypto_manager_library import load, features, ts, met
from research_crypto_arsenal_expansion import common_features
import research_crypto_multihorizon_trend as mh
import research_eth_long_neighborhood as el
import research_eth_short_neighborhood as es

OUT=Path("regime_specialist_out");OUT.mkdir(exist_ok=True)
STRESS=.0005

def combine(asset):
    old=load_old(asset);new=load(asset)
    return pd.concat([old,new],ignore_index=True).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)

def price_regimes(raw):
    x=common_features(raw)
    c=x.close.to_numpy(float);e20=x.ema20.to_numpy(float);e50=x.ema50.to_numpy(float);e200=x.ema200.to_numpy(float)
    score=np.zeros(len(x),float)
    score+=np.where(c>e20,1,-1);score+=np.where(e20>e50,1,-1);score+=np.where(e50>e200,1,-1)
    score+=np.sign(np.nan_to_num(x.ret48.to_numpy(float)));score+=np.sign(np.nan_to_num(x.ret288.to_numpy(float)))
    eff=x.eff48.to_numpy(float);ar=x.atr_ratio.to_numpy(float);rv=x.rv_ratio.to_numpy(float);z=x.z288.to_numpy(float)
    r48=x.ret48.to_numpy(float);r288=x.ret288.to_numpy(float)
    st=np.full(len(x),"TRANSITION",dtype=object)
    m=(np.abs(score)<=1)&(eff<=.30)&(ar<=.90)&(rv<=.95);st[m]="RANGE_COMPRESSION"
    m=(score>=2)&(r48>0)&(ar>=.95)&(ar<=1.45)&(eff>=.20);st[m]="EARLY_BULL"
    m=(score<=-2)&(r48<0)&(ar>=.95)&(ar<=1.45)&(eff>=.20);st[m]="EARLY_BEAR"
    m=(score>=4)&(r288>0)&(eff>=.35);st[m]="TREND_BULL"
    m=(score<=-4)&(r288<0)&(eff>=.35);st[m]="TREND_BEAR"
    m=(z<=-2.0)&(ar>=1.35)&(rv>=1.25);st[m]="EXTREME_DOWN"
    m=(z>=2.0)&(ar>=1.35)&(rv>=1.25);st[m]="EXTREME_UP"
    x["price_regime"]=st
    return x[["ts","price_regime"]]

def attach_regime(trades,reg):
    if not trades:return []
    rt=reg.ts.to_numpy(np.int64);rs=reg.price_regime.to_numpy(object)
    out=[]
    for t in trades:
        # 5m row is left-labelled and only fully known at ts+300.
        avail=int(t["opened"])-300
        i=np.searchsorted(rt,avail,side="right")-1
        if i<0:continue
        z=dict(t);z["regime"]=str(rs[i]);out.append(z)
    return out

def specialist_streams(btc,eth,stress=0.):
    out={}
    # BTC SHORT exact frozen specialist.
    xb=mh.add_horizon_features(btc,features(btc))
    prof=next(p for p in mh.PROFILES if p[0]=="QUALITY20")
    ev=mh.make_events(xb,-1,"H1H4","BOTH",prof)
    out["BTC_SHORT_H1H4"]=mh.sim(xb,ev,stop_h=6,buf=.10,mode="PART_3R",maxhold=1440,
                                   start="2020-01-01",end="2026-10-04",stress=stress)

    # ETH LONG exact frozen specialist.
    xe=features(eth)
    lp=next(p for p in el.ENTRY if p[0]=="TIGHT_C")
    lex=next(e for e in el.EXITS if e[0]=="FIXED_2_5R")
    lev=el.events(xe,lp)
    out["ETH_LONG_TIGHT_C"]=el.simulate(xe,lev,start=ts("2020-01-01T00:00:00Z"),end=ts("2026-10-04T00:00:00Z"),
                                         stress=stress,**el.pars(lex))

    # ETH SHORT exact frozen specialist.
    b60,b240=es.btc_context(btc,xe.ts.to_numpy())
    sp=next(p for p in es.ENTRY_PROFILES if p[0]=="WIDER_C")
    sex=next(e for e in es.EXITS if e[0]=="FIXED_2R")
    sev=es.make_events(xe,sp,b60,b240,"BTC4H")
    out["ETH_SHORT_WIDER_C_BTC4H"]=es.simulate(xe,sev,start=ts("2020-01-01T00:00:00Z"),end=ts("2026-10-04T00:00:00Z"),
                                                stress=stress,**es.pars(sex))
    return out

def mvals(rows):
    return met(rows)

def select_regimes(rows,stress_rows):
    # Both streams have identical opens under normal/stress except edge risk filters
    # can change at high stress. Join by opened for conservative evidence.
    sm={r["opened"]:r for r in stress_rows}
    chosen=[];stats={}
    for regime in sorted(set(r["regime"] for r in rows)):
        r=[x for x in rows if x["regime"]==regime and ts("2022-01-01T00:00:00Z")<=x["opened"]<ts("2026-01-01T00:00:00Z")]
        s=[sm[x["opened"]] for x in r if x["opened"] in sm]
        base=mvals(r);st=mvals(s)
        yearly={}
        pos=0;represented=0
        for y in (2022,2023,2024,2025):
            a=ts(f"{y}-01-01T00:00:00Z");b=ts(f"{y+1}-01-01T00:00:00Z")
            q=[x for x in r if a<=x["opened"]<b]
            qm=mvals(q);yearly[str(y)]=qm
            if qm["n"]>=2:
                represented+=1
                if (qm["avg"] or -9)>0:pos+=1
        ok=(base["n"]>=12 and represented>=2 and pos>=2 and (base["avg"] or -9)>0 and
            base["pf"]>=1.10 and st["n"]>=8 and (st["avg"] or -9)>0 and st["pf"]>=1.03)
        stats[regime]={"base_2022_25":base,"stress_2022_25":st,"years":yearly,"allowed":bool(ok)}
        if ok:chosen.append(regime)
    return chosen,stats

def eval_period(rows,allowed,start,end):
    a=ts(start);b=ts(end)
    return met([r for r in rows if r["regime"] in allowed and a<=r["opened"]<b])

def main():
    btc=combine("BTC");eth=combine("ETH")
    reg={"BTC":price_regimes(btc),"ETH":price_regimes(eth)}
    base=specialist_streams(btc,eth,0.0);stress=specialist_streams(btc,eth,STRESS)
    out={"generated_at":datetime.now(timezone.utc).isoformat(),
         "method":"Frozen specialist + fixed price regimes; regime selection 2022-25 only; external 2020-21 after freeze.",
         "modules":{}}
    for name,rows in base.items():
        asset="BTC" if name.startswith("BTC") else "ETH"
        rb=attach_regime(rows,reg[asset]);rs=attach_regime(stress[name],reg[asset])
        allowed,stats=select_regimes(rb,rs)
        out["modules"][name]={
            "allowed_regimes":allowed,"selection":stats,
            "external_2020":eval_period(rb,allowed,"2020-01-01T00:00:00Z","2021-01-01T00:00:00Z"),
            "external_2021":eval_period(rb,allowed,"2021-01-01T00:00:00Z","2022-01-01T00:00:00Z"),
            "external_stress_2020":eval_period(rs,allowed,"2020-01-01T00:00:00Z","2021-01-01T00:00:00Z"),
            "external_stress_2021":eval_period(rs,allowed,"2021-01-01T00:00:00Z","2022-01-01T00:00:00Z"),
            "diagnostic_2026":eval_period(rb,allowed,"2026-04-04T00:00:00Z","2026-10-04T00:00:00Z"),
        }
        z=out["modules"][name]
        passed=(len(allowed)>0 and z["external_2020"]["n"]>=3 and z["external_2021"]["n"]>=3 and
                (z["external_2020"]["avg"] or -9)>0 and (z["external_2021"]["avg"] or -9)>0 and
                (z["external_stress_2020"]["avg"] or -9)>0 and (z["external_stress_2021"]["avg"] or -9)>0)
        z["external_verdict"]="PASS" if passed else "FAIL"
        print(name,"REGIME_SPECIALIST",json.dumps({k:z[k] for k in ("allowed_regimes","external_2020","external_2021","external_stress_2020","external_stress_2021","diagnostic_2026","external_verdict")},separators=(",",":")),flush=True)
    (OUT/"result.json").write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False))
    print("VERITAS_REGIME_SPECIALIST="+json.dumps({k:{"allowed_regimes":v["allowed_regimes"],"external_2020":v["external_2020"],"external_2021":v["external_2021"],"diagnostic_2026":v["diagnostic_2026"],"verdict":v["external_verdict"]} for k,v in out["modules"].items()},separators=(",",":")),flush=True)

if __name__=="__main__":main()
