"""Stable ProFinance authority for new normalized BRENT paper candidates.

Delayed MOEX contract discovery previously displaced the direct public quote
and could alternate BRX/BRZ price bases. This adapter has one explicit authority
for both OHLC and quote. Existing positions retain their source and contract
through veritas_position_guard; this module never changes positions.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import math

import veritas_price_source as VPS
import veritas_timeframe_structure as TS
from veritas_quote_time import quote_gate


VERSION = "CTC_BRENT_PROFINANCE_V1"
ASSET = "BRENT"
LABEL = "Brent oil"
NATIVE_TIMEFRAMES = ("1m", "5m", "1h", "4h", "1d")


def _identity():
    return VPS.identity(ASSET, {"source": "ProFinance", "raw_label": LABEL})


def _clock(now):
    value = datetime.now(timezone.utc) if now is None else now
    stamp = TS.timestamp(value)
    if stamp is None:
        raise ValueError("BRENT_DECISION_TIME_REQUIRED")
    return datetime.fromtimestamp(stamp, timezone.utc)


def _price(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _native_rows(history, clock):
    expected = _identity()
    actual = (history or {}).get("source_identity")
    if (not VPS.same(expected, actual) or actual.get("contract_id")
            or (history or {}).get("asset") != ASSET
            or (history or {}).get("raw_label") != LABEL):
        return {}, "BRENT_HISTORY_SOURCE_MISMATCH"
    mapping = {}
    for tf in NATIVE_TIMEFRAMES:
        rows = ((history or {}).get("bars_by_timeframe") or {}).get(tf) or []
        labelled = [row for row in rows if isinstance(row, dict)
                    and row.get("timeframe") == tf
                    and row.get("raw_label") == LABEL
                    and VPS.same(expected, row.get("source_identity"))
                    and not (row.get("source_identity") or {}).get("contract_id")]
        native = {TS.timestamp(row.get("ts")): row for row in labelled}
        valid = TS.closed_bars(labelled, tf, clock)[-500:]
        mapping[tf] = [{**native[row["ts"]], **row, "volume": None,
                        "volume_available": False} for row in valid]
    return mapping, None


def _daily_evidence(history):
    """Preserve observed D1 proof/revision times before historical cutoffs.

    DA validates OHLC and completion evidence. Filtering available_at here
    would hide a later revision and incorrectly substitute an older SMA day.
    Called only after the complete bundle's source identity has passed.
    """
    expected = _identity()
    return [deepcopy(row) for row in
            ((history or {}).get("bars_by_timeframe") or {}).get("1d") or []
            if isinstance(row, dict) and row.get("timeframe") == "1d"
            and row.get("raw_label") == LABEL
            and VPS.same(expected, row.get("source_identity"))
            and not (row.get("source_identity") or {}).get("contract_id")]


def _observed_partials(history, clock):
    """Keep provider-observed unfinished OHLC only as invalidation evidence."""
    expected = _identity()
    result = {}
    end = clock.timestamp()
    for tf in NATIVE_TIMEFRAMES:
        by_time, conflicts = {}, set()
        for row in ((history or {}).get("forming_bars_by_timeframe") or {}).get(tf) or []:
            if not isinstance(row, dict):
                continue
            identity = row.get("source_identity")
            if (row.get("timeframe") != tf or row.get("raw_label") != LABEL
                    or not VPS.same(expected, identity) or identity.get("contract_id")
                    or row.get("finalized") is not False or row.get("synthetic")
                    or row.get("price_type") != "Last"):
                continue
            stamp = TS.timestamp(row.get("ts"))
            observed = TS.timestamp(row.get("observed_at"))
            until = TS.timestamp(row.get("end_ts"))
            if (stamp is None or observed is None or until is None
                    or until != stamp + TS.timeframe_seconds(tf)
                    or not stamp <= observed <= end < until):
                continue
            values = {key: _price(row.get(key)) for key in ("open", "high", "low", "close")}
            if (any(value is None for value in values.values())
                    or values["low"] > min(values["open"], values["close"])
                    or values["high"] < max(values["open"], values["close"])
                    or values["low"] > values["high"]):
                continue
            current = {**row, **values, "ts":stamp, "observed_at":observed,
                       "available_at":until, "end_ts":until, "volume":None,
                       "volume_available":False, "finalized":False}
            if stamp in by_time and any(by_time[stamp][key] != values[key] for key in values):
                conflicts.add(stamp)
            else:
                by_time.setdefault(stamp, current)
        result[tf] = [by_time[t] for t in sorted(by_time) if t not in conflicts][-1:]
    return result


def build_market(quote, history, now=None):
    """Build one source-locked raw bundle from observed data only.

    Missing/stale quotes stay ineligible even when history has a recent candle.
    A native candle close may be a displayed research reference, never a fill.
    """
    clock = _clock(now)
    q = dict(quote or {})
    expected = _identity()
    mapping, history_error = _native_rows(history, clock)
    partials = {} if history_error else _observed_partials(history, clock)
    actual = VPS.identity(ASSET, q)
    same_quote = bool(q.get("raw_label") == LABEL and VPS.same(expected, actual)
                      and not (actual or {}).get("contract_id"))
    direct_price = _price(q.get("price")) if same_quote else None
    observed = q.get("observed_at") if direct_price else None
    freshness = quote_gate(observed, now=clock, execution=True, asset=ASSET)
    fresh = bool(direct_price and freshness.get("eligible"))
    reason = ("BRENT_PROFINANCE_QUOTE_READY" if fresh else
              "BRENT_PROFINANCE_QUOTE_UNAVAILABLE" if not direct_price else
              "EXECUTION_QUOTE_STALE")

    hourly = mapping.get("1h") or []
    # Legacy numerical features need numeric volume. Native bars retain None;
    # the working zeros cannot certify observed activity.
    closes = [float(row["close"]) for row in hourly]
    highs = [float(row["high"]) for row in hourly]
    lows = [float(row["low"]) for row in hourly]
    volumes = [0.0 for _ in hourly]
    reference_rows = [row for rows in mapping.values() for row in rows]
    latest = max(reference_rows, key=lambda row: row["ts"], default=None)
    reference_price = _price(latest.get("close")) if latest else None
    if not direct_price and reference_price is None:
        raise RuntimeError(history_error or "BRENT_PROFINANCE_DATA_UNAVAILABLE")
    price = direct_price or reference_price
    if history_error:
        fresh = False
        reason = history_error
    quality = [{"source": "ProFinance", "provider": "ProFinance",
        "asset_class": "Brent normalized futures", "role": "primary paper quote",
        "observed_at": observed, "age_seconds": freshness.get("age_seconds"),
        "documented_delay_seconds": 0, "effective_lag_seconds": freshness.get("age_seconds"),
        "decision_eligible": fresh, "status": "OK" if fresh else "UNAVAILABLE_OR_STALE",
        "detail": reason}]
    return {"asset": ASSET, "price": price, "secondary_price": None, "coinbase_price": None,
        "source_names": {"primary": "ProFinance", "secondary": "NOT_CONFIGURED"},
        "raw_label": LABEL, "source_divergence": 0.0, "source_gate_pass": fresh,
        "raw_ticker": q.get("raw_ticker") if same_quote else None,
        "instrument_id": q.get("instrument_id") if same_quote else None,
        "provider_ticker_verified": bool(same_quote and q.get("provider_ticker_verified")),
        "contract_identity_status": "UNVERIFIED_PROVIDER_SERIES",
        "price_series_type": "UNVERIFIED",
        "paper_eligible": fresh, "execution_eligible": fresh, "production_eligible": False,
        "production_direct_feed": False, "paper_only": True, "exact_contract_verified": False,
        "market_open": fresh, "observed_at": observed,
        "binance_close_time_ms": int(TS.timestamp(observed) * 1000) if TS.timestamp(observed) is not None else None,
        "data_latency_class": "PUBLIC_DIRECT_PAPER" if fresh else "REFERENCE_ONLY",
        "verification_mode": "PROFINANCE_BRENT_SAME_SOURCE",
        "brent_source_policy_version": VERSION, "brent_source_gate_reason": reason,
        "source_quality": quality, "quote_time_gate": freshness,
        "closes": closes, "highs": highs, "lows": lows, "vols": volumes,
        "taker_buy": [0.0 for _ in hourly], "volume_available": False,
        "returns": [closes[i] / closes[i-1] - 1 for i in range(1, len(closes))],
        "hourly_bars": hourly, "daily_bars": mapping.get("1d") or [],
        "canonical_hourly_bars": hourly, "canonical_daily_bars": mapping.get("1d") or [],
        "canonical_five_minute_bars": mapping.get("5m") or [],
        "intraday_bars": mapping.get("5m") or [], "intraday_5m": mapping.get("5m") or [],
        "structure_intraday_bars": mapping.get("5m") or [],
        "structure_minute_bars": mapping.get("1m") or [],
        "structure_source_identity": expected, "structure_bars_by_timeframe": mapping,
        "structure_forming_bars_by_timeframe": partials,
        "structure_history_status": (history or {}).get("status_by_timeframe") or {},
        "structure_history_error": history_error,
        "native_source_history_attached": True,
        "native_daily_evidence": [] if history_error else _daily_evidence(history),
        "structure_quote": {"price": direct_price, "observed_at": observed,
                            "direct": fresh, "paper_only": True},
        "reference_price_basis": "OBSERVED_QUOTE" if direct_price else "NATIVE_HISTORY_CONTEXT_ONLY",
        "reference_observed_at": observed if direct_price else None,
        "fresh_quote_diagnostics": {"selected_direct_source": "ProFinance" if fresh else None,
            "quote_refreshed_after_history": True, "quote_age_seconds": freshness.get("age_seconds"),
            "source_policy": VERSION, "foreign_source_fallback_allowed": False,
            "history_error": history_error}}


def fetch_market(quote_fetcher=None, history_fetcher=None, now=None):
    """Bounded native history then current quote; no foreign-provider fallback."""
    if history_fetcher is None:
        from veritas_profinance_history import fetch_history_bundle
        history_fetcher = fetch_history_bundle
    if quote_fetcher is None:
        from veritas_profinance import fetch_quotes
        quote_fetcher = lambda asset: fetch_quotes().get(asset) or {}
    history = history_fetcher(asset=ASSET, now=now)
    try:
        quote = quote_fetcher(ASSET) or {}
    except Exception:
        quote = {}
    return build_market(quote, history, now)
