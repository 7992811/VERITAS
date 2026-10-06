"""Complete-ledger diagnostics. No orders, balance rewrites or fitted thresholds."""
from collections import defaultdict
from datetime import datetime
import json
import math
import veritas_price_source as VPS


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


def _timestamp(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value) if math.isfinite(value) else None
        except (TypeError, ValueError, OverflowError):
            return None
    try:
        moment=value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return moment.timestamp() if moment.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def observed_event(trade):
    """An event ID or a true flag alone does not prove a closed structural event."""
    p=payload(trade.get('payload'))
    event=p.get('entry_event_snapshot')
    event=event if isinstance(event,dict) else {}
    declared=p.get('idea_event_id') or p.get('r66_event_id')
    event_id=event.get('event_id') or declared
    if not isinstance(event_id,str) or not event_id:
        return None,False
    if event_id.startswith('R79_SIG_') or str(declared or '').startswith('R79_SIG_'):
        return event_id,False
    if declared and declared!=event_id:
        return event_id,False
    if (event.get('event_type')!='SAME_TIMEFRAME_STRUCTURAL_BREAKOUT'
            or not str(event.get('confirmation') or '').startswith('CLOSED_')
            or event.get('asset')!=trade.get('asset')
            or event.get('direction')!=trade.get('direction')):
        return event_id,False
    opening,signal,confirmed,entered=(_timestamp(value) for value in
        (event.get('breakout_bar_at'),event.get('signal_at'),event.get('confirmed_at'),trade.get('opened_at')))
    known=[_timestamp(event.get(key)) for key in
           ('level_available_at','stop_level_available_at','atr_observed_until')]
    if any(value is None for value in (opening,signal,confirmed,entered,*known)):
        return event_id,False
    return event_id,bool(opening<signal<=confirmed<=entered and all(value<=opening for value in known))


def _source_exclusion(trade,p):
    """Use immutable entry identities; never infer a clean historical provider."""
    mark=p.get('source_locked_mark') or {}
    mark=mark if isinstance(mark,dict) else {}
    locks=[p.get(key) for key in ('price_source_lock','entry_execution_source_identity',
                                  'last_exit_source_identity','contract_identity')]
    locks.append(mark.get('identity'))
    names=p.get('entry_source_names') or {}
    names=names if isinstance(names,dict) else {}
    source_parts=[p.get('entry_primary_source'),p.get('entry_data_latency_class'),names.get('primary')]
    for lock in locks:
        if isinstance(lock,dict):
            source_parts.extend(lock.get(key) for key in ('key','primary_source','contract_id'))
        elif lock:
            source_parts.append(str(lock))
    source=' '.join(str(value or '') for value in source_parts).upper()
    if any(token in source for token in ('PROXY','BRIDGE','QQQ','GLD')):
        return 'PROXY_PRICE'
    try:
        expected=VPS.position_identity(trade)
    except (TypeError,ValueError,KeyError):
        return 'SOURCE_UNVERIFIED'
    if (not expected or expected.get('legacy_fixed_adapter')
            or not expected.get('key') or expected.get('asset')!=trade.get('asset')):
        return 'SOURCE_UNVERIFIED'
    if str(expected.get('key')).startswith('TBANK_GRPC:') and not expected.get('contract_id'):
        return 'SOURCE_UNVERIFIED'
    observations=[p.get('last_exit_source_identity'),mark.get('identity')]
    observations=[value for value in observations if value]
    if not observations:
        return 'SOURCE_UNVERIFIED'
    entry=p.get('entry_execution_source_identity')
    if entry:
        observations.append(entry)
    for actual in observations:
        if not isinstance(actual,dict) or not actual.get('key'):
            return 'SOURCE_UNVERIFIED'
        if actual.get('asset')!=trade.get('asset') or not VPS.same(expected,actual):
            return 'DATA_INTEGRITY'
    return None


def evidence_exclusion(trade):
    p = payload(trade.get('payload'))
    reason = str(trade.get('exit_reason') or p.get('exit_reason') or p.get('close_reason') or '')
    if 'REBASE' in reason.upper():
        return 'ADMINISTRATIVE_EXIT'
    integrity = str(p.get('data_integrity_status') or '').strip().upper()
    if (integrity and integrity not in ('OK', 'VALID', 'CLEAN')) or 'DATA_DISCONTINUITY' in reason.upper():
        return 'DATA_INTEGRITY'
    source_problem=_source_exclusion(trade,p)
    if source_problem:
        return source_problem
    if not integrity:
        return 'SOURCE_UNVERIFIED'
    if p.get('recovered') or p.get('learning_eligible') is False:
        return 'INCOMPLETE_EVIDENCE'
    if not observed_event(trade)[1]:
        return 'UNVERIFIED_EVENT'
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
        episode_independence_established=False,
        worst_losses=[example(r) for r in losses[:12]],recent_trades=[example(r) for r in latest[:30]],
        note='Accounting includes every closed trade. Administrative, proxy, source-unverified and event-unverified cases are excluded only from strategy evidence. MFE is not a guaranteed realizable profit.')


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
