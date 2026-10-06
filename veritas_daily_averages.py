"""Causal averages from native, source-locked completed daily candles.

No network, other venue, hourly row-count aggregation or partial daily close is
permitted. Every period has independent availability; SMA200 needs 200 days.
"""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
import math
import veritas_price_source as VPS
from veritas_timeframe_structure import timestamp

VERSION = "NATIVE_DAILY_AVERAGES_V1"
DAY = 86400
MAX_BARS = 500


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _source(identity):
    if not isinstance(identity, dict) or not identity.get("key"):
        return None
    return {"key": str(identity["key"]),
            "contract_id": str(identity["contract_id"]) if identity.get("contract_id") else None}


def _native(row):
    if (row.get("synthetic") or row.get("source_bar_count")
            or any(row.get(k) is False for k in
                   ("complete", "is_complete", "isComplete", "closed", "finalized"))):
        return False
    if str(row.get("aggregation") or "NATIVE").upper() not in ("NATIVE", "NATIVE_DAILY"):
        return False
    if row.get("native_timeframe") == "1d" or str(row.get("native_interval")) in (
            "1d", "24", "CANDLE_INTERVAL_DAY"):
        return True
    # Existing ProFinance protocol certifies native tt=9. Other provider rows
    # require the explicit native-interval proof supplied by their adapter.
    return bool(str((row.get("source_identity") or {}).get("key", "")).startswith("PROFINANCE:")
                and row.get("chart_symbol") and row.get("price_type") == "Last")


def validated_bars(daily_bars, now, *, source_identity):
    """Closed native days plus window-local taint and diagnostics.

    Bad/conflicting observations taint every averaging window containing their
    timestamp. They cannot turn SMA200 into 200 selectively retained good days.
    """
    asof, expected = timestamp(now), _source(source_identity)
    diagnostics = {"rejected_rows": 0, "excluded_future_or_partial": 0,
                   "conflicting_timestamps": 0, "duplicate_rows": 0,
                   "tainted_timestamps": []}
    if asof is None or expected is None:
        return [], diagnostics
    rows, taint = {}, set()
    for item in daily_bars or []:
        if not isinstance(item, dict):
            diagnostics["rejected_rows"] += 1
            continue
        start = timestamp(item.get("ts", item.get("time")))
        if start is None:
            diagnostics["rejected_rows"] += 1
            continue
        end = timestamp(item.get("end_ts")) if "end_ts" in item else start + DAY
        available = timestamp(item.get("available_at")) if "available_at" in item else end
        if start >= asof or (end is not None and end > asof):
            diagnostics["excluded_future_or_partial"] += 1
            continue
        if available is not None and available > asof:
            diagnostics["excluded_future_or_partial"] += 1
            continue
        prices = {k: _number(item.get(k)) for k in ("open", "high", "low", "close")}
        valid = (str(item.get("timeframe", item.get("interval", ""))).lower() == "1d"
                 and _native(item) and VPS.same(expected, _source(item.get("source_identity")))
                 and end is not None and available is not None
                 and start < end <= available <= asof and end-start <= DAY+3600
                 and (expected["key"].startswith("MOEX:") or end-start >= DAY-3600)
                 and all(v is not None and v > 0 for v in prices.values()))
        if valid:
            valid = (prices["low"] <= min(prices["open"], prices["close"])
                     and prices["high"] >= max(prices["open"], prices["close"])
                     and prices["low"] <= prices["high"])
        if not valid:
            taint.add(start)
            diagnostics["rejected_rows"] += 1
            continue
        row = {**prices, "ts": start, "end_ts": end, "available_at": available,
               "timeframe": "1d", "native_timeframe": "1d",
               "source_identity": dict(expected)}
        if start in rows:
            diagnostics["duplicate_rows"] += 1
            if any(rows[start][k] != row[k] for k in (*prices, "end_ts", "available_at")):
                taint.add(start)
                diagnostics["conflicting_timestamps"] += 1
        else:
            rows[start] = row
    diagnostics["tainted_timestamps"] = sorted(taint)
    return [rows[t] for t in sorted(rows) if t not in taint][-MAX_BARS:], diagnostics


def _setting(config, key, default, maximum):
    number = _number((config or {}).get(key, default))
    if number is None or not 1 <= number <= maximum or int(number) != number:
        raise ValueError("invalid daily-average policy: " + key)
    return int(number)


