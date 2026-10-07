"""Pure paper sizing from final modeled fills and the unchanged structural stop.

Fractions ending in ``_pct``/``_nav`` are ratios, not percentage points. Prices
passed as fills already contain spread/slippage: this module never fills again
or adds a round-trip slippage allowance. It grants no entry/exit permission.
"""
from __future__ import annotations

import json
import math

import veritas_canonical_constitution as CTC
import veritas_costs as VC

VERSION = "NET_STOP_RISK_BUDGET_V1"


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _blocked(reason, **details):
    return dict(version=VERSION, eligible=False, status="BLOCK", reason=reason,
                blockers=[reason], **details)


def _funding_fraction(expected_hold_seconds, position_age_seconds=0.0):
    hold, age = _number(expected_hold_seconds), _number(position_age_seconds)
    if hold is None or age is None or hold < 0 or age < 0 or not math.isfinite(age + hold):
        raise ValueError("EXPECTED_FUNDING_HORIZON_INVALID")
    # An add does not restart the original position's first free 24 hours.
    return VC.funding_fraction(age + hold) - VC.funding_fraction(age)


def stop_risk_components(direction, entry_fill_price, stop_price, stop_fill_price,
                         *, expected_hold_seconds=0.0, position_age_seconds=0.0):
    """Full marginal entry-to-stop cost per unit using already modeled fills.

This is for new units, including adds at the held stop. Existing units use the
separate forward-risk helper below; their paid entry commission is not charged
again. Funding is an estimate on the new entry notional for the stated horizon.
"""
    entry, stop, fill = map(_number, (entry_fill_price, stop_price, stop_fill_price))
    if direction not in ("LONG", "SHORT"):
        return _blocked("STOP_RISK_DIRECTION_INVALID")
    if any(value is None or value <= 0 for value in (entry, stop, fill)):
        return _blocked("NET_STOP_RISK_FILL_REQUIRED")
    sign = 1 if direction == "LONG" else -1
    if sign * (entry - stop) <= 0:
        return _blocked("STOP_DIRECTION_INVALID")
    if sign * (stop - fill) < -1e-12:
        return _blocked("STOP_FILL_NOT_ADVERSE")
    try:
        funding = entry * _funding_fraction(expected_hold_seconds, position_age_seconds)
    except ValueError as error:
        return _blocked(str(error))
    price_risk = sign * (entry - fill)
    entry_fee, exit_fee = entry * VC.COMMISSION_RATE, fill * VC.COMMISSION_RATE
    risk = price_risk + entry_fee + exit_fee + funding
    if not math.isfinite(risk) or risk <= 0:
        return _blocked("NET_STOP_RISK_COSTS_INVALID")
    return dict(version=VERSION, eligible=True, status="PASS", blockers=[],
                basis="FINAL_ENTRY_AND_STOP_FILLS_WITH_FULL_MARGINAL_COSTS",
                entry_fill_price=entry, stop_price=stop, stop_fill_price=fill,
                price_risk_per_unit=price_risk, entry_commission_per_unit=entry_fee,
                stop_commission_per_unit=exit_fee, expected_funding_per_unit=funding,
                net_risk_per_unit=risk, net_risk_pct=risk / entry,
                slippage_already_in_fills=True, cost_policy_version=VC.VERSION)


