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
    if 0 <= stage < len(ladder):
        next_target = ladder[stage]['price']
    elif second_kind in ('TP2', 'RUNNER') and second is not None:
        next_target = second
    elif second_kind == 'TRAILING_RUNNER':
        next_target = None
    else:
        next_target = _first_number(p.get('take_price'), p.get('target_price'),
                                    p.get('last_target_price'))

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



POSITION_PROTECTION_AUDIT_VERSION = 'POSITION_PROTECTION_AUDIT_V1'


def _directional_distance_pct(direction, current, level, *, target=False):
    current, level = _number(current), _number(level)
    if current is None or level is None or current <= 0 or level <= 0:
        return None
    if direction == 'LONG':
        delta = level-current if target else current-level
    elif direction == 'SHORT':
        delta = current-level if target else level-current
    else:
        return None
    return 100.0*delta/current


def _position_protection_audit(position):
    """Audit one open position's complete management contract.

    This is read-only diagnosis. It never authorizes or mutates an order.
    """
    z = position or {}
    p = _payload(z.get('payload'))
    direction = str(z.get('direction') or '')
    current = _number(z.get('last_price'))
    stop = _number(z.get('effective_stop_price'))
    tp1 = _number(z.get('tp1_price'))
    second = _number(z.get('second_take_price'))
    next_target = _number(z.get('next_target_price'))
    second_kind = z.get('second_take_kind')
    target_mode = str(z.get('target_plan_mode') or 'UNAVAILABLE')
    tp1_done = bool(z.get('tp1_done') or p.get('r17_tp1_done'))
    source_identity = z.get('price_source_lock') or p.get('price_source_lock') or {}
    source_status = str(z.get('price_source_status') or 'UNAVAILABLE')
    data_integrity = str(p.get('data_integrity_status') or 'OK')
    management_tf = (p.get('management_horizon') or z.get('management_horizon')
                     or z.get('execution_timeframe') or z.get('horizon'))
    protection = z.get('net_profit_protection') or {}
    protection_state = str(protection.get('state') or 'UNAVAILABLE')

    to_stop = _directional_distance_pct(direction, current, stop, target=False)
    to_target = _directional_distance_pct(direction, current, next_target, target=True)
    fresh = source_status == 'OK'
    stop_breached = bool(fresh and to_stop is not None and to_stop <= 0)
    target_reached = bool(fresh and to_target is not None and to_target <= 0)

    errors, warnings = [], []
    if stop is None:
        errors.append('SL_MISSING')
    elif stop_breached:
        errors.append('STOP_REACHED_OPEN_POSITION')
    if data_integrity not in ('', 'OK'):
        errors.append('DATA_INTEGRITY_'+data_integrity)

    if z.get('position_management_status') == 'TARGET_PLAN_INCOMPLETE':
        warnings.append('TARGET_PLAN_INCOMPLETE')
    if tp1 is None and not tp1_done:
        warnings.append('TP1_MISSING')
    second_ready = bool(second_kind in ('TP2', 'RUNNER') and second is not None)
    trailing_runner = bool(second_kind == 'TRAILING_RUNNER' and
                           _first_number(p.get('trailing_stop')) is not None)
    second_not_required = target_mode == 'SINGLE_TARGET' and tp1 is not None
    if not (second_ready or trailing_runner or second_not_required):
        warnings.append('TP2_OR_RUNNER_UNDEFINED')
    if not source_identity:
        warnings.append('SOURCE_LOCK_MISSING')
    elif source_status != 'OK':
        warnings.append('SOURCE_QUOTE_'+source_status)
    if not management_tf:
        warnings.append('MANAGEMENT_TIMEFRAME_MISSING')
    if protection_state == 'UNAVAILABLE':
        warnings.append('PROFIT_PROTECTION_UNAVAILABLE')
    if target_reached:
        warnings.append('TARGET_REACHED_PENDING_LIFECYCLE')

    status = 'ERROR' if errors else 'PARTIAL' if warnings else 'OK'
    source_check = ('OK' if source_identity and source_status == 'OK' else
                    'PARTIAL' if source_identity else 'MISSING')
    if data_integrity not in ('', 'OK'):
        source_check = 'ERROR'
    tp2_check = ('OK' if second_ready or trailing_runner else
                 'NOT_REQUIRED' if second_not_required else 'PARTIAL')
    profit_check = ('OK' if protection_state == 'PROTECTED' else
                    'ACTION' if protection_state == 'STOP_REACHED' else
                    'WAITING' if protection_state == 'COSTS_NOT_COVERED' else
                    'PARTIAL')

    return {
        'version': POSITION_PROTECTION_AUDIT_VERSION,
        'status': status,
        'errors': errors,
        'warnings': warnings,
        'checks': {
            'sl': {'status':'ERROR' if stop is None or stop_breached else 'OK',
                   'price':stop, 'source':z.get('effective_stop_source'),
                   'distance_pct':to_stop, 'reached':stop_breached},
            'tp1': {'status':'DONE' if tp1_done else 'OK' if tp1 is not None else 'PARTIAL',
                    'price':tp1, 'done':tp1_done},
            'tp2_or_runner': {'status':tp2_check, 'kind':second_kind,
                              'price':second, 'mode':target_mode},
            'source': {'status':source_check, 'quote_status':source_status,
                       'locked':bool(source_identity)},
            'timeframe': {'status':'OK' if management_tf else 'PARTIAL',
                          'value':management_tf},
            'profit_protection': {'status':profit_check, 'state':protection_state,
                                  'active':bool(z.get('profit_protection_active')),
                                  'net_at_stop_rub':_number(protection.get('net_at_stop_rub'))},
        },
        'management_timeframe': management_tf,
        'distance_to_stop_pct': to_stop,
        'distance_to_next_target_pct': to_target,
        'next_target_price': next_target,
        'stop_reached': stop_breached,
        'target_reached': target_reached,
    }


