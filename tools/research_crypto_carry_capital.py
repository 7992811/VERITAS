"""VERITAS carry capital-efficiency and margin-buffer study.

Research only. Revalues the frozen STRONG_21D carry policy under different
futures-collateral allocations.

Economic setup:
- Spot notional = 1.0 N.
- Perpetual short initial notional = 1.0 N.
- The strategy P&L from adaptive_carry is expressed on total capital 2.0 N.
- If isolated futures collateral is increased to C*N, total deployed/reserved
  capital becomes (1+C)*N and return is rescaled accordingly.

This does not emulate an exchange liquidation engine. It reports the historical
short-leg adverse-rally proxy and whether a collateral scenario would have
covered that proxy plus a fixed safety buffer.

Portfolio-margin mode is reported separately and assumes the venue actually
recognizes the spot/futures hedge as collateral; that operational assumption
must be verified on the user's account before live enablement.
"""
from __future__ import annotations
import json, math
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

import research_crypto_adaptive_carry as ac
import research_crypto_carry_operational_stress as ops

OUT=Path("carry_capital_out");OUT.mkdir(exist_ok=True)
POL=next(p for p in ac.POLICIES if p["name"]=="STRONG_21D")
COLLATERAL=(1.0,1.25,1.50,1.75,2.0)
SAFETY_BUFFER=.15  # 15% of short notional over observed adverse-rally proxy

def metrics(vals):
 a=np.asarray(vals,float)
 if not len(a):return {"n":0,"win_rate":None,"avg":None,"sum":0.,"pf":0.,"dd":0.,"compounded":0.}
 p=a[a>0].sum();n=-a[a<0].sum();eq=np.cumsum(a);pk=np.maximum.accumulate(np.r_[0.,eq])[1:]
 return {"n":len(a),"win_rate":float((a>0).mean()),"avg":float(a.mean()),"sum":float(a.sum()),
         "pf":float(p/n) if n else 99.,"dd":float((pk-eq).max()),"compounded":float(np.prod(1+a)-1)}

def cagr(compounded,years=6.76):
 return float((1+compounded)**(1/years)-1) if compounded>-1 else -1.

def run(asset):
 z,s,p=ac.prep(asset);tr=ac.simulate(z,s,p,POL,0.0)
 raw=ops.combine(asset)
 adverse=[ops.short_adverse_proxy(raw,r["opened"],r["closed"]) for r in tr]
 adverse=[x for x in adverse if x is not None and np.isfinite(x)]
 mx=max(adverse) if adverse else None
 p95=float(np.quantile(adverse,.95)) if adverse else None
 out={"asset":asset,"observed_short_adverse":{"max":mx,"p95":p95},
      "isolated":{},"portfolio_margin":{}}
 for coll in COLLATERAL:
  # adaptive carry net is return on 2N capital. Convert back to P&L/N and
  # divide by actual capital (1+coll)N.
  vals=[float(r["net"])*2.0/(1.0+coll) for r in tr]
  m=metrics(vals);m["cagr_approx"]=cagr(m["compounded"])
  required=(mx+SAFETY_BUFFER) if mx is not None else None
  out["isolated"][f"{coll:.2f}x_short_collateral"]={
    "short_collateral_over_notional":coll,
    "total_capital_per_spot_notional":1+coll,
    "historical_proxy_plus_15pct_buffer":required,
    "buffer_pass":bool(required is not None and coll>=required),
    "metrics":m,
  }
 # Portfolio-margin accounting keeps the original 2N capital denominator.
 pm=metrics([float(r["net"]) for r in tr]);pm["cagr_approx"]=cagr(pm["compounded"])
 out["portfolio_margin"]={
   "assumed_total_capital_per_spot_notional":2.0,
   "metrics":pm,
   "live_requirement":"Account-level hedge collateral recognition must be verified; internal uniMMR floor must exceed exchange liquidation floor by a large buffer."
 }
 return out

def main():
 out={"generated_at":datetime.now(timezone.utc).isoformat(),
      "method":"Frozen STRONG_21D P&L rescaled for futures collateral; historical spot-rally short-risk proxy.",
      "assets":{a:run(a) for a in ("BTC","ETH")}}
 (OUT/"result.json").write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False))
 print("VERITAS_CARRY_CAPITAL="+json.dumps(out["assets"],separators=(",",":")),flush=True)

if __name__=="__main__":main()
