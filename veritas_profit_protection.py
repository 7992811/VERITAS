"""Whole-trade net result at the existing structural stop, for paper positions."""
from datetime import datetime, timezone
import json
import math

import veritas_costs as VC
import veritas_execution as VX
import veritas_protection_read_model as PR
from veritas_quote_time import utc_datetime

VERSION = 'NET_STOP_AFTER_COSTS_V1'


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def payload(z):
    value = (z or {}).get('payload') or {}
    return json.loads(value) if isinstance(value, str) else dict(value)


def effective_stop(z):
    stops = [number(x) for x in (z.get('stop_price'), payload(z).get('trailing_stop'))]
    stops = [x for x in stops if x is not None and x > 0]
    return (max(stops) if z.get('direction') == 'LONG' else min(stops)) if stops else None


def evaluate(z, accounting, *, stop=None, price=None, nav=None, now=None, commission=VC.COMMISSION_RATE):
    """Fees/funding already booked are subtracted once, across all partial fills.

    Outstanding funding accrues to this observation; future holding time and gaps
    cannot be guaranteed. The exit uses the same adverse fill as the paper book.
    """
    now = utc_datetime(now) or datetime.now(timezone.utc)
    a = accounting or {}
    stop = number(stop) if stop is not None else effective_stop(z)
    units, entry = number(z.get('units')), number(z.get('avg_entry_price'))
    mark = number(price if price is not None else z.get('last_price'))
    nav = number(nav if nav is not None else a.get('portfolio_nav_rub'))
    gross, fees, funding = [number(a.get(k)) for k in ('gross_pnl_rub', 'fees_rub', 'funding_rub')]
    rate = number(commission)
    opened = utc_datetime(z.get('opened_at') or a.get('opened_at') or payload(z).get('entry_time'))
    last_mark = utc_datetime(a.get('last_mark_at'))
    direction = z.get('direction')
    result = {'version': VERSION, 'state': 'UNAVAILABLE', 'checked_at': now.isoformat(),
              'net_at_stop_rub': None, 'break_even_stop_price': None, 'stop_price': stop,
              'units': units, 'avg_entry_price': entry,
              'reason': 'ACCOUNTING_OR_STOP_MISSING', 'estimate_only': True}
    def finish():
        protected = result['state'] == 'PROTECTED'
        return {'profit_protection_active': protected, 'net_profit_protection': result,
                'trailing_stage': 'NET_PROFIT_PROTECTED' if protected else
                'PROTECTION_UNVERIFIED' if result['state'] == 'UNAVAILABLE' else 'RISK_REDUCTION_STRUCTURAL'}
    if (direction not in ('LONG', 'SHORT') or a.get('status') == 'CLOSED'
            or any(x is None for x in (units, entry, mark, nav, stop, gross, fees, funding, rate))
            or min(units, entry, mark, nav, stop) <= 0 or min(fees, funding, rate) < 0
            or rate >= 1 or opened is None or last_mark is None or (last_mark-now).total_seconds() > 5
            or payload(z).get('data_integrity_status', 'OK') not in ('', 'OK')):
        return finish()
    sign = 1 if direction == 'LONG' else -1
    annual = VC.FUNDING_ANNUAL_RATE
    unbooked = VC.funding_between(units * mark, opened, last_mark, now)
    side = 'SELL' if direction == 'LONG' else 'BUY_TO_COVER'

    def exit_result(reference):
        fill = VX.simulated_fill(z['asset'], side, reference, units*reference/nav)['fill_price']
        exit_fee = units * fill * rate
        return gross + sign*units*(fill-entry) - fees - funding - unbooked - exit_fee, fill, exit_fee

    net, fill, exit_fee = exit_result(stop)
    # Invert the actual size-dependent fill model instead of adding a fixed bps buffer.
    required_fill = (sign*units*entry - gross + fees + funding + unbooked) / (units*(sign-rate))
    breakeven = None
    if required_fill > 0:
        low, high = 1e-12, max(entry, stop, required_fill)*2
        for _ in range(50):
            mid = (low+high)/2
            if exit_result(mid)[1] < required_fill:
                low = mid
            else:
                high = mid
        breakeven = (low+high)/2
    elif sign == 1:
        breakeven = 0.0  # Already realized profit covers even a zero-price remainder.
    crossed = (mark <= stop) if sign == 1 else (mark >= stop)
    state = 'STOP_REACHED' if crossed else 'PROTECTED' if net >= .01 else 'COSTS_NOT_COVERED'
    result.update(state=state, reason=None, net_at_stop_rub=net, break_even_stop_price=breakeven,
                  modeled_stop_fill=fill, realized_gross_pnl_rub=gross,
                  booked_commission_rub=fees, booked_funding_rub=funding,
                  unbooked_funding_rub=unbooked, estimated_exit_commission_rub=exit_fee,
                  commission_rate=rate, funding_annual_rate=annual)
    return finish()


