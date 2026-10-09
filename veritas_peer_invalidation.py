"""Propagate a proved hard thesis invalidation across copies of one canonical setup."""
from __future__ import annotations
import json

VERSION="CANONICAL_SETUP_SHARED_INVALIDATION_V1"

def _payload(z):
    v=(z or {}).get("payload") or {}
    try:return json.loads(v) if isinstance(v,str) else dict(v)
    except (TypeError,ValueError):return {}

def setup_key(z):
    p=_payload(z)
    return p.get("canonical_setup_id") or p.get("setup_id") or p.get("r66_event_id") or p.get("entry_event_id")

def find(c, position):
    z=dict(position or {}); key=setup_key(z)
    out={"version":VERSION,"active":False,"reason":"NO_SHARED_HARD_INVALIDATION","setup_key":key}
    if not key or not z.get("active_trade_id"):
        return out
    row=c.execute("""
      SELECT t.trade_id,t.portfolio_name,t.closed_at,
             COALESCE(t.payload->>'exit_reason',
               (SELECT o.reason FROM paper_orders o WHERE o.trade_id=t.trade_id
                ORDER BY o.created_at DESC LIMIT 1)) AS exit_reason
      FROM paper_trades t
      WHERE t.trade_id<>%s
        AND t.asset=%s AND t.direction=%s
        AND t.closed_at IS NOT NULL
        AND t.closed_at>=COALESCE(%s::timestamptz,t.opened_at)
        AND COALESCE(t.payload->>'canonical_setup_id',t.payload->>'setup_id',
                     t.payload->>'r66_event_id',t.payload->>'entry_event_id')=%s
      ORDER BY t.closed_at DESC LIMIT 8
    """,(z.get("active_trade_id"),z.get("asset"),z.get("direction"),z.get("opened_at"),str(key))).fetchall()
    for r0 in row or []:
        r=dict(r0); reason=str(r.get("exit_reason") or "")
        if reason.startswith("HARD_THESIS_INVALIDATION"):
            return {"version":VERSION,"active":True,
                    "reason":"HARD_THESIS_INVALIDATION_SHARED_CANONICAL_SETUP",
                    "setup_key":key,"source_trade_id":r.get("trade_id"),
                    "source_portfolio":r.get("portfolio_name"),
                    "source_closed_at":r.get("closed_at"),"source_reason":reason}
    return out
