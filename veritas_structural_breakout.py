"""Causal quote breakouts with an explicit structural risk timeframe.

This module observes supplied native bars and verified exchange quotes only. It
does not fetch data, write a ledger, or submit an order. ``quote_state`` is an
explicit, serializable state transition; callers carry it between observations.
Targets are previously observed price zones, never a price invented to meet RR.
Numerical defaults are engineering controls, not fitted profitability evidence.
"""
from veritas_data_copy import deepcopy
from collections import OrderedDict
from datetime import datetime, timezone
import hashlib
import json
import math
import pickle
import sys
from statistics import median
from threading import RLock

import veritas_price_source as VPS
import veritas_timeframe_structure as TS
from veritas_quote_time import quote_gate
import veritas_structural_validation_cache as SVC


VERSION = "CAUSAL_QUOTE_STRUCTURE_V1"
EVENT_TYPE = "VERIFIED_QUOTE_STRUCTURAL_BREAKOUT"
# Private, bounded native facts only. Quotes, events and allocation state are
# evaluated on every call. No returned context shares these mutable objects.
_FACTS_CACHE = OrderedDict()
_FACTS_LOCK = RLock()
_FACTS_CACHE_BYTES = 0
_FACTS_CACHE_MAX_BYTES = 8 * 1024 * 1024
_FACTS_CACHE_MAX_BUNDLES = 3
DEFAULT_POLICY = {
    "version": "CTC_INTRABAR_STRUCTURE_V1",
    "teaching_id": "USER_INTRABAR_STRUCTURE_2026_10_07",
    "enabled": True,
    "parent_timeframe": "1h",
    "atr_period": 20,
    "pivot_left": 2,
    "pivot_right": 2,
    "stop_buffer_atr": 0.15,
    "max_stop_atr": 6.0,
    "max_signal_age_bars": 2.0,
    "minimum_entry_window_seconds": 120.0,
    "max_target_progress": 0.60,
    "target_cluster_atr": 0.25,
    "min_target_atr": 1.5,
    "target_zone_min_touches": 3,
    "target_consolidation_gap_bars": 1.0,
    "protected_swing_min_prominence_atr": 1.0,
    "target_one_fraction": 0.50,
}
_SEALED_FIELDS = (
    "version", "event_type", "event_id", "asset", "direction", "timeframe",
    "trigger_timeframe", "structural_timeframe", "stop_timeframe", "atr_timeframe",
    "target_timeframe", "confirmation", "signal_at", "confirmed_at", "signal_price",
    "trigger_level", "trigger", "reference", "trigger_quote", "source_identity",
    "leg_id", "parent_event_id", "phase", "stop_anchor", "protected_swing",
    "stop_price", "atr", "atr_proof", "atr_observed_until", "target_price",
    "runner_target_price", "target_ladder", "target_zones", "policy",
)


def _number(value):
    if isinstance(value, bool):
        return None
    return TS._number(value)


def _digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _identity_token(source):
    return {"key": (source or {}).get("key"),
            "contract_id": (source or {}).get("contract_id")}


def _same_source(expected, actual):
    return bool(VPS.same(expected, actual) and VPS.same(actual, expected))


def _policy(config=None):
    policy = dict(DEFAULT_POLICY)
    policy.update({k: v for k, v in (config or {}).items() if k in policy})
    for key in ("atr_period", "pivot_left", "pivot_right", "target_zone_min_touches"):
        value = _number(policy[key])
        minimum, maximum = (2, 200) if key == "atr_period" else (1, 20)
        if value is None or int(value) != value or not minimum <= value <= maximum:
            raise ValueError("invalid breakout policy: " + key)
        policy[key] = int(value)
    for key in ("stop_buffer_atr", "max_stop_atr", "max_signal_age_bars",
                "minimum_entry_window_seconds", "max_target_progress",
                "target_cluster_atr", "min_target_atr",
                "target_consolidation_gap_bars",
                "protected_swing_min_prominence_atr", "target_one_fraction"):
        value = _number(policy[key])
        if value is None or value < 0 or (key != "stop_buffer_atr" and value == 0):
            raise ValueError("invalid breakout policy: " + key)
        policy[key] = value
    if not 0 < policy["target_one_fraction"] < 1 or not 0 < policy["max_target_progress"] < 1:
        raise ValueError("invalid breakout fractions")
    if policy["parent_timeframe"] not in TS.TIMEFRAMES:
        raise ValueError("invalid parent timeframe")
    return policy


def applies(context):
    context = context or {}
    return bool(context.get("version") == VERSION
                or (context.get("event") or {}).get("version") == VERSION)


def _quote(raw, now, source):
    quote = VPS.quote_from_row(raw or {})
    asset = str((raw or {}).get("asset") or "")
    identity = VPS.identity(asset, quote)
    observed = TS.timestamp(quote.get("observed_at"))
    end = TS.timestamp(now)
    price = _number(quote.get("price"))
    out = {"eligible": False, "reason": "STRUCTURAL_EXECUTION_QUOTE_INVALID"}
    if (not identity or not _same_source(source, identity)
            or quote.get("asset") not in (None, asset)):
        return dict(out, reason="SAME_TF_SOURCE_MISMATCH"), None
    if (quote.get("source_gate_pass") is not True or quote.get("market_open") is not True
            or (asset == "BRENT" and not VPS.brent_quote_verified(quote))):
        return dict(out, reason="STRUCTURAL_VERIFIED_QUOTE_REQUIRED"), None
    if observed is None or end is None or price is None or price <= 0:
        return out, None
    fresh = quote_gate(datetime.fromtimestamp(observed, timezone.utc),
                       (raw or {}).get("horizon"), datetime.fromtimestamp(end, timezone.utc),
                       execution=True, asset=asset)
    if not fresh["eligible"]:
        return dict(out, reason="EXECUTION_QUOTE_STALE", quote_time_gate=fresh), None
    bid, ask = _number(quote.get("best_bid", quote.get("bid"))), _number(quote.get("best_ask", quote.get("ask")))
    if (bid is None) != (ask is None) or (bid is not None and (bid <= 0 or ask <= bid)):
        return dict(out, reason="STRUCTURAL_EXECUTION_BOOK_INVALID"), None
    normalized = {"price": price, "observed_at": observed, "source_identity": identity,
                  "best_bid": bid, "best_ask": ask,
                  "source_gate_pass": True, "market_open": True}
    return {"eligible": True, "reason": "STRUCTURAL_VERIFIED_QUOTE_READY",
            "quote_time_gate": fresh}, normalized


