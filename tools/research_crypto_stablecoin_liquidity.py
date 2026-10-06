import argparse, json, math
from pathlib import Path
import numpy as np
import pandas as pd

def minute_to_hour(path):
    x = pd.read_pickle(path).copy()
    x["dt"] = pd.to_datetime(x["ts"], unit="s", utc=True)
    x = x.set_index("dt").sort_index()
    return x.resample("1h", label="right", closed="left").agg(
        open=("open","first"), high=("high","max"), low=("low","min"), close=("close","last")
    ).dropna()

def okx_hour(path):
    x = pd.read_pickle(path).copy()
    x["dt"] = pd.to_datetime(x["ts"], unit="s", utc=True)
    x = x.set_index("dt").sort_index()
    x.index = x.index + pd.Timedelta(hours=1)
    return x[["open","high","low","close"]]

def stablecoin_daily(path):
    raw = json.loads(Path(path).read_text())
    rows=[]
    for r in raw:
        try:
            ts = int(r["date"])
        except Exception:
            continue
        v = None
        for key in ("totalCirculatingUSD","totalCirculating"):
            obj = r.get(key)
            if isinstance(obj, dict):
                val = obj.get("peggedUSD")
                if val is not None:
                    try:
                        v=float(val)
                    except Exception:
                        v=None
                    if v is not None:
                        break
        if v is not None and np.isfinite(v) and v > 0:
            rows.append((pd.to_datetime(ts, unit="s", utc=True).normalize(), v))
    s = pd.Series(dict(rows), dtype=float).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    return s

def price_components(btc, eth):
    idx=btc.index.intersection(eth.index)
    b,e=btc.loc[idx],eth.loc[idx]
    rb=b["close"].pct_change().fillna(0.0)
    re=e["close"].pct_change().fillna(0.0)
    port_r=0.5*rb+0.5*re
    bd=b["close"].resample("1D", label="right", closed="left").last().dropna()
    ed=e["close"].resample("1D", label="right", closed="left").last().dropna()
    didx=bd.index.intersection(ed.index)
    bd,ed=bd.loc[didx],ed.loc[didx]
    pr=0.5*bd.pct_change()+0.5*ed.pct_change()
    s={
        "b7":np.sign(bd.pct_change(7)),
        "b28":np.sign(bd.pct_change(28)),
        "e7":np.sign(ed.pct_change(7)),
        "e28":np.sign(ed.pct_change(28)),
        "vol":pr.rolling(28,min_periods=20).std(ddof=0)*np.sqrt(365.0),
        "down":np.sqrt((pr.clip(upper=0.0)**2).rolling(28,min_periods=20).mean())*np.sqrt(365.0),
    }
    b4=b["close"].resample("4h",label="right",closed="left").last().dropna()
    e4=e["close"].resample("4h",label="right",closed="left").last().dropna()
    s["b4"]=np.sign(b4.rolling(18).mean()-b4.rolling(50).mean())
    s["e4"]=np.sign(e4.rolling(18).mean()-e4.rolling(50).mean())
    f=pd.DataFrame(index=idx)
    for k,v in s.items():
        f[k]=v.reindex(idx,method="ffill")
    vote_cols=["b7","b28","e7","e28","b4","e4"]
    sum6=f[vote_cols].sum(axis=1)
    breadth=(sum6/6.0).clip(lower=0.0)
    vol_cap=(0.20/f["vol"]).clip(upper=1.0)
    down_cap=((0.20/np.sqrt(2.0))/f["down"]).clip(upper=1.0)
    risk_cap=(vol_cap*down_cap).clip(0.0,1.0).fillna(0.0)
    base=(breadth*risk_cap).clip(0.0,1.0).fillna(0.0)
    return pd.DataFrame({"port_r":port_r,"sum6":sum6,"risk_cap":risk_cap,"base_signal":base},index=idx)

def liquidity_vote_hourly(stables, idx, availability_lag_days=1):
    raw_vote=np.sign(stables.pct_change(28)).replace([np.inf,-np.inf],np.nan)
    raw_vote.index=raw_vote.index+pd.Timedelta(days=availability_lag_days)
    return raw_vote.reindex(idx,method="ffill").fillna(0.0)

def candidate(c, liq_vote, invert=False):
    v=(-liq_vote if invert else liq_vote).clip(-1.0,1.0)
    breadth=((c["sum6"]+v)/6.0).clip(0.0,1.0)
    out=c.copy()
    out["liq_vote"]=v
    out["candidate_signal"]=(breadth*c["risk_cap"]).clip(0.0,1.0).fillna(0.0)
    return out

def apply_cost(x, cost):
    y=x.copy()
    for label,col in (("base","base_signal"),("candidate","candidate_signal")):
        exp=y[col].shift(1).fillna(0.0)
        turn=exp.diff().abs().fillna(exp.abs())
        net=exp*y["port_r"]-cost*turn
        y[f"exp_{label}"]=exp
        y[f"turn_{label}"]=turn
        y[f"net_{label}"]=net
    return y

