"""Causal structural entries whose trigger, stop and ATR share one timeframe.

Bars carry opening timestamps in UTC seconds and must belong to the requested
timeframe. A signal is the first COMPLETED candle closing beyond a previously
confirmed pivot. A quote executes that signal; it cannot create or refresh it.

The defaults are deterministic engineering controls, not fitted evidence of a
profitable strategy. Source/quote checks, portfolio risk and net-cost economics
remain the responsibility of the single canonical admission path.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
from statistics import median


VERSION = "SAME_TF_STRUCTURAL_V1"
TIMEFRAMES = {"1m": 60, "5m": 300, "1h": 3600, "4h": 14400,
              "1d": 86400, "3d": 259200, "7d": 604800}
DEFAULT_POLICY = {
    "atr_period": 20,
    "pivot_left": 2,
    "pivot_right": 2,
    "stop_buffer_atr": 0.15,
    "max_stop_atr": 3.0,
    "max_extension_atr": 0.5,
    "max_signal_age_bars": 1.0,
    "target_r_multiple": 2.0,
    "min_target_atr": 1.5,
}
_SYNTHETIC_SOURCES = {"PROXY_TIMING_BRIDGE", "DIRECT_QUOTE_ANCHOR",
                      "SYNTHETIC", "INTERPOLATED", "QUOTE_ANCHOR"}
_INCOMPLETE_FLAGS = ("complete", "is_complete", "isComplete", "closed", "finalized")


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def timestamp(value):
    """Accept seconds or a timezone-aware datetime/ISO string, never local time."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _number(value)
    try:
        value = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00"))
        return value.timestamp() if value.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def timeframe_seconds(timeframe):
    return TIMEFRAMES[str(timeframe).lower()]


def _source_token(identity):
    """Labels/adapter versions may change without changing the price series."""
    if isinstance(identity, dict):
        return {"key": identity.get("key"),
                "contract_id": str(identity["contract_id"]) if identity.get("contract_id") else None}
    return identity


def _same_source(expected, actual):
    # Match the source-lock contract: an explicitly pinned contract must match;
    # an unpinned normalized price series is identified by its canonical key.
    if isinstance(expected, dict) and isinstance(actual, dict):
        return bool(expected.get("key") and expected.get("key") == actual.get("key")
                    and (not expected.get("contract_id")
                         or str(expected["contract_id"]) == str(actual.get("contract_id"))))
    return expected == actual


def _policy(config=None):
    result = dict(DEFAULT_POLICY)
    result.update({k: v for k, v in (config or {}).items() if k in result})
    for key in ("atr_period", "pivot_left", "pivot_right"):
        number = _number(result[key])
        maximum = 200 if key == "atr_period" else 10
        minimum = 2 if key == "atr_period" else 1
        if number is None or int(number) != number or not minimum <= number <= maximum:
            raise ValueError("invalid structural policy: " + key)
        result[key] = int(number)
    for key in set(result) - {"atr_period", "pivot_left", "pivot_right"}:
        number = _number(result[key])
        if number is None or number < 0 or (key != "stop_buffer_atr" and number == 0):
            raise ValueError("invalid structural policy: " + key)
        result[key] = number
    if result["target_r_multiple"] < 1:
        raise ValueError("target_r_multiple must be at least one")
    return result


def closed_bars(bars, timeframe, now):
    """Return valid observed completed bars; preserve a native feed's phase.

    A conflicting duplicate timestamp is omitted rather than selecting whichever
    OHLC happened to arrive last. Explicit incomplete/synthetic bars cannot certify
    structure. Input dictionaries are never mutated.
    """
    seconds = timeframe_seconds(timeframe)
    timeframe = str(timeframe).lower()
    end = timestamp(now)
    if end is None:
        return []
    by_time, conflicts = {}, set()
    for raw in bars or []:
        if not isinstance(raw, dict):
            continue
        t = timestamp(raw.get("ts", raw.get("time")))
        label = raw.get("timeframe", raw.get("interval"))
        if label is not None and str(label).lower() != timeframe:
            continue
        if any(raw.get(k) is False for k in _INCOMPLETE_FLAGS):
            continue
        if raw.get("synthetic") or str(raw.get("source") or "").upper() in _SYNTHETIC_SOURCES:
            continue
        values = {k: _number(raw.get(k)) for k in ("open", "high", "low", "close")}
        if t is None or any(v is None or v <= 0 for v in values.values()):
            continue
        available = t + seconds
        supplied_available = timestamp(raw.get("available_at"))
        if supplied_available is not None:
            available = max(available, supplied_available)
        if available > end or t >= end:
            continue
        if (values["low"] > min(values["open"], values["close"])
                or values["high"] < max(values["open"], values["close"])
                or values["high"] < values["low"]):
            continue
        row = dict(values, ts=t, available_at=available, timeframe=timeframe,
                   volume=max(0.0, _number(raw.get("volume")) or 0.0))
        if t in by_time and any(by_time[t][k] != row[k] for k in values):
            conflicts.add(t)
        elif t not in by_time:
            by_time[t] = row
    return [by_time[t] for t in sorted(by_time) if t not in conflicts]


