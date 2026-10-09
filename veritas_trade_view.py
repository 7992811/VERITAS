"""Read-only trade accounting for the dashboard; no execution decisions."""
import json
import math
import veritas_profit_protection as VPP
import veritas_position_guard as VPG
import veritas_price_source as VPS


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _payload(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or '{}')
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _first_number(*values):
    for value in values:
        number = _number(value)
        if number is not None and number > 0:
            return number
    return None


def _position_management_projection(position):
    """Normalize stop/target presentation without changing execution authority."""
    p = _payload((position or {}).get('payload'))
    event = p.get('active_target_event_snapshot') or p.get('entry_event_snapshot') or {}
    candidates = (
        p.get('active_target_ladder'),
        event.get('target_ladder') if isinstance(event, dict) else None,
        p.get('initial_target_ladder'),
    )
    ladder = []
    for candidate in candidates:
        if not isinstance(candidate, list):
            continue
        cleaned = []
        for step in candidate:
            if not isinstance(step, dict):
                continue
            price = _number(step.get('price'))
            if price is None or price <= 0:
                continue
            cleaned.append({
                'price': price,
                'fraction': _number(step.get('fraction')),
                'kind': step.get('kind'),
            })
        if cleaned:
            ladder = cleaned
            break

    stage = p.get('active_target_stage', 0)
    if not isinstance(stage, int) or isinstance(stage, bool) or stage < 0:
        stage = 0

    stop = VPP.effective_stop(position or {})
    tp1 = ladder[0]['price'] if ladder else _first_number(
        p.get('initial_take_price'), p.get('take_price'),
        p.get('target_price'), p.get('last_target_price'))
    fixed_tp2 = ladder[1]['price'] if len(ladder) > 1 else _first_number(
        p.get('tp2'), p.get('tp2_price'), p.get('second_target_price'))
    runner = _first_number(p.get('runner_target_price'))
    second = fixed_tp2 if fixed_tp2 is not None else runner
    second_kind = ('TP2' if fixed_tp2 is not None else
                   'RUNNER' if runner is not None else
                   'TRAILING_RUNNER' if bool(p.get('r17_tp1_done')) and
                   _first_number(p.get('trailing_stop')) is not None else None)
    next_target = ladder[stage]['price'] if 0 <= stage < len(ladder) else _first_number(
        p.get('take_price'), p.get('target_price'), p.get('last_target_price'))

    structural = bool(p.get('structural_policy_version'))
    missing = []
    if stop is None:
        missing.append('SL')
    if structural and not ladder and tp1 is None:
        missing.append('TARGET_LADDER')
    status = ('PROTECTION_ERROR' if 'SL' in missing else
              'TARGET_PLAN_INCOMPLETE' if missing else 'OK')

    trailing = _first_number(p.get('trailing_stop'))
    hard_stop = _first_number((position or {}).get('stop_price'))
    stop_source = None
    if stop is not None:
        if trailing is not None and abs(stop-trailing) <= max(1e-12, abs(stop)*1e-12):
            stop_source = 'TRAILING_STOP'
        elif hard_stop is not None:
            stop_source = 'HARD_STOP'

    return {
        'effective_stop_price': stop,
        'effective_stop_source': stop_source,
        'tp1_price': tp1,
        'second_take_price': second,
        'second_take_kind': second_kind,
        'next_target_price': next_target,
        'target_stage': stage,
        'target_plan_mode': ('LADDER' if len(ladder) > 1 else
                             'RUNNER' if second_kind in ('RUNNER', 'TRAILING_RUNNER') else
                             'SINGLE_TARGET' if tp1 is not None else 'UNAVAILABLE'),
        'position_management_status': status,
        'position_management_missing': missing,
    }


