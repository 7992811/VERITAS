"""VERITAS live shadow signal for frozen market-neutral carry candidate.

NO ORDER ROUTING. This script cannot place trades.

Candidate:
  STRONG_21D
  long spot + short USD-M perpetual, equal notionals

Data source: Binance public REST only.
Fail closed on stale/missing/inconsistent data.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

from research_crypto_manager_library import FEE, SLIP

OUT = Path("carry_shadow_signal_out")
OUT.mkdir(exist_ok=True)

PAIR_RT = 4 * (FEE + SLIP)
POL = {
    "name":"STRONG_21D",
    "forecast_events":63,
    "cost_multiple":1.50,
    "min_positive_frac":.78,
    "min_basis":-.0005,
    "max_events":126,
}
SYMS={"BTC":"BTCUSDT","ETH":"ETHUSDT"}

def get_json(url, timeout=15):
    req=Request(url,headers={"User-Agent":"VERITAS-carry-shadow/1.0"})
    with urlopen(req,timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def funding(symbol):
    q=urlencode({"symbol":symbol,"limit":1000})
    x=get_json("https://fapi.binance.com/fapi/v1/fundingRate?"+q)
    rows=[]
    for r in x:
        try:
            rows.append((int(r["fundingTime"])//1000,float(r["fundingRate"])))
        except Exception:
            continue
    z=pd.DataFrame(rows,columns=["ts","rate"]).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    return z

def book(url,symbol):
    q=urlencode({"symbol":symbol})
    x=get_json(url+"?"+q)
    return {
        "bid":float(x["bidPrice"]),
        "ask":float(x["askPrice"]),
        "bid_qty":float(x.get("bidQty",0)),
        "ask_qty":float(x.get("askQty",0)),
    }

def state(asset):
    symbol=SYMS[asset]
    f=funding(symbol)
    if len(f)<30:
        return {"asset":asset,"decision":"DATA_FAIL","reason":"INSUFFICIENT_FUNDING_HISTORY"}
    # Only completed funding settlements are present.
    f["mean3"]=f.rate.rolling(3).mean().shift(1)
    f["mean9"]=f.rate.rolling(9).mean().shift(1)
    f["pos9"]=f.rate.gt(0).rolling(9).mean().shift(1)
    f["pos21"]=f.rate.gt(0).rolling(21).mean().shift(1)
    f["fund_vol"]=f.rate.rolling(21,min_periods=9).std().shift(1)
    r=f.iloc[-1]
    vals=(r.mean3,r.mean9,r.pos9,r.pos21,r.fund_vol,r.rate)
    if not all(np.isfinite(v) for v in vals):
        return {"asset":asset,"decision":"DATA_FAIL","reason":"BAD_FUNDING_FEATURES"}

    spot=book("https://api.binance.com/api/v3/ticker/bookTicker",symbol)
    perp=book("https://fapi.binance.com/fapi/v1/ticker/bookTicker",symbol)
    now=int(time.time())
    age=now-int(r.ts)
    # Funding should normally be no older than one settlement interval plus buffer.
    if age>9*3600:
        return {"asset":asset,"decision":"DATA_FAIL","reason":"STALE_FUNDING","funding_age_sec":age}

    # Executable entry: buy spot at ask, short perpetual at bid.
    basis_exec=perp["bid"]/spot["ask"]-1.0
    forecast=.45*r.mean3+.35*r.mean9+.20*r.rate
    lcb=forecast-.50*r.fund_vol
    positive_frac=.55*r.pos9+.45*r.pos21
    expected=max(0.,float(lcb))*POL["forecast_events"]
    threshold=POL["cost_multiple"]*PAIR_RT

    checks={
        "expected_carry_gt_cost_buffer":bool(expected>=threshold),
        "positive_fraction":bool(positive_frac>=POL["min_positive_frac"]),
        "basis":bool(basis_exec>=POL["min_basis"]),
        "mean3_positive":bool(r.mean3>0),
        "mean9_positive":bool(r.mean9>0),
        "latest_rate_positive":bool(r.rate>0),
    }
    decision="ENTER_CARRY" if all(checks.values()) else "NO_TRADE"
    return {
        "asset":asset,"decision":decision,
        "timestamp":datetime.now(timezone.utc).isoformat(),
        "funding_ts":datetime.fromtimestamp(int(r.ts),timezone.utc).isoformat(),
        "funding_age_sec":age,
        "spot_bid":spot["bid"],"spot_ask":spot["ask"],
        "perp_bid":perp["bid"],"perp_ask":perp["ask"],
        "executable_basis":basis_exec,
        "rate":float(r.rate),"mean3":float(r.mean3),"mean9":float(r.mean9),
        "positive_frac":float(positive_frac),"forecast_rate":float(forecast),
        "forecast_lcb":float(lcb),"expected_21d_carry":float(expected),
        "required_carry_buffer":float(threshold),
        "checks":checks,
        "order_routing_enabled":False,
    }

def main():
    out={"generated_at":datetime.now(timezone.utc).isoformat(),
         "mode":"SHADOW_ONLY","order_routing_enabled":False,
         "policy":POL,"assets":{}}
    for a in ("BTC","ETH"):
        try:
            out["assets"][a]=state(a)
        except Exception as e:
            out["assets"][a]={"asset":a,"decision":"DATA_FAIL","reason":str(e),"order_routing_enabled":False}
    (OUT/"result.json").write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False))
    print("VERITAS_CARRY_SHADOW="+json.dumps(out,separators=(",",":")),flush=True)

if __name__=="__main__":
    main()