def _shared_source_identity(identities):
    """Certify only one explicitly labelled series, including its contract."""
    if not identities:
        return None
    first = identities[0]
    if not isinstance(first, dict) or not isinstance(first.get("key"), str) or not first["key"].strip():
        return None
    if any(not isinstance(source, dict) or _source_token(source) != _source_token(first)
           or source.get("asset") != first.get("asset") for source in identities):
        return None
    return deepcopy(first)


def aggregate_closed_bars(bars, source_timeframe, timeframe, now, anchor=None):
    """Aggregate only full observed buckets with an explicit alignment anchor.

    Native bars are returned without assuming midnight/UTC alignment. Different
    timeframes require a feed-specific anchor (e.g. the observed native 4h phase).
    Missing sub-bars, including unknown session gaps, are never manufactured.
    """
    source_timeframe, timeframe = str(source_timeframe).lower(), str(timeframe).lower()
    base, target = timeframe_seconds(source_timeframe), timeframe_seconds(timeframe)
    original = list(bars or [])
    rows = closed_bars(original, source_timeframe, now)
    sources = {}
    for raw in original:
        if isinstance(raw, dict):
            sources.setdefault(timestamp(raw.get("ts", raw.get("time"))), []).append(raw.get("source_identity"))
    for row in rows:
        source = _shared_source_identity(sources.get(row["ts"]) or [])
        if source is not None:
            row["source_identity"] = source
    if source_timeframe == timeframe:
        return rows
    if target <= base or target % base:
        raise ValueError("target must be an integer multiple of source timeframe")
    offset = timestamp(anchor)
    if offset is None:
        raise ValueError("aggregation requires an explicit feed alignment anchor")
    groups = {}
    for row in rows:
        key = math.floor((row["ts"] - offset) / target) * target + offset
        groups.setdefault(key, []).append(row)
    end, result = timestamp(now), []
    count = target // base
    for start, group in sorted(groups.items()):
        if (len(group) != count or start + target > end
                or any(abs(b["ts"] - (start + i * base)) > 1e-6 for i, b in enumerate(group))):
            continue
        combined = {"ts": start, "available_at": max(start + target, max(b["available_at"] for b in group)),
                       "open": group[0]["open"], "high": max(b["high"] for b in group),
                       "low": min(b["low"] for b in group), "close": group[-1]["close"],
                       "volume": sum(b["volume"] for b in group), "timeframe": timeframe,
                       "aggregation": "COMPLETE_OBSERVED_BUCKET", "aggregation_anchor": offset}
        source = _shared_source_identity([b.get("source_identity") for b in group])
        if source is not None:
            combined["source_identity"] = source
        result.append(combined)
    return result


def _pivot(rows, index, left, right, kind):
    field = "high" if kind == "resistance" else "low"
    value = rows[index][field]
    before = [b[field] for b in rows[index-left:index]]
    after = [b[field] for b in rows[index+1:index+right+1]]
    # On a flat top/bottom choose its first peak deterministically.
    extreme = (all(value > v for v in before) and all(value >= v for v in after)
               if kind == "resistance" else
               all(value < v for v in before) and all(value <= v for v in after))
    if not extreme:
        return None
    return {"kind": kind, "price": value, "pivot_at": rows[index]["ts"],
            "available_at": max(b["available_at"] for b in rows[index-left:index+right+1]),
            "timeframe": rows[index]["timeframe"]}


