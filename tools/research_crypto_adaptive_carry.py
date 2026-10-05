"""VERITAS adaptive market-neutral spot/perpetual carry research.

Research only. No directional BTC/ETH prediction.

Position:
    long spot + short USD-M perpetual, equal notionals.

Unlike the earlier fixed carry rule, entry is based on a causal break-even test:
expected cumulative funding over a fixed horizon must exceed all four execution
costs by a safety margin. Funding persistence and basis are observed, not
forecast with future data.

Three theory-led policies are evaluated unchanged across 2020-2026. There is no
calendar-year parameter search.

PnL uses actual spot/perpetual prices, realized funding, fees/slippage and +5bp
stress. BTC and ETH are independent.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import research_crypto_carry as base
from research_crypto_manager_library import FEE, SLIP, ts

OUT=Path("adaptive_carry_out");OUT.mkdir(exist_ok=True)
LEG=FEE+SLIP
PAIR_RT=4*LEG
STRESS=.0005

POLICIES=[
    {
      "name":"BREAKEVEN_14D",
      "target_days":14,
      "cost_multiple":1.25,
      "min_positive_frac":.67,
      "min_basis":-.0010,
      "max_days":28,
    },
    {
      "name":"STRONG_21D",
      "target_days":21,
      "cost_multiple":1.50,
      "min_positive_frac":.78,
      "min_basis":-.0005,
      "max_days":42,
    },
    {
      "name":"HIGH_CONVICTION_28D",
      "target_days":28,
      "cost_multiple":1.75,
      "min_positive_frac":.82,
      "min_basis":0.0,
      "max_days":56,
    },
]

def jd(o):
    if isinstance(o,np.integer):return int(o)
    if isinstance(o,np.floating):return float(o)
    if isinstance(o,np.bool_):return bool(o)
    raise TypeError(type(o).__name__)

def prep(asset):
    f=base.funding(asset)
    z,s,p=base.attach_prices(asset,f)
    z=z.copy()
    z["pos9"]=z.rate.gt(0).rolling(9,min_periods=9).mean().shift(1)
    z["pos21"]=z.rate.gt(0).rolling(21,min_periods=21).mean().shift(1)
    # Forecast uses only previously observed rates plus the just-settled current
    # rate that is known before the post-settlement entry.
    z["forecast_rate"]=(.45*z.mean3+.35*z.mean9+.20*z.rate).clip(lower=-.01,upper=.01)
    z["fund_vol"]=z.rate.rolling(21,min_periods=9).std().shift(1)
    z["forecast_lcb"]=z.forecast_rate-.50*z.fund_vol.fillna(0)
    z["positive_frac"]=(.55*z.pos9+.45*z.pos21)
    # Causal funding frequency. Use only intervals completed before the current
    # settlement. This keeps "14/21/28D" in calendar time if the venue changes
    # funding from 8h to another interval.
    z["interval_sec"]=z.ts.diff().rolling(21,min_periods=9).median().shift(1)
    return z,s,p

def entry_ok(r,pol):
    vals=(r.forecast_lcb,r.positive_frac,r.basis,r.mean3,r.mean9,r.rate,r.interval_sec)
    if not all(np.isfinite(v) for v in vals):return False
    # Four executions. Stress is round-trip pair stress and is added once.
    cost=PAIR_RT
    forecast_events=int(max(1,round(pol["target_days"]*86400/float(r.interval_sec))))
    expected=max(0.,float(r.forecast_lcb))*forecast_events
    return (
        expected >= pol["cost_multiple"]*cost
        and r.positive_frac >= pol["min_positive_frac"]
        and r.basis >= pol["min_basis"]
        and r.mean3>0 and r.mean9>0 and r.rate>0
    )

def exit_now(r,held_events):
    # Hysteresis reduces churn: one weak print does not force an exit after we
    # already paid entry costs. Exit on persistent loss of carry economics.
    if held_events<3:return False
    bad_short=(r.mean3<=0 and r.rate<=0)
    bad_medium=(r.mean9<=0)
    basis_flip=(r.basis<-.004)
    return bool(bad_short or bad_medium or basis_flip)

def nearest_open(df,t):
    i=np.searchsorted(df.ts.to_numpy(),t,side="left")
    if i>=len(df):return None
    return float(df.open.iloc[i]),int(df.ts.iloc[i])

def simulate(z,s,p,pol,pnl_stress=0.):
    trades=[];i=0
    while i<len(z):
        r=z.iloc[i]
        if not entry_ok(r,pol):
            i+=1;continue
        entry_i=i;entry_ts=int(r.entry_ts)
        spot0=float(r.spot_open);perp0=float(r.perp_open)
        funding_sum=0.;reason="MAX";j=i+1
        if not np.isfinite(r.interval_sec) or r.interval_sec<=0:
            i+=1;continue
        max_events=int(max(1,round(pol["max_days"]*86400/float(r.interval_sec))))
        last=min(len(z)-1,i+max_events)
        while j<=last:
            rr=z.iloc[j]
            mk=base.close_before(p,int(rr.ts))
            if mk is not None:
                mark,_=mk
                funding_sum+=float(rr.rate)*(mark/perp0)
            held=j-entry_i
            if exit_now(rr,held):
                reason="CARRY_END";break
            j+=1
        if j>last:j=last
        exit_event_ts=int(z.iloc[j].ts)+3600
        so=nearest_open(s,exit_event_ts);po=nearest_open(p,exit_event_ts)
        if so is None or po is None:break
        spot1,ets=so;perp1,etp=po
        spot_ratio=spot1/spot0
        perp_ratio=perp1/perp0
        spot_pnl=spot_ratio-1.
        perp_pnl=1.-perp_ratio
        execution_cost=LEG*(1.+1.+spot_ratio+perp_ratio)
        costs=execution_cost+pnl_stress
        pair_pnl=spot_pnl+perp_pnl+funding_sum-costs
        capital_return=pair_pnl/2.
        # Hourly mark-to-market path of the hedged pair, including realized
        # funding as it accrues. This measures basis/liquidation stress while open.
        t0=entry_ts; t1=max(ets,etp)
        ss=s[(s.ts>=t0)&(s.ts<=t1)]
        pp=p[(p.ts>=t0)&(p.ts<=t1)]
        mm=ss[["ts","close"]].merge(pp[["ts","close"]],on="ts",suffixes=("_s","_p"))
        mae=0.0
        if len(mm):
            ff=base.funding(asset) if False else None
            # Price-only pair MAE is conservative enough for basis drift; funding
            # is positive carry and is not credited to the intratrade risk figure.
            path=(mm.close_s/spot0-1.)+(1.-mm.close_p/perp0)
            mae=float(min(0.0,path.min()/2.0))
        trades.append({
            "opened":entry_ts,"closed":max(ets,etp),"net":float(capital_return),
            "raw_pair_pnl":float(pair_pnl),"funding":float(funding_sum),
            "spot":float(spot_pnl),"perp":float(perp_pnl),
            "execution_cost":float(execution_cost),
            "spot_exit_ratio":float(spot_ratio),"perp_exit_ratio":float(perp_ratio),
            "entry_basis":float(r.basis),"events":int(j-entry_i),"reason":reason,
            "forecast_lcb":float(r.forecast_lcb),"positive_frac":float(r.positive_frac),
            "funding_interval_sec":float(r.interval_sec),
            "target_days":int(pol["target_days"]),"max_days":int(pol["max_days"]),
            "pair_price_mae":mae,
        })
        i=j+1
    return trades

def met(tr):
    if not tr:return {"n":0,"win_rate":None,"avg":None,"sum":0.,"pf":0.,"dd":0.,"worst_pair_mae":None}
    a=np.asarray([x["net"] for x in tr],float)
    pos=a[a>0].sum();neg=-a[a<0].sum()
    eq=np.cumsum(a);pk=np.maximum.accumulate(np.r_[0.,eq])[1:]
    maes=[float(x.get("pair_price_mae",0.0)) for x in tr]
    return {"n":int(len(a)),"win_rate":float((a>0).mean()),"avg":float(a.mean()),
            "sum":float(a.sum()),"pf":float(pos/neg) if neg else 99.,
            "dd":float((pk-eq).max()),"worst_pair_mae":float(min(maes)) if maes else None}

def yearly(tr):
    out={}
    for y in range(2020,2027):
        a=ts(f"{y}-01-01T00:00:00Z")
        b=ts(f"{y+1}-01-01T00:00:00Z") if y<2026 else ts("2026-10-04T00:00:00Z")
        out[str(y)]=met([x for x in tr if a<=x["opened"]<b])
    return out

def bootstrap(tr,nboot=2000,block=3):
    if len(tr)<12:return {"n":len(tr),"p_positive":None,"p05_sum":None,"p95_dd":None}
    a=np.asarray([x["net"] for x in tr],float);n=len(a)
    rng=np.random.default_rng(20261005)
    starts=np.arange(max(1,n-block+1));sums=[];dds=[]
    for _ in range(nboot):
        z=[]
        while len(z)<n:
            k=int(rng.choice(starts));z.extend(a[k:k+block].tolist())
        z=np.asarray(z[:n],float)
        eq=np.cumsum(z);pk=np.maximum.accumulate(np.r_[0.,eq])[1:]
        sums.append(float(z.sum()));dds.append(float((pk-eq).max()))
    return {"n":n,"p_positive":float(np.mean(np.asarray(sums)>0)),
            "p05_sum":float(np.quantile(sums,.05)),
            "p95_dd":float(np.quantile(dds,.95))}

def run(asset):
    z,s,p=prep(asset)
    out={"asset":asset,"generated_at":datetime.now(timezone.utc).isoformat(),
         "method":"Causal funding break-even market-neutral carry; fixed policies across all years.",
         "policies":{}}
    for pol in POLICIES:
        b=simulate(z,s,p,pol,0.);s5=simulate(z,s,p,pol,.0005);s10=simulate(z,s,p,pol,.0010);s20=simulate(z,s,p,pol,.0020)
        out["policies"][pol["name"]]={
            "policy":pol,"base":met(b),"stress5":met(s5),"stress10":met(s10),"stress20":met(s20),
            "years":yearly(b),"stress_years":yearly(s5),
            "bootstrap":bootstrap(b),"trades":b,
        }
        print(asset,pol["name"],"ADAPTIVE_CARRY",json.dumps({
            "base":out["policies"][pol["name"]]["base"],
            "stress5":out["policies"][pol["name"]]["stress5"],
            "stress10":out["policies"][pol["name"]]["stress10"],
            "stress20":out["policies"][pol["name"]]["stress20"],
            "years":out["policies"][pol["name"]]["years"],
            "stress_years":out["policies"][pol["name"]]["stress_years"],
            "bootstrap":out["policies"][pol["name"]]["bootstrap"],
        },separators=(",",":"),default=jd),flush=True)
    q=OUT/asset.lower();q.mkdir(parents=True,exist_ok=True)
    (q/"result.json").write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False,default=jd))
    print("VERITAS_ADAPTIVE_CARRY="+json.dumps({"asset":asset,"policies":{k:{q:v[q] for q in ("base","stress5","stress10","stress20","years","stress_years","bootstrap")} for k,v in out["policies"].items()}},separators=(",",":"),default=jd),flush=True)

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--asset",choices=["BTC","ETH"],required=True);args=ap.parse_args()
    run(args.asset)

if __name__=="__main__":main()
