from __future__ import annotations

import hashlib
import json
import math
import os
import veritas_costs as VC
import veritas_canonical_constitution as CTC
import veritas_price_source as VPS
import veritas_execution_snapshot as VES
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from copy import deepcopy
from typing import Any, Dict, Optional

VERSION = "veritas-execution-safety-v4-verified-fill"
RESEARCH_PAPER_ASSETS = frozenset(("NQ", "BRENT", "GOLD", "MOEX", "CNYRUBF"))
PAPER_ASSETS = RESEARCH_PAPER_ASSETS | frozenset(("BTC", "ETH"))
PAPER_SOURCE_POLICY = "ONE_VALID_PRIMARY_SOURCE"

# Research/paper economics gate. This is deliberately independent from signal quality:
# even a SUPER signal cannot bypass bad trade economics.
MIN_REWARD_RISK = max(CTC.STRUCTURAL_ENTRY_POLICY['minimum_net_reward_risk'], float(os.getenv("VERITAS_FINAL_MIN_RR", "1.15")))
# CTC owns the move floor; stale environment overrides cannot change it.
MIN_EXPECTED_MOVE_PCT = float(CTC.COST_POLICY["minimum_expected_move_floor_pct"])
MIN_MOVE_COST_MULTIPLE = float(CTC.COST_POLICY["cost_buffer_multiple"])
MOVE_POLICY_VERSION = VC.VERSION
ROUND_TRIP_COST_BPS = VC.ROUND_TRIP_RATE * 10000.0

# User-approved fixed paper slippage; observed bid/ask remains the execution anchor.
_DEFAULT_FILL_BPS = {asset: VC.SLIPPAGE_RATE * 10000.0 for asset in PAPER_ASSETS}


LIVE_RISK_PROFILE = dict(CTC.LIVE_RISK_POLICY)


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


def is_proxy_price(asset, raw):
    """NQ prices must remain observed futures prices across the entire book."""
    if asset != 'NQ':
        return False
    raw=raw or {}
    names=raw.get('source_names') or raw.get('market_source_names') or {}
    labels=' '.join(str(v or '') for v in (
        names.get('primary'),raw.get('verification_mode'),raw.get('data_latency_class'),raw.get('source'))).upper()
    return 'PROXY' in labels or 'QQQ' in labels


def paper_source_gate(asset, raw, clock_info=None):
    """Single authority for source admission to every normalized paper portfolio.

    Optional cross-checks may reject contradictory data, but their absence is
    never a veto. Quote age and trade economics are checked again at entry.
    """
    r = raw or {}
    blockers = []
    if asset not in PAPER_ASSETS:
        blockers.append("UNSUPPORTED_PAPER_ASSET")
    if is_proxy_price(asset,r):
        blockers.append("R67_DIRECT_NQ_QUOTE_REQUIRED")
    # New oil risk requires the configured observed provider feed. A generic
    # source_gate_pass flag cannot admit another future or a label-only quote.
    # Existing positions retain their own protective source in position_guard.
    if not r.get("source_gate_pass") or (asset=='BRENT' and not VPS.brent_quote_verified(r)):
        blockers.append("PRIMARY_SOURCE_GATE_FAILED")
    if not r.get("market_open"):
        blockers.append("MARKET_TIME_GATE_FAILED")
    price = _num(r.get("price"))
    if price is None or price <= 0:
        blockers.append("PRIMARY_PRICE_INVALID")
    if r.get("direct_sources") is not None and (_num(r["direct_sources"], 0) < 1):
        blockers.append("PRIMARY_SOURCE_MISSING")
    secondary = _num(r.get("secondary_price", r.get("coinbase_price")))
    if asset in ("BTC", "ETH"):
        bid, ask = _num(r.get("best_bid")), _num(r.get("best_ask"))
        if bid is None or ask is None or bid <= 0 or ask <= bid:
            blockers.append("PRIMARY_TOP_OF_BOOK_MISSING")
        if clock_info is not None and not clock_info.get("ok", False):
            blockers.append("CLOCK_GATE_FAILED")
        if secondary is not None and secondary > 0 and price is not None and price > 0:
            divergence = abs(price-secondary) / ((price+secondary)/2)
            if divergence > float(os.getenv("VERITAS_MAX_SOURCE_DIVERGENCE", "0.01")):
                blockers.append("DIRECT_QUOTE_DIVERGENCE_TOO_LARGE")
    return {"eligible": not blockers, "reason": "paper_one_valid_source" if not blockers else blockers[0],
            "blockers": blockers, "source_policy": PAPER_SOURCE_POLICY,
            "minimum_sources": 1}


