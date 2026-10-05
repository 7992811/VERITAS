"""External 2020-2021 control for frozen specialist modules.

No parameter search and no use of 2020-2021 in selection.

Frozen from pre-2026 research:
- BTC SHORT: H1/H4 dual-pullback, QUALITY20, PART_3R, 24h max hold.
- ETH LONG: TIGHT_C, fixed 2.5R.
- ETH SHORT: WIDER_C + BTC 4h confirmation, fixed 2R.

The purpose is not to rescue a rule by retuning it. Any failure is reported as-is.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

from research_crypto_external_holdout import load_old
from research_crypto_manager_library import features, ts, met
import research_crypto_multihorizon_trend as mh
import research_eth_long_neighborhood as el
import research_eth_short_neighborhood as es

OUT=Path("specialist_external_out");OUT.mkdir(exist_ok=True)

def yr(trades,y):
    a=ts(f"{y}-01-01T00:00:00Z");b=ts(f"{y+1}-01-01T00:00:00Z")
    return met([t for t in trades if a<=t["opened"]<b])

def years(trades):
    return {str(y):yr(trades,y) for y in (2020,2021)}

def btc_short(raw,stress):
    x=mh.add_horizon_features(raw,features(raw))
    prof=next(p for p in mh.PROFILES if p[0]=="QUALITY20")
    ev=mh.make_events(x,-1,"H1H4","BOTH",prof)
    p=dict(stop_h=6,buf=.10,mode="PART_3R",maxhold=1440)
    tr=mh.sim(x,ev,start="2020-01-01",end="2022-01-01",stress=stress,**p)
    return ev,tr

def eth_long(raw,stress):
    x=features(raw)
    prof=next(p for p in el.ENTRY if p[0]=="TIGHT_C")
    ex=next(e for e in el.EXITS if e[0]=="FIXED_2_5R")
    ev=el.events(x,prof)
    tr=el.simulate(x,ev,start=ts("2020-01-01T00:00:00Z"),end=ts("2022-01-01T00:00:00Z"),
                   stress=stress,**el.pars(ex))
    return ev,tr

def eth_short(eth,btc,stress):
    x=features(eth)
    btc60,btc240=es.btc_context(btc,x.ts.to_numpy())
    prof=next(p for p in es.ENTRY_PROFILES if p[0]=="WIDER_C")
    ex=next(e for e in es.EXITS if e[0]=="FIXED_2R")
    ev=es.make_events(x,prof,btc60,btc240,"BTC4H")
    tr=es.simulate(x,ev,start=ts("2020-01-01T00:00:00Z"),end=ts("2022-01-01T00:00:00Z"),
                   stress=stress,**es.pars(ex))
    return ev,tr

def score(base,s5):
    # External-control verdict only; no selection.
    represented=sum(base[str(y)]["n"]>=3 for y in (2020,2021))
    positive=sum((base[str(y)]["avg"] or -9)>0 for y in (2020,2021) if base[str(y)]["n"]>=3)
    spos=sum((s5[str(y)]["avg"] or -9)>0 for y in (2020,2021) if s5[str(y)]["n"]>=3)
    return "PASS" if represented==2 and positive==2 and spos==2 else "FAIL"

def main():
    btc=load_old("BTC");eth=load_old("ETH")
    specs={
      "BTC_SHORT_H1H4":lambda st:btc_short(btc,st),
      "ETH_LONG_TIGHT_C":lambda st:eth_long(eth,st),
      "ETH_SHORT_WIDER_C_BTC4H":lambda st:eth_short(eth,btc,st),
    }
    out={"generated_at":datetime.now(timezone.utc).isoformat(),
         "method":"Frozen specialist rules; external 2020-2021 control; no retuning.",
         "modules":{}}
    for name,fn in specs.items():
        ev,b=fn(0.0);_,s5=fn(.0005);_,s10=fn(.0010)
        base=years(b);st5=years(s5);st10=years(s10)
        out["modules"][name]={"n_events":len(ev),"base":base,"stress5":st5,"stress10":st10,
                              "external_verdict":score(base,st5)}
        print(name,"SPECIALIST_EXTERNAL",json.dumps(out["modules"][name],separators=(",",":")),flush=True)
    (OUT/"result.json").write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False))
    print("VERITAS_SPECIALIST_EXTERNAL="+json.dumps(out["modules"],separators=(",",":")),flush=True)

if __name__=="__main__":main()