def _native_rows(raw, timeframe, as_of, source):
    values = ((raw or {}).get("structure_bars_by_timeframe") or {}).get(timeframe) or []
    for bar in values:
        if not isinstance(bar, dict):
            continue
        if bar.get("source_identity") and not _same_source(source, bar["source_identity"]):
            return [], "STRUCTURAL_BAR_SOURCE_MISMATCH"
    return TS.closed_bars(values, timeframe, as_of), None


def _atr(rows, period):
    if len(rows) < period + 1:
        return None
    sample = rows[-period-1:]
    ranges = [max(b["high"] - b["low"], abs(b["high"] - a["close"]),
                  abs(b["low"] - a["close"])) for a, b in zip(sample, sample[1:])]
    value = sum(ranges) / period
    return {"value": value, "observed_until": max(b["available_at"] for b in sample),
            "timeframe": sample[-1]["timeframe"], "period": period,
            "bars": deepcopy(sample)} if value > 0 else None


def _levels(rows, source, policy):
    left, right = policy["pivot_left"], policy["pivot_right"]
    levels = []
    # Include the pivot whose last confirming bar is available exactly as of
    # the quote. Waiting for an additional completed breakout bar is unnecessary.
    for index in range(left, len(rows) - right):
        for kind in ("resistance", "support"):
            pivot = TS._pivot(rows, index, left, right, kind)
            if not pivot:
                continue
            before, after = rows[index-left:index], rows[index+1:index+right+1]
            if kind == "resistance":
                prominence = min(pivot["price"] - min(b["low"] for b in before),
                                 pivot["price"] - min(b["low"] for b in after))
            else:
                prominence = min(max(b["high"] for b in before) - pivot["price"],
                                 max(b["high"] for b in after) - pivot["price"])
            pivot.update(prominence=max(0.0, prominence), source_identity=deepcopy(source))
            pivot["level_id"] = "QL_" + _digest({"source": _identity_token(source),
                **{key: pivot[key] for key in ("timeframe", "kind", "price", "pivot_at", "available_at")}})[:24]
            levels.append(pivot)
    return levels


def _facts_size(value, seen=None):
    seen = set() if seen is None else seen
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        size += sum(_facts_size(k, seen) + _facts_size(v, seen) for k, v in value.items())
    elif isinstance(value, (list, tuple)):
        size += sum(_facts_size(v, seen) for v in value)
    return size


def _clear_native_facts_cache():
    global _FACTS_CACHE_BYTES
    with _FACTS_LOCK:
        _FACTS_CACHE.clear()
        _FACTS_CACHE_BYTES = 0


def _first_cross_times(rows_by_tf, levels):
    """Earliest available historical crossing, preserving delayed bar times."""
    result = {}
    for level in levels:
        crossed_at = None
        for bar in rows_by_tf.get(level['timeframe'], ()):
            if bar['ts'] < level['available_at']:
                continue
            crossed = (bar['high'] > level['price'] if level['kind'] == 'resistance'
                       else bar['low'] < level['price'])
            if crossed:
                available = bar['available_at']
                crossed_at = available if crossed_at is None else min(crossed_at, available)
        if crossed_at is not None:
            result[level['level_id']] = crossed_at
    return result


def _native_facts(raw, as_of, source, policy):
    """Memoize an exact history snapshot within its causal availability window.

    The fingerprint covers all input fields, including historical corrections,
    completion flags, source labels and supplied availability times. The time
    window ends at the next possible native-bar availability, including delayed
    availability of an older candle. An earlier as-of always rebuilds.
    """
    global _FACTS_CACHE_BYTES
    mapping = raw.get("structure_bars_by_timeframe") or {}
    try:
        # Pickle is used only to hash in-memory input; it is never deserialized.
        # This keeps exact float/datetime values and is cheaper than reparsing
        # every OHLC field seven times for the seven execution horizons.
        key = hashlib.sha256(pickle.dumps((raw.get("asset"), source, policy, mapping), protocol=5)).digest()
    except (TypeError, ValueError, OverflowError, AttributeError, RecursionError, pickle.PickleError):
        key = None  # Unsupported metadata bypasses memoization, not validation.
    if key is not None:
        with _FACTS_LOCK:
            cached = _FACTS_CACHE.get(key)
            if cached and cached["valid_from"] <= as_of < cached["valid_until"]:
                _FACTS_CACHE.move_to_end(key)
                return cached["facts"], None
    rows_by_tf, levels, atr_by_tf = {}, [], {}
    next_available = math.inf
    for tf, supplied in mapping.items():
        if tf not in TS.TIMEFRAMES:
            continue
        rows, issue = _native_rows(raw, tf, as_of, source)
        if issue:
            return None, issue
        if rows:
            rows_by_tf[tf] = rows
            levels.extend(_levels(rows, source, policy))
            atr_by_tf[tf] = _atr(rows, policy["atr_period"])
        seconds = TS.timeframe_seconds(tf)
        for bar in supplied or []:
            if not isinstance(bar, dict):
                continue
            opening = TS.timestamp(bar.get("ts", bar.get("time")))
            if opening is None:
                continue
            supplied_at = TS.timestamp(bar.get("available_at"))
            available = max(opening + seconds, supplied_at if supplied_at is not None else -math.inf)
            if available > as_of:
                next_available = min(next_available, available)
    facts = {"rows_by_tf": rows_by_tf, "levels": levels, "atr_by_tf": atr_by_tf,
             "first_cross_at": _first_cross_times(rows_by_tf, levels)}
    if key is not None:
        entry = {"facts": facts, "valid_from": as_of, "valid_until": next_available}
        size = _facts_size((key, entry))
        if size <= _FACTS_CACHE_MAX_BYTES:
            with _FACTS_LOCK:
                previous = _FACTS_CACHE.pop(key, None)
                if previous:
                    _FACTS_CACHE_BYTES -= previous["bytes"]
                while _FACTS_CACHE and (len(_FACTS_CACHE) >= _FACTS_CACHE_MAX_BUNDLES
                                       or _FACTS_CACHE_BYTES + size > _FACTS_CACHE_MAX_BYTES):
                    _, removed = _FACTS_CACHE.popitem(last=False)
                    _FACTS_CACHE_BYTES -= removed["bytes"]
                entry["bytes"] = size
                _FACTS_CACHE[key] = entry
                _FACTS_CACHE_BYTES += size
    return facts, None


