from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional

VERSION = "veritas-execution-safety-v1"

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


def round_trip_cost_pct() -> float:
    return ROUND_TRIP_COST_BPS / 10000.0


def economics_gate(asset: str, plan: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    p = dict(plan or {})
    blockers = []
    rr = _num(p.get("expected_to_stop_ratio"))
    move = _num(p.get("expected_move_pct"))
    if move is not None:
        move = abs(move)

    # Require enough gross move to survive modeled round-trip costs with margin.
    min_move = max(MIN_EXPECTED_MOVE_PCT, 2.5 * round_trip_cost_pct())
    if rr is None or rr < MIN_REWARD_RISK:
        blockers.append("RR_BELOW_FINAL_FLOOR")
    if move is None or move < min_move:
        blockers.append("EXPECTED_MOVE_BELOW_COST_BUFFER")

    stop = _num(p.get("stop_price"))
    entry = _num(p.get("entry_price"))
    if stop is None:
        blockers.append("STOP_MISSING")
    if entry is not None and entry > 0 and stop is not None:
        stop_distance = abs(entry - stop) / entry
        if stop_distance <= 0:
            blockers.append("STOP_DISTANCE_INVALID")
    else:
        stop_distance = None

    passed = len(blockers) == 0
    return {
        "status": "PASS" if passed else "BLOCK",
        "eligible": passed,
        "asset": str(asset or ""),
        "blockers": blockers,
        "expected_to_stop_ratio": rr,
        "minimum_reward_risk": MIN_REWARD_RISK,
        "expected_move_pct": move,
        "minimum_expected_move_pct": min_move,
        "modeled_round_trip_cost_pct": round_trip_cost_pct(),
        "stop_distance_pct": stop_distance,
        "principle": "No signal tier or setup may bypass final post-cost economics.",
    }


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


def simulated_fill(asset: str, side: str, reference_price: float, fraction_nav: float = 0.0) -> Dict[str, Any]:
    px = float(reference_price)
    if not math.isfinite(px) or px <= 0:
        raise ValueError("reference_price must be positive")
    asset = str(asset or "")
    side = str(side or "").upper()

    base_bps = float(os.getenv(f"VERITAS_PAPER_FILL_BPS_{asset}", str(_DEFAULT_FILL_BPS.get(asset, 6.0))))
    # Simple size penalty for normalized paper exposure. This is not a substitute
    # for a real order-book simulator, but prevents perfect-price fills.
    size_bps = min(20.0, max(0.0, float(fraction_nav or 0.0) - 0.10) * 3.0)
    adverse_bps = max(0.0, base_bps + size_bps)
    bump = adverse_bps / 10000.0

    is_buy = side in ("BUY", "BUY_TO_COVER")
    fill = px * (1.0 + bump if is_buy else 1.0 - bump)
    return {
        "reference_price": px,
        "fill_price": fill,
        "adverse_fill_bps": adverse_bps,
        "base_fill_bps": base_bps,
        "size_impact_bps": size_bps,
        "side": side,
        "asset": asset,
        "model": "CONSERVATIVE_NORMALIZED_PAPER_FILL_V1",
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
    cost_r = (round_trip_cost_pct() / stop_distance) if stop_distance and stop_distance > 0 else None
    expectancy_r = (p * rr - (1.0 - p) - cost_r) if (p is not None and rr is not None and cost_r is not None) else None
    min_expectancy_r = float(os.getenv("VERITAS_LIVE_MIN_EXPECTANCY_R", "0.05"))
    if expectancy_r is None:
        blockers.append("POST_COST_EXPECTANCY_UNAVAILABLE")
    elif expectancy_r <= min_expectancy_r:
        blockers.append("POST_COST_EXPECTANCY_TOO_LOW")

    sr = _num(stop_risk_nav)
    if sr is None or sr > LIVE_RISK_PROFILE["max_stop_risk_nav"]:
        blockers.append("STOP_RISK_LIMIT")
    sf = _num(single_asset_fraction)
    if sf is None or sf > LIVE_RISK_PROFILE["max_single_asset_fraction"]:
        blockers.append("SINGLE_ASSET_LIMIT")
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
