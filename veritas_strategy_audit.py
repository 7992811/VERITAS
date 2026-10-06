"""Independent paper-strategy audit. Never mutates trades, balances or rules."""
from __future__ import annotations
import copy
import hashlib
import json
import math
import threading
import time
from datetime import datetime, timezone

import veritas_strategy_quality as Q

VERSION = 'TRADE_REVIEW_V1'
MAX_ROWS = 10000
_lock = threading.RLock()
_cache = {'status': 'NOT_STARTED', 'portfolios': {}, 'real_orders_enabled': False}
_worker = None


def payload(value):
    if isinstance(value, dict):
        return value
    try:
        value = json.loads(value or '{}')
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def identity(trade):
    p = payload(trade.get('payload'))
    m = payload(payload(p.get('execution_policy')).get('strategy_quality'))
    opened = Q.timestamp(trade.get('opened_at'))
    reference = Q.timestamp(Q.REFERENCE_STARTED_AT)
    recorded = Q.timestamp(m.get('recorded_market_at'))
    # Reject metadata first written by a later add: it must not reclassify an old entry.
    exact = bool(m.get('strategy_epoch') and opened and recorded and -5 <= (opened-recorded).total_seconds() <= 3600)
    epoch = m['strategy_epoch'] if exact else ('SINCE_73266D9_TIME_BASED' if opened and opened >= reference else 'PRE_73266D9')
    key = m.get('idea_id') if exact else None
    if not key:
        source = p.get('price_source_lock') or {}
        bucket = int(opened.timestamp() // 1800) if opened else str(trade.get('trade_id'))
        seed = [trade.get('asset'), trade.get('direction'), source.get('provider'), source.get('contract'), bucket]
        key = hashlib.sha256(json.dumps(seed, sort_keys=True, default=str).encode()).hexdigest()[:24]
    return {'trade_id': str(trade.get('trade_id')), 'strategy_epoch': epoch,
            'entry_git_sha': m.get('entry_git_sha') if exact else None, 'idea_id': key,
            'classification_basis': 'ENTRY_METADATA' if exact else 'ENTRY_TIME_NOT_EXACT_GIT_SHA',
            'opened_at': opened.isoformat() if opened else None}


def review(trade, frozen_identity=None):
    p = payload(trade.get('payload'))
    i = frozen_identity or identity(trade)
    gross, fees, funding = [Q.number(trade.get(k)) for k in ('gross_pnl_rub','fees_rub','funding_rub')]
    net = Q.number(trade.get('net_pnl_rub'))
    if net is None and all(v is not None for v in (gross, fees, funding)):
        net = gross-fees-funding
    basis = Q.number(trade.get('entry_notional_rub'))
    net_pct = 100*net/basis if net is not None and basis is not None and basis > 0 else None
    gross_pct = 100*gross/basis if gross is not None and basis is not None and basis > 0 else None
    mfe, mae = Q.number(p.get('mfe_pct')), Q.number(p.get('mae_pct'))
    capture = gross_pct/mfe if gross_pct is not None and mfe is not None and mfe > 0 else None
    eaten = bool(gross is not None and net is not None and gross > 0 and net <= 0)
    hypotheses = []
    if eaten:
        hypotheses.append('COSTS_CONSUMED_PRICE_PROFIT')
    if net is not None and net < 0 and mfe is not None and mfe <= 0:
        hypotheses.append('ENTRY_OR_DIRECTION_REVIEW')
    if capture is not None and mfe >= .20 and capture < .4:
        hypotheses.append('LOW_CAPTURE_REVIEW_EXITS_AND_PARTIAL_SIZING')
    if not hypotheses:
        hypotheses.append('NO_SINGLE_CAUSE_PROVEN')
    components = ['economics'] if eaten else ['entry'] if 'ENTRY_OR_DIRECTION_REVIEW' in hypotheses else ['exit'] if any('LOW_CAPTURE' in h for h in hypotheses) else []
    return {**i, 'review_version': VERSION, 'portfolio': trade.get('portfolio_name'),
            'asset': trade.get('asset'), 'direction': trade.get('direction'),
            'closed_at': str(trade.get('closed_at') or ''), 'net_pnl_rub': net,
            'gross_pnl_rub': gross, 'fees_rub': fees, 'funding_rub': funding,
            'net_return_on_entry_notional_pct': net_pct, 'mfe_pct': mfe, 'mae_pct': mae,
            'capture_ratio': capture, 'capture_basis': 'REALIZED_GROSS_PER_CUMULATIVE_ENTRY_NOTIONAL_OVER_RECORDED_MFE',
            'capture_caveat': 'RETROSPECTIVE_DIAGNOSTIC_NOT_EXECUTABLE_PEAK; scaling may change comparability',
            'costs_consumed_profit': eaten,
            'exit_reason': p.get('exit_reason') or p.get('close_reason') or trade.get('exit_reason'),
            'initial_stop': p.get('initial_structural_stop') or p.get('entry_stop_price'),
            'stop_assessment': 'REQUIRES_ENTRY_BAR_REPLAY',
            'hypotheses_not_causal_proof': hypotheses, 'components_to_review': components,
            'rule_change': 'NONE', 'promotion': 'SHADOW_ONLY'}


def _wilson(wins, n):
    if not n:
        return None
    z = 1.959963984540054
    p = wins/n
    den = 1+z*z/n
    centre = (p+z*z/(2*n))/den
    radius = z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return [max(0, centre-radius), min(1, centre+radius)]


def metrics(reviews):
    valid = [r for r in reviews if r.get('net_pnl_rub') is not None]
    wins = [r for r in valid if r['net_pnl_rub'] > 0]
    losses = [r for r in valid if r['net_pnl_rub'] < 0]
    gp = sum(r['net_pnl_rub'] for r in wins)
    gl = -sum(r['net_pnl_rub'] for r in losses)
    clusters = {}
    for r in valid:
        clusters.setdefault(r['idea_id'], []).append(r)
    cluster_results = [sum(r['net_pnl_rub'] for r in rows)/len(rows) for rows in clusters.values()]
    cw = sum(v > 0 for v in cluster_results)
    captured = [r['capture_ratio'] for r in valid if r.get('capture_ratio') is not None]
    normalized = [r['net_return_on_entry_notional_pct'] for r in valid if r.get('net_return_on_entry_notional_pct') is not None]
    return {'closed_trades': len(valid), 'wins': len(wins), 'losses': len(losses),
            'net_pnl_rub': sum(r['net_pnl_rub'] for r in valid),
            'win_rate': len(wins)/len(valid) if valid else None,
            'profit_factor': gp/gl if gl > 0 else None,
            'profit_factor_state': 'NO_LOSSES' if gp > 0 and gl == 0 else 'MEASURED' if gl > 0 else 'NO_SAMPLE',
            'avg_win_rub': gp/len(wins) if wins else None,
            'avg_loss_rub': gl/len(losses) if losses else None,
            'payoff_ratio': (gp/len(wins))/(gl/len(losses)) if wins and losses else None,
            'avg_net_pnl_rub': sum(r['net_pnl_rub'] for r in valid)/len(valid) if valid else None,
            'mean_net_return_pct': sum(normalized)/len(normalized) if normalized else None,
            'mean_capture_ratio': sum(captured)/len(captured) if captured else None,
            'cost_eaten_count': sum(r['costs_consumed_profit'] for r in valid),
            'independent_idea_clusters': len(clusters), 'cluster_win_rate': cw/len(clusters) if clusters else None,
            'cluster_win_rate_interval_95': _wilson(cw, len(clusters)),
            'independence_caveat': 'CONSERVATIVE_CLUSTERS_NOT_PROOF_OF_STATISTICAL_INDEPENDENCE',
            'sample_status': 'DIAGNOSTIC_ONLY' if len(clusters) < 50 else 'REQUIRES_OUT_OF_SAMPLE_VALIDATION',
            'live_money_ready': False}


def last_ideas(reviews, count):
    ordered = sorted(reviews, key=lambda r: r.get('closed_at') or '', reverse=True)
    chosen = []
    for r in ordered:
        if r['idea_id'] not in chosen and len(chosen) < count:
            chosen.append(r['idea_id'])
    return [r for r in ordered if r['idea_id'] in chosen]


def build_report(trades, identities=None):
    identities = identities or {}
    closed = [t for t in trades if t.get('closed_at') is not None or t.get('status') == 'CLOSED']
    reviews = [review(t, identities.get(str(t.get('trade_id')))) for t in closed]
    out = {}
    cutoff = Q.timestamp(Q.REFERENCE_STARTED_AT)
    names = set(t.get('portfolio_name') for t in trades if t.get('portfolio_name'))
    for name in names:
        rows = [r for r in reviews if r['portfolio'] == name]
        since = [r for r in rows if Q.timestamp(r.get('opened_at')) and Q.timestamp(r['opened_at']) >= cutoff]
        current = [r for r in rows if r['strategy_epoch'] == Q.VERSION]
        carry = [t for t in trades if t.get('portfolio_name') == name and t.get('closed_at') is None and t.get('status') != 'CLOSED'
                 and (identities.get(str(t.get('trade_id'))) or identity(t))['strategy_epoch'] != Q.VERSION]
        out[name] = {'all_history': metrics(rows), 'since_73266d9': metrics(since),
                     'current_strategy': metrics(current), 'last_20_ideas': metrics(last_ideas(current, 20)),
                     'last_50_ideas': metrics(last_ideas(current, 50)),
                     'legacy_open_positions': len(carry), 'legacy_positions_still_in_nav': True,
                     'last_reviews': sorted(rows, key=lambda r: r['closed_at'], reverse=True)[:5]}
    return {'status': 'OK', 'review_version': VERSION, 'strategy_epoch': Q.VERSION,
            'reference_sha': Q.REFERENCE_SHA, 'reference_started_at': Q.REFERENCE_STARTED_AT,
            'cohort_basis': 'ENTRY_TIME; EXACT SHA ONLY WHEN RECORDED AT ENTRY',
            'as_of': datetime.now(timezone.utc).isoformat(), 'portfolios': out,
            'pooled_diagnostic': metrics(reviews), 'automatic_rule_changes': False,
            'real_orders_enabled': False, 'reviews': reviews}


def snapshot():
    with _lock:
        return copy.deepcopy(_cache)


def refresh(pg_connect, emit=None):
    with pg_connect() as c:
        c.execute("SET LOCAL statement_timeout = '4000ms'")
        c.execute('''CREATE TABLE IF NOT EXISTS strategy_trade_identity (
            trade_id TEXT PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), identity JSONB NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS strategy_trade_reviews (
            trade_id TEXT NOT NULL, review_version TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(), review JSONB NOT NULL,
            PRIMARY KEY(trade_id,review_version))''')
        rows = c.execute('''SELECT t.trade_id,t.portfolio_name,t.asset,t.direction,t.status,
            t.opened_at,t.closed_at,t.gross_pnl_rub,t.fees_rub,t.funding_rub,t.net_pnl_rub,t.payload,
            (SELECT SUM(o.notional_rub) FROM paper_orders o WHERE o.trade_id=t.trade_id
             AND o.side IN ('BUY','SELL_SHORT')) AS entry_notional_rub,
            count(*) OVER() AS source_total
            FROM paper_trades t ORDER BY t.opened_at DESC LIMIT %s''', (MAX_ROWS,)).fetchall()
        trades = [dict(t) for t in rows]
        known = {str(r['trade_id']): payload(r['identity']) for r in c.execute('SELECT trade_id,identity FROM strategy_trade_identity').fetchall()}
        for t in trades:
            tid = str(t['trade_id'])
            if tid not in known:
                frozen = identity(t)
                c.execute('INSERT INTO strategy_trade_identity(trade_id,identity) VALUES(%s,%s::jsonb) ON CONFLICT DO NOTHING',
                          (tid, json.dumps(frozen)))
                known[tid] = frozen
        data = build_report(trades, known)
        added = []
        for r in data.pop('reviews'):
            inserted = c.execute('''INSERT INTO strategy_trade_reviews(trade_id,review_version,review)
                VALUES(%s,%s,%s::jsonb) ON CONFLICT DO NOTHING RETURNING trade_id''',
                (r['trade_id'], VERSION, json.dumps(r, default=str))).fetchone()
            if inserted:
                added.append(r)
        total = int(trades[0]['source_total']) if trades else 0
        data.update(source_trade_count=total, rows_scanned=len(trades), history_truncated=total > MAX_ROWS,
                    review_storage='DURABLE', new_reviews=len(added))
    with _lock:
        _cache.clear()
        _cache.update(data)
    if callable(emit) and added:
        emit('strategy_trade_review_batch', added=len(added), last_reviews=added[:3],
             promotion='SHADOW_ONLY', history_rewritten=False)
    return data


def start(pg_connect, emit=None):
    global _worker
    if not callable(pg_connect):
        return
    with _lock:
        if _worker and _worker.is_alive():
            return
        def loop():
            while True:
                try:
                    refresh(pg_connect, emit)
                except Exception as exc:
                    with _lock:
                        _cache.update(status='DEGRADED', error_type=type(exc).__name__,
                                      real_orders_enabled=False)
                    if callable(emit):
                        try:
                            emit('strategy_trade_review_error', error_type=type(exc).__name__)
                        except Exception:
                            pass
                time.sleep(60)
        _worker = threading.Thread(target=loop, name='veritas-strategy-audit', daemon=True)
        _worker.start()