def _same_observed_swing(first, second):
    """Two native timeframes can describe one physical high or low."""
    if not math.isclose(first["price"], second["price"], rel_tol=1e-12, abs_tol=1e-12):
        return False
    first_end = first["pivot_at"] + TS.timeframe_seconds(first["timeframe"])
    second_end = second["pivot_at"] + TS.timeframe_seconds(second["timeframe"])
    return max(first["pivot_at"], second["pivot_at"]) < min(first_end, second_end)


def _zone_episodes(members, structural_tf, atr, policy):
    # Count the finest available observations first. A daily/three-day view of
    # the same extremum supplies extra provenance, never another market touch.
    observations = []
    for member in sorted(members, key=lambda x: (TS.timeframe_seconds(x["timeframe"]), x["pivot_at"])):
        existing = next((x for x in observations if _same_observed_swing(x["representative"], member)), None)
        if existing:
            existing["variants"].append(member)
        else:
            observations.append({"representative": member, "variants": [member]})
    observations.sort(key=lambda x: x["representative"]["pivot_at"])
    episodes = []
    max_gap = policy["target_consolidation_gap_bars"] * TS.timeframe_seconds(structural_tf)
    for observation in observations:
        if (not episodes or observation["representative"]["pivot_at"] -
                episodes[-1][-1]["representative"]["pivot_at"] > max_gap):
            episodes.append([observation])
        else:
            episodes[-1].append(observation)
    qualified = []
    for episode in episodes:
        variants = [member for observation in episode for member in observation["variants"]]
        parent = any(TS.timeframe_seconds(x["timeframe"]) >= TS.timeframe_seconds(structural_tf)
                     and x["prominence"] >= policy["min_target_atr"] * atr for x in variants)
        if len(episode) >= policy["target_zone_min_touches"] or parent:
            qualified.append((episode, variants))
    return qualified


def _zone_cluster(levels, direction, trigger, price, atr, structural_tf, policy, rows_by_tf=None):
    sign = 1 if direction == "LONG" else -1
    kind = "resistance" if sign == 1 else "support"
    # Targets use repeated zones from at least five-minute structure. A lone
    # minute wick is not promoted into a major take-profit level.
    relevant = [p for p in levels if p["kind"] == kind and
                TS.timeframe_seconds(p["timeframe"]) >= min(300, TS.timeframe_seconds(structural_tf))
                and sign * (p["price"] - trigger) > 0]
    relevant.sort(key=lambda p: p["price"])
    width = policy["target_cluster_atr"] * atr
    groups = []
    for level in relevant:
        if not groups or level["price"] - groups[-1][0]["price"] > 2.0 * width:
            groups.append([level])
        else:
            groups[-1].append(level)
    zones = []
    for members in groups:
        # A nearer major zone remains TP1 even if its distance is smaller than
        # ATR or its economics are weak. Only zone *evidence*, never required
        # payoff, determines major/minor classification.
        qualified = _zone_episodes(members, structural_tf, atr, policy)
        if not qualified:
            continue
        episode, members = max(qualified, key=lambda item: max(x["available_at"] for x in item[1]))
        observed = [item["representative"] for item in episode]
        center = median(x["price"] for x in observed)
        low, high = min(x["price"] for x in members), max(x["price"] for x in members)
        available = max(x["available_at"] for x in members)
        # Historical acceptance outside the far edge consumes a resistance or
        # support zone. This uses completed native structure, not the current
        # quote, and is never applied to an already frozen event's targets.
        accepted = any(bar["ts"] >= available and
                       (bar["low"] > high if sign == 1 else bar["high"] < low)
                       for tf, rows in (rows_by_tf or {}).items()
                       if TS.timeframe_seconds(tf) >= min(300, TS.timeframe_seconds(structural_tf))
                       for bar in rows)
        if accepted:
            continue
        # A quote inside or past a known target zone cannot turn it into a new
        # farther target. Such an event is caught by the late-entry gate below.
        zone = {"price": center, "low": low, "high": high,
                "kind": kind, "basis": "OBSERVED_HISTORICAL_PIVOT_ZONE",
                "touches": len(observed), "available_at": available,
                "evidence_method": "INDEPENDENT_TOUCHES_IN_ONE_CONSOLIDATION_OR_PROMINENT_PARENT_SWING",
                "consolidation_start": min(x["pivot_at"] for x in observed),
                "consolidation_end": max(x["pivot_at"] for x in observed),
                "timeframes": sorted({x["timeframe"] for x in members}, key=TS.timeframe_seconds),
                "members": [{k: x[k] for k in ("level_id", "timeframe", "price", "pivot_at", "available_at")}
                            for x in members]}
        zone["zone_id"] = "QZ_" + _digest(zone)[:24]
        zones.append(zone)
    zones.sort(key=lambda z: sign * z["price"])
    return zones


def _reference(level, current, previous, rows):
    available = level["available_at"]
    end = current["observed_at"]
    if previous and available <= previous["observed_at"] < end:
        return {"kind": "VERIFIED_QUOTE", "price": previous["price"],
                "observed_at": previous["observed_at"],
                "source_identity": deepcopy(previous["source_identity"])}
    available_rows = [b for b in rows if available <= b["available_at"] <= end]
    if not available_rows:
        return None
    bar = available_rows[-1]
    return {"kind": "NATIVE_CLOSED_BAR", "price": bar["close"],
            "observed_at": bar["available_at"], "bar_at": bar["ts"],
            "timeframe": bar["timeframe"], "source_identity": deepcopy(current["source_identity"])}


def _event_id(asset, horizon, source, trigger, leg_id):
    # A parent level observed by 1m, 5m and 1h is one market event. The display
    # or execution lane must not manufacture three allocation permissions.
    # A restart may reconstruct the protected leg from another starting point;
    # that cannot grant another allocation at the same already-used level.
    return "QSB_" + _digest({"asset": asset,
                            "source": _identity_token(source),
                            "direction": "LONG" if trigger["kind"] == "resistance" else "SHORT",
                            "trigger": trigger["level_id"]})[:24]


def _seal(event):
    return _digest({key: event.get(key) for key in _SEALED_FIELDS})