def round_trip_cost_pct(spread_bps: Optional[float] = None) -> float:
    base = ROUND_TRIP_COST_BPS / 10000.0
    sb = _num(spread_bps)
    if sb is None or sb < 0:
        return base
    # Spread is paid once over a buy->sell round trip (half on each side).
    # Preserve the configured commission/slippage floor if it is more conservative.
    spread_cost = sb / 10000.0
    return max(base, spread_cost)


def minimum_expected_move_pct(modeled_cost: float, asset: Optional[str] = None) -> float:
    """Canonical move floor; native proof and net profit/RR remain separate gates."""
    multiple = VC.entry_cost_multiple(asset) if asset is not None else MIN_MOVE_COST_MULTIPLE
    return max(MIN_EXPECTED_MOVE_PCT, multiple * max(0.0, _num(modeled_cost, 0.0)))


def paper_quote_time_gate(raw: Optional[Dict[str, Any]], horizon=None, now=None, *, protective=False) -> Dict[str, Any]:
    """Freshness gate for simulated paper execution.

    Delayed research feeds are admissible only when their upstream source gate
    has already certified the observation. Live/production does not use this
    helper and remains fail-closed on delayed feeds.
    """
    from veritas_quote_time import quote_gate, utc_datetime
    r=dict(raw or {})
    observed=r.get("observed_at") or r.get("market_observed_at")
    asset=str(r.get("asset") or "")
    latency=str(r.get("data_latency_class") or "").upper()
    clock=utc_datetime(now) or datetime.now(timezone.utc)
    def book_checked(result):
        book_at=next((r[key] for key in ('orderbook_observed_at','book_observed_at','orderbook_ts')
                      if r.get(key) is not None),None)
        if book_at is not None:
            book=quote_gate(book_at,horizon,now=clock,protective=protective,
                            execution=not protective,asset=asset)
            result=dict(result,orderbook_time_gate=book)
            if not book['eligible']:
                result.update(eligible=False,reason='EXECUTION_ORDERBOOK_STALE')
        return result
    if latency=="DELAYED_RESEARCH":
        dt=utc_datetime(observed); clock=utc_datetime(now) or datetime.now(timezone.utc)
        age=(clock-dt).total_seconds() if dt else None
        limit=3600.0
        ok=bool(r.get("source_gate_pass") and r.get("market_open") is not False
                and age is not None and math.isfinite(age) and -5 <= age <= limit)
        return book_checked({"eligible":ok,"observed_at":dt.isoformat() if dt else None,
                "age_seconds":age,"max_age_seconds":limit,
                "reason":None if ok else "QUOTE_TIME_MISSING" if dt is None else
                "QUOTE_TIME_FUTURE" if age is not None and age < -5 else
                "DELAYED_RESEARCH_QUOTE_STALE",
                "paper_delayed_research":True})
    return book_checked(quote_gate(observed,horizon,now=clock,protective=protective,
                      execution=not protective,asset=asset))


STRUCTURAL_ECONOMICS_VERSION = "PAPER_STRUCTURAL_WEIGHTED_ECONOMICS_V1"