def build_context(daily_bars, now, *, asset, source_identity,
                  periods=(18, 50, 200), config=None):
    """Native SMA/ATR snapshot known before an event or touch.

    daily_asof is the final daily END, last_daily_open_at is its opening time.
    known_at is the latest native availability time used, never retrieval time.
    slope/previous_value use one completed day by default; independent 5-day
    slopes and causal crossings_10d support flat/repeated-crossing diagnostics.
    """
    asof, identity = timestamp(now), _source(source_identity)
    atr_period = _setting(config, "atr_period", 20, 200)
    slope_count = _setting(config, "slope_lookback_days", 1, 20)
    periods = tuple(dict.fromkeys(periods))
    if not periods or any(isinstance(p, bool) or not isinstance(p, int) or not 2 <= p <= 400 for p in periods):
        raise ValueError("daily-average periods must be integers from 2 to 400")
    result = {"version": VERSION, "asset": str(asset), "timeframe": "1d",
              "source_identity": deepcopy(identity), "status": "UNAVAILABLE",
              "as_of": asof, "daily_asof": None, "last_daily_open_at": None,
              "known_at": None, "valid_until": None, "atr": None, "atr20": None, "atr_period": atr_period,
              "periods": {}, "native_daily": True, "provenance": {}}
    if asof is None:
        result["status"] = "INVALID_DECISION_TIME"
    elif identity is None:
        result["status"] = "DAILY_SOURCE_IDENTITY_REQUIRED"
    elif isinstance(source_identity, dict) and source_identity.get("asset") not in (None, "", str(asset)):
        result["status"] = "DAILY_ASSET_IDENTITY_MISMATCH"
        identity = None
    rows, diagnostics = validated_bars(daily_bars, now, source_identity=identity)
    result["diagnostics"] = diagnostics
    crypto = (identity or {}).get("key", "").startswith("BINANCE:")
    max_age = _setting(config, "max_daily_age_days", 1 if crypto else 4, 14)*DAY
    max_gap = _setting(config, "max_daily_gap_days", 1 if crypto else 14, 31)*DAY
    stale = bool(rows and asof-rows[-1]["end_ts"] > max_age+60)

    def window_bad(window):
        if not window:
            return False
        # Include invalid trailing observations too: otherwise dropping the
        # latest bad daily candle would silently use the previous 50 days.
        if any(window[0]["ts"] <= t < asof for t in diagnostics["tainted_timestamps"]):
            return True
        return any(b["ts"]-a["ts"] > max_gap+1 or b["ts"] < a["end_ts"]
                   for a, b in zip(window, window[1:]))

    for period in periods:
        window = rows[-period:]
        record = {"status": "INSUFFICIENT_DAILY_HISTORY", "value": None,
                  "previous_value": None, "slope": None, "slope_points": None,
                  "slope_points_5d": None, "slope_atr_5d": None,
                  "slope_lookback_days_5d": 5, "crossings_10d": None,
                  "sample_count": len(window), "required_count": period,
                  "window_start": window[0]["ts"] if window else None,
                  "window_end": window[-1]["end_ts"] if window else None}
        if asof is None or identity is None:
            record["status"] = result["status"]
        elif stale:
            record["status"] = "STALE_DAILY_HISTORY"
        elif len(window) == period and window_bad(window):
            record["status"] = "INVALID_DAILY_WINDOW"
        elif len(window) == period:
            value = math.fsum(b["close"] for b in window)/period
            record.update(status="OK", value=value)
            previous_window = rows[-period-slope_count:-slope_count]
            if len(previous_window) == period and not window_bad(rows[-period-slope_count:]):
                prev = math.fsum(b["close"] for b in previous_window)/period
                record.update(previous_value=prev, slope=value/prev-1,
                              slope_points=value-prev, slope_lookback_days=slope_count)
        result["periods"][str(period)] = record
        result["sma"+str(period)] = record["value"]
    if rows:
        used = rows[-max(max(periods)+max(slope_count,10), atr_period+1):]
        result.update(daily_asof=rows[-1]["end_ts"], last_daily_open_at=rows[-1]["ts"],
                      known_at=max(b["available_at"] for b in used),
                      valid_until=rows[-1]["end_ts"]+max_age+60)
        atr_rows = rows[-atr_period-1:]
        if len(atr_rows) == atr_period+1 and not stale and not window_bad(atr_rows):
            ranges = [max(b["high"]-b["low"], abs(b["high"]-prev["close"]),
                          abs(b["low"]-prev["close"])) for prev,b in zip(atr_rows,atr_rows[1:])]
            result["atr"] = math.fsum(ranges)/atr_period
            if atr_period == 20:
                result["atr20"] = result["atr"]
        for period in periods:
            record = result["periods"][str(period)]
            if record["status"] != "OK":
                continue
            extended = rows[-period-5:]
            if len(extended) == period+5 and not window_bad(extended):
                past = math.fsum(b["close"] for b in extended[:period])/period
                delta = record["value"]-past
                record["slope_points_5d"] = delta
                if result["atr"] and result["atr"] > 0:
                    record["slope_atr_5d"] = delta/result["atr"]
            crossing_window = rows[-period-9:]
            if len(crossing_window) == period+9 and not window_bad(crossing_window):
                prior_sign, crosses = 0, 0
                for end_index in range(period,period+10):
                    window = crossing_window[end_index-period:end_index]
                    average = math.fsum(b["close"] for b in window)/period
                    delta = window[-1]["close"]-average
                    sign = 1 if delta>0 else -1 if delta<0 else 0
                    if sign:
                        crosses += bool(prior_sign and sign != prior_sign)
                        prior_sign = sign
                record["crossings_10d"] = crosses
        proof = {"source_identity": identity, "timeframe": "1d",
                 "bars": [[b[k] for k in ("ts","end_ts","available_at","open","high","low","close")] for b in used]}
        digest = hashlib.sha256(json.dumps(proof,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()
        result["provenance"] = {"source_identity":deepcopy(identity),"native_timeframe":"1d",
                                "bar_count":len(rows),"digest_bar_count":len(used),
                                "first_bar_at":used[0]["ts"],"last_bar_at":rows[-1]["ts"],
                                "last_closed_at":rows[-1]["end_ts"],"sha256":digest}
        result["digest"] = digest
    if any(p["status"] == "OK" for p in result["periods"].values()):
        result["status"] = "OK"
    elif stale:
        result["status"] = "STALE_DAILY_HISTORY"
    elif result["status"] == "UNAVAILABLE" and rows:
        result["status"] = "INSUFFICIENT_OR_INVALID_DAILY_HISTORY"
    return result
