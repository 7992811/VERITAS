"""Complete-ledger diagnostics. No orders, balance rewrites or fitted thresholds."""
from collections import defaultdict
from datetime import datetime
import json
import math


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def payload(value):
    if isinstance(value, dict):
        return value
    try:
        result = json.loads(value or '{}')
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def evidence_exclusion(trade):
    p = payload(trade.get('payload'))
    reason = str(trade.get('exit_reason') or p.get('exit_reason') or p.get('close_reason') or '')
    if 'REBASE' in reason.upper():
        return 'ADMINISTRATIVE_EXIT'
    integrity = str(p.get('data_integrity_status') or '').upper()
    if integrity not in ('', 'OK', 'VALID', 'CLEAN') or 'DATA_DISCONTINUITY' in reason.upper():
        return 'DATA_INTEGRITY'
    identity = p.get('contract_identity') or {}
    source = ' '.join(str(x or '') for x in (p.get('entry_primary_source'),
        p.get('entry_data_latency_class'), identity.get('primary_source'))).upper()
    if trade.get('asset') in ('NQ', 'NDX') and ('PROXY' in source or 'QQQ' in source):
        return 'PROXY_PRICE'
    if p.get('recovered') or p.get('learning_eligible') is False:
        return 'INCOMPLETE_EVIDENCE'
    return None


def cohort(trade):
    p = payload(trade.get('payload'))
    if p.get('entry_rule_revision'):
        return str(p['entry_rule_revision'])
    event = str(p.get('r66_event_id') or '')
    if event.startswith('R69_'):
        return 'R69_MINUTE_STRUCTURAL_ENTRY'
    if event.startswith(('R66_', 'R67_', 'R68_')):
        return 'CLOSED_STRUCTURAL_TRIGGER'
    return 'LEGACY_WITHOUT_CLOSED_TRIGGER'


def summarize(rows):
    values = [number(r.get('net_pnl_rub')) for r in rows]
    nets = [x for x in values if x is not None]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    gain, loss = sum(wins), -sum(losses)
    return dict(trades=len(rows), accounting_complete=len(nets), wins=len(wins),
        losses=len(losses), flat=sum(x == 0 for x in nets),
        win_rate=len(wins)/len(nets) if nets else None,
        net_pnl_rub=sum(nets),
        gross_pnl_rub=sum(number(r.get('gross_pnl_rub')) or 0 for r in rows),
        costs_rub=sum((number(r.get('fees_rub')) or 0)+(number(r.get('funding_rub')) or 0) for r in rows),
        profit_factor=gain/loss if loss > 0 else None,
        avg_net_pnl_rub=sum(nets)/len(nets) if nets else None,
        avg_win_rub=gain/len(wins) if wins else None,
        avg_loss_rub=loss/len(losses) if losses else None)


