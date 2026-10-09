"""Compact immutable fields used by closed-trade review."""
from __future__ import annotations

CLOSED_EXTRA_FIELDS=(
    "initial_stop_price","exit_position_stop_price","exit_trailing_stop_price",
    "exit_effective_stop_price","exit_effective_stop_reason","add_count","add_fee_rub",
    "last_add_fee_rub","last_add_at","last_add_price","mfe_before_last_add_pct",
    "mfe_since_last_add_pct","mae_since_last_add_pct","initial_entry_price",
    "initial_entry_units","initial_entry_fee_rub","initial_tranche_final_exit_gross_rub",
    "initial_tranche_final_exit_net_proxy_rub","initial_tranche_counterfactual_basis",
)


def _num(value,default=0.0):
    try:
        value=float(value)
        return value if value==value and abs(value)!=float("inf") else default
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
    fee=max(0.0,_num(p.get("initial_entry_fee_rub")))
    fill=_num(fill_price); rate=max(0.0,_num(commission))
    if entry<=0 or units<=0 or fill<=0:
        return {}
    gross=float(sign)*units*(fill-entry)
    return {"initial_tranche_final_exit_gross_rub":gross,
            "initial_tranche_final_exit_net_proxy_rub":gross-fee-units*fill*rate,
            "initial_tranche_counterfactual_basis":"INITIAL_TRANCHE_HELD_TO_FINAL_EXIT_NO_ADDS_PROXY"}


def lifetime_mfe(payload):
    p=payload if isinstance(payload,dict) else {}
    for key in ("r55_lifetime_mfe_pct","r_accel_mfe_pct","mfe_pct"):
        if p.get(key) is not None:
            return p.get(key)
    return None


def closed_trade_fields(payload):
    p=payload if isinstance(payload,dict) else {}
    return {field:p.get(field) for field in CLOSED_EXTRA_FIELDS}
