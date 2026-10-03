"""Comparable, net-of-cost evidence for launch. Never enables live execution."""
from datetime import datetime, timezone
from veritas_trade_audit import summarize, evidence_exclusion, payload, number

COHORT = 'R72_COST_AWARE_STRUCTURAL'


def comparable_trade(row):
    p = payload(row.get('payload'))
    return (p.get('entry_rule_revision') == COHORT and bool(p.get('r66_event_id'))
        and evidence_exclusion(row) is None
        and number(p.get('r55_lifetime_mfe_pct',p.get('mfe_pct'))) is not None
        and number(p.get('r55_lifetime_mae_pct',p.get('mae_pct'))) is not None)


def utc(value):
    try:
        v = value if isinstance(value,datetime) else datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v
    except (TypeError,ValueError):
        return None


def accounting_complete(row):
    values = [number(row.get(k)) for k in ('net_pnl_rub','gross_pnl_rub','fees_rub','funding_rub')]
    if any(v is None for v in values):
        return False
    net,gross,fees,funding = values
    return abs(net-(gross-fees-funding)) <= max(.01,abs(net)*1e-8)


def evaluate_evidence(rows, history, name):
    all_rows = list(rows)
    comparable = [r for r in all_rows if comparable_trade(r)]
    stats = summarize(comparable)
    opened = [utc(r.get('opened_at')) for r in comparable]
    closed = [utc(r.get('closed_at')) for r in comparable]
    first = min((t for t in opened if t),default=None)
    last = max((t for t in closed if t),default=None)
    unique = len({(r.get('asset'),r.get('direction'),payload(r.get('payload'))['r66_event_id']) for r in comparable})
    history = dict(history or {})
    start,end = utc(history.get('first_observation')),utc(history.get('last_observation'))
    coverage = bool(first and last and start and end and all(opened) and all(closed)
        and start <= first.replace(microsecond=0) and end >= last.replace(microsecond=0)
        and history.get('invalid_observations') == 0)
    pf_state = 'FINITE' if stats['profit_factor'] is not None else 'NO_LOSSES' if stats['wins'] else 'NO_PROFITS'
    return dict(portfolio=name,closed_trades=stats['trades'],unique_episodes=unique,
        wins=stats['wins'],win_rate=stats['win_rate'],net_pnl_rub=stats['net_pnl_rub'],
        avg_net_pnl_rub=stats['avg_net_pnl_rub'],profit_factor=stats['profit_factor'],
        profit_factor_state=pf_state,
        costs_rub=stats['costs_rub'],unknown_exits=sum(str(payload(r.get('payload')).get('exit_reason')
            or payload(r.get('payload')).get('close_reason') or '').upper() in ('','UNKNOWN') for r in comparable),
        max_drawdown=number(history.get('max_drawdown')) if coverage else None,
        history_complete=coverage,accounting_complete=bool(comparable) and all(accounting_complete(r) for r in comparable),
        evidence_epoch=first.isoformat() if first else None,execution_cohort=COHORT,
        noncomparable_closed_trades_excluded=len(all_rows)-len(comparable),administrative_rebase_excluded=True)


def candidate_metrics(conn, name):
    rows=conn.execute("""SELECT trade_id,asset,direction,opened_at,closed_at,net_pnl_rub,
        gross_pnl_rub,fees_rub,funding_rub,payload FROM paper_trades
        WHERE portfolio_name=%s AND (closed_at IS NOT NULL OR status IN ('CLOSED','CLOSE','EXITED'))""",(name,)).fetchall()
    first=min((utc(r.get('opened_at')) for r in rows if comparable_trade(r) and utc(r.get('opened_at'))),default=None)
    history={}
    if first:
        history=conn.execute("""WITH series AS (
            SELECT observed_at,nav_rub,MAX(nav_rub) OVER(ORDER BY observed_at) AS hwm
            FROM paper_nav_history WHERE portfolio_name=%s AND observed_at >= %s::timestamptz-INTERVAL '5 minutes'
        ) SELECT MIN(observed_at) AS first_observation,MAX(observed_at) AS last_observation,
            COUNT(*) FILTER(WHERE nav_rub IS NULL OR nav_rub<=0 OR nav_rub IN ('NaN'::float8,'Infinity'::float8)) AS invalid_observations,
            MAX(CASE WHEN hwm>0 THEN (hwm-nav_rub)/hwm ELSE NULL END) AS max_drawdown
          FROM series""",(name,first)).fetchone() or {}
    return evaluate_evidence(rows,history,name)
