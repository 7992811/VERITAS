"""Bounded execution-efficiency rules for paper adds.

This module is intentionally read-free: it consumes the already locked position,
current quote economics and owner policy so latency-sensitive add admission does
not introduce another database round trip.
"""
from __future__ import annotations
import math


def _num(value, default=0.0):
    try:
        value=float(value)
        return value if math.isfinite(value) else default
    except (TypeError,ValueError,OverflowError):
        return default


def _payload(row):
    value=(row or {}).get("payload") or {}
    return dict(value) if isinstance(value,dict) else {}


def add_precheck(existing,row,price,direction,commission,policy):
    p=_payload(existing)
    if (policy.get("block_add_on_fast_opposite_confirmation")
            and bool((row or {}).get("_ctc_fast_opposite_confirmed"))):
        return {"eligible":False,"reason":"ADD_FAST_OPPOSITE_STRUCTURE_CONFIRMED",
                "details":{"fast_opposite_confirmed":True}}
    units=abs(_num((existing or {}).get("units")))
    avg=_num((existing or {}).get("avg_entry_price"),_num(price))
    px=_num(price)
    unrealized=max(0.0,(1.0 if direction=="LONG" else -1.0)*units*(px-avg))
    # Churn means repeated adjustments. The first entry fee is already included
    # in the incremental post-cost economics gate and must not veto the first add.
    fees=max(0.0,_num(p.get("add_fee_rub")))
    limit=max(0.0,_num(policy.get("max_fee_to_positive_gross_edge"),.25))
    ratio=fees/unrealized if unrealized>1e-9 else (float("inf") if fees>0 else 0.0)
    before={"units":units,"avg_entry_price":avg,"fees_rub":fees,
            "mfe_pct":max(_num(p.get("r55_lifetime_mfe_pct")),
                          _num(p.get("mfe_pct")),_num(p.get("r_accel_mfe_pct")))}
    if ratio>=limit:
        return {"eligible":False,"reason":"ADD_CHURN_COST_LIMIT","before":before,
                "details":{"fees_rub":fees,"positive_gross_edge_rub":unrealized,
                           "fee_to_positive_gross_edge":ratio,"fee_limit":limit}}
    return {"eligible":True,"reason":"ADD_EFFICIENCY_PRECHECK_OK","before":before}


def incremental_gate(economics,policy):
    if not policy.get("require_positive_incremental_net_reward"):
        return {"eligible":True,"reason":"INCREMENTAL_NET_CHECK_DISABLED"}
    value=_num((economics or {}).get("net_reward_pct"),float("nan"))
    return {"eligible":bool(math.isfinite(value) and value>0),
            "reason":"ADD_INCREMENTAL_NET_EDGE_POSITIVE" if math.isfinite(value) and value>0
                     else "ADD_INCREMENTAL_NET_EDGE_NOT_POSITIVE",
            "net_reward_pct":value}


def entry_analysis_patch(opened,is_new,before,price,ts,commission):
    opened=opened or {}; before=before or {}; p=_payload(opened)
    after_units=abs(_num(opened.get("units")))
    after_avg=_num(opened.get("avg_entry_price"),_num(price))
    fee_rate=max(0.0,_num(commission))
    if is_new:
        return {"initial_entry_price":after_avg,"initial_entry_units":after_units,
                "initial_entry_fee_rub":after_units*after_avg*fee_rate,
                "add_count":0,"add_fee_rub":0.0,
                "mfe_since_last_add_pct":0.0,"mae_since_last_add_pct":0.0}
    before_units=max(0.0,_num(before.get("units")))
    before_avg=_num(before.get("avg_entry_price"),_num(price))
    added=max(0.0,after_units-before_units)
    if added<=1e-12:
        return {}
    fill=(after_units*after_avg-before_units*before_avg)/added
    if not math.isfinite(fill) or fill<=0:
        fill=_num(price)
    fee=added*fill*fee_rate
    return {"add_count":int(_num(p.get("add_count")))+1,
            "add_fee_rub":max(0.0,_num(p.get("add_fee_rub")))+fee,
            "last_add_fee_rub":fee,"last_add_at":str(ts),"last_add_price":fill,
            "pre_add_units":before_units,"post_add_units":after_units,
            "pre_add_avg_entry_price":before_avg,"post_add_avg_entry_price":after_avg,
            "mfe_before_last_add_pct":max(0.0,_num(before.get("mfe_pct"))),
            "mfe_since_last_add_pct":0.0,"mae_since_last_add_pct":0.0}
