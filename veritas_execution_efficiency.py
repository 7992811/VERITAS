"""Bounded owner-approved efficiency checks for canonical paper adds."""
from __future__ import annotations
import json
import math


def _num(value,default=0.0):
    try:
        value=float(value)
        return value if math.isfinite(value) else default
    except (TypeError,ValueError,OverflowError):
        return default


def _payload(row):
    value=(row or {}).get("payload") or {}
    if isinstance(value,dict):
        return dict(value)
    try:
        return json.loads(value)
    except Exception:
        return {}


def add_precheck(existing,row,price,direction,commission,policy):
    p=_payload(existing)
    guard=(((row or {}).get("trade_plan") or {}).get("ctc_open_position_thesis_guard") or {})
    fast=(guard.get("fast_reversal") or {}).get("eligible") is True
    if policy.get("block_add_on_fast_opposite_confirmation") and fast:
        return {"eligible":False,"reason":"ADD_FAST_OPPOSITE_STRUCTURE_CONFIRMED"}
    units=abs(_num((existing or {}).get("units")))
    avg=_num((existing or {}).get("avg_entry_price"),_num(price))
    px=_num(price)
    before={"units":units,"avg_entry_price":avg,
            "mfe_pct":max(_num(p.get("r55_lifetime_mfe_pct")),
                          _num(p.get("r_accel_mfe_pct")),_num(p.get("mfe_pct")))}
    add_count=max(0,int(_num(p.get("add_count"))))
    add_fees=max(0.0,_num(p.get("add_fee_rub")))
    if add_count<1 or add_fees<=0:
        return {"eligible":True,"reason":"FIRST_ADD_OR_NO_CHURN_HISTORY","before":before}
    positive=max(0.0,(1.0 if direction=="LONG" else -1.0)*units*(px-avg))
    limit=max(0.0,_num(policy.get("max_repeated_add_fee_to_positive_edge"),.25))
    ratio=add_fees/positive if positive>1e-9 else float("inf")
    if ratio>=limit:
        return {"eligible":False,"reason":"ADD_CHURN_COST_LIMIT","before":before,
                "fee_to_positive_edge":ratio,"fee_limit":limit}
    return {"eligible":True,"reason":"ADD_CHURN_OK","before":before}


def incremental_gate(economics,policy):
    if not policy.get("require_positive_incremental_net_reward"):
        return {"eligible":True,"reason":"INCREMENTAL_NET_CHECK_DISABLED"}
    reward=_num((economics or {}).get("net_reward_pct"),float("nan"))
    ok=math.isfinite(reward) and reward>0
    return {"eligible":ok,"reason":"ADD_INCREMENTAL_NET_EDGE_POSITIVE" if ok
            else "ADD_INCREMENTAL_NET_EDGE_NOT_POSITIVE","net_reward_pct":reward}


def entry_analysis_patch(opened,is_new,before,price,ts,commission):
    opened=opened or {}; before=before or {}; p=_payload(opened)
    units=abs(_num(opened.get("units"))); avg=_num(opened.get("avg_entry_price"),_num(price))
    rate=max(0.0,_num(commission))
    if is_new:
        return {"initial_entry_price":avg,"initial_entry_units":units,
                "initial_entry_fee_rub":units*avg*rate,"add_count":0,"add_fee_rub":0.0,
                "mfe_since_last_add_pct":0.0,"mae_since_last_add_pct":0.0}
    prior_units=max(0.0,_num(before.get("units"))); prior_avg=_num(before.get("avg_entry_price"),_num(price))
    added=max(0.0,units-prior_units)
    if added<=1e-12:
        return {}
    fill=(units*avg-prior_units*prior_avg)/added
    if not math.isfinite(fill) or fill<=0:
        fill=_num(price)
    fee=added*fill*rate
    return {"add_count":max(0,int(_num(p.get("add_count"))))+1,
            "add_fee_rub":max(0.0,_num(p.get("add_fee_rub")))+fee,
            "last_add_fee_rub":fee,"last_add_at":str(ts),"last_add_price":fill,
            "pre_add_units":prior_units,"post_add_units":units,
            "pre_add_avg_entry_price":prior_avg,"post_add_avg_entry_price":avg,
            "mfe_before_last_add_pct":max(0.0,_num(before.get("mfe_pct"))),
            "mfe_since_last_add_pct":0.0,"mae_since_last_add_pct":0.0}