def existing_stop_risk_nav(position, modeled_stop_fill, nav, now, *, mark_price,
                           expected_hold_seconds=0.0):
    """Conservative forward risk of held units, for an add's budget reservation.

The mark must be the same frozen execution quote used for the add. Reserve the
entire mark-to-stop loss, future exit fee and estimated remaining funding. Paid
fees/funding are already in current NAV; no entry fee is charged again, and no
realized or protected profit is credited to enlarge the budget. This is a NAV
drawdown basis, not an entry-to-exit or whole-cycle P&L projection.
"""
    position = position or {}
    units = _number(position.get("units"))
    mark, fill, capital = map(_number, (mark_price, modeled_stop_fill, nav))
    stop = _number(position.get("stop_price"))
    direction = position.get("direction")
    if direction not in ("LONG", "SHORT"):
        return _blocked("STOP_RISK_DIRECTION_INVALID")
    if any(value is None or value <= 0 for value in (units, mark, fill, capital, stop)):
        return _blocked("EXISTING_STOP_RISK_DATA_REQUIRED")
    sign = 1 if direction == "LONG" else -1
    if sign * (mark - stop) <= 0:
        return _blocked("STOP_DIRECTION_INVALID")
    if sign * (stop - fill) < -1e-12:
        return _blocked("STOP_FILL_NOT_ADVERSE")
    hold = _number(expected_hold_seconds)
    if hold is None or hold < 0:
        return _blocked("EXPECTED_FUNDING_HORIZON_INVALID")
    age = 0.0
    if hold > 0:
        payload = position.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                payload = {}
        payload = payload if isinstance(payload, dict) else {}
        opened = (position.get("opened_at") or position.get("entry_time")
                  or payload.get("entry_time") or payload.get("opened_at"))
        try:
            age = (VC.utc_datetime(now) - VC.utc_datetime(opened)).total_seconds()
            if age < 0:
                raise ValueError("future entry")
        except (TypeError, ValueError, OverflowError):
            return _blocked("EXISTING_FUNDING_AGE_REQUIRED")
    price_risk = units * max(0.0, sign * (mark - fill))
    exit_fee = units * fill * VC.COMMISSION_RATE
    funding = units * max(mark, fill) * _funding_fraction(hold, age)
    risk = price_risk + exit_fee + funding
    if not math.isfinite(risk) or risk <= 0:
        return _blocked("NET_STOP_RISK_COSTS_INVALID")
    return dict(version=VERSION, eligible=True, status="PASS", blockers=[],
                basis="FORWARD_MARK_TO_STOP_PLUS_EXIT_COSTS", units=units,
                mark_price=mark, stop_price=stop, modeled_stop_fill=fill,
                price_risk_rub=price_risk, exit_commission_rub=exit_fee,
                expected_funding_rub=funding, net_stop_risk_rub=risk,
                net_stop_risk_nav=risk / capital, slippage_already_in_fills=True)


def cap_fraction_from_economics(economics, requested_fraction, policy, *,
                                risk_cap_nav=None, current_fraction=0.0,
                                existing_stop_risk_nav=None,
                                gross_excluding_position=0.0):
    """Cap only incremental exposure; bad/missing net economics is a hard veto.

``requested_fraction`` is total desired exposure. For an ADD the caller must
provide both current marked exposure and held forward stop risk. The gate must
use the stored position stop/target and the same frozen entry fill. A cap below
held exposure returns HOLD with that exposure unchanged, never a forced sale.
The incremental fraction is floored to the portfolio's configured step.
"""
    gate, policy = economics or {}, policy or {}
    current = _number(current_fraction)
    current = current if current is not None and current >= 0 else None
    def denied(reason, **details):
        return _blocked(reason, fraction=current or 0.0, add_fraction=0.0,
                        action="BLOCK", **details)
    requested, used_risk, other_gross = map(_number, (
        requested_fraction, 0.0 if existing_stop_risk_nav is None and current == 0 else
        existing_stop_risk_nav, gross_excluding_position))
    if current is not None and current > 0 and (used_risk is None or used_risk <= 0):
        return denied("EXISTING_STOP_RISK_REQUIRED")
    if current is None or any(v is None or v < 0 for v in (requested, used_risk, other_gross)):
        return denied("NET_STOP_RISK_BUDGET_INPUT_INVALID")
    hard = [str(code) for code in (gate.get("blockers") or [])
            if CTC.veto_severity(code) == "HARD"]
    if hard:
        return denied(hard[0], economics_blockers=hard)
    if not isinstance(gate.get("eligible"), bool) or (
            not gate["eligible"] and not gate.get("blockers")):
        return denied("NET_STOP_RISK_ECONOMICS_REQUIRED")
    risk, reward, rr = map(_number, (gate.get("net_risk_pct"),
                                   gate.get("net_reward_pct"), gate.get("net_reward_risk")))
    entry, stop_fill, target_fill = map(_number, (gate.get("modeled_entry_fill"),
                                      gate.get("modeled_stop_fill"), gate.get("modeled_target_fill")))
    if any(v is None or v <= 0 for v in (risk, entry, stop_fill, target_fill)):
        return denied("NET_STOP_RISK_FILL_REQUIRED")
    if (stop_fill - entry) * (target_fill - entry) >= 0:
        return denied("NET_STOP_RISK_GEOMETRY_INVALID")
    if reward is None or reward <= 0:
        return denied("TARGET_NOT_PROFITABLE_AFTER_COSTS")
    floor = max(float(CTC.STRUCTURAL_ENTRY_POLICY["minimum_net_reward_risk"]),
                _number(gate.get("minimum_reward_risk")) or 0.0)
    actual_rr = reward / risk
    if rr is None or min(rr, actual_rr) < floor:
        return denied("NET_REWARD_RISK_BELOW_FLOOR")
    if not math.isclose(rr, actual_rr, rel_tol=1e-9, abs_tol=1e-12):
        return denied("NET_STOP_RISK_ECONOMICS_INCONSISTENT")
    minimum_risk = (abs(entry - stop_fill) + VC.COMMISSION_RATE * (entry + stop_fill)) / entry
    if risk + 1e-12 < minimum_risk:
        return denied("NET_STOP_RISK_COSTS_INCOMPLETE")
    canonical_cap = float(CTC.PAPER_RISK_POLICY["per_idea_structural_stop_risk_cap_nav"])
    supplied_cap = canonical_cap if risk_cap_nav is None else _number(risk_cap_nav)
    step = _number(policy.get("position_step", 0.05))
    maximum = _number(policy.get("max_fraction", policy.get("max_single_asset_fraction")))
    gross = _number(policy.get("max_gross"))
    if any(v is None or v <= 0 for v in (supplied_cap, step, maximum, gross)):
        return denied("NET_STOP_RISK_POLICY_REQUIRED")
    cap = min(canonical_cap, supplied_cap)
    available = max(0.0, cap - used_risk)
    incremental = min(max(0.0, requested - current), available / risk,
                      max(0.0, maximum - current), max(0.0, gross - other_gross - current))
    incremental = max(0.0, math.floor(incremental / step + 1e-12) * step)
    if used_risk + incremental * risk > cap + 1e-12:
        incremental = max(0.0, incremental - step)
    fraction = current + incremental
    allowed = incremental > 0
    reason = ("NET_STOP_RISK_BUDGET_PASS" if allowed else
              "TARGET_ALREADY_REACHED" if requested <= current else "STOP_RISK_CAP_EXCEEDED")
    return dict(version=VERSION, eligible=allowed,
                status="PASS" if allowed else "HOLD" if current > 0 else "BLOCK",
                action="ADD" if allowed and current > 0 else "OPEN" if allowed else
                       "HOLD" if current > 0 else "BLOCK",
                reason=reason, blockers=[] if allowed else [reason],
                fraction=fraction, add_fraction=incremental,
                requested_fraction=requested, current_fraction=current,
                risk_cap_nav=cap, existing_stop_risk_nav=used_risk,
                available_add_risk_nav=available, marginal_net_risk_pct=risk,
                incremental_stop_risk_nav=incremental * risk,
                total_stop_risk_nav_after=used_risk + incremental * risk,
                net_reward_risk=actual_rr, modeled_entry_fill=entry,
                modeled_stop_fill=stop_fill, modeled_target_fill=target_fill,
                sizing_basis="NET_STOP_RISK_FROM_FINAL_FROZEN_FILLS")


