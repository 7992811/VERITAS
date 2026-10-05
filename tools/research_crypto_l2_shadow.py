"""VERITAS live L2 microstructure shadow snapshot.

Current-only context. It is intentionally NOT backfilled into historical tests.
Historical Binance bookDepth coverage/quality is uneven, so production promotion
requires forward-collected stable history.

Captures:
- top-of-book spread
- depth-weighted midpoint
- 5/10/20 level quantity and notional imbalance
- microprice proxy
"""
from __future__ import annotations
import json,time
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request,urlopen
import numpy as np

OUT=Path("l2_shadow_out");OUT.mkdir(exist_ok=True)

def get(symbol,limit=20):
    url="https://fapi.binance.com/fapi/v1/depth?"+urlencode({"symbol":symbol,"limit":limit})
    last=None
    for k in range(4):
        try:
            with urlopen(Request(url,headers={"User-Agent":"VERITAS-L2-shadow/1.0"}),timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last=e;time.sleep(1+k)
    raise RuntimeError(last)

def metrics(symbol):
    x=get(symbol,20)
    bids=np.asarray([[float(p),float(q)] for p,q in x.get("bids",[])],float)
    asks=np.asarray([[float(p),float(q)] for p,q in x.get("asks",[])],float)
    if len(bids)<5 or len(asks)<5:return {"status":"INSUFFICIENT_DEPTH"}
    bb=bids[0,0];ba=asks[0,0];mid=(bb+ba)/2
    bq=bids[0,1];aq=asks[0,1]
    micro=(ba*bq+bb*aq)/(bq+aq) if bq+aq>0 else mid
    out={"status":"OK","best_bid":bb,"best_ask":ba,"mid":mid,
         "spread_bps":float((ba-bb)/mid*10000),"microprice":float(micro),
         "microprice_edge_bps":float((micro-mid)/mid*10000)}
    for n in (5,10,20):
        b=bids[:n];a=asks[:n]
        bqty=float(b[:,1].sum());aqty=float(a[:,1].sum())
        bnot=float((b[:,0]*b[:,1]).sum());anot=float((a[:,0]*a[:,1]).sum())
        out[f"qty_imbalance_{n}"]=float((bqty-aqty)/(bqty+aqty)) if bqty+aqty else 0.
        out[f"notional_imbalance_{n}"]=float((bnot-anot)/(bnot+anot)) if bnot+anot else 0.
    return out

def main():
    out={"generated_at":datetime.now(timezone.utc).isoformat(),"mode":"shadow_live_only",
         "assets":{},"note":"No historical backfill; requires forward-collected evidence before admission."}
    for a,s in (("BTC","BTCUSDT"),("ETH","ETHUSDT")):
        try:out["assets"][a]=metrics(s)
        except Exception as e:out["assets"][a]={"status":"ERROR","error":str(e)}
    (OUT/"result.json").write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False))
    print("VERITAS_L2_SHADOW="+json.dumps(out,separators=(",",":")),flush=True)

if __name__=="__main__":main()