def _leg_for(direction, trigger, structural_tf, levels, atr, previous_leg, quote, policy):
    sign = 1 if direction == "LONG" else -1
    kind = "support" if sign == 1 else "resistance"
    candidates = [p for p in levels if p["timeframe"] == structural_tf
                  and p["kind"] == kind and sign * (trigger["price"] - p["price"]) > 0
                  and p["available_at"] <= quote["observed_at"]]
    candidates.sort(key=lambda p: (p["pivot_at"], p["available_at"]), reverse=True)
    if previous_leg and previous_leg.get("direction") == direction and not previous_leg.get("invalidated"):
        leg = deepcopy(previous_leg)
        # Only a *confirmed structural-timeframe* pullback may advance the
        # protected swing. A fast higher low cannot tighten a senior stop.
        for candidate in candidates:
            old = leg["protected_swing"]
            if (candidate["pivot_at"] > leg["started_at"] and
                    sign * (candidate["price"] - old["price"]) > 0 and
                    candidate["prominence"] >= policy["protected_swing_min_prominence_atr"] * atr["value"]):
                revised_stop = candidate["price"] - sign * policy["stop_buffer_atr"] * atr["value"]
                if sign * (revised_stop - leg["stop_price"]) > 0:
                    leg.update(protected_swing=deepcopy(candidate), stop_price=revised_stop,
                               atr=atr["value"], atr_proof=deepcopy(atr),
                               protected_revision=leg.get("protected_revision", 0) + 1)
                break
        return leg
    if not candidates:
        return None
    # A confirmed pullback may form AFTER the high/low being broken.
    # For a new leg use the latest opposite swing available at the quote,
    # not an older swing preceding the trigger's own pivot. Existing
    # protected legs retain the no-widening path above.
    anchor = candidates[0]
    leg_id = "QLEG_" + _digest({"source": _identity_token(quote["source_identity"]),
                               "direction": direction, "structural_tf": structural_tf,
                               "anchor": anchor["level_id"], "first_trigger": trigger["level_id"]})[:24]
    return {"leg_id": leg_id, "direction": direction, "structural_timeframe": structural_tf,
            "protected_swing": deepcopy(anchor), "started_at": quote["observed_at"],
            "stop_price": anchor["price"] - sign * policy["stop_buffer_atr"] * atr["value"],
            "atr": atr["value"], "atr_proof": deepcopy(atr), "protected_revision": 0,
            "invalidated": False, "last_event_id": None}


def _spend(event, quote, rows_by_tf):
    if not event or event.get("spent"):
        return
    direction = event["direction"]
    sign = 1 if direction == "LONG" else -1
    stop, target = event["stop_price"], event["target_price"]
    stop_hit = sign * (quote["price"] - stop) <= 0
    target_hit = sign * (target - quote["price"]) <= 0
    # A closed bar beginning before the actual quote trigger cannot establish
    # whether its earlier high/low occurred before or after entry.
    for rows in rows_by_tf.values():
        for bar in rows:
            if bar["ts"] < event["signal_at"]:
                continue
            stop_hit |= bar["low"] <= stop if sign == 1 else bar["high"] >= stop
            target_hit |= bar["high"] >= target if sign == 1 else bar["low"] <= target
    if stop_hit or target_hit:
        event.update(spent=True, spent_at=quote["observed_at"],
                     spent_reason="STRUCTURAL_BOTH_BARRIERS_OBSERVED" if stop_hit and target_hit else
                     "STRUCTURAL_STOP_ALREADY_REACHED" if stop_hit else "STRUCTURAL_TARGET_ALREADY_REACHED")


def build_context(raw, horizon, now=None, base_context=None, *, state=None, config=None):
    result = _build_context_impl(raw, horizon, now, base_context,
                                 state=state, config=config)
    # Invalid returns preserve the supplied diagnostic state. A successful
    # observation already owns a new detached state, so do not first clone a
    # complete old proof graph that would immediately be discarded.
    if result.get('quote_state') is None and state is not None:
        result['quote_state'] = deepcopy(state)
    return result


