"""Causal daily-MA context confirmed by a closed local-timeframe rebound.

Touch is not entry. Each episode needs a clear approach, a touch, and a later
closed local candle beyond both the touch extreme and the pinned daily average.
Its first confirmation consumes the episode, including when execution is later
blocked. Numerical defaults are unvalidated safeguards, not a fitted edge.
"""
from bisect import bisect_right
from copy import deepcopy
import hashlib
import json
import math
from statistics import median

import veritas_timeframe_structure as TS

VERSION = "DAILY_MA_REBOUND_V1"
EVENT_TYPE = "DAILY_MA_REBOUND"
DEFAULT_POLICY = {
    "periods": (50, 200), "zone_atr_daily": 0.10, "max_episode_bars": 12,
    "rearm_bars": 3, "rearm_atr_daily": 0.25, "slope_lookback_days": 5,
    "max_adverse_slope_atr": 0.25, "flat_slope_atr": 0.10,
    "max_flat_crossings_10d": 3,
    "supported_timeframes": ("1m", "5m", "1h", "4h", "1d"),
}


def _policy(config):
    p = dict(DEFAULT_POLICY)
    p.update({k: v for k, v in (config or {}).items() if k in p})
    periods = tuple(p["periods"])
    if not periods or any(isinstance(x, bool) or x not in (50, 200) for x in periods):
        raise ValueError("MA periods must be 50 and/or 200")
    p["periods"] = tuple(sorted({int(x) for x in periods}))
    p["supported_timeframes"] = tuple(p["supported_timeframes"])
    if not set(p["supported_timeframes"]) <= set(DEFAULT_POLICY["supported_timeframes"]):
        raise ValueError("unsupported rebound timeframe")
    for k in ("max_episode_bars", "rearm_bars", "slope_lookback_days", "max_flat_crossings_10d"):
        n = TS._number(p[k])
        if isinstance(p[k], bool) or n is None or int(n) != n or not 1 <= n <= 100:
            raise ValueError("invalid rebound policy: " + k)
        p[k] = int(n)
    if p["slope_lookback_days"] != 5:
        raise ValueError("daily slope evidence explicitly spans five days")
    for k in ("zone_atr_daily", "rearm_atr_daily", "max_adverse_slope_atr", "flat_slope_atr"):
        n = TS._number(p[k])
        if isinstance(p[k], bool) or n is None or n < 0:
            raise ValueError("invalid rebound policy: " + k)
        p[k] = n
    if p["rearm_atr_daily"] <= p["zone_atr_daily"]:
        raise ValueError("rearm excursion must leave touch zone")
    return p


