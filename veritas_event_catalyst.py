"""Event/catalyst registry for execution-time continuation setups.

The registry is deliberately small and explicit. It exists to bridge verified
fundamental events into the execution rules without turning headlines into an
unconditional order. A catalyst can only relax timing/old-event identity; source,
structure, stop-risk and post-cost economics remain mandatory.

Future event feeds may populate row['fundamental_catalyst'] or
row['event_catalyst'] with the same schema.
"""
from __future__ import annotations
from datetime import datetime, timezone
import math

VERSION='R78_EVENT_CATALYST_V1'

# Verified public-policy event. The announcement window is intentionally short:
# after it expires, ordinary structural timing rules resume. The announced flow
# itself can remain part of research context, but does not permanently disable
# anti-chase.
KNOWN_CATALYSTS=(
    {
      'id':'MINFIN_BUDGET_RULE_2026_10',
      'asset':'CNYRUBF',
      'direction':'LONG',
      'category':'RUSSIAN_BUDGET_RULE_FX_PURCHASES',
      'published_at':'2026-10-05T09:00:00+00:00',
      'expires_at':'2026-10-05T16:00:00+00:00',
      'strength':0.95,
      'daily_purchase_rub_bn':12.7,
      'previous_daily_purchase_rub_bn':2.5,
      'net_authority_purchase_rub_bn':12.12,
      'flow_start':'2026-10-07',
      'flow_end':'2026-11-06',
      'source_class':'VERIFIED_PUBLIC_POLICY_RELEASE',
    },
)


def _ts(v):
    if v is None:return None
    if isinstance(v,(int,float)):
        try:
            x=float(v);return x if math.isfinite(x) else None
        except Exception:return None
    try:
        d=datetime.fromisoformat(str(v).replace('Z','+00:00'))
        if d.tzinfo is None:d=d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except Exception:return None


def _normalize(x):
    if not isinstance(x,dict):return None
    out=dict(x)
    if out.get('active') is False:return None
    if str(out.get('direction') or '') not in ('LONG','SHORT'):return None
    try:out['strength']=float(out.get('strength') or out.get('score') or 0.0)
    except Exception:out['strength']=0.0
    return out


def active_catalyst(row,direction=None,now=None):
    """Return a currently active direction-aligned catalyst, else None."""
    row=row or {}; direction=str(direction or row.get('research_decision') or '')
    if direction not in ('LONG','SHORT'):return None
    t=_ts(now if now is not None else datetime.now(timezone.utc))

    # Runtime/upstream event feeds take priority over the built-in verified event.
    for key in ('fundamental_catalyst','event_catalyst'):
        c=_normalize(row.get(key))
        if not c or c.get('direction')!=direction:continue
        start=_ts(c.get('published_at') or c.get('starts_at'))
        end=_ts(c.get('expires_at') or c.get('ends_at'))
        if t is not None and start is not None and t<start:continue
        if t is not None and end is not None and t>end:continue
        if c.get('strength',0.0)>=0.65:return c

    asset=str(row.get('asset') or '')
    for raw in KNOWN_CATALYSTS:
        if raw['asset']!=asset or raw['direction']!=direction:continue
        start=_ts(raw['published_at']);end=_ts(raw['expires_at'])
        if t is not None and start<=t<=end:
            return dict(raw)
    return None
