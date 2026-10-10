"""Causal intrabar event-impulse and news-verification helpers.

The execution path never waits for a headline. A verified senior structural
cross plus a forming-price volatility shock can authorize the first entry.
News is checked in parallel from already-verified upstream evidence and affects
hold/add conviction, not the existence of the causal price event.
"""
from __future__ import annotations

import math

VERSION = "EVENT_IMPULSE_INTRABAR_V2"
TIMEFRAMES = ("1m", "5m", "1h")
FAST_TIMEFRAMES = ("1m", "5m")
NEWS_SCORE_THRESHOLD = 0.65


def _num(value, default=None):
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _atr_value(value):
    if isinstance(value, dict):
        value = value.get("value")
    x = _num(value)
    return x if x is not None and x > 0 else None


def quote_shock(last_closed_price, quote_price, atr_by_tf, thresholds=None):
    """Compare the forming move with ATRs completed before the quote.

    GAME_CHANGER_EXTREME requires a completed 1h ATR, at least one completed
    fast ATR, and a move that clears every available required threshold.
    EVENT_IMPULSE requires both completed 1m and 5m ATRs to be exceeded.
    Missing volatility evidence never counts as a pass.
    """
    base, px = _num(last_closed_price), _num(quote_price)
    limits = {"1m": 1.0, "5m": 1.0, "1h": 1.0}
    limits.update({k: float(v) for k, v in (thresholds or {}).items() if k in limits})
    out = {
        "version": VERSION, "eligible": False, "severity": "NONE",
        "last_closed_price": base, "quote_price": px, "move_points": None,
        "ratios": {}, "exceeds": {}, "available_timeframes": [],
        "minimum_ratios": limits,
    }
    if base is None or px is None or base <= 0 or px <= 0:
        return dict(out, reason="INTRABAR_PRICE_INVALID")
    move = abs(px - base)
    ratios, exceeds = {}, {}
    for tf in TIMEFRAMES:
        atr = _atr_value((atr_by_tf or {}).get(tf))
        if atr is None:
            continue
        ratios[tf] = move / atr
        exceeds[tf] = ratios[tf] >= limits[tf]
    available = [tf for tf in TIMEFRAMES if tf in ratios]
    fast_available = [tf for tf in FAST_TIMEFRAMES if tf in ratios]
    out.update(move_points=move, ratios=ratios, exceeds=exceeds,
               available_timeframes=available, coverage=len(available) / len(TIMEFRAMES))
    extreme = bool(
        "1h" in ratios and exceeds.get("1h") is True and fast_available
        and all(exceeds.get(tf) is True for tf in fast_available)
    )
    fast = bool(all(tf in ratios and exceeds.get(tf) is True for tf in FAST_TIMEFRAMES))
    if extreme:
        return dict(out, eligible=True, severity="GAME_CHANGER_EXTREME",
                    reason="FORMING_MOVE_EXCEEDS_COMPLETED_SENIOR_VOLATILITY")
    if fast:
        return dict(out, eligible=True, severity="EVENT_IMPULSE",
                    reason="FORMING_MOVE_EXCEEDS_COMPLETED_FAST_VOLATILITY")
    return dict(out, reason="INTRABAR_VOLATILITY_SHOCK_NOT_CONFIRMED")


def news_check(row, direction, now=None):
    """Classify already-verified news/catalyst evidence without network I/O.

    A PENDING result explicitly requests the asynchronous news layer to search
    immediately. Signed event_shadow_score is directional: opposite news is a
    conflict, never a confirmation through abs(score).
    """
    row = row or {}
    direction = str(direction or "")
    sign = 1.0 if direction == "LONG" else -1.0 if direction == "SHORT" else 0.0
    score = _num(row.get("event_shadow_score"), 0.0) or 0.0
    explicit = row.get("event_news_verification") or row.get("news_verification") or {}
    status = str(explicit.get("status") or "").upper() if isinstance(explicit, dict) else ""
    explicit_direction = str(explicit.get("direction") or "") if isinstance(explicit, dict) else ""
    source = None
    catalyst = None
    if status in ("CONFIRMED", "ALIGNED") and explicit_direction in ("", direction):
        result = "CONFIRMED"; source = "EXPLICIT_VERIFIED_NEWS"
    elif status in ("CONFLICT", "CONTRADICTED") or (
            explicit_direction in ("LONG", "SHORT") and explicit_direction != direction):
        result = "CONFLICT"; source = "EXPLICIT_VERIFIED_NEWS"
    else:
        try:
            from veritas_event_catalyst import active_catalyst
            catalyst = active_catalyst(row, direction, now)
        except Exception:
            catalyst = None
        if catalyst:
            result = "CONFIRMED"; source = "VERIFIED_EVENT_CATALYST"
        elif bool(row.get("news_catalyst_confirmed") or row.get("event_news_confirmation")):
            result = "CONFIRMED"; source = "UPSTREAM_NEWS_CONFIRMATION"
        elif sign and score * sign >= NEWS_SCORE_THRESHOLD:
            result = "CONFIRMED"; source = "SIGNED_EVENT_SHADOW"
        elif sign and score * sign <= -NEWS_SCORE_THRESHOLD:
            result = "CONFLICT"; source = "SIGNED_EVENT_SHADOW"
        else:
            result = "PENDING"; source = None
    return {
        "required": True, "requested": result == "PENDING", "status": result,
        "confirmed": result == "CONFIRMED", "conflict": result == "CONFLICT",
        "direction": direction, "event_shadow_score": score, "source": source,
        "catalyst": catalyst, "blocking_first_entry": False,
        "role": "HOLD_SCALE_AND_REVERSAL_RISK_CONFIRMATION",
        "priority": "IMMEDIATE_NON_BLOCKING",
    }


