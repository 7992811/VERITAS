"""VERITAS scheduled macro event research for BTC and ETH.

Research-only. Uses official BLS release calendars for CPI and Employment
Situation. Ordinary price signals are not allowed to ignore these windows:
either a separately validated event setup trades them, or event risk becomes a
veto / size reduction in production.

This module studies two fixed public-style event responses:
1) confirmed post-release range breakout;
2) failed first impulse / range re-entry fade.
"""
from __future__ import annotations

import json, re, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from research_crypto_manager_library import load, FEE, SLIP

OUT=Path("event_driven_out"); OUT.mkdir(exist_ok=True)
ET=ZoneInfo("America/New_York")

def jd(o):
    if isinstance(o,np.integer):return int(o)
    if isinstance(o,np.floating):return float(o)
    if isinstance(o,np.bool_):return bool(o)
    raise TypeError(type(o).__name__)

def fetch_html(url):
    last=None
    for k in range(4):
        try:
            with urlopen(Request(url,headers={"User-Agent":"VERITAS-event-research/1.0"}),timeout=45) as r:
                return r.read().decode("utf-8","ignore")
        except Exception as e:
            last=e;time.sleep(1+k)
    raise RuntimeError(last)

def bls_events():
    out=[]
    for y in range(2022,2027):
        url=f"https://www.bls.gov/schedule/{y}/"
        try:
            html=fetch_html(url)
            # The BLS annual list has rows containing month/day, time and release title.
            # Strip tags into row-like text, then parse target releases.
            for row in re.findall(r"<tr[^>]*>(.*?)</tr>",html,flags=re.I|re.S):
                txt=re.sub(r"<[^>]+>"," ",row)
                txt=re.sub(r"&nbsp;"," ",txt)
                txt=re.sub(r"\s+"," ",txt).strip()
                kind=None
                if "Consumer Price Index" in txt: kind="CPI"
                elif "Employment Situation" in txt: kind="NFP"
                if not kind:continue
                md=re.search(r"([A-Z][a-z]{2,8}\.?\s+\d{1,2},?\s+%d)"%y,txt)
                tm=re.search(r"(\d{1,2}:\d{2})\s*(AM|PM)",txt,re.I)
                if not md or not tm:continue
                ds=md.group(1).replace(".","")
                dt=pd.to_datetime(ds).to_pydatetime()
                hh,mm=map(int,tm.group(1).split(":"))
                if tm.group(2).upper()=="PM" and hh!=12:hh+=12
                if tm.group(2).upper()=="AM" and hh==12:hh=0
                loc=datetime(dt.year,dt.month,dt.day,hh,mm,tzinfo=ET)
                out.append({"kind":kind,"ts":int(loc.astimezone(timezone.utc).timestamp()),"source":url})
        except Exception as e:
            print("BLS_PARSE_FAIL",y,str(e)[:160],flush=True)
    # de-duplicate title/date rows on annual pages
    seen=set();z=[]
    for e in sorted(out,key=lambda x:x["ts"]):
        k=(e["kind"],e["ts"])
        if k not in seen:seen.add(k);z.append(e)
    return z

def atr1(raw,n=60):
    pc=raw.close.shift(1)
    tr=pd.concat([raw.high-raw.low,(raw.high-pc).abs(),(raw.low-pc).abs()],axis=1).max(axis=1)
    return tr.rolling(n,min_periods=n).mean()

