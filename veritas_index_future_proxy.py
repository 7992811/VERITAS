"""Select an executable futures proxy for an index signal.

The signal remains on the cash/index series. Execution must use a broker-resolved
futures contract with a live book. Prefer a perpetual future; otherwise choose
the nearest valid expiry strictly more than one day away.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

VERSION="INDEX_FUTURE_EXECUTION_PROXY_V1"
MIN_EXPIRY_BUFFER=timedelta(days=1)


def _utc(value):
    if isinstance(value,datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z","+00:00")).astimezone(timezone.utc)
    except (TypeError,ValueError):
        return None


def choose(candidates, now=None):
    """Return deterministic preferred contract or a fail-closed reason."""
    clock=_utc(now) or datetime.now(timezone.utc)
    valid=[]
    for raw in candidates or ():
        if not isinstance(raw,dict):
            continue
        if raw.get("real_exchange")!="REAL_EXCHANGE_MOEX":
            continue
        if not raw.get("uid") or not raw.get("ticker"):
            continue
        item=dict(raw)
        perpetual=bool(item.get("perpetual"))
        expiry=_utc(item.get("expiration_date"))
        if perpetual:
            item["_selection_expiry"]=None
            valid.append((0,clock,item))
            continue
        if expiry is None or expiry-clock<=MIN_EXPIRY_BUFFER:
            continue
        item["_selection_expiry"]=expiry.isoformat()
        valid.append((1,expiry,item))
    if not valid:
        return {"eligible":False,"reason":"INDEX_FUTURE_EXECUTION_PROXY_UNAVAILABLE",
                "minimum_expiry_buffer_seconds":MIN_EXPIRY_BUFFER.total_seconds(),
                "version":VERSION}
    valid.sort(key=lambda x:(x[0],x[1],str(x[2].get("ticker"))))
    selected=dict(valid[0][2])
    selected.pop("_selection_expiry",None)
    return {
        "eligible":True,"reason":"INDEX_FUTURE_EXECUTION_PROXY_READY",
        "selection":"PERPETUAL" if bool(selected.get("perpetual")) else "NEAREST_EXPIRY_GT_1D",
        "signal_asset":"MOEX","execution_asset":"MOEXF",
        "ticker":selected.get("ticker"),"uid":selected.get("uid"),
        "expiration_date":selected.get("expiration_date"),
        "perpetual":bool(selected.get("perpetual")),
        "minimum_expiry_buffer_seconds":MIN_EXPIRY_BUFFER.total_seconds(),
        "instrument":selected,"version":VERSION,
    }


def remaining_lifetime_ok(instrument, now=None):
    """A held/selected dated contract must retain >1 day for a new entry."""
    if not isinstance(instrument,dict):
        return False
    if instrument.get("perpetual"):
        return True
    clock=_utc(now) or datetime.now(timezone.utc)
    expiry=_utc(instrument.get("expiration_date"))
    return bool(expiry is not None and expiry-clock>MIN_EXPIRY_BUFFER)
