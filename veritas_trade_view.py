"""Read-only trade accounting for the dashboard; no execution decisions."""
import json
import math


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
    nav = _number(payload.get('entry_nav_rub'))
    return_pct = 100 * total / nav if total is not None and nav is not None and nav > 0 else None
    if position is None and return_pct is None:
        fraction = _number(trade.get('return_on_entry_nav'))
        return_pct = 100 * fraction if fraction is not None else None
    full_tp = position is None and str(trade.get('exit_reason') or payload.get('exit_reason') or '').startswith('TAKE_PROFIT')
    return {
        'trade_result_status': 'COMPLETE' if complete else 'INCOMPLETE',
        'realized_gross_pnl_rub': gross,
        'trade_fees_rub': fees,
        'trade_funding_rub': funding,
        'total_trade_pnl_rub': total,
        'total_trade_return_pct': return_pct,
        'tp1_done': bool(payload.get('r17_tp1_done') or full_tp),
        'tp1_at': payload.get('r17_tp1_at') or (trade.get('closed_at') if full_tp else None),
        'tp1_partial': bool(payload.get('r17_tp1_done')),
    }


def enrich_positions(report, pg_connect):
    """Join by exact trade identity so a reopened position cannot inherit old P&L."""
    out = dict(report)
    out['portfolios'] = [dict(p, positions=[dict(z) for z in p.get('positions') or []])
                         for p in report.get('portfolios') or []]
    positions = [z for p in out['portfolios'] for z in p['positions']]
    ids = list({z['active_trade_id'] for z in positions if z.get('active_trade_id')})
    trades = {}
    if ids:
        try:
            with pg_connect() as conn:
                rows = conn.execute('''SELECT trade_id,gross_pnl_rub,fees_rub,funding_rub,payload
                                       FROM paper_trades WHERE trade_id=ANY(%s)''', (ids,)).fetchall()
            trades = {r['trade_id']: dict(r) for r in rows}
        except Exception:
            # Keep the position visible, but never substitute zero for unknown costs.
            pass
    for position in positions:
        position.update(trade_result(trades.get(position.get('active_trade_id')), position))
    return out