def _build_context_impl(raw, horizon, now=None, base_context=None, *, state=None, config=None):
    """Observe a quote against levels that were known at its exchange timestamp.

    Pass the returned ``quote_state`` into the next call for this exact source,
    contract, asset and horizon. Input state and raw data are never mutated.
    """
    raw = raw or {}
    end = TS.timestamp(datetime.now(timezone.utc) if now is None else now)
    horizon = str(horizon).lower()
    asset = str(raw.get("asset") or "")
    source = raw.get("structure_source_identity") or (base_context or {}).get("source_identity")
    out = {"version": VERSION, "asset": asset, "timeframe": horizon,
           "source_identity": deepcopy(source), "status": "INVALID",
           "reason": "STRUCTURAL_CONTEXT_INVALID", "event": None, "levels": [],
           "target_zones": [], "closed_at": None, "bars": 0, "quote_state": None}
    try:
        policy = _policy(config)
        seconds = TS.timeframe_seconds(horizon)
    except (KeyError, TypeError, ValueError):
        return out
    if end is None or not isinstance(source, dict) or not source.get("key"):
        return out
    out["policy"] = policy
    if not policy["enabled"]:
        return dict(out, reason="STRUCTURAL_QUOTE_ENTRY_DISABLED")
    quote_check, quote = _quote(dict(raw, horizon=horizon), end, source)
    out["quote_gate"] = quote_check
    if not quote:
        return dict(out, reason=quote_check["reason"])
    as_of = quote["observed_at"]
    previous = deepcopy(state or {})
    if previous and (previous.get("version") != VERSION or previous.get("asset") != asset
                     or previous.get("timeframe") != horizon
                     or not _same_source(previous.get("source_identity"), source)):
        return dict(out, reason="STRUCTURAL_STATE_SOURCE_MISMATCH")
    last_quote = previous.get("last_quote")
    if previous:
        if (not isinstance(last_quote, dict) or TS.timestamp(last_quote.get("observed_at")) is None
                or _number(last_quote.get("price")) is None or last_quote["price"] <= 0
                or not _same_source(source, last_quote.get("source_identity"))
                or not isinstance(previous.get("seen_level_ids"), list)):
            return dict(out, reason="STRUCTURAL_STATE_INVALID")
        active = previous.get("active_event")
        if active and not validate_event(active, source)["eligible"]:
            return dict(out, reason="STRUCTURAL_STATE_EVENT_INVALID")
        prior_leg = previous.get("protected_leg")
        if prior_leg:
            try:
                leg_anchor = prior_leg["protected_swing"]
                leg_atr = _atr(prior_leg["atr_proof"]["bars"], prior_leg["atr_proof"]["period"])
                leg_sign = 1 if prior_leg["direction"] == "LONG" else -1
                if (prior_leg["direction"] not in ("LONG", "SHORT")
                        or not _same_source(source, leg_anchor["source_identity"])
                        or not leg_atr or not math.isclose(leg_atr["value"], prior_leg["atr"], rel_tol=1e-10)
                        or not math.isclose(prior_leg["stop_price"], leg_anchor["price"] - leg_sign *
                                            policy["stop_buffer_atr"] * prior_leg["atr"], rel_tol=1e-12)):
                    return dict(out, reason="STRUCTURAL_STATE_LEG_INVALID")
            except (KeyError, TypeError, ValueError):
                return dict(out, reason="STRUCTURAL_STATE_LEG_INVALID")
    if last_quote and (last_quote["observed_at"] > as_of or
                       (last_quote["observed_at"] == as_of and last_quote["price"] != quote["price"])):
        return dict(out, reason="STRUCTURAL_QUOTE_OUT_OF_ORDER")
    facts, issue = _native_facts(raw, as_of, source, policy)
    if issue:
        return dict(out, reason=issue)
    rows_by_tf, levels, atr_by_tf = facts["rows_by_tf"], facts["levels"], facts["atr_by_tf"]
    trigger_rows = rows_by_tf.get(horizon) or []
    parent_tf = str(policy["parent_timeframe"])
    structural_tf = (parent_tf if seconds < TS.timeframe_seconds(parent_tf)
                     and atr_by_tf.get(parent_tf) else horizon)
    held_leg = previous.get("protected_leg") or {}
    if held_leg and not held_leg.get("invalidated"):
        # Additional newly loaded history cannot silently change the risk TF
        # of an already protected leg.
        structural_tf = held_leg["structural_timeframe"]
    structural_rows = rows_by_tf.get(structural_tf) or []
    atr = atr_by_tf.get(structural_tf)
    out.update(as_of=as_of, quote_observed_at=as_of, quote=deepcopy(quote),
               bars=len(trigger_rows), closed_at=trigger_rows[-1]["available_at"] if trigger_rows else None,
               structural_timeframe=structural_tf, atr_timeframe=structural_tf,
               structure_closed_at=structural_rows[-1]["available_at"] if structural_rows else None,
               levels=deepcopy(levels))
    if not trigger_rows or not atr:
        return dict(out, status="INSUFFICIENT", reason="STRUCTURAL_HISTORY_INSUFFICIENT")
    out.update(status="OK", reason="STRUCTURAL_WAIT_VERIFIED_CROSS", atr=atr["value"],
               structure_atr_proof=deepcopy(atr))
    next_state = previous or {"version": VERSION, "asset": asset, "timeframe": horizon,
                            "source_identity": deepcopy(source), "seen_level_ids": [],
                            "active_event": None, "protected_leg": None}
    seen = set(next_state.get("seen_level_ids") or [])
    # A cold process must not rediscover every old resistance that price traded
    # through days ago. With rolling quotes, only bars already known before the
    # previous observation may consume a level: a bar completed *between* two
    # verified quotes must not suppress the crossing those quotes establish.
    known_before = (max(last_quote["observed_at"], as_of - policy["minimum_entry_window_seconds"])
                    if last_quote else as_of)
    # A persisted observation from hours ago cannot make an already observed
    # intervening bar break fresh again. Session-open gaps remain possible when
    # no native bars traded through the level during the absence of quotes.
    # One historical scan per exact native bundle, not seven scans for seven
    # execution horizons. Compare its original availability to this lane's
    # known-before clock; a later bar never consumes a fresh quote crossing.
    seen.update(level_id for level_id, available in facts['first_cross_at'].items()
                if available <= known_before)
    event = deepcopy(next_state.get("active_event"))
    leg = deepcopy(next_state.get("protected_leg"))
    _spend(event, quote, rows_by_tf)
    if leg:
        sign = 1 if leg["direction"] == "LONG" else -1
        if sign * (quote["price"] - leg["stop_price"]) <= 0:
            leg["invalidated"] = True
        for bar in structural_rows:
            if bar["ts"] >= leg["started_at"] and (bar["low"] <= leg["stop_price"] if sign == 1
                                                     else bar["high"] >= leg["stop_price"]):
                leg["invalidated"] = True
    candidates = []
    same_observation = last_quote and last_quote["observed_at"] == as_of
    if not same_observation:
        for level in levels:
            if level["timeframe"] not in (horizon, structural_tf) or level["level_id"] in seen:
                continue
            reference = _reference(level, quote, last_quote, rows_by_tf[level["timeframe"]])
            if not reference:
                continue
            sign = 1 if level["kind"] == "resistance" else -1
            if sign * (reference["price"] - level["price"]) <= 0 < sign * (quote["price"] - level["price"]):
                # If no structural parent break was observed, a fast continuation
                # still needs the parent risk anchor; it cannot invent one.
                candidates.append((level, reference, "LONG" if sign == 1 else "SHORT"))
    candidates.sort(key=lambda x: (x[0]["timeframe"] == structural_tf,
                                   x[0]["pivot_at"], x[0]["available_at"]), reverse=True)
    for trigger, reference, direction in candidates:
        seen.add(trigger["level_id"])
        new_leg = _leg_for(direction, trigger, structural_tf, levels, atr, leg, quote, policy)
        if not new_leg:
            out["reason"] = "STRUCTURAL_PROTECTED_SWING_UNAVAILABLE"
            continue
        sign = 1 if direction == "LONG" else -1
        zones = _zone_cluster(levels, direction, trigger["price"], quote["price"],
                              new_leg["atr"], structural_tf, policy, rows_by_tf)
        if not zones:
            out["reason"] = "STRUCTURAL_TARGET_ZONE_UNAVAILABLE"
            continue
        selected = zones[:2]
        fractions = [policy["target_one_fraction"], 1.0 - policy["target_one_fraction"]] if len(selected) == 2 else [1.0]
        ladder = [dict(price=z["price"], fraction=f, zone_id=z["zone_id"],
                       timeframes=list(z["timeframes"]), available_at=z["available_at"],
                       kind="TP1" if i == 0 else "TP2") for i, (z, f) in enumerate(zip(selected, fractions))]
        old_leg_id = (leg or {}).get("leg_id")
        event = {"version": VERSION, "event_type": EVENT_TYPE, "asset": asset,
                 "direction": direction, "timeframe": horizon,
                 "trigger_timeframe": trigger["timeframe"], "structural_timeframe": structural_tf,
                 "stop_timeframe": structural_tf, "atr_timeframe": structural_tf,
                 "target_timeframe": "HISTORICAL_ZONES", "confirmation": "VERIFIED_QUOTE_CROSS",
                 "signal_at": as_of, "confirmed_at": as_of, "signal_price": quote["price"],
                 "trigger_level": trigger["price"], "trigger": deepcopy(trigger),
                 "reference": deepcopy(reference), "trigger_quote": deepcopy(quote),
                 "source_identity": deepcopy(source), "leg_id": new_leg["leg_id"],
                 "parent_event_id": new_leg.get("last_event_id"),
                 "phase": "CONTINUATION" if old_leg_id == new_leg["leg_id"] else "INITIAL_BREAKOUT",
                 "stop_anchor": new_leg["protected_swing"]["price"],
                 "protected_swing": deepcopy(new_leg["protected_swing"]),
                 "stop_price": new_leg["stop_price"], "atr": new_leg["atr"],
                 "atr_proof": deepcopy(new_leg["atr_proof"]),
                 "atr_observed_until": new_leg["atr_proof"]["observed_until"],
                 "target_price": ladder[0]["price"], "runner_target_price": ladder[-1]["price"],
                 "target_ladder": ladder, "target_zones": deepcopy(selected), "policy": deepcopy(policy),
                 "spent": False, "spent_reason": None, "spent_at": None,
                 "activity_basis": "VERIFIED_EXCHANGE_QUOTE", "activity_confirmed": True,
                 "session_gap": as_of - reference["observed_at"] > seconds,
                 "breakout_bar_at": trigger_rows[-1]["available_at"],
                 "trigger_pivot_at": trigger["pivot_at"], "level_available_at": trigger["available_at"],
                 "stop_pivot_at": new_leg["protected_swing"]["pivot_at"],
                 "stop_level_available_at": new_leg["protected_swing"]["available_at"]}
        event["event_id"] = _event_id(asset, horizon, source, trigger, new_leg["leg_id"])
        event["proof_hash"] = _seal(event)
        new_leg["last_event_id"] = event["event_id"]
        leg = new_leg
        out["reason"] = "STRUCTURAL_QUOTE_BREAKOUT_OBSERVED"
        _spend(event, quote, rows_by_tf)
        # Simultaneously crossed micro-levels are the same observed impulse;
        # never emit them later as fresh confirmations of this same quote.
        seen.update(item[0]["level_id"] for item in candidates)
        break
    # Keep consumption bounded by the native history window. Active/protected
    # evidence stays retained even if its candle has rolled out of that window.
    retained = {level["level_id"] for level in levels}
    if event:
        retained.add(event["trigger"]["level_id"])
    if leg:
        retained.add(leg["protected_swing"]["level_id"])
    next_state.update(last_quote=deepcopy(quote), seen_level_ids=sorted(seen & retained),
                      active_event=event, protected_leg=leg)
    out.update(event=deepcopy(event), quote_state=next_state,
               protected_leg=deepcopy(leg), target_zones=deepcopy((event or {}).get("target_zones") or []))
    if event:
        gate = entry_gate(out, quote["price"], event["direction"], end, config=policy)
        out["entry_gate"] = gate
        out["reason"] = gate["reason"]
    return out


