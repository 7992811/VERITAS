"""Finite legacy research features with explicit missing-history diagnostics.

Missing hourly data may disable hourly research, but must not crash a valid
native minute entry. No candle, return or volume observation is fabricated.
"""
import math


def base_features(raw, horizon, horizon_bars):
    asset = raw.get("asset")
    n = int(horizon_bars(asset, horizon))
    closes = list(raw.get("closes") or [])
    volumes = list(raw.get("vols") or [])
    taker = list(raw.get("taker_buy") or [])
    price = float(raw["price"])
    fast = max(4, min(n, 24))
    slow = min(len(closes), max(24, min(max(3*n, 72), 168)))
    prior = volumes[-slow:-fast] if slow else []
    denominator = sum(volumes[-fast:])
    lookbacks = {"ret_h": n, "ret_4h": 4,
                 "ret_24h": int(horizon_bars(asset, "1d")),
                 "ret_72h": int(horizon_bars(asset, "3d")),
                 "ret_168h": int(horizon_bars(asset, "7d")),
                 "momentum": fast}
    availability = {key: len(closes)>count for key,count in lookbacks.items()}
    values = {key: price/closes[-1-count]-1 if availability[key] else 0.
              for key,count in lookbacks.items()}
    mean = sum(closes[-slow:])/slow if slow else None
    returns = list(raw.get("returns") or [])[-fast:]
    availability.update(trend=len(closes)>=24, rv=bool(returns),
                        volume_ratio=bool(prior and sum(prior) and denominator),
                        taker_buy_share=bool(denominator) and raw.get("volume_available") is not False)
    return dict(values, asset=asset, price=price,
        coinbase_price=raw.get("coinbase_price"),
        secondary_price=raw.get("secondary_price", raw.get("coinbase_price")),
        source_divergence=raw.get("source_divergence", 0.),
        trend=price/mean-1 if mean and availability["trend"] else 0.,
        rv=math.sqrt(sum(x*x for x in returns)),
        volume_ratio=((sum(volumes[-fast:])/min(fast,len(volumes)))/(sum(prior)/len(prior))
                      if availability["volume_ratio"] else 1.),
        taker_buy_share=sum(taker[-fast:])/denominator if denominator else .5,
        observed_at=raw.get("observed_at"), binance_close_time_ms=raw.get("binance_close_time_ms"),
        source_gate_pass=raw.get("source_gate_pass", True), market_open=raw.get("market_open", True),
        research_history_status="COMPLETE" if all(availability[k] for k in lookbacks) else "PARTIAL",
        research_feature_availability=availability, research_hourly_bar_count=len(closes))
