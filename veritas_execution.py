from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional

VERSION = "veritas-execution-safety-v2"
RESEARCH_PAPER_ASSETS = frozenset(("NQ", "BRENT", "GOLD", "MOEX", "CNYRUBF"))

# Research/paper economics gate. This is deliberately independent from signal quality:
# even a SUPER signal cannot bypass bad trade economics.
MIN_REWARD_RISK = max(1.0, float(os.getenv("VERITAS_FINAL_MIN_RR", "1.15")))
MIN_EXPECTED_MOVE_PCT = max(0.0025, float(os.getenv("VERITAS_FINAL_MIN_EXPECTED_MOVE", "0.004")))
ROUND_TRIP_COST_BPS = max(1.0, float(os.getenv("VERITAS_EXECUTION_ROUND_TRIP_COST_BPS", "20")))

# Adverse fill assumptions for the normalized paper book. These are configurable
# and intentionally conservative relative to perfect mid/last execution.
_DEFAULT_FILL_BPS = {
    "BTC": 5.0,
    "ETH": 5.0,
    "NQ": 4.0,
    "BRENT": 6.0,
    "GOLD": 5.0,
    "MOEX": 8.0,
    "CNYRUBF": 8.0,
}

LIVE_RISK_PROFILE = {
    "max_stop_risk_nav": 0.005,
    "max_total_open_stop_risk_nav": 0.025,
    "max_correlated_stop_risk_nav": 0.0125,
    "max_single_asset_fraction": 0.25,
    "max_gross": 1.25,
    "daily_loss_stop": 0.02,
    "weekly_loss_stop": 0.05,
    "hard_drawdown_stop": 0.10,
    "allow_new_risk_without_durable_storage": False,
}


