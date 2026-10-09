"""Thin owner-approved lifecycle orchestration outside the frozen runtime."""
from __future__ import annotations
import json, math
import veritas_episode_profit_floor as VEF
import veritas_peer_invalidation as VPI

VERSION="OWNER_RUNTIME_V1"

def close_shared_invalidation(c,name,z,asset,quote,prices,ts,portfolio_rows,mark_nav,close_fn):
    peer=VPI.find(c,z)
    if not peer.get("active") or not quote:
        return False
    patch={"shared_canonical_setup_invalidation":peer}
    encoded=json.dumps(patch,ensure_ascii=False,default=str)
    c.execute("UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
              "WHERE portfolio_name=%s AND asset=%s AND active_trade_id=%s",
              (encoded,name,asset,z.get("active_trade_id")))
    if z.get("active_trade_id"):
        c.execute("UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s",
                  (encoded,z.get("active_trade_id")))
    p_now,pos_now=portfolio_rows(c,name)
    nav_now,_,_,_=mark_nav(p_now,pos_now,prices)
    return bool(close_fn(c,p_now,name,z,prices[asset],0.0,nav_now,ts,
                         "HARD_THESIS_INVALIDATION_SHARED_CANONICAL_SETUP"))

def cap_episode_add(c,existing,row,name,asset,direction,price,requested,nav,step,record_outcome):
    if not existing or str(existing.get("direction") or "")!=str(direction) or not VEF.protection_active(existing,{}):
        return requested,False
    tid=existing.get("active_trade_id")
    trade=(c.execute("SELECT * FROM paper_trades WHERE trade_id=%s",(tid,)).fetchone() if tid else None)
    floor=VEF.assess_add(existing,dict(trade or {}),price,requested,nav)
    if not floor.get("active"):
        return requested,False
    encoded=json.dumps({"episode_profit_floor_last_check":floor},ensure_ascii=False,default=str)
    c.execute("UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
              "WHERE portfolio_name=%s AND asset=%s",(encoded,name,asset))
    if tid:
        c.execute("UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s",
                  (encoded,tid))
    current=float(floor.get("current_fraction") or 0.0)
    allowed=max(0.0,float(floor.get("cap_fraction") or current)-current)
    stepped=current+math.floor(allowed/step+1e-9)*step
    if stepped<requested-1e-9:
        requested=max(current,stepped); row["_episode_profit_floor_cap"]=floor
    if requested<=current+0.0025:
        record_outcome(row,"BLOCKED","EPISODE_PROFIT_FLOOR_ADD_BLOCKED",
                       episode_profit_floor=floor,current_fraction=current)
        return requested,True
    return requested,False
