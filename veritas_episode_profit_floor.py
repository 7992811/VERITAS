"""Episode-level profit floor for adds after earned profit.

Once an episode has realized a partial profit or armed sustained-profit
protection, a new ADD may consume only the profit buffer that remains at the
currently active stop after all known costs. It may never turn the projected
whole-cycle result at that stop below economic breakeven.
"""
from __future__ import annotations
import json, math

import veritas_costs as VC
import veritas_profit_protection as VPP

VERSION="EPISODE_PROFIT_FLOOR_V2_NATIVE_ACCEL"

def _num(v):
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except (TypeError,ValueError):
        return None

def _payload(position):
    v=(position or {}).get("payload") or {}
    try:
        return json.loads(v) if isinstance(v,str) else dict(v)
    except (TypeError,ValueError):
        return {}

def protection_active(position,trade=None):
    p=_payload(position); t=trade or {}
    stage=p.get("active_target_stage")
    realized=_num(t.get("gross_pnl_rub"))
    return bool(
        p.get("r17_tp1_done")
        or (isinstance(stage,int) and not isinstance(stage,bool) and stage>0)
        or p.get("r_accel_mfe_profit_lock_active")
        or p.get("r55_net_profit_lock_active")
        or p.get("profit_protection_active")
        or (realized is not None and realized>0)
    )

def assess_add(position,trade,price,requested_fraction,nav,
               commission=VC.COMMISSION_RATE,slippage=VC.SLIPPAGE_RATE):
    """Return the maximum total position fraction preserving whole-cycle net >= 0."""
    z=dict(position or {}); t=dict(trade or {})
    out={"version":VERSION,"active":False,"eligible":True,
         "reason":"EPISODE_PROFIT_FLOOR_NOT_ACTIVE"}
    if not protection_active(z,t):
        return out
    out["active"]=True
    direction=str(z.get("direction") or "")
    units=_num(z.get("units")); avg=_num(z.get("avg_entry_price"))
    mark=_num(price); capital=_num(nav); requested=_num(requested_fraction)
    stop=_num(VPP.effective_stop(z))
    realized=_num(t.get("gross_pnl_rub"))
    fees=_num(t.get("fees_rub")); funding=_num(t.get("funding_rub"))
    if (direction not in ("LONG","SHORT") or any(v is None for v in
        (units,avg,mark,capital,requested,stop,realized,fees,funding))
        or units<=0 or avg<=0 or mark<=0 or capital<=0 or stop<=0
        or fees<0 or funding<0):
        return {**out,"eligible":False,"reason":"EPISODE_PROFIT_FLOOR_ACCOUNTING_REQUIRED"}

    sign=1.0 if direction=="LONG" else -1.0
    if sign*(mark-stop)<=0:
        return {**out,"eligible":False,"reason":"EPISODE_PROFIT_FLOOR_STOP_NOT_LIVE"}

    current_fraction=units*mark/capital
    requested=max(current_fraction,requested)
    entry_fill=mark*(1.0+slippage if direction=="LONG" else 1.0-slippage)
    stop_fill=stop*(1.0-slippage if direction=="LONG" else 1.0+slippage)
    if entry_fill<=0 or stop_fill<=0:
        return {**out,"eligible":False,"reason":"EPISODE_PROFIT_FLOOR_FILL_INVALID"}

    existing_remaining_gross=sign*units*(stop_fill-avg)
    existing_exit_fee=units*stop_fill*commission
    base_net=realized+existing_remaining_gross-fees-funding-existing_exit_fee

    marginal_gross=sign*(stop_fill-entry_fill)
    marginal_net=marginal_gross-entry_fill*commission-stop_fill*commission
    add_requested_notional=max(0.0,(requested-current_fraction)*capital)
    add_requested_units=add_requested_notional/entry_fill
    projected_requested_net=base_net+add_requested_units*marginal_net

    if marginal_net>=0:
        cap_fraction=requested
    elif base_net<=0:
        cap_fraction=current_fraction
    else:
        max_add_units=base_net/(-marginal_net)
        cap_fraction=current_fraction+max_add_units*entry_fill/capital

    cap_fraction=max(current_fraction,cap_fraction)
    eligible=bool(projected_requested_net>=-1e-9)
    return {
        **out,"eligible":eligible,
        "reason":"EPISODE_PROFIT_FLOOR_OK" if eligible else "EPISODE_PROFIT_FLOOR_CAP_REQUIRED",
        "current_fraction":current_fraction,
        "requested_fraction":requested,
        "cap_fraction":cap_fraction,
        "realized_gross_pnl_rub":realized,
        "paid_fees_rub":fees,
        "booked_funding_rub":funding,
        "effective_stop_price":stop,
        "modeled_stop_fill":stop_fill,
        "modeled_add_fill":entry_fill,
        "existing_cycle_net_at_stop_rub":base_net,
        "marginal_add_net_at_stop_rub_per_unit":marginal_net,
        "projected_requested_cycle_net_at_stop_rub":projected_requested_net,
        "floor_rub":0.0,
    }