def _position_protection_summary(report):
    positions = [z for p in report.get('portfolios') or [] for z in p.get('positions') or []]
    counts = {'OK':0, 'PARTIAL':0, 'ERROR':0}
    exceptions = []
    protected_after_costs = 0
    for z in positions:
        audit = z.get('protection_audit') or {}
        status = audit.get('status') if audit.get('status') in counts else 'PARTIAL'
        counts[status] += 1
        if ((audit.get('checks') or {}).get('profit_protection') or {}).get('status') == 'OK':
            protected_after_costs += 1
        if status != 'OK':
            exceptions.append({
                'portfolio': z.get('portfolio_name') or z.get('portfolio'),
                'asset': z.get('asset'),
                'trade_id': z.get('active_trade_id'),
                'status': status,
                'errors': list(audit.get('errors') or []),
                'warnings': list(audit.get('warnings') or []),
                'distance_to_stop_pct': audit.get('distance_to_stop_pct'),
                'distance_to_next_target_pct': audit.get('distance_to_next_target_pct'),
            })
    overall = 'ERROR' if counts['ERROR'] else 'PARTIAL' if counts['PARTIAL'] else 'OK'
    return {
        'version': POSITION_PROTECTION_AUDIT_VERSION,
        'status': overall,
        'open_positions': len(positions),
        'ok': counts['OK'],
        'partial': counts['PARTIAL'],
        'error': counts['ERROR'],
        'protected_after_costs': protected_after_costs,
        'checked_at': report.get('positions_checked_at'),
        'exceptions': exceptions,
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
        protection = position.get('net_profit_protection') or {}
        stop_net = _number(protection.get('net_at_stop_rub'))
        mark_net = _number(position.get('total_trade_pnl_rub'))
        position['mark_to_market_net_pnl_rub'] = mark_net
        position['pnl_if_effective_stop_rub'] = stop_net
        position['stop_scenario_delta_rub'] = (
            stop_net-mark_net if stop_net is not None and mark_net is not None else None)
        position['realized_net_after_booked_costs_rub'] = (
            (_number(position.get('realized_gross_pnl_rub'))
             - _number(position.get('trade_fees_rub'))
             - _number(position.get('trade_funding_rub')))
            if all(_number(position.get(k)) is not None for k in
                   ('realized_gross_pnl_rub','trade_fees_rub','trade_funding_rub'))
            else None)
        position['protection_audit'] = _position_protection_audit(position)

    # NAV remains fair-value / mark-to-market. Stops are a separate liquidation
    # scenario, never a replacement valuation basis. For a complete book,
    # show the capital that would remain if every current effective stop were
    # executed through the same adverse-fill/cost model used by the engine.
    for portfolio in out['portfolios']:
        nav = _number(portfolio.get('nav_rub'))
        rows = portfolio.get('positions') or []
        deltas = [_number(z.get('stop_scenario_delta_rub')) for z in rows]
        complete = nav is not None and all(x is not None for x in deltas)
        if complete:
            nav_at_stops = nav + sum(deltas)
            downside = nav_at_stops-nav
            portfolio.update(
                stop_scenario_status='COMPLETE',
                nav_if_all_stops_rub=nav_at_stops,
                stop_scenario_pnl_delta_rub=downside,
                stop_scenario_loss_rub=max(0.0,-downside),
                stop_scenario_loss_pct_nav=(100.0*max(0.0,-downside)/nav if nav>0 else None),
                valuation_policy='MARK_TO_MARKET_NAV_WITH_SEPARATE_STOP_LIQUIDATION_SCENARIO',
            )
        else:
            portfolio.update(
                stop_scenario_status='UNAVAILABLE',
                nav_if_all_stops_rub=None,
                stop_scenario_pnl_delta_rub=None,
                stop_scenario_loss_rub=None,
                stop_scenario_loss_pct_nav=None,
                valuation_policy='MARK_TO_MARKET_NAV_WITH_SEPARATE_STOP_LIQUIDATION_SCENARIO',
            )
    out['position_protection_audit'] = _position_protection_summary(out)
    return out
