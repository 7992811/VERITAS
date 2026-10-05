"""Independent OKX replication of VERITAS frozen STRONG_21D carry economics.

Research only. No order routing.

Venue:
- Spot: BTC-USDT / ETH-USDT
- Perpetual: BTC-USDT-SWAP / ETH-USDT-SWAP

Policy is NOT re-optimized for OKX. It preserves the economic rule:
- long spot + short perpetual, equal notionals
- use only already-settled realized funding
- require conservative 21-day expected funding >= 1.5x all round-trip costs
- positive funding persistence >= 78%
- executable basis >= -5bp
- exit after persistent funding deterioration or 42 days

Funding event counts are derived from the venue's causal trailing median funding
interval rather than assuming exactly 8 hours.

Fees are deliberately conservative current regular-tier taker assumptions:
- spot 10bp per side
- futures 5bp per side
plus 2.5bp slippage per execution.
Same signals are revalued with +5/+10/+20bp pair-level stress.

Historical public API coverage determines the usable independent sample.
"""
from __future__ import annotations

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

from research_crypto_manager_library import ts

OUT=Path("okx_carry_out")
CACHE=OUT/"data"
OUT.mkdir(exist_ok=True);CACHE.mkdir(exist_ok=True)

BASE="https://www.okx.com"
SPOT_FEE=.0010
SWAP_FEE=.0005
SLIP=.00025
PAIR_RT=2*(SPOT_FEE+SLIP)+2*(SWAP_FEE+SLIP)  # 40bp pair capital before stress
STRESS_LEVELS=(0.,.0005,.0010,.0020)

POLICIES=[
 {"name":"BREAKEVEN_14D","cost_multiple":1.25,"min_positive_frac":.67,"min_basis":-.0010,"target_days":14,"max_days":28},
 {"name":"STRONG_21D","cost_multiple":1.50,"min_positive_frac":.78,"min_basis":-.0005,"target_days":21,"max_days":42},
 {"name":"HIGH_CONVICTION_28D","cost_multiple":1.75,"min_positive_frac":.82,"min_basis":0.0,"target_days":28,"max_days":56},
]
START_TS=int(pd.Timestamp("2022-03-01T00:00:00Z").timestamp()*1000)
END_TS=int(pd.Timestamp("2026-10-05T00:00:00Z").timestamp()*1000)

def jd(o):
    if isinstance(o,np.integer):return int(o)
    if isinstance(o,np.floating):return float(o)
    if isinstance(o,np.bool_):return bool(o)
    raise TypeError(type(o).__name__)

def get(path,params):
    url=BASE+path+"?"+urlencode(params)
    last=None
    for k in range(6):
        try:
            req=Request(url,headers={"User-Agent":"VERITAS-OKX-carry-replication/1.0"})
            with urlopen(req,timeout=30) as r:
                obj=json.loads(r.read().decode("utf-8"))
            if str(obj.get("code","0"))!="0":
                raise RuntimeError(f"OKX {obj.get('code')}: {obj.get('msg')}")
            return obj.get("data",[])
        except Exception as e:
            last=e;time.sleep(.5+1.0*k)
    raise RuntimeError(f"{path}: {last}")

def get_bytes(url):
    last=None
    for k in range(6):
        try:
            req=Request(url,headers={"User-Agent":"VERITAS-OKX-carry-replication/1.0"})
            with urlopen(req,timeout=60) as r:
                return r.read()
        except Exception as e:
            last=e;time.sleep(.5+1.0*k)
    raise RuntimeError(f"download {url}: {last}")

def parse_funding_archive(blob, inst):
    rows=[]
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names=[n for n in zf.namelist() if not n.endswith("/")]
        for name in names:
            raw=zf.read(name)
            try:
                df=pd.read_csv(io.BytesIO(raw))
            except Exception:
                continue
            cols={str(x).strip().lower():x for x in df.columns}
            tcol=next((cols[k] for k in ("fundingtime","funding_time","ts","timestamp") if k in cols),None)
            rcol=next((cols[k] for k in ("realizedrate","realized_rate","fundingrate","funding_rate") if k in cols),None)
            icol=next((cols[k] for k in ("instid","inst_id") if k in cols),None)
            if tcol is None or rcol is None:
                print("OKX_ARCHIVE_HEADER_UNKNOWN",name,list(df.columns),flush=True)
                continue
            for rr in df.itertuples(index=False):
                d=rr._asdict()
                try:
                    if icol is not None and str(d.get(str(icol),d.get(icol,""))) not in ("",inst):
                        continue
                except Exception:
                    pass
            for _,rr in df.iterrows():
                try:
                    if icol is not None and str(rr[icol])!=inst:continue
                    t=int(float(rr[tcol]));t=t//1000 if t>10**11 else t
                    rate=float(rr[rcol]);rows.append((t,rate))
                except Exception:
                    continue
    return rows