def validate_event(event, source_identity=None):
    # Static evidence only. Current quote/time, spent barriers, entry expiry and
    # full portfolio risk remain independently checked on every decision.
    return SVC.verify(event, source_identity, version=VERSION,
                      defaults=DEFAULT_POLICY, validator=_validate_event_uncached)


def _validate_event_uncached(event, source_identity=None):
    """Check the frozen causal evidence, source, geometry and content digest."""
    event = event or {}
    fail = {"eligible": False, "reason": "STRUCTURAL_EVENT_PROOF_INVALID"}
    try:
        if event.get("version") != VERSION or event.get("event_type") != EVENT_TYPE:
            return fail
        if event.get("proof_hash") != _seal(event):
            return fail
        policy = _policy(event.get("policy"))
        source = event["source_identity"]
        if source_identity is not None and not _same_source(source_identity, source):
            return dict(fail, reason="SAME_TF_SOURCE_MISMATCH")
        direction = event["direction"]
        if direction not in ("LONG", "SHORT"):
            return fail
        sign = 1 if direction == "LONG" else -1
        signal = TS.timestamp(event["signal_at"])
        trigger, anchor, quote, reference = (event[k] for k in ("trigger", "protected_swing", "trigger_quote", "reference"))
        if trigger.get("kind") != ("resistance" if sign == 1 else "support"):
            return fail
        if signal is None or any(TS.timestamp(x) is None or TS.timestamp(x) > signal for x in (
                trigger["available_at"], anchor["available_at"], event["atr_observed_until"], reference["observed_at"])):
            return fail
        if quote["observed_at"] != signal or event["confirmed_at"] != signal:
            return fail
        if not all(_same_source(source, x["source_identity"]) for x in (trigger, anchor, quote, reference)):
            return dict(fail, reason="SAME_TF_SOURCE_MISMATCH")
        if not (sign * (reference["price"] - trigger["price"]) <= 0 < sign * (quote["price"] - trigger["price"])):
            return fail
        tf = event["structural_timeframe"]
        if (event["trigger_timeframe"] != trigger["timeframe"] or
                any(event[k] != tf for k in ("stop_timeframe", "atr_timeframe")) or anchor["timeframe"] != tf):
            return fail
        atr_proof = event["atr_proof"]
        calculated_atr = _atr(atr_proof["bars"], atr_proof["period"])
        if (not calculated_atr or calculated_atr["timeframe"] != tf or
                not math.isclose(calculated_atr["value"], event["atr"], rel_tol=1e-10) or
                calculated_atr["observed_until"] != event["atr_observed_until"]):
            return fail
        expected_stop = anchor["price"] - sign * policy["stop_buffer_atr"] * event["atr"]
        if not math.isclose(expected_stop, event["stop_price"], rel_tol=1e-12):
            return fail
        if event["event_id"] != _event_id(event["asset"], event["timeframe"], source, trigger, event["leg_id"]):
            return fail
        ladder, zones = event["target_ladder"], event["target_zones"]
        if not ladder or len(ladder) != len(zones) or not math.isclose(sum(x["fraction"] for x in ladder), 1.0):
            return fail
        last = trigger["price"]
        for step, zone in zip(ladder, zones):
            if (step["fraction"] <= 0 or step["zone_id"] != zone["zone_id"] or step["price"] != zone["price"]
                    or not zone["low"] <= step["price"] <= zone["high"] or zone["available_at"] > signal
                    or sign * (step["price"] - last) <= 0
                    or any(member["available_at"] > signal for member in zone["members"])):
                return fail
            last = step["price"]
        if event["target_price"] != ladder[0]["price"] or event["runner_target_price"] != ladder[-1]["price"]:
            return fail
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return fail
    return {"eligible": True, "reason": "STRUCTURAL_EVENT_PROOF_VALID"}