def _config(cfg=None):
    if cfg is not None:
        return cfg
    try:
        import veritas_canonical_constitution as CTC
        return ((getattr(CTC, "TREND_ACCELERATION_POLICY", {}) or {}).get("event_impulse") or {})
    except Exception:
        return {}


def _context(row):
    row = row or {}
    plan = row.get("trade_plan") or {}
    return (row.get("timeframe_entry_context") or plan.get("timeframe_entry_context")
            or row.get("trend_entry_context") or plan.get("trend_entry_context") or {})


def assess(row, direction, cfg=None, now=None):
    """Cross-asset event-impulse assessment used by paper and Currency admission."""
    cfg = _config(cfg)
    row = row or {}
    out = {"eligible": False, "reason": "EVENT_IMPULSE_NOT_CONFIRMED",
           "owner_priority": cfg.get("owner_priority")}
    if not cfg.get("enabled") or direction not in ("LONG", "SHORT"):
        return out
    plan = row.get("trade_plan") or {}
    integrity = plan.get("trade_integrity") or {}
    if integrity.get("hard_invalidation"):
        return dict(out, reason="EVENT_IMPULSE_THESIS_INVALID")

    event = (_context(row).get("event") or {})
    event_direction = str(event.get("direction") or "")
    event_type = str(event.get("event_type") or "")
    breakout = bool(event_direction == direction and
                    ("BREAKOUT" in event_type or "CONTINUATION" in event_type))
    shock = event.get("intrabar_volatility_shock") or {}
    news_clock = now if now is not None else event.get("signal_at")
    news = news_check(row, direction, news_clock)
    game_changer = bool(
        breakout and event.get("game_changer_extreme") is True
        and event.get("senior_level_break") is True
        and shock.get("severity") == "GAME_CHANGER_EXTREME"
        and shock.get("eligible") is True
    )
    if game_changer:
        return {
            "eligible": True, "reason": "GAME_CHANGER_EXTREME_CONFIRMED",
            "owner_priority": cfg.get("owner_priority"), "direction": direction,
            "regime": "INTRABAR_GAME_CHANGER",
            "signal_tier": str(row.get("signal_tier") or direction),
            "breakout": True, "activity_confirmed": bool(event.get("activity_confirmed")),
            "volatility_expansion": True, "intrabar_volatility_shock": shock,
            "senior_level_break": True, "game_changer_extreme": True,
            "immediate_max": bool(cfg.get("game_changer_immediate_max", True) and not news["conflict"]),
            "forming_bar_close_required": bool(cfg.get("forming_bar_close_required", False)),
            "news_confirmed": news["confirmed"], "news_conflict": news["conflict"],
            "news_check": news, "news_check_required": True,
            "entry_requires_news": bool(cfg.get("entry_requires_news", False)),
            "scale_allowed": not news["conflict"],
            "defer_fixed_take_profit": bool(cfg.get("defer_fixed_take_profit", True)),
            "target_reference_mode": cfg.get("target_reference_mode"),
            "event_id": event.get("event_id"), "event_type": event_type,
        }

    regime = str(row.get("regime") or "")
    if regime not in set(cfg.get("accepted_regimes") or ()):
        return dict(out, reason="EVENT_IMPULSE_HIGH_VOL_REGIME_REQUIRED", regime=regime)
    tier = str(row.get("signal_tier") or row.get("execution_signal_tier") or "")
    direction_tiers = ("LONG", "SUPER_LONG") if direction == "LONG" else ("SHORT", "SUPER_SHORT")
    if tier not in direction_tiers or tier not in set(cfg.get("accepted_fast_tiers") or ()):
        return dict(out, reason="EVENT_IMPULSE_DIRECTIONAL_SIGNAL_REQUIRED", signal_tier=tier)
    hs = row.get("horizon_structure") or {}
    hs_direction = str(hs.get("direction") or row.get("horizon_structure_direction") or "")
    score = _num(hs.get("score"), _num(row.get("horizon_structure_score"), 0.0)) or 0.0
    if hs_direction not in ("", direction) or score < float(cfg.get("minimum_structure_score") or .90):
        return dict(out, reason="EVENT_IMPULSE_STRUCTURE_REQUIRED",
                    structure_direction=hs_direction, structure_score=score)
    inst = row.get("institutional_signal") or {}
    try:
        evidence = int(row.get("independent_evidence_families")
                       or ((inst.get("evidence_independence") or {}).get("independent_count")) or 0)
    except Exception:
        evidence = 0
    if evidence < int(cfg.get("minimum_independent_evidence") or 4):
        return dict(out, reason="EVENT_IMPULSE_EVIDENCE_INSUFFICIENT", evidence=evidence)
    if cfg.get("breakout_required") and not breakout:
        return dict(out, reason="EVENT_IMPULSE_BREAKOUT_REQUIRED",
                    event_type=event_type, event_direction=event_direction)
    st, ti = row.get("intraday_structure") or {}, row.get("trend_impulse") or {}
    relvol = _num(st.get("relative_volume"), _num(ti.get("relative_volume"), row.get("relative_volume")))
    activity = bool(st.get("volume_confirmed") or st.get("activity_confirmed")
                    or ti.get("volume_confirmed") or ti.get("activity_confirmed"))
    if relvol is not None and relvol >= float(cfg.get("relative_volume_floor_if_available") or 1.25):
        activity = True
    if cfg.get("activity_or_volume_confirmation_required") and not activity:
        return dict(out, reason="EVENT_IMPULSE_ACTIVITY_CONFIRMATION_REQUIRED", relative_volume=relvol)
    volatility = bool("HIGH_VOL" in regime or ti.get("volatility_expansion") or st.get("volatility_expansion"))
    if cfg.get("volatility_expansion_required") and not volatility:
        return dict(out, reason="EVENT_IMPULSE_VOLATILITY_EXPANSION_REQUIRED")
    return {
        "eligible": True, "reason": "EVENT_IMPULSE_CONFIRMED",
        "owner_priority": cfg.get("owner_priority"), "direction": direction,
        "regime": regime, "signal_tier": tier, "structure_score": score,
        "evidence": evidence, "breakout": breakout, "activity_confirmed": activity,
        "relative_volume": relvol, "volatility_expansion": volatility,
        "news_confirmed": news["confirmed"], "news_conflict": news["conflict"],
        "news_check": news, "news_check_required": True,
        "entry_requires_news": bool(cfg.get("entry_requires_news", False)),
        "scale_allowed": not news["conflict"],
        "defer_fixed_take_profit": bool(cfg.get("defer_fixed_take_profit", True)),
        "target_reference_mode": cfg.get("target_reference_mode"),
        "event_id": event.get("event_id"), "event_type": event_type,
    }


def _temporary_cap(policy):
    try:
        import veritas_canonical_constitution as CTC
        cfg = getattr(CTC, "TREND_ACCELERATION_POLICY", {}) or {}
        return (cfg.get("temporary_caps") or {}).get(str((policy or {}).get("mode") or "")) or {}
    except Exception:
        return {}


def effective_cap(row, policy, base_cap):
    direction = str((row or {}).get("research_decision") or (row or {}).get("decision") or "")
    impulse = assess(row, direction)
    if not (impulse.get("eligible") and impulse.get("immediate_max")):
        return float(base_cap)
    cap = _temporary_cap(policy)
    return max(float(base_cap), float(cap.get("max_fraction") or base_cap))


def requested_fraction(row, policy, risk_governor, current, cap, *, soft=False):
    """Request max only in NORMAL risk and only for a clean causal admission."""
    if soft or (risk_governor or {}).get("state") != "NORMAL":
        return float(current)
    direction = str((row or {}).get("research_decision") or (row or {}).get("decision") or "")
    impulse = assess(row, direction)
    return float(cap) if impulse.get("eligible") and impulse.get("immediate_max") else float(current)


def position_cap(row, policy, base_cap):
    return effective_cap(row, policy, base_cap)