def _event_id(asset, timeframe, source, period, sign, touch, signal):
    token = [VERSION, asset, timeframe, TS._source_token(source), period, sign, touch, signal]
    return "MAR_" + hashlib.sha256(
        json.dumps(token, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:24]


def _daily_boundaries(bars):
    """Sort availability once, then cache daily snapshots for the local scan."""
    out = []
    for b in bars:
        if not isinstance(b, dict):
            continue
        t = TS.timestamp(b.get("ts", b.get("time")))
        if t is not None:
            daily_end = TS.timestamp(b.get("end_ts"))
            daily_end = t + 86400 if daily_end is None else daily_end
            available = TS.timestamp(b.get("available_at"))
            out.append(max(daily_end, daily_end if available is None else available))
    return sorted(set(out))


def _daily_level(snapshot, period, sign, policy, at):
    pe = (snapshot.get("periods") or {}).get(str(period)) or {}
    value, atr = TS._number(pe.get("value")), TS._number(snapshot.get("atr20"))
    slope, crosses = TS._number(pe.get("slope_atr_5d")), TS._number(pe.get("crossings_10d"))
    if pe.get("status") != "OK" or not value or not atr or min(value, atr) <= 0:
        return None, "MA_DAILY_HISTORY_REQUIRED"
    valid_until = TS.timestamp(snapshot.get("valid_until"))
    if valid_until is None:
        return None, "MA_DAILY_FRESHNESS_REQUIRED"
    if at > valid_until:
        return None, "MA_DAILY_CONTEXT_STALE"
    if snapshot.get("status") != "OK":
        return None, "MA_DAILY_HISTORY_REQUIRED"
    if slope is None or crosses is None:
        return None, "MA_DAILY_REGIME_HISTORY_REQUIRED"
    if sign and sign * slope < -policy["max_adverse_slope_atr"]:
        return None, "MA_ADVERSE_DAILY_SLOPE"
    if abs(slope) <= policy["flat_slope_atr"] and crosses >= policy["max_flat_crossings_10d"]:
        return None, "MA_FLAT_REPEATED_CROSSES"
    return {"value":value, "atr":atr, "period":period, "period_evidence":deepcopy(pe),
            "known_at":snapshot.get("known_at"), "daily_asof":snapshot.get("daily_asof"),
            "asof_basis":snapshot.get("daily_asof_basis", "NATIVE_INTERVAL_END"),
            "valid_until":valid_until,
            "provenance":deepcopy(snapshot.get("provenance") or {})}, None


def _clear_side(bar, level, policy):
    band = policy["rearm_atr_daily"] * level["atr"]
    return (1 if bar["low"] > level["value"] + band else
            -1 if bar["high"] < level["value"] - band else 0)


def _make_event(ep, bar, prior, atr, atr_known, timeframe, asset, source, risk_policy, ma_policy):
    sign, touch, anchor = ep["sign"], ep["touch"], ep["anchor"]
    trigger = ep["trigger"]
    stop_anchor = anchor["low" if sign == 1 else "high"]
    stop = stop_anchor - sign * risk_policy["stop_buffer_atr"] * atr
    risk = sign * (trigger - stop)
    if risk <= 0 or stop <= 0:
        return None
    target = trigger + sign * max(risk_policy["target_r_multiple"] * risk,
                                 risk_policy["min_target_atr"] * atr)
    if target <= 0:
        return None
    level = ep["level"]
    signal = bar["ts"] + TS.timeframe_seconds(timeframe)
    baseline = median(b["volume"] for b in prior)
    proof = {
        "period":level["period"], "ma_value":level["value"], "daily_atr":level["atr"],
        "daily_known_at":level["known_at"], "daily_asof":level["daily_asof"],
        "daily_asof_basis":level["asof_basis"],
        "daily_valid_until":level["valid_until"],
        "daily_provenance":deepcopy(level["provenance"]),
        "period_evidence":deepcopy(level["period_evidence"]),
        "episode_start_at":touch["ts"], "touch_available_at":touch["available_at"],
        "touch_high":touch["high"], "touch_low":touch["low"],
        "bounce_level":touch["high" if sign == 1 else "low"],
        "approach_bars":deepcopy(ep["approach"]),
        "confirmation_close":bar["close"], "previous_close":ep["previous_close"],
        "episode_bars":ep["bars"], "episode_elapsed_seconds":bar["ts"]-touch["ts"],
        "policy":deepcopy(ma_policy),
    }
    return {
        "version":TS.VERSION, "ma_rebound_version":VERSION,
        "event_id":_event_id(asset, timeframe, source, level["period"], sign, touch["ts"], signal),
        "asset":asset, "direction":"LONG" if sign == 1 else "SHORT", "timeframe":timeframe,
        "event_type":EVENT_TYPE, "confirmation":"CLOSED_" + timeframe + "_BAR",
        "trigger_level":trigger, "signal_price":bar["close"], "signal_at":signal,
        "confirmed_at":bar["available_at"], "breakout_bar_at":bar["ts"],
        "trigger_pivot_at":touch["ts"], "level_available_at":touch["available_at"],
        "stop_pivot_at":anchor["ts"], "stop_level_available_at":anchor["available_at"],
        "stop_anchor":stop_anchor, "stop_price":stop, "target_price":target,
        "atr":atr, "atr_timeframe":timeframe, "stop_timeframe":timeframe,
        "target_timeframe":timeframe, "atr_observed_until":atr_known,
        "original_stop_distance_atr":risk / atr,
        "confirmation_extension_atr":sign * (bar["close"] - trigger) / atr,
        "source_identity":deepcopy(source), "policy":deepcopy(risk_policy), "ma_proof":proof,
        "activity_basis":"CLOSED_LOCAL_REBOUND_AFTER_DAILY_MA_TOUCH", "activity_confirmed":True,
        "relative_volume":bar["volume"] / baseline if baseline > 0 else None,
        "volume_observed":baseline > 0, "spent":False, "spent_reason":None, "spent_at":None,
    }


def validate_event(event, source_identity=None):
    """Check immutable daily/local evidence before shared age/geometry/cost gates."""
    fail = {"eligible":False, "reason":"MA_REBOUND_PROVENANCE_INVALID"}
    if not isinstance(event, dict):
        return fail
    e, proof = event, event.get("ma_proof") or {}
    if not isinstance(proof, dict):
        return fail
    try:
        p, risk = _policy(proof.get("policy")), TS._policy(e.get("policy"))
        tf = e["timeframe"]
        sign = 1 if e["direction"] == "LONG" else -1 if e["direction"] == "SHORT" else 0
        if (not sign or tf not in p["supported_timeframes"] or e.get("version") != TS.VERSION
                or e.get("ma_rebound_version") != VERSION or e.get("event_type") != EVENT_TYPE
                or any(e.get(k) != tf for k in ("atr_timeframe", "stop_timeframe", "target_timeframe"))
                or e.get("confirmation") != "CLOSED_" + tf + "_BAR"):
            return fail
        source = e.get("source_identity")
        if not isinstance(source, dict) or not source.get("key"):
            return fail
        if source_identity is not None and not TS._same_source(source_identity, source):
            return fail
        dp, pe = proof["daily_provenance"], proof["period_evidence"]
        if not isinstance(dp, dict) or not isinstance(pe, dict):
            return fail
        if (not TS._same_source(source, dp.get("source_identity")) or not dp.get("sha256")
                or proof["period"] not in p["periods"] or pe.get("status") != "OK"
                or pe.get("sample_count") != proof["period"] or pe.get("value") != proof["ma_value"]):
            return fail
        values = [TS._number(proof[k]) for k in ("ma_value", "daily_atr", "touch_high", "touch_low", "bounce_level")]
        if any(v is None or v <= 0 for v in values):
            return fail
        ma, daily_atr, high, low, bounce = values
        times = [TS.timestamp(proof[k]) for k in ("daily_known_at", "daily_asof", "episode_start_at", "touch_available_at")]
        opening, signal, confirmed = [TS.timestamp(e.get(k)) for k in ("breakout_bar_at", "signal_at", "confirmed_at")]
        if any(t is None for t in times + [opening, signal, confirmed]):
            return fail
        known, asof, touch, available = times
        if not asof <= known <= touch < available <= opening < signal <= confirmed:
            return fail
        valid_until = TS.timestamp(proof.get("daily_valid_until"))
        if valid_until is None or touch > valid_until:
            return fail
        if signal != opening + TS.timeframe_seconds(tf):
            return fail
        if (available != e.get("level_available_at") or touch != e.get("trigger_pivot_at")
                or TS.timestamp(pe.get("window_end")) is None or pe["window_end"] > asof):
            return fail
        if str(source["key"]).startswith("PROFINANCE:"):
            # A native date label is an index, not a verified session close.
            # Daily proof was first observed before this episode's touch.
            import veritas_daily_averages as DA
            if (proof.get("daily_asof_basis") != "PROVIDER_DATE_LABEL_ONLY"
                    or TS.timestamp(dp.get("nominal_last_period_end")) != asof
                    or dp.get("last_closed_at") is not None
                    or dp.get("verified_close_at") is not None
                    or not DA.validate_provenance(dp, source, known)):
                return fail
        elif TS.timestamp(dp.get("last_closed_at")) != asof:
            return fail
        if (not 1 <= proof["episode_bars"] <= p["max_episode_bars"]
                or not 0 < opening - touch <= p["max_episode_bars"] * TS.timeframe_seconds(tf)
                or proof.get("episode_elapsed_seconds") != opening - touch):
            return fail
        if not (low <= ma + p["zone_atr_daily"] * daily_atr and high >= ma - p["zone_atr_daily"] * daily_atr):
            return fail
        trigger = max(high, ma) if sign == 1 else min(low, ma)
        if bounce != (high if sign == 1 else low) or not math.isclose(e["trigger_level"], trigger, abs_tol=1e-10):
            return fail
        if sign * (proof["previous_close"] - trigger) > 0 or sign * (proof["confirmation_close"] - trigger) <= 0:
            return fail
        if proof["confirmation_close"] != e.get("signal_price"):
            return fail
        slope, crosses = TS._number(pe.get("slope_atr_5d")), TS._number(pe.get("crossings_10d"))
        if slope is None or crosses is None or sign * slope < -p["max_adverse_slope_atr"]:
            return fail
        if abs(slope) <= p["flat_slope_atr"] and crosses >= p["max_flat_crossings_10d"]:
            return fail
        approach = proof["approach_bars"]
        if (not isinstance(approach, (list, tuple)) or len(approach) != p["rearm_bars"]
                or any(not isinstance(bar, dict) for bar in approach)):
            return fail
        ats = [TS.timestamp(b.get("ts")) for b in approach]
        if any(t is None for t in ats) or ats != sorted(set(ats)):
            return fail
        if any(TS.timestamp(b.get("available_at")) is None
               or not b["ts"] + TS.timeframe_seconds(tf) <= b["available_at"] <= touch
               or _clear_side(b, {"value":ma, "atr":daily_atr}, p) != sign for b in approach):
            return fail
        if any(TS.timestamp(e.get(k)) is None or e[k] > opening
               for k in ("stop_level_available_at", "atr_observed_until")):
            return fail
        if not touch <= e["stop_pivot_at"] < opening:
            return fail
        atr, anchor = TS._number(e["atr"]), TS._number(e["stop_anchor"])
        if not atr or not anchor or min(atr, anchor) <= 0:
            return fail
        stop = anchor - sign * risk["stop_buffer_atr"] * atr
        if sign * (trigger - stop) <= 0:
            return fail
        target = trigger + sign * max(risk["target_r_multiple"] * sign * (trigger - stop), risk["min_target_atr"] * atr)
        if not math.isclose(e["stop_price"], stop, abs_tol=1e-10) or not math.isclose(e["target_price"], target, abs_tol=1e-10):
            return fail
        if e.get("event_id") != _event_id(e["asset"], tf, source, proof["period"], sign, touch, signal):
            return fail
    except (AttributeError, KeyError, ValueError, TypeError, OverflowError):
        return fail
    return {"eligible":True, "reason":"MA_REBOUND_PROVENANCE_READY"}


def build_context(local_bars, timeframe, now, *, daily_bars, asset, source_identity,
                  config=None, ma_config=None):
    """Use the identical prefix-only episode state machine in live and replay."""
    timeframe = str(timeframe).lower()
    out = {"version":TS.VERSION, "builder_version":VERSION, "asset":str(asset),
           "timeframe":timeframe, "atr_timeframe":timeframe,
           "source_identity":deepcopy(source_identity), "status":"INSUFFICIENT",
           "reason":"MA_LOCAL_HISTORY_REQUIRED", "event":None, "levels":[], "bars":0,
           "closed_at":None, "atr":None, "local_support":None, "local_resistance":None}
    end = TS.timestamp(now)
    if end is None:
        return dict(out, status="INVALID", reason="SAME_TF_DECISION_TIME_REQUIRED")
    try:
        p, risk = _policy(ma_config), TS._policy(config)
        if timeframe not in p["supported_timeframes"]:
            return dict(out, reason="MA_TIMEFRAME_UNSUPPORTED")
        seconds = TS.timeframe_seconds(timeframe)
    except (TypeError, ValueError, KeyError):
        return dict(out, status="INVALID", reason="MA_POLICY_INVALID")
    if not isinstance(source_identity, dict) or not source_identity.get("key"):
        return dict(out, status="INVALID", reason="MA_SOURCE_REQUIRED")
    original = list(local_bars or [])
    normalized = [dict(b, volume=None) if isinstance(b, dict) and b.get("volume_available") is False else b
                  for b in original]
    rows = TS.closed_bars(normalized, timeframe, end)
    present = {b["ts"] for b in rows}
    if any(TS.timestamp(b.get("ts", b.get("time"))) in present
           and (not b.get("source_identity") or not TS._same_source(source_identity, b["source_identity"]))
           for b in original if isinstance(b, dict)):
        return dict(out, status="INVALID", reason="MA_LOCAL_SOURCE_MISMATCH")
    out.update(bars=len(rows), closed_at=rows[-1]["available_at"] if rows else None,
               bar_seconds=seconds, policy=risk, ma_policy=p)
    period = risk["atr_period"]
    if len(rows) < period + p["rearm_bars"] + 2:
        return out
    import veritas_daily_averages as DA
    daily = list(daily_bars or [])
    boundaries, snapshots = _daily_boundaries(daily), {}
    # Normalize native observations once. Keep the complete local episode walk;
    # each cache miss still receives precisely the daily prefix known then.
    daily_context = DA.context_builder(daily, asset=asset, source_identity=source_identity,
                                       periods=p["periods"])
    ranges = [None] + [max(b["high"] - b["low"], abs(b["high"] - a["close"]),
                          abs(b["low"] - a["close"])) for a,b in zip(rows, rows[1:])]
    states = {n:{"approach":[], "episode":None} for n in p["periods"]}
    active, reasons = None, set()
    for i, bar in enumerate(rows):
        if active and bar["ts"] >= active["signal_at"]:
            TS._spend_event(active, bar)
        if i < period + 1:
            continue
        cache_key = bisect_right(boundaries, bar["ts"])
        if cache_key not in snapshots:
            snapshots[cache_key] = daily_context(bar["ts"])
        snapshot = snapshots[cache_key]
        for n, state in states.items():
            ep = state["episode"]
            if ep and not ep["consumed"]:
                ep["bars"] += 1
                if (ep["bars"] > p["max_episode_bars"]
                        or bar["ts"] - ep["touch"]["ts"] > p["max_episode_bars"] * seconds):
                    ep["consumed"] = True
                    reasons.add("MA_EPISODE_EXPIRED")
                else:
                    sign, trigger = ep["sign"], ep["trigger"]
                    crossed = sign * (rows[i-1]["close"] - trigger) <= 0 < sign * (bar["close"] - trigger)
                    if crossed:
                        ep["consumed"] = True
                        ep["previous_close"] = rows[i-1]["close"]
                        atr = sum(ranges[i-period:i]) / period
                        known = max(b["available_at"] for b in rows[i-period-1:i])
                        if atr > 0 and max(known, ep["touch"]["available_at"], ep["anchor"]["available_at"]) <= bar["ts"]:
                            e = _make_event(ep, bar, rows[i-period:i], atr, known, timeframe,
                                            str(asset), source_identity, risk, p)
                            if e and validate_event(e, source_identity)["eligible"]:
                                active = e
                                TS._spend_event(active, bar, confirmation=True)
                            else:
                                reasons.add("MA_REBOUND_PROVENANCE_INVALID")
                        else:
                            reasons.add("MA_LOCAL_EVIDENCE_NOT_AVAILABLE")
                    field = "low" if sign == 1 else "high"
                    if sign * bar[field] < sign * ep["anchor"][field]:
                        ep["anchor"] = bar
                    continue
            if ep:
                # Rearming uses the pinned OLD MA zone: daily MA movement alone
                # cannot manufacture an independent price excursion.
                clear = _clear_side(bar, ep["level"], p)
                prev = state["approach"]
                prev = prev if prev and _clear_side(prev[-1], ep["level"], p) == clear else []
                state["approach"] = (prev + [bar])[-p["rearm_bars"]:] if clear else []
                if len(state["approach"]) >= p["rearm_bars"]:
                    state["episode"] = None
                continue
            level, reason = _daily_level(snapshot, n, 0, p, bar["ts"])
            if reason:
                reasons.add(reason)
                state["approach"] = []
                continue
            if (TS.timestamp(level["known_at"]) is None or level["known_at"] > bar["ts"]
                    or not TS._same_source(source_identity, level["provenance"].get("source_identity"))):
                reasons.add("MA_DAILY_SOURCE_OR_TIME_INVALID")
                state["approach"] = []
                continue
            approach = state["approach"]
            sign = _clear_side(approach[-1], level, p) if approach else 0
            armed = (len(approach) == p["rearm_bars"] and sign
                     and all(b["available_at"] <= bar["ts"] and _clear_side(b, level, p) == sign for b in approach))
            touched = (bar["low"] <= level["value"] + p["zone_atr_daily"] * level["atr"]
                       and bar["high"] >= level["value"] - p["zone_atr_daily"] * level["atr"])
            if armed and touched:
                _, reason = _daily_level(snapshot, n, sign, p, bar["ts"])
                state["episode"] = {
                    "sign":sign, "touch":bar, "anchor":bar, "level":level,
                    "trigger":max(bar["high"], level["value"]) if sign == 1 else min(bar["low"], level["value"]),
                    "approach":deepcopy(approach), "bars":0, "consumed":bool(reason),
                }
                state["approach"] = []
                if reason:
                    reasons.add(reason)
                continue
            clear = _clear_side(bar, level, p)
            state["approach"] = ((approach if sign == clear else []) + [bar])[-p["rearm_bars"]:] if clear else []
    if active:
        TS._invalidate_from_observed_partials(active, original, timeframe, end, source_identity)
    out.update(status="OK", reason="MA_REBOUND_READY" if active else "MA_WAIT_REBOUND_CONFIRMATION",
               event=active, atr=sum(ranges[-period:]) / period,
               diagnostics=sorted(reasons), daily_snapshot_builds=len(snapshots))
    return out