def context_gate(context, now=None):
    """Validate causal context and current quote, without entry/expiry rules.

    A session gap may leave the last closed bar old. Its original time remains
    visible; the fresh exchange quote is separately checked for execution.
    """
    context = context or {}
    fail = {"eligible": False, "reason": "STRUCTURAL_CONTEXT_INVALID"}
    if context.get("version") != VERSION or context.get("status") != "OK":
        return dict(fail, reason=context.get("reason") or fail["reason"])
    quote = context.get("quote") or {}
    source = context.get("source_identity")
    observed = TS.timestamp(context.get("quote_observed_at"))
    end = TS.timestamp(datetime.now(timezone.utc) if now is None else now)
    if (observed is None or end is None or quote.get("observed_at") != observed
            or not _same_source(source, quote.get("source_identity"))
            or quote.get("source_gate_pass") is not True or quote.get("market_open") is not True):
        return fail
    if any(TS.timestamp(value) is None or TS.timestamp(value) > observed for value in (
            context.get("closed_at"), context.get("structure_closed_at"))):
        return dict(fail, reason="STRUCTURAL_CONTEXT_FROM_FUTURE")
    fresh = quote_gate(datetime.fromtimestamp(observed, timezone.utc), context.get("timeframe"),
                       datetime.fromtimestamp(end, timezone.utc), execution=True, asset=context.get("asset"))
    if not fresh["eligible"]:
        return dict(fail, reason="EXECUTION_QUOTE_STALE", quote_time_gate=fresh)
    if context.get("event"):
        proof = validate_event(context["event"], source)
        if not proof["eligible"]:
            return dict(fail, reason=proof["reason"])
        if context["event"]["signal_at"] > observed:
            return dict(fail, reason="STRUCTURAL_QUOTE_PREDATES_EVENT")
    return {"eligible": True, "reason": "STRUCTURAL_CONTEXT_READY",
            "timeframe": context.get("timeframe"), "quote_observed_at": observed,
            "closed_at": context.get("closed_at"), "structure_closed_at": context.get("structure_closed_at"),
            "quote_time_gate": fresh}


def rebind_quote(context, rawquote, now=None):
    """Reprice certified context with one newer verified execution quote.

    This is an execution read, not a new structural observation. The event's
    trigger quote, first signal time, targets, proof and tracker are unchanged.
    The returned event may only become spent after a barrier is observed.
    """
    out = deepcopy(context or {})
    end = TS.timestamp(datetime.now(timezone.utc) if now is None else now)
    if out.get("version") != VERSION or out.get("status") != "OK" or end is None:
        return dict(out, status="INVALID", reason="STRUCTURAL_CONTEXT_INVALID")
    if out.get("event"):
        proof = validate_event(out["event"], out.get("source_identity"))
        if not proof["eligible"]:
            return dict(out, status="INVALID", reason=proof["reason"])
    supplied = dict(rawquote or {})
    if supplied.get("asset") not in (None, out.get("asset")):
        return dict(out, status="INVALID", reason="EXECUTION_QUOTE_ASSET_MISMATCH")
    supplied.update(asset=out.get("asset"), horizon=out.get("timeframe"))
    check, quote = _quote(supplied, end, out.get("source_identity"))
    if not quote:
        return dict(out, status="INVALID", reason=check["reason"], quote_gate=check)
    previous = out.get("quote") or {}
    old_time = TS.timestamp(out.get("quote_observed_at"))
    new_time = quote["observed_at"]
    if old_time is None or new_time < old_time or (new_time == old_time and quote["price"] != previous.get("price")):
        return dict(out, status="INVALID", reason="STRUCTURAL_QUOTE_OUT_OF_ORDER")
    if any(TS.timestamp(value) is None or TS.timestamp(value) > new_time for value in (
            out.get("closed_at"), out.get("structure_closed_at"))):
        return dict(out, status="INVALID", reason="STRUCTURAL_CONTEXT_FROM_FUTURE")
    out.update(as_of=new_time, quote_observed_at=new_time, quote=quote, quote_gate=check,
               execution_quote_rebound=True)
    if out.get("event"):
        _spend(out["event"], quote, {})
        gate = entry_gate(out, quote["price"], out["event"]["direction"], end)
        out.update(entry_gate=gate, reason=gate["reason"])
    return out


def management_structure(context, now=None):
    """Reconstruct recent confirmed parent swings from their native ATR bars.

    Management uses the current structural ATR and new parent pivots. The
    entry event's own ATR, protected swing and target ladder remain frozen.
    Entry expiry and target progress have no authority over an existing hold.
    """
    gate = context_gate(context, now)
    if not gate["eligible"]:
        return gate
    fail = {"eligible": False, "reason": "STRUCTURAL_PARENT_MANAGEMENT_PROOF_REQUIRED"}
    try:
        tf = context["structural_timeframe"]
        if tf != context["atr_timeframe"]:
            return fail
        policy = _policy(context.get("policy"))
        proof = context["structure_atr_proof"]
        if proof["timeframe"] != tf or proof["period"] != policy["atr_period"]:
            return fail
        rows = TS.closed_bars(proof["bars"], tf, context["quote_observed_at"])
        calculated = _atr(rows, proof["period"])
        if (not calculated or calculated["observed_until"] != context["structure_closed_at"]
                or calculated["observed_until"] != proof["observed_until"]
                or not math.isclose(calculated["value"], context["atr"], rel_tol=1e-10)
                or not math.isclose(calculated["value"], proof["value"], rel_tol=1e-10)):
            return fail
        return {"eligible": True, "reason": "STRUCTURAL_PARENT_MANAGEMENT_READY",
                "timeframe": tf, "atr": calculated["value"], "atr_proof": calculated,
                "closed_at": calculated["observed_until"], "policy": policy,
                "source_identity": deepcopy(context["source_identity"]),
                "levels": _levels(rows, context["source_identity"], policy)}
    except (KeyError, TypeError, ValueError, OverflowError):
        return fail