def _num(x: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def research_paper_source_ok(raw: Dict[str, Any]) -> bool:
    """One research source is enough after its freshness/session gate passes.

    source_gate_pass is set by the feed adapter (MOEX checks quote age there).
    Missing gate evidence or an invalid price must not admit a paper position.
    """
    price = _num(raw.get("price"))
    return bool(raw.get("source_gate_pass") and raw.get("market_open")
                and price is not None and price > 0)


def round_trip_cost_pct(spread_bps: Optional[float] = None) -> float:
    base = ROUND_TRIP_COST_BPS / 10000.0
    sb = _num(spread_bps)
    if sb is None or sb < 0:
        return base
    # Spread is paid once over a buy->sell round trip (half on each side).
    # Preserve the configured commission/slippage floor if it is more conservative.
    spread_cost = sb / 10000.0
    return max(base, spread_cost)


def economics_gate(asset: str, plan: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Validate the same target, stop, size and adverse fills used by paper execution."""
    p = dict(plan or {})
    blockers = []
    forecast_rr = _num(p.get("expected_to_stop_ratio"))
    forecast_move = _num(p.get("expected_move_pct"))
    entry, stop = _num(p.get("entry_price")), _num(p.get("stop_price"))
    target = _num(p.get("target_price") or p.get("tactical_target_price"))
    direction = str(p.get("direction") or "")
    if direction not in ("LONG", "SHORT") and entry and stop:
        direction = "LONG" if stop < entry else "SHORT"
    sign = 1 if direction == "LONG" else -1
    valid_entry = entry is not None and entry > 0
    if not valid_entry:
        blockers.append("ENTRY_PRICE_INVALID")
    if stop is None or stop <= 0:
        blockers.append("STOP_MISSING")
    elif valid_entry and sign * (entry - stop) <= 0:
        blockers.append("STOP_DIRECTION_INVALID")
    if target is None or target <= 0:
        blockers.append("TARGET_MISSING")
    elif valid_entry and sign * (target - entry) <= 0:
        blockers.append("TARGET_DIRECTION_INVALID")
    if forecast_rr is None or forecast_rr < MIN_REWARD_RISK:
        blockers.append("RR_BELOW_FINAL_FLOOR")

    spread_bps = _num(p.get("spread_bps"))
    modeled_cost = round_trip_cost_pct(spread_bps)
    stop_distance = abs(entry - stop) / entry if valid_entry and stop else None
    reward = risk = net_rr = target_move = entry_fill = target_fill = stop_fill = None
    if valid_entry and stop and stop > 0 and target and target > 0:
        fraction = max(0.0, _num(p.get("initial_position_fraction"), 0.10))
        commission = 0.0005
        buy = direction == "LONG"
        entry_fill = simulated_fill(asset, "BUY" if buy else "SELL_SHORT", entry,
                                    fraction, bid=p.get("best_bid"), ask=p.get("best_ask"))["fill_price"]
        # The current exit engine uses an adverse reference-price fill; use that
        # same model here, including size impact and both commission legs.
        target_fill = simulated_fill(asset, "SELL" if buy else "BUY_TO_COVER", target,
                                     fraction * target / entry_fill)["fill_price"]
        stop_fill = simulated_fill(asset, "SELL" if buy else "BUY_TO_COVER", stop,
                                   fraction * stop / entry_fill)["fill_price"]
        hold = max(0.0, _num(p.get("expected_hold_seconds"),
                   {"5m": 300, "1h": 3600, "4h": 14400, "1d": 86400,
                    "3d": 259200, "7d": 604800}.get(p.get("horizon"), 3600)))
        funding = entry_fill * 0.16 * hold / (365.25 * 86400)
        fees = commission * (entry_fill + target_fill)
        modeled_cost = max(modeled_cost, (abs(entry_fill-entry) + abs(target_fill-target)
                                          + fees + funding) / entry)
        reward = (sign * (target_fill-entry_fill) - fees - funding) / entry_fill
        risk = (sign * (entry_fill-stop_fill) + commission * (entry_fill+stop_fill)
                + funding) / entry_fill
        net_rr = reward / risk if risk > 0 else None
        target_move = sign * (target-entry) / entry
        if reward <= 0:
            blockers.append("TARGET_NOT_PROFITABLE_AFTER_COSTS")
        if net_rr is None or net_rr < MIN_REWARD_RISK:
            blockers.append("NET_REWARD_RISK_BELOW_FLOOR")

    min_move = max(MIN_EXPECTED_MOVE_PCT, 2.5 * modeled_cost)
    effective_move = min(abs(forecast_move), target_move) if forecast_move is not None and target_move is not None else None
    if effective_move is None or effective_move < min_move:
        blockers.append("EXPECTED_MOVE_BELOW_COST_BUFFER")
    return {
        "status": "BLOCK" if blockers else "PASS", "eligible": not blockers,
        "asset": str(asset or ""), "blockers": blockers,
        "expected_to_stop_ratio": net_rr, "forecast_reward_risk": forecast_rr,
        "minimum_reward_risk": MIN_REWARD_RISK,
        "expected_move_pct": effective_move, "forecast_move_pct": forecast_move,
        "minimum_expected_move_pct": min_move,
        "modeled_round_trip_cost_pct": modeled_cost,
        "observed_spread_bps": spread_bps, "stop_distance_pct": stop_distance,
        "target_price": target, "target_distance_pct": target_move,
        "modeled_entry_fill": entry_fill, "modeled_target_fill": target_fill,
        "modeled_stop_fill": stop_fill, "net_reward_pct": reward, "net_risk_pct": risk,
        "principle": "Actual target/stop economics after adverse fills, commission and funding.",
    }


def entry_gate(row, price, direction, fraction, position=None):
    """Last check after all setup/sizing mutations, immediately before any order."""
    from veritas_quote_time import quote_gate
    row = row or {}
    plan = dict(row.get('trade_plan') or {})
    if position:
        payload = position.get('payload') or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        plan['stop_price'] = position.get('stop_price')
        plan['target_price'] = payload.get('take_price') or payload.get('target_price')
    plan.update(entry_price=price, direction=direction, initial_position_fraction=fraction,
                horizon=row.get('horizon'), best_bid=row.get('best_bid'), best_ask=row.get('best_ask'))
    gate = economics_gate(row.get('asset'), plan)
    timing = quote_gate(row.get('market_observed_at') or row.get('observed_at') or plan.get('market_observed_at'), row.get('horizon'))
    gate['quote_time_gate'] = timing
    if not timing['eligible']:
        gate['eligible'] = False
        gate['status'] = 'BLOCK'
        gate['blockers'].append(timing['reason'])
    return gate


def production_source_gate(asset: str, raw: Optional[Dict[str, Any]], clock_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    r = dict(raw or {})
    asset = str(asset or "")
    blockers = []
    research_ok = bool(r.get("source_gate_pass", False))
    time_ok = bool(r.get("market_open", False) or asset in ("BTC", "ETH"))

    if not research_ok:
        blockers.append("RESEARCH_SOURCE_GATE_FAILED")
    if not time_ok:
        blockers.append("MARKET_TIME_GATE_FAILED")

    if asset in ("BTC", "ETH"):
        clock_ok = bool((clock_info or {}).get("ok", True))
        secondary = r.get("secondary_price", r.get("coinbase_price"))
        divergence = abs(_num(r.get("source_divergence"), 1.0) or 0.0)
        if not clock_ok:
            blockers.append("CLOCK_GATE_FAILED")
        if secondary is None:
            blockers.append("SECOND_DIRECT_QUOTE_MISSING")
        bid=_num(r.get("best_bid")); ask=_num(r.get("best_ask"))
        if bid is None or ask is None or bid<=0 or ask<=bid:
            blockers.append("PRIMARY_TOP_OF_BOOK_MISSING")
        if divergence > float(os.getenv("VERITAS_PRODUCTION_MAX_SOURCE_DIVERGENCE", "0.003")):
            blockers.append("DIRECT_QUOTE_DIVERGENCE_TOO_LARGE")
    else:
        # Current public/delayed research feeds are explicitly not sufficient for
        # automatic real-money execution. A future broker/direct feed adapter must
        # set production_direct_feed=True only after contract + timestamp validation.
        if not bool(r.get("production_direct_feed", False)):
            blockers.append("PRODUCTION_DIRECT_FEED_NOT_CONFIGURED")

    ok = len(blockers) == 0
    return {
        "eligible": ok,
        "status": "PASS" if ok else "BLOCK",
        "asset": asset,
        "blockers": blockers,
        "research_ok": research_ok,
        "time_ok": time_ok,
        "principle": "Production eligibility is fail-closed and cannot be relaxed by the research strict-gate setting.",
    }


def simulated_fill(asset: str, side: str, reference_price: float, fraction_nav: float = 0.0,
                   bid: Optional[float] = None, ask: Optional[float] = None) -> Dict[str, Any]:
    px = float(reference_price)
    if not math.isfinite(px) or px <= 0:
        raise ValueError("reference_price must be positive")
    asset = str(asset or "")
    side = str(side or "").upper()
    is_buy = side in ("BUY", "BUY_TO_COVER")

    base_bps = float(os.getenv(f"VERITAS_PAPER_FILL_BPS_{asset}", str(_DEFAULT_FILL_BPS.get(asset, 6.0))))
    size_bps = min(20.0, max(0.0, float(fraction_nav or 0.0) - 0.10) * 3.0)

    b = _num(bid)
    a = _num(ask)
    quote_valid = bool(b is not None and a is not None and b > 0 and a > b)
    if quote_valid:
        executable_quote = a if is_buy else b
        mid = 0.5 * (a + b)
        spread_bps = (a - b) / mid * 10000.0 if mid > 0 else None
        # Quote already includes spread. Add only residual adverse slippage + size impact.
        residual_bps = max(1.0, 0.40 * base_bps)
        impact_bps = residual_bps + size_bps
        bump = impact_bps / 10000.0
        fill = executable_quote * (1.0 + bump if is_buy else 1.0 - bump)
        model = "BID_ASK_ADVERSE_PAPER_FILL_V2"
    else:
        executable_quote = px
        spread_bps = None
        residual_bps = base_bps
        impact_bps = base_bps + size_bps
        bump = impact_bps / 10000.0
        fill = px * (1.0 + bump if is_buy else 1.0 - bump)
        model = "CONSERVATIVE_NORMALIZED_PAPER_FILL_V1_FALLBACK"

    adverse_vs_reference_bps = abs(fill / px - 1.0) * 10000.0
    return {
        "reference_price": px,
        "executable_quote": executable_quote,
        "bid": b,
        "ask": a,
        "spread_bps": spread_bps,
        "fill_price": fill,
        "adverse_fill_bps": adverse_vs_reference_bps,
        "base_fill_bps": base_bps,
        "residual_slippage_bps": residual_bps,
        "size_impact_bps": size_bps,
        "side": side,
        "asset": asset,
        "model": model,
        "quote_valid": quote_valid,
    }


@dataclass(frozen=True)
class OrderIntent:
    client_order_id: str
    portfolio: str
    asset: str
    direction: str
    side: str
    target_fraction: float
    reference_price: float
    horizon: Optional[str]
    created_at: str
    reason: str
    production_eligible: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def make_client_order_id(portfolio: str, asset: str, direction: str, target_fraction: float,
                         horizon: Optional[str], signal_time: str, reason: str) -> str:
    raw = {
        "portfolio": str(portfolio or ""),
        "asset": str(asset or ""),
        "direction": str(direction or ""),
        "target_fraction": round(float(target_fraction or 0.0), 6),
        "horizon": str(horizon or ""),
        "signal_time": str(signal_time or ""),
        "reason": str(reason or ""),
    }
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
    return "VRT-" + digest


def build_order_intent(portfolio: str, asset: str, direction: str, target_fraction: float,
                       reference_price: float, horizon: Optional[str], signal_time: str,
                       reason: str, production_eligible: bool = False) -> OrderIntent:
    direction = str(direction or "").upper()
    side = "BUY" if direction == "LONG" else "SELL_SHORT"
    cid = make_client_order_id(portfolio, asset, direction, target_fraction, horizon, signal_time, reason)
    return OrderIntent(
        client_order_id=cid,
        portfolio=str(portfolio or ""),
        asset=str(asset or ""),
        direction=direction,
        side=side,
        target_fraction=float(target_fraction or 0.0),
        reference_price=float(reference_price),
        horizon=horizon,
        created_at=str(signal_time or ""),
        reason=str(reason or ""),
        production_eligible=bool(production_eligible),
    )


def production_order_gate(asset: str, plan: Optional[Dict[str, Any]], source_gate: Dict[str, Any],
                          durable_storage: bool, calibrated_probability: Optional[float],
                          stop_risk_nav: Optional[float], single_asset_fraction: Optional[float],
                          gross_after: Optional[float], drawdown: Optional[float],
                          total_open_stop_risk_nav_after: Optional[float] = None,
                          correlated_stop_risk_nav_after: Optional[float] = None,
                          instrument_spec_validated: bool = False,
                          model_promoted: bool = False,
                          model_version: Optional[str] = None,
                          daily_pnl_pct: Optional[float] = None, weekly_pnl_pct: Optional[float] = None,
                          broker_reconciled: bool = False, kill_switch: bool = False) -> Dict[str, Any]:
    blockers = []
    econ = economics_gate(asset, plan)
    if not econ.get("eligible"):
        blockers.extend(econ.get("blockers") or ["ECONOMICS_BLOCK"])
    if not bool((source_gate or {}).get("eligible")):
        blockers.append("PRODUCTION_SOURCE_GATE_FAILED")
    if not durable_storage:
        blockers.append("DURABLE_STORAGE_REQUIRED")
    p = _num(calibrated_probability)
    min_p = float(os.getenv("VERITAS_LIVE_MIN_CALIBRATED_PROBABILITY", "0.60"))
    if p is None:
        blockers.append("CALIBRATED_PROBABILITY_REQUIRED")
    elif p < min_p:
        blockers.append("CALIBRATED_PROBABILITY_TOO_LOW")

    rr = _num(econ.get("expected_to_stop_ratio"))
    stop_distance = _num(econ.get("stop_distance_pct"))
    cost_r = (float(econ.get("modeled_round_trip_cost_pct") or round_trip_cost_pct()) / stop_distance) if stop_distance and stop_distance > 0 else None
    expectancy_r = (p * rr - (1.0 - p)) if (p is not None and rr is not None and cost_r is not None) else None
    min_expectancy_r = float(os.getenv("VERITAS_LIVE_MIN_EXPECTANCY_R", "0.05"))
    if expectancy_r is None:
        blockers.append("POST_COST_EXPECTANCY_UNAVAILABLE")
    elif expectancy_r <= min_expectancy_r:
        blockers.append("POST_COST_EXPECTANCY_TOO_LOW")

    sr = _num(stop_risk_nav)
    if sr is None or sr > LIVE_RISK_PROFILE["max_stop_risk_nav"]:
        blockers.append("STOP_RISK_LIMIT")
    total_sr = _num(total_open_stop_risk_nav_after)
    if total_sr is None:
        blockers.append("TOTAL_OPEN_STOP_RISK_REQUIRED")
    elif total_sr > LIVE_RISK_PROFILE["max_total_open_stop_risk_nav"]:
        blockers.append("TOTAL_OPEN_STOP_RISK_LIMIT")
    corr_sr = _num(correlated_stop_risk_nav_after)
    if corr_sr is None:
        blockers.append("CORRELATED_STOP_RISK_REQUIRED")
    elif corr_sr > LIVE_RISK_PROFILE["max_correlated_stop_risk_nav"]:
        blockers.append("CORRELATED_STOP_RISK_LIMIT")
    sf = _num(single_asset_fraction)
    if sf is None or sf > LIVE_RISK_PROFILE["max_single_asset_fraction"]:
        blockers.append("SINGLE_ASSET_LIMIT")
    if not instrument_spec_validated:
        blockers.append("INSTRUMENT_SPEC_REQUIRED")
    if not model_promoted:
        blockers.append("MODEL_PROMOTION_REQUIRED")
    ga = _num(gross_after)
    if ga is None or ga > LIVE_RISK_PROFILE["max_gross"]:
        blockers.append("GROSS_LIMIT")
    dd = _num(drawdown)
    if dd is None or dd >= LIVE_RISK_PROFILE["hard_drawdown_stop"]:
        blockers.append("DRAWDOWN_LIMIT")
    dp = _num(daily_pnl_pct, 0.0)
    if dp is not None and dp <= -LIVE_RISK_PROFILE["daily_loss_stop"]:
        blockers.append("DAILY_LOSS_STOP")
    wp = _num(weekly_pnl_pct, 0.0)
    if wp is not None and wp <= -LIVE_RISK_PROFILE["weekly_loss_stop"]:
        blockers.append("WEEKLY_LOSS_STOP")
    if not broker_reconciled:
        blockers.append("BROKER_RECONCILIATION_REQUIRED")
    if kill_switch:
        blockers.append("KILL_SWITCH_ACTIVE")
    ok = len(blockers) == 0
    return {
        "eligible": ok,
        "status": "PASS" if ok else "BLOCK",
        "asset": str(asset or ""),
        "model_version": model_version,
        "model_promoted": bool(model_promoted),
        "blockers": list(dict.fromkeys(blockers)),
        "economics": econ,
        "source_gate": source_gate,
        "calibrated_probability": p,
        "minimum_calibrated_probability": min_p,
        "post_cost_expectancy_r": expectancy_r,
        "minimum_post_cost_expectancy_r": min_expectancy_r,
        "modeled_cost_r": cost_r,
        "live_risk_profile": dict(LIVE_RISK_PROFILE),
        "principle": "Real-money orders require data, edge, calibration, durable state, broker reconciliation and risk limits simultaneously.",
    }