def whole_cycle_projection(direction, avg_entry_price, remaining_units, exit_fill_price,
                           realized_gross_pnl_rub, booked_commission_rub, booked_funding_rub,
                           *, projected_funding_rub=0.0):
    """Diagnostic only: keep the remaining leg distinct from the whole trade.

The actual ledger's paid costs are inputs, never recalculated using today's
rates. A profitable remaining leg need not make the already reduced cycle a
winner. This helper cannot authorize a trade, move a target or suppress a stop.
"""
    entry, units, fill, realized, fees, funding, future = map(_number, (
        avg_entry_price, remaining_units, exit_fill_price, realized_gross_pnl_rub,
        booked_commission_rub, booked_funding_rub, projected_funding_rub))
    if (direction not in ("LONG", "SHORT") or
        any(v is None or v <= 0 for v in (entry, fill)) or
        any(v is None or v < 0 for v in (units, fees, funding, future)) or realized is None):
        return dict(version=VERSION, complete=False, diagnostic_only=True,
                    reason="WHOLE_CYCLE_COST_DATA_REQUIRED")
    remaining_gross = units * (fill - entry) * (1 if direction == "LONG" else -1)
    exit_fee = units * fill * VC.COMMISSION_RATE
    remaining_net = remaining_gross - exit_fee - future
    return dict(version=VERSION, complete=True, diagnostic_only=True,
                remaining_gross_pnl_rub=remaining_gross,
                remaining_exit_commission_rub=exit_fee,
                remaining_expected_funding_rub=future,
                remaining_net_before_paid_cycle_costs_rub=remaining_net,
                booked_cycle_net_pnl_rub=realized - fees - funding,
                whole_cycle_net_pnl_rub=realized - fees - funding + remaining_net)