def load_accounts(c, ids, include_entry_notional=False, *, include_payload=True):
    """Keep display evidence by default; cost checks can request only accounts."""
    notional_sql = ''' , (SELECT SUM(o.notional_rub) FROM paper_orders o
                         WHERE o.trade_id=t.trade_id AND o.side IN ('BUY','SELL_SHORT'))
                         AS entry_notional_rub''' if include_entry_notional else ''
    payload_sql = ',t.payload' if include_payload else ''
    rows = c.execute('''SELECT t.trade_id,t.status,t.opened_at,t.gross_pnl_rub,t.fees_rub,t.funding_rub''' + payload_sql + ''',
               p.last_mark_at,p.last_ruonia,
               p.initial_nav_rub+p.realized_pnl_rub-p.fees_rub-p.funding_rub+
               COALESCE((SELECT SUM((CASE WHEN z.direction='LONG' THEN 1 ELSE -1 END)*
                                    z.units*(z.last_price-z.avg_entry_price))
                         FROM paper_positions z WHERE z.portfolio_name=p.name),0) AS portfolio_nav_rub''' + notional_sql + '''
        FROM paper_trades t JOIN paper_portfolios p ON p.name=t.portfolio_name
        WHERE t.trade_id=ANY(%s)''', (ids,)).fetchall()
    return {r['trade_id']: dict(r) for r in rows}


def assess(c, z, **kwargs):
    tid = z.get('active_trade_id')
    a = load_accounts(c, [tid], include_payload=False).get(tid) if tid else None
    if a and kwargs.get('nav') is None and kwargs.get('price') is not None:
        values = [number(x) for x in (a.get('portfolio_nav_rub'), z.get('units'), z.get('last_price'), kwargs['price'])]
        if all(x is not None for x in values):
            book_nav, units, previous, current = values
            kwargs['nav'] = book_nav + (1 if z.get('direction') == 'LONG' else -1)*units*(current-previous)
    return evaluate(z, a, **kwargs)


REFRESH_POSITIONS_SQL = '''SELECT z.asset,z.direction,z.units,z.avg_entry_price,z.last_price,
    z.stop_price,z.opened_at,z.active_trade_id,
    CASE WHEN jsonb_typeof(z.payload)='object' THEN
        jsonb_build_object('trailing_stop',p.trailing_stop,'entry_time',p.entry_time) ||
        CASE WHEN z.payload ? 'data_integrity_status' THEN
            jsonb_build_object('data_integrity_status',p.data_integrity_status)
            ELSE '{}'::jsonb END
        ELSE z.payload END AS payload
    FROM paper_positions z CROSS JOIN LATERAL jsonb_to_record(
        CASE WHEN jsonb_typeof(z.payload)='object' THEN z.payload ELSE '{}'::jsonb END
    ) AS p(trailing_stop jsonb,entry_time jsonb,data_integrity_status jsonb)'''


def refresh(c, name=None, now=None, commission=VC.COMMISSION_RATE):
    """Refresh persisted flags after fills/funding, without changing any stop."""
    # The integrity default accepts an absent key, but not explicit JSON null.
    # Non-object JSON retains the original parser and exception behavior.
    rows = c.execute(REFRESH_POSITIONS_SQL + (' WHERE portfolio_name=%s' if name else ''),
                     (name,) if name else ()).fetchall()
    if not rows:
        return
    accounts = load_accounts(c, [z['active_trade_id'] for z in rows], include_payload=False)
    patches = []
    for item in rows:
        z = dict(item)
        patch = evaluate(z, accounts.get(z['active_trade_id']), now=now, commission=commission)
        patches.append((z['active_trade_id'], patch))
    PR.write_patches(c, patches)


def is_protected(z):
    p = payload(z)
    n = p.get('net_profit_protection') or {}
    at = utc_datetime(n.get('checked_at'))
    age = (datetime.now(timezone.utc)-at).total_seconds() if at else None
    return bool(n.get('version') == VERSION and n.get('state') == 'PROTECTED'
                and (number(n.get('net_at_stop_rub')) or 0) >= .01
                and number(n.get('units')) == number(z.get('units'))
                and number(n.get('avg_entry_price')) == number(z.get('avg_entry_price'))
                and number(n.get('stop_price')) == effective_stop(z)
                and age is not None and -5 <= age <= 90)