def _structural_economics_terms(asset, plan, direction, entry, execution_mode, now):
    """A policy flag alone cannot waive RR: require the sealed causal evidence."""
    import veritas_structural_breakout as SB
    p = plan or {}
    ctx = p.get('timeframe_entry_context') or p.get('trend_entry_context') or {}
    configured = CTC.BREAKOUT_LIFECYCLE_POLICY
    legacy = dict(version='LEGACY_SINGLE_TARGET_ECONOMICS', execution_mode=execution_mode,
                  minimum_reward_risk=MIN_REWARD_RISK, net_rr_role='HARD_FLOOR')
    if not isinstance(ctx, dict):
        return legacy, [], ['STRUCTURAL_ECONOMICS_PROOF_INVALID'], None
    claimed = SB.applies(ctx) or p.get('structural_policy_version') == configured['version']
    if not claimed:
        return legacy, [], [], None
    def failed(reason):
        return legacy, [], [reason], None
    stamp = SB.TS.timestamp(datetime.now(timezone.utc) if now is None else now)
    if stamp is None:
        return failed('STRUCTURAL_ECONOMICS_DECISION_TIME_REQUIRED')
    try:
        clock = datetime.fromtimestamp(stamp, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return failed('STRUCTURAL_ECONOMICS_DECISION_TIME_REQUIRED')
    checked = SB.context_gate(ctx, clock)
    if not checked.get('eligible'):
        return failed(checked.get('reason') or 'STRUCTURAL_ECONOMICS_PROOF_INVALID')
    event = ctx.get('event') or {}
    if (not configured.get('enabled') or configured.get('scope') != 'PAPER_PORTFOLIOS'
            or event.get('version') != SB.VERSION
            or (event.get('policy') or {}).get('version') != configured['version']
            or ctx.get('asset') != asset or event.get('asset') != asset
            or event.get('direction') != direction
            or p.get('horizon') != ctx.get('timeframe')):
        return failed('STRUCTURAL_ECONOMICS_PROOF_INVALID')
    fresh = SB.entry_gate(ctx, entry, direction, clock)
    if not fresh.get('eligible'):
        return failed(fresh.get('reason') or 'STRUCTURAL_ECONOMICS_PROOF_INVALID')
    quote = ctx.get('quote') or {}
    if entry != _num(quote.get('price')):
        return failed('STRUCTURAL_ECONOMICS_QUOTE_MISMATCH')
    if (p.get('market_observed_at') is not None and
            SB.TS.timestamp(p['market_observed_at']) != ctx.get('quote_observed_at')):
        return failed('STRUCTURAL_ECONOMICS_QUOTE_MISMATCH')
    for field in ('best_bid', 'best_ask'):
        if _num(p.get(field)) != _num(quote.get(field)):
            return failed('STRUCTURAL_ECONOMICS_QUOTE_MISMATCH')
    ladder = event.get('target_ladder') or []
    weights = [1.0] if len(ladder) == 1 else list(configured['target_fractions'])
    if (len(ladder) not in (1, 2) or p.get('target_ladder') != ladder
            or p.get('target_price') != event.get('target_price')
            or p.get('runner_target_price', event.get('runner_target_price')) != event.get('runner_target_price')
            or any(_num(step.get('fraction')) != weight for step, weight in zip(ladder, weights))):
        return failed('STRUCTURAL_TARGET_LADDER_PROVENANCE_MISMATCH')
    # LIVE retains the existing single-target economics and hard RR floor.
    if execution_mode != 'PAPER':
        return legacy, [], [], None
    policy = dict(version=STRUCTURAL_ECONOMICS_VERSION, execution_mode='PAPER',
                  scope='PAPER_PORTFOLIOS', structural_policy_version=configured['version'],
                  event_id=event['event_id'], event_proof_hash=event['proof_hash'],
                  evaluated_at=clock.isoformat(), minimum_reward_risk=0.0,
                  net_rr_role='DIAGNOSTIC_WITH_POSITIVE_WEIGHTED_TARGET_ECONOMICS')
    return policy, deepcopy(ladder), [], deepcopy(ctx)


def economics_gate(asset: str, plan: Optional[Dict[str, Any]], *,
                   execution_mode='PAPER', now=None) -> Dict[str, Any]:
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
    mode = str(execution_mode).upper()
    econ_policy, target_ladder, proof_blockers, proof_context = _structural_economics_terms(
        asset, p, direction, entry, mode, now)
    blockers.extend(proof_blockers)
    rr_floor = econ_policy['minimum_reward_risk']
    weighted = bool(target_ladder)
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
    if forecast_rr is None or forecast_rr < rr_floor:
        blockers.append("RR_BELOW_FINAL_FLOOR")

    spread_bps = _num(p.get("spread_bps"))
    modeled_cost = round_trip_cost_pct(spread_bps)
    stop_distance = abs(entry - stop) / entry if valid_entry and stop else None
    reward = risk = net_rr = target_move = entry_fill = target_fill = stop_fill = None
    fees = funding = slippage = entry_model = hold = age = None
    exit_models = []
    weighted_target = weighted_fill = weighted_move = None
    if valid_entry and stop and stop > 0 and target and target > 0:
        fraction = max(0.0, _num(p.get("initial_position_fraction"), 0.10))
        commission = VC.COMMISSION_RATE
        buy = direction == "LONG"
        entry_model = simulated_fill(asset, "BUY" if buy else "SELL_SHORT", entry,
                                     fraction, bid=p.get("best_bid"), ask=p.get("best_ask"))
        entry_fill = entry_model["fill_price"]
        # The current exit engine uses an adverse reference-price fill; use that
        # same model here, including size impact and both commission legs.
        stop_fill = simulated_fill(asset, "SELL" if buy else "BUY_TO_COVER", stop,
                                   fraction * stop / entry_fill)["fill_price"]
        hold = max(0.0, _num(p.get("expected_hold_seconds"),
                   {"5m": 300, "1h": 3600, "4h": 14400, "1d": 86400,
                    "3d": 259200, "7d": 604800}.get(p.get("horizon"), 3600)))
        age = max(0.0, _num(p.get('position_age_seconds'),0.0))
        funding = entry_fill * (VC.funding_fraction(age+hold)-VC.funding_fraction(age))
        exits = target_ladder if weighted else [dict(price=target, fraction=1.0, kind='TP1')]
        weighted_target = weighted_fill = exit_fees = exit_slippage = 0.0
        for step in exits:
            weight, level = float(step['fraction']), float(step['price'])
            model = simulated_fill(asset, 'SELL' if buy else 'BUY_TO_COVER', level,
                                   fraction * weight * level / entry_fill)
            fill = model['fill_price']
            entry_fee, exit_fee, leg_funding = weight * commission * entry_fill, weight * commission * fill, weight * funding
            leg_reward = weight * sign * (fill-entry_fill) - entry_fee - exit_fee - leg_funding
            exit_models.append(dict(kind=step.get('kind'), price=level, fraction=weight,
                execution_model=model, modeled_fill=fill,
                exit_fraction_nav=fraction * weight * level / entry_fill,
                entry_commission_per_unit=entry_fee, exit_commission_per_unit=exit_fee,
                expected_funding_per_unit=leg_funding, expected_hold_seconds=hold,
                net_reward_per_unit=leg_reward, net_reward_pct=leg_reward / entry_fill,
                leg_net_return_pct=leg_reward / (weight * entry_fill)))
            weighted_target += weight * level
            weighted_fill += weight * fill
            exit_fees += exit_fee
            exit_slippage += weight * abs(fill-level)
        target_fill = exit_models[0]['modeled_fill']
        fees = commission * entry_fill + exit_fees
        slippage = (abs(entry_fill-entry) + exit_slippage) / entry
        modeled_cost = max(modeled_cost, slippage + (fees + funding) / entry)
        reward = (sign * (weighted_fill-entry_fill) - fees - funding) / entry_fill
        risk = (sign * (entry_fill-stop_fill) + commission * (entry_fill+stop_fill)
                + funding) / entry_fill
        net_rr = reward / risk if risk > 0 else None
        target_move = sign * (target-entry) / entry
        weighted_move = sign * (weighted_target-entry) / entry
        if reward <= 0:
            blockers.append("TARGET_NOT_PROFITABLE_AFTER_COSTS")
        if net_rr is None or net_rr < rr_floor:
            blockers.append("NET_REWARD_RISK_BELOW_FLOOR")
        if weighted:
            import veritas_structural_breakout as SB
            fill_check = SB.entry_gate(proof_context, entry_fill, direction,
                                       econ_policy['evaluated_at'])
            if not fill_check.get('eligible'):
                blockers.append(fill_check.get('reason') or 'STRUCTURAL_FILL_TIMING_INVALID')

    min_move = minimum_expected_move_pct(modeled_cost, asset)
    available_move = weighted_move if weighted else target_move
    effective_move = min(abs(forecast_move), available_move) if forecast_move is not None and available_move is not None else None
    if effective_move is None or effective_move < min_move:
        blockers.append("EXPECTED_MOVE_BELOW_COST_BUFFER")
    return {
        "status": "BLOCK" if blockers else "PASS", "eligible": not blockers,
        "asset": str(asset or ""), "blockers": blockers,
        "cost_policy": VC.policy(asset),
        "modeled_commission_pct": fees / entry if fees is not None else None,
        "modeled_execution_cost_pct": slippage,
        "modeled_funding_pct": funding / entry if funding is not None else None,
        "expected_hold_seconds":hold,"position_age_seconds":age,
        "expected_to_stop_ratio": net_rr, "forecast_reward_risk": forecast_rr,
        "minimum_reward_risk": rr_floor,
        "expected_move_pct": effective_move, "forecast_move_pct": forecast_move,
        "minimum_expected_move_pct": min_move,
        "minimum_move_cost_multiple": VC.entry_cost_multiple(asset),
        "modeled_round_trip_cost_pct": modeled_cost,
        "observed_spread_bps": spread_bps, "stop_distance_pct": stop_distance,
        "target_price": target, "target_distance_pct": target_move,
        "modeled_entry_fill": entry_fill, "modeled_target_fill": target_fill,
        "entry_execution_model": entry_model,
        "modeled_stop_fill": stop_fill, "net_reward_pct": reward, "net_risk_pct": risk,
        "net_reward_risk": net_rr,
        "direction":direction, "entry_reference_price":entry, "stop_price":stop,
        "evaluated_fraction_nav":max(0.0, _num(p.get('initial_position_fraction'), .10)),
        "economics_policy":econ_policy, "structural_economics_context":proof_context,
        "target_ladder":deepcopy(target_ladder), "target_execution_models":exit_models if weighted else [],
        "runner_target_price":target_ladder[-1]['price'] if weighted else target,
        "weighted_target_price":weighted_target if weighted else None,
        "modeled_weighted_target_fill":weighted_fill if weighted else None,
        "weighted_target_distance_pct":weighted_move if weighted else None,
        "principle": "Actual target/stop economics after adverse fills, commission and funding.",
    }


def stored_position_target_price(position):
    """Match the held executable TP; a mutable last-signal target is not evidence."""
    payload = (position or {}).get('payload') or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None
    if payload.get('r17_tp1_done'):
        # The remaining runner no longer executes this already-harvested TP.
        return None
    target = _num(payload.get('take_price') or payload.get('target_price'))
    return target if target is not None and target > 0 else None


def paper_structural_economics_validated(gate):
    """Recheck a sizing proof using its frozen quote and explicit decision time.

    This pure verification is not entry permission and does not refresh a quote.
    A forged zero floor, altered ladder or incomplete costs cannot enlarge size.
    """
    g = gate if isinstance(gate,dict) else {}
    policy = g.get('economics_policy') or {}
    if (not isinstance(policy,dict) or policy.get('version') != STRUCTURAL_ECONOMICS_VERSION
            or policy.get('execution_mode') != 'PAPER' or policy.get('scope') != 'PAPER_PORTFOLIOS'):
        return False
    context = g.get('structural_economics_context') or {}
    if not isinstance(context,dict):
        return False
    quote = context.get('quote') or {}
    if not isinstance(quote,dict):
        return False
    plan = dict(timeframe_entry_context=context, horizon=context.get('timeframe'),
                direction=g.get('direction'), entry_price=g.get('entry_reference_price'),
                stop_price=g.get('stop_price'), target_price=g.get('target_price'),
                runner_target_price=g.get('runner_target_price'), target_ladder=g.get('target_ladder'),
                expected_move_pct=g.get('forecast_move_pct'),
                expected_to_stop_ratio=g.get('forecast_reward_risk'),
                initial_position_fraction=g.get('evaluated_fraction_nav'),
                expected_hold_seconds=g.get('expected_hold_seconds'),
                position_age_seconds=g.get('position_age_seconds'),
                best_bid=quote.get('best_bid'), best_ask=quote.get('best_ask'),
                spread_bps=g.get('observed_spread_bps'), market_observed_at=context.get('quote_observed_at'))
    try:
        expected = economics_gate(g.get('asset'), plan, execution_mode='PAPER', now=policy.get('evaluated_at'))
        fields = ('economics_policy', 'target_ladder', 'target_execution_models',
                  'entry_execution_model', 'modeled_entry_fill', 'modeled_stop_fill',
                  'modeled_target_fill', 'modeled_weighted_target_fill', 'weighted_target_price',
                  'weighted_target_distance_pct', 'net_reward_pct', 'net_risk_pct', 'net_reward_risk',
                  'modeled_commission_pct', 'modeled_execution_cost_pct', 'modeled_funding_pct',
                  'modeled_round_trip_cost_pct', 'expected_move_pct', 'minimum_expected_move_pct',
                  'minimum_reward_risk')
        return bool(expected.get('eligible') and all(g.get(k) == expected.get(k) for k in fields))
    except (TypeError, ValueError, KeyError, OverflowError, AttributeError):
        return False


def entry_gate(row, price, direction, fraction, position=None, existing_target_price=None, now=None,
               *, execution_fraction=None):
    """Last check after all setup/sizing mutations, immediately before any order."""
    from veritas_quote_time import quote_gate
    row = VPS.execution_row(row)
    import veritas_trend_entry as VTE
    import veritas_timeframe_policy as TFP
    clock = TFP._decision_clock(datetime.now(timezone.utc) if now is None else now)
    if clock is None:
        return {"eligible":False, "status":"BLOCK", "blockers":["SAME_TF_DECISION_TIME_REQUIRED"]}
    execution=VPS.quote_from_row(row)
    if '_execution_quote' in row:
        price=_num(execution.get('price'))
    price=_num(price)
    if price is None or price<=0:
        return {'eligible':False,'status':'BLOCK','blockers':['ENTRY_PRICE_INVALID']}
    row = VTE.prepare_row(row, price, clock)
    plan = dict(row.get('trade_plan') or {})
    import veritas_structural_breakout as SB
    sb_context = TFP.context_of(row)
    structural_add = bool(position and SB.applies(sb_context))
    if position:
        if structural_add:
            import veritas_stop_risk as VSR
            held_stop = VSR.effective_stop_price(position)
            held_target = _num((sb_context.get('event') or {}).get('target_price'))
        else:
            held_stop = _num(position.get('stop_price'))
            held_target = stored_position_target_price(position)
        add_blockers = []
        if held_stop is None or held_stop <= 0:
            add_blockers.append('ADD_STORED_STOP_REQUIRED')
        if held_target is None:
            add_blockers.append('ADD_STORED_TARGET_REQUIRED')
        elif not structural_add and existing_target_price is not None and _num(existing_target_price) != held_target:
            add_blockers.append('ADD_STORED_TARGET_MISMATCH')
        px = _num(price)
        sign = 1 if direction == 'LONG' else -1
        if px and px > 0:
            if held_stop and sign*(px-held_stop) <= 0:
                add_blockers.append('STOP_DIRECTION_INVALID')
            if held_target and sign*(held_target-px) <= 0:
                add_blockers.append('TARGET_DIRECTION_INVALID')
        if add_blockers:
            return {'eligible':False, 'status':'BLOCK', 'blockers':add_blockers,
                    'target_price':held_target,
                    'add_geometry_basis':'STORED_POSITION_STOP_TARGET',
                    'entry_geometry':{'eligible':False, 'reason':add_blockers[0],
                                      'stop_price':held_stop, 'target_price':held_target}}
        plan.update(stop_price=held_stop, target_price=held_target)
    if position and TFP.applies(row):
        geometry=TFP.geometry(dict(row,trade_plan=plan),price,direction,held_stop,
                              existing_target_price=None if structural_add else held_target)
    else:
        geometry=VTE.geometry(dict(row,trade_plan=plan),price,direction,
                              position.get('stop_price') if position else None)
        if position and geometry.get('eligible'):
            # Historical event geometry must not restore a different target at
            # this final accounting boundary either.
            risk=sign*(float(price)-held_stop)/float(price)
            room=sign*(held_target-float(price))/float(price)
            geometry=dict(geometry, stop_price=held_stop, target_price=held_target,
                          remaining_move_pct=room, stop_distance_pct=risk,
                          reward_risk=room/risk, runner_target_price=held_target)
    if (geometry.get('eligible') or geometry.get('reason')=='R66_SENIOR_BREAK_NOT_HELD') and VTE.has_geometry_context(row):
        forecast=_num(plan.get('expected_move_pct'))
        room = (geometry.get('weighted_remaining_move_pct', geometry['remaining_move_pct'])
                if SB.applies(sb_context) else geometry['remaining_move_pct'])
        expected=min(forecast,room) if forecast is not None and forecast>=0 else room
        plan.update(stop_price=geometry['stop_price'],target_price=geometry['target_price'],
                    expected_move_pct=expected,expected_to_stop_ratio=geometry['reward_risk'])
    fill_fraction=fraction if execution_fraction is None else execution_fraction
    plan.update(entry_price=price, direction=direction, initial_position_fraction=fill_fraction,
                horizon=row.get('horizon'), best_bid=execution.get('best_bid',execution.get('bid')),
                best_ask=execution.get('best_ask',execution.get('ask')),spread_bps=execution.get('spread_bps'))
    funding_age_valid=True
    if position:
        opened=TFP._utc_time(position.get('opened_at'))
        funding_age_valid=opened is not None and opened<=clock
        if funding_age_valid:
            plan['position_age_seconds']=(clock-opened).total_seconds()
    gate = economics_gate(row.get('asset'), plan, now=clock)
    if position:
        gate['add_geometry_basis']='HELD_EFFECTIVE_STOP_NEW_HISTORICAL_TARGETS' if structural_add else 'STORED_POSITION_STOP_TARGET'
    timing = paper_quote_time_gate(
        dict(execution,asset=row.get('asset')),
        row.get('horizon'), now=clock)
    gate['entry_geometry']=geometry
    if is_proxy_price(row.get('asset'),execution or row):
        gate.update(eligible=False,status='BLOCK')
        gate['blockers'].append('R67_DIRECT_NQ_QUOTE_REQUIRED')
    # Canonical same-timeframe entries and adds check extension at the modeled
    # adverse fill. Historical paths retain their observed-quote timing check.
    timing_price=(gate.get('modeled_entry_fill') or price) if TFP.applies(row) else price
    event=(VTE.context_gate(row,clock) if position and not TFP.applies(row) else
           VTE.event_gate(row,timing_price,direction,clock))
    gate['trend_event']=event
    if not event.get('eligible'):
        gate.update(eligible=False,status='BLOCK')
        gate['blockers'].append(event['reason'])
    if VTE.has_geometry_context(row) and not geometry.get('eligible'):
        gate['eligible']=False;gate['status']='BLOCK';gate['blockers'].append(geometry['reason'])
    gate['quote_time_gate'] = timing
    if not timing['eligible']:
        gate['eligible'] = False
        gate['status'] = 'BLOCK'
        gate['blockers'].append(timing['reason'])
    source=paper_source_gate(row.get('asset'),execution)
    gate['source_gate']=source
    if not source.get('eligible'):
        gate.update(eligible=False,status='BLOCK')
        gate['blockers'].extend(source.get('blockers') or [])
    if execution.get('asset') not in (None,row.get('asset')):
        gate.update(eligible=False,status='BLOCK')
        gate['blockers'].append('EXECUTION_QUOTE_ASSET_MISMATCH')
    gate['blockers']=list(dict.fromkeys(gate['blockers']))
    if not funding_age_valid:
        gate.update(eligible=False,status='BLOCK')
        gate['blockers'].append('EXISTING_FUNDING_AGE_REQUIRED')
    gate['execution_snapshot']=VES.capture(row,execution,direction,fill_fraction,clock,gate)
    if gate['execution_snapshot'] is None:
        gate.update(eligible=False,status='BLOCK')
        gate['blockers'].append('EXECUTION_SNAPSHOT_INVALID')
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

    base_bps = VC.SLIPPAGE_RATE * 10000.0
    size_bps = 0.0  # R82 fixed 0.04% slippage per side at every paper position size.

    b = _num(bid)
    a = _num(ask)
    quote_valid = bool(b is not None and a is not None and b > 0 and a > b)
    if quote_valid:
        executable_quote = a if is_buy else b
        mid = 0.5 * (a + b)
        spread_bps = (a - b) / mid * 10000.0 if mid > 0 else None
        # Quote already includes spread. Add only residual adverse slippage + size impact.
        residual_bps = base_bps
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
        "cost_policy_version": VC.VERSION,
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
    # This explicit mode cannot inherit the PAPER structural RR diagnostic rule.
    econ = economics_gate(asset, plan, execution_mode='LIVE')
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