def _spend_event(event, bar, confirmation=False):
    if not event or event.get("spent"):
        return
    long = event["direction"] == "LONG"
    target_hit = bar["high"] >= event["target_price"] if long else bar["low"] <= event["target_price"]
    # A stop crossing inside the pre-entry confirmation candle does not certify
    # its intrabar ordering. Fail closed when that candle breaks both anchors.
    stop_hit = bar["low"] <= event["stop_price"] if long else bar["high"] >= event["stop_price"]
    if target_hit or stop_hit:
        reason = ("SAME_TF_BOTH_BARRIERS_REACHED" if target_hit and stop_hit else
                  "SAME_TF_TARGET_ALREADY_REACHED" if target_hit else
                  "SAME_TF_CONFIRMATION_BREAKS_STRUCTURE" if confirmation else
                  "SAME_TF_STOP_ALREADY_REACHED")
        event.update(spent=True, spent_reason=reason, spent_at=bar["available_at"],
                     spent_on_confirmation=bool(confirmation))


def _invalidate_from_observed_partials(event, bars, timeframe, end, source_identity):
    """Observed partial extremes can spend an event, never certify its creation.

    Historical OHLC lacking an actual observation timestamp must not smuggle a
    future candle high/low into a replay prefix. A partial beginning before the
    signal cannot establish whether its extreme occurred after confirmation.
    """
    if not event or event.get("spent"):
        return
    seconds = timeframe_seconds(timeframe)
    observations = []
    for raw in bars or []:
        if not isinstance(raw, dict):
            continue
        opened, observed = timestamp(raw.get("ts", raw.get("time"))), timestamp(raw.get("observed_at"))
        if (opened is None or observed is None or not opened <= observed <= end
                or opened < event["signal_at"]
                or (opened + seconds <= end and not any(raw.get(k) is False for k in _INCOMPLETE_FLAGS))):
            continue
        label = raw.get("timeframe", raw.get("interval"))
        if label is not None and str(label).lower() != timeframe:
            continue
        if (raw.get("synthetic") or str(raw.get("source") or "").upper() in _SYNTHETIC_SOURCES
                or (raw.get("source_identity") and not _same_source(source_identity, raw["source_identity"]))):
            continue
        values = {k: _number(raw.get(k)) for k in ("open", "high", "low", "close")}
        if (any(v is None or v <= 0 for v in values.values())
                or values["low"] > min(values["open"], values["close"])
                or values["high"] < max(values["open"], values["close"])
                or values["high"] < values["low"]):
            continue
        observations.append(dict(values, available_at=observed))
    for observation in sorted(observations, key=lambda b: b["available_at"]):
        _spend_event(event, observation)
        if event.get("spent"):
            event["spent_evidence"] = "OBSERVED_PARTIAL_AFTER_CONFIRMATION"
            break


def _continuation_after_target(parent, previous, bar, timeframe, asset, source_identity,
                               policy, atr, atr_observed_until, identity):
    """Create one causal continuation leg after a completed target.

    A finished target never becomes executable again. The next leg exists only
    after a later CLOSED candle continues through the immediately preceding
    candle's extreme in the same direction. Its stop is anchored beyond that
    preceding candle, matching the user rule "below previous low / above
    previous high". This makes the event deterministic per closed bar and avoids
    creating a fresh setup on every quote.
    """
    if not parent or parent.get("spent_reason") != "SAME_TF_TARGET_ALREADY_REACHED":
        return None
    if not previous or not bar or not atr or not math.isfinite(atr) or atr <= 0:
        return None
    spent_at = timestamp(parent.get("spent_at"))
    if spent_at is None or bar["ts"] < spent_at:
        return None
    direction = parent.get("direction")
    if direction not in ("LONG", "SHORT"):
        return None
    sign = 1 if direction == "LONG" else -1
    trigger = previous["high"] if sign == 1 else previous["low"]
    anchor = previous["low"] if sign == 1 else previous["high"]
    directional_close = sign * (bar["close"] - trigger) > 0
    directional_body = sign * (bar["close"] - bar["open"]) > 0
    if not (directional_close and directional_body):
        return None
    stop = anchor - sign * policy["stop_buffer_atr"] * atr
    risk = sign * (bar["close"] - stop)
    extension = sign * (bar["close"] - trigger) / atr
    if (risk <= 0 or stop <= 0 or extension <= 0
            or extension > policy["max_extension_atr"]
            or atr_observed_until is None or atr_observed_until > bar["ts"]):
        return None
    distance = max(policy["target_r_multiple"] * risk, policy["min_target_atr"] * atr)
    target = bar["close"] + sign * distance
    if target <= 0:
        return None
    signal_at = bar["available_at"]
    key = "|".join(map(str, (VERSION, asset, timeframe, identity, direction,
                             "CONTINUATION", parent.get("event_id"),
                             previous["ts"], bar["ts"], signal_at)))
    prior_event = parent.get("event_id")
    return {
        "version": VERSION,
        "event_id": "STF_" + hashlib.sha256(key.encode()).hexdigest()[:24],
        "asset": str(asset), "direction": direction, "timeframe": timeframe,
        "event_type": "SAME_TIMEFRAME_TREND_CONTINUATION",
        "confirmation": "CLOSED_" + timeframe + "_BAR",
        "trigger_level": trigger, "signal_price": bar["close"],
        "signal_at": signal_at, "confirmed_at": bar["available_at"],
        "breakout_bar_at": bar["ts"], "trigger_pivot_at": previous["ts"],
        "level_available_at": previous["available_at"],
        "stop_pivot_at": previous["ts"], "stop_level_available_at": previous["available_at"],
        "stop_anchor": anchor, "stop_price": stop, "target_price": target,
        "atr": atr, "atr_timeframe": timeframe, "stop_timeframe": timeframe,
        "target_timeframe": timeframe, "atr_observed_until": atr_observed_until,
        "original_stop_distance_atr": risk / atr,
        "confirmation_extension_atr": extension,
        "source_identity": deepcopy(source_identity), "policy": dict(policy),
        "activity_basis": "SAME_TIMEFRAME_CLOSED_CONTINUATION",
        "activity_confirmed": True, "relative_volume": None,
        "volume_observed": False, "spent": False, "spent_reason": None,
        "spent_at": None, "parent_event_id": prior_event,
        "continuation_from_spent_reason": parent.get("spent_reason"),
    }


