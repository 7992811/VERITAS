"""Small source/history timing evidence; no candles or network work in exports."""
from datetime import datetime, timezone

from veritas_timeframe_structure import timestamp, timeframe_seconds


def summary(raw, horizon, context, now=None):
    clock = datetime.now(timezone.utc) if now is None else now
    stamp = timestamp(clock)
    context = context or {}
    provider = (raw.get("structure_history_status") or {}).get(horizon) or {}
    closed = timestamp(context.get("closed_at"))
    seconds = timeframe_seconds(horizon)
    age = stamp - closed if stamp is not None and closed is not None else None
    fresh = bool(age is not None and seconds and 0 <= age <= seconds)
    # Provider exceptions can include request URLs. Export the class only;
    # the gate and retry reason explain the operational state without a SID.
    error = str(provider.get("fetch_error") or "").split(":", 1)[0][:80] or None
    quote = timestamp(raw.get("observed_at") or raw.get("market_observed_at"))
    ready = context.get("status") == "OK"
    return {"status": ("READY" if fresh else "STALE") if ready and closed else "UNAVAILABLE",
            "timing_fresh": fresh, "context_status": context.get("status"),
            "timeframe": horizon, "source_identity": context.get("source_identity"),
            "checked_at": stamp, "last_closed_at": closed, "age_seconds": age,
            "quote_observed_at": quote,
            "quote_context_lag_seconds": quote - closed if quote is not None and closed else None,
            "fetched_at": provider.get("fetched_at"),
            "derived_from_timeframe": provider.get("derived_from_timeframe"),
            "derived_closed_bars": provider.get("derived_closed_bars"),
            "derived_source_fetched_at": provider.get("derived_source_fetched_at"),
            "cache_reused": provider.get("cache_reused"),
            "fetch_error": error, "provider_reason": provider.get("reason"),
            "bars": context.get("bars"), "context_reason": context.get("reason")}