def archive_funding(inst):
    # Public archive module 3 = funding rate. Monthly files are available from
    # March 2022. Query <=10 inclusive months at a time.
    family=inst.replace("-SWAP","")
    start=pd.Timestamp("2022-03-01",tz="UTC")
    # T+2 publication lag; recent history is merged from normal REST below.
    end=pd.Timestamp.now(tz="UTC").normalize()-pd.Timedelta(days=4)
    rows=[];cur=start
    while cur<=end:
        chunk_end=min(cur+pd.DateOffset(months=8),end)
        q={
          "module":"3","instType":"SWAP","dateAggrType":"monthly",
          "begin":str(int(cur.timestamp()*1000)),
          "end":str(int(chunk_end.timestamp()*1000)),
          "instFamilyList":family,
        }
        try:
            data=get("/api/v5/public/market-data-history",q)
        except Exception as e:
            print(inst,"archive_query_fail",cur.date(),chunk_end.date(),str(e)[:180],flush=True)
            cur=(chunk_end+pd.Timedelta(days=1)).replace(day=1)
            continue
        urls=[]
        for root in data:
            for item in root.get("details",[]) or []:
                for gd in item.get("groupDetails",[]) or []:
                    u=gd.get("url")
                    if u:urls.append(u)
        print(inst,"archive_range",str(cur.date()),str(chunk_end.date()),"files",len(urls),flush=True)
        for u in sorted(set(urls)):
            try:rows.extend(parse_funding_archive(get_bytes(u),inst))
            except Exception as e:print(inst,"archive_file_fail",u,str(e)[:160],flush=True)
        cur=(chunk_end+pd.DateOffset(months=1)).replace(day=1)
        time.sleep(.25)
    return rows

