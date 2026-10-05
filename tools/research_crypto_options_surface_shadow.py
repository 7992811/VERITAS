"""Current Deribit options-surface shadow context for BTC and ETH.

This is not historical validation. It provides live/shadow context that cannot be
retroactively substituted for missing historical skew/smile data.
"""
from __future__ import annotations
import json, re, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import numpy as np
import pandas as pd

OUT=Path("options_surface_shadow_out"); OUT.mkdir(exist_ok=True)

def get(method,params):
    url="https://www.deribit.com/api/v2/public/"+method+"?"+urlencode(params)
    last=None
    for k in range(4):
        try:
            with urlopen(Request(url,headers={"User-Agent":"VERITAS-options-shadow/1.0"}),timeout=30) as r:
                x=json.loads(r.read().decode("utf-8"))
            return x.get("result")
        except Exception as e:
            last=e;time.sleep(1+k)
    raise RuntimeError(last)

def parse_inst(name):
    # BTC-30OCT26-100000-C
    p=name.split("-")
    if len(p)<4:return None
    try:
        exp=pd.to_datetime(p[1],format="%d%b%y",utc=True)
        strike=float(p[2]); cp=p[3]
        return exp,strike,cp
    except Exception:return None

def asset_surface(asset):
    rows=get("get_book_summary_by_currency",{"currency":asset,"kind":"option"}) or []
    now=pd.Timestamp.now(tz="UTC")
    data=[]
    for r in rows:
        p=parse_inst(str(r.get("instrument_name","")))
        if not p:continue
        exp,strike,cp=p
        days=(exp-now).total_seconds()/86400
        iv=r.get("mark_iv");oi=r.get("open_interest");under=r.get("underlying_price")
        try:iv=float(iv);oi=float(oi or 0);under=float(under)
        except Exception:continue
        if not np.isfinite(iv) or not np.isfinite(under) or under<=0:continue
        data.append({"name":r["instrument_name"],"expiry":str(exp.date()),"days":days,"strike":strike,
                     "cp":cp,"iv":iv,"oi":oi,"under":under,"moneyness":strike/under})
    df=pd.DataFrame(data)
    if df.empty:return {"status":"NO_DATA"}
    # near expiry 7-45d and medium 46-120d
    near=df[(df.days>=7)&(df.days<=45)].copy()
    med=df[(df.days>45)&(df.days<=120)].copy()
    def atm(z):
        if z.empty:return None
        q=z.iloc[(z.moneyness-1).abs().argsort()[:max(2,min(8,len(z)))]]
        return float(np.average(q.iv,weights=np.maximum(q.oi,1)))
    def wing(z,cp,m):
        q=z[z.cp==cp].copy()
        if q.empty:return None
        q=q.iloc[(q.moneyness-m).abs().argsort()[:max(1,min(4,len(q)))]]
        return float(np.average(q.iv,weights=np.maximum(q.oi,1)))
    atm_near=atm(near);atm_med=atm(med)
    put90=wing(near,"P",.90);call110=wing(near,"C",1.10)
    put_oi=float(near.loc[near.cp=="P","oi"].sum()) if not near.empty else 0.
    call_oi=float(near.loc[near.cp=="C","oi"].sum()) if not near.empty else 0.
    return {
        "status":"OK","underlying":float(df.under.median()),"n_options":int(len(df)),
        "near_atm_iv":atm_near,"medium_atm_iv":atm_med,
        "term_slope_iv":(atm_med-atm_near) if atm_med is not None and atm_near is not None else None,
        "put90_iv":put90,"call110_iv":call110,
        "downside_skew_proxy":(put90-call110) if put90 is not None and call110 is not None else None,
        "near_put_call_oi":(put_oi/call_oi) if call_oi>0 else None,
        "note":"Skew proxy uses fixed moneyness, not 25-delta; shadow context only."
    }

def main():
    out={"generated_at":datetime.now(timezone.utc).isoformat(),
         "mode":"shadow_live_only","assets":{}}
    for a in ("BTC","ETH"):
        try:out["assets"][a]=asset_surface(a)
        except Exception as e:out["assets"][a]={"status":"ERROR","error":str(e)}
    (OUT/"result.json").write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False))
    print("VERITAS_OPTIONS_SURFACE_SHADOW="+json.dumps(out,separators=(",",":")),flush=True)

if __name__=="__main__":main()