def event_trade(raw,event,mode):
    T=raw.ts.to_numpy(np.int64);O=raw.open.to_numpy();H=raw.high.to_numpy();L=raw.low.to_numpy();C=raw.close.to_numpy()
    i=int(np.searchsorted(T,event["ts"]))
    if i<180 or i+400>=len(raw):return None
    A=atr1(raw.iloc[max(0,i-200):i+400].reset_index(drop=True),60)
    av=float(A.iloc[min(200,len(A)-1)])
    if not np.isfinite(av) or av<=0:return None
    pre_hi=float(np.max(H[i-120:i]));pre_lo=float(np.min(L[i-120:i]))
    confirm=i+15
    post_close=float(C[confirm])
    d=1 if post_close>pre_hi else (-1 if post_close<pre_lo else 0)
    if mode=="EVENT_BREAKOUT":
        if d==0:return None
        # Require meaningful displacement, not a one-tick breach.
        lvl=pre_hi if d>0 else pre_lo
        if d*(post_close-lvl)<.20*av:return None
        entry=float(O[confirm+1])*(1+d*SLIP)
        stop=(pre_lo if d>0 else pre_hi)
        risk=d*(entry-stop)/entry
        if not (.002<=risk<=.04):return None
        target=entry*(1+d*1.5*risk)
    else:
        # First impulse must leave the old range, then close back inside by 30m.
        hi30=float(np.max(H[i:i+30]));lo30=float(np.min(L[i:i+30]));c30=float(C[i+29])
        if hi30>pre_hi+1.0*av and c30<pre_hi:
            d=-1;entry=float(O[i+30])*(1-d*SLIP);stop=hi30+.1*av
        elif lo30<pre_lo-1.0*av and c30>pre_lo:
            d=1;entry=float(O[i+30])*(1+d*SLIP);stop=lo30-.1*av
        else:return None
        risk=d*(entry-stop)/entry
        if not (.002<=risk<=.04):return None
        target=entry*(1+d*1.25*risk)
        confirm=i+29

    j=confirm+1;last=min(len(raw)-1,j+360);reason="TIME"
    while j<=last:
        if (L[j]<=stop if d>0 else H[j]>=stop):
            q=(min(O[j],stop) if d>0 else max(O[j],stop))*(1-d*SLIP);reason="STOP";break
        if (H[j]>=target if d>0 else L[j]<=target):
            q=target*(1-d*SLIP);reason="TARGET";break
        j+=1
    if j>last:j=last;q=C[j]*(1-d*SLIP)
    net=d*(q/entry-1)-FEE-(FEE*q/entry)
    return {"opened":int(T[confirm+1]),"closed":int(T[j]),"d":d,"net":float(net),"reason":reason,"kind":event["kind"]}

def met(v):
    a=np.asarray([z["net"] for z in v],float)
    if not len(a):return {"n":0,"win_rate":None,"avg":None,"sum":0.0,"pf":0.0}
    p=a[a>0].sum();n=-a[a<0].sum()
    return {"n":len(a),"win_rate":float((a>0).mean()),"avg":float(a.mean()),"sum":float(a.sum()),"pf":float(p/n) if n else 99.}

def run_asset(asset,events):
    raw=load(asset)
    rows={}
    for mode in ("EVENT_BREAKOUT","EVENT_FADE"):
        yearly={}
        trades=[]
        for y in range(2022,2027):
            vv=[]
            for e in events:
                dt=datetime.fromtimestamp(e["ts"],timezone.utc)
                if dt.year!=y:continue
                t=event_trade(raw,e,mode)
                if t:vv.append(t);trades.append(t)
            yearly[str(y)]=met(vv)
        # This is an event study, not parameter search. Require 2022-25 aggregate
        # and at least 3 positive years before 2026 is considered usable.
        hist=[yearly[str(y)] for y in (2022,2023,2024,2025)]
        positive=sum((m["avg"] or -9)>0 for m in hist if m["n"]>=3)
        agg=met([t for t in trades if datetime.fromtimestamp(t["opened"],timezone.utc).year<=2025])
        validated=agg["n"]>=20 and (agg["avg"] or -9)>0 and agg["pf"]>=1.15 and positive>=3
        rows[mode]={"years":yearly,"pre2026":agg,"validated_pre2026":validated,
                    "production_policy":"EVENT_MODEL" if validated else "RISK_VETO_ONLY"}
    return rows

def main():
    ev=bls_events()
    out={"generated_at":datetime.now(timezone.utc).isoformat(),"event_count":len(ev),
         "sources":"Official BLS annual release calendars; CPI and Employment Situation.",
         "assets":{a:run_asset(a,ev) for a in ("BTC","ETH")}}
    (OUT/"result.json").write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False,default=jd))
    print("VERITAS_EVENT_DRIVEN="+json.dumps(out,separators=(",",":"),default=jd),flush=True)

if __name__=="__main__":main()
