"""Compact closed-trade review helpers; no market I/O and no trading authority."""
from __future__ import annotations
import math

CLOSED_EXTRA_FIELDS=(
    "initial_stop_price","exit_position_stop_price","exit_trailing_stop_price",
    "exit_effective_stop_price","exit_effective_stop_reason","add_count","add_fee_rub",
    "last_add_fee_rub","last_add_at","last_add_price","mfe_before_last_add_pct",
    "mfe_since_last_add_pct","mae_since_last_add_pct","initial_entry_price",
    "initial_entry_units","initial_entry_fee_rub","initial_tranche_final_exit_gross_rub",
    "initial_tranche_final_exit_net_proxy_rub","initial_tranche_counterfactual_basis",
    "final_exit_favorable_pct_points","final_exit_mfe_capture_ratio",
    "counterfactual_mfe_50_lock_pct_points","counterfactual_mfe_50_lock_price",
    "counterfactual_mfe_70_lock_pct_points","counterfactual_mfe_70_lock_price",
    "counterfactual_mfe_status",
)

def _num(value,default=0.0):
    try:
        value=float(value)
        return value if math.isfinite(value) else default
    except (TypeError,ValueError,OverflowError):
        return default

def exit_stop_patch(position,payload,reason,effective_stop):
    p=payload if isinstance(payload,dict) else {}
    return {"initial_stop_price":p.get("initial_stop_price"),
            "exit_position_stop_price":(position or {}).get("stop_price"),
            "exit_trailing_stop_price":p.get("trailing_stop"),
            "exit_effective_stop_price":effective_stop,
            "exit_effective_stop_reason":str(reason)}

def initial_tranche_counterfactual(payload,fill_price,sign,commission):
    p=payload if isinstance(payload,dict) else {}
    entry=_num(p.get("initial_entry_price")); units=abs(_num(p.get("initial_entry_units")))
    fee=max(0.0,_num(p.get("initial_entry_fee_rub"))); fill=_num(fill_price); rate=max(0.0,_num(commission))
    if entry<=0 or units<=0 or fill<=0:
        return {}
    gross=float(sign)*units*(fill-entry)
    return {"initial_tranche_final_exit_gross_rub":gross,
            "initial_tranche_final_exit_net_proxy_rub":gross-fee-units*fill*rate,
            "initial_tranche_counterfactual_basis":"INITIAL_TRANCHE_HELD_TO_FINAL_EXIT_NO_ADDS_PROXY"}


def mfe_capture_counterfactual(payload,fill_price,direction,avg_entry_price):
    """Bounded exit diagnostics; never claim an unobserved trigger occurred."""
    p=payload if isinstance(payload,dict) else {}
    mfe=_num(lifetime_mfe(p)); fill=_num(fill_price); entry=_num(avg_entry_price)
    if mfe<=0 or fill<=0 or entry<=0 or direction not in ("LONG","SHORT"):
        return {}
    favorable=100.0*((fill/entry-1.0) if direction=="LONG" else (entry/fill-1.0))
    ratio=favorable/mfe if mfe>1e-12 else None
    def lock(level):
        pct=mfe*level
        price=entry*(1.0+pct/100.0) if direction=="LONG" else entry/(1.0+pct/100.0)
        return pct,price
    p50,px50=lock(.50); p70,px70=lock(.70)
    return {
        "final_exit_favorable_pct_points":favorable,
        "final_exit_mfe_capture_ratio":ratio,
        "counterfactual_mfe_50_lock_pct_points":p50,
        "counterfactual_mfe_50_lock_price":px50,
        "counterfactual_mfe_70_lock_pct_points":p70,
        "counterfactual_mfe_70_lock_price":px70,
        "counterfactual_mfe_status":"DIAGNOSTIC_ONLY_TRIGGER_NOT_PROVEN",
    }

def lifetime_mfe(payload):
    p=payload if isinstance(payload,dict) else {}
    for key in ("r55_lifetime_mfe_pct","r_accel_mfe_pct","mfe_pct"):
        if p.get(key) is not None:
            return p.get(key)
    return None

def closed_trade_fields(payload):
    p=payload if isinstance(payload,dict) else {}
    return {field:p.get(field) for field in CLOSED_EXTRA_FIELDS}