def metrics(x,label):
    r=x[f"net_{label}"].fillna(0.0)
    if len(r)==0:
        return {"return":0.0,"sharpe":None,"max_drawdown":0.0,"turnover":0.0,"avg_exposure":0.0}
    eq=(1+r).cumprod()
    sd=float(r.std(ddof=0))
    return {
        "return":float(eq.iloc[-1]-1),
        "sharpe":float(r.mean()/sd*np.sqrt(365*24)) if sd>0 else None,
        "max_drawdown":float((eq/eq.cummax()-1).min()),
        "turnover":float(x[f"turn_{label}"].sum()),
        "avg_exposure":float(x[f"exp_{label}"].mean()),
    }

def campaigns(x,label):
    active=x[f"exp_{label}"]>1e-12
    gid=(active!=active.shift(fill_value=False)).cumsum()
    vals=[]
    for _,g in x[active].groupby(gid[active]):
        vals.append(float((1+g[f"net_{label}"].fillna(0.0)).prod()-1))
    if not vals:
        return {"n":0,"win_rate":None,"mean_return":None}
    a=np.asarray(vals)
    return {"n":int(len(a)),"win_rate":float((a>0).mean()),"mean_return":float(a.mean()),"median_return":float(np.median(a))}

def sl(x,start,end):
    return x[(x.index>=pd.Timestamp(start,tz="UTC"))&(x.index<=pd.Timestamp(end,tz="UTC"))]

def report(x,periods):
    out={}
    for name,start,end in periods:
        z=sl(x,start,end)
        out[name]={
            "base":metrics(z,"base"),"candidate":metrics(z,"candidate"),
            "base_campaigns":campaigns(z,"base"),"candidate_campaigns":campaigns(z,"candidate")
        }
    return out