def analyze(rows):
    rows = [dict(r) for r in rows]
    buckets = {k: defaultdict(list) for k in ('portfolio', 'asset', 'horizon', 'exit_reason', 'cohort', 'entry_day', 'evidence')}
    patterns = defaultdict(list)
    diagnostics = defaultdict(int)
    episodes = set()
    for r in rows:
        p = payload(r.get('payload'))
        reason = str(p.get('exit_reason') or p.get('close_reason') or r.get('last_exit_reason') or 'UNKNOWN')
        r['exit_reason'] = reason
        r['cohort'] = cohort(r)
        r['evidence_exclusion'] = evidence_exclusion(r)
        net, gross = number(r.get('net_pnl_rub')), number(r.get('gross_pnl_rub'))
        held = number(r.get('held_seconds'))
        fields = dict(portfolio=r.get('portfolio_name'), asset=r.get('asset'), horizon=r.get('horizon'),
            exit_reason=reason, cohort=r['cohort'], entry_day=str(r.get('opened_at') or '')[:10],
            evidence=r['evidence_exclusion'] or 'NOT_FLAGGED')
        for key, value in fields.items():
            buckets[key][str(value or 'UNKNOWN')].append(r)
        if net is not None and net < 0:
            patterns[(r.get('portfolio_name'),r.get('asset'),r.get('direction'),r.get('horizon'),r.get('setup'),reason)].append(r)
            if gross is not None and gross >= 0: diagnostics['cost_dominated_losses'] += 1
            if held is not None and held < 600: diagnostics['losses_under_10m'] += 1
            if 'STOP' in reason: diagnostics['stop_losses'] += 1
            if 'SIGNAL' in reason or 'FLIP' in reason: diagnostics['signal_exit_losses'] += 1
            mfe = number(p.get('r55_lifetime_mfe_pct', p.get('mfe_pct')))
            basis = number(r.get('entry_notional_rub'))
            # Observed favorable excursion is diagnostic, not an executable fill.
            costs = (number(r.get('fees_rub')) or 0)+(number(r.get('funding_rub')) or 0)
            if mfe is not None and basis and mfe > 100*costs/basis:
                diagnostics['losses_with_observed_mfe_above_booked_costs'] += 1
            if mfe is None: diagnostics['losses_without_mfe_telemetry'] += 1
        if (number(r.get('entry_fill_count')) or 0) > 1: diagnostics['trades_with_adds'] += 1
        if (number(r.get('exit_fill_count')) or 0) > 1: diagnostics['trades_with_partial_exits'] += 1
        if not r['evidence_exclusion']:
            # Cross-portfolio copies of the same market event are not independent tests.
            episodes.add((r.get('asset'),r.get('direction'),
                p.get('r66_event_id') or p.get('canonical_setup_id') or str(r.get('opened_at'))))
    total = summarize(rows)
    grouped = {kind: [dict(key=k, **summarize(v)) for k,v in sorted(groups.items())]
               for kind,groups in buckets.items()}
    loss_patterns = []
    for key, values in patterns.items():
        row = dict(zip(('portfolio_name','asset','direction','horizon','setup','exit_reason'),key))
        row.update(summarize(values))
        durations = [number(x.get('held_seconds')) for x in values]
        durations = [x for x in durations if x is not None]
        row['avg_hold_seconds'] = sum(durations)/len(durations) if durations else None
        loss_patterns.append(row)
    loss_patterns.sort(key=lambda r:r['net_pnl_rub'])
    def example(r):
        p = payload(r.get('payload'))
        fields = ('trade_id','portfolio_name','asset','direction','horizon','setup','opened_at','closed_at',
            'gross_pnl_rub','fees_rub','funding_rub','net_pnl_rub','entry_notional_rub','entry_fill_count',
            'exit_fill_count','held_seconds','exit_reason','cohort','evidence_exclusion')
        result = {k: (r[k].isoformat() if isinstance(r.get(k),datetime) else r.get(k)) for k in fields}
        result.update(mfe_pct=p.get('r55_lifetime_mfe_pct',p.get('mfe_pct')),
            entry_quality=p.get('entry_quality'),entry_source=p.get('entry_primary_source'),
            expected_move_pct=p.get('expected_move_pct'),last_management_reason=p.get('last_management_reason'))
        return result
    latest = sorted(rows,key=lambda r:str(r.get('closed_at') or ''),reverse=True)
    losses = sorted((r for r in rows if (number(r.get('net_pnl_rub')) or 0)<0),key=lambda r:float(r['net_pnl_rub']))
    return dict(status='OK',scope='ALL_CLOSED_TRADES',all_trades=total,
        systemic=dict(losses=total['losses'],total_costs_rub=total['costs_rub'],
            total_net_pnl_rub=total['net_pnl_rub'],**diagnostics),
        groups=grouped,patterns=loss_patterns[:30],independent_unflagged_episodes=len(episodes),
        worst_losses=[example(r) for r in losses[:12]],recent_trades=[example(r) for r in latest[:30]],
        note='Accounting includes every closed trade. Administrative and data-integrity cases are excluded only from strategy evidence. MFE is not a guaranteed realizable profit.')


def audit_closed_trades(conn):
    rows = conn.execute('''SELECT t.*,
        EXTRACT(EPOCH FROM (t.closed_at-t.opened_at)) AS held_seconds,
        o.entry_notional_rub,o.entry_fill_count,o.exit_fill_count,o.last_exit_reason
        FROM paper_trades t LEFT JOIN (
          SELECT trade_id,
            SUM(notional_rub) FILTER(WHERE side IN ('BUY','SELL_SHORT')) AS entry_notional_rub,
            COUNT(*) FILTER(WHERE side IN ('BUY','SELL_SHORT')) AS entry_fill_count,
            COUNT(*) FILTER(WHERE side IN ('SELL','BUY_TO_COVER')) AS exit_fill_count,
            (ARRAY_AGG(reason ORDER BY created_at DESC) FILTER(WHERE side IN ('SELL','BUY_TO_COVER')))[1] AS last_exit_reason
          FROM paper_orders GROUP BY trade_id
        ) o ON o.trade_id=t.trade_id
        WHERE t.closed_at IS NOT NULL OR t.status IN ('CLOSED','CLOSE','EXITED')''').fetchall()
    return analyze(rows)
