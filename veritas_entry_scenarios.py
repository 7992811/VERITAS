"""Compose independent native-TF entry scenarios without changing their events."""
from copy import deepcopy
from datetime import datetime, timezone
import veritas_canonical_constitution as CTC
import veritas_daily_averages as DA
import veritas_timeframe_structure as TS


def daily_context(raw, now=None):
    clock = datetime.now(timezone.utc) if now is None else now
    return DA.build_context(raw.get("native_daily_bars") or [], clock,
        asset=raw.get("asset") or "",
        source_identity=raw.get("structure_source_identity"),
        config={"atr_period": CTC.STRUCTURAL_ENTRY_POLICY["atr_period"]})


def daily_features(raw, now=None):
    context = daily_context(raw, now)
    price = TS._number(raw.get("price"))
    out = {"status": context["status"], "price": price, "daily_ma_context": context,
           "daily_ma_status": context["status"], "ma_timeframe": "1d",
           "ma_source_identity": context["source_identity"], "ma_daily_asof": context["daily_asof"],
           "ma_native_daily": True}
    for period in (18, 50, 200):
        record = context["periods"][str(period)]
        value = record["value"]
        out["sma"+str(period)] = value
        out["sma"+str(period)+"_slope"] = record["slope"]
        out["price_vs_sma"+str(period)] = price/value-1 if price and value else None
    # Retain the existing trend weights, now from genuine native daily closes.
    bias = 0.
    for period, weight in ((18,.35), (50,.25)):
        value = out["sma"+str(period)]
        if value and price:
            bias += weight if price>value else -weight
        slope = out["sma"+str(period)+"_slope"]
        if slope is not None:
            bias += .10 if slope>0 else -.10 if slope<0 else 0.
    if out["sma18"] and out["sma50"]:
        bias += .20 if out["sma18"]>out["sma50"] else -.20
    out["trend_bias"] = round(max(-1., min(1., bias)), 4)
    return out


def _assess(context, raw, horizon, clock):
    import veritas_timeframe_policy as TFP
    import veritas_execution as VX
    import veritas_price_source as VPS
    quote = VPS.quote_from_row(raw)
    price = quote.get("price") if quote.get("price") is not None else raw.get("price")
    event = context.get("event") or {}
    direction = event.get("direction")
    row = dict(raw, price=price, horizon=horizon, research_decision=direction,
               timeframe_entry_context=context, trade_plan={})
    timing = TFP.entry_gate(row, price, direction, clock)
    economics = {}
    if timing.get("eligible"):
        plan = TFP.prepare_row(row, price=price, now=clock)["trade_plan"]
        for key in ("best_bid", "best_ask", "spread_bps"):
            value = quote.get(key) if quote.get(key) is not None else raw.get(key)
            if value is not None:
                plan[key] = value
        economics = VX.economics_gate(raw.get("asset"), dict(plan, direction=direction))
        fill = economics.get("modeled_entry_fill")
        if fill:
            filled = TS.entry_gate(context, fill, direction, clock, CTC.STRUCTURAL_ENTRY_POLICY)
            if not filled.get("eligible"):
                economics = dict(economics, eligible=False,
                    blockers=list(economics.get("blockers") or [])+[filled["reason"]])
    admitted = bool(timing.get("eligible") and economics.get("eligible"))
    rank = (int(admitted), int(bool(timing.get("eligible"))),
            TS.timestamp(event.get("signal_at")) or -1.)
    evidence = {"scenario": event.get("event_type") or context.get("scenario"),
                "status": context.get("status"), "reason": context.get("reason"),
                "event_id": event.get("event_id"), "direction": direction,
                "signal_at": event.get("signal_at"), "trigger_level": event.get("trigger_level"),
                "stop_price": event.get("stop_price"), "target_price": event.get("target_price"),
                "atr": event.get("atr"), "timing_reason": timing.get("reason"),
                "timing_eligible": bool(timing.get("eligible")),
                "economics_blockers": list(economics.get("blockers") or []),
                "economics_eligible": bool(economics.get("eligible")),
                "net_reward_risk": economics.get("net_reward_risk"),
                "modeled_round_trip_cost_pct": economics.get("modeled_round_trip_cost_pct")}
    return rank, evidence


def select_context(raw, horizon, clock, structural):
    """Timing/cost-qualified event first, then immutable confirmation time.

    Portfolio-specific trend, evidence, capital and reuse checks still run at
    admission. A missing MA history never invalidates a structural candidate.
    """
    import veritas_ma_rebound as MR
    candidates = [dict(structural, scenario="SAME_TIMEFRAME_STRUCTURAL_BREAKOUT")]
    if CTC.MA_REBOUND_POLICY.get("enabled"):
        candidates.append(MR.build_context(
            (raw.get("structure_bars_by_timeframe") or {}).get(horizon) or [], horizon, clock,
            daily_bars=raw.get("native_daily_bars") or [], asset=raw.get("asset") or "",
            source_identity=raw.get("structure_source_identity"),
            config=CTC.STRUCTURAL_ENTRY_POLICY, ma_config=CTC.MA_REBOUND_POLICY))
    assessed = [_assess(c, raw, horizon, clock) for c in candidates]
    winner = max(range(len(candidates)), key=lambda index: assessed[index][0])
    result = deepcopy(candidates[winner])
    result["entry_scenarios"] = [item[1] for item in assessed]
    result["selected_scenario"] = (result.get("event") or {}).get("event_type")
    result["selection_basis"] = "CURRENT_TIMING_AND_COST_ADMISSION_THEN_ORIGINAL_EVENT_TIME"
    return result


def confirmed_structure(features, direction, confidence, horizon):
    """A fresh trigger must not demote an already confirmed aligned trend."""
    previous = features.get("horizon_structure") or {}
    aligned = previous.get("direction") == direction
    state = ("CONFIRMED_TREND" if aligned and previous.get("state") == "CONFIRMED_TREND"
             else "BUILDING_TREND")
    score = max(float(confidence or 0.), float(previous.get("score") or 0.) if aligned else 0.)
    return dict(previous, horizon=horizon, resolution=horizon,
                direction=direction, state=state, score=score)