def validate_management_revision(revision, entry_event, opened_at, as_of=None):
    """Certify a persisted parent stop revision without altering entry proof."""
    fail = {"eligible": False, "reason": "STRUCTURAL_PARENT_REVISION_INVALID"}
    try:
        if not validate_event(entry_event)["eligible"]:
            return fail
        if (revision.get("management_rule") != "PROTECTED_PARENT_SWING"
                or revision.get("entry_event_id") != entry_event["event_id"]
                or revision.get("timeframe") != entry_event["structural_timeframe"]
                or not _same_source(revision.get("source_identity"), entry_event["source_identity"])):
            return fail
        observed, opened = TS.timestamp(revision["quote_observed_at"]), TS.timestamp(opened_at)
        if observed is None or opened is None or observed < opened:
            return fail
        if as_of is not None and (TS.timestamp(as_of) is None or observed > TS.timestamp(as_of)):
            return fail
        policy = _policy(revision["structural_policy"])
        proof, tf = revision["parent_atr_proof"], revision["timeframe"]
        rows = TS.closed_bars(proof["bars"], tf, observed)
        atr = _atr(rows, policy["atr_period"])
        if (not atr or atr["observed_until"] != revision["closed_at"]
                or not math.isclose(atr["value"], revision["atr"], rel_tol=1e-10)):
            return fail
        level = next((level for level in _levels(rows, entry_event["source_identity"], policy)
                      if level["level_id"] == revision["reference_level_id"]), None)
        sign = 1 if entry_event["direction"] == "LONG" else -1
        if (not level or level["kind"] != ("support" if sign == 1 else "resistance")
                or level["pivot_at"] < opened
                or level["prominence"] < policy["protected_swing_min_prominence_atr"] * atr["value"]
                or level["price"] != revision["reference_level"]
                or level["pivot_at"] != revision["reference_pivot_at"]
                or level["available_at"] != revision["level_available_at"]):
            return fail
        expected = level["price"] - sign * policy["stop_buffer_atr"] * atr["value"]
        if (not math.isclose(expected, revision["stop_price"], rel_tol=1e-12)
                or sign * (expected - entry_event["stop_price"]) <= 0):
            return fail
        return {"eligible": True, "reason": "STRUCTURAL_PARENT_REVISION_VALID",
                "anchor": level["price"], "stop_price": expected,
                "quote_observed_at": observed, "reference_level_id": level["level_id"]}
    except (KeyError, TypeError, ValueError, OverflowError):
        return fail


def entry_gate(context, price, direction, now, config=None):
    """Validate a frozen quote event at the current execution/decision clock."""
    context = context or {}
    event = context.get("event") or {}
    out = {"version": VERSION, "eligible": False, "reason": context.get("reason") or "STRUCTURAL_CONTEXT_REQUIRED",
           "timeframe": context.get("timeframe"), "event_id": event.get("event_id")}
    if context.get("status") != "OK" or not event:
        return out
    context_check = context_gate(context, now)
    if not context_check["eligible"]:
        return dict(out, reason=context_check["reason"])
    proof = validate_event(event, context.get("source_identity"))
    if not proof["eligible"]:
        return dict(out, reason=proof["reason"])
    if event.get("timeframe") != context.get("timeframe") or direction != event.get("direction"):
        return dict(out, reason="STRUCTURAL_DIRECTION_OR_TIMEFRAME_MISMATCH")
    end, observed = TS.timestamp(now), TS.timestamp(context.get("quote_observed_at"))
    if end is None or observed is None or observed < event["signal_at"]:
        return dict(out, reason="STRUCTURAL_QUOTE_PREDATES_EVENT")
    fresh = quote_gate(datetime.fromtimestamp(observed, timezone.utc), context.get("timeframe"),
                       datetime.fromtimestamp(end, timezone.utc), execution=True, asset=event["asset"])
    if not fresh["eligible"]:
        return dict(out, reason="EXECUTION_QUOTE_STALE", quote_time_gate=fresh)
    if event.get("spent"):
        return dict(out, reason=event.get("spent_reason") or "STRUCTURAL_EVENT_SPENT", spent_at=event.get("spent_at"))
    try:
        policy = _policy(dict(event["policy"], **(config or {})))
        seconds = TS.timeframe_seconds(event["timeframe"])
    except (KeyError, TypeError, ValueError):
        return dict(out, reason="STRUCTURAL_POLICY_INVALID")
    age = end - event["signal_at"]
    max_age = max(policy["minimum_entry_window_seconds"], policy["max_signal_age_bars"] * seconds)
    out.update(age_seconds=age, max_age_seconds=max_age)
    if age < 0 or age > max_age:
        return dict(out, reason="STRUCTURAL_EVENT_EXPIRED")
    px = _number(price)
    if px is None or px <= 0:
        return dict(out, reason="STRUCTURAL_EXECUTION_QUOTE_INVALID")
    sign = 1 if direction == "LONG" else -1
    trigger, stop, target, atr = (event[k] for k in ("trigger_level", "stop_price", "target_price", "atr"))
    if sign * (px - stop) <= 0:
        return dict(out, reason="STRUCTURAL_STOP_ALREADY_REACHED")
    if sign * (target - px) <= 0:
        return dict(out, reason="STRUCTURAL_TARGET_ALREADY_REACHED")
    if sign * (px - trigger) <= 0:
        return dict(out, reason="STRUCTURAL_BREAKOUT_LEVEL_NOT_HELD")
    progress = sign * (px - trigger) / (sign * (target - trigger))
    risk_atr = sign * (px - stop) / atr
    out.update(trigger_level=trigger, stop_price=stop, target_price=target,
               runner_target_price=event["runner_target_price"], target_ladder=deepcopy(event["target_ladder"]),
               atr=atr, extension_atr=sign * (px - trigger) / atr,
               target_progress=progress, max_target_progress=policy["max_target_progress"],
               stop_distance_atr=risk_atr, source_identity=deepcopy(event["source_identity"]),
               signal_at=event["signal_at"], trigger_timeframe=event["trigger_timeframe"],
               structural_timeframe=event["structural_timeframe"], stop_timeframe=event["stop_timeframe"],
               atr_timeframe=event["atr_timeframe"], phase=event["phase"])
    if progress > policy["max_target_progress"]:
        return dict(out, reason="STRUCTURAL_ENTRY_TOO_LATE_TO_TARGET")
    if risk_atr > policy["max_stop_atr"]:
        return dict(out, reason="STRUCTURAL_STOP_TOO_DISTANT")
    risk = sign * (px - stop) / px
    weighted = sum(step["fraction"] * sign * (step["price"] - px) / px for step in event["target_ladder"])
    return dict(out, eligible=True, reason="STRUCTURAL_VERIFIED_QUOTE_ENTRY_READY",
                stop_distance_pct=risk, remaining_move_pct=weighted,
                reward_risk=weighted / risk, target_distance_pct=sign * (target - px) / px,
                weighted_target_price=sum(step["fraction"] * step["price"] for step in event["target_ladder"]))