def build_context(bars, timeframe, now, asset="", source_identity=None, config=None):
    """Build a causal same-timeframe event from historical prefixes only.

    A pivot's right-hand confirmation must be available BEFORE the breakout bar
    opens. ATR is measured on the preceding completed bars, excluding the breakout
    candle. Each structural pivot can trigger once; a later price refresh or
    recross cannot rename a spent event. A new confirmed pivot can start a new one.
    """
    timeframe = str(timeframe).lower()
    out = {"version": VERSION, "asset": str(asset), "timeframe": timeframe,
           "atr_timeframe": timeframe, "source_identity": deepcopy(source_identity),
           "status": "INSUFFICIENT", "reason": "SAME_TF_HISTORY_INSUFFICIENT",
           "event": None, "levels": [], "local_support": None, "local_resistance": None,
           "atr": None, "closed_at": None, "bars": 0}
    try:
        seconds, policy = timeframe_seconds(timeframe), _policy(config)
        identity = json.dumps(_source_token(source_identity), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (KeyError, ValueError, TypeError):
        out.update(status="INVALID", reason="SAME_TF_CONTEXT_OR_POLICY_INVALID")
        return out
    end = timestamp(now)
    if end is None:
        out.update(status="INVALID", reason="SAME_TF_DECISION_TIME_REQUIRED")
        return out
    rows = closed_bars(bars, timeframe, end)
    out.update(bar_seconds=seconds, policy=policy, bars=len(rows),
               closed_at=rows[-1]["available_at"] if rows else None)
    period, left, right = policy["atr_period"], policy["pivot_left"], policy["pivot_right"]
    if len(rows) < max(period + 2, left + right + 2):
        return out
    ranges = [None] + [max(b["high"] - b["low"], abs(b["high"] - a["close"]),
                           abs(b["low"] - a["close"])) for a, b in zip(rows, rows[1:])]
    levels, latest, consumed, active = [], {}, set(), None
    for i, bar in enumerate(rows):
        if active and bar["ts"] >= active["signal_at"]:
            _spend_event(active, bar)
        j = i - right - 1
        if j >= left:
            for kind in ("resistance", "support"):
                pivot = _pivot(rows, j, left, right, kind)
                if pivot and pivot["available_at"] <= bar["ts"]:
                    levels.append(pivot)
                    latest[kind] = pivot
        if i == 0:
            continue
        atr = sum(ranges[i-period:i]) / period if i >= period+1 else None
        atr_observed_until = max(b["available_at"] for b in rows[i-period-1:i]) if atr else None
        continuation = _continuation_after_target(
            active, rows[i-1], bar, timeframe, asset, source_identity,
            policy, atr, atr_observed_until, identity)
        if continuation:
            active = continuation
            prior_volumes = [b["volume"] for b in rows[i-period:i]]
            baseline = median(prior_volumes)
            active["relative_volume"] = bar["volume"] / baseline if baseline > 0 else None
            active["volume_observed"] = baseline > 0
            _spend_event(active, bar, confirmation=True)
            continue
        for direction, sign, trigger_kind, stop_kind in (
                ("LONG", 1, "resistance", "support"), ("SHORT", -1, "support", "resistance")):
            trigger, opposite = latest.get(trigger_kind), latest.get(stop_kind)
            if not trigger:
                continue
            level = trigger["price"]
            pivot_key = (direction, trigger["pivot_at"])
            crossed = sign * (rows[i-1]["close"] - level) <= 0 < sign * (bar["close"] - level)
            if not crossed or pivot_key in consumed:
                continue
            # The first crossing is spent even if history cannot yet certify
            # ATR/stop geometry. A later recross is not a new structural event.
            consumed.add(pivot_key)
            if (not opposite or not atr or not math.isfinite(atr)
                    or atr_observed_until is None or atr_observed_until > bar["ts"]):
                continue
            anchor = opposite["price"]
            stop = anchor - sign * policy["stop_buffer_atr"] * atr
            risk = sign * (level - stop)
            if risk <= 0 or stop <= 0:
                continue
            distance = max(policy["target_r_multiple"] * risk, policy["min_target_atr"] * atr)
            target = level + sign * distance
            if target <= 0:
                continue
            signal_at = bar["ts"] + seconds
            key = "|".join(map(str, (VERSION, asset, timeframe, identity, direction,
                                     trigger["pivot_at"], opposite["pivot_at"], signal_at)))
            prior_volumes = [b["volume"] for b in rows[i-period:i]]
            baseline = median(prior_volumes)
            active = {
                "version": VERSION, "event_id": "STF_" + hashlib.sha256(key.encode()).hexdigest()[:24],
                "asset": str(asset), "direction": direction, "timeframe": timeframe,
                "event_type": "SAME_TIMEFRAME_STRUCTURAL_BREAKOUT",
                "confirmation": "CLOSED_" + timeframe + "_BAR",
                "trigger_level": level, "signal_price": bar["close"],
                "signal_at": signal_at, "confirmed_at": bar["available_at"],
                "breakout_bar_at": bar["ts"], "trigger_pivot_at": trigger["pivot_at"],
                "level_available_at": trigger["available_at"],
                "stop_pivot_at": opposite["pivot_at"], "stop_level_available_at": opposite["available_at"],
                "stop_anchor": anchor, "stop_price": stop, "target_price": target,
                "atr": atr, "atr_timeframe": timeframe, "stop_timeframe": timeframe,
                "target_timeframe": timeframe, "atr_observed_until": atr_observed_until,
                "original_stop_distance_atr": risk / atr,
                "confirmation_extension_atr": sign * (bar["close"] - level) / atr,
                "source_identity": deepcopy(source_identity), "policy": dict(policy),
                "activity_basis": "SAME_TIMEFRAME_CLOSED_BREAKOUT", "activity_confirmed": True,
                "relative_volume": bar["volume"] / baseline if baseline > 0 else None,
                "volume_observed": baseline > 0, "spent": False, "spent_reason": None,
                "spent_at": None,
            }
            _spend_event(active, bar, confirmation=True)
            break
    _invalidate_from_observed_partials(active, bars, timeframe, end, source_identity)
    atr_now = sum(ranges[-period:]) / period
    out.update(status="OK", reason="SAME_TF_STRUCTURE_READY" if active else "SAME_TF_WAIT_STRUCTURAL_BREAKOUT",
               atr=atr_now, levels=levels[-24:], event=active,
               local_support=(latest.get("support") or {}).get("price"),
               local_resistance=(latest.get("resistance") or {}).get("price"))
    return out


def entry_gate(context, price, direction, now, config=None):
    """Validate the immutable event at a current quote without rebasing geometry."""
    context = context or {}
    event = context.get("event") or {}
    out = {"version": VERSION, "eligible": False, "reason": "SAME_TF_CONTEXT_REQUIRED",
           "timeframe": context.get("timeframe"), "event_id": event.get("event_id")}
    if context.get("status") != "OK":
        return dict(out, reason=context.get("reason") or "SAME_TF_HISTORY_INSUFFICIENT")
    if not event:
        return dict(out, reason="SAME_TF_WAIT_STRUCTURAL_BREAKOUT")
    timeframe = str(context.get("timeframe") or "")
    if (event.get("version") != VERSION or event.get("event_type") not in (
            "SAME_TIMEFRAME_STRUCTURAL_BREAKOUT", "SAME_TIMEFRAME_TREND_CONTINUATION",
            "DAILY_MA_REBOUND")
            or any(event.get(k) != timeframe for k in ("timeframe", "atr_timeframe", "stop_timeframe", "target_timeframe"))
            or event.get("confirmation") != "CLOSED_" + timeframe + "_BAR"):
        return dict(out, reason="SAME_TF_PROVENANCE_MISMATCH")
    if not _same_source(event.get("source_identity"), context.get("source_identity")):
        return dict(out, reason="SAME_TF_SOURCE_IDENTITY_MISMATCH")
    if event.get("event_type") == "DAILY_MA_REBOUND":
        from veritas_ma_rebound import validate_event
        proof = validate_event(event, source_identity=context.get("source_identity"))
        if not proof.get("eligible"):
            return dict(out, reason=proof.get("reason") or "DAILY_MA_REBOUND_PROOF_INVALID")
    direction = str(direction or "").upper()
    if direction not in ("LONG", "SHORT") or event.get("direction") != direction:
        return dict(out, reason="SAME_TF_DIRECTION_CONFLICT")
    if event.get("spent"):
        return dict(out, reason=event.get("spent_reason") or "SAME_TF_EVENT_SPENT", spent_at=event.get("spent_at"))
    end, signal, opening = timestamp(now), timestamp(event.get("signal_at")), timestamp(event.get("breakout_bar_at"))
    confirmed = timestamp(event.get("confirmed_at"))
    known = [timestamp(event.get(k)) for k in ("level_available_at", "stop_level_available_at", "atr_observed_until")]
    if (end is None or signal is None or opening is None or any(v is None or v > opening for v in known)
            or confirmed is None or signal <= opening or not signal <= confirmed <= end
            or (timestamp(context.get("closed_at")) or end) > end):
        return dict(out, reason="SAME_TF_NONCAUSAL_EVENT")
    try:
        policy = _policy(dict(event.get("policy") or {}, **(config or {})))
        seconds = timeframe_seconds(timeframe)
    except (KeyError, TypeError, ValueError):
        return dict(out, reason="SAME_TF_CONTEXT_OR_POLICY_INVALID")
    age, max_age = end - signal, policy["max_signal_age_bars"] * seconds
    out.update(age_seconds=age, max_age_seconds=max_age)
    if age > max_age:
        return dict(out, reason="SAME_TF_EVENT_EXPIRED")
    px, level, stop, target, atr = [_number(v) for v in
                                  (price, event.get("trigger_level"), event.get("stop_price"),
                                   event.get("target_price"), event.get("atr"))]
    if any(v is None or v <= 0 for v in (px, level, stop, target, atr)):
        return dict(out, reason="SAME_TF_GEOMETRY_INVALID")
    sign = 1 if direction == "LONG" else -1
    if sign * (level - stop) <= 0 or sign * (target - level) <= 0:
        return dict(out, reason="SAME_TF_GEOMETRY_INVALID")
    extension = sign * (px - level) / atr
    out.update(trigger_level=level, stop_price=stop, target_price=target, atr=atr,
               extension_atr=extension, max_extension_atr=policy["max_extension_atr"],
               signal_at=signal, source_identity=deepcopy(event.get("source_identity")))
    if sign * (px - stop) <= 0:
        return dict(out, reason="SAME_TF_STOP_ALREADY_REACHED")
    if sign * (target - px) <= 0:
        return dict(out, reason="SAME_TF_TARGET_ALREADY_REACHED")
    if extension < 0:
        return dict(out, reason="SAME_TF_BREAKOUT_LEVEL_NOT_HELD")
    if extension > policy["max_extension_atr"]:
        return dict(out, reason="SAME_TF_ENTRY_EXTENDED")
    risk_atr = sign * (px - stop) / atr
    if max(risk_atr, sign * (level - stop) / atr) > policy["max_stop_atr"]:
        return dict(out, reason="SAME_TF_STOP_TOO_DISTANT", stop_distance_atr=risk_atr,
                    max_stop_atr=policy["max_stop_atr"])
    risk, reward = sign * (px - stop) / px, sign * (target - px) / px
    return dict(out, eligible=True, reason="SAME_TF_ENTRY_READY", stop_distance_atr=risk_atr,
                stop_distance_pct=risk, remaining_move_pct=reward, reward_risk=reward / risk,
                atr_timeframe=timeframe, stop_timeframe=timeframe, target_timeframe=timeframe)
