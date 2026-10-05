"""VERITAS full-arsenal expansion research.

Research only. Adds economically distinct families that were missing from the
earlier price-action-heavy research:
- carry / funding / basis
- relative-value ETH/BTC mean reversion and momentum
- systematic mean reversion / failed breakout
- macro-regime filtered trend
- volatility compression/expansion
- optional on-chain regime filter (Coin Metrics Community API)

Protocol:
- fixed hypothesis templates, not a broad parameter mine;
- choose one exit variant per family on 2022-2023;
- require frozen choice to remain positive in BOTH 2024 and 2025 after costs
  and under +5bp stress;
- only then inspect Apr-Oct 2026.
BTC and ETH are evaluated independently except explicit relative-value pair modules.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

from research_crypto_manager_library import (
    load, ts, FEE, SLIP, FUND_LONG, FUND_SHORT, YEAR_MIN,
    TEST_START, TEST_END
)
import research_crypto_derivative_router as der

OUT = Path("full_arsenal_out")
CACHE = OUT / "data"
OUT.mkdir(exist_ok=True)
CACHE.mkdir(exist_ok=True)

ASSETS = ("BTC", "ETH")
SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}
BAR = 300
MAX_HOLD = 576   # 48h on 5m bars
COOLDOWN = 3
STRESS = 0.0005

ACTIONS = [
    {"name": "F15", "stop_atr": 1.00, "rr": 1.50, "partial": False},
    {"name": "F20", "stop_atr": 1.00, "rr": 2.00, "partial": False},
    {"name": "W20", "stop_atr": 1.50, "rr": 2.00, "partial": False},
    {"name": "W25", "stop_atr": 1.50, "rr": 2.50, "partial": False},
    {"name": "P30", "stop_atr": 1.25, "rr": 3.00, "partial": True},
]

def jdefault(o):
    if isinstance(o, np.integer): return int(o)
    if isinstance(o, np.floating): return float(o)
    if isinstance(o, np.bool_): return bool(o)
    raise TypeError(type(o).__name__)

def get(url, timeout=90):
    last = None
    for k in range(5):
        try:
            with urlopen(Request(url, headers={"User-Agent":"VERITAS-full-arsenal/1.0"}), timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(1.0 + 1.5*k)
    raise RuntimeError(f"{url}: {last}")

def pf(a):
    a=np.asarray(a,float)
    if not len(a): return 0.0
    p=a[a>0].sum(); n=-a[a<0].sum()
    return float(p/n) if n>0 else 99.0

def met(trades):
    a=np.asarray([x["net"] for x in trades],float)
    if not len(a):
        return {"n":0,"win_rate":None,"avg":None,"sum":0.0,"pf":0.0,"dd":0.0}
    eq=np.cumsum(a); pk=np.maximum.accumulate(np.r_[0.0,eq])[1:]
    return {
        "n":int(len(a)), "win_rate":float((a>0).mean()), "avg":float(a.mean()),
        "sum":float(a.sum()), "pf":pf(a), "dd":float((pk-eq).max())
    }

def resample5(raw):
    z=raw.copy()
    z.index=pd.to_datetime(z.ts,unit="s",utc=True)
    g=z.resample("5min",closed="left",label="left")
    x=g.agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"})
    cnt=g.size()
    x=x[cnt==5].copy()
    x["ts"]=(x.index.view("int64")//10**9).astype(np.int64)
    x=x.reset_index(drop=True)
    return x

def rsi(s,n=14):
    d=s.diff()
    up=d.clip(lower=0).ewm(alpha=1/n,adjust=False,min_periods=n).mean()
    dn=(-d.clip(upper=0)).ewm(alpha=1/n,adjust=False,min_periods=n).mean()
    rs=up/dn.replace(0,np.nan)
    return 100-100/(1+rs)

def common_features(raw):
    x=resample5(raw)
    pc=x.close.shift(1)
    tr=pd.concat([x.high-x.low,(x.high-pc).abs(),(x.low-pc).abs()],axis=1).max(axis=1)
    x["atr"]=tr.rolling(20,min_periods=20).mean()
    x["atr_med"]=x.atr.rolling(288,min_periods=96).median().shift(1)
    x["atr_ratio"]=x.atr/x.atr_med.replace(0,np.nan)
    x["vmed"]=x.volume.rolling(288,min_periods=96).median().shift(1)
    x["vr"]=x.volume/x.vmed.replace(0,np.nan)
    x["ema20"]=x.close.ewm(span=20,adjust=False).mean()
    x["ema50"]=x.close.ewm(span=50,adjust=False).mean()
    x["ema200"]=x.close.ewm(span=200,adjust=False).mean()
    x["rsi"]=rsi(x.close,14)
    rng=(x.high-x.low).replace(0,np.nan)
    x["bull"]=(x.close-x.open)/rng
    x["bear"]=(x.open-x.close)/rng
    for n in (12,36,72,144,288,576,2016):
        x[f"hi{n}"]=x.high.rolling(n,min_periods=n).max().shift(1)
        x[f"lo{n}"]=x.low.rolling(n,min_periods=n).min().shift(1)
    for n in (12,48,288):
        x[f"ret{n}"]=x.close/x.close.shift(n)-1
    # rolling trend efficiency
    x["eff48"]=(x.close-x.close.shift(48)).abs()/x.close.diff().abs().rolling(48).sum().replace(0,np.nan)
    x["mean288"]=x.close.rolling(288,min_periods=288).mean().shift(1)
    x["sd288"]=x.close.rolling(288,min_periods=288).std().shift(1)
    x["z288"]=(x.close-x.mean288)/x.sd288.replace(0,np.nan)
    rv=x.close.pct_change().rolling(12).std()
    x["rv12"]=rv
    x["rv12_med"]=rv.rolling(288,min_periods=96).median().shift(1)
    x["rv_ratio"]=rv/x.rv12_med.replace(0,np.nan)
    x["squeeze"]=x.atr.rolling(12).median().shift(1)/x.atr.rolling(144).median().shift(1)
    return x

def attach_pair_features(btc5,eth5):
    b=btc5.set_index("ts")
    e=eth5.set_index("ts")
    idx=b.index.intersection(e.index)
    ratio=np.log(e.loc[idx,"close"]/b.loc[idx,"close"])
    r=pd.DataFrame({"ts":idx,"ratio":ratio.to_numpy()})
    r["ratio_ret12"]=r.ratio-r.ratio.shift(12)
    r["ratio_ret48"]=r.ratio-r.ratio.shift(48)
    r["ratio_ma"]=r.ratio.rolling(2016,min_periods=1008).mean().shift(1)
    r["ratio_sd"]=r.ratio.rolling(2016,min_periods=1008).std().shift(1)
    r["ratio_z"]=(r.ratio-r.ratio_ma)/r.ratio_sd.replace(0,np.nan)
    r["ratio_hi288"]=r.ratio.rolling(288,min_periods=288).max().shift(1)
    r["ratio_lo288"]=r.ratio.rolling(288,min_periods=288).min().shift(1)
    return r

def parse_funding_zip(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        raw=z.read(z.namelist()[0]).decode("utf-8")
    rows=list(csv.reader(io.StringIO(raw)))
    if not rows: return pd.DataFrame(columns=["ts","funding"])
    header=[c.strip().lower() for c in rows[0]]
    start=1 if any("fund" in c or "time" in c for c in header) else 0
    out=[]
    for r in rows[start:]:
        if len(r)<2: continue
        # Binance archive has calc_time and last_funding_rate; tolerate schema changes.
        nums=[]
        for q in r:
            try: nums.append(float(q))
            except Exception: nums.append(np.nan)
        ti=None; fr=None
        for v in nums:
            if np.isfinite(v) and v>1e11 and ti is None: ti=int(v//1000)
        # funding is a small signed decimal; choose the first plausible non-time field.
        for v in nums:
            if np.isfinite(v) and abs(v)<0.1 and abs(v)>0:
                fr=float(v); break
        if ti is not None and fr is not None: out.append((ti,fr))
    return pd.DataFrame(out,columns=["ts","funding"])

def funding_history(asset):
    p=CACHE/f"{asset}_funding.pkl"
    if p.exists(): return pd.read_pickle(p)
    sym=SYMBOLS[asset]; parts=[]
    for per in pd.period_range("2022-01","2026-09",freq="M"):
        url=(f"https://data.binance.vision/data/futures/um/monthly/fundingRate/"
             f"{sym}/{sym}-fundingRate-{per.year}-{per.month:02d}.zip")
        try:
            parts.append(parse_funding_zip(get(url)))
        except Exception as e:
            print(asset,"funding_missing",str(per),str(e)[:120],flush=True)
    if not parts:
        return pd.DataFrame(columns=["ts","funding"])
    f=pd.concat(parts,ignore_index=True).drop_duplicates("ts").sort_values("ts")
    f.to_pickle(p)
    return f

def attach_funding_basis(asset,x,raw):
    f=funding_history(asset)
    out=x.copy()
    if len(f):
        s=f.set_index("ts").funding
        idx=np.searchsorted(s.index.to_numpy(),out.ts.to_numpy(),side="right")-1
        good=idx>=0
        arr=np.full(len(out),np.nan)
        vals=s.to_numpy()
        arr[good]=vals[idx[good]]
        out["funding"]=arr
        out["funding_ma"]=pd.Series(arr).rolling(12*21,min_periods=24).mean().shift(1).to_numpy()
        out["funding_sd"]=pd.Series(arr).rolling(12*21,min_periods=24).std().shift(1).to_numpy()
        out["funding_z"]=(out.funding-out.funding_ma)/out.funding_sd.replace(0,np.nan)
    else:
        out["funding"]=np.nan; out["funding_z"]=np.nan
    try:
        fut=der.download_futures_5m(asset)
        ff=der.add_derivative_features(fut,raw)
        ft=ff.avail_ts.to_numpy(np.int64); t=out.ts.to_numpy(np.int64)
        ix=np.searchsorted(ft,t,side="right")-1; good=ix>=0
        for src,dst in [("basis_z","basis_z"),("basis_impulse_z","basis_imp"),
                        ("imb3_z","imb3"),("imb12_z","imb12"),("fut_vol_ratio","fut_vr"),
                        ("lead_z","lead_z")]:
            arr=np.full(len(out),np.nan)
            if good.any(): arr[good]=ff[src].to_numpy()[ix[good]]
            out[dst]=arr
    except Exception as e:
        print(asset,"derivative_attach_failed",str(e)[:180],flush=True)
        for c in ("basis_z","basis_imp","imb3","imb12","fut_vr","lead_z"): out[c]=np.nan
    return out

def fred_series(series):
    url=f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
    b=get(url).decode("utf-8")
    z=pd.read_csv(io.StringIO(b))
    z.columns=["date",series]
    z["date"]=pd.to_datetime(z.date,utc=True,errors="coerce")
    z[series]=pd.to_numeric(z[series],errors="coerce")
    return z.dropna(subset=["date"])

def macro_daily():
    p=CACHE/"macro.pkl"
    if p.exists(): return pd.read_pickle(p)
    names=["VIXCLS","DTWEXBGS","DFII10","DFF","NFCI"]
    base=None
    for n in names:
        try:
            z=fred_series(n)
            base=z if base is None else base.merge(z,on="date",how="outer")
        except Exception as e:
            print("macro_missing",n,str(e)[:120],flush=True)
    if base is None: return pd.DataFrame()
    base=base.sort_values("date").ffill()
    for n in [c for c in base.columns if c!="date"]:
        base[f"{n}_20d"]=base[n].pct_change(20) if n not in ("DFF","DFII10","NFCI") else base[n].diff(20)
    base["ts"]=(base.date.view("int64")//10**9).astype(np.int64)+24*3600
    base.to_pickle(p)
    return base

def attach_macro(x):
    m=macro_daily()
    out=x.copy()
    if m.empty:
        for c in ("vix","usd","real10","ff","nfci","vix20","usd20","real20","nfci20"): out[c]=np.nan
        return out
    mt=m.ts.to_numpy(np.int64); t=out.ts.to_numpy(np.int64)
    ix=np.searchsorted(mt,t,side="right")-1; good=ix>=0
    mapping={"VIXCLS":"vix","DTWEXBGS":"usd","DFII10":"real10","DFF":"ff","NFCI":"nfci",
             "VIXCLS_20d":"vix20","DTWEXBGS_20d":"usd20","DFII10_20d":"real20","NFCI_20d":"nfci20"}
    for src,dst in mapping.items():
        arr=np.full(len(out),np.nan)
        if src in m and good.any(): arr[good]=m[src].to_numpy()[ix[good]]
        out[dst]=arr
    return out

def coinmetrics_daily(asset):
    p=CACHE/f"{asset}_onchain.pkl"
    if p.exists(): return pd.read_pickle(p)
    metrics=["AdrActCnt","TxCnt","CapMrktCurUSD","CapRealUSD","NVTAdj"]
    params={
        "assets":asset.lower(),"metrics":",".join(metrics),"frequency":"1d",
        "start_time":"2022-01-01","end_time":"2026-10-04","page_size":10000
    }
    url="https://community-api.coinmetrics.io/v4/timeseries/asset-metrics?"+urlencode(params)
    try:
        obj=json.loads(get(url).decode("utf-8"))
        rows=obj.get("data",[])
        z=pd.DataFrame(rows)
        if z.empty: return z
        z["date"]=pd.to_datetime(z["time"],utc=True)
        for c in metrics:
            if c in z: z[c]=pd.to_numeric(z[c],errors="coerce")
        z["ts"]=(z.date.view("int64")//10**9).astype(np.int64)+24*3600
        z.to_pickle(p)
        return z
    except Exception as e:
        print(asset,"onchain_unavailable",str(e)[:180],flush=True)
        return pd.DataFrame()

def attach_onchain(asset,x):
    z=coinmetrics_daily(asset)
    out=x.copy()
    cols=["AdrActCnt","TxCnt","CapMrktCurUSD","CapRealUSD","NVTAdj"]
    if z.empty:
        for c in cols: out["oc_"+c]=np.nan
        return out
    for c in cols:
        if c in z:
            z[c+"_roc30"]=z[c].pct_change(30)
    t=out.ts.to_numpy(np.int64); zt=z.ts.to_numpy(np.int64)
    ix=np.searchsorted(zt,t,side="right")-1; good=ix>=0
    for c in cols:
        arr=np.full(len(out),np.nan)
        src=c+"_roc30"
        if src in z and good.any(): arr[good]=z[src].to_numpy()[ix[good]]
        out["oc_"+c]=arr
    return out

def event_cross(x,d,n):
    lvl=x[f"hi{n}"] if d>0 else x[f"lo{n}"]
    return (d*(x.close-lvl)>0)&(d*(x.close.shift(1)-lvl)<=0),lvl

def build_events(asset,x):
    rows=[]
    for d in (1,-1):
        body=x.bull if d>0 else x.bear

        # 1) Fast trend breakout, multiple public trend-following speeds are handled
        # by separate fast/slow families rather than one universal threshold.
        cross,lvl=event_cross(x,d,36)
        m=cross&(d*x.ret12>0)&(d*x.ret48>0)&(x.vr>=1.15)&(body>=.40)&(x.atr_ratio>=.95)
        rows += collect(x,m,d,"TREND_FAST",lvl)

        # 2) Slow trend / channel.
        cross,lvl=event_cross(x,d,144)
        slow=(x.close>x.ema50)&(x.ema50>x.ema200) if d>0 else (x.close<x.ema50)&(x.ema50<x.ema200)
        m=cross&slow&(d*x.ret288>0)&(x.vr>=1.05)&(body>=.30)
        rows += collect(x,m,d,"TREND_SLOW",lvl)

        # 3) Mean reversion in low-efficiency/range state.
        z=x.z288
        cond=(z<=-2.0)&(x.rsi<35)&(body>=.25) if d>0 else (z>=2.0)&(x.rsi>65)&(body>=.25)
        m=cond&(x.eff48<=.30)&(x.atr_ratio<=1.35)
        rows += collect(x,m,d,"MEAN_REVERSION",x.mean288)

        # 4) Failed breakout / liquidity sweep proxy.
        level=x.lo72 if d>0 else x.hi72
        sweep=(x.low<level)&(x.close>level) if d>0 else (x.high>level)&(x.close<level)
        m=sweep&(body>=.25)&(x.eff48<=.45)&(x.vr>=1.05)
        rows += collect(x,m,d,"FAILED_BREAKOUT",level)

        # 5) Compression -> volatility expansion.
        cross,lvl=event_cross(x,d,72)
        m=cross&(x.squeeze.shift(3)<=.75)&(x.atr_ratio>=1.10)&(x.rv_ratio>=1.10)&(x.vr>=1.25)&(body>=.45)
        rows += collect(x,m,d,"VOL_EXPANSION",lvl)

        # 6) Carry-aligned trend: collect funding while following price trend.
        cross,lvl=event_cross(x,d,36)
        funding_ok=(x.funding_z<=-.35) if d>0 else (x.funding_z>=.35)
        basis_ok=(d*x.basis_z<=1.25)
        m=cross&funding_ok&basis_ok&(d*x.ret48>0)&(x.vr>=1.05)&(body>=.30)
        rows += collect(x,m,d,"CARRY_TREND",lvl)

        # 7) Crowded carry fade: extreme carry+basis, then structural reversal.
        cross,lvl=event_cross(x,d,12)
        old_crowd=(-d*x.funding_z>=1.25)&(-d*x.basis_z>=.75)
        flowturn=(d*x.imb3>=.20)
        m=cross&old_crowd&flowturn&(body>=.35)&(x.vr>=1.15)
        rows += collect(x,m,d,"CROWDED_FADE",lvl)

        # 8) Macro-confirmed trend. Daily macro is an overlay, not a timing signal.
        cross,lvl=event_cross(x,d,36)
        macro_long=(x.vix20<0)&(x.usd20<0)&(x.real20<=0)
        macro_short=(x.vix20>0)&(x.usd20>0)&(x.real20>=0)
        mok=macro_long if d>0 else macro_short
        m=cross&mok&(d*x.ret48>0)&(x.vr>=1.10)&(body>=.35)
        rows += collect(x,m,d,"MACRO_TREND",lvl)

        # 9) On-chain-confirmed swing trend; only fires if real on-chain history exists.
        cross,lvl=event_cross(x,d,144)
        act=x.oc_AdrActCnt.fillna(0); tx=x.oc_TxCnt.fillna(0)
        if d>0:
            om=(act>0)&(tx>0)
        else:
            om=(act<0)&(tx<0)
        m=cross&om&(d*x.ret288>0)&(body>=.25)
        rows += collect(x,m,d,"ONCHAIN_TREND",lvl)

    # one signal per family/direction/bar
    seen=set(); out=[]
    for r in sorted(rows,key=lambda z:(z["sig"],z["family"],z["d"])):
        k=(r["sig"],r["family"],r["d"])
        if k in seen: continue
        seen.add(k); out.append(r)
    return out

def collect(x,mask,d,family,level):
    out=[]
    idx=np.flatnonzero(mask.fillna(False).to_numpy())
    for i in idx:
        av=x.atr.iloc[i]
        lv=level.iloc[i] if hasattr(level,"iloc") else level
        if np.isfinite(av) and np.isfinite(lv):
            out.append({"sig":int(i),"ts":int(x.ts.iloc[i]),"d":int(d),"family":family,
                        "level":float(lv),"atr":float(av)})
    return out

def execution_slip(x,i):
    """Dynamic one-way slippage proxy from volatility and liquidity state.

    Base is 2.5bp. High volatility / weak relative volume increases expected
    slippage. The cap is deliberately conservative because we do not have
    historical full-depth L2 for the complete sample.
    """
    ar=float(x.atr_ratio.iloc[i]) if np.isfinite(x.atr_ratio.iloc[i]) else 1.0
    vr=float(x.vr.iloc[i]) if np.isfinite(x.vr.iloc[i]) else 1.0
    mult=max(1.0, math.sqrt(max(ar,0.25))/math.sqrt(max(vr,0.25)))
    return float(min(0.0015, max(SLIP, SLIP*mult)))

def simulate(x,events,action,start,end,stress=0.0):
    T=x.ts.to_numpy(np.int64); O=x.open.to_numpy(); H=x.high.to_numpy(); L=x.low.to_numpy(); C=x.close.to_numpy()
    st=ts(start) if isinstance(start,str) else int(start)
    en=ts(end) if isinstance(end,str) else int(end)
    ei=int(np.searchsorted(T,en)); out=[]; free=-1
    for e in events:
        sig=e["sig"]
        if T[sig]<st or T[sig]>=en-BAR: continue
        i=sig+1
        if i<free or i>=ei: continue
        d=e["d"]; eslip=execution_slip(x,i); entry=O[i]*(1+d*eslip)
        stop=entry-d*action["stop_atr"]*e["atr"]
        risk=d*(entry-stop)/entry
        if not (.0025<=risk<=.05): continue
        target=entry*(1+d*risk*action["rr"])
        gross=0.0; cost=FEE+stress/2; rem=1.0; partial=False
        j=i; last=min(ei-1,i+MAX_HOLD); reason="TIME"
        while j<=last:
            if j>i: cost+=rem*(FUND_LONG if d>0 else FUND_SHORT)/(YEAR_MIN/5)
            hitstop=(L[j]<=stop if d>0 else H[j]>=stop)
            if hitstop:
                q=(min(O[j],stop) if d>0 else max(O[j],stop))*(1-d*eslip)
                gross+=rem*d*(q/entry-1); cost+=rem*(FEE+stress/2)*q/entry; rem=0; reason="STOP"; break
            if action["partial"] and not partial:
                one=entry*(1+d*risk)
                if (H[j]>=one if d>0 else L[j]<=one):
                    q=one*(1-d*eslip)
                    gross+=.5*d*(q/entry-1); cost+=.5*(FEE+stress/2)*q/entry
                    rem=.5; partial=True; stop=entry
            if (H[j]>=target if d>0 else L[j]<=target):
                q=target*(1-d*eslip)
                gross+=rem*d*(q/entry-1); cost+=rem*(FEE+stress/2)*q/entry
                rem=0; reason="TARGET"; break
            j+=1
        if rem:
            j=min(j,last); q=C[j]*(1-d*eslip)
            gross+=rem*d*(q/entry-1); cost+=rem*(FEE+stress/2)*q/entry
        out.append({"opened":int(T[i]),"closed":int(T[j]),"d":d,"net":float(gross-cost),
                    "risk":float(risk),"family":e["family"],"action":action["name"],"reason":reason})
        free=j+COOLDOWN
    return out

def family_events(events,name):
    return [e for e in events if e["family"]==name]

def period_metrics(x,ev,a,y,stress=0.0):
    return met(simulate(x,ev,a,f"{y}-01-01T00:00:00Z",f"{y+1}-01-01T00:00:00Z",stress))

def choose_family(x,ev,name):
    z=family_events(ev,name)
    if len(z)<20: return None,{"reason":"TOO_FEW_EVENTS","n_events":len(z)}
    cand=[]
    for a in ACTIONS:
        m22=period_metrics(x,z,a,2022); m23=period_metrics(x,z,a,2023)
        s22=period_metrics(x,z,a,2022,STRESS); s23=period_metrics(x,z,a,2023,STRESS)
        if m22["n"]<5 or m23["n"]<5: continue
        if (m22["avg"] or -9)<=0 or (m23["avg"] or -9)<=0: continue
        if m22["pf"]<1.02 or m23["pf"]<1.02: continue
        if (s22["avg"] or -9)<=0 or (s23["avg"] or -9)<=0: continue
        score=100*min(m22["avg"],m23["avg"])+.25*min(m22["win_rate"],m23["win_rate"])+.25*min(m22["pf"],m23["pf"])
        cand.append((score,a,{"2022":m22,"2023":m23},{"2022":s22,"2023":s23}))
    if not cand: return None,{"reason":"NO_DISCOVERY_EDGE","n_events":len(z)}
    cand.sort(key=lambda q:q[0],reverse=True)
    score,a,disc,discstress=cand[0]
    v24=period_metrics(x,z,a,2024); v25=period_metrics(x,z,a,2025)
    s24=period_metrics(x,z,a,2024,STRESS); s25=period_metrics(x,z,a,2025,STRESS)
    valid=(v24["n"]>=5 and v25["n"]>=5 and (v24["avg"] or -9)>0 and (v25["avg"] or -9)>0
           and v24["pf"]>=1.05 and v25["pf"]>=1.05 and (s24["avg"] or -9)>0 and (s25["avg"] or -9)>0)
    out={"family":name,"action":a,"discovery":disc,"discovery_stress":discstress,
         "validation":{"2024":v24,"2025":v25},"validation_stress":{"2024":s24,"2025":s25},
         "validated_2024_2025":bool(valid),"n_events":len(z)}
    if valid:
        test=met(simulate(x,z,a,TEST_START,TEST_END,0.0))
        stres=met(simulate(x,z,a,TEST_START,TEST_END,STRESS))
        blocks=[]
        for a0,b0 in [("2026-04-04T00:00:00Z","2026-06-01T00:00:00Z"),
                      ("2026-06-01T00:00:00Z","2026-08-01T00:00:00Z"),
                      ("2026-08-01T00:00:00Z","2026-10-04T00:00:00Z")]:
            blocks.append(met(simulate(x,z,a,a0,b0,0.0)))
        out.update({"test_2026":test,"test_stress_5bp":stres,"test_blocks":blocks,
                    "passed_2026":bool(test["n"]>=6 and (test["avg"] or -9)>0 and test["pf"]>=1.15
                                      and (stres["avg"] or -9)>0 and sum((b["avg"] or -9)>0 for b in blocks)>=2)})
    return out,None

def pair_trade_frame(btc,eth):
    b=btc.set_index("ts"); e=eth.set_index("ts"); idx=b.index.intersection(e.index)
    x=pd.DataFrame({"ts":idx,"btc":b.loc[idx,"close"],"eth":e.loc[idx,"close"]}).reset_index(drop=True)
    rb=np.log(x.btc/x.btc.shift(1)); re=np.log(x.eth/x.eth.shift(1))
    win=2016
    cov=re.rolling(win,min_periods=1008).cov(rb).shift(1)
    var=rb.rolling(win,min_periods=1008).var().shift(1)
    x["beta"]=(cov/var.replace(0,np.nan)).clip(.25,4.0)
    x["spread"]=np.log(x.eth)-x.beta*np.log(x.btc)
    x["sm"]=x.spread.rolling(win,min_periods=1008).mean().shift(1)
    x["ss"]=x.spread.rolling(win,min_periods=1008).std().shift(1)
    x["z"]=(x.spread-x.sm)/x.ss.replace(0,np.nan)
    x["mom"]=x.spread-x.spread.shift(48)
    return x

def sim_pair(x,mode,start,end,stress=0.0):
    T=x.ts.to_numpy(np.int64); st=ts(start); en=ts(end)
    i0=np.searchsorted(T,st); i1=np.searchsorted(T,en)
    out=[]; free=i0
    for i in range(max(i0,2017),i1-1):
        if i<free or not np.isfinite(x.z.iloc[i]) or not np.isfinite(x.beta.iloc[i]): continue
        z=float(x.z.iloc[i]); mom=float(x.mom.iloc[i])
        side=0
        if mode=="PAIR_MEANREV":
            if z>=2.0: side=-1
            elif z<=-2.0: side=1
        elif mode=="PAIR_MOMENTUM":
            if z>=1.0 and mom>0: side=1
            elif z<=-1.0 and mom<0: side=-1
        if side==0: continue
        ent_sp=float(x.spread.iloc[i+1]); stop_z=3.0 if mode=="PAIR_MEANREV" else 0.25
        j=i+1; last=min(i1-1,j+576); reason="TIME"
        while j<=last:
            zj=float(x.z.iloc[j]) if np.isfinite(x.z.iloc[j]) else z
            if mode=="PAIR_MEANREV":
                if (side<0 and zj>=3.0) or (side>0 and zj<=-3.0): reason="STOP"; break
                if (side<0 and zj<=.25) or (side>0 and zj>=-.25): reason="TARGET"; break
            else:
                if (side>0 and zj<=stop_z) or (side<0 and zj>=-stop_z): reason="EXIT"; break
            j+=1
        move=side*(float(x.spread.iloc[j])-ent_sp)
        # Approximate pair P&L in log-return units, both legs incur costs.
        net=move-2*(2*(FEE+SLIP)+stress)
        out.append({"opened":int(T[i+1]),"closed":int(T[j]),"d":side,"net":float(net),"family":mode,"reason":reason})
        free=j+COOLDOWN
    return out

def pair_protocol(x,mode):
    disc={}
    valid={}
    stress={}
    for y in (2022,2023):
        disc[str(y)]=met(sim_pair(x,mode,f"{y}-01-01T00:00:00Z",f"{y+1}-01-01T00:00:00Z",0.0))
    discovered=(all(disc[str(y)]["n"]>=5 and (disc[str(y)]["avg"] or -9)>0 and disc[str(y)]["pf"]>=1.02 for y in (2022,2023)))
    if not discovered: return {"family":mode,"discovery":disc,"validated_2024_2025":False,"reason":"NO_DISCOVERY_EDGE"}
    for y in (2024,2025):
        valid[str(y)]=met(sim_pair(x,mode,f"{y}-01-01T00:00:00Z",f"{y+1}-01-01T00:00:00Z",0.0))
        stress[str(y)]=met(sim_pair(x,mode,f"{y}-01-01T00:00:00Z",f"{y+1}-01-01T00:00:00Z",STRESS))
    ok=all(valid[str(y)]["n"]>=5 and (valid[str(y)]["avg"] or -9)>0 and valid[str(y)]["pf"]>=1.05
           and (stress[str(y)]["avg"] or -9)>0 for y in (2024,2025))
    out={"family":mode,"discovery":disc,"validation":valid,"validation_stress":stress,"validated_2024_2025":ok}
    if ok:
        out["test_2026"]=met(sim_pair(x,mode,TEST_START,TEST_END,0.0))
        out["test_stress_5bp"]=met(sim_pair(x,mode,TEST_START,TEST_END,STRESS))
    return out

def run():
    raw={a:load(a) for a in ASSETS}
    x={a:common_features(raw[a]) for a in ASSETS}
    for a in ASSETS:
        x[a]=attach_funding_basis(a,x[a],raw[a])
        x[a]=attach_macro(x[a])
        x[a]=attach_onchain(a,x[a])
    out={"generated_at":datetime.now(timezone.utc).isoformat(),
         "method":"Fixed full-arsenal expansion hypotheses; 2022-23 discovery, 2024+2025 frozen validation, 2026 inspected only after pass.",
         "costs":{"fee_each_side":FEE,"base_slippage_each_side":SLIP,"dynamic_slippage_cap_each_side":0.0015,"stress_round_trip":STRESS},
         "assets":{},"pair_modules":{}}
    families=["TREND_FAST","TREND_SLOW","MEAN_REVERSION","FAILED_BREAKOUT","VOL_EXPANSION",
              "CARRY_TREND","CROWDED_FADE","MACRO_TREND","ONCHAIN_TREND"]
    for a in ASSETS:
        ev=build_events(a,x[a])
        rr={}; rej={}
        for fam in families:
            row,fail=choose_family(x[a],ev,fam)
            if row is not None: rr[fam]=row
            else: rej[fam]=fail
        out["assets"][a]={"families":rr,"rejected":rej,
                          "data_status":{"funding":bool(np.isfinite(x[a].funding).any()),
                                         "basis":bool(np.isfinite(x[a].basis_z).any()),
                                         "macro":bool(np.isfinite(x[a].vix).any()),
                                         "onchain":bool(np.isfinite(x[a].oc_AdrActCnt).any())}}
        print(a,"ARSENAL",json.dumps({"families":rr,"rejected":rej,"data_status":out["assets"][a]["data_status"]},
                                      separators=(",",":"),default=jdefault),flush=True)
    pair=pair_trade_frame(x["BTC"],x["ETH"])
    for mode in ("PAIR_MEANREV","PAIR_MOMENTUM"):
        out["pair_modules"][mode]=pair_protocol(pair,mode)
    (OUT/"result.json").write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False,default=jdefault))
    print("VERITAS_FULL_ARSENAL="+json.dumps(out,ensure_ascii=False,separators=(",",":"),default=jdefault),flush=True)

if __name__=="__main__":
    run()
