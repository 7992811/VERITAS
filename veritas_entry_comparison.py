"""Offline, paired comparison of two causal entries on one structural event.

No database, network, portfolio mutation or promotion is reachable from here.
This deliberately fixes exits to isolate entry timing. It is not a replay of
the full live strategy. A missed entry is a zero opportunity outcome only when
its observation window is complete; censored observations stay unknown.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import random
from statistics import median

import veritas_timeframe_structure as S


VERSION = "PAIRED_STRUCTURAL_ENTRY_RESEARCH_V1"
VARIANTS = ("CONFIRMED_BREAKOUT", "FIRST_CONFIRMED_RETEST")
DEFAULT_RESEARCH_POLICY = {
    "retest_window_bars": 6,
    "retest_touch_zone_atr": 0.1,
    "retest_confirm_extension_atr": 0.05,
    "retest_invalid_close_atr": 0.15,
    "max_hold_bars": 48,
    "minimum_net_reward_risk": 1.15,
    "commission_per_side": 0.0004,
    "slippage_per_side": 0.0004,
    "stress_slippage_per_side": 0.0008,
    "minimum_move": 0.0019,
    "cost_multiple": 1.1,
    "funding_annual_rate": 0.16,
    "funding_free_seconds": 86400,
    "funding_year_days": 365.25,
    "bootstrap_seed": 20261007,
    "bootstrap_replicates": 2000,
}


def content_sha256(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _policy(config=None):
    out = dict(DEFAULT_RESEARCH_POLICY)
    if set(config or {}) - set(out):
        raise ValueError("unknown research policy setting")
    out.update(config or {})
    for key, value in out.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("non-numeric policy: " + key)
        if not math.isfinite(value) or value < 0:
            raise ValueError("invalid policy: " + key)
    for key in ("retest_window_bars", "max_hold_bars", "bootstrap_replicates"):
        if int(out[key]) != out[key] or not 1 <= out[key] <= 10000:
            raise ValueError("invalid bounded integer policy: " + key)
        out[key] = int(out[key])
    if (out["funding_year_days"] <= 0 or out["cost_multiple"] < 1
            or out["minimum_net_reward_risk"] < 1
            or out["stress_slippage_per_side"] < out["slippage_per_side"]
            or out["stress_slippage_per_side"] >= 1):
        raise ValueError("invalid cost/risk policy")
    return out


def validate_bars(records, timeframe, source_identity, as_of):
    """Strict source/contract/OHLC validation; no interpolation or repair.

    Native phase is preserved. Incomplete/future candles are excluded; malformed
    supplied observations and conflicting duplicates reject the dataset.
    Historical availability is an explicit assumption, not measured latency.
    """
    seconds = S.timeframe_seconds(timeframe)
    end = S.timestamp(as_of)
    if end is None or not isinstance(source_identity, dict):
        raise ValueError("explicit timestamp and source identity required")
    if any(not isinstance(source_identity.get(k), str) or not source_identity[k].strip()
           for k in ("key", "contract_id")):
        raise ValueError("source key and exact product/contract required")
    by_time = {}
    for raw in records:
        if not isinstance(raw, dict) or raw.get("source_identity") != source_identity:
            raise ValueError("source or contract mismatch")
        if (raw.get("synthetic") or raw.get("timeframe") != timeframe
                or str(raw.get("source") or "").upper() in S._SYNTHETIC_SOURCES):
            raise ValueError("synthetic or wrong-timeframe observation")
        if any(raw.get(k) is False for k in S._INCOMPLETE_FLAGS):
            continue
        t = S.timestamp(raw.get("ts"))
        available = S.timestamp(raw.get("available_at", t + seconds if t is not None else None))
        if t is None or available is None or available < t + seconds:
            raise ValueError("invalid candle availability")
        if available > end or t + seconds > end:
            continue
        out = {"ts": t, "available_at": available, "timeframe": timeframe,
               "source_identity": deepcopy(source_identity)}
        for key in ("open", "high", "low", "close", "volume"):
            value = raw.get(key, 0.0 if key == "volume" else None)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("non-numeric candle value: " + key)
            if not math.isfinite(value) or (value < 0 if key == "volume" else value <= 0):
                raise ValueError("invalid candle value: " + key)
            out[key] = float(value)
        if (out["low"] > min(out["open"], out["close"])
                or out["high"] < max(out["open"], out["close"])):
            raise ValueError("invalid OHLC ordering")
        if t in by_time and by_time[t] != out:
            raise ValueError("conflicting duplicate candle")
        by_time[t] = out
    return [by_time[t] for t in sorted(by_time)]


def structural_opportunities(rows, timeframe, asset, source_identity, structural_policy=None):
    """Streaming equivalent of shared structural event creation, O(n).

    Pivot certification and confirmation-bar invalidation use the existing pure
    structural module. The same pivot is consumed on its first crossing even if
    its geometry is not yet eligible. A data gap restarts warmup. Every event is
    returned, unlike build_context, which returns only the latest active event.
    """
    policy = S._policy(structural_policy)
    step = S.timeframe_seconds(timeframe)
    identity = json.dumps(S._source_token(source_identity), sort_keys=True,
                          separators=(",", ":"), ensure_ascii=True)
    period, left, right = (policy[k] for k in ("atr_period", "pivot_left", "pivot_right"))
    segment, ranges, latest, consumed = [], [], {}, set()
    events = []
    for absolute_i, bar in enumerate(rows):
        if segment and bar["ts"] != segment[-1]["ts"] + step:
            segment, ranges, latest, consumed = [], [], {}, set()
        previous = segment[-1] if segment else None
        ranges.append(None if previous is None else max(
            bar["high"] - bar["low"], abs(bar["high"] - previous["close"]),
            abs(bar["low"] - previous["close"])))
        segment.append(bar)
        i, j = len(segment) - 1, len(segment) - right - 2
        if j >= left:
            for kind in ("resistance", "support"):
                pivot = S._pivot(segment, j, left, right, kind)
                if pivot and pivot["available_at"] <= bar["ts"]:
                    latest[kind] = pivot
        if previous is None:
            continue
        atr = sum(ranges[i-period:i]) / period if i >= period + 1 else None
        atr_until = max(b["available_at"] for b in segment[i-period-1:i]) if atr else None
        for direction, sign, trigger_kind, stop_kind in (
                ("LONG", 1, "resistance", "support"),
                ("SHORT", -1, "support", "resistance")):
            trigger, opposite = latest.get(trigger_kind), latest.get(stop_kind)
            if not trigger:
                continue
            level = trigger["price"]
            key = (direction, trigger["pivot_at"])
            crossed = sign * (previous["close"] - level) <= 0 < sign * (bar["close"] - level)
            if not crossed or key in consumed:
                continue
            consumed.add(key)
            if not opposite or not atr or atr_until > bar["ts"]:
                continue
            stop = opposite["price"] - sign * policy["stop_buffer_atr"] * atr
            original_risk = sign * (level - stop)
            target = level + sign * max(policy["target_r_multiple"] * original_risk,
                                       policy["min_target_atr"] * atr)
            if original_risk <= 0 or stop <= 0 or target <= 0:
                continue
            signal_at = bar["ts"] + step
            event_key = "|".join(map(str, (S.VERSION, asset, timeframe, identity, direction,
                                          trigger["pivot_at"], opposite["pivot_at"], signal_at)))
            baseline_volume = median(b["volume"] for b in segment[i-period:i])
            event = {
                "version": S.VERSION, "event_id": "STF_" + hashlib.sha256(event_key.encode()).hexdigest()[:24],
                "asset": asset, "direction": direction, "timeframe": timeframe,
                "event_type": "SAME_TIMEFRAME_STRUCTURAL_BREAKOUT",
                "confirmation": "CLOSED_" + timeframe + "_BAR",
                "trigger_level": level, "signal_price": bar["close"],
                "signal_at": signal_at, "confirmed_at": bar["available_at"],
                "breakout_bar_at": bar["ts"], "trigger_pivot_at": trigger["pivot_at"],
                "level_available_at": trigger["available_at"],
                "stop_pivot_at": opposite["pivot_at"], "stop_level_available_at": opposite["available_at"],
                "stop_anchor": opposite["price"], "stop_price": stop, "target_price": target,
                "atr": atr, "atr_timeframe": timeframe, "stop_timeframe": timeframe,
                "target_timeframe": timeframe, "atr_observed_until": atr_until,
                "original_stop_distance_atr": original_risk / atr,
                "confirmation_extension_atr": sign * (bar["close"] - level) / atr,
                "source_identity": deepcopy(source_identity), "policy": dict(policy),
                "activity_basis": "SAME_TIMEFRAME_CLOSED_BREAKOUT", "activity_confirmed": True,
                "relative_volume": bar["volume"] / baseline_volume if baseline_volume > 0 else None,
                "volume_observed": baseline_volume > 0, "spent": False,
                "spent_reason": None, "spent_at": None,
            }
            S._spend_event(event, bar, confirmation=True)
            events.append({**event, "breakout_index": absolute_i})
            break
    return events


def _sign(event):
    return 1 if event["direction"] == "LONG" else -1


def _admission(event, quote, policy):
    """Geometry and net economics at the adverse next-open entry fill."""
    sign, atr, level = _sign(event), event["atr"], event["trigger_level"]
    stop, target, p = event["stop_price"], event["target_price"], event["policy"]
    slip, fee = policy["slippage_per_side"], policy["commission_per_side"]
    entry_fill, stop_fill, target_fill = quote * (1 + sign * slip), stop * (1 - sign * slip), target * (1 - sign * slip)
    extension = sign * (entry_fill - level) / atr
    if sign * (entry_fill - stop) <= 0:
        return {"eligible": False, "reason": "STOP_ALREADY_REACHED"}
    if sign * (target - entry_fill) <= 0:
        return {"eligible": False, "reason": "TARGET_ALREADY_REACHED"}
    if extension < 0 or extension > p["max_extension_atr"]:
        return {"eligible": False, "reason": "LEVEL_NOT_HELD" if extension < 0 else "ENTRY_EXTENDED"}
    if max(sign * (level - stop), sign * (entry_fill - stop)) / atr > p["max_stop_atr"]:
        return {"eligible": False, "reason": "STOP_TOO_DISTANT"}
    hold = policy["max_hold_bars"] * S.timeframe_seconds(event["timeframe"])
    funding = entry_fill * policy["funding_annual_rate"] * max(0, hold - policy["funding_free_seconds"]) / (policy["funding_year_days"] * 86400)
    risk = sign * (entry_fill - stop_fill) + fee * (entry_fill + stop_fill) + funding
    reward = sign * (target_fill - entry_fill) - fee * (entry_fill + target_fill) - funding
    costs = max(2 * (fee + slip),
                (abs(entry_fill - quote) + abs(target_fill - target)
                 + fee * (entry_fill + target_fill) + funding) / quote)
    move = sign * (target - quote) / quote
    floor = max(policy["minimum_move"], policy["cost_multiple"] * costs)
    details = {"entry_quote": quote, "entry_fill": entry_fill, "initial_net_risk_price": risk,
               "target_net_reward_price": reward, "net_reward_risk": reward / risk if risk > 0 else None,
               "modeled_cost_pct": costs, "remaining_move_pct": move,
               "minimum_move_pct": floor, "extension_atr": extension}
    if move < floor:
        return dict(details, eligible=False, reason="MOVE_BELOW_COST_FLOOR")
    if risk <= 0 or reward <= 0 or reward / risk < policy["minimum_net_reward_risk"]:
        return dict(details, eligible=False, reason="NET_REWARD_RISK_BELOW_FLOOR")
    return dict(details, eligible=True, reason="READY")


def select_entry(rows, event, variant, policy=None):
    """Choose an entry using only candles available by its confirmation time."""
    policy = _policy(policy)
    if variant not in VARIANTS:
        raise ValueError("exactly the two predeclared entry variants are supported")
    empty = {"status": "NO_ENTRY", "opportunity_r": 0.0}
    if event.get("spent"):
        return dict(empty, reason=event.get("spent_reason") or "EVENT_SPENT")
    step, sign = S.timeframe_seconds(event["timeframe"]), _sign(event)
    start = event["breakout_index"]
    confirmation_i = start
    if variant == VARIANTS[1]:
        confirmation_i = None
        for j in range(start + 1, start + policy["retest_window_bars"] + 1):
            if j >= len(rows):
                return {"status": "PENDING", "reason": "RETEST_WINDOW_NOT_OBSERVED"}
            bar = rows[j]
            if bar["ts"] != rows[j - 1]["ts"] + step:
                return {"status": "CENSORED", "reason": "DATA_GAP_BEFORE_RETEST"}
            if bar["available_at"] < event["confirmed_at"]:
                return dict(empty, reason="BREAKOUT_CONFIRMATION_DELIVERED_LATE")
            stop_hit = bar["low"] <= event["stop_price"] if sign == 1 else bar["high"] >= event["stop_price"]
            target_hit = bar["high"] >= event["target_price"] if sign == 1 else bar["low"] <= event["target_price"]
            if stop_hit or target_hit:
                return dict(empty, reason="BOTH_BARRIERS_BEFORE_RETEST" if stop_hit and target_hit else
                            "STOP_BEFORE_RETEST" if stop_hit else "TARGET_BEFORE_RETEST")
            level, atr = event["trigger_level"], event["atr"]
            close_extension = sign * (bar["close"] - level) / atr
            if close_extension < -policy["retest_invalid_close_atr"]:
                return dict(empty, reason="RETEST_CLOSE_INVALIDATED")
            touched = (bar["low"] <= level + policy["retest_touch_zone_atr"] * atr if sign == 1
                       else bar["high"] >= level - policy["retest_touch_zone_atr"] * atr)
            confirmed = (close_extension >= policy["retest_confirm_extension_atr"]
                         and close_extension <= event["policy"]["max_extension_atr"]
                         and sign * (bar["close"] - bar["open"]) > 0)
            if touched and confirmed:
                confirmation_i = j
                break
        if confirmation_i is None:
            return dict(empty, reason="NO_CONFIRMED_RETEST_IN_WINDOW")
    entry_i = confirmation_i + 1
    if entry_i >= len(rows):
        return {"status": "PENDING", "reason": "NEXT_OPEN_NOT_OBSERVED"}
    if rows[entry_i]["ts"] != rows[confirmation_i]["ts"] + step:
        return {"status": "CENSORED", "reason": "DATA_GAP_AT_ENTRY"}
    known_at = max(event["confirmed_at"], rows[confirmation_i]["available_at"])
    if known_at > rows[entry_i]["ts"]:
        return dict(empty, reason="CONFIRMATION_DELIVERED_AFTER_NEXT_OPEN")
    admission = _admission(event, rows[entry_i]["open"], policy)
    if not admission["eligible"]:
        return dict(empty, **admission)
    return dict(admission, status="ENTERED", entry_index=entry_i,
                entry_at=rows[entry_i]["ts"], confirmation_at=known_at,
                initial_event_at=event["signal_at"],
                stop_price=event["stop_price"], target_price=event["target_price"],
                timeframe=event["timeframe"], source_identity=deepcopy(event["source_identity"]))


def _trade_result(entry, event, exit_quote, exit_at, reason, funding, policy, ambiguous=False):
    sign, fee = _sign(event), policy["commission_per_side"]
    risk = entry["initial_net_risk_price"]
    outcomes = {}
    for name, slip in (("base", policy["slippage_per_side"]), ("stress", policy["stress_slippage_per_side"])):
        entry_fill = entry["entry_quote"] * (1 + sign * slip)
        exit_fill = exit_quote * (1 - sign * slip)
        gross = sign * (exit_fill - entry_fill)
        fees = fee * (entry_fill + exit_fill)
        net = gross - fees - funding
        outcomes[name] = {"entry_fill": entry_fill, "exit_fill": exit_fill,
                          "gross_r": gross / risk, "commission_r": fees / risk,
                          "funding_r": funding / risk, "net_r": net / risk,
                          "net_return_pct": net / entry_fill}
    return dict(entry, status="CLOSED", reason=reason, exit_at=exit_at, exit_quote=exit_quote,
                hold_seconds=exit_at - entry["entry_at"],
                ambiguous_barrier_order=ambiguous, opportunity_r=outcomes["base"]["net_r"],
                base=outcomes["base"], stress=outcomes["stress"])


def replay_entry(rows, event, entry, policy=None):
    """Fixed barriers, next-open time exit, conservative order and funding."""
    policy = _policy(policy)
    if entry["status"] != "ENTERED":
        return deepcopy(entry)
    start, sign, funding = entry["entry_index"], _sign(event), 0.0
    step = S.timeframe_seconds(event["timeframe"])
    stop, target = event["stop_price"], event["target_price"]
    for i in range(start, min(len(rows), start + policy["max_hold_bars"] + 1)):
        bar = rows[i]
        if i > start and bar["ts"] != rows[i - 1]["ts"] + step:
            return dict(entry, status="CENSORED", reason="DATA_GAP_DURING_POSITION",
                        last_observed_at=rows[i - 1]["available_at"])
        if i == start + policy["max_hold_bars"]:
            return _trade_result(entry, event, bar["open"], bar["ts"], "TIME_EXIT_NEXT_OPEN", funding, policy)
        stop_at_open = sign * (bar["open"] - stop) <= 0
        target_at_open = sign * (bar["open"] - target) >= 0
        if stop_at_open or target_at_open:
            quote = min(bar["open"], stop) if sign == 1 and stop_at_open else max(bar["open"], stop) if stop_at_open else target
            return _trade_result(entry, event, quote, bar["ts"], "STOP_GAP" if stop_at_open else "TARGET_AT_OPEN", funding, policy)
        stop_hit = bar["low"] <= stop if sign == 1 else bar["high"] >= stop
        target_hit = bar["high"] >= target if sign == 1 else bar["low"] <= target
        # OHLC gives no exact intrabar execution time. Charge the whole final
        # observed interval, with current opening notional, after the free day.
        free_until = entry["entry_at"] + policy["funding_free_seconds"]
        billable = max(0.0, bar["ts"] + step - max(bar["ts"], free_until))
        funding += bar["open"] * policy["funding_annual_rate"] * billable / (policy["funding_year_days"] * 86400)
        if stop_hit or target_hit:
            return _trade_result(entry, event, stop if stop_hit else target, bar["ts"] + step,
                                 "STOP_AND_TARGET_AMBIGUOUS_STOP_FIRST" if stop_hit and target_hit else
                                 "STOP" if stop_hit else "TARGET", funding, policy, stop_hit and target_hit)
    return dict(entry, status="PENDING", reason="POSITION_END_NOT_OBSERVED",
                last_observed_at=rows[-1]["available_at"] if rows else None)


def compare_dataset(records, *, asset, timeframe, source_identity, evaluation_start,
                    holdout_start, end, research_policy=None, structural_policy=None):
    """Evaluate exactly two frozen variants; never fit or promote anything."""
    policy = _policy(research_policy)
    rows = validate_bars(records, timeframe, source_identity, end)
    start, split, cutoff = (S.timestamp(x) for x in (evaluation_start, holdout_start, end))
    if any(x is None for x in (start, split, cutoff)) or not start < split < cutoff:
        raise ValueError("strict chronological evaluation/holdout/end required")
    step = S.timeframe_seconds(timeframe)
    # Latest retest close + next-open entry + maximum hold; an extra full bar
    # makes the development embargo conservative and independent of outcomes.
    guard_seconds = (policy["retest_window_bars"] + policy["max_hold_bars"] + 1) * step
    events = structural_opportunities(rows, timeframe, asset, source_identity, structural_policy)
    pairs, purged = [], 0
    for event in events:
        t = event["signal_at"]
        if not start <= t < cutoff:
            continue
        if t < split and t + guard_seconds >= split:
            purged += 1
            continue
        outcomes = {variant: replay_entry(rows, event, select_entry(rows, event, variant, policy), policy)
                    for variant in VARIANTS}
        pairs.append({"opportunity_id": event["event_id"], "asset": asset,
                      "timeframe": timeframe, "signal_at": t, "direction": event["direction"],
                      "source_identity": deepcopy(source_identity),
                      "period": "development" if t < split else "holdout",
                      "trigger_level": event["trigger_level"], "stop_price": event["stop_price"],
                      "target_price": event["target_price"], "atr": event["atr"],
                      "outcomes": outcomes})
    gaps = sum(b["ts"] != a["ts"] + step for a, b in zip(rows, rows[1:]))
    return {"asset": asset, "timeframe": timeframe, "source_identity": deepcopy(source_identity),
            "observed_bars": len(rows), "gap_count": gaps, "data_sha256": content_sha256(rows),
            "first_bar_at": rows[0]["ts"] if rows else None,
            "last_closed_at": rows[-1]["available_at"] if rows else None,
            "purged_boundary_opportunities": purged, "opportunities": pairs}


def _quantile(values, fraction):
    values = sorted(values)
    at = fraction * (len(values) - 1)
    lo, hi = math.floor(at), math.ceil(at)
    return values[lo] + (at - lo) * (values[hi] - values[lo])


def summarize_pairs(pairs, policy=None):
    """Descriptive opportunity statistics with UTC-day paired resampling.

    Financial records count separately from complete paired comparisons. A
    bootstrap interval is not a promotion gate or proof of independent trials.
    """
    policy = _policy(policy)
    complete = [p for p in pairs if all(p["outcomes"][v]["status"] in ("CLOSED", "NO_ENTRY") for v in VARIANTS)]
    result = {"opportunities": len(pairs), "complete_pairs": len(complete),
              "pending_or_censored_pairs": len(pairs) - len(complete), "variants": {}}
    days = defaultdict(list)
    for pair in complete:
        day = datetime.fromtimestamp(pair["signal_at"], timezone.utc).date().isoformat()
        values = [pair["outcomes"][v].get("opportunity_r", 0.0) for v in VARIANTS]
        days[day].append(values[1] - values[0])
    for variant in VARIANTS:
        all_rows = [p["outcomes"][variant] for p in pairs]
        matched = [p["outcomes"][variant] for p in complete]
        closed = [x for x in matched if x["status"] == "CLOSED"]
        net = [x["base"]["net_r"] for x in closed]
        stress = [x["stress"]["net_r"] for x in closed]
        positive, negative = sum(x for x in net if x > 0), -sum(x for x in net if x < 0)
        result["variants"][variant] = {
            "status_counts_all": dict(Counter(x["status"] for x in all_rows)),
            "no_entry_reasons_all": dict(Counter(x["reason"] for x in all_rows if x["status"] == "NO_ENTRY")),
            "paired_closed_trades": len(closed), "paired_wins": sum(x > 0 for x in net),
            "paired_win_rate": sum(x > 0 for x in net) / len(net) if net else None,
            "paired_net_r_sum": sum(net), "paired_mean_trade_r": sum(net) / len(net) if net else None,
            "mean_opportunity_r": sum(net) / len(complete) if complete else None,
            "stress_net_r_sum_same_trades": sum(stress),
            "stress_mean_opportunity_r_same_trades": sum(stress) / len(complete) if complete else None,
            "profit_factor_r": positive / negative if negative else None,
            "commission_r_sum": sum(x["base"]["commission_r"] for x in closed),
            "funding_r_sum": sum(x["base"]["funding_r"] for x in closed),
            "ambiguous_barrier_trades": sum(x.get("ambiguous_barrier_order", False) for x in closed),
        }
    differences = [x for values in days.values() for x in values]
    interval = None
    if len(days) >= 2:
        rng = random.Random(policy["bootstrap_seed"])
        clusters = list(days.values())
        estimates = []
        for _ in range(policy["bootstrap_replicates"]):
            sample = [clusters[rng.randrange(len(clusters))] for _ in clusters]
            estimates.append(sum(sum(x) for x in sample) / sum(len(x) for x in sample))
        interval = [_quantile(estimates, 0.025), _quantile(estimates, 0.975)]
    result["paired_retest_minus_breakout"] = {
        "mean_opportunity_r_difference": sum(differences) / len(differences) if differences else None,
        "utc_day_clusters": len(days), "descriptive_cluster_bootstrap_95pct": interval,
        "status": "DESCRIPTIVE_ONLY_NO_EDGE_OR_PROMOTION_CLAIM",
    }
    return result