def funding(inst):
    p=CACHE/f"{inst}_funding.pkl"
    if p.exists():return pd.read_pickle(p)
    rows=archive_funding(inst)
    after=None;last_old=None
    # Merge recent normal REST history (roughly most recent months).
    for _ in range(40):
        q={"instId":inst,"limit":"400"}
        if after is not None:q["after"]=str(after)
        data=get("/api/v5/public/funding-rate-history",q)
        if not data:break
        oldest=None
        for r in data:
            try:
                t=int(r["fundingTime"])
                rr=r.get("realizedRate")
                rate=float(rr if rr not in (None,"") else r["fundingRate"])
                rows.append((t//1000,rate))
                oldest=t if oldest is None else min(oldest,t)
            except Exception:continue
        if oldest is None or oldest<=START_TS:break
        if last_old==oldest:break
        last_old=oldest;after=oldest
        time.sleep(.12)
    z=pd.DataFrame(rows,columns=["ts","rate"]).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    if len(z):z.to_pickle(p)
    print(inst,"funding_rows",len(z),"from",int(z.ts.min()) if len(z) else None,"to",int(z.ts.max()) if len(z) else None,flush=True)
    return z

def candles(inst):
    p=CACHE/f"{inst}_1h.pkl"
    if p.exists():return pd.read_pickle(p)
    rows=[];after=None;last_old=None
    # ~4.5y hourly / 300 per request => under 150 requests.
    for k in range(220):
        q={"instId":inst,"bar":"1H","limit":"300"}
        if after is not None:q["after"]=str(after)
        data=get("/api/v5/market/history-candles",q)
        if not data:break
        oldest=None
        for r in data:
            try:
                t=int(r[0]);o=float(r[1]);h=float(r[2]);l=float(r[3]);c=float(r[4]);confirm=str(r[8]) if len(r)>8 else "1"
                # Only completed hourly bars.
                if confirm!="1":continue
                rows.append((t//1000,o,h,l,c))
                oldest=t if oldest is None else min(oldest,t)
            except Exception:continue
        if oldest is None or oldest<=START_TS:break
        if last_old==oldest:break
        last_old=oldest;after=oldest
        if k%30==0:print(inst,"candle_pages",k+1,"oldest",oldest,flush=True)
        time.sleep(.12)
    z=pd.DataFrame(rows,columns=["ts","open","high","low","close"]).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    if len(z):z.to_pickle(p)
    print(inst,"candle_rows",len(z),"from",int(z.ts.min()) if len(z) else None,"to",int(z.ts.max()) if len(z) else None,flush=True)
    return z

def attach(asset):
    spot=f"{asset}-USDT";swap=f"{asset}-USDT-SWAP"
    f=funding(swap)
    s=candles(spot);p=candles(swap)
    if f.empty or s.empty or p.empty:return pd.DataFrame(),s,p

    # Strict causal sequence:
    # funding settlement T -> observe the completed common 1h close before T ->
    # decide -> execute at first common 1h open >= T+1h.
    common=s[["ts","open","close"]].merge(p[["ts","open","close"]],on="ts",suffixes=("_s","_p")).sort_values("ts").reset_index(drop=True)
    T=common.ts.to_numpy(np.int64)
    ft=f.ts.to_numpy(np.int64)
    obs=np.searchsorted(T,ft,side="left")-1
    ent=np.searchsorted(T,ft+3600,side="left")
    good=(obs>=0)&(ent<len(common))
    z=f.loc[good].copy().reset_index(drop=True)
    obs=obs[good];ent=ent[good]
    z["basis_obs_ts"]=T[obs]+3600
    z["basis"]=common.close_p.to_numpy()[obs]/common.close_s.to_numpy()[obs]-1.
    z["entry_ts"]=T[ent]
    z["spot_open"]=common.open_s.to_numpy()[ent]
    z["perp_open"]=common.open_p.to_numpy()[ent]
    z=z[z.basis_obs_ts<=z.entry_ts].reset_index(drop=True)

    # Causal features; current settled rate is known before post-settlement entry.
    z["mean3"]=z.rate.rolling(3).mean().shift(1)
    z["mean9"]=z.rate.rolling(9).mean().shift(1)
    z["pos9"]=z.rate.gt(0).rolling(9).mean().shift(1)
    z["pos21"]=z.rate.gt(0).rolling(21).mean().shift(1)
    z["fund_vol"]=z.rate.rolling(21,min_periods=9).std().shift(1)
    z["forecast_rate"]=(.45*z.mean3+.35*z.mean9+.20*z.rate).clip(-.01,.01)
    z["forecast_lcb"]=z.forecast_rate-.50*z.fund_vol.fillna(0)
    z["positive_frac"]=.55*z.pos9+.45*z.pos21

    dt=pd.Series(z.ts).diff()
    z["interval_sec"]=dt.rolling(21,min_periods=9).median().shift(1)
    z["forecast_events"]=(POL["target_days"]*86400/z.interval_sec).round().clip(1,1000)
    z["max_events"]=(POL["max_days"]*86400/z.interval_sec).round().clip(1,2000)
    return z,common,common

def entry_ok(r,pol):
    vals=(r.forecast_lcb,r.positive_frac,r.basis,r.mean3,r.mean9,r.rate,r.forecast_events,r.max_events)
    if not all(np.isfinite(v) for v in vals):return False
    expected=max(0.,float(r.forecast_lcb))*int(round(pol["target_days"]*86400/float(r.interval_sec)))
    return (expected>=pol["cost_multiple"]*PAIR_RT and
            r.positive_frac>=pol["min_positive_frac"] and
            r.basis>=pol["min_basis"] and r.mean3>0 and r.mean9>0 and r.rate>0)

def exit_now(r,held):
    if held<3:return False
    return bool((r.mean3<=0 and r.rate<=0) or r.mean9<=0 or r.basis<-.004)

def simulate(z,common,pol,stress=0.):
    if z.empty:return []
    T=common.ts.to_numpy(np.int64);SO=common.open_s.to_numpy(float);SC=common.close_s.to_numpy(float)
    PO=common.open_p.to_numpy(float);PC=common.close_p.to_numpy(float)
    trades=[];i=0
    while i<len(z):
        r=z.iloc[i]
        if not entry_ok(r,pol):
            i+=1;continue
        entry_i=i;entry_ts=int(r.entry_ts)
        ci=np.searchsorted(T,entry_ts)
        if ci>=len(T):break
        s0=float(SO[ci])*(1+SLIP) # buy spot
        p0=float(PO[ci])*(1-SLIP) # short swap
        funding_sum=0.;j=i+1;last=min(len(z)-1,i+int(r.max_events));reason="MAX"
        while j<=last:
            rr=z.iloc[j]
            mt=int(rr.ts)
            mi=np.searchsorted(T,mt,side="left")-1
            if mi>=0:
                mark=float(PC[mi])
                funding_sum+=float(rr.rate)*(mark/p0)
            if exit_now(rr,j-entry_i):
                reason="CARRY_END";break
            j+=1
        if j>last:j=last
        exit_ts=int(z.iloc[j].ts)+3600
        cj=np.searchsorted(T,exit_ts,side="left")
        if cj>=len(T):break
        s1=float(SO[cj])*(1-SLIP)
        p1=float(PO[cj])*(1+SLIP)
        spot_ratio=s1/s0
        perp_ratio=p1/p0
        spot_pnl=spot_ratio-1.
        perp_pnl=1.-perp_ratio
        # Entry fee on one initial notional per leg; exit fee scales with
        # actual exit notionals. Slippage is kept as explicit four-execution cost.
        execution_cost=SPOT_FEE*(1.+spot_ratio)+SWAP_FEE*(1.+perp_ratio)+4*SLIP
        pair=spot_pnl+perp_pnl+funding_sum-execution_cost-stress
        net=pair/2.

        # Pair price MAE, excluding positive funding credit.
        path_s=SC[ci:cj+1]/s0-1.
        path_p=1.-PC[ci:cj+1]/p0
        mae=float(min(0.,np.min((path_s+path_p)/2.))) if len(path_s) else 0.
        trades.append({"opened":entry_ts,"closed":int(T[cj]),"net":float(net),
                       "funding":float(funding_sum),"spot":float(spot_pnl),"perp":float(perp_pnl),
                       "execution_cost":float(execution_cost),
                       "spot_exit_ratio":float(spot_ratio),"perp_exit_ratio":float(perp_ratio),
                       "pair_price_mae":mae,"entry_basis":float(r.basis),
                       "events":int(j-entry_i),"reason":reason})
        i=j+1
    return trades

def met(tr):
    if not tr:return {"n":0,"win_rate":None,"avg":None,"sum":0.,"pf":0.,"dd":0.,"worst_pair_mae":None}
    a=np.asarray([x["net"] for x in tr],float);pos=a[a>0].sum();neg=-a[a<0].sum()
    eq=np.cumsum(a);pk=np.maximum.accumulate(np.r_[0.,eq])[1:]
    return {"n":len(a),"win_rate":float((a>0).mean()),"avg":float(a.mean()),"sum":float(a.sum()),
            "pf":float(pos/neg) if neg else 99.,"dd":float((pk-eq).max()),
            "worst_pair_mae":float(min(x["pair_price_mae"] for x in tr))}

def yearly(tr):
    out={}
    for y in range(2022,2027):
        a=ts(f"{y}-01-01T00:00:00Z");b=ts(f"{y+1}-01-01T00:00:00Z") if y<2026 else ts("2026-10-05T00:00:00Z")
        out[str(y)]=met([x for x in tr if a<=x["opened"]<b])
    return out

def bootstrap(tr,nboot=3000,block=3):
    if len(tr)<10:return {"n":len(tr),"p_positive":None,"p05_sum":None,"p95_dd":None}
    a=np.asarray([x["net"] for x in tr],float);n=len(a);rng=np.random.default_rng(20261005)
    starts=np.arange(max(1,n-block+1));sums=[];dds=[]
    for _ in range(nboot):
        q=[]
        while len(q)<n:
            k=int(rng.choice(starts));q.extend(a[k:k+block].tolist())
        q=np.asarray(q[:n]);eq=np.cumsum(q);pk=np.maximum.accumulate(np.r_[0.,eq])[1:]
        sums.append(float(q.sum()));dds.append(float((pk-eq).max()))
    return {"n":n,"p_positive":float(np.mean(np.asarray(sums)>0)),
            "p05_sum":float(np.quantile(sums,.05)),"p95_dd":float(np.quantile(dds,.95))}

def run(asset):
    z,common,_=attach(asset)
    out={"asset":asset,"data":{"funding_rows":int(len(z)),"common_hourly_rows":int(len(common)),
                               "start_ts":int(common.ts.min()) if len(common) else None,
                               "end_ts":int(common.ts.max()) if len(common) else None},
         "costs":{"spot_fee_each_side":SPOT_FEE,"swap_fee_each_side":SWAP_FEE,
                  "slippage_each_execution":SLIP,"pair_roundtrip_cost":PAIR_RT},
         "policies":{}}
    for pol in POLICIES:
        pr={"policy":pol}
        for st in STRESS_LEVELS:
            tr=simulate(z,common,pol,st)
            key=f"stress_{int(st*10000)}bp" if st else "base"
            pr[key]={"metrics":met(tr),"years":yearly(tr),"bootstrap":bootstrap(tr),"trades":tr}
        out["policies"][pol["name"]]=pr
        print(asset,pol["name"],"OKX_CARRY_RESULT",json.dumps({k:{"metrics":v["metrics"],"years":v["years"],"bootstrap":v["bootstrap"]} for k,v in pr.items() if k=="base" or k.startswith("stress_")},separators=(",",":"),default=jd),flush=True)
    return out

def main():
    out={"generated_at":datetime.now(timezone.utc).isoformat(),
         "method":"Independent OKX replication of frozen STRONG_21D economic carry rule.",
         "assets":{}}
    for a in ("BTC","ETH"):
        out["assets"][a]=run(a)
    (OUT/"result.json").write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False,default=jd))
    print("VERITAS_OKX_CARRY="+json.dumps({a:{p:{k:{"metrics":v["metrics"],"bootstrap":v["bootstrap"]} for k,v in pr.items() if k=="base" or k.startswith("stress_")} for p,pr in z["policies"].items()} for a,z in out["assets"].items()},separators=(",",":"),default=jd),flush=True)

if __name__=="__main__":
    main()