def trade_result(trade, position=None):
    trade = trade or {}
    payload = dict(_payload(trade.get('payload')))
    payload.update(_payload((position or {}).get('payload')))
    gross, fees, funding = (_number(trade.get(k)) for k in
                            ('gross_pnl_rub', 'fees_rub', 'funding_rub'))
    unrealized = _number(position.get('unrealized_pnl_rub')) if position is not None else 0.0
    complete = all(x is not None for x in (gross, fees, funding, unrealized))
    total = gross + unrealized - fees - funding if complete else None
    # The durable net result is authoritative for a fully closed trade.
    if position is None and str(trade.get('status')) == 'CLOSED':
        total = _number(trade.get('net_pnl_rub')) if trade.get('net_pnl_rub') is not None else total
    # Sum actual opening/add fills. Exits never reduce this denominator: after
    # a partial close the numerator still contains the entire trade's result.
    # Portfolio balance and its historical return field are never substitutes.
    basis = _number(trade.get('entry_notional_rub'))
    basis = basis if basis is not None and basis > 0 else None
    return_pct = 100 * total / basis if total is not None and basis is not None else None
    full_tp = position is None and str(trade.get('exit_reason') or payload.get('exit_reason') or '').startswith('TAKE_PROFIT')
    return {
        'trade_result_status': 'COMPLETE' if complete else 'INCOMPLETE',
        'realized_gross_pnl_rub': gross,
        'trade_fees_rub': fees,
        'trade_funding_rub': funding,
        'total_trade_pnl_rub': total,
        'total_trade_return_pct': return_pct,
        'trade_return_basis': 'ENTRY_NOTIONAL' if basis is not None else 'UNAVAILABLE',
        'trade_return_basis_rub': basis,
        'tp1_done': bool(payload.get('r17_tp1_done') or full_tp),
        'tp1_at': payload.get('r17_tp1_at') or (trade.get('closed_at') if full_tp else None),
        'tp1_partial': bool(payload.get('r17_tp1_done')),
    }


def enrich_positions(report, pg_connect, *, preloaded_accounts=None):
    """Join exact trades, optionally using the position read's accounting snapshot.

    A supplied mapping, including an empty one, is authoritative for this read.
    Querying newer costs or realized P&L after a concurrent fill would mix them
    with the earlier quantities. ``None`` retains the legacy loading behavior.
    """
    out = dict(report)
    out['portfolios'] = [dict(p, positions=[dict(z) for z in p.get('positions') or []])
                         for p in report.get('portfolios') or []]
    positions = [z for p in out['portfolios'] for z in p['positions']]
    ids = list({z['active_trade_id'] for z in positions if z.get('active_trade_id')})
    trades = {} if preloaded_accounts is None else dict(preloaded_accounts)
    if ids and preloaded_accounts is None:
        try:
            with pg_connect() as conn:
                trades = VPP.load_accounts(conn, ids, include_entry_notional=True)
        except Exception:
            # Keep the position visible, but never substitute zero for unknown costs.
            pass
    for position in positions:
        trade=trades.get(position.get('active_trade_id')) or {}
        combined=dict(_payload(trade.get('payload')));combined.update(_payload(position.get('payload')))
        position['payload']=combined
        quote=VPG.quote_for_position(position)
        mark=combined.get('source_locked_mark') or {}
        current=float(quote['price']) if quote else VPS.frozen_price(position)
        position.update(last_price=current,price_source_lock=VPS.position_identity(position),
                        price_source_status='OK' if quote else 'PINNED_SOURCE_QUOTE_UNAVAILABLE',
                        last_mark_at=quote.get('observed_at') or mark.get('observed_at'),
                        mark_source='PINNED_ENTRY_SOURCE' if quote else 'LAST_PINNED_SOURCE_PRICE')
        position['valuation_basis']=VPS.valuation_basis(position,quote)
        entry=_number(position.get('avg_entry_price'));units=_number(position.get('units'))
        if entry and units is not None:
            sign=1 if position.get('direction')=='LONG' else -1
            position.update(notional_rub=abs(units*current),
                            unrealized_pnl_rub=sign*units*(current-entry),
                            unrealized_return_pct=100*sign*(current/entry-1))
        position.update(trade_result(trades.get(position.get('active_trade_id')), position))
        position.update(_position_management_projection(position))
        position.update(VPP.evaluate(position, trades.get(position.get('active_trade_id'))))
    return out