def shift_control(c, vote, start, end, cost, max_shifts=1200):
    idx=c.index
    mask=(idx>=pd.Timestamp(start,tz="UTC"))&(idx<=pd.Timestamp(end,tz="UTC"))
    base_x=c.copy(); base_x["candidate_signal"]=c["base_signal"]; base_x=apply_cost(base_x,cost)
    b=metrics(base_x.loc[mask],"base")
    vals=vote.to_numpy(); sum6=c["sum6"].to_numpy(); risk=c["risk_cap"].to_numpy(); pr=c["port_r"].to_numpy()
    def one(v):
        sig=np.clip((sum6+v)/6.0,0.0,1.0)*risk
        exp=np.r_[0.0,sig[:-1]]
        turn=np.abs(np.diff(np.r_[0.0,exp]))
        net=exp*pr-cost*turn
        r=net[mask]
        eq=np.cumprod(1+r)
        ret=float(eq[-1]-1) if len(eq) else 0.0
        sd=float(np.std(r))
        sh=float(np.mean(r)/sd*np.sqrt(365*24)) if sd>0 else None
        return ret,sh
    obs=one(vals)
    full_days=max(1,len(vals)//24)
    shifts=np.arange(1,full_days)
    if len(shifts)>max_shifts:
        shifts=np.unique(np.linspace(1,full_days-1,max_shifts).astype(int))
    ctrl=np.asarray([one(np.roll(vals,24*int(k))) for k in shifts],float)
    dret=obs[0]-b["return"]; dsh=obs[1]-b["sharpe"]
    return {
        "observed":{"return":obs[0],"sharpe":obs[1]},"base":b,"n_shifts":int(len(shifts)),
        "return_percentile":float((ctrl[:,0]<obs[0]).mean()),
        "sharpe_percentile":float((ctrl[:,1]<obs[1]).mean()),
        "p_return_improvement":float((1+np.sum((ctrl[:,0]-b["return"])>=dret))/(len(ctrl)+1)),
        "p_sharpe_improvement":float((1+np.sum((ctrl[:,1]-b["sharpe"])>=dsh))/(len(ctrl)+1)),
    }

def dataset(btc,eth,stables,cost,periods,lag=1,invert=False):
    c=price_components(btc,eth)
    v=liquidity_vote_hourly(stables,c.index,availability_lag_days=lag)
    x=apply_cost(candidate(c,v,invert=invert),cost)
    return c,v,report(x,periods),x

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--manager",default="manager_library_out")
    ap.add_argument("--external",default="external_holdout_out")
    ap.add_argument("--okx",default="okx_out")
    ap.add_argument("--stablecoins",default="stablecoincharts_all.json")
    ap.add_argument("--out",default="stablecoin_liquidity_out")
    args=ap.parse_args()
    outdir=Path(args.out); outdir.mkdir(exist_ok=True)
    mb=minute_to_hour(Path(args.manager)/"data/BTC.pkl"); me=minute_to_hour(Path(args.manager)/"data/ETH.pkl")
    eb=minute_to_hour(Path(args.external)/"data/BTC_2020_21.pkl"); ee=minute_to_hour(Path(args.external)/"data/ETH_2020_21.pkl")
    ob=okx_hour(Path(args.okx)/"data/BTC-USDT_1h.pkl"); oe=okx_hour(Path(args.okx)/"data/ETH-USDT_1h.pkl")
    st=stablecoin_daily(args.stablecoins)
    bp=[("2022","2022-01-01","2022-12-31 23:59"),("2023","2023-01-01","2023-12-31 23:59"),("2024","2024-01-01","2024-12-31 23:59"),("2025","2025-01-01","2025-12-31 23:59"),("2026","2026-01-01","2026-10-03 23:59"),("2022_2026","2022-01-01","2026-10-03 23:59")]
    ep=[("2020","2020-01-01","2020-12-31 23:59"),("2021","2021-01-01","2021-12-31 23:59"),("2020_2021","2020-01-01","2021-12-31 23:59")]
    op=[("2022","2022-01-01","2022-12-31 23:59"),("2023","2023-01-01","2023-12-31 23:59"),("2024","2024-01-01","2024-12-31 23:59"),("2025","2025-01-01","2025-12-31 23:59"),("2026","2026-01-01","2026-10-05 19:59"),("2022_2026","2022-01-01","2026-10-05 19:59")]
    result={"method":{"baseline":"Frozen 50/50 BTC+ETH sleeve: completed 7d/28d direction votes, completed 4h SMA18/50 votes, 20% volatility cap, 28d downside-semivolatility cap.","candidate":"Add one equal-weight external liquidity vote: sign of total USD stablecoin supply 28-day change. Positive expansion adds one vote; contraction subtracts one vote. No fitted threshold or coefficient.","causality":"DefiLlama daily observation dated D is first usable on D+1 UTC; exposure still changes one hour after the resulting signal. T+2 is a publication-lag stress.","external_controls":"Independent OKX price path; 2020-2021 historical holdout; inverted-sign control; circular daily timing shifts; 2x trading-cost stress; T+2 data-lag stress."},"stablecoin_data":{"rows":int(len(st)),"start":str(st.index.min()),"end":str(st.index.max())},"costs":{"base":0.00125,"stress":0.0025}}
    for tag,cost in (("base_cost",0.00125),("stress_cost",0.0025)):
        bc,bv,br,bx=dataset(mb,me,st,cost,bp,lag=1,invert=False)
        ec,ev,er,ex=dataset(eb,ee,st,cost,ep,lag=1,invert=False)
        oc,ov,orr,ox=dataset(ob,oe,st,cost,op,lag=1,invert=False)
        result[tag]={"binance":br,"external":er,"okx":orr}
        if tag=="base_cost":
            _,_,binv,_=dataset(mb,me,st,cost,bp,lag=1,invert=True)
            _,_,oinv,_=dataset(ob,oe,st,cost,op,lag=1,invert=True)
            _,_,elag,_=dataset(eb,ee,st,cost,ep,lag=2,invert=False)
            _,_,blag,_=dataset(mb,me,st,cost,bp,lag=2,invert=False)
            _,_,olag,_=dataset(ob,oe,st,cost,op,lag=2,invert=False)
            result["controls"]={"inverted_sign":{"binance":binv,"okx":oinv},"t_plus_2":{"binance":blag,"external":elag,"okx":olag},"circular_shift":{"binance":shift_control(bc,bv,"2022-01-01","2026-10-03 23:59",cost),"external":shift_control(ec,ev,"2020-01-01","2021-12-31 23:59",cost),"okx":shift_control(oc,ov,"2022-01-01","2026-10-05 19:59",cost)}}
    b=result["base_cost"]["binance"]["2022_2026"]; o=result["base_cost"]["okx"]["2022_2026"]; e=result["base_cost"]["external"]["2020_2021"]
    bs=result["stress_cost"]["binance"]["2022_2026"]; os=result["stress_cost"]["okx"]["2022_2026"]
    bt=result["controls"]["t_plus_2"]["binance"]["2022_2026"]; ot=result["controls"]["t_plus_2"]["okx"]["2022_2026"]
    def better(z):
        return z["candidate"]["return"]>z["base"]["return"] and z["candidate"]["sharpe"]>z["base"]["sharpe"]
    validated=better(b) and better(o) and better(e) and better(bs) and better(os) and better(bt) and better(ot)
    result["conclusion"]={"validated":bool(validated),"promotion_allowed":False,"note":"This research branch never promotes or deploys rules automatically. A validated result would still require explicit review."}
    (outdir/"result.json").write_text(json.dumps(result,indent=2))
    print(json.dumps(result["conclusion"],indent=2))

if __name__=="__main__":
    main()
