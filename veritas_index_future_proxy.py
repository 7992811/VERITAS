"""Select an executable MOEX futures proxy for an index signal."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

VERSION = "INDEX_FUTURE_EXECUTION_PROXY_V2"
MIN_EXPIRY_BUFFER = timedelta(days=1)


def _utc(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def choose(candidates, now=None):
    """Prefer perpetual IMOEX execution; else nearest MOEX expiry >24h."""
    clock = _utc(now) or datetime.now(timezone.utc)
    valid = []
    for raw in candidates or ():
        if not isinstance(raw, dict) or raw.get("real_exchange") != "REAL_EXCHANGE_MOEX":
            continue
        if not raw.get("uid") or not raw.get("ticker"):
            continue
        item = dict(raw)
        perpetual = bool(item.get("perpetual"))
        expiry = _utc(item.get("expiration_date"))
        if perpetual:
            valid.append((0, clock, item))
        elif expiry is not None and expiry - clock > MIN_EXPIRY_BUFFER:
            valid.append((1, expiry, item))
    if not valid:
        return {
            "eligible": False, "reason": "INDEX_FUTURE_EXECUTION_PROXY_UNAVAILABLE",
            "minimum_expiry_buffer_seconds": MIN_EXPIRY_BUFFER.total_seconds(),
            "version": VERSION,
        }
    valid.sort(key=lambda x: (x[0], x[1], str(x[2].get("ticker"))))
    selected = dict(valid[0][2])
    return {
        "eligible": True, "reason": "INDEX_FUTURE_EXECUTION_PROXY_READY",
        "selection": "PERPETUAL" if bool(selected.get("perpetual")) else "NEAREST_EXPIRY_GT_1D",
        "signal_asset": "MOEX", "execution_asset": "MOEXF",
        "ticker": selected.get("ticker"), "uid": selected.get("uid"),
        "expiration_date": selected.get("expiration_date"),
        "perpetual": bool(selected.get("perpetual")),
        "minimum_expiry_buffer_seconds": MIN_EXPIRY_BUFFER.total_seconds(),
        "instrument": selected, "version": VERSION,
    }


def remaining_lifetime_ok(instrument, now=None):
    if not isinstance(instrument, dict):
        return False
    if instrument.get("perpetual"):
        return True
    clock = _utc(now) or datetime.now(timezone.utc)
    expiry = _utc(instrument.get("expiration_date"))
    return bool(expiry is not None and expiry - clock > MIN_EXPIRY_BUFFER)
